# -*- coding: utf-8 -*-
"""adclean.llm 单元测试 —— 全部对**本地伪造的 OpenAI 兼容端点**打，
绝不调用任何真实第三方 API。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.llm import (DEFAULT_CONCURRENCY, DEFAULT_MAX_ITEMS,
                         DEFAULT_RETRIES, DEFAULT_TIMEOUT, LLMConfig, LLMError,
                         OpenAICompatResolver, chat, list_models, load_config,
                         probe_chat, save_config)
from adclean.review import (DECISION_UNCERTAIN, CandidateRepair, ReviewItem,
                            parse_decision)

STATE: dict = {}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._raw(code, body, "application/json")

    def _raw(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            pass          # 客户端因超时先断开，测试里忽略

    def do_GET(self):
        STATE["auth"] = self.headers.get("Authorization", "")
        if STATE.get("models_status", 200) != 200:
            return self._send(STATE["models_status"], {"error": "denied"})
        if STATE.get("bad_body"):
            return self._raw(200, b"<html>not json</html>", "text/html")
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"data": [{"id": "m-b"}, {"id": "m-a"},
                                             {"id": "m-a"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        STATE["auth"] = self.headers.get("Authorization", "")
        STATE["calls"] = STATE.get("calls", 0) + 1
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            payload = {}
        STATE["last_payload"] = payload
        STATE["paths"] = STATE.get("paths", []) + [self.path]
        if STATE.get("delay"):
            time.sleep(STATE["delay"])
        if STATE.get("fail_first", 0) > 0:
            STATE["fail_first"] -= 1
            return self._send(STATE.get("fail_status", 429), {"error": "slow down"})
        if STATE.get("bad_body"):
            return self._raw(200, b"<html>gateway error</html>", "text/html")
        if STATE.get("echo_candidate"):
            # 从 prompt 里读出系统给出的候选，回选第一个"能改动"的
            txt = json.dumps(payload, ensure_ascii=False)
            ids = re.findall(r"(candidate_\d+):\s*删除\[", txt)
            if "restore（撤销引擎改动，恢复原文）" in txt:
                dec = {"decision": "repair", "selected_candidate": "restore",
                       "confidence": 0.93, "reason": "假端点：撤销引擎改动"}
            elif ids:
                dec = {"decision": "repair", "selected_candidate": ids[0],
                       "confidence": 0.93, "reason": "假端点：回选首个可删候选"}
            else:
                dec = {"decision": "keep", "confidence": 0.9,
                       "reason": "假端点：没有可改动的候选"}
            return self._send(200, {"choices": [{"message": {
                "content": json.dumps(dec, ensure_ascii=False)}}]})
        content = ("oops" if STATE.get("bad_content") else
                   json.dumps({"decision": "repair",
                               "selected_candidate": "candidate_1",
                               "confidence": 0.9, "reason": "假端点",
                               **({"final_text": STATE["final_text"]}
                                  if STATE.get("final_text") else {})},
                              ensure_ascii=False))
        return self._send(200, {"choices": [{"message": {"content": content}}]})


@pytest.fixture
def fake():
    STATE.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    yield "http://127.0.0.1:%d/v1" % port
    srv.shutdown()
    srv.server_close()


def cfg_for(base: str, **kw) -> LLMConfig:
    c = LLMConfig(base_url=base, api_key="sk-test-1234567890", model="m-a", **kw)
    return c.clamp()


def item(i: int = 1) -> ReviewItem:
    return ReviewItem(
        item_id="b:L%d" % i, book_id="b", chapter_id="c",
        line_start=i, line_end=i, start=0, end=5, severity="HIGH",
        context_before="", suspicious_text="她冲到常榕霖身前。", context_after="",
        evidence=[{"type": "statistical_anomaly", "score": 3.0}],
        candidate_repairs=[CandidateRepair("candidate_1", [[4, 5]], -10.0, "霖"),
                           CandidateRepair("candidate_2", [], -30.0, "")])


# ------------------------------------------------------------- 端点归一化
def test_endpoint_normalization():
    cases = [
        ("https://api.x.com", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1/", "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/v1/chat/completions",
         "https://api.x.com/v1/chat/completions"),
        ("https://api.x.com/openai/v1", "https://api.x.com/openai/v1/chat/completions"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
    ]
    for given, want in cases:
        c = LLMConfig(base_url=given)
        assert c.chat_url() == want, given
        assert c.models_url() == want.replace("/chat/completions", "/models")


def test_missing_base_url_raises():
    with pytest.raises(LLMError):
        LLMConfig(base_url="").chat_url()


def test_clamp_bounds():
    c = LLMConfig(base_url="http://a", timeout=0.01, retries=99,
                  concurrency=999, max_items=-5, max_tokens=99999).clamp()
    assert c.timeout >= 1.0 and c.retries <= 6
    assert 1 <= c.concurrency <= 16 and c.max_items == 0 and c.max_tokens <= 8192


# ------------------------------------------------------------- 模型列表
def test_list_models_sorted_unique(fake):
    ids = list_models(cfg_for(fake))
    assert ids == ["m-a", "m-b"]
    assert STATE["auth"] == "Bearer sk-test-1234567890"


def test_list_models_uses_models_path(fake):
    list_models(cfg_for(fake))
    assert STATE["auth"].startswith("Bearer ")


def test_list_models_401_error_has_no_key(fake):
    STATE["models_status"] = 401
    with pytest.raises(LLMError) as ei:
        list_models(cfg_for(fake))
    msg = str(ei.value)
    assert "401" in msg
    assert "sk-test-1234567890" not in msg, "错误消息里泄漏了 api_key"


# ------------------------------------------------------------- chat
def test_chat_returns_content(fake):
    out = chat(cfg_for(fake), [{"role": "user", "content": "hi"}])
    assert "candidate_1" in out
    assert STATE["last_payload"]["model"] == "m-a"
    assert STATE["last_payload"]["stream"] is False


def test_chat_sends_json_mode_then_degrades(fake):
    """不支持 response_format 的中转：第一次带、400 后去掉再试。"""
    chat(cfg_for(fake), [{"role": "user", "content": "hi"}])
    assert STATE["last_payload"].get("response_format") == {"type": "json_object"}


def test_chat_retries_on_429(fake):
    STATE["fail_first"] = 1
    out = chat(cfg_for(fake, retries=1), [{"role": "user", "content": "hi"}])
    assert "candidate_1" in out
    assert STATE["calls"] == 2


def test_chat_gives_up_after_retries(fake):
    STATE["fail_first"] = 9
    with pytest.raises(LLMError) as ei:
        chat(cfg_for(fake, retries=1), [{"role": "user", "content": "hi"}])
    assert "429" in str(ei.value)
    assert STATE["calls"] == 2          # 1 次 + 1 次重试


def test_chat_timeout(fake):
    STATE["delay"] = 2.0
    t0 = time.time()
    with pytest.raises(LLMError) as ei:
        chat(cfg_for(fake, timeout=1.0, retries=0),
             [{"role": "user", "content": "hi"}])
    assert "超时" in str(ei.value)
    assert time.time() - t0 < 1.9, "超时没有生效"


def test_chat_body_not_json(fake):
    """HTTP 200 但响应体不是 JSON → 必须报错，不能静默当成内容。"""
    STATE["bad_body"] = True
    with pytest.raises(LLMError) as ei:
        chat(cfg_for(fake, retries=0), [{"role": "user", "content": "hi"}])
    assert "合法 JSON" in str(ei.value)


def test_chat_content_not_json_is_returned_raw(fake):
    """content 本身不是 JSON 时 chat() 原样返回 —— 解析是 review.parse_decision 的事。"""
    STATE["bad_content"] = True
    out = chat(cfg_for(fake, retries=0), [{"role": "user", "content": "hi"}])
    assert out == "oops"
    assert parse_decision(out)["decision"] == DECISION_UNCERTAIN


def test_list_models_body_not_json(fake):
    STATE["bad_body"] = True
    with pytest.raises(LLMError) as ei:
        list_models(cfg_for(fake, retries=0))
    assert "合法 JSON" in str(ei.value)


def test_chat_requires_model(fake):
    with pytest.raises(LLMError):
        chat(LLMConfig(base_url=fake, model=""), [{"role": "user", "content": "x"}])


def test_probe_chat(fake):
    r = probe_chat(cfg_for(fake))
    assert r["model"] == "m-a" and r["parsed"]["decision"] == "repair"


# ------------------------------------------------------------- Resolver
def test_resolver_prepare_and_resolve(fake):
    r = OpenAICompatResolver(cfg_for(fake, concurrency=3))
    items = [item(i) for i in range(1, 6)]
    st = r.prepare(items)
    assert st["ok"] == 5 and st["fail"] == 0 and st["total"] == 5
    for it in items:
        d = r.resolve(it)
        assert d["decision"] == "repair"
        assert d["selected_candidate"] == "candidate_1"


def test_resolver_max_items_cap(fake):
    r = OpenAICompatResolver(cfg_for(fake, max_items=2, concurrency=2))
    items = [item(i) for i in range(1, 6)]
    st = r.prepare(items)
    assert st["ok"] == 2 and st["skipped"] == 3
    assert STATE["calls"] == 2, "超出上限的条目不应发起请求"
    skipped = r.resolve(items[4])
    assert skipped["decision"] == DECISION_UNCERTAIN
    assert "超出单次上限" in skipped["reason"]


def test_resolver_progress_callback(fake):
    seen = []
    r = OpenAICompatResolver(cfg_for(fake, concurrency=2),
                             progress=lambda d, t, ok, fail: seen.append((d, t)))
    r.prepare([item(i) for i in range(1, 4)])
    assert seen and seen[-1] == (3, 3)


def test_resolver_failure_becomes_uncertain(fake):
    STATE["fail_first"] = 99          # 永远 429
    r = OpenAICompatResolver(cfg_for(fake, retries=0, max_items=3, concurrency=1))
    st = r.prepare([item(i) for i in range(1, 4)])
    assert st["fail"] == 3 and st["ok"] == 0
    d = r.resolve(item(1))
    assert d["decision"] == DECISION_UNCERTAIN
    assert "调用失败" in d["reason"]
    assert r.errors and "sk-test-1234567890" not in json.dumps(r.errors)


def test_resolver_cancel(fake):
    r = OpenAICompatResolver(cfg_for(fake), cancel=lambda: True)
    r.prepare([item(1)])
    assert r.resolve(item(1))["decision"] == DECISION_UNCERTAIN
    assert STATE.get("calls", 0) == 0


def test_resolver_concurrency_is_real(fake):
    """并发必须真的并发：4 条 × 0.4s，并发 4 应显著快于串行。"""
    STATE["delay"] = 0.4
    r = OpenAICompatResolver(cfg_for(fake, concurrency=4, timeout=10))
    t0 = time.time()
    r.prepare([item(i) for i in range(1, 5)])
    el = time.time() - t0
    assert el < 1.3, "并发没生效，耗时 %.2fs" % el


def test_resolver_cancel_mid_run(fake):
    flag = {"n": 0}

    def cb(d, t, ok, fail):
        flag["n"] = d
    r = OpenAICompatResolver(cfg_for(fake, concurrency=1, max_items=5),
                             progress=cb)
    r.prepare([item(i) for i in range(1, 6)])
    assert flag["n"] == 5


# ------------------------------------------------------------- 密钥
def test_public_dict_hides_key():
    c = LLMConfig(base_url="https://x/v1", api_key="sk-secret-abcdefg",
                  model="m")
    d = c.as_public_dict()
    assert "api_key" not in d
    assert d["has_key"] is True
    assert "secret" not in json.dumps(d)
    assert d["key_masked"].startswith("sk-s")


def test_redact():
    c = LLMConfig(base_url="https://x", api_key="sk-secret-abcdefg")
    assert "abcdefg" not in c.redact("failed with sk-secret-abcdefg")
    assert "abcdefg" not in c.redact("Authorization: Bearer sk-secret-abcdefg")
    assert "sk-abc" not in c.redact("key sk-abcdefghijklmnop")


def test_config_roundtrip_and_no_save_key(tmp_path):
    p = str(tmp_path / "llm.json")
    c = LLMConfig(base_url="https://x/v1", api_key="sk-keep", model="m",
                  concurrency=3)
    save_config(c, p)
    back = load_config(p)
    assert back.api_key == "sk-keep" and back.concurrency == 3

    c.save_key = False
    save_config(c, p)                      # 勾了"不保存"
    back2 = load_config(p)
    assert back2.api_key == ""
    assert back2.base_url == "https://x/v1"   # 其它字段照常保留


def test_load_config_missing_file():
    assert load_config("Z:\\definitely\\missing\\llm.json").base_url == ""


def test_defaults_are_the_agreed_ones():
    c = LLMConfig()
    assert (c.timeout, c.retries, c.concurrency, c.max_items) == (
        DEFAULT_TIMEOUT, DEFAULT_RETRIES, DEFAULT_CONCURRENCY, DEFAULT_MAX_ITEMS)
    assert (DEFAULT_TIMEOUT, DEFAULT_RETRIES, DEFAULT_CONCURRENCY,
            DEFAULT_MAX_ITEMS) == (60.0, 2, 2, 50)


# ------------------------------------------------- 与真实 pipeline 的集成
CLEAN = [
    "常榕叹了口气坐到沙发旁边的书桌前，同时取出放在抽屉里的代办清单列表。",
    "这个房间里几天前发生过一场命案，而就在命案的前一天，自己在这里和被害人互相闲聊过。",
    "她冲到常榕身前，张开双手试图护住常榕。",
    "樱庭娜娜看到常榕之后先是怔了一下，接着立刻尴尬的想要转身离开。",
]
POLLUTED = "常榕又思索片刻后一，将（裠鸠  霖瘤是 六霓吧2紦【更换七门锁）】仪直接划八掉。"


@pytest.fixture(scope="module")
def tiny_engine():
    from adclean.v2lm import LanguageModel
    from adclean.v3engine import V3Config, V3Engine
    lm = LanguageModel().fit(CLEAN * 40)
    e = V3Engine(lm, V3Config())
    e.cfg.whitelist_words = ("常榕", "樱庭娜娜")
    return e


@pytest.fixture(autouse=True)
def _echo_default():
    STATE["echo_candidate"] = True
    yield


def test_pipeline_with_openai_resolver(fake, tiny_engine):
    """端到端：真引擎 + 假 OpenAI 端点，LLM 决策必须落到文本并进 changelog。"""
    from adclean.audit import AuditConfig
    from adclean.pipeline import PipelineConfig, run_book
    from adclean.review import NullResolver

    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)       # 全部进 LLM 队列
    cfg.audit = AuditConfig(merge_lines=False)

    r0 = run_book(text, tiny_engine, cfg, book_id="b", resolver=NullResolver())
    n_items = len(r0.review_queue)
    assert n_items >= 1
    # V3 无门禁，默认输出就是 V3 的清洗结果（不是原文）
    assert r0.cleaned != text, "V3 必须在没有 LLM 时也完成自动修复"

    res = OpenAICompatResolver(cfg_for(fake, concurrency=2, max_items=50))
    r1 = run_book(text, tiny_engine, cfg, book_id="b", resolver=res)

    assert res.stats["ok"] == n_items, res.stats
    assert res.stats["skipped"] == 0
    assert r1.report["llm_review"]["stats"]["ok"] == n_items
    assert r1.report["llm_review"]["resolver"] == "openai-compat"
    # 决策必须落地
    llm_logs = [e for e in r1.change_log if e["method"] == "llm"]
    assert llm_logs, "LLM 的 repair 决策没有落到 changelog"
    for e in llm_logs:
        assert e["selected_candidate"] in ("restore", "no_change", "llm_ops", "")
        assert e["prompt_version"] == "review-v2"


def test_pipeline_llm_respects_max_items(fake, tiny_engine):
    """单次上限：超出的条目不发请求，且保持原文。"""
    from adclean.audit import AuditConfig
    from adclean.pipeline import PipelineConfig, run_book
    from adclean.review import NullResolver

    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    r0 = run_book(text, tiny_engine, cfg, book_id="b", resolver=NullResolver())
    n_items = len(r0.review_queue)

    res = OpenAICompatResolver(cfg_for(fake, concurrency=1, max_items=1))
    r1 = run_book(text, tiny_engine, cfg, book_id="b", resolver=res)
    assert STATE["calls"] == 1, "只应发 1 次请求，实际 %d" % STATE["calls"]
    assert res.stats["ok"] == 1
    assert res.stats["skipped"] == n_items - 1
    assert r1.report["llm_review"]["stats"]["skipped"] == n_items - 1


def test_pipeline_llm_progress_hook(fake, tiny_engine):
    from adclean.audit import AuditConfig
    from adclean.pipeline import PipelineConfig, run_book

    text = "\n".join(CLEAN + [POLLUTED])
    seen = []
    cfg = PipelineConfig(llm_max_priority=3,
                         llm_progress=lambda d, t, ok, fail: seen.append((d, t)))
    cfg.audit = AuditConfig(merge_lines=False)
    res = OpenAICompatResolver(cfg_for(fake, concurrency=2))
    run_book(text, tiny_engine, cfg, book_id="b", resolver=res)
    assert seen and seen[-1][0] == seen[-1][1]


def test_pipeline_llm_all_fail_keeps_text(fake, tiny_engine):
    """LLM 全部失败 → 全部 uncertain → 原文一字不改。"""
    from adclean.audit import AuditConfig
    from adclean.pipeline import PipelineConfig, run_book

    STATE["fail_first"] = 999
    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    from adclean.review import NullResolver
    base = run_book(text, tiny_engine, cfg, book_id="b", resolver=NullResolver())
    res = OpenAICompatResolver(cfg_for(fake, retries=0, concurrency=2))
    r = run_book(text, tiny_engine, cfg, book_id="b", resolver=res)
    assert res.stats["fail"] >= 1
    # 全部失败 → 全部 uncertain → 输出必须与 V3 默认输出完全一致
    assert r.cleaned == base.cleaned
    assert not [e for e in r.change_log if e["method"] == "llm"]
