# -*- coding: utf-8 -*-
"""V2 处理器：把"污染检测 + 最小编辑恢复"引擎接到文件级流程。"""
from __future__ import annotations

import json
import os
import time

from .processor import read_text
from .t2s import to_simplified
from .v2engine import V2Config, V2Engine, clean_text_v2
from .v2lm import LanguageModel


def clean_filter(line: str) -> bool:
    """构造语言模型时排除污染行（含异常字符的行），避免"教会"模型接受污染。"""
    from . import charclass as cc
    for ch in line:
        if cc.is_invisible(ch):
            return False
        if not cc.is_cjk(ch) and not ch.isascii() and not cc.is_cjk_punct(ch) \
                and not cc.is_space(ch):
            return False
    return True


def get_language_model(files: list[str], cache: str = "", progress=None,
                       rebuild: bool = False, simplify: bool = True) -> LanguageModel:
    if cache and not rebuild and os.path.exists(cache):
        try:
            return LanguageModel.load(cache)
        except Exception:                       # noqa: BLE001
            pass
    texts = []
    for i, p in enumerate(files):
        try:
            t, _enc = read_text(p)
        except OSError:
            continue
        if simplify:
            t = to_simplified(t)
        texts.append(t)
        if progress:
            progress(i + 1, len(files), "读取 %s" % os.path.basename(p))
    lm = LanguageModel().fit(texts, line_filter=clean_filter)
    if cache:
        try:
            lm.save(cache)
        except OSError:
            pass
    return lm


def process_files_v2(files: list[str], out_dir: str, lm: LanguageModel,
                     cfg: V2Config | None = None, progress=None,
                     report_path: str | None = None,
                     same_folder: bool = False,
                     simplify: bool = True) -> list[dict]:
    cfg = cfg or V2Config()
    engine = V2Engine(lm, cfg)
    os.makedirs(out_dir, exist_ok=True)
    reports = []
    for idx, src in enumerate(files):
        if progress:
            progress(idx, len(files), "清洗 %s" % os.path.basename(src))
        base = os.path.basename(src)
        try:
            text, _enc = read_text(src)
        except OSError as e:
            reports.append({"src": src, "error": str(e)})
            continue
        crlf = "\r\n" in text
        cr = (not crlf) and ("\r" in text)
        if simplify:
            text = to_simplified(text)          # 繁体 → 简体（用户要求输出简体）
        t0 = time.time()
        res = clean_text_v2(text, engine, sample_limit=60)
        secs = round(time.time() - t0, 2)
        out_text = res.cleaned
        if crlf:
            out_text = out_text.replace("\n", "\r\n")
        elif cr:
            out_text = out_text.replace("\n", "\r")
        if same_folder:
            stem, ext = os.path.splitext(base)
            dst = os.path.join(os.path.dirname(src), "%s.cleaned%s" % (stem, ext))
        else:
            dst = os.path.join(out_dir, base)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(out_text)

        samples = []
        for lineno, r in res.edits:
            samples.append({
                "line": lineno,
                "before": r.original,
                "after": r.cleaned,
                "edits": [e.as_dict() for e in r.edits],
            })
        reports.append({
            "src": src,
            "dst": dst,
            "lines": res.original.count("\n") + 1,
            "changed_lines": res.changed_lines,
            "deleted_chars": res.removed_chars,
            "replaced_chars": res.replaced_chars,
            "review_pending": res.review_chars,
            "seconds": secs,
            "samples": samples,
        })
        if progress:
            progress(idx + 1, len(files), "%s 改动 %d 行" % (base, res.changed_lines))
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "engine": "v2",
                "config": {
                    "lang_cut": cfg.lang_cut, "garbage_pair": cfg.garbage_pair,
                    "cluster_gain": cfg.cluster_gain, "mutual_ratio": cfg.mutual_ratio,
                    "foot_rare_freq": cfg.foot_rare_freq,
                },
                "language_model": lm.stats(),
                "files": reports,
            }, f, ensure_ascii=False, indent=1)
    return reports
