# -*- coding: utf-8 -*-
"""adclean.server —— 零依赖本地 Web UI（Python 标准库 http.server）。

启动：
    python -m adclean.server            # 自动打开浏览器 http://127.0.0.1:8777
    python -m adclean.server --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import difflib
import inspect
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from .processor import list_txt_files, newline_of, read_text

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
STATIC_REAL = os.path.realpath(STATIC)

UPLOAD_ROOT = os.path.join(tempfile.gettempdir(), "adclean_uploads")
OUTPUT_ROOT = os.path.join(tempfile.gettempdir(), "adclean_outputs")

MAX_UPLOAD_FILE_BYTES = 80 * 1024 * 1024      # 单文件 80MB
MAX_UPLOAD_SESSION_BYTES = 300 * 1024 * 1024  # 单会话 300MB
MAX_JOBS = 20
JOB_TTL_SEC = 2 * 3600                        # 2 小时

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_MAX_BODY_BYTES = 10 * 1024 * 1024

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

JOBS: dict[str, dict] = {}
JOB_DATA: dict[str, dict] = {}
CANCEL: dict[str, threading.Event] = {}
JOB_OUTPUTS: dict[str, list[str]] = {}
JOBS_LOCK = threading.RLock()
UPLOAD_LOCK = threading.Lock()
ENGINE_LOCK = threading.Lock()
_ENGINE = {"obj": None, "path": "", "error": ""}


def _configure_stdio() -> None:
    """避免 Windows GBK 管道下打印 UTF-8 字符抛出 UnicodeEncodeError。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(__import__("sys"), stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                                    # noqa: BLE001
                pass


# --------------------------------------------------------------------------- 任务状态与清理
def _cleanup_jobs_locked(now: float | None = None) -> None:
    """在持有 JOBS_LOCK 时按 TTL（2h）和 LRU（最多 20 个已结束任务）清理旧任务。"""
    now = now or time.time()
    finished = [
        (jid, meta)
        for jid, meta in JOBS.items()
        if meta.get("state") not in ("queued", "running")
    ]
    evict_ids: set[str] = set()
    active_remain: list[tuple[str, float]] = []
    for jid, meta in finished:
        ts = float(meta.get("updated") or meta.get("started") or now)
        if now - ts > JOB_TTL_SEC:
            evict_ids.add(jid)
        else:
            active_remain.append((jid, ts))
    if len(active_remain) > MAX_JOBS:
        active_remain.sort(key=lambda x: x[1])
        for jid, _ in active_remain[: len(active_remain) - MAX_JOBS]:
            evict_ids.add(jid)
    for jid in evict_ids:
        JOBS.pop(jid, None)
        JOB_DATA.pop(jid, None)
        CANCEL.pop(jid, None)
        JOB_OUTPUTS.pop(jid, None)


def _cleanup_expired_uploads(now: float | None = None) -> None:
    """清理超时且不再被活跃任务引用的临时上传会话目录。"""
    now = now or time.time()
    if not os.path.isdir(UPLOAD_ROOT):
        return
    with JOBS_LOCK:
        active_files: set[str] = set()
        for meta in JOBS.values():
            if meta.get("state") in ("queued", "running"):
                for p in meta.get("files") or ():
                    active_files.add(os.path.realpath(p))
    with UPLOAD_LOCK:
        try:
            entries = os.listdir(UPLOAD_ROOT)
        except OSError:
            return
        for name in entries:
            sdir = os.path.join(UPLOAD_ROOT, name)
            if not os.path.isdir(sdir):
                continue
            try:
                mtime = os.path.getmtime(sdir)
            except OSError:
                continue
            if now - mtime <= JOB_TTL_SEC:
                continue
            sdir_real = os.path.realpath(sdir)
            in_use = any(
                os.path.commonpath([sdir_real, fp]) == sdir_real
                for fp in active_files
                if os.path.isabs(fp)
            )
            if not in_use:
                shutil.rmtree(sdir, ignore_errors=True)


def _set(job_id: str, **kw) -> None:
    with JOBS_LOCK:
        now = time.time()
        entry = JOBS.setdefault(job_id, {})
        entry.update(kw)
        entry["updated"] = now
        if "progress" in kw and "percent" not in kw:
            entry["percent"] = kw["progress"]
        elif "percent" in kw and "progress" not in kw:
            entry["progress"] = kw["percent"]


def _get(job_id: str) -> dict:
    with JOBS_LOCK:
        _cleanup_jobs_locked()
        raw = JOBS.get(job_id)
        if not raw:
            return {}
        out = dict(raw)
        if out.get("state") in ("queued", "running") and out.get("started"):
            out["elapsed_sec"] = round(max(0.0, time.time() - out["started"]), 2)
        return out


def _cancel_of(job_id: str) -> threading.Event | None:
    with JOBS_LOCK:
        if job_id not in JOBS:
            return None
        ev = CANCEL.get(job_id)
        if ev is None:
            ev = threading.Event()
            CANCEL[job_id] = ev
        return ev


# --------------------------------------------------------------------------- 只读引擎懒加载与并发隔离
def get_engine(lm_path: str = ""):
    """惰性加载只读定位器引擎，进程内线程安全复用。"""
    with ENGINE_LOCK:
        if _ENGINE["obj"] is None:
            try:
                from .pipeline import build_engine
                eng = build_engine(lm_path)
                _ENGINE["obj"] = eng
                _ENGINE["path"] = getattr(eng, "_lm_path", "")
                _ENGINE["error"] = ""
            except Exception as exc:                             # noqa: BLE001
                _ENGINE["error"] = f"{type(exc).__name__}: {exc}"
                raise
        return _ENGINE["obj"]


def engine_info() -> dict:
    with ENGINE_LOCK:
        obj = _ENGINE["obj"]
        return {
            "loaded": obj is not None,
            "lm": os.path.basename(_ENGINE["path"] or ""),
            "lm_mode": getattr(obj, "_lm_mode", "") if obj is not None else "",
            "error": _ENGINE["error"],
        }


def parse_names(raw) -> tuple[str, ...]:
    """解析白名单人名/专词，支持英文逗号、中文逗号、顿号、分号与换行。"""
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            parts.extend(re.split(r"[,，、;\r\n]+", str(item)))
    else:
        parts = re.split(r"[,，、;\r\n]+", str(raw))
    seen: list[str] = []
    seen_set: set[str] = set()
    for p in parts:
        s = p.strip()
        if s and s not in seen_set:
            seen.append(s)
            seen_set.add(s)
    return tuple(seen)


class _ScopedEngineProxy:
    """任务级只读引擎代理，绝不在共享引擎实例上写入 names/whitelist_words。"""

    def __init__(self, base_engine, names: tuple[str, ...]):
        self._base = base_engine
        self._names = tuple(names or ())
        from types import SimpleNamespace
        base_cfg = getattr(base_engine, "cfg", None)
        cfg_dict = dict(vars(base_cfg)) if base_cfg is not None else {}
        cfg_dict["names"] = self._names
        cfg_dict["whitelist_words"] = self._names
        self.cfg = SimpleNamespace(**cfg_dict)
        self.lm = getattr(base_engine, "lm", None)
        self.loc = getattr(base_engine, "loc", None)
        self._lm_path = getattr(base_engine, "_lm_path", "")
        self._engine_kind = getattr(base_engine, "_engine_kind", "locate")

    def clean_book(self, lines, progress=None, **kwargs):
        fn = self._base.clean_book
        try:
            sig = inspect.signature(fn)
            params = sig.parameters
            call_kw = {}
            if "progress" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                call_kw["progress"] = progress
            if self._names:
                if "names" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    call_kw["names"] = self._names
                if "whitelist_words" in params:
                    call_kw["whitelist_words"] = self._names
            for k, v in kwargs.items():
                if k in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    call_kw[k] = v
            return fn(lines, **call_kw)
        except TypeError:
            return fn(lines, progress=progress)

    def clean_line(self, line: str, *args, **kwargs):
        return self._base.clean_line(line, *args, **kwargs)

    def _line_score(self, chars, removed):
        return self._base._line_score(chars, removed)

    def _protect_mask(self, chars):
        n = len(chars)
        base_mask = list(self._base._protect_mask(chars)) if hasattr(self._base, "_protect_mask") else [False] * n
        if not self._names or n == 0:
            return base_mask
        line = "".join(chars)
        for name in self._names:
            if not name:
                continue
            start = 0
            step = len(name)
            while True:
                idx = line.find(name, start)
                if idx < 0:
                    break
                for j in range(idx, min(n, idx + step)):
                    base_mask[j] = True
                start = idx + 1
        return base_mask

    def suspicion(self, chars, mask, i):
        return self._base.suspicion(chars, mask, i)


# --------------------------------------------------------------------------- LLM 配置与 SSRF 防护
LLM_LOCK = threading.Lock()
_LLM_CFG = {"obj": None}

_BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",
    "169.254.170.2",
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",
}


