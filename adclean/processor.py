# -*- coding: utf-8 -*-
"""adclean.processor —— 文件级处理流程（CLI 与 Web UI 共用）。"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict

from .engine import Model, Config, clean_text, TextResult

TXT_EXT = {".txt"}


@dataclass
class FileReport:
    src: str
    dst: str = ""
    size_bytes: int = 0
    lines: int = 0
    changed_lines: int = 0
    removed_chars: int = 0
    invisible_chars: int = 0
    rare_left: int = 0
    exotic_left: int = 0
    seconds: float = 0.0
    samples: list = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("samples", None)
        return d


def read_text(path: str) -> tuple[str, str]:
    """读取文本，返回 (内容, 编码)。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def newline_of(text: str) -> str:
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if crlf >= cr and crlf >= lf and crlf:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def list_txt_files(folder: str, recursive: bool = True) -> list[str]:
    out = []
    if os.path.isfile(folder):
        return [folder]
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in TXT_EXT:
                out.append(os.path.join(root, f))
        if not recursive:
            break
    return out


def build_model(files: list[str], rare_threshold: int = 200, progress=None):
    """从待处理文件本身学习用字 / 搭配统计。"""
    texts = []
    for i, p in enumerate(files):
        try:
            t, _enc = read_text(p)
        except OSError:
            continue
        texts.append(t)
        if progress:
            progress(i + 1, len(files), f"读取 {os.path.basename(p)}")
    m = Model()
    m.fit(texts, rare_threshold=rare_threshold)
    return m


_EXOTIC = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def _count_left(text: str, model: Model):
    from . import charclass as cc
    exotic = len(_EXOTIC.findall(text))
    others = 0
    for ch in text:
        if cc.in_suspect_block(ch):
            others += 1
    rare = 0
    for ch in text:
        if cc.is_cjk(ch) and model.is_rare(ch):
            rare += 1
    return rare, exotic + others


def process_files(files: list[str], out_dir: str, cfg: Config | None = None,
                  same_folder: bool = False, model=None,
                  progress=None, sample_limit: int = 30,
                  report_path: str | None = None) -> tuple[list[FileReport], Model]:
    """批量清洗并写出文件。原文件绝不修改。"""
    cfg = cfg or Config()
    if model is None:
        model = build_model(files, cfg.rare_threshold, progress=progress)

    reports: list[FileReport] = []
    for idx, src in enumerate(files):
        rep = FileReport(src=src)
        rep.size_bytes = os.path.getsize(src)
        try:
            text, enc = read_text(src)
        except OSError as e:
            rep.error = str(e)
            reports.append(rep)
            continue
        nl = newline_of(text)
        t0 = time.time()
        if progress:
            progress(idx, len(files), f"清洗 {os.path.basename(src)}")
        res: TextResult = clean_text(text, model, cfg, sample_limit=sample_limit)
        rep.seconds = round(time.time() - t0, 2)

        base = os.path.basename(src)
        if same_folder:
            stem, ext = os.path.splitext(base)
            dst = os.path.join(os.path.dirname(src), f"{stem}.cleaned{ext}")
        else:
            os.makedirs(out_dir, exist_ok=True)
            dst = os.path.join(out_dir, base)
        # 写回原换行风格
        out_text = res.cleaned
        if nl != "\n":
            out_text = out_text.replace("\n", nl)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(out_text)

        rare_left, exotic_left = _count_left(out_text, model)
        rep.dst = dst
        rep.lines = res.original.count("\n") + 1
        rep.changed_lines = res.changed_lines
        rep.removed_chars = res.removed_chars
        rep.invisible_chars = res.invisible_chars
        rep.rare_left = rare_left
        rep.exotic_left = exotic_left
        rep.samples = res.samples
        reports.append(rep)

    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "model": model.stats(),
                "files": [r.as_dict() for r in reports],
                "samples": [{"file": r.src, **s} for r in reports for s in r.samples],
            }, f, ensure_ascii=False, indent=2)
    return reports, model
