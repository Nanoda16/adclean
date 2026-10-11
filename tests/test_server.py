# -*- coding: utf-8 -*-
"""adclean.server 的 HTTP 层测试。

所有 LLM 请求都打向本地伪造的 OpenAI 兼容端点，配置一律写到 tmp_path。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean import server as srv

FAKE_KEY = "sk-test-SECRET-abcdefghijklmnop"


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
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
        self._json(200, {"choices": [{"message": {
            "content": '{"decision":"keep","confidence":0.9,"reason":"t"}'}}]})


@pytest.fixture
def fake_llm():
    s = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/v1" % s.server_address[1]
    s.shutdown()
    s.server_close()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """起一个真的 adclean HTTP 服务，配置目录指向 tmp_path。"""
    monkeypatch.setenv("ADCLEAN_HOME", str(tmp_path))
    monkeypatch.setattr(srv, "_LLM_CFG", {"obj": None})
    s = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % s.server_address[1]
    yield base, tmp_path
    s.shutdown()
    s.server_close()
    srv.JOB_OUTPUTS.clear()
    srv.CANCEL.clear()
    srv.JOBS.clear()


def req(method, url, body=None, origin=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if origin:
        r.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def jget(url):
    s, t = req("GET", url)
    return s, json.loads(t)


def jpost(url, body, origin=None):
    s, t = req("POST", url, body, origin)
    try:
        return s, json.loads(t)
    except ValueError:
        return s, t


# --------------------------------------------------------------- 静态页
def test_index_served(ui):
    base, _ = ui
    s, t = req("GET", base + "/")
    assert s == 200
    for token in ("自动清洗", "LLM 审查", "LLM 连接",
                  "获取模型列表", "测试连接", "未应用 LLM"):
        assert token in t, token
    # 旧版引擎已下线，不该再出现在界面上
    for gone in ("旧版引擎", "tab-old"):
        assert gone not in t, gone


def test_unknown_route_404(ui):
    base, _ = ui
    s, _ = req("GET", base + "/api/nope")
    assert s == 404


# --------------------------------------------------------------- LLM 配置
def test_config_get_has_no_plain_key(ui):
    base, _ = ui
    s, j = jget(base + "/api/llm/config")
    assert s == 200
    assert "api_key" not in j["config"]
    assert j["config"]["has_key"] is False
    assert j["config_path"].startswith(str(ui[1]))


def test_config_save_and_mask(ui, fake_llm):
    base, tmp = ui
    s, j = jpost(base + "/api/llm/config",
                 {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200
    assert FAKE_KEY not in json.dumps(j, ensure_ascii=False), "回执泄漏了明文密钥"
    assert j["config"]["has_key"] is True
    assert j["config"]["key_masked"].startswith("sk-t")
    # 落盘在临时目录，文件里确实有 key（save_key 默认 True）
    p = os.path.join(str(tmp), "llm.json")
    assert os.path.exists(p)
    assert json.load(open(p, encoding="utf-8"))["api_key"] == FAKE_KEY


def test_config_keeps_stored_key_when_body_omits_it(ui, fake_llm):
    base, _ = ui
    jpost(base + "/api/llm/config",
          {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    s, j = jpost(base + "/api/llm/config", {"model": "m-2"})   # 不带 api_key
    assert s == 200
    assert j["config"]["has_key"] is True, "省略 api_key 时不应清空已存的 key"
    assert j["config"]["model"] == "m-2"


def test_config_clear_key(ui, fake_llm):
    base, _ = ui
    jpost(base + "/api/llm/config", {"base_url": fake_llm, "api_key": FAKE_KEY})
    s, j = jpost(base + "/api/llm/config", {"clear_key": True})
    assert j["config"]["has_key"] is False


def test_no_save_key_does_not_persist(ui, fake_llm):
    base, tmp = ui
    jpost(base + "/api/llm/config",
          {"base_url": fake_llm, "api_key": FAKE_KEY, "save_key": False})
    p = os.path.join(str(tmp), "llm.json")
    assert json.load(open(p, encoding="utf-8"))["api_key"] == ""


def test_models_and_probe(ui, fake_llm):
    base, _ = ui
    s, j = jpost(base + "/api/llm/models",
                 {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200 and j["models"] == ["m-1", "m-2"]
    s, j = jpost(base + "/api/llm/probe",
                 {"base_url": fake_llm, "api_key": FAKE_KEY, "model": "m-1"})
    assert s == 200 and j["ok"] is True


def test_models_bad_endpoint_reports_error(ui):
    base, _ = ui
    s, j = jpost(base + "/api/llm/models",
                 {"base_url": "http://127.0.0.1:1/v1", "api_key": "x",
                  "timeout": 2, "retries": 0})
    assert s == 200 and j["models"] == [] and j["error"]


# --------------------------------------------------------------- 安全边界
def test_csrf_rejects_foreign_origin(ui):
    base, _ = ui
    s, _ = jpost(base + "/api/llm/config", {"model": "x"},
                 origin="http://evil.example.com")
    assert s == 403


def test_csrf_accepts_localhost_origin(ui):
    """浏览器对同源 POST 也会带 Origin，必须放行，否则 UI 不可用。"""
    base, _ = ui
    for origin in (base, base.replace("127.0.0.1", "localhost")):
        s, j = jpost(base + "/api/llm/config", {"model": "m"}, origin=origin)
        assert s == 200, origin
    # 无 Origin（curl 等非浏览器客户端）也应放行
    s, _ = jpost(base + "/api/llm/config", {"model": "m"})
    assert s == 200


def test_download_rejects_arbitrary_path(ui):
    base, _ = ui
    s, _ = req("GET", base + "/api/download?path=" +
               urllib.parse.quote(r"C:\Windows\win.ini"))
    assert s == 403


def test_job_unknown_404(ui):
    base, _ = ui
    s, _ = req("GET", base + "/api/job?id=nope")
    assert s == 404


def test_cancel_endpoint(ui):
    base, _ = ui
    s, j = jpost(base + "/api/job/cancel", {"job": "abc"})
    assert s == 200 and j["ok"] is True
    assert srv.CANCEL["abc"].is_set()


def test_review_requires_llm_config(ui):
    base, tmp = ui
    book = tmp / "b.txt"
    book.write_text("正常的一行文字。\n", encoding="utf-8")
    s, j = jpost(base + "/api/pipeline/review",
                 {"files": [str(book)], "out": str(tmp / "o"), "options": {}})
    assert s == 400 and "API 地址" in j["error"]


def test_clean_requires_files(ui):
    base, _ = ui
    s, j = jpost(base + "/api/pipeline/clean", {"files": [], "out": "x"})
    assert s == 400


# --------------------------------------------------------------- 端口独占
@pytest.mark.skipif(os.name != "nt", reason="Windows SO_REUSEADDR 语义特定")
def test_port_is_exclusive_on_windows():
    """Windows 上必须用 SO_EXCLUSIVEADDRUSE 独占端口，SO_REUSEADDR 会让两个进程绑同一端口。"""
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
        assert "--port" in out, "应提示换个端口"
    finally:
        s1.server_close()


def test_serve_restart_same_port_ok():
    """停止后立刻在同一端口重启不应被 TIME_WAIT 挡住。"""
    s1 = srv.AdcleanHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = s1.server_address[1]
    s1.server_close()
    s2 = srv.AdcleanHTTPServer(("127.0.0.1", port), srv.Handler)
    assert s2.server_address[1] == port
    s2.server_close()


import urllib.parse  # noqa: E402  (末尾导入，避免与上面的导入混淆)