def get_llm_cfg():
    with LLM_LOCK:
        if _LLM_CFG["obj"] is None:
            from .llm import load_config
            _LLM_CFG["obj"] = load_config()
        return _LLM_CFG["obj"]


def _same_origin_url(url_a: str, url_b: str) -> bool:
    """判断两个 API base_url 是否同源，防止覆写 base_url 窃取已存密钥。"""
    if not url_a or not url_b:
        return False
    try:
        pa, pb = urlparse(url_a.strip()), urlparse(url_b.strip())
    except ValueError:
        return False
    return (pa.scheme.lower(), pa.netloc.lower()) == (pb.scheme.lower(), pb.netloc.lower())


def validate_llm_url(raw_url: str) -> tuple[bool, str, str]:
    """校验 LLM API 地址，拦截非 http/https、非法端口、云元数据与私有内网（放行本机回环）。

    返回 (ok, error_code, error_message)。
    """
    u = (raw_url or "").strip()
    if not u:
        return False, "INVALID_URL", "未填写 API 地址（base_url）"
    try:
        parsed = urlparse(u)
    except ValueError:
        return False, "INVALID_URL", "API 地址格式非法"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "INVALID_URL", "API 地址仅支持 http:// 或 https://"
    if parsed.username is not None or parsed.password is not None:
        return False, "INVALID_URL", "API 地址不能包含用户名或密码"
    try:
        port = parsed.port
    except ValueError:
        return False, "INVALID_URL", "API 地址端口非法"
    if port is not None and not (1 <= port <= 65535):
        return False, "INVALID_URL", "API 地址端口必须在 1~65535 之间"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "INVALID_URL", "API 地址缺少主机名"
    if host in _BLOCKED_METADATA_HOSTS:
        return False, "SSRF_BLOCKED", "禁止访问云元数据地址"

    def _check_ip(ip_obj) -> tuple[bool, str]:
        if str(ip_obj) in _BLOCKED_METADATA_HOSTS or ip_obj.is_link_local:
            return False, "禁止访问链路本地或云元数据地址"
        if ip_obj.is_multicast or ip_obj.is_unspecified:
            return False, "禁止访问广播或未指定地址"
        if getattr(ip_obj, "is_reserved", False):
            return False, "禁止访问保留网段地址"
        if ip_obj.is_private and not ip_obj.is_loopback:
            return False, "禁止访问私有内网地址（如需本机调试请使用 127.0.0.1 / localhost）"
        return True, ""

    try:
        ip_literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        ok, msg = _check_ip(ip_literal)
        if not ok:
            return False, "SSRF_BLOCKED", msg
        return True, "", ""

    if host != "localhost":
        try:
            infos = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0].split("%")[0]
                try:
                    ip_obj = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                ok, msg = _check_ip(ip_obj)
                if not ok:
                    return False, "SSRF_BLOCKED", msg
        except socket.gaierror:
            pass
    return True, "", ""


def merge_llm(body: dict):
    """用请求体覆盖已存配置；未提供的字段保持原值。"""
    from .llm import LLMConfig
    base = get_llm_cfg()
    kw = {}
    for k in ("base_url", "model"):
        if k in body and body[k] is not None:
            kw[k] = str(body[k]).strip()
    for k in ("timeout", "retries", "concurrency", "max_items", "temperature",
              "max_tokens", "json_mode", "save_key"):
        if k in body and body[k] is not None:
            kw[k] = body[k]
    key = body.get("api_key")
    if body.get("clear_key"):
        kw["api_key"] = ""
    elif isinstance(key, str) and key.strip():
        kw["api_key"] = key.strip()
    elif "base_url" in kw and kw["base_url"] and base.base_url and not _same_origin_url(kw["base_url"], base.base_url):
        kw["api_key"] = ""
    return LLMConfig(**{**base.__dict__, **kw}).clamp()


def save_llm(cfg) -> None:
    from .llm import save_config
    with LLM_LOCK:
        _LLM_CFG["obj"] = cfg
    save_config(cfg)


# --------------------------------------------------------------------------- 审校数据构建与重构引擎
def _diff_removed_chars(orig: str, new: str) -> int:
    if orig == new:
        return 0
    sm = difflib.SequenceMatcher(None, orig, new, autojunk=False)
    dele = 0
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            dele += (i2 - i1)
    return dele


def _apply_non_overlapping_patch(base_line: str, edits: list[tuple[str, str]]) -> str | None:
    """将多个针对各自 before 文本的局部改动安全合并到 base_line；有重叠时返回 None。"""
    ops: list[tuple[int, int, str]] = []
    for before, after in edits:
        if before == after:
            continue
        if before == base_line:
            sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    ops.append((i1, i2, after[j1:j2]))
            continue
        # 若 before 与 base_line 不同，尝试将 before 的改动映射到 base_line
        sm_map = difflib.SequenceMatcher(None, before, base_line, autojunk=False)
        b2base: dict[int, int] = {}
        for tag, i1, i2, j1, j2 in sm_map.get_opcodes():
            if tag == "equal":
                for off in range(i2 - i1):
                    b2base[i1 + off] = j1 + off
        sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if i1 == i2:
                anchor = b2base.get(i1)
                if anchor is None and i1 == len(before):
                    anchor = len(base_line)
                if anchor is None:
                    return None
                ops.append((anchor, anchor, after[j1:j2]))
            else:
                mapped = [b2base.get(k) for k in range(i1, i2)]
                if any(m is None for m in mapped):
                    return None
                if mapped != list(range(mapped[0], mapped[0] + len(mapped))):
                    return None
                ops.append((mapped[0], mapped[-1] + 1, after[j1:j2]))
    if not ops:
        return base_line
    ops.sort(key=lambda x: (x[0], x[1]))
    for idx in range(1, len(ops)):
        prev_s, prev_e, _ = ops[idx - 1]
        cur_s, cur_e, _ = ops[idx]
        if cur_s < prev_e or (cur_s == prev_s and cur_e == prev_e):
            return None
    out: list[str] = []
    pos = 0
    for s, e, rep in ops:
        out.append(base_line[pos:s])
        out.append(rep)
        pos = e
    out.append(base_line[pos:])
    return "".join(out)


