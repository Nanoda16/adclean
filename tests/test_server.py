# -*- coding: utf-8 -*-
"""adclean.server 与 adclean.gui 的 HTTP / 审校闭环 / 安全 / 生命周期测试。"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean import gui
from adclean import server as srv

FAKE_KEY = "sk-test-SECRET-abcdefghijklmnop"


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"data": [{"id": "m-2"}, {"id": "m-1"}]})
        self._json(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._json(200, {
            "choices": [{"message": {
                "content": '{"decision":"keep","confidence":0.9,"reason":"t"}'
            }}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        })


@pytest.fixture
def fake_llm():
    s = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/v1" % s.server_address[1]
    s.shutdown()
    s.server_close()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """起一个真实 adclean HTTP 服务，配置与上传临时目录隔离到 tmp_path。"""
    monkeypatch.setenv("ADCLEAN_HOME", str(tmp_path))
    monkeypatch.setattr(srv, "_LLM_CFG", {"obj": None})
    monkeypatch.setattr(srv, "UPLOAD_ROOT", str(tmp_path / "_uploads"))
    monkeypatch.setattr(srv, "OUTPUT_ROOT", str(tmp_path / "_outputs"))
    s = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % s.server_address[1]
    yield base, tmp_path
    s.shutdown()
    s.server_close()
    with srv.JOBS_LOCK:
        srv.JOB_OUTPUTS.clear()
        srv.CANCEL.clear()
        srv.JOB_DATA.clear()
        srv.JOBS.clear()


def req(method, url, body=None, headers=None, raw_bytes: bytes | None = None):
    hdrs = dict(headers or {})
    if raw_bytes is not None:
        data = raw_bytes
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    else:
        data = None
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jget(url, headers=None):
    s, h, b = req("GET", url, headers=headers)
    return s, h, json.loads(b.decode("utf-8", "replace"))


def jpost(url, body, origin=None, headers=None):
    hdrs = dict(headers or {})
    if origin is not None:
        hdrs["Origin"] = origin
    s, h, b = req("POST", url, body=body, headers=hdrs)
    try:
        return s, h, json.loads(b.decode("utf-8", "replace"))
    except ValueError:
        return s, h, b.decode("utf-8", "replace")


def _wait_job(base: str, jid: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s, _, j = jget(f"{base}/api/job?job={jid}")
        assert s == 200
        if j["state"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


# --------------------------------------------------------------- 静态资源与路由
def test_static_served_and_traversal_blocked(ui, tmp_path, monkeypatch):
    base, _ = ui
    static_dir = tmp_path / "static_mock"
    (static_dir / "styles").mkdir(parents=True)
    (static_dir / "js").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (static_dir / "pywebview_bridge.js").write_text("// bridge", encoding="utf-8")
    (static_dir / "styles" / "editorial.css").write_text("body{}", encoding="utf-8")
    (static_dir / "js" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(srv, "STATIC", str(static_dir))
    monkeypatch.setattr(srv, "STATIC_REAL", os.path.realpath(static_dir))

    s, h, b = req("GET", base + "/")
    assert s == 200 and b == b"<html>ok</html>"
    assert "text/html" in h.get("Content-Type", "")
    assert h.get("X-Content-Type-Options") == "nosniff"

    s, h, _ = req("GET", base + "/styles/editorial.css")
    assert s == 200 and "text/css" in h.get("Content-Type", "")

    s, h, _ = req("GET", base + "/js/app.js")
    assert s == 200 and "javascript" in h.get("Content-Type", "")

    s, _, _ = req("GET", base + "/pywebview_bridge.js")
    assert s == 200

    # /favicon.ico 兜底返回 204（禁止浏览器自动探测 404）
    s, _, _ = req("GET", base + "/favicon.ico")
    assert s in (200, 204)

    # 路径穿越必须被拦
    s, _, _ = req("GET", base + "/../server.py")
    assert s == 404


def test_unknown_route_404(ui):
    base, _ = ui
    s, _, _ = req("GET", base + "/api/nope")
    assert s == 404


# --------------------------------------------------------------- LLM 配置与 SSRF
def test_config_save_mask_keep_and_clear(ui, fake_llm):
    base, tmp = ui
    s, _, j = jget(base + "/api/llm/config")
    assert s == 200
    assert "api_key" not in j["config"]
    assert j["config"]["has_key"] is False

    s, _, j = jpost(base + "/api/llm/config",
                    {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200
    assert FAKE_KEY not in json.dumps(j, ensure_ascii=False)
    assert j["config"]["has_key"] is True
    assert j["config"]["key_masked"].startswith("sk-t")

    # 省略 api_key 且同源时保留
    s, _, j = jpost(base + "/api/llm/config", {"model": "m-2"})
    assert s == 200 and j["config"]["has_key"] is True and j["config"]["model"] == "m-2"

    # clear_key 清空
    s, _, j = jpost(base + "/api/llm/config", {"clear_key": True})
    assert s == 200 and j["config"]["has_key"] is False

    # save_key=False 不落盘密钥
    jpost(base + "/api/llm/config",
          {"base_url": fake_llm, "api_key": FAKE_KEY, "save_key": False})
    p = os.path.join(str(tmp), "llm.json")
    assert json.load(open(p, encoding="utf-8"))["api_key"] == ""


def test_models_and_probe_and_errors(ui, fake_llm):
    base, _ = ui
    s, _, j = jpost(base + "/api/llm/models",
                    {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200 and j["models"] == ["m-1", "m-2"]

    s, _, j = jpost(base + "/api/llm/probe",
                    {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200 and j["ok"] is True and "latency_ms" in j

    # 上游不可达返回 502（非 200）
    s, _, j = jpost(base + "/api/llm/models",
                    {"base_url": "http://127.0.0.1:1/v1", "api_key": "x",
                     "timeout": 1, "retries": 0})
    assert s == 502 and j["models"] == [] and j["error"]

    s, _, j = jpost(base + "/api/llm/probe",
                    {"base_url": "http://127.0.0.1:1/v1", "api_key": "x",
                     "model": "m-1", "timeout": 1, "retries": 0})
    assert s == 502 and j["ok"] is False and j["error"]


@pytest.mark.parametrize("bad_url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5:8080/v1",
    "http://192.168.1.10/v1",
    "http://172.16.0.1/v1",
    "ftp://example.com/v1",
    "http://user:pass@example.com/v1",
])
def test_ssrf_blocked_on_models_and_probe(ui, bad_url):
    base, _ = ui
    s, _, j = jpost(base + "/api/llm/models", {"base_url": bad_url, "api_key": "x"})
    assert s == 400 and j.get("code") in ("SSRF_BLOCKED", "INVALID_URL")
    s, _, j = jpost(base + "/api/llm/probe", {"base_url": bad_url, "api_key": "x", "model": "m"})
    assert s == 400 and j.get("code") in ("SSRF_BLOCKED", "INVALID_URL")


def test_merge_llm_does_not_leak_key_to_different_host(ui, fake_llm):
    base, _ = ui
    jpost(base + "/api/llm/config",
          {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    merged = srv.merge_llm({"base_url": "https://attacker.example.com/v1"})
    assert merged.api_key == ""


# --------------------------------------------------------------- Host / Origin / 同源保护
def test_host_and_origin_security(ui):
    base, _ = ui
    parsed = urllib.parse.urlparse(base)
    # 拒绝恶意 Host（防 DNS Rebinding），用 HTTPConnection 直连本地端口绕过系统代理
    import http.client
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.request("GET", "/api/llm/config", headers={"Host": "evil.attacker.com"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 403

    # 拒绝跨站 Origin（包括 GET /api/drives 与 POST）
    s, _, _ = jget(base + "/api/drives", headers={"Origin": "http://evil.example.com"})
    assert s == 403
    s, _, _ = jget(base + "/api/drives", headers={"Origin": "null"})
    assert s == 403
    s, _, _ = jget(base + "/api/browse?path=.", headers={"Referer": "http://evil.example.com/x"})
    assert s == 403

    s, _, _ = jpost(base + "/api/llm/config", {"model": "x"}, origin="http://evil.example.com")
    assert s == 403

    # 同源放行
    for origin in (base, base.replace("127.0.0.1", "localhost")):
        s, _, _ = jpost(base + "/api/llm/config", {"model": "m"}, origin=origin)
        assert s == 200


def test_non_loopback_bind_disables_local_disk_browse(tmp_path, monkeypatch):
    monkeypatch.setenv("ADCLEAN_HOME", str(tmp_path))
    s = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    # 模拟监听 0.0.0.0
    s.server_address = ("0.0.0.0", s.server_address[1])
    threading.Thread(target=s.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % s.server_address[1]
    try:
        st, _, _ = jget(base + "/api/drives")
        assert st == 403
        st, _, _ = jget(base + "/api/browse?path=" + urllib.parse.quote(str(tmp_path)))
        assert st == 403
        st, _, _ = jget(base + "/api/list?folder=" + urllib.parse.quote(str(tmp_path)))
        assert st == 403
    finally:
        s.shutdown()
        s.server_close()


# --------------------------------------------------------------- 上传 & 目录列表
def test_upload_clear_and_list_non_recursive(ui, tmp_path):
    base, _ = ui
    # 流式上传并清洗路径穿越文件名
    content = "第一章 开始\nQQ群：123456\n正文在这里。\n".encode("utf-8")
    qname = urllib.parse.quote("../../evil/书名测试.txt")
    s, _, b = req(
        "POST",
        f"{base}/api/upload?filename={qname}&session_id=sess_01",
        headers={"Content-Type": "application/octet-stream"},
        raw_bytes=content,
    )
    j = json.loads(b.decode("utf-8"))
    assert s == 200 and j["ok"] is True and j["session_id"] == "sess_01"
    up_path = j["file"]["path"]
    assert os.path.isfile(up_path)
    assert j["file"]["size"] == len(content)
    assert "evil" not in os.path.basename(up_path)

    # 非 .txt 被拒绝
    s, _, b = req(
        "POST",
        f"{base}/api/upload?filename=bad.exe&session_id=sess_01",
        headers={"Content-Type": "application/octet-stream"},
        raw_bytes=b"abc",
    )
    assert s == 400

    # 超单文件上限（仅通过 Content-Length 声明测试，不实际发 81MB）
    import http.client
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.putrequest("POST", "/api/upload?filename=big.txt&session_id=sess_01")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(srv.MAX_UPLOAD_FILE_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()

    # clear 清理
    s, _, j = jpost(base + "/api/upload/clear", {"session_id": "sess_01"})
    assert s == 200 and not os.path.exists(up_path)

    # /api/list 默认 recursive=false
    root_dir = tmp_path / "books"
    sub_dir = root_dir / "sub"
    sub_dir.mkdir(parents=True)
    (root_dir / "a.txt").write_text("a", encoding="utf-8")
    (sub_dir / "b.txt").write_text("b", encoding="utf-8")

    s, _, j = jget(base + "/api/list?folder=" + urllib.parse.quote(str(root_dir)))
    assert s == 200 and len(j["files"]) == 1 and j["files"][0].endswith("a.txt")

    s, _, j = jget(base + "/api/list?folder=" + urllib.parse.quote(str(root_dir)) + "&recursive=true")
    assert s == 200 and len(j["files"]) == 2


# --------------------------------------------------------------- 审校闭环 & 导出 & 并发隔离
@dataclass
class _MockPipelineResult:
    book_id: str
    cleaned: str
    cleaned_v3: str = ""
    lm_mode: str = "auto_fit"
    llm_usage: dict = field(default_factory=dict)
    llm_stats: dict = field(default_factory=dict)
    edit_items: list = field(default_factory=list)
    audit_events: list = field(default_factory=list)
    final_events: list = field(default_factory=list)
    review_queue: list = field(default_factory=list)
    change_log: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    seconds: float = 0.1

    def write_outputs(self, out_dir: str, basename: str, newline: str = "\n") -> dict:
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, basename)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(self.cleaned.replace("\n", newline) if newline != "\n" else self.cleaned)
        paths = {"cleaned": p}
        if self.cleaned_v3:
            ap = os.path.join(out_dir, os.path.splitext(basename)[0] + ".auto.txt")
            with open(ap, "w", encoding="utf-8", newline="") as f:
                f.write(self.cleaned_v3.replace("\n", newline) if newline != "\n" else self.cleaned_v3)
            paths["auto_v3"] = ap
        return paths


def _install_mock_pipeline(monkeypatch, captured_names=None, delay: float = 0.0, raise_err: bool = False):
    import adclean.pipeline as pl
    from types import SimpleNamespace

    class _MockEngine:
        def __init__(self):
            self.cfg = SimpleNamespace(names=(), whitelist_words=())
            self._lm_path = ""
            self._engine_kind = "locate"

        def clean_book(self, lines, progress=None, **kw):
            return {}

        def clean_line(self, line):
            return SimpleNamespace(removed=())

        def _line_score(self, chars, rm):
            return 0.0

    mock_eng = _MockEngine()
    monkeypatch.setattr(srv, "get_engine", lambda lm_path="": mock_eng)

    class _MockCancelled(Exception):
        pass

    monkeypatch.setattr(pl, "JobCancelledError", _MockCancelled, raising=False)

    def fake_run_book(text, engine, cfg, book_id="", resolver=None, cancel_check=None, names=None):
        if captured_names is not None:
            captured_names.append(tuple(names if names is not None else getattr(engine.cfg, "names", ())))
        if raise_err:
            raise RuntimeError("boom-pipeline-error")
        if delay:
            deadline = time.time() + delay
            while time.time() < deadline:
                if (cancel_check and cancel_check()) or (getattr(cfg, "cancel", None) and cfg.cancel()):
                    raise _MockCancelled("cancelled")
                time.sleep(0.02)

        lines = text.split("\n")
        # 构造第 2 行 ad_line，第 3 行 auto_delete + llm_delete（同处多条变动），第 4 行 llm_keep
        l2 = lines[1] if len(lines) > 1 else "广告行"
        l3 = lines[2] if len(lines) > 2 else "正常开头[广告甲]中间文本[广告乙]结尾。"
        l4 = lines[3] if len(lines) > 3 else "疑似误杀的正文句子。"
        l3_v3 = l3.replace("[广告甲]", "")
        l3_final = l3_v3.replace("[广告乙]", "")

        cleaned_lines = list(lines)
        if len(cleaned_lines) > 1:
            cleaned_lines[1] = ""
        if len(cleaned_lines) > 2:
            cleaned_lines[2] = l3_final
        v3_lines = list(lines)
        if len(v3_lines) > 1:
            v3_lines[1] = ""
        if len(v3_lines) > 2:
            v3_lines[2] = l3_v3

        items = [
            {
                "line_no": 2,
                "kind": "ad_line",
                "source": "ads",
                "reason": "整行广告水印",
                "confidence": 0.99,
                "before": l2,
                "after": "",
                "context_prev": [lines[0]] if lines else [],
                "context_next": [l3],
                "default_accepted": True,
            },
            {
                "line_no": 3,
                "kind": "auto_delete",
                "source": "locator",
                "reason": "定位器检测到插入广告",
                "confidence": 0.92,
                "before": l3,
                "after": l3_v3,
                "context_prev": [l2],
                "context_next": [l4],
                "default_accepted": True,
            },
            {
                "line_no": 3,
                "kind": "llm_delete",
                "source": "llm",
                "reason": "LLM 确认尾部广告",
                "confidence": 0.88,
                "before": l3_v3,
                "after": l3_final,
                "context_prev": [l2],
                "context_next": [l4],
                "default_accepted": True,
            },
            {
                "line_no": 4,
                "kind": "llm_keep",
                "source": "llm",
                "reason": "属于正常叙事对话，建议保留",
                "confidence": 0.95,
                "before": l4,
                "after": l4,
                "context_prev": [l3_final],
                "context_next": [],
                "default_accepted": False,
            },
        ]
        return _MockPipelineResult(
            book_id=book_id,
            cleaned="\n".join(cleaned_lines),
            cleaned_v3="\n".join(v3_lines),
            lm_mode="auto_fit",
            llm_usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            llm_stats={"ok": 2, "fail": 0},
            edit_items=items,
        )

    monkeypatch.setattr(pl, "run_book", fake_run_book)
    return mock_eng


def test_full_review_apply_rollback_diff_export_loop(ui, tmp_path, monkeypatch):
    base, _ = ui
    captured_names = []
    mock_eng = _install_mock_pipeline(monkeypatch, captured_names=captured_names)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    book = src_dir / "小说测试.txt"
    book.write_text(
        "第一章\n"
        "QQ群：888888\n"
        "正常开头[广告甲]中间文本[广告乙]结尾。\n"
        "疑似误杀的正文句子。\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out_clean"

    # 启动 pipeline，测试中文逗号/顿号/换行白名单解析且不污染共享 eng.cfg
    s, _, start_res = jpost(base + "/api/pipeline", {
        "mode": "auto",
        "files": [str(book)],
        "out": str(out_dir),
        "options": {"simplify": True, "names": "林晚，陈默、苏晴\n陆沉"},
    })
    assert s == 200
    jid = start_res["job"]
    job = _wait_job(base, jid)
    assert job["state"] == "done" and job["progress"] == 100
    assert captured_names[0] == ("林晚", "陈默", "苏晴", "陆沉")
    assert mock_eng.cfg.names == ()

    # 检查 summary 与 files 元数据
    sm = job["summary"]
    assert sm["file_count"] == 1
    assert sm["total_items"] == 4
    assert sm["accepted_items"] == 3
    assert sm["ad_lines"] == 1
    assert sm["auto_deleted"] == 1
    assert sm["llm_deleted"] == 1
    assert sm["llm_kept"] == 1
    assert sm["prompt_tokens"] == 120 and sm["total_tokens"] == 160
    assert sm["lm_mode"] == "auto_fit"
    f0 = job["files"][0]
    assert f0["dirty"] is False and f0["item_count"] == 4

    # 分页与筛选 /api/job/items
    s, _, items_p1 = jget(f"{base}/api/job/items?job={jid}&page=1&page_size=2")
    assert s == 200 and len(items_p1["items"]) == 2 and items_p1["total"] == 4 and items_p1["total_pages"] == 2
    assert items_p1["counts"]["auto_delete"] == 1 and items_p1["counts"]["accepted"] == 3

    s, _, kw_items = jget(f"{base}/api/job/items?job={jid}&kind=auto_delete&q=" + urllib.parse.quote("广告甲"))
    assert s == 200 and len(kw_items["items"]) == 1
    auto_del_id = kw_items["items"][0]["id"]

    # 人工撤回第 3 行的 auto_delete，保留同行的 llm_delete -> 验证同一行非重叠重构！
    s, _, dec_res = jpost(base + "/api/job/decisions", {
        "job": jid,
        "updates": [{"id": auto_del_id, "accepted": False}],
    })
    assert s == 200 and dec_res["updated"] == 1
    assert dec_res["files"][0]["dirty"] is True
    assert dec_res["summary"]["accepted_items"] == 2

    # Diff 实时预览应反映：第 3 行仅去掉 [广告乙]，保留 [广告甲]
    s, _, diff_res = jget(f"{base}/api/job/diff?job={jid}&file_id={f0['file_id']}&only_changed=true")
    assert s == 200 and diff_res["changed_lines"] == 2
    line3_diff = [r for r in diff_res["lines"] if r["line_no"] == 3][0]
    assert line3_diff["after"] == "正常开头[广告甲]中间文本结尾。"

    # 应用审校重生成终稿 -> 原子写盘并清除 dirty
    s, _, app_res = jpost(base + "/api/job/apply", {"job": jid})
    assert s == 200 and app_res["files"][0]["dirty"] is False
    disk_text = open(f0["output_path"], encoding="utf-8").read()
    assert "正常开头[广告甲]中间文本结尾。" in disk_text

    # 批量采纳
    s, _, bulk_res = jpost(base + "/api/job/decisions", {
        "job": jid,
        "bulk": {"file_id": f0["file_id"], "kind": "auto_delete", "accepted": True},
    })
    assert s == 200 and bulk_res["updated"] == 1
    jpost(base + "/api/job/apply", {"job": jid})
    assert "正常开头中间文本结尾。" in open(f0["output_path"], encoding="utf-8").read()

    # 回滚至 auto 初稿
    s, _, rb_auto = jpost(base + "/api/job/rollback", {"job": jid, "file_id": f0["file_id"], "target": "auto"})
    assert s == 200 and rb_auto["files"][0]["dirty"] is False
    assert "正常开头中间文本[广告乙]结尾。" in open(f0["output_path"], encoding="utf-8").read()

    # 回滚至 original 原文
    s, _, rb_orig = jpost(base + "/api/job/rollback", {"job": jid, "file_id": f0["file_id"], "target": "original"})
    assert s == 200 and rb_orig["summary"]["accepted_items"] == 0
    assert "QQ群：888888" in open(f0["output_path"], encoding="utf-8").read()

    # 下载单文件与导出 ZIP
    s, hdrs, body = req("GET", f"{base}/api/download?job={jid}&file_id={f0['file_id']}")
    assert s == 200
    assert hdrs.get("X-Content-Type-Options") == "nosniff"
    assert hdrs.get("Cache-Control") == "no-store"
    assert "filename*=UTF-8''" in hdrs.get("Content-Disposition", "")
    assert "QQ群：888888" in body.decode("utf-8")

    s, hdrs, zbytes = req("GET", f"{base}/api/job/export-zip?job={jid}")
    assert s == 200 and hdrs.get("Content-Type") == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        assert "小说测试.txt" in zf.namelist()


def test_job_error_populates_error_and_trace(ui, tmp_path, monkeypatch):
    base, _ = ui
    _install_mock_pipeline(monkeypatch, raise_err=True)
    book = tmp_path / "b.txt"
    book.write_text("测试内容。\n", encoding="utf-8")
    s, _, start_res = jpost(base + "/api/pipeline", {
        "mode": "auto", "files": [str(book)], "out": str(tmp_path / "out"),
    })
    assert s == 200
    j = _wait_job(base, start_res["job"])
    assert j["state"] == "error"
    assert "boom-pipeline-error" in j["error"]
    assert "boom-pipeline-error" in j["message"]
    assert "Traceback" in j["trace"]


def test_cancel_validation_and_real_cancellation(ui, tmp_path, monkeypatch):
    base, _ = ui
    # 1. 不存在的任务返回 404 且不建幽灵键
    s, _, _ = jpost(base + "/api/job/cancel", {"job": "not_exist_999"})
    assert s == 404
    assert "not_exist_999" not in srv.CANCEL
    assert "not_exist_999" not in srv.JOBS

    # 2. 运行中任务真实取消
    _install_mock_pipeline(monkeypatch, delay=2.0)
    book = tmp_path / "b.txt"
    book.write_text("一行。\n二行。\n", encoding="utf-8")
    s, _, start_res = jpost(base + "/api/pipeline", {
        "mode": "auto", "files": [str(book)], "out": str(tmp_path / "out"),
    })
    jid = start_res["job"]
    time.sleep(0.1)
    s, _, cj = jpost(base + "/api/job/cancel", {"job": jid})
    assert s == 200 and cj["ok"] is True
    j = _wait_job(base, jid)
    assert j["state"] == "cancelled"
    assert j["progress"] < 100


def test_jobs_lru_and_ttl_cleanup():
    with srv.JOBS_LOCK:
        srv.JOBS.clear()
        srv.JOB_DATA.clear()
        srv.CANCEL.clear()
        srv.JOB_OUTPUTS.clear()
        now = time.time()
        # 放入 1 个超期任务和 25 个新结束任务
        srv.JOBS["expired"] = {"job": "expired", "state": "done", "started": now - 10000, "updated": now - 10000}
        for i in range(25):
            jid = f"j{i}"
            srv.JOBS[jid] = {"job": jid, "state": "done", "started": now - 100 + i, "updated": now - 100 + i}
            srv.JOB_DATA[jid] = {}
        srv._cleanup_jobs_locked(now)
        assert "expired" not in srv.JOBS
        assert len(srv.JOBS) == srv.MAX_JOBS
        assert "j0" not in srv.JOBS and "j24" in srv.JOBS


# --------------------------------------------------------------- 校验与端口独占
def test_pipeline_validations(ui, tmp_path):
    base, _ = ui
    book = tmp_path / "novel.txt"
    book.write_text("测试正文。\n", encoding="utf-8")

    # LLM 模式未配置 API 地址 -> 400
    s, _, j = jpost(base + "/api/pipeline", {"mode": "llm", "files": [str(book)], "out": str(tmp_path / "o")})
    assert s == 400 and "API 地址" in j["error"]

    # 无可处理文件 -> 400
    s, _, _ = jpost(base + "/api/pipeline", {"mode": "auto", "files": [], "out": "x"})
    assert s == 400

    # 覆盖源文件保护 -> 400
    s, _, j = jpost(base + "/api/pipeline", {"mode": "auto", "files": [str(book)], "out": str(tmp_path)})
    assert s == 400 and "原文件" in j["error"]

    # 下载任意路径拒绝 -> 403
    s, _, _ = req("GET", base + "/api/download?path=" + urllib.parse.quote(r"C:\Windows\win.ini"))
    assert s == 403

    # 未知任务 -> 404
    s, _, _ = req("GET", base + "/api/job?job=nope")
    assert s == 404


def test_gui_start_server_binds_atomically():
    httpd, port = gui.start_server(port=0, prefer=0)
    try:
        assert port > 0
        assert httpd.server_address[1] == port
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(os.name != "nt", reason="Windows SO_REUSEADDR 语义特定")
def test_port_is_exclusive_on_windows():
    s1 = srv.AdcleanHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = s1.server_address[1]
    try:
        with pytest.raises(OSError):
            srv.AdcleanHTTPServer(("127.0.0.1", port), srv.Handler)
    finally:
        s1.server_close()


def test_serve_reports_busy_port(capsys):
    s1 = srv.AdcleanHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = s1.server_address[1]
    try:
        rc = srv.serve(port, open_browser=False)
        assert rc == 1
        out = capsys.readouterr().out
        assert "无法在" in out and "端口可能已被占用" in out
    finally:
        s1.server_close()


def test_real_engine_end_to_end_pipeline_and_review(ui, tmp_path, monkeypatch):
    """不 mock 引擎与管线，真实端到端验证上传 -> auto_fit 定位 -> 审校 -> apply -> rollback -> zip。"""
    base, _ = ui
    # 重置全局引擎缓存，确保真实加载原生 LightGBM 模型
    monkeypatch.setattr(srv, "_ENGINE", {"obj": None, "path": "", "error": ""})

    sample_lines = [
        "第一章 初入江湖",
        "清晨的微风拂过青石板路，林晚背着行囊缓缓走来。",
        "更多精彩小说尽在www.example-ad.com，请收藏本站！",
        "陈默在茶馆二楼靠窗的位置坐下，点了一壶碧螺春。",
        "QQ群：987654321",
        "两人相视一笑，决定明日一早便动身前往江南。",
    ] * 8
    raw_bytes = ("\n".join(sample_lines) + "\n").encode("utf-8")

    s, _, b = req(
        "POST",
        f"{base}/api/upload?filename={urllib.parse.quote('真实联调.txt')}&session_id=e2e_sess",
        headers={"Content-Type": "application/octet-stream"},
        raw_bytes=raw_bytes,
    )
    assert s == 200
    up_file = json.loads(b.decode("utf-8"))["file"]["path"]

    out_dir = tmp_path / "e2e_out"
    s, _, start_res = jpost(base + "/api/pipeline", {
        "mode": "auto",
        "files": [up_file],
        "out": str(out_dir),
        "options": {"simplify": True, "names": "林晚，陈默"},
    })
    assert s == 200
    job = _wait_job(base, start_res["job"], timeout=30.0)
    assert job["state"] == "done", job
    assert job["summary"]["file_count"] == 1
    assert job["summary"]["lm_mode"] in ("auto_fit", "file")
    assert job["summary"]["total_items"] >= 1

    # 检查共享引擎未被写入任务级白名单
    with srv.ENGINE_LOCK:
        eng_obj = srv._ENGINE["obj"]
        assert eng_obj is not None
        assert getattr(eng_obj.cfg, "names", ()) == ()

    # 获取审校条目，撤回第一条并重生成终稿
    s, _, items_res = jget(f"{base}/api/job/items?job={start_res['job']}&page=1&page_size=20")
    assert s == 200 and items_res["total"] >= 1
    first_id = items_res["items"][0]["id"]
    s, _, dec_res = jpost(base + "/api/job/decisions", {
        "job": start_res["job"],
        "updates": [{"id": first_id, "accepted": False}],
    })
    assert s == 200 and dec_res["files"][0]["dirty"] is True

    s, _, app_res = jpost(base + "/api/job/apply", {"job": start_res["job"]})
    assert s == 200 and app_res["files"][0]["dirty"] is False

    # 全文 diff 与 ZIP 导出
    fid = job["files"][0]["file_id"]
    s, _, diff_res = jget(f"{base}/api/job/diff?job={start_res['job']}&file_id={fid}&only_changed=true")
    assert s == 200 and "lines" in diff_res

    s, hdrs, zbytes = req("GET", f"{base}/api/job/export-zip?job={start_res['job']}")
    assert s == 200 and hdrs.get("Content-Type") == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        assert "真实联调.txt" in zf.namelist()

