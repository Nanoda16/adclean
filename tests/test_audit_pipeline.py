# -*- coding: utf-8 -*-
"""Audit / Review / Pipeline 三层单元测试：不依赖外部语料缓存，现场训练小语料。"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.audit import (SEV_HIGH, SEV_LOW, SEV_MEDIUM, AuditConfig,
                           AuditLayer, EV_INVISIBLE, EV_STATISTICAL,
                           EV_STRUCTURE)
from adclean.pipeline import PipelineConfig, line_candidates, run_book
from adclean.review import (CandidateRepair, MappingResolver, NullResolver,
                            ReviewItem, apply_decision, build_prompt,
                            parse_decision, render_region)
from adclean.v2lm import LanguageModel
from adclean.v3engine import V3Config, V3Engine

CLEAN = [
    "常榕叹了口气坐到沙发旁边的书桌前，同时取出放在抽屉里的代办清单列表。",
    "这个房间里几天前发生过一场命案，而就在命案的前一天，自己在这里和被害人互相闲聊过。",
    "她冲到常榕身前，张开双手试图护住常榕。",
    "樱庭娜娜看到常榕之后先是怔了一下，接着立刻尴尬的想要转身离开。",
    "常榕冷静就算再怎么迟钝也能意识到了，眼前这个伪人感十足的怪物。",
    "不光是大小姐，常榕光是在旁边听二人的对话都快要听力竭了。",
    "但是一群人不知道为什么全都异口同声，而亚里沙自己也是跟喝多了似的。",
]
POLLUTED = "常榕又思索片刻后一，将（裠鸠  霖瘤是 六霓吧2紦【更换七门锁）】仪直接划八掉。"


@pytest.fixture(scope="module")
def engine():
    lm = LanguageModel().fit(CLEAN * 40)
    eng = V3Engine(lm, V3Config())
    eng.cfg.whitelist_words = ("常榕", "樱庭娜娜", "亚里沙")
    return eng


def test_build_engine_default_is_locator():
    """生产默认引擎必须是多特征定位器。"""
    from adclean.locate import LocatorEngine
    from adclean.pipeline import build_engine
    eng = build_engine()
    assert isinstance(eng, LocatorEngine)
    # 只删不改：引擎不提供任何"改写正文"的能力
    assert not hasattr(eng, "set_cost")
    for banned in ("common_char_freq", "anchor_min", "min_freq", "max_freq",
                   "homophone_max_freq"):
        assert not hasattr(eng.cfg, banned), banned


def test_build_engine_semantic_still_available():
    """V4 仍可显式选用（--engine semantic），配置里同样不含字频字段。"""
    from adclean.pipeline import build_engine
    from adclean.semantic import SemanticConfig
    eng = build_engine(kind="semantic")
    assert isinstance(eng.cfg, SemanticConfig)
    assert not hasattr(eng.cfg, "max_anchors")        # 旧的字频锚点门禁
    for banned in ("common_char_freq", "anchor_min", "min_freq", "max_freq",
                   "homophone_max_freq"):
        assert not hasattr(eng.cfg, banned), banned


def test_build_engine_v3_uses_frozen_config():
    """显式指定 --engine v3 时必须用冻结配置，而不是 V3Config 的默认值。"""
    from adclean.pipeline import FROZEN_V3, build_engine
    from adclean.v3engine import V3Config
    eng = build_engine(kind="v3")
    for k, v in FROZEN_V3.items():
        assert getattr(eng.cfg, k) == v, (k, getattr(eng.cfg, k), v)
    # 与默认值必须不同，否则这个测试会失效
    d = V3Config()
    assert d.candidate_mode != FROZEN_V3["candidate_mode"]
    assert d.score_mode != FROZEN_V3["score_mode"]
    # coherence_beta 是冻结的生产值；B'（anchor）必须关闭
    assert eng.cfg.coherence_beta == 2.0
    assert eng.cfg.anchor_beta == 0.0
    assert FROZEN_V3["coherence_beta"] == eng.cfg.coherence_beta
    assert V3Config().coherence_beta == 0.0


# --------------------------------------------------------------- Audit
def test_audit_finds_evidence(engine):
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    ev = layer.line_evidence(POLLUTED)
    assert ev, "被污染行必须产生证据"
    types = {e.type for e in ev}
    assert EV_STATISTICAL in types
    # `（……】` 括号类型不匹配必须被结构检测抓到
    assert EV_STRUCTURE in types
    # 不截断
    assert len(ev) >= 3


def test_audit_clean_line_low_evidence(engine):
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    ev = layer.line_evidence(CLEAN[0])
    assert len(ev) <= 2, "正常行不应产生大量证据: %r" % ev


def test_audit_scan_book_severity(engine):
    text = "\n".join([CLEAN[0], POLLUTED, CLEAN[1]])
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    events = layer.scan_book(text, "book")
    assert events
    pol = [e for e in events if "裠" in e.text]
    assert pol, "污染行必须被标出"
    assert pol[0].severity in (SEV_HIGH, SEV_MEDIUM)
    assert pol[0].n_evidence >= 2
    assert pol[0].start < pol[0].end
    assert pol[0].line_start == 2


def test_audit_invisible_char(engine):
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    ev = layer.line_evidence("正常文字\u200b正常文字正常文字")
    assert any(e.type == EV_INVISIBLE for e in ev)
    st = layer.scan_book("正常文字\u200b正常文字正常文字", "b")
    assert st and st[0].severity == SEV_HIGH      # 不可见字符直接判 HIGH


def test_audit_stats(engine):
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    events = layer.scan_book("\n".join(CLEAN + [POLLUTED]), "b")
    st = layer.stats(events)
    assert st["events"] == len(events)
    assert sum(st["by_severity"].values()) == len(events)


# --------------------------------------------------- 覆盖率缺口（非 BMP CJK）
def test_coverage_gap_detection():
    """`charclass.is_cjk` 不覆盖非 BMP 的 CJK 扩展 B–H，必须由 coverage_gap 补上。"""
    from adclean.audit import is_coverage_gap
    assert is_coverage_gap("\U0003087B")          # 扩展 G
    assert is_coverage_gap("\U00030C21")          # 扩展 G
    assert is_coverage_gap("\U00020000")          # 扩展 B
    assert not is_coverage_gap("夯")               # 基本区，由 is_cjk 覆盖
    assert not is_coverage_gap("焖")
    assert not is_coverage_gap("あ")               # 假名
    assert not is_coverage_gap("한")               # 谚文
    assert not is_coverage_gap("a")
    assert not is_coverage_gap("，")
    assert not is_coverage_gap("\u200b")          # 不可见字符走另一条通道


def test_coverage_gap_raises_severity(engine):
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    ev = layer.line_evidence("他抬头看向那个\U0003087B字，心里发慌。")
    assert any(e.type == "coverage_gap" for e in ev), ev
    evs = layer.scan_book("他抬头看向那个\U0003087B字，心里发慌。", "b")
    assert evs and evs[0].has_coverage_gap
    assert evs[0].severity == SEV_HIGH


def test_engine_proposal_excluded_from_independent(engine):
    """engine_proposal 是引擎自己的意见，不能算作独立印证。"""
    from adclean.audit import EV_ENGINE_PROPOSAL
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    events = layer.scan_book("\n".join(CLEAN + [POLLUTED]), "b")
    with_prop = [e for e in events
                 if any(x.type == EV_ENGINE_PROPOSAL for x in e.evidence)]
    assert with_prop, "引擎会修的行必须带 engine_proposal 证据"
    for e in with_prop:
        n_prop = len({(x.type, x.pos) for x in e.evidence
                      if x.type == EV_ENGINE_PROPOSAL})
        assert e.n_evidence >= e.n_independent
        assert e.n_independent == e.n_evidence - n_prop or n_prop == 0


def test_proposal_conf_never_reaches_high_conf(engine):
    """proposal 自身的置信度上限必须低于 high_conf，否则单靠它就能升到 HIGH。"""
    cfg = AuditConfig()
    assert cfg.proposal_conf_cap < cfg.high_conf
    layer = AuditLayer(engine, cfg)
    events = layer.scan_book("\n".join(CLEAN + [POLLUTED]), "b")
    for e in events:
        for x in e.evidence:
            if x.type == "engine_proposal":
                assert x.score < cfg.high_conf


def test_strong_positions_recorded(engine):
    """强证据位置必须被记录下来。"""
    from adclean.audit import is_coverage_gap
    line = "他抬头看向那个\U0003087B字，心里发慌。"
    layer = AuditLayer(engine, AuditConfig(merge_lines=False))
    layer.scan_book(line, "b")
    want = {i for i, c in enumerate(line) if is_coverage_gap(c)}
    assert want, "测试串里必须有 coverage_gap 字符"
    assert layer.strong_positions.get(0) == want, \
        (layer.strong_positions, want)
    # 不含 coverage_gap 的普通行不应产生强证据位置
    layer2 = AuditLayer(engine, AuditConfig(merge_lines=False))
    layer2.scan_book(CLEAN[0], "b")
    assert 0 not in layer2.strong_positions


def test_pipeline_has_no_v3_gate_config():
    """任何"门禁"配置都不允许存在：Audit 只能旁路，不能决定 V3 是否运行。"""
    from dataclasses import fields
    names = {f.name for f in fields(PipelineConfig)}
    for banned in ("auto_margin", "auto_need_high", "auto_min_evidence",
                   "auto_min_density", "strong_evidence_only", "engine_only",
                   "candidate_min_margin"):
        assert banned not in names, "禁止的门禁配置又出现了：%s" % banned


def test_audit_cannot_block_v3(engine, monkeypatch):
    """反门禁：就算 Audit 一个事件都找不到，V3 也必须照常处理。"""
    from adclean import audit as audit_mod
    text = "\n".join(CLEAN + [POLLUTED])
    monkeypatch.setattr(audit_mod.AuditLayer, "scan_book",
                        lambda self, *a, **k: [])
    cfg = PipelineConfig()
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert res.audit_events == []
    assert res.review_queue == []
    # V3 仍然必须完成自己的修复
    assert res.report["auto_repair"]["chars"] > 0
    assert "裠" not in res.cleaned


def test_v3_only_equals_pipeline_default(engine):
    """默认 resolver 下，pipeline 输出必须与直接用 V3 清洗逐字节相同。"""
    text = "\n".join(CLEAN + [POLLUTED] + CLEAN)
    direct = "\n".join(engine.clean_line(x).cleaned if x else x
                       for x in text.split("\n"))
    cfg = PipelineConfig()
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert res.cleaned == direct


def test_queue_has_no_duplicate_lines(engine):
    """同一行可能被多个事件覆盖：队列里绝不能重复建 item。"""
    # 构造相邻污染行，使事件合并且互相重叠
    text = "\n".join(CLEAN[:2] + [POLLUTED, POLLUTED] + CLEAN[2:])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=True, merge_gap=8)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    ids = [it.item_id for it in res.review_queue]
    lines = [it.line_start for it in res.review_queue]
    assert len(ids) == len(set(ids)), "队列里有重复 item_id: %r" % ids
    assert len(lines) == len(set(lines)), "同一行被建了多个 item: %r" % lines


def test_queue_has_no_duplicate_lines_merged_off(engine):
    text = "\n".join(CLEAN[:2] + [POLLUTED, POLLUTED] + CLEAN[2:])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    lines = [it.line_start for it in res.review_queue]
    assert len(lines) == len(set(lines)), lines


def test_llm_replacement_is_not_dropped(engine, monkeypatch):
    """LLM 的 final_text 若含"替换"，必须落到文本，不能被静默丢弃。"""
    from adclean import review as _rv
    monkeypatch.setattr(_rv, "DELETE_ONLY", False)
    text = "\n".join(CLEAN + ["　　灰\U00030C21\U00030C21的天空下，是一座繁荣的城镇。"])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    r0 = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert r0.review_queue
    target = None
    for it in r0.review_queue:
        if "\U00030C21" in it.suspicious_text:
            target = it
            break
    assert target is not None, "含乱码字的行必须进队列"
    fixed = target.suspicious_text.replace("\U00030C21", "蒙")
    decisions = {target.item_id: {"decision": "repair", "confidence": 0.95,
                                  "reason": "真字被替换", "final_text": fixed}}
    r1 = run_book(text, engine, cfg, book_id="b",
                  resolver=MappingResolver(decisions, "test"))
    assert "灰蒙蒙的天空" in r1.cleaned, r1.cleaned
    assert "\U00030C21" not in r1.cleaned
    e = [x for x in r1.change_log
         if x["location"]["line"] == len(CLEAN) + 1 and x["method"] == "llm"]
    assert e and e[0]["replacement"] == "蒙蒙", e


def test_llm_keep_does_not_change(engine):
    """keep 决策 = 认可引擎的清洗结果，必须保持 pipeline 默认输出。"""
    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    r0 = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert r0.review_queue
    d = {it.item_id: {"decision": "keep", "confidence": 0.9}
         for it in r0.review_queue}
    r1 = run_book(text, engine, cfg, book_id="b",
                  resolver=MappingResolver(d, "test"))
    assert r1.cleaned == r0.cleaned


def test_audit_never_gates_engine_even_with_impossible_thresholds(engine):
    """把 Audit 的判据调到极端，V3 的修复能力也不能受任何影响。"""
    text = "\n".join(CLEAN + [POLLUTED])
    base_cfg = PipelineConfig()
    base_cfg.audit = AuditConfig(merge_lines=False)
    base = run_book(text, engine, base_cfg, book_id="b", resolver=NullResolver())
    for aud in (AuditConfig(merge_lines=False, min_conf=1e9),
                AuditConfig(merge_lines=False, min_conf=-1e9,
                            high_conf=-1e9, med_conf=-1e9)):
        cfg = PipelineConfig()
        cfg.audit = aud
        r = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
        assert r.cleaned == base.cleaned, "Audit 配置改变了 V3 的结果，说明它变成了门禁"
        assert r.report["auto_repair"] == base.report["auto_repair"]


# --------------------------------------------------------------- Review
def _item():
    # 索引: 她0 冲1 到2 常3 榕4 霖5 身6 前7 。8
    return ReviewItem(
        item_id="b:L1", book_id="b", chapter_id="c",
        line_start=1, line_end=1, start=0, end=9,
        severity=SEV_HIGH, context_before="", suspicious_text="她冲到常榕霖身前。",
        context_after="",
        evidence=[{"type": "statistical_anomaly", "score": 3.0}],
        candidate_repairs=[
            CandidateRepair("candidate_1", [[5, 6]], -10.0, "霖"),
            CandidateRepair("candidate_2", [], -30.0, ""),
        ])


def test_parse_decision_robustness():
    assert parse_decision(None)["decision"] == "uncertain"
    assert parse_decision("")["decision"] == "uncertain"
    assert parse_decision("not json")["decision"] == "uncertain"
    assert parse_decision('{"decision":"bogus"}')["decision"] == "uncertain"
    d = parse_decision('{"decision":"repair"}')
    assert d["decision"] == "repair" and d["confidence"] == 0.0
    d = parse_decision('```json\n{"decision":"keep","confidence":0.9}\n```')
    assert d["decision"] == "keep" and abs(d["confidence"] - 0.9) < 1e-9
    d = parse_decision({"decision": "repair", "selected_candidate": "candidate_1",
                        "confidence": 1.7})
    assert d["confidence"] == 1.0            # 被夹到 [0,1]
    assert d["selected_candidate"] == "candidate_1"


def test_render_and_apply():
    it = _item()
    assert render_region(it.suspicious_text, it.start, it.candidate_repairs[0]) \
        == "她冲到常榕身前。"
    txt, cid, _ = apply_decision(it, {"decision": "repair",
                                      "selected_candidate": "candidate_1"})
    assert txt == "她冲到常榕身前。" and cid == "candidate_1"
    txt, _cid, note = apply_decision(it, {"decision": "repair",
                                          "selected_candidate": "nope"})
    assert txt is None and "无效" in note
    txt, _cid, note = apply_decision(it, {"decision": "keep"})
    assert txt == it.suspicious_text and note == "keep"
    txt, _cid, note = apply_decision(it, {"decision": "uncertain"})
    assert txt is None


def test_prompt_contains_candidates():
    p = build_prompt(_item())
    assert "candidate_1" in p and "candidate_2" in p
    assert "她冲到常榕霖身前。" in p
    assert "JSON" in p


def test_mapping_resolver(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"b:L1": {"decision": "repair",
                                      "selected_candidate": "candidate_1",
                                      "confidence": 0.8}}), encoding="utf-8")
    r = MappingResolver.from_json(str(p))
    d = r.resolve(_item())
    assert d["decision"] == "repair"
    assert r.resolve(ReviewItem("x", "b", "c", 1, 1, 0, 1, SEV_LOW, "", "", ""))[
        "decision"] == "uncertain"


def test_null_resolver():
    assert NullResolver().resolve(_item())["decision"] == "uncertain"


# --------------------------------------------------------------- line_candidates
def test_line_candidates_shape(engine):
    cands = line_candidates(engine, POLLUTED, k=4)
    assert len(cands) == 3                       # (list, engine_rm, margin)
    lst, engine_rm, margin = cands
    assert isinstance(engine_rm, frozenset)
    assert isinstance(margin, float)
    assert lst, "候选列表不能为空"
    assert lst[-1][1] == frozenset(), "最后一项必须是『不修改』"
    assert len(lst) <= 4
    # 引擎决策必须出现在候选池里
    assert any(rm == engine_rm for _s, rm in lst)
    # 分数单调不增
    scores = [s for s, _rm in lst[:-1]]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------- Pipeline
def test_pipeline_end_to_end(engine, tmp_path):
    text = "\n".join(CLEAN + [POLLUTED] + CLEAN)
    cfg = PipelineConfig()
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert res.cleaned
    assert res.cleaned.count("\n") == text.count("\n"), "行数不能变"
    rp = res.report
    assert rp["audit"]["events_initial"] >= 1
    assert rp["auto_repair"]["chars"] + rp["llm_review"]["items"] > 0, \
        "污染行必须被自动修复或进入队列"
    assert rp["auto_repair"]["chars"] > 0, "小语料下污染行应能被自动修复"
    assert "裠" not in res.cleaned, "自动修复应删掉广告字"
    assert "常榕" in res.cleaned, "白名单人名必须保留"
    # changelog 每条都有可审计字段
    for e in res.change_log:
        assert e["method"] in ("rule_engine", "llm")
        assert "location" in e and "original" in e
        assert e["location"]["line"] >= 1
    # 三件套落盘
    paths = res.write_outputs(str(tmp_path), "book.txt")
    for k in ("cleaned", "report", "queue", "log"):
        assert os.path.exists(paths[k]), k
    q = json.load(open(paths["queue"], encoding="utf-8"))
    assert isinstance(q, list)
    rep = json.load(open(paths["report"], encoding="utf-8"))
    assert rep["chars_total"] == len(text)
    lg = json.load(open(paths["log"], encoding="utf-8"))
    assert isinstance(lg, list) and len(lg) == len(res.change_log)


def test_pipeline_queues_regions_v3_did_not_fix(engine):
    """V3 没能处理的异常区域必须进 LLM 队列。"""
    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert res.review_queue, "可疑区域必须进队列"
    it = res.review_queue[0]
    assert it.candidate_repairs
    assert it.v3_result and "original_text" in it.v3_result
    assert it.suspicious_text
    assert it.evidence, "队列项必须带异常证据"
    # 队列项可序列化为结构化 JSON（供外部 LLM）
    d = it.as_dict()
    assert d["candidate_repairs"][-1]["id"] == it.candidate_repairs[-1].id
    assert "context_before" in d and "context_after" in d
    json.dumps(d, ensure_ascii=False)          # 必须可 JSON 化


def test_pipeline_resolver_applies_repair(engine):
    """LLM 不必从 V3 候选里选，可以直接给出自己的 operations。"""
    text = "\n".join(CLEAN + [POLLUTED])
    cfg = PipelineConfig(llm_max_priority=3)
    cfg.audit = AuditConfig(merge_lines=False)
    r0 = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert r0.review_queue, "应有队列项"
    target = None
    for it in r0.review_queue:
        if "裠" in it.suspicious_text:
            target = it
            break
    assert target is not None, "污染行必须进队列"
    iid = target.item_id
    lo = target.suspicious_text.index("裠")
    decisions = {iid: {
        "decision": "repair", "confidence": 0.9, "reason": "test",
        "operations": [{"type": "delete", "start": target.start + lo,
                        "end": target.start + lo + 1}]}}
    r1 = run_book(text, engine, cfg, book_id="b",
                  resolver=MappingResolver(decisions, "test"))
    assert r1.report["llm_review"]["resolver"] == "test"
    assert r1.cleaned != r0.cleaned, "operations 必须落到文本"
    llm_logs = [e for e in r1.change_log if e["method"] == "llm"]
    assert llm_logs and llm_logs[0]["operations"], llm_logs[:1]
    assert llm_logs[0]["prompt_version"] == "review-v2"
    assert llm_logs[0]["decision"] == "repair"
    # keep 决策 = 认可系统默认（V3 结果），不得改变输出
    decisions2 = {iid: {"decision": "keep", "confidence": 0.9, "reason": "t"}}
    r2 = run_book(text, engine, cfg, book_id="b",
                  resolver=MappingResolver(decisions2, "test"))
    assert r2.cleaned == r0.cleaned


def test_llm_operations_outside_region_are_rejected(engine):
    """operations 越出 Review Region 必须整条拒绝。"""
    from adclean.review import CandidateRepair, ReviewItem, apply_decision
    it = ReviewItem(item_id="x:L1", book_id="b", chapter_id="c",
                    line_start=1, line_end=1, start=100, end=105,
                    severity=SEV_MEDIUM, context_before="", context_after="",
                    suspicious_text="她冲到常榕。", default_text="她冲到常榕。")
    txt, _cid, note = apply_decision(it, {
        "decision": "repair",
        "operations": [{"type": "delete", "start": 50, "end": 55}]})
    assert txt is None and "越出" in note, (txt, note)


def test_llm_operations_are_applied_locally(engine, monkeypatch):
    from adclean import review as _rv
    monkeypatch.setattr(_rv, "DELETE_ONLY", False)
    from adclean.review import ReviewItem, apply_decision
    it = ReviewItem(item_id="x:L1", book_id="b", chapter_id="c",
                    line_start=1, line_end=1, start=100, end=107,
                    severity=SEV_MEDIUM, context_before="", context_after="",
                    suspicious_text="她冲到常榕霖身前。", default_text="她冲到常榕身前。")
    txt, cid, _n = apply_decision(it, {
        "decision": "repair",
        "operations": [{"type": "delete", "start": 105, "end": 106}]})
    assert txt == "她冲到常榕身前。" and cid == "llm_ops", (txt, cid)
    # replace 型
    it2 = ReviewItem(item_id="x:L2", book_id="b", chapter_id="c",
                     line_start=1, line_end=1, start=0, end=8,
                     severity=SEV_MEDIUM, context_before="", context_after="",
                     suspicious_text="灰\U00030C21\U00030C21的天空",
                     default_text="灰的天空")
    txt2, cid2, _n2 = apply_decision(it2, {
        "decision": "repair",
        "operations": [{"type": "replace", "start": 1, "end": 3, "text": "蒙蒙"}]})
    assert txt2 == "灰蒙蒙的天空" and cid2 == "llm_ops", (txt2, cid2)


def test_pipeline_low_severity_untouched(engine):
    text = "\n".join(CLEAN)
    cfg = PipelineConfig()
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, engine, cfg, book_id="b", resolver=NullResolver())
    assert res.cleaned == text, "全正常书不应被改动"
    assert res.report["auto_repair"]["chars"] == 0


def test_delete_only_blocks_replace_and_insert(engine, monkeypatch):
    """只删不改：replace / insert / final_text 一律拒绝。"""
    from adclean import review as _rv
    monkeypatch.setattr(_rv, "DELETE_ONLY", True)
    from adclean.review import ReviewItem, apply_decision
    it = ReviewItem(item_id="x:L9", book_id="b", chapter_id="c",
                    line_start=1, line_end=1, start=0, end=8,
                    severity=SEV_MEDIUM, context_before="", context_after="",
                    suspicious_text="灰\U00030C21\U00030C21的天空",
                    default_text="灰的天空")
    txt, cid, _n = apply_decision(it, {
        "decision": "repair",
        "operations": [{"type": "replace", "start": 1, "end": 3, "text": "蒙蒙"}]})
    assert txt is None, ("只删不改：替换必须被拒绝", txt)
    txt2, _c2, _n2 = apply_decision(it, {
        "decision": "repair", "final_text": "灰蒙蒙的天空"})
    assert txt2 is None, ("只删不改：final_text 必须被拒绝", txt2)
    txt3, cid3, _n3 = apply_decision(it, {
        "decision": "repair",
        "operations": [{"type": "delete", "start": 1, "end": 3}]})
    assert txt3 == "灰的天空" and cid3 == "llm_ops", (txt3, cid3)