def _reconstruct_lines(orig_lines: list[str], items: list[dict]) -> list[str]:
    """基于基线行与所有 accepted == True 的审校条目重构终稿行列表。"""
    cur = list(orig_lines)
    by_line: dict[int, list[dict]] = {}
    for it in items:
        if it.get("accepted") and it.get("after") != it.get("before"):
            by_line.setdefault(int(it["line_no"]) - 1, []).append(it)

    for li, active in by_line.items():
        if not (0 <= li < len(cur)):
            continue
        base = orig_lines[li]
        # 1) 优先链式精确匹配（如 V3 -> LLM）
        chained = base
        chain_ok = True
        for it in active:
            if it["before"] == chained:
                chained = it["after"]
            else:
                chain_ok = False
                break
        if chain_ok:
            cur[li] = chained
            continue

        # 2) 单条改动且 before == base（直接应用）或单条孤立操作映射
        if len(active) == 1:
            merged = _apply_non_overlapping_patch(base, [(active[0]["before"], active[0]["after"])])
            cur[li] = merged if merged is not None else active[-1]["after"]
            continue

        # 3) 多条非链式改动：尝试无重叠合并，冲突时回退到最新有效结果
        merged = _apply_non_overlapping_patch(base, [(it["before"], it["after"]) for it in active])
        cur[li] = merged if merged is not None else active[-1]["after"]

    return cur


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _extract_edit_items_from_result(result, orig_lines: list[str], file_id: str,
                                    file_name: str, engine_kind: str) -> list[dict]:
    """从 PipelineResult.edit_items（或兼容兜底从 change_log/review_queue）生成标准审校条目。"""
    raw_items = getattr(result, "edit_items", None)
    items: list[dict] = []

    if isinstance(raw_items, list):
        for idx, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            line_no = int(raw.get("line_no") or raw.get("line") or 1)
            li = max(0, min(len(orig_lines) - 1, line_no - 1)) if orig_lines else 0
            kind = str(raw.get("kind") or "auto_delete")
            source = str(raw.get("source") or engine_kind or "locator")
            before = str(raw.get("before", orig_lines[li] if orig_lines else ""))
            after = str(raw.get("after", before))
            default_acc = raw.get("default_accepted")
            if default_acc is None:
                default_acc = kind in ("ad_line", "auto_delete", "llm_delete")
            ctx_prev = raw.get("context_prev")
            if not isinstance(ctx_prev, list):
                ctx_prev = orig_lines[max(0, li - 2):li]
            ctx_next = raw.get("context_next")
            if not isinstance(ctx_next, list):
                ctx_next = orig_lines[li + 1:li + 3]
            items.append({
                "id": f"{file_id}:item:{idx}",
                "file_id": file_id,
                "file_name": file_name,
                "line_no": line_no,
                "kind": kind,
                "source": source,
                "reason": str(raw.get("reason") or ""),
                "confidence": float(raw.get("confidence") or 0.0),
                "before": before,
                "after": after,
                "context_prev": [str(x) for x in ctx_prev[:2]],
                "context_next": [str(x) for x in ctx_next[:2]],
                "accepted": bool(default_acc),
                "default_accepted": bool(default_acc),
            })
        return items

    # 兼容兜底：从 change_log 与 review_queue 组装
    seen_llm_lines: set[int] = set()
    idx = 0
    for e in getattr(result, "change_log", []) or []:
        if e.get("noop"):
            continue
        loc = e.get("location") or {}
        line_no = int(loc.get("line") or 1)
        li = max(0, min(len(orig_lines) - 1, line_no - 1)) if orig_lines else 0
        method = e.get("method", "rule_engine")
        before = str(e.get("before_llm") if method == "llm" and "before_llm" in e else e.get("original", ""))
        after = str(e.get("result_text", ""))
        if method == "llm":
            kind = "llm_delete"
            source = "llm"
            seen_llm_lines.add(line_no)
        else:
            kind = "ad_line" if (before and after == "") else "auto_delete"
            source = str(e.get("engine") or engine_kind or "locator")
        ev_types = [str(ev.get("type", "")) for ev in (e.get("evidence") or []) if isinstance(ev, dict)]
        reason = str(e.get("reason") or "; ".join(t for t in ev_types if t) or ("整行广告清理" if kind == "ad_line" else "定位器自动清理"))
        items.append({
            "id": f"{file_id}:item:{idx}",
            "file_id": file_id,
            "file_name": file_name,
            "line_no": line_no,
            "kind": kind,
            "source": source,
            "reason": reason,
            "confidence": float(e.get("confidence") or e.get("margin") or 0.0),
            "before": before,
            "after": after,
            "context_prev": orig_lines[max(0, li - 2):li],
            "context_next": orig_lines[li + 1:li + 3],
            "accepted": True,
            "default_accepted": True,
        })
        idx += 1

    for it in getattr(result, "review_queue", []) or []:
        dec = getattr(it, "decision", "") or ""
        if dec not in ("keep", "uncertain"):
            continue
        line_no = int(getattr(it, "line_start", 1) or 1)
        if line_no in seen_llm_lines:
            continue
        li = max(0, min(len(orig_lines) - 1, line_no - 1)) if orig_lines else 0
        kind = "llm_keep" if dec == "keep" else "uncertain"
        v3_res = getattr(it, "v3_result", None) or {}
        before = str(v3_res.get("cleaned_text") or getattr(it, "default_text", "") or (orig_lines[li] if orig_lines else ""))
        items.append({
            "id": f"{file_id}:item:{idx}",
            "file_id": file_id,
            "file_name": file_name,
            "line_no": line_no,
            "kind": kind,
            "source": "llm",
            "reason": str(getattr(it, "reason", "") or ""),
            "confidence": float(getattr(it, "confidence", 0.0) or 0.0),
            "before": before,
            "after": before,
            "context_prev": orig_lines[max(0, li - 2):li],
            "context_next": orig_lines[li + 1:li + 3],
            "accepted": False,
            "default_accepted": False,
        })
        idx += 1

    items.sort(key=lambda x: (x["line_no"], x["id"]))
    return items


def _recompute_file_record(frec: dict) -> None:
    items = frec["items"]
    frec["item_count"] = len(items)
    frec["accepted_count"] = sum(1 for it in items if it.get("accepted"))
    if frec.get("rolled_back_original") and frec["accepted_count"] == 0:
        frec["removed_chars"] = 0
        return
    cur_lines = _reconstruct_lines(frec["orig_lines"], items)
    frec["removed_chars"] = sum(
        _diff_removed_chars(a, b)
        for a, b in zip(frec["orig_lines"], cur_lines)
        if a != b
    )


def _public_file_entry(frec: dict) -> dict:
    return {
        "file_id": frec["file_id"],
        "name": frec["name"],
        "input_path": frec["input_path"],
        "output_path": frec["output_path"],
        "auto_path": frec["auto_path"],
        "encoding": frec["encoding"],
        "total_lines": frec["total_lines"],
        "item_count": frec["item_count"],
        "accepted_count": frec["accepted_count"],
        "removed_chars": frec["removed_chars"],
        "dirty": bool(frec["dirty"]),
    }


def _compute_job_summary(job_meta: dict, job_data: dict) -> dict:
    files_map = job_data.get("files_map") or {}
    file_order = job_data.get("file_order") or []
    total_lines = 0
    total_items = 0
    accepted_items = 0
    removed_chars = 0
    ad_lines = 0
    auto_deleted = 0
    llm_deleted = 0
    llm_kept = 0
    llm_uncertain = 0

    for fid in file_order:
        frec = files_map.get(fid)
        if not frec:
            continue
        total_lines += int(frec.get("total_lines") or 0)
        total_items += int(frec.get("item_count") or 0)
        accepted_items += int(frec.get("accepted_count") or 0)
        removed_chars += int(frec.get("removed_chars") or 0)
        for it in frec.get("items") or ():
            k = it.get("kind")
            if k == "ad_line":
                ad_lines += 1
            elif k == "auto_delete":
                auto_deleted += 1
            elif k == "llm_delete":
                llm_deleted += 1
            elif k == "llm_keep":
                llm_kept += 1
            elif k == "uncertain":
                llm_uncertain += 1

    llm_stats = job_data.get("llm_stats") or {}
    llm_usage = job_data.get("llm_usage") or {}
    llm_called = int(
        llm_stats.get("ok", 0) + llm_stats.get("fail", 0)
        or (llm_deleted + llm_kept + llm_uncertain)
    )
    prompt_tokens = int(llm_usage.get("prompt_tokens") or llm_stats.get("prompt_tokens") or 0)
    completion_tokens = int(llm_usage.get("completion_tokens") or llm_stats.get("completion_tokens") or 0)
    total_tokens = int(llm_usage.get("total_tokens") or llm_stats.get("total_tokens") or (prompt_tokens + completion_tokens))
    elapsed = job_meta.get("elapsed_sec")
    if elapsed is None and job_meta.get("started"):
        end_ts = job_meta.get("finished") or time.time()
        elapsed = round(max(0.0, end_ts - job_meta["started"]), 2)

    return {
        "file_count": len(file_order),
        "total_lines": total_lines,
        "total_items": total_items,
        "accepted_items": accepted_items,
        "removed_chars": removed_chars,
        "ad_lines": ad_lines,
        "auto_deleted": auto_deleted,
        "llm_called": llm_called,
        "llm_deleted": llm_deleted,
        "llm_kept": llm_kept,
        "llm_uncertain": llm_uncertain,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "elapsed_sec": float(elapsed or 0.0),
        "lm_mode": job_data.get("lm_mode") or "auto_fit",
    }


def _sync_job_views_locked(job_id: str) -> tuple[dict, list[dict]]:
    meta = JOBS[job_id]
    data = JOB_DATA[job_id]
    files_list = [_public_file_entry(data["files_map"][fid]) for fid in data["file_order"]]
    summary = _compute_job_summary(meta, data)
    meta["summary"] = summary
    meta["files"] = files_list
    meta["updated"] = time.time()
    return summary, files_list


