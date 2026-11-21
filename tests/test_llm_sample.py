# -*- coding: utf-8 -*-
"""审校条目抽取与采样：覆盖 PipelineResult.edit_items 与 server._extract_edit_items_from_result。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.server import _extract_edit_items_from_result          # noqa: E402


def _engine_entry(i: int) -> dict:
    return {"location": {"chapter": "c", "line": i + 1, "start": 0, "end": 10},
            "original": "原文%d" % i, "replacement": "", "deleted": "霖",
            "result_text": "改后%d" % i, "method": "rule_engine",
            "engine": "locator", "severity": "HIGH", "confidence": 0.9}


def _llm_entry(i: int) -> dict:
    return {"location": {"chapter": "c", "line": i + 1, "start": 0, "end": 10},
            "original": "原文%d" % i, "before_llm": "引擎结果%d" % i,
            "result_text": "LLM 改后%d" % i,
            "replacement": "（", "deleted": "揪",
            "method": "llm", "engine": "llm_review", "resolver": "test",
            "model": "m1", "decision": "repair", "selected_candidate": "llm_ops",
            "confidence": 0.9, "reason": "删掉注入字符"}


def _fake_result(n_engine=60, n_llm=40):
    log = [_engine_entry(i) for i in range(n_engine)]
    log += [_llm_entry(i) for i in range(n_llm)]
    return SimpleNamespace(change_log=log, review_queue=[], seconds=1.0)


def test_llm_sample_reaches_entries_after_the_first_40():
    orig_lines = ["原文%d" % i for i in range(120)]
    items = _extract_edit_items_from_result(_fake_result(), orig_lines, "f1", "x.txt", "locate")
    llm_items = [it for it in items if it["kind"] == "llm_delete"]
    assert len(llm_items) == 40
    e = llm_items[0]
    for k in ("line_no", "kind", "source", "before", "after", "confidence", "reason", "default_accepted"):
        assert k in e, "缺字段: %s" % k
    assert e["before"] == "引擎结果0" and e["after"] == "LLM 改后0"
    assert e["reason"] == "删掉注入字符"
    assert e["default_accepted"] is True
    auto_items = [it for it in items if it["kind"] in ("auto_delete", "ad_line")]
    assert len(auto_items) == 60


def test_llm_sample_empty_when_no_llm_changes():
    orig_lines = ["原文%d" % i for i in range(60)]
    items = _extract_edit_items_from_result(
        _fake_result(n_engine=50, n_llm=0), orig_lines, "f1", "x.txt", "locate")
    assert [it for it in items if it["kind"] == "llm_delete"] == []
    assert len(items) == 50


def _qitem(i, decision, text="句子"):
    """造一个 ReviewItem 形状的假对象。"""
    return SimpleNamespace(
        item_id="b:L%d" % (i + 1), line_start=i + 1, line_end=i + 1,
        severity="HIGH", priority=1, reason_code="v3_left",
        suspicious_text=text, evidence=[], candidate_repairs=[],
        decision=decision, selected_candidate="", operations=[],
        confidence=0.8, reason="不是广告", v3_result={})


def test_llm_kept_sample_covers_reviewed_but_unchanged():
    """LLM 看过但决定不动的行也要能进入审校列表（且 default_accepted=False）。"""
    r = _fake_result(n_engine=5, n_llm=1)
    r.review_queue = [_qitem(i + 10, "keep" if i % 2 else "uncertain")
                      for i in range(12)]
    orig_lines = ["句子%d" % i for i in range(30)]
    items = _extract_edit_items_from_result(r, orig_lines, "f1", "x.txt", "locate")
    kept = [it for it in items if it["kind"] in ("llm_keep", "uncertain")]
    assert len(kept) == 12
    for it in kept:
        assert it["default_accepted"] is False
        assert it["source"] == "llm"


def test_llm_kept_sample_skips_repair_items():
    """已经被改过的行不该出现在"判定保留"里。"""
    r = _fake_result(n_engine=1, n_llm=1)
    r.review_queue = [_qitem(0, "repair", "改过的"),
                      _qitem(1, "keep", "保留的")]
    orig_lines = ["原文0", "原文1", "原文2"]
    items = _extract_edit_items_from_result(r, orig_lines, "f1", "x.txt", "locate")
    kept = [it for it in items if it["kind"] == "llm_keep"]
    assert [x["line_no"] for x in kept] == [2]
