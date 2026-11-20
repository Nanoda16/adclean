# -*- coding: utf-8 -*-
"""审计汇总里的"LLM 改动抽样"必须真的能抽到 LLM 条目。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.server import _summarize                          # noqa: E402


def _engine_entry(i: int) -> dict:
    return {"location": {"chapter": "c", "line": i, "start": 0, "end": 10},
            "original": "原文%d" % i, "replacement": "", "deleted": "霖",
            "result_text": "改后%d" % i, "method": "rule_engine",
            "engine": "v3", "severity": "HIGH", "confidence": 0}


def _llm_entry(i: int) -> dict:
    return {"location": {"chapter": "c", "line": i, "start": 0, "end": 10},
            "original": "原文%d" % i, "before_llm": "引擎结果%d" % i,
            "result_text": "LLM 改后%d" % i,
            "replacement": "（", "deleted": "揪",
            "method": "llm", "engine": "llm_review", "resolver": "test",
            "model": "m1", "decision": "repair", "selected_candidate": "llm_ops",
            "confidence": 0.9, "reason": "删掉注入字符"}


def _fake_result(n_engine=60, n_llm=40):
    report = {
        "chars_total": 1000, "lines_total": 100,
        "audit": {"events_initial": n_engine + n_llm, "events_final": 0,
                  "by_severity_initial": {"HIGH": 1},
                  "by_evidence_type": {"statistical_anomaly": 1}},
        "auto_repair": {"lines": n_engine, "chars": n_engine},
        "llm_review": {"items": n_llm, "sent": n_llm, "lines": n_llm,
                       "chars": n_llm, "added": 0, "kept": 0, "uncertain": 0,
                       "stats": {"total": n_llm, "ok": n_llm, "fail": 0,
                                 "skipped": 0}},
        "unresolved_events": 0, "metrics": {}, "final_audit": {},
    }
    # LLM 条目全部排在引擎条目之后
    log = [_engine_entry(i) for i in range(n_engine)]
    log += [_llm_entry(i) for i in range(n_llm)]
    return SimpleNamespace(report=report, change_log=log, review_queue=[],
                           seconds=1.0)


def test_llm_sample_reaches_entries_after_the_first_40():
    s = _summarize(_fake_result(), "x.txt", {})
    assert s["llm_sample"], ("LLM 改动抽样为空 —— 界面又会看不到 LLM 改了什么",
                             len(s["llm_sample"]))
    e = s["llm_sample"][0]
    for k in ("line", "before", "after", "deleted", "added", "decision",
              "confidence", "reason"):
        assert k in e, "抽样缺字段: %s" % k
    assert e["before"] == "引擎结果0" and e["after"] == "LLM 改后0"
    assert e["reason"] == "删掉注入字符"
    # 引擎抽样里不该混进 LLM 条目
    assert all(x["method"] != "llm" for x in s["changelog_sample"])


def test_llm_sample_empty_when_no_llm_changes():
    s = _summarize(_fake_result(n_engine=50, n_llm=0), "x.txt", {})
    assert s["llm_sample"] == []
    assert len(s["changelog_sample"]) == 40


def _qitem(i, decision, text="句子"):
    """造一个 ReviewItem 形状的假对象。"""
    return SimpleNamespace(
        item_id="b:L%d" % (i + 1), line_start=i + 1, line_end=i + 1,
        severity="HIGH", priority=1, reason_code="v3_left",
        suspicious_text=text, evidence=[], candidate_repairs=[],
        decision=decision, selected_candidate="", operations=[],
        confidence=0.8, reason="不是广告", v3_result={})


def test_llm_kept_sample_covers_reviewed_but_unchanged():
    """LLM 看过但决定不动的行也要能抽样。"""
    r = _fake_result(n_engine=5, n_llm=1)
    r.review_queue = [_qitem(i, "keep" if i % 2 else "uncertain")
                      for i in range(12)]
    s = _summarize(r, "x.txt", {})
    assert len(s["llm_kept_sample"]) == 12
    e = s["llm_kept_sample"][0]
    for k in ("line", "text", "decision", "confidence", "reason"):
        assert k in e, "抽样缺字段: %s" % k
    assert e["decision"] in ("keep", "uncertain")


def test_llm_kept_sample_skips_repair_items():
    """已经被改过的行不该出现在"判定保留"里。"""
    r = _fake_result(n_engine=1, n_llm=1)
    r.review_queue = [_qitem(0, "repair", "改过的"),
                      _qitem(1, "keep", "保留的")]
    s = _summarize(r, "x.txt", {})
    assert [x["line"] for x in s["llm_kept_sample"]] == [2]