# --------------------------------------------------------------------------- 流水线任务执行
def _build_pipeline_cfg(job_id: str, llm_on: bool, simplify: bool, names: tuple[str, ...]):
    from .audit import AuditConfig
    from .pipeline import PipelineConfig

    kw: dict = {"simplify": bool(simplify)}
    try:
        sig = inspect.signature(PipelineConfig)
        if "names" in sig.parameters:
            kw["names"] = names
    except (TypeError, ValueError):
        pass
    cfg = PipelineConfig(**kw)
    cfg.audit = AuditConfig()
    cancel_ev = _cancel_of(job_id) or threading.Event()
    if hasattr(cfg, "cancel"):
        cfg.cancel = cancel_ev.is_set
    if hasattr(cfg, "cancel_check"):
        cfg.cancel_check = cancel_ev.is_set
    if hasattr(cfg, "names"):
        cfg.names = names
    return cfg, cancel_ev


def _prepare_files(files: list) -> list[str]:
    out: list[str] = []
    for p in files:
        if isinstance(p, str) and os.path.isfile(p) and p.lower().endswith(".txt"):
            out.append(os.path.abspath(p))
    return list(dict.fromkeys(out))


def _register_file_result(job_id: str, file_id: str, src_path: str, base: str,
                          raw_text: str, base_text: str, nl: str, enc: str,
                          result, paths: dict, engine_kind: str) -> list[str]:
    orig_lines = base_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    auto_text = getattr(result, "cleaned_v3", "") or getattr(result, "cleaned", "")
    auto_lines = auto_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    items = _extract_edit_items_from_result(result, orig_lines, file_id, base, engine_kind)

    frec = {
        "file_id": file_id,
        "name": base,
        "input_path": src_path,
        "output_path": os.path.abspath(paths.get("cleaned") or os.path.join(os.path.dirname(src_path), base)),
        "auto_path": os.path.abspath(paths["auto_v3"]) if paths.get("auto_v3") else "",
        "encoding": enc or "utf-8",
        "newline": nl or "\n",
        "raw_text": raw_text,
        "orig_lines": orig_lines,
        "auto_lines": auto_lines,
        "items": items,
        "dirty": False,
        "rolled_back_original": False,
        "total_lines": len(orig_lines),
        "item_count": 0,
        "accepted_count": 0,
        "removed_chars": 0,
    }
    _recompute_file_record(frec)

    out_paths = [os.path.abspath(v) for v in paths.values() if v]
    with JOBS_LOCK:
        data = JOB_DATA.setdefault(job_id, {
            "files_map": {},
            "file_order": [],
            "items_by_id": {},
            "llm_stats": {},
            "llm_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "lm_mode": "",
        })
        data["files_map"][file_id] = frec
        data["file_order"].append(file_id)
        for it in items:
            data["items_by_id"][it["id"]] = it
        res_lm_mode = getattr(result, "lm_mode", "")
        if res_lm_mode:
            data["lm_mode"] = res_lm_mode
        res_usage = getattr(result, "llm_usage", None) or {}
        if isinstance(res_usage, dict):
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                data["llm_usage"][k] = int(data["llm_usage"].get(k, 0)) + int(res_usage.get(k) or 0)
        res_stats = getattr(result, "llm_stats", None) or {}
        if isinstance(res_stats, dict):
            for k in ("ok", "fail", "skipped", "retried"):
                data["llm_stats"][k] = int(data["llm_stats"].get(k, 0)) + int(res_stats.get(k) or 0)
        cur_outputs = JOB_OUTPUTS.setdefault(job_id, [])
        for op in out_paths:
            if op not in cur_outputs:
                cur_outputs.append(op)
        _sync_job_views_locked(job_id)
    return out_paths


def run_pipeline_job(job_id: str, files: list[str], out_dir: str, use_llm: bool,
                     names=(), llm_cfg=None, simplify: bool = True) -> None:
    """运行新清洗管线并构建结构化审校数据。"""
    names_tuple = parse_names(names)
    try:
        _set(job_id, state="running", stage="engine", progress=1,
             message="正在加载语言模型与定位器…")
        base_eng = get_engine()

        import adclean.pipeline as pl
        run_book = pl.run_book
        JobCancelledError = getattr(pl, "JobCancelledError", None)
        from .review import NullResolver
        from .t2s import to_simplified

        cfg, cancel_ev = _build_pipeline_cfg(job_id, use_llm, simplify=simplify, names=names_tuple)
        if use_llm and llm_cfg is not None and hasattr(cfg, "max_llm_items"):
            cfg.max_llm_items = int(getattr(llm_cfg, "max_items", 0) or 0)
        if cancel_ev.is_set():
            _set(job_id, state="cancelled", stage="cancelled",
                 message="任务已取消")
            return

        resolver = NullResolver()
        if use_llm:
            from .llm import OpenAICompatResolver
            resolver = OpenAICompatResolver(llm_cfg or get_llm_cfg(), cancel=cancel_ev.is_set)

        os.makedirs(out_dir, exist_ok=True)
        n = max(1, len(files))
        audit_share = 0.6 if use_llm else 1.0
        rb_sig = inspect.signature(run_book)
        rb_params = rb_sig.parameters
        t2s_params = inspect.signature(to_simplified).parameters
        # 若引擎原生支持 with_context 或 run_book 接受 names 参数，直接传入只读引擎，
        # 让 pipeline._prepare_request_engine 处理 auto_fit 与请求级隔离；否则回退代理。
        if hasattr(base_eng, "with_context") or "names" in rb_params:
            eng = base_eng
        else:
            eng = _ScopedEngineProxy(base_eng, names_tuple)

        for i, p in enumerate(files):
            if cancel_ev.is_set():
                cur_job = _get(job_id)
                _set(job_id, state="cancelled", stage="cancelled",
                     progress=cur_job.get("progress", int(i / n * 100)),
                     finished=time.time(),
                     message="已取消（已完成 %d/%d 个文件）" % (i, n))
                return

            base = os.path.basename(p)
            file_id = f"f{i + 1}"
            _set(job_id, stage="engine",
                 message="[%d/%d] %s · 定位与清洗扫描中…" % (i + 1, n, base))

            def on_audit(done, total, _i=i, _b=base):
                if cancel_ev.is_set():
                    return
                frac = audit_share * done / max(total, 1)
                _set(job_id, stage="engine",
                     progress=min(99, max(2, int((_i + frac) / n * 100))),
                     message="[%d/%d] %s · 清洗与审计 %d/%d 行" % (_i + 1, n, _b, done, total))

            def on_llm(done, total, ok, fail, _i=i, _b=base):
                if cancel_ev.is_set():
                    return
                frac = audit_share + (1 - audit_share) * done / max(total, 1)
                _set(job_id, stage="llm",
                     progress=min(99, max(2, int((_i + frac) / n * 100))),
                     message="[%d/%d] %s · LLM 复核 %d/%d（成功 %d / 失败 %d）"
                             % (_i + 1, n, _b, done, total, ok, fail))

            cfg.audit_progress = on_audit
            cfg.llm_progress = on_llm

            raw_text, enc = read_text(p)
            nl = newline_of(raw_text)

            rb_kwargs: dict = {"book_id": base, "resolver": resolver}
            if "cancel_check" in rb_params:
                rb_kwargs["cancel_check"] = cancel_ev.is_set
            if "names" in rb_params:
                rb_kwargs["names"] = names_tuple

            try:
                if cfg.simplify:
                    if "cancel_check" in t2s_params:
                        base_text = to_simplified(raw_text, cancel_check=cancel_ev.is_set)
                    else:
                        base_text = to_simplified(raw_text)
                else:
                    base_text = raw_text
                r = run_book(base_text, eng, cfg, **rb_kwargs)
            except Exception as exc:
                if JobCancelledError is not None and isinstance(exc, JobCancelledError):
                    cur_job = _get(job_id)
                    _set(job_id, state="cancelled", stage="cancelled",
                         progress=cur_job.get("progress", int(i / n * 100)),
                         finished=time.time(),
                         message="已取消（已完成 %d/%d 个文件）" % (i, n))
                    return
                raise

            if cancel_ev.is_set():
                cur_job = _get(job_id)
                _set(job_id, state="cancelled", stage="cancelled",
                     progress=cur_job.get("progress", int(i / n * 100)),
                     finished=time.time(),
                     message="已取消（已完成 %d/%d 个文件）" % (i, n))
                return

            _set(job_id, stage="write", message="[%d/%d] %s · 正在写入终稿…" % (i + 1, n, base))
            paths = r.write_outputs(out_dir, base, nl)
            _register_file_result(
                job_id, file_id, p, base, raw_text, base_text, nl, enc, r, paths,
                getattr(eng, "_engine_kind", "locate"),
            )

        with JOBS_LOCK:
            data = JOB_DATA.get(job_id) or {}
            if hasattr(resolver, "usage") and isinstance(resolver.usage, dict):
                data["llm_usage"] = {
                    "prompt_tokens": int(resolver.usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(resolver.usage.get("completion_tokens") or 0),
                    "total_tokens": int(resolver.usage.get("total_tokens") or 0),
                }
            if hasattr(resolver, "as_dict"):
                st = resolver.as_dict()
                if isinstance(st, dict):
                    data.setdefault("llm_stats", {}).update({
                        k: st[k] for k in ("ok", "fail", "skipped", "retried") if k in st
                    })
            meta = JOBS.get(job_id)
            if meta is not None:
                meta["finished"] = time.time()
                meta["elapsed_sec"] = round(max(0.0, meta["finished"] - meta.get("started", meta["finished"])), 2)
                if job_id in JOB_DATA:
                    _sync_job_views_locked(job_id)

        _set(job_id, state="done", progress=100, stage="done",
             message="完成：%d 个文件" % len(files))
    except Exception as exc:                                     # noqa: BLE001
        err_msg = "%s: %s" % (type(exc).__name__, exc)
        _set(job_id, state="error", stage="error",
             finished=time.time(),
             error=err_msg,
             message="处理失败：%s" % err_msg,
             trace=traceback.format_exc())


# --------------------------------------------------------------------------- 安全与上传辅助
def _extract_host(header_val: str) -> str:
    """安全提取 Host 或 URL 的主机名（小写）。"""
    raw = (header_val or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        try:
            return (urlparse(raw).hostname or "").lower()
        except ValueError:
            return ""
    try:
        return (urlparse("http://" + raw).hostname or "").lower()
    except ValueError:
        return ""


def _sanitize_upload_filename(raw_name: str) -> str:
    name = unquote(raw_name or "").replace("\\", "/")
    base = os.path.basename(name).strip()
    # 剔除路径穿越与控制字符
    base = re.sub(r'[\x00-\x1f<>:"/|?*]', "_", base)
    base = base.lstrip(".")
    if not base:
        base = "upload.txt"
    return base


def _valid_session_id(sid: str) -> bool:
    return bool(sid) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid))


