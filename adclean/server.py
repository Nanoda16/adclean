# -*- coding: utf-8 -*-
"""adclean.server —— 零依赖本地 Web UI（Python 标准库 http.server）。

启动：
    python -m adclean.server            # 自动打开浏览器 http://127.0.0.1:8777
    python -m adclean.server --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .engine import Config, clean_line, clean_text
from .processor import (build_model, list_txt_files, newline_of, process_files,
                        read_text)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
CANCEL: dict[str, threading.Event] = {}
JOB_OUTPUTS: dict[str, list] = {}          # job_id -> 允许下载/定位的绝对路径
ENGINE_LOCK = threading.Lock()
_ENGINE = {"obj": None, "path": "", "error": ""}


def _set(job_id: str, **kw) -> None:
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kw)


def _get(job_id: str) -> dict:
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


def _cancel_of(job_id: str) -> threading.Event:
    with JOBS_LOCK:
        ev = CANCEL.get(job_id)
        if ev is None:
            ev = threading.Event()
            CANCEL[job_id] = ev
        return ev


# --------------------------------------------------------------------------- 引擎
def get_engine(lm_path: str = ""):
    """惰性加载 V3 引擎，进程内复用。"""
    with ENGINE_LOCK:
        if _ENGINE["obj"] is None:
            from .pipeline import build_engine
            eng = build_engine(lm_path)
            _ENGINE["obj"] = eng
            _ENGINE["path"] = getattr(eng, "_lm_path", "")
        return _ENGINE["obj"]


def engine_info() -> dict:
    with ENGINE_LOCK:
        return {"loaded": _ENGINE["obj"] is not None,
                "lm": os.path.basename(_ENGINE["path"] or ""),
                "error": _ENGINE["error"]}


# --------------------------------------------------------------------------- LLM 配置
LLM_LOCK = threading.Lock()
_LLM_CFG = {"obj": None}


def get_llm_cfg():
    with LLM_LOCK:
        if _LLM_CFG["obj"] is None:
            from .llm import load_config
            _LLM_CFG["obj"] = load_config()
        return _LLM_CFG["obj"]


def merge_llm(body: dict):
    """用请求体覆盖已存配置；未提供的字段保持原值。

    不提供 `api_key`（或为空串）→ 沿用已保存的 key；`clear_key=true` → 清空。
    """
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
    return LLMConfig(**{**base.__dict__, **kw}).clamp()


def save_llm(cfg) -> None:
    from .llm import save_config
    with LLM_LOCK:
        _LLM_CFG["obj"] = cfg
    save_config(cfg)


# --------------------------------------------------------------------------- 旧链路任务
def run_job(job_id: str, files: list[str], out_dir: str, cfg: Config,
            same_folder: bool, preview_only: bool) -> None:
    try:
        _set(job_id, state="running", stage="learning", percent=0,
             message="正在学习语料用字统计…")

        def on_progress(done, total, msg):
            pct = int(done / max(total, 1) * 60)
            _set(job_id, percent=pct, message=msg, done=done, total=total)

        model = build_model(files, cfg.rare_threshold, progress=on_progress)
        st = model.stats()
        _set(job_id, stage="cleaning", percent=62, model=st,
             message=f"语料：字符 {st['chars']} 种 / 搭配 {st['bigrams']} 种 / "
                     f"稀有字 {st['rare_cjk']}")

        if preview_only:
            previews = []
            for p in files:
                text, _enc = read_text(p)
                res = clean_text(text, model, cfg, sample_limit=12)
                previews.append({
                    "file": os.path.basename(p),
                    "path": p,
                    "lines": res.original.count("\n") + 1,
                    "changed_lines": res.changed_lines,
                    "removed_chars": res.removed_chars,
                    "samples": res.samples,
                })
            _set(job_id, state="done", percent=100, previews=previews,
                 message="扫描完成（未写入文件）")
            return

        def on_write(done, total, msg):
            pct = 62 + int(done / max(total, 1) * 37)
            _set(job_id, percent=pct, message=msg, done=done, total=total)

        reports, _m = process_files(
            files, out_dir, cfg, same_folder=same_folder, model=model,
            progress=on_write, report_path=None)
        _set(job_id, state="done", percent=100,
             message=f"完成：{len(reports)} 个文件",
             results=[{**r.as_dict(),
                       "samples": r.samples[:12]} for r in reports])
    except Exception:
        _set(job_id, state="error", percent=100, message="处理失败",
             trace=traceback.format_exc())


# --------------------------------------------------------------------------- 新链路
def _pipeline_cfg(job_id: str, llm_on: bool, simplify: bool = False):
    from .audit import AuditConfig
    from .pipeline import PipelineConfig
    cfg = PipelineConfig(simplify=bool(simplify))
    cfg.audit = AuditConfig()
    cancel = _cancel_of(job_id)

    def audit_prog(done, total):
        _set(job_id, percent=min(55, int(done / max(total, 1) * 55)),
             message="审计扫描 %d/%d 行" % (done, total))

    def llm_prog(done, total, ok, fail):
        _set(job_id, stage="llm",
             percent=60 + int(done / max(total, 1) * 39),
             message="LLM 审查 %d/%d（成功 %d / 失败 %d）" % (done, total, ok, fail))

    cfg.audit_progress = audit_prog if not llm_on else None
    cfg.llm_progress = llm_prog
    cfg.cancel = cancel.is_set
    return cfg


def _queue_preview(result, limit: int = 40) -> dict:
    """给 UI 的队列摘要：严重度分布 + 优先级分布 + 前若干条。"""
    q = result.review_queue
    sev: dict = {}
    prio: dict = {}
    code: dict = {}
    for it in q:
        sev[it.severity] = sev.get(it.severity, 0) + 1
        prio[str(it.priority)] = prio.get(str(it.priority), 0) + 1
        code[it.reason_code] = code.get(it.reason_code, 0) + 1
    items = []
    for it in q[:limit]:
        v3 = it.v3_result or {}
        items.append({
            "item_id": it.item_id,
            "line": it.line_start,
            "severity": it.severity,
            "priority": it.priority,
            "reason_code": it.reason_code,
            "text": it.suspicious_text,
            "types": sorted({e.get("type", "") for e in it.evidence}),
            "v3_acted": bool(v3.get("acted")),
            "v3_removed": v3.get("removed_text", ""),
            # 改写（如繁→简）时 removed_text 为空，需与「删除」区分
            "v3_pure_delete": bool(v3.get("acted")) and bool(v3.get("removed_text")),
            "v3_rewritten": bool(v3.get("acted")) and not v3.get("removed_text"),
            "v3_cleaned": v3.get("cleaned_text", ""),
            "n_candidates": len([c for c in it.candidate_repairs
                                 if c.delete_ranges or c.text]),
            "decision": it.decision,
            "selected_candidate": it.selected_candidate,
            "operations": it.operations,
            "reason": it.reason,
        })
    return {"by_severity": sev, "by_priority": prio, "by_reason_code": code,
            "shown": len(items), "items": items}


def _summarize(result, src: str, paths: dict, auto_path: str = "") -> dict:
    rp = result.report
    return {
        "src": src,
        "name": os.path.basename(src),
        "chars": rp["chars_total"],
        "lines": rp["lines_total"],
        "events_initial": rp["audit"]["events_initial"],
        "events_final": rp["audit"]["events_final"],
        "by_severity": rp["audit"]["by_severity_initial"],
        "by_evidence": rp["audit"]["by_evidence_type"],
        "auto_lines": rp["auto_repair"]["lines"],
        "auto_chars": rp["auto_repair"]["chars"],
        "queue_items": rp["llm_review"]["items"],
        "queue_sent": rp["llm_review"]["sent"],
        "llm_lines": rp["llm_review"]["lines"],
        "llm_chars": rp["llm_review"]["chars"],
        "llm_added": rp["llm_review"].get("added", 0),
        "kept": rp["llm_review"]["kept"],
        "uncertain": rp["llm_review"]["uncertain"],
        "unresolved": rp["unresolved_events"],
        "metrics": rp.get("metrics", {}),
        "final_audit": rp.get("final_audit", {}),
        "seconds": result.seconds,
        "paths": paths,
        "auto_path": auto_path,
        "queue": _queue_preview(result),
        "changelog_sample": [
            {"line": e["location"]["line"], "method": e["method"],
             "original": (e.get("original") or "")[:60],
             "replacement": (e.get("replacement") or "")[:60],
             "deleted": (e.get("deleted") or "")[:60],
             "candidate": e.get("selected_candidate", ""),
             "confidence": e.get("confidence", 0),
             "severity": e.get("severity", "")}
            for e in [x for x in result.change_log
                      if x.get("method") != "llm"][:40]],
        "llm_sample": [
            {"line": e["location"]["line"],
             "before": (e.get("before_llm") or e.get("original") or "")[:80],
             "after": (e.get("result_text") or "")[:80],
             "original": (e.get("original") or "")[:80],
             "deleted": (e.get("deleted") or "")[:60],
             "added": (e.get("replacement") or "")[:60],
             "decision": e.get("decision", ""),
             "candidate": e.get("selected_candidate", ""),
             "confidence": e.get("confidence", 0),
             "reason": (e.get("reason") or "")[:100],
             "model": e.get("model", "")}
            for e in [x for x in result.change_log
                      if x.get("method") == "llm"][:80]],
        "llm_kept_sample": [
            {"line": it.line_start,
             "text": (it.suspicious_text or "")[:100],
             "decision": getattr(it, "decision", ""),
             "confidence": getattr(it, "confidence", 0),
             "reason": (getattr(it, "reason", "") or "")[:100]}
            for it in result.review_queue
            if getattr(it, "decision", "") in ("keep", "uncertain")][:40],
        "llm_stats": rp["llm_review"].get("stats", {}),
    }


def _prepare_files(files: list) -> list:
    out = []
    for p in files:
        if os.path.isfile(p) and p.lower().endswith(".txt"):
            out.append(os.path.abspath(p))
    return list(dict.fromkeys(out))


def run_pipeline_job(job_id: str, files: list, out_dir: str, use_llm: bool,
                     names=(), llm_cfg=None, simplify: bool = False) -> None:
    """新链路：use_llm=False 只做审计+自动修复；True 再接 LLM 审查并写回。"""
    try:
        _set(job_id, state="running", stage="engine", percent=0,
             message="正在加载 clean n-gram 语料（首次约 10~20 秒）…")
        eng = get_engine()
        if names:
            eng.cfg.whitelist_words = tuple(names)
        _set(job_id, percent=2, message="语料就绪：%s"
             % os.path.basename(getattr(eng, "_lm_path", "")))

        from .pipeline import run_book
        from .review import NullResolver
        from .t2s import to_simplified

        cfg = _pipeline_cfg(job_id, use_llm, simplify=simplify)
        resolver = NullResolver()
        if use_llm:
            from .llm import OpenAICompatResolver
            resolver = OpenAICompatResolver(llm_cfg or get_llm_cfg(),
                                            cancel=cfg.cancel)

        os.makedirs(out_dir, exist_ok=True)
        results, all_paths = [], []
        n = max(1, len(files))
        audit_share = 0.6 if use_llm else 1.0

        for i, p in enumerate(files):
            if cfg.cancel():
                _set(job_id, state="cancelled", percent=100, results=results,
                     message="已取消（已完成 %d/%d 个文件）" % (i, n))
                return
            base = os.path.basename(p)
            _set(job_id, stage="engine",
                 message="[%d/%d] %s · V3 清洗 + 审计扫描中…" % (i + 1, n, base))

            def on_audit(done, total, _i=i, _b=base):
                frac = audit_share * done / max(total, 1)
                _set(job_id, stage="engine",
                     percent=int((_i + frac) / n * 100),
                     message="[%d/%d] %s · V3 清洗+审计 %d/%d 行"
                             % (_i + 1, n, _b, done, total))

            def on_llm(done, total, ok, fail, _i=i, _b=base):
                frac = audit_share + (1 - audit_share) * done / max(total, 1)
                _set(job_id, stage="llm",
                     percent=int((_i + frac) / n * 100),
                     message="[%d/%d] %s · LLM 审查 %d/%d（成功 %d / 失败 %d）"
                             % (_i + 1, n, _b, done, total, ok, fail))

            cfg.audit_progress = on_audit
            cfg.llm_progress = on_llm

            text, _enc = read_text(p)
            nl = newline_of(text)
            if cfg.simplify:
                text = to_simplified(text)
            r = run_book(text, eng, cfg, book_id=base, resolver=resolver)

            # 未应用 LLM 的中间版本（V3-only），供 UI 对比与回滚
            paths = r.write_outputs(out_dir, base, nl)
            auto_path = paths.get("auto_v3", "")
            all_paths.extend(paths.values())
            results.append(_summarize(r, p, paths, auto_path))
            _set(job_id, results=results)

        JOB_OUTPUTS[job_id] = list(dict.fromkeys(all_paths))
        stats = resolver.as_dict() if hasattr(resolver, "as_dict") else {}
        _set(job_id, state="done", percent=100, stage="done",
             message="完成：%d 个文件" % len(results), results=results,
             llm=stats)
    except Exception:
        _set(job_id, state="error", percent=100, message="处理失败",
             trace=traceback.format_exc())


# --------------------------------------------------------------------------- HTTP
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Handler(BaseHTTPRequestHandler):
    server_version = "adclean"

    # -- 工具 ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _csrf_ok(self) -> bool:
        """拒绝来自其它站点的浏览器请求（本机 UI 不带 Origin 或为 localhost）。"""
        origin = self.headers.get("Origin") or ""
        if not origin:
            return True
        try:
            host = urlparse(origin).hostname or ""
        except ValueError:
            return False
        return host in _LOCAL_HOSTS

    def log_message(self, fmt, *args):    # 静音
        pass

    def _static(self, name: str, ctype: str):
        p = os.path.join(STATIC, name)
        if not os.path.exists(p):
            return self._send(404, b"missing", "text/plain")
        with open(p, "rb") as f:
            return self._send(200, f.read(), ctype)

    # -- GET -----------------------------------------------------------
    def do_GET(self) -> None:                                  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if u.path == "/pywebview_bridge.js":
            return self._static("pywebview_bridge.js",
                                "application/javascript; charset=utf-8")
        if u.path == "/api/job":
            jid = (q.get("id") or [""])[0]
            j = _get(jid)
            if not j:
                return self._json({"error": "no such job"}, 404)
            return self._json(j)
        if u.path == "/api/browse":
            return self._browse((q.get("path") or [""])[0])
        if u.path == "/api/list":
            folder = (q.get("folder") or [""])[0]
            if not os.path.isdir(folder):
                return self._json({"error": "目录不存在"}, 400)
            files = list_txt_files(folder)
            return self._json({"files": files,
                               "sizes": {f: os.path.getsize(f) for f in files}})
        if u.path == "/api/drives":
            drives = []
            for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                p = f"{d}:\\"
                if os.path.exists(p):
                    drives.append(p)
            return self._json({"drives": drives,
                               "cwd": os.path.abspath(os.getcwd()),
                               "home": os.path.expanduser("~")})
        if u.path == "/api/llm/config":
            cfg = get_llm_cfg()
            return self._json({"config": cfg.as_public_dict(),
                               "config_path": _config_path(),
                               "engine": engine_info()})
        if u.path == "/api/download":
            return self._download((q.get("path") or [""])[0])
        if u.path == "/api/reveal":
            return self._reveal((q.get("path") or [""])[0])
        return self._json({"error": "not found"}, 404)

    def _allowed_output(self, path: str) -> bool:
        ap = os.path.abspath(path)
        for paths in JOB_OUTPUTS.values():
            for p in paths:
                if os.path.abspath(p) == ap:
                    return True
        return False

    def _download(self, path: str):
        if not path:
            return self._json({"error": "缺少 path"}, 400)
        if not self._allowed_output(path):
            return self._json({"error": "只能下载本次任务产出的文件"}, 403)
        if not os.path.isfile(path):
            return self._json({"error": "文件不存在"}, 404)
        fn = os.path.basename(path)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % quote(fn))
        self.end_headers()
        self.wfile.write(body)

    def _reveal(self, path: str):
        if not path or not self._allowed_output(path):
            return self._json({"error": "不允许"}, 403)
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            if os.name == "nt":
                os.startfile(folder)                            # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:                                  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "folder": folder})

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
        return self._json({"path": os.path.abspath(path), "parent": parent,
                           "dirs": dirs[:500], "files": files[:2000]})

    # -- POST ----------------------------------------------------------
    def do_POST(self) -> None:                                 # noqa: N802
        u = urlparse(self.path)
        if not self._csrf_ok():
            return self._json({"error": "拒绝跨站请求"}, 403)
        try:
            body = self._read_json()
        except Exception:
            return self._json({"error": "请求体解析失败"}, 400)

        if u.path == "/api/llm/config":
            try:
                cfg = merge_llm(body)
                save_llm(cfg)
            except Exception as e:                              # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            return self._json({"config": cfg.as_public_dict(),
                               "config_path": _config_path()})

        if u.path == "/api/llm/models":
            from .llm import LLMError, list_models
            cfg = merge_llm(body)
            try:
                ids = list_models(cfg)
            except LLMError as e:
                return self._json({"error": str(e), "models": []}, 200)
            if not ids:
                return self._json({"error": "该端点没有返回任何模型", "models": []})
            return self._json({"models": ids, "current": cfg.model,
                               "count": len(ids)})

        if u.path == "/api/llm/probe":
            from .llm import LLMError, probe_chat
            cfg = merge_llm(body)
            try:
                out = probe_chat(cfg)
            except LLMError as e:
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._json({"ok": True, "model": out["model"],
                               "raw": out["raw"],
                               "decision": out["parsed"].get("decision")})

        if u.path == "/api/llm/save":
            try:
                save_llm(merge_llm(body))
            except Exception as e:                              # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "config_path": _config_path()})

        if u.path == "/api/pipeline/clean" or u.path == "/api/pipeline/review":
            use_llm = u.path.endswith("review")
            files = _prepare_files(body.get("files") or [])
            folder = body.get("folder") or ""
            if folder and os.path.isdir(folder):
                files.extend(list_txt_files(
                    folder, recursive=bool(body.get("recursive", False))))
                files = _prepare_files(files)
            if not files:
                return self._json({"error": "没有可处理的 txt 文件"}, 400)
            out_dir = body.get("out") or os.path.join(
                os.path.dirname(files[0]), "cleaned_audit")
            opts = body.get("options") or {}
            llm_cfg = None
            if use_llm:
                from .llm import LLMError
                llm_cfg = merge_llm(opts.get("llm") or {})
                if not llm_cfg.base_url or not llm_cfg.model:
                    return self._json(
                        {"error": "请先填写 API 地址并选择模型"}, 400)
                if not llm_cfg.api_key:
                    return self._json({"error": "请先填写 API Key"}, 400)
                try:
                    llm_cfg.chat_url()
                except LLMError as e:
                    return self._json({"error": str(e)}, 400)
                if opts.get("saveConfig"):
                    save_llm(llm_cfg)
            jid = uuid.uuid4().hex[:12]
            _set(jid, state="queued", percent=0, message="排队中",
                 files=files, out=out_dir, use_llm=use_llm,
                 started=time.time())
            _cancel_of(jid)
            threading.Thread(
                target=run_pipeline_job,
                args=(jid, files, out_dir, use_llm,
                      [s for s in str(opts.get("names", "")).split(",") if s],
                      llm_cfg, bool(opts.get("simplify", False))),
                daemon=True).start()
            return self._json({"job": jid, "files": len(files), "out": out_dir,
                               "use_llm": use_llm})

        if u.path == "/api/job/cancel":
            jid = body.get("job") or ""
            ev = _cancel_of(jid)
            ev.set()
            _set(jid, message="正在取消…")
            return self._json({"ok": True})

        if u.path == "/api/clean":                              # 旧链路
            files = [f for f in body.get("files", []) if isinstance(f, str)]
            folder = body.get("folder") or ""
            if folder and os.path.isdir(folder):
                files.extend(list_txt_files(folder, recursive=bool(body.get("recursive", True))))
            files = [os.path.abspath(f) for f in dict.fromkeys(files) if os.path.isfile(f)]
            if not files:
                return self._json({"error": "没有可处理的 txt 文件"}, 400)
            out_dir = body.get("out") or os.path.join(os.path.dirname(files[0]), "cleaned")
            same_folder = bool(body.get("sameFolder", False))
            opts = body.get("options") or {}
            cfg = Config(
                alpha=float(opts.get("alpha", Config.alpha)),
                gain_threshold=float(opts.get("gainThreshold", Config.gain_threshold)),
                rare_threshold=int(opts.get("rareThreshold", Config.rare_threshold)),
                common_threshold=int(opts.get("commonThreshold", Config.common_threshold)),
                max_span=int(opts.get("maxSpan", Config.max_span)),
                drop_unusual_blocks=not bool(opts.get("keepUnusual", False)),
                whitelist=frozenset(str(opts.get("whitelist", ""))),
                aggressive=bool(opts.get("aggressive", False)),
            )
            preview = bool(body.get("preview", False))
            jid = uuid.uuid4().hex[:12]
            _set(jid, state="queued", percent=0, message="排队中",
                 files=files, out=out_dir, preview=preview,
                 started=time.time())
            t = threading.Thread(target=run_job,
                                 args=(jid, files, out_dir, cfg, same_folder, preview),
                                 daemon=True)
            t.start()
            return self._json({"job": jid, "files": len(files), "out": out_dir})
        return self._json({"error": "not found"}, 404)


def _config_path() -> str:
    from .llm import config_path
    return config_path()


class AdcleanHTTPServer(ThreadingHTTPServer):
    """Windows 上必须**独占**绑定端口，端口被占时直接报错（由 `serve()` 提示）。"""

    daemon_threads = True
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(port: int = 8777, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    try:
        httpd = AdcleanHTTPServer((host, port), Handler)
    except OSError as e:
        print("")
        print("[错误] 无法在 %s:%d 启动：%s" % (host, port, e))
        print("       端口可能已被占用（比如已经开着一个 adclean 窗口）。")
        print("       换个端口试试：  python -m adclean.server --port 8791")
        print("       或者直接用浏览器打开已开着的那个窗口。")
        return 1
    url = f"http://{host}:{port}/"
    print("")
    print("  ✅ adclean Web UI 已启动：%s" % url)
    print("     浏览器会自动打开；若没有，请手动访问上面的地址。")
    print("")
    print("  使用顺序：")
    print("     ① 审计 + 自动修复   —— 不花钱，先跑这一步")
    print("     ② LLM 审查          —— 填你自己的 OpenAI 兼容连接，再跑这一步")
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