def _dir_size(path: str) -> int:
    total = 0
    try:
        for name in os.listdir(path):
            fp = os.path.join(path, name)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    except OSError:
        pass
    return total


def _would_overwrite_source(files: list[str], out_dir: str) -> str:
    """检查输出目录是否会覆盖任意源 .txt 文件；若冲突返回冲突源文件路径。"""
    out_real = os.path.realpath(out_dir)
    for src in files:
        dst_real = os.path.realpath(os.path.join(out_real, os.path.basename(src)))
        if dst_real == os.path.realpath(src):
            return src
    return ""


# --------------------------------------------------------------------------- HTTP Handler
class Handler(BaseHTTPRequestHandler):
    server_version = "adclean"

    def _allowed_hosts(self) -> set[str]:
        hosts = set(_LOCAL_HOSTS)
        bind = ""
        try:
            bind = str(self.server.server_address[0] or "").lower()
        except Exception:                                        # noqa: BLE001
            pass
        if bind and bind not in ("0.0.0.0", "::"):
            hosts.add(bind)
        return hosts

    def _is_loopback_bind(self) -> bool:
        try:
            bind = str(self.server.server_address[0] or "").lower()
        except Exception:                                        # noqa: BLE001
            return True
        return bind in _LOCAL_HOSTS or bind == ""

    def _host_ok(self) -> bool:
        host_hdr = (self.headers.get("Host") or "").strip()
        if not host_hdr:
            return True
        h = _extract_host(host_hdr)
        return bool(h) and h in self._allowed_hosts()

    def _same_origin_ok(self) -> bool:
        if not self._host_ok():
            return False
        allowed = self._allowed_hosts()
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin.lower() == "null":
                return False
            oh = _extract_host(origin)
            return bool(oh) and oh in allowed
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            rh = _extract_host(referer)
            return bool(rh) and rh in allowed
        return True

    # -- 基础输出工具 ---------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > _MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt, *args):    # 静音
        pass

    def _serve_static(self, rel_path: str):
        clean_rel = unquote(rel_path).lstrip("/")
        if not clean_rel:
            clean_rel = "index.html"
        target = os.path.realpath(os.path.join(STATIC_REAL, clean_rel))
        try:
            inside = os.path.commonpath([STATIC_REAL, target]) == STATIC_REAL
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            return self._send(404, b"missing", "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        ctype = _MIME_TYPES.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            return self._send(200, f.read(), ctype)

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:                                    # noqa: N802
        if not self._host_ok():
            return self._json({"error": "非法 Host 头"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/favicon.ico":
            fav = os.path.realpath(os.path.join(STATIC_REAL, "favicon.ico"))
            if os.path.isfile(fav):
                return self._serve_static("/favicon.ico")
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return

        if not u.path.startswith("/api/"):
            return self._serve_static(u.path)

        if not self._same_origin_ok():
            return self._json({"error": "拒绝跨站 API 请求"}, 403)

        if u.path == "/api/job":
            jid = (q.get("job") or q.get("id") or [""])[0]
            j = _get(jid)
            if not j:
                return self._json({"error": "no such job"}, 404)
            return self._json(j)

        if u.path == "/api/job/items":
            return self._handle_job_items(q)

        if u.path == "/api/job/diff":
            return self._handle_job_diff(q)

        if u.path == "/api/job/export-zip":
            return self._handle_export_zip((q.get("job") or q.get("id") or [""])[0])

        if u.path == "/api/browse":
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下已禁用本机磁盘浏览"}, 403)
            return self._browse((q.get("path") or [""])[0])

        if u.path == "/api/list":
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下已禁用本机磁盘浏览"}, 403)
            folder = (q.get("folder") or [""])[0]
            if not os.path.isdir(folder):
                return self._json({"error": "目录不存在"}, 400)
            recursive = (q.get("recursive") or ["false"])[0].lower() in ("1", "true", "yes")
            files = list_txt_files(folder, recursive=recursive)
            return self._json({"files": files, "sizes": {f: os.path.getsize(f) for f in files}})

        if u.path == "/api/drives":
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下已禁用本机磁盘浏览"}, 403)
            drives = []
            for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                p = f"{d}:\\"
                if os.path.exists(p):
                    drives.append(p)
            return self._json({
                "drives": drives,
                "cwd": os.path.abspath(os.getcwd()),
                "home": os.path.expanduser("~"),
            })

        if u.path == "/api/llm/config":
            cfg = get_llm_cfg()
            return self._json({
                "config": cfg.as_public_dict(),
                "config_path": _config_path(),
                "engine": engine_info(),
            })

        if u.path == "/api/download":
            jid = (q.get("job") or [""])[0]
            fid = (q.get("file_id") or [""])[0]
            path = (q.get("path") or [""])[0]
            return self._download(job_id=jid, file_id=fid, path=path)

        if u.path == "/api/reveal":
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下已禁用本机文件定位"}, 403)
            return self._reveal(path=(q.get("path") or [""])[0])

        return self._json({"error": "not found"}, 404)

    # -- 审校查询与导出 -------------------------------------------------
    def _handle_job_items(self, q: dict):
        jid = (q.get("job") or q.get("id") or [""])[0]
        file_id = (q.get("file_id") or [""])[0]
        kind = (q.get("kind") or [""])[0]
        status = (q.get("status") or [""])[0]
        kw = (q.get("q") or [""])[0].strip().lower()
        try:
            page = max(1, int((q.get("page") or ["1"])[0]))
            page_size = max(1, min(200, int((q.get("page_size") or ["40"])[0])))
        except ValueError:
            return self._json({"error": "分页参数无效"}, 400)

        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            pool = []
            for fid in data["file_order"]:
                if file_id and fid != file_id:
                    continue
                frec = data["files_map"].get(fid)
                if frec:
                    pool.extend(frec["items"])

            counts = {
                "all": len(pool),
                "ad_line": 0,
                "auto_delete": 0,
                "llm_delete": 0,
                "llm_keep": 0,
                "uncertain": 0,
                "accepted": 0,
                "rejected": 0,
            }
            for it in pool:
                k = it.get("kind", "")
                if k in counts:
                    counts[k] += 1
                if it.get("accepted"):
                    counts["accepted"] += 1
                else:
                    counts["rejected"] += 1

            filtered = []
            for it in pool:
                if kind and it.get("kind") != kind:
                    continue
                if status == "accepted" and not it.get("accepted"):
                    continue
                if status == "rejected" and it.get("accepted"):
                    continue
                if kw:
                    hay = "\n".join([
                        str(it.get("line_no", "")),
                        it.get("before", ""),
                        it.get("after", ""),
                        it.get("reason", ""),
                    ]).lower()
                    if kw not in hay:
                        continue
                filtered.append(dict(it))

        total = len(filtered)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        return self._json({
            "job": jid,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "counts": counts,
            "items": filtered[start:start + page_size],
        })

    def _handle_job_diff(self, q: dict):
        jid = (q.get("job") or q.get("id") or [""])[0]
        file_id = (q.get("file_id") or [""])[0]
        only_changed = (q.get("only_changed") or ["true"])[0].lower() in ("1", "true", "yes")
        try:
            page = max(1, int((q.get("page") or ["1"])[0]))
            page_size = max(1, min(300, int((q.get("page_size") or ["60"])[0])))
        except ValueError:
            return self._json({"error": "分页参数无效"}, 400)

        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            if not file_id:
                if not data["file_order"]:
                    return self._json({"error": "任务无文件"}, 400)
                file_id = data["file_order"][0]
            frec = data["files_map"].get(file_id)
            if not frec:
                return self._json({"error": "file_id 不存在"}, 404)

            orig_lines = list(frec["orig_lines"])
            cur_lines = _reconstruct_lines(orig_lines, frec["items"])
            by_line: dict[int, list[dict]] = {}
            for it in frec["items"]:
                by_line.setdefault(int(it["line_no"]), []).append(it)

        all_rows = []
        changed_count = 0
        for idx, (b_line, a_line) in enumerate(zip(orig_lines, cur_lines), start=1):
            changed = (b_line != a_line)
            if changed:
                changed_count += 1
            if only_changed and not changed:
                continue
            line_items = by_line.get(idx, [])
            all_rows.append({
                "line_no": idx,
                "before": b_line,
                "after": a_line,
                "changed": changed,
                "item_ids": [it["id"] for it in line_items],
                "kinds": sorted({it["kind"] for it in line_items}),
            })

        total = len(all_rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        return self._json({
            "job": jid,
            "file_id": file_id,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "changed_lines": changed_count,
            "total_lines": len(orig_lines),
            "lines": all_rows[start:start + page_size],
        })

    def _allowed_output(self, path: str) -> bool:
        ap = os.path.abspath(path)
        with JOBS_LOCK:
            for paths in JOB_OUTPUTS.values():
                for p in paths:
                    if os.path.abspath(p) == ap:
                        return True
        return False

    def _download(self, job_id: str = "", file_id: str = "", path: str = ""):
        target_path = ""
        if job_id:
            with JOBS_LOCK:
                data = JOB_DATA.get(job_id)
                if data is None:
                    return self._json({"error": "no such job"}, 404)
                if not file_id and data["file_order"]:
                    file_id = data["file_order"][0]
                frec = data["files_map"].get(file_id)
                if not frec:
                    return self._json({"error": "file_id 不存在"}, 404)
                target_path = frec["output_path"]
        elif path:
            if not self._allowed_output(path):
                return self._json({"error": "只能下载本次任务产出的文件"}, 403)
            target_path = path
        else:
            return self._json({"error": "缺少 job/file_id 或 path"}, 400)

        if not os.path.isfile(target_path):
            return self._json({"error": "文件不存在"}, 404)
        fn = os.path.basename(target_path)
        with open(target_path, "rb") as f:
            body = f.read()
        self._send(
            200,
            body,
            "text/plain; charset=utf-8",
            {"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(fn)},
        )

    def _handle_export_zip(self, jid: str):
        if not jid:
            return self._json({"error": "缺少 job"}, 400)
        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            entries = [
                (frec["name"], frec["output_path"])
                for fid in data["file_order"]
                for frec in [data["files_map"].get(fid)]
                if frec and os.path.isfile(frec["output_path"])
            ]
        if not entries:
            return self._json({"error": "当前任务没有可导出的终稿文件"}, 404)

        with tempfile.SpooledTemporaryFile(max_size=20 * 1024 * 1024) as spool:
            used_names: set[str] = set()
            with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for arcname, fp in entries:
                    final_name = arcname
                    if final_name in used_names:
                        stem, ext = os.path.splitext(arcname)
                        k = 2
                        while f"{stem}_{k}{ext}" in used_names:
                            k += 1
                        final_name = f"{stem}_{k}{ext}"
                    used_names.add(final_name)
                    zf.write(fp, arcname=final_name)
            spool.seek(0)
            body = spool.read()

        zip_name = f"adclean_{jid}.zip"
        self._send(
            200,
            body,
            "application/zip",
            {"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(zip_name)},
        )

    def _reveal(self, job_id: str = "", file_id: str = "", path: str = ""):
        target = ""
        if job_id:
            with JOBS_LOCK:
                data = JOB_DATA.get(job_id)
                if data is None:
                    return self._json({"error": "no such job"}, 404)
                if not file_id and data["file_order"]:
                    file_id = data["file_order"][0]
                frec = data["files_map"].get(file_id)
                if not frec:
                    return self._json({"error": "file_id 不存在"}, 404)
                target = frec["output_path"]
        elif path:
            if not self._allowed_output(path):
                return self._json({"error": "不允许"}, 403)
            target = path
        else:
            return self._json({"error": "缺少目标文件"}, 400)

        ap = os.path.abspath(target)
        folder = ap if os.path.isdir(ap) else os.path.dirname(ap)
        try:
            import subprocess
            if os.name == "nt":
                if os.path.isfile(ap):
                    subprocess.Popen(["explorer", f"/select,{ap}"])
                else:
                    os.startfile(folder)                        # noqa: S606
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:                                  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "path": ap, "folder": folder})

    def _browse(self, path: str):
        if not path:
            return self._json({"error": "缺少 path"}, 400)
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            return self._json({"error": "目录不存在"}, 400)
        try:
            entries = sorted(os.listdir(path))
        except OSError as e:
            return self._json({"error": str(e)}, 400)
        dirs, files = [], []
        for e in entries:
            fp = os.path.join(path, e)
            try:
                if os.path.isdir(fp):
                    dirs.append(e)
                elif os.path.splitext(e)[1].lower() == ".txt":
                    files.append({"name": e, "size": os.path.getsize(fp)})
            except OSError:
                continue
        parent = os.path.dirname(path.rstrip("\\/")) or path
        return self._json({
            "path": os.path.abspath(path),
            "parent": parent,
            "dirs": dirs[:500],
            "files": files[:2000],
        })

    # -- 上传处理（流式） ------------------------------------------------
    def _handle_upload(self, q: dict):
        _cleanup_expired_uploads()
        raw_name = (q.get("filename") or [""])[0]
        if not raw_name:
            return self._json({"error": "缺少 filename 参数"}, 400)
        safe_name = _sanitize_upload_filename(raw_name)
        if not safe_name.lower().endswith(".txt"):
            return self._json({"error": "仅支持上传 .txt 文件"}, 400)

        sid = (q.get("session_id") or [""])[0].strip()
        if not sid:
            sid = uuid.uuid4().hex[:16]
        elif not _valid_session_id(sid):
            return self._json({"error": "session_id 非法"}, 400)

        try:
            content_len = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            return self._json({"error": "Content-Length 非法"}, 400)
        if content_len < 0:
            return self._json({"error": "需要提供有效的 Content-Length"}, 411)
        if content_len > MAX_UPLOAD_FILE_BYTES:
            return self._json({"error": "单文件大小超过 80MB 上限"}, 413)

        sdir = os.path.realpath(os.path.join(UPLOAD_ROOT, sid))
        upload_root_real = os.path.realpath(UPLOAD_ROOT)
        if os.path.commonpath([upload_root_real, sdir]) != upload_root_real:
            return self._json({"error": "非法会话目录"}, 400)

        with UPLOAD_LOCK:
            os.makedirs(sdir, exist_ok=True)
            existing_bytes = _dir_size(sdir)
            if existing_bytes + content_len > MAX_UPLOAD_SESSION_BYTES:
                return self._json({"error": "单会话上传总量超过 300MB 上限"}, 413)

            dst = os.path.join(sdir, safe_name)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(safe_name)
                k = 1
                while os.path.exists(os.path.join(sdir, f"{stem}_{k}{ext}")):
                    k += 1
                dst = os.path.join(sdir, f"{stem}_{k}{ext}")
            tmp_dst = dst + f".{uuid.uuid4().hex[:6]}.part"

        remaining = content_len
        written = 0
        chunk_size = 65536
        try:
            with open(tmp_dst, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(chunk_size, remaining))
                    if not chunk:
                        raise IOError("上传连接意外中断")
                    written += len(chunk)
                    if written > MAX_UPLOAD_FILE_BYTES:
                        raise ValueError("FILE_TOO_LARGE")
                    fh.write(chunk)
                    remaining -= len(chunk)
            with UPLOAD_LOCK:
                if _dir_size(sdir) + written > MAX_UPLOAD_SESSION_BYTES:
                    raise ValueError("SESSION_TOO_LARGE")
                os.replace(tmp_dst, dst)
        except ValueError as ve:
            if os.path.exists(tmp_dst):
                os.remove(tmp_dst)
            msg = "单会话上传总量超过 300MB 上限" if str(ve) == "SESSION_TOO_LARGE" else "单文件大小超过 80MB 上限"
            return self._json({"error": msg}, 413)
        except Exception as exc:                                 # noqa: BLE001
            if os.path.exists(tmp_dst):
                try:
                    os.remove(tmp_dst)
                except OSError:
                    pass
            return self._json({"error": f"上传失败：{exc}"}, 400)

        return self._json({
            "ok": True,
            "session_id": sid,
            "file": {
                "path": os.path.abspath(dst),
                "name": os.path.basename(dst),
                "size": written,
                "source": "upload",
            },
        })

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:                                   # noqa: N802
        if not self._same_origin_ok():
            return self._json({"error": "拒绝跨站请求"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/api/upload":
            return self._handle_upload(q)

        try:
            body = self._read_json()
        except Exception:
            return self._json({"error": "请求体解析失败"}, 400)

        if u.path == "/api/upload/clear":
            sid = str(body.get("session_id") or "").strip()
            if not _valid_session_id(sid):
                return self._json({"error": "session_id 非法"}, 400)
            sdir = os.path.realpath(os.path.join(UPLOAD_ROOT, sid))
            upload_root_real = os.path.realpath(UPLOAD_ROOT)
            if os.path.commonpath([upload_root_real, sdir]) != upload_root_real:
                return self._json({"error": "非法会话路径"}, 400)
            target_path = str(body.get("path") or "").strip()
            with UPLOAD_LOCK:
                if target_path:
                    tp = os.path.realpath(target_path)
                    if os.path.commonpath([sdir, tp]) == sdir and os.path.isfile(tp):
                        os.remove(tp)
                elif os.path.isdir(sdir):
                    shutil.rmtree(sdir, ignore_errors=True)
            return self._json({"ok": True, "session_id": sid})

        if u.path == "/api/llm/config":
            try:
                cfg = merge_llm(body)
                if cfg.base_url:
                    ok, code, msg = validate_llm_url(cfg.base_url)
                    if not ok:
                        return self._json({"error": msg, "code": code}, 400)
                save_llm(cfg)
            except Exception as e:                               # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            return self._json({
                "config": cfg.as_public_dict(),
                "config_path": _config_path(),
            })

        if u.path == "/api/llm/models":
            from .llm import LLMError, list_models
            cfg = merge_llm(body)
            ok, code, msg = validate_llm_url(cfg.base_url)
            if not ok:
                return self._json({"error": msg, "code": code, "models": []}, 400)
            try:
                ids = list_models(cfg)
            except LLMError as e:
                return self._json({"error": str(e), "code": "UPSTREAM_ERROR", "models": []}, 502)
            if not ids:
                return self._json({"error": "该端点没有返回任何模型", "code": "EMPTY_MODELS", "models": []}, 502)
            return self._json({"models": ids, "current": cfg.model, "count": len(ids)})

        if u.path == "/api/llm/probe":
            from .llm import LLMError, probe_chat
            cfg = merge_llm(body)
            ok, code, msg = validate_llm_url(cfg.base_url)
            if not ok:
                return self._json({"ok": False, "error": msg, "code": code}, 400)
            try:
                t0 = time.time()
                out = probe_chat(cfg)
                latency_ms = int((time.time() - t0) * 1000)
            except LLMError as e:
                return self._json({"ok": False, "error": str(e), "code": "UPSTREAM_ERROR"}, 502)
            return self._json({
                "ok": True,
                "model": out["model"],
                "raw": out["raw"],
                "decision": out["parsed"].get("decision"),
                "latency_ms": latency_ms,
            })

        if u.path == "/api/llm/save":
            try:
                cfg = merge_llm(body)
                if cfg.base_url:
                    ok, code, msg = validate_llm_url(cfg.base_url)
                    if not ok:
                        return self._json({"error": msg, "code": code}, 400)
                save_llm(cfg)
            except Exception as e:                               # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "config_path": _config_path()})

        if u.path in ("/api/pipeline", "/api/pipeline/clean", "/api/pipeline/review", "/api/audit"):
            return self._handle_start_pipeline(u.path, body)

        if u.path == "/api/job/cancel":
            jid = str(body.get("job") or body.get("id") or "").strip()
            if not jid:
                return self._json({"error": "缺少 job 参数"}, 400)
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job is None:
                    return self._json({"error": "任务不存在"}, 404)
                ev = _cancel_of(jid)
                if ev is not None:
                    ev.set()
                state = job.get("state")
                if state in ("queued", "running"):
                    job["message"] = "正在取消…"
                    job["updated"] = time.time()
            return self._json({"ok": True, "state": state})

        if u.path == "/api/job/decisions":
            return self._handle_job_decisions(body)

        if u.path == "/api/job/apply":
            return self._handle_job_apply(body)

        if u.path == "/api/job/rollback":
            return self._handle_job_rollback(body)

        if u.path == "/api/reveal":
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下已禁用本机文件定位"}, 403)
            return self._reveal(
                job_id=str(body.get("job") or ""),
                file_id=str(body.get("file_id") or ""),
                path=str(body.get("path") or ""),
            )

        return self._json({"error": "not found"}, 404)

    def _handle_start_pipeline(self, route_path: str, body: dict):
        if route_path == "/api/pipeline":
            mode = str(body.get("mode") or "auto").strip().lower()
            if mode not in ("auto", "llm"):
                return self._json({"error": f"不支持的 mode: {mode}"}, 400)
            use_llm = (mode == "llm")
        elif route_path == "/api/pipeline/review":
            use_llm = True
            mode = "llm"
        else:
            use_llm = False
            mode = "auto"

        files = _prepare_files(body.get("files") or [])
        folder = str(body.get("folder") or "").strip()
        if folder and os.path.isdir(folder):
            if not self._is_loopback_bind():
                return self._json({"error": "非本机回环监听模式下禁止读取本机目录"}, 403)
            files.extend(list_txt_files(folder, recursive=bool(body.get("recursive", False))))
            files = _prepare_files(files)

        if not self._is_loopback_bind() and files:
            upload_real = os.path.realpath(UPLOAD_ROOT)
            for fp in files:
                if os.path.commonpath([upload_real, os.path.realpath(fp)]) != upload_real:
                    return self._json({"error": "非本机回环监听模式下仅允许处理上传文件"}, 403)

        if not files:
            return self._json({"error": "没有可处理的 txt 文件"}, 400)

        jid = uuid.uuid4().hex[:12]
        raw_out = str(body.get("out") or "").strip()
        if raw_out:
            out_dir = raw_out
        else:
            upload_real = os.path.realpath(UPLOAD_ROOT)
            all_uploads = all(
                os.path.commonpath([upload_real, os.path.realpath(fp)]) == upload_real
                for fp in files
            )
            out_dir = os.path.join(OUTPUT_ROOT, jid) if all_uploads else os.path.join(
                os.path.dirname(files[0]), "cleaned_audit"
            )

        if _would_overwrite_source(files, out_dir):
            return self._json({
                "error": "输出目录不能与原文件所在目录相同，请更换输出目录以保护原文件"
            }, 400)

        opts = body.get("options") or {}
        llm_cfg = None
        if use_llm:
            from .llm import LLMError
            llm_cfg = merge_llm(opts.get("llm") or {})
            if not llm_cfg.base_url or not llm_cfg.model:
                return self._json({"error": "请先填写 API 地址并选择模型"}, 400)
            ok, code, msg = validate_llm_url(llm_cfg.base_url)
            if not ok:
                return self._json({"error": msg, "code": code}, 400)
            if not llm_cfg.api_key:
                return self._json({"error": "请先填写 API Key"}, 400)
            try:
                llm_cfg.chat_url()
            except LLMError as e:
                return self._json({"error": str(e)}, 400)
            if opts.get("saveConfig"):
                save_llm(llm_cfg)

        names = parse_names(opts.get("names", ""))
        simplify = bool(opts.get("simplify", True))
        now = time.time()

        with JOBS_LOCK:
            _cleanup_jobs_locked(now)
            JOBS[jid] = {
                "job": jid,
                "state": "queued",
                "stage": "queued",
                "progress": 0,
                "percent": 0,
                "message": "排队中",
                "error": "",
                "trace": "",
                "files": [],
                "input_files": files,
                "out": out_dir,
                "use_llm": use_llm,
                "mode": mode,
                "started": now,
                "updated": now,
                "elapsed_sec": 0.0,
                "summary": {},
            }
            CANCEL[jid] = threading.Event()

        threading.Thread(
            target=run_pipeline_job,
            args=(jid, files, out_dir, use_llm, names, llm_cfg, simplify),
            daemon=True,
        ).start()

        return self._json({
            "job": jid,
            "mode": mode,
            "files": len(files),
            "out": out_dir,
            "use_llm": use_llm,
        })

    def _handle_job_decisions(self, body: dict):
        jid = str(body.get("job") or body.get("id") or "").strip()
        updates = body.get("updates") or []
        bulk = body.get("bulk") or None
        if not jid:
            return self._json({"error": "缺少 job 参数"}, 400)

        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            touched_files: set[str] = set()
            updated_count = 0

            for u in updates:
                if not isinstance(u, dict):
                    continue
                item_id = str(u.get("id") or "")
                it = data["items_by_id"].get(item_id)
                if it is None or "accepted" not in u:
                    continue
                new_val = bool(u["accepted"])
                if it["accepted"] != new_val:
                    it["accepted"] = new_val
                    updated_count += 1
                    touched_files.add(it["file_id"])

            if isinstance(bulk, dict) and "accepted" in bulk:
                b_fid = str(bulk.get("file_id") or "")
                b_kind = str(bulk.get("kind") or "")
                b_status = str(bulk.get("status") or "")
                b_kw = str(bulk.get("q") or "").strip().lower()
                b_acc = bool(bulk["accepted"])
                for fid in data["file_order"]:
                    if b_fid and fid != b_fid:
                        continue
                    frec = data["files_map"].get(fid)
                    if not frec:
                        continue
                    for it in frec["items"]:
                        if b_kind and it.get("kind") != b_kind:
                            continue
                        if b_status == "accepted" and not it.get("accepted"):
                            continue
                        if b_status == "rejected" and it.get("accepted"):
                            continue
                        if b_kw:
                            hay = "\n".join([
                                str(it.get("line_no", "")),
                                it.get("before", ""),
                                it.get("after", ""),
                                it.get("reason", ""),
                            ]).lower()
                            if b_kw not in hay:
                                continue
                        if it["accepted"] != b_acc:
                            it["accepted"] = b_acc
                            updated_count += 1
                            touched_files.add(fid)

            for fid in touched_files:
                frec = data["files_map"].get(fid)
                if frec:
                    frec["dirty"] = True
                    frec["rolled_back_original"] = False
                    _recompute_file_record(frec)

            summary, files_list = _sync_job_views_locked(jid)

        return self._json({
            "ok": True,
            "updated": updated_count,
            "summary": summary,
            "files": files_list,
        })

    def _handle_job_apply(self, body: dict):
        jid = str(body.get("job") or body.get("id") or "").strip()
        target_fid = str(body.get("file_id") or "").strip()
        if not jid:
            return self._json({"error": "缺少 job 参数"}, 400)

        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            fids = [target_fid] if target_fid else list(data["file_order"])
            to_write: list[tuple[str, dict, str]] = []
            for fid in fids:
                frec = data["files_map"].get(fid)
                if not frec:
                    if target_fid:
                        return self._json({"error": "file_id 不存在"}, 404)
                    continue
                if target_fid or frec.get("dirty"):
                    cur_lines = _reconstruct_lines(frec["orig_lines"], frec["items"])
                    txt = "\n".join(cur_lines)
                    nl = frec.get("newline") or "\n"
                    if nl != "\n":
                        txt = txt.replace("\n", nl)
                    to_write.append((fid, frec, txt))

        applied: list[str] = []
        for fid, frec, txt in to_write:
            _atomic_write_text(frec["output_path"], txt, encoding="utf-8")
            applied.append(fid)

        with JOBS_LOCK:
            data = JOB_DATA[jid]
            for fid in applied:
                frec = data["files_map"][fid]
                frec["dirty"] = False
                frec["rolled_back_original"] = False
                _recompute_file_record(frec)
            summary, files_list = _sync_job_views_locked(jid)

        return self._json({
            "ok": True,
            "applied_files": applied,
            "summary": summary,
            "files": files_list,
        })

    def _handle_job_rollback(self, body: dict):
        jid = str(body.get("job") or body.get("id") or "").strip()
        file_id = str(body.get("file_id") or "").strip()
        target = str(body.get("target") or "auto").strip().lower()
        if not jid:
            return self._json({"error": "缺少 job 参数"}, 400)
        if target not in ("auto", "original"):
            return self._json({"error": f"不支持的回滚目标: {target}"}, 400)

        with JOBS_LOCK:
            data = JOB_DATA.get(jid)
            if data is None:
                return self._json({"error": "no such job"}, 404)
            if not file_id:
                if len(data["file_order"]) == 1:
                    file_id = data["file_order"][0]
                else:
                    return self._json({"error": "多文件任务请指定 file_id"}, 400)
            frec = data["files_map"].get(file_id)
            if not frec:
                return self._json({"error": "file_id 不存在"}, 404)

            if target == "auto":
                txt = "\n".join(frec["auto_lines"])
                nl = frec.get("newline") or "\n"
                if nl != "\n":
                    txt = txt.replace("\n", nl)
            else:
                txt = frec.get("raw_text") or "\n".join(frec["orig_lines"])

        _atomic_write_text(frec["output_path"], txt, encoding="utf-8")

        with JOBS_LOCK:
            frec = JOB_DATA[jid]["files_map"][file_id]
            for it in frec["items"]:
                if target == "original":
                    it["accepted"] = False
                else:
                    it["accepted"] = it.get("kind") in ("ad_line", "auto_delete")
            frec["dirty"] = False
            frec["rolled_back_original"] = (target == "original")
            _recompute_file_record(frec)
            summary, files_list = _sync_job_views_locked(jid)

        return self._json({
            "ok": True,
            "target": target,
            "file_id": file_id,
            "summary": summary,
            "files": files_list,
        })


def _config_path() -> str:
    from .llm import config_path
    return config_path()


class AdcleanHTTPServer(ThreadingHTTPServer):
    """Windows 上必须独占绑定端口，端口被占时直接报错。"""

    daemon_threads = True
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


def serve(port: int = 8777, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    _configure_stdio()
    try:
        httpd = AdcleanHTTPServer((host, port), Handler)
    except OSError as e:
        print("")
        print("[错误] 无法在 %s:%d 启动：%s" % (host, port, e))
        print("       端口可能已被占用（比如已经开着一个 adclean 窗口）。")
        print("       换个端口试试：  python -m adclean.server --port 8791")
        print("       或者直接用浏览器打开已开着的那个窗口。")
        return 1
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print("")
    print("  [OK] adclean Web UI 已启动：%s" % url)
    print("       浏览器会自动打开；若没有，请手动访问上面的地址。")
    print("")
    print("  使用顺序：")
    print("     1) 自动清洗          —— 本地离线完成，先跑这一步")
    print("     2) LLM 深度复核      —— 填你自己的 OpenAI 兼容连接，再跑这一步")
    print("")
    print("  关闭本窗口即可停止服务（或按 Ctrl+C）。")
    print("")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="adclean.server", description="adclean 本地 Web UI")
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    return serve(a.port, not a.no_browser, a.host)


if __name__ == "__main__":
    raise SystemExit(main())
