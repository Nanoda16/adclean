# -*- coding: utf-8 -*-
"""adclean.pipeline —— 主链路：V3 Auto Repair → Audit → Review Queue → LLM Review → Final Audit。

约束：Audit 是只读旁路，永远不决定 V3 是否运行、也不决定 V3 能改哪些行。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

from .audit import (SEV_HIGH, SEV_LOW, SEV_MEDIUM, AuditConfig, AuditLayer,
                    strong_positions_of)
from .processor import list_txt_files, newline_of, read_text
from .review import (DECISION_KEEP, DECISION_REPAIR, DECISION_UNCERTAIN,
                     CandidateRepair, NullResolver, ReviewItem, apply_decision,
                     build_prompt, parse_decision)
from .v3engine import V3Engine, default_lm_path

# 语料模型候选（按优先级）。
LM_CANDIDATES = (".adclean-lm-a5.json", ".adclean-lm-a0c.json",
                 ".adclean-lm-a4.json", ".adclean-lm-c1.json",
                 ".adclean-lm.json")

# 已冻结的 Auto Repair 引擎配置：**不要**用 V3Config() 的默认值跑生产。
FROZEN_V3 = dict(
    score_mode="lr",
    insert_prior=0.005,
    max_rounds=3,
    use_structure=True,
    candidate_mode="beam_v2",
    beam_size=20,
    local_top_k=5,
    local_inner_k=12,
    local_union_max=4,
    local_composite=10,
    merge_max=1,          # merge 关闭
    merge_gap=-1,
    # B 机制：span coherence 奖励
    coherence_beta=2.0,
    # anchor_beta 保持 0（不启用）
    anchor_beta=0.0,
)

DEFAULT_NAMES = ("常榕", "艾玛", "黑部奈叶香", "樱羽艾玛")


def build_engine(lm_path: str = "", names=DEFAULT_NAMES,
                 overrides: dict | None = None, kind: str = "locate"):
    """构造 Auto Repair 引擎。

    kind="locate"（默认）：多特征字级定位器；kind="semantic"：句子级语义判据；
    kind="v3"：旧字频引擎。三者接口一致（clean_line / clean_book / _line_score）。
    """
    from .v2lm import LanguageModel
    path = resolve_lm_path(lm_path)
    lm = LanguageModel.load(path)
    if kind == "locate":
        from .locate import LocatorEngine
        eng = LocatorEngine(lm, lm_path=path, overrides=overrides)
        return eng
    if kind == "v3":
        eng = V3Engine(lm)
        for k, v in FROZEN_V3.items():
            setattr(eng.cfg, k, v)
        if names:
            eng.cfg.whitelist_words = tuple(names)
    else:
        from .semantic import SemanticEngine
        eng = SemanticEngine(lm)
        if names:
            eng.cfg.names = tuple(names)
    for k, v in (overrides or {}).items():
        setattr(eng.cfg, k, v)
    if kind != "v3" and overrides and "names" in overrides:
        eng.cfg.names = tuple(overrides["names"])
    eng._lm_path = path
    eng._engine_kind = kind
    return eng


def resolve_lm_path(explicit: str = "") -> str:
    """返回语料模型路径；explicit 为空时按 源码目录 → exe 目录 → 当前工作目录 查找。"""
    if explicit:
        return explicit
    roots = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.append(os.getcwd())
    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for name in LM_CANDIDATES:
            p = os.path.join(root, name)
            if os.path.exists(p):
                return p
    return default_lm_path()


# ---------------------------------------------------------------------------
@dataclass
class PipelineConfig:
    audit: AuditConfig = field(default_factory=AuditConfig)
    # --- Review Queue 上下文 ---
    context_chars: int = 60        # 前后上下文窗口（字符）
    # --- 送审范围（§九）---
    # 只有 priority <= llm_max_priority 的 item 才真的调用 LLM。
    #   1 = 引擎动过且删掉了常用字；2 = 引擎动过但独立证据不足；
    #   3 = 引擎动过且证据充分（只记录）。
    llm_max_priority: int = 2
    # “引擎删了常用字”的判据：被删字符在这个语料里的词频高于此值
    common_char_freq: int = 2000
    # 引擎动过但外部独立证据少于这个数 → 弱支持
    weak_support_evidence: int = 2
    max_llm_items: int = 0         # 0 = 不限
    # --- §十二 Final Audit ---
    reaudit: bool = True
    final_review_rounds: int = 1   # Final Audit 之后**最多**再送审的轮数（有界）
    # --- 给 UI / 外部调用者的钩子 ---
    audit_progress: object = None  # callable(done_lines, total_lines)
    llm_progress: object = None    # callable(done, total, ok, fail)
    cancel: object = None          # callable() -> bool，返回 True 则停止送审
    # --- 其它 ---
    # 繁→简转换：默认开启（繁体字会被换成简体字，属于字形改写）。
    # 关闭它则保留原字形，但简繁混排的书容易把繁体正文当插入字误删。
    simplify: bool = True


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _offsets(lines: list) -> list:
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln) + 1
    return offs


def _remove(line: str, rm) -> str:
    if not rm:
        return line
    return "".join(c for j, c in enumerate(line) if j not in rm)


def _ranges(pos_sorted) -> list:
    """把位置集合转成 [start,end) 区间列表。"""
    out = []
    for p in pos_sorted:
        if out and out[-1][1] == p:
            out[-1][1] = p + 1
        else:
            out.append([p, p + 1])
    return out


def _diff_removed(orig: str, new: str) -> set:
    """new 相对 orig 的纯删除位置（若含插入/替换则返回空集）。"""
    import difflib
    sm = difflib.SequenceMatcher(None, orig, new, autojunk=False)
    rm = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            rm.update(range(i1, i2))
        elif tag in ("replace", "insert"):
            return set()
    return rm


def _diff_ops(orig: str, new: str) -> tuple:
    """返回 (保持, 新增文本, 被删文本)。"""
    import difflib
    sm = difflib.SequenceMatcher(None, orig, new, autojunk=False)
    same, ins, dele = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            same.append(orig[i1:i2])
        elif tag == "insert":
            ins.append(new[j1:j2])
        elif tag == "delete":
            dele.append(orig[i1:i2])
        else:
            ins.append(new[j1:j2])
            dele.append(orig[i1:i2])
    return "".join(same), "".join(ins), "".join(dele)


def _only_removed(a: str, b: str) -> str:
    """a→b 是纯删除时返回被删文本，否则空串。"""
    rm = _diff_removed(a, b)
    if not rm:
        return ""
    return "".join(a[j] for j in sorted(rm))


# ---------------------------------------------------------------------------
# 一、V3 主引擎（无门禁）
# ---------------------------------------------------------------------------
def v3_auto_repair(engine, lines: list, progress=None) -> dict:
    """主引擎跑完整本书，逐行决策原样生效，不做任何门禁（§三）。

    返回 {行号: (frozenset(行内删除位置), margin)}，可直接喂给
    AuditLayer.scan_book(proposals=...)。
    """
    # 需要全书统计的引擎走整本接口
    if hasattr(engine, "clean_book"):
        return engine.clean_book(lines, progress=progress)
    out: dict = {}
    n = len(lines)
    step = max(1, n // 100)
    for i, line in enumerate(lines):
        if line:
            r = engine.clean_line(line)
            if r.removed:
                chs = list(line)
                sc = engine._line_score(chs, tuple(sorted(r.removed)))
                mg = sc - engine._line_score(chs, ())
                out[i] = (frozenset(r.removed), float(mg))
        if progress and (i % step == 0 or i == n - 1):
            try:
                progress(i + 1, n)
            except Exception:                                # noqa: BLE001
                pass
    return out


def line_candidates(engine: V3Engine, line: str, k: int = 6,
                    min_margin: float | None = None):
    """一行的候选修复方案（仅供人工/调试参考，不在主链路上）。

    返回 (candidates, engine_removed, engine_margin)。
    """
    chars = list(line)
    trace: list = []
    r = engine.clean_line(line, trace=trace)
    seen = {frozenset()}
    for g in trace:
        pool = g.get("pool")
        if pool:
            for _sc, rm in pool:
                seen.add(frozenset(rm))
    for _k in r.seen:
        seen.add(frozenset(_k))
    engine_rm = frozenset(r.removed)
    seen.add(engine_rm)
    empty_sc = engine._line_score(chars, ())
    scored = []
    for s in seen:
        if not s:
            continue
        sc = engine._line_score(chars, tuple(sorted(s)))
        if min_margin is not None and sc - empty_sc <= min_margin:
            continue
        scored.append((sc, s))
    scored.sort(key=lambda x: -x[0])
    rm_sorted = tuple(sorted(engine_rm))
    engine_margin = (engine._line_score(chars, rm_sorted) if engine_rm else empty_sc) \
        - empty_sc

    picked: list = []
    picked_sets: list = []

    def _jac(a, b):
        if not a and not b:
            return 1.0
        return len(a & b) / max(1, len(a | b))

    def _push(sc, s):
        if s in picked_sets:
            return
        picked.append((sc, s))
        picked_sets.append(s)

    if engine_rm and (min_margin is None or engine_margin > min_margin):
        _push(engine._line_score(chars, rm_sorted), engine_rm)
    if scored:
        _push(scored[0][0], scored[0][1])
    for sc, s in scored:
        if len(picked) >= max(1, k - 1):
            break
        if any(_jac(s, t) >= 0.7 for t in picked_sets):
            continue
        _push(sc, s)
    for sc, s in scored:
        if len(picked) >= max(1, k - 1):
            break
        _push(sc, s)
    picked.sort(key=lambda x: -x[0])
    out = picked[: max(1, k - 1)]
    out.append((empty_sc, frozenset()))
    return out, engine_rm, engine_margin


# ---------------------------------------------------------------------------
@dataclass
class PipelineResult:
    book_id: str
    cleaned: str
    audit_events: list = field(default_factory=list)
    final_events: list = field(default_factory=list)
    review_queue: list = field(default_factory=list)
    change_log: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    seconds: float = 0.0
    llm_stats: dict = field(default_factory=dict)
    # 只跑 V3（未应用 LLM 决策）的文本，用于对比与回滚（§十一）。
    cleaned_v3: str = ""

    def write_outputs(self, out_dir: str, basename: str,
                      newline: str = "\n") -> dict:
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(basename)[0]
        paths = {
            "cleaned": os.path.join(out_dir, basename),
            "report": os.path.join(out_dir, stem + ".audit.json"),
            "queue": os.path.join(out_dir, stem + ".review_queue.json"),
            "log": os.path.join(out_dir, stem + ".changelog.json"),
            "events": os.path.join(out_dir, stem + ".events.json"),
            # 同一批文件的别名，便于外部对接
            "cleaned_book": os.path.join(out_dir, basename),
            "audit_report": os.path.join(out_dir, stem + ".audit.json"),
            "review_queue": os.path.join(out_dir, stem + ".review_queue.json"),
            "change_log": os.path.join(out_dir, stem + ".changelog.json"),
        }
        txt = self.cleaned if newline == "\n" else self.cleaned.replace("\n", newline)
        with open(paths["cleaned"], "w", encoding="utf-8", newline="") as f:
            f.write(txt)
        with open(paths["report"], "w", encoding="utf-8") as f:
            json.dump(self.report, f, ensure_ascii=False, indent=1)
        with open(paths["queue"], "w", encoding="utf-8") as f:
            json.dump([i.as_dict() for i in self.review_queue], f,
                      ensure_ascii=False, indent=1)
        with open(paths["log"], "w", encoding="utf-8") as f:
            json.dump(self.change_log, f, ensure_ascii=False, indent=1)
        with open(paths["events"], "w", encoding="utf-8") as f:
            json.dump({"initial": [e.as_dict() for e in self.audit_events],
                       "final": [e.as_dict() for e in self.final_events]},
                      f, ensure_ascii=False, indent=1)
        if self.cleaned_v3 and self.cleaned_v3 != self.cleaned:
            auto = os.path.join(out_dir, stem + ".auto.txt")
            txt2 = self.cleaned_v3 if newline == "\n" \
                else self.cleaned_v3.replace("\n", newline)
            with open(auto, "w", encoding="utf-8", newline="") as f:
                f.write(txt2)
            paths["auto_v3"] = auto
        return paths


# ---------------------------------------------------------------------------
# Review Queue 构造（§八）
# ---------------------------------------------------------------------------
def _priority_of(engine: V3Engine, orig_line: str, cur_line: str,
                 n_independent: int, cfg: PipelineConfig) -> int:
    """给一个"引擎动过"的区域定送审优先级（§九），只影响送审顺序。

    1 = 删掉了常用字、删掉了语料里不存在的字、或把整行删掉一半以上；
    2 = 引擎动过但外部独立证据不足；3 = 引擎动过且证据充分（只记录）。
    """
    rm = _diff_removed(orig_line, cur_line)
    for j in sorted(rm):
        f = engine.lm.uni.get(orig_line[j], 0)
        if f > cfg.common_char_freq or f == 0:
            return 1
    if len(rm) >= 8 and len(rm) * 2 >= max(1, len(orig_line)):
        return 1
    if n_independent < cfg.weak_support_evidence:
        return 2
    return 3


def _find_event(events: list, li: int):
    for ev in events:
        if ev.line_start - 1 <= li < ev.line_end:
            return ev
    return None


def _build_item(engine: V3Engine, ev, cur: list, offs: list, li: int,
                cfg: PipelineConfig, orig_line: str) -> ReviewItem:
    """为一个异常区域中的某一行构造 ReviewItem（§八）。

    Review Region 就是这一行的原文（suspicious_text 必须给原文），
    default_text 是 V3 清洗后的默认结果；候选为 no_change / restore。
    """
    line = cur[li]
    acted = orig_line != line
    empty_sc = engine._line_score(list(orig_line), ())
    cands = [CandidateRepair("no_change", [], empty_sc, "", "keep", text=line)]
    if acted:
        cands.append(CandidateRepair("restore", [], empty_sc, "", "revert",
                                     text=orig_line))
    reason_code = "v3_acted" if acted else "v3_left"

    if not acted:
        prio = 1 if ev.severity == SEV_HIGH else (
            2 if ev.severity == SEV_MEDIUM else 3)
    else:
        prio = _priority_of(engine, orig_line, line, ev.n_independent, cfg)

    types = sorted({e.type for e in ev.evidence})
    rm_pos = sorted(_diff_removed(orig_line, line)) if acted else []
    return ReviewItem(
        item_id="%s:L%d" % (ev.book_id or "book", li + 1),
        book_id=ev.book_id, chapter_id=ev.chapter_id,
        line_start=li + 1, line_end=li + 1,
        start=offs[li], end=offs[li] + len(orig_line),
        severity=ev.severity,
        context_before="".join(cur[max(0, li - 1):li])[-cfg.context_chars:],
        suspicious_text=orig_line,
        context_after="".join(cur[li + 1:li + 2])[:cfg.context_chars],
        evidence=[e.as_dict() for e in ev.evidence][:12],
        candidate_repairs=cands,
        default_text=line,
        strong_positions=strong_positions_of(orig_line),
        v3_result={
            "acted": acted,
            "line": li + 1,
            "original_text": orig_line,
            "cleaned_text": line,
            "removed_text": _only_removed(orig_line, line),
            "n_removed": len(orig_line) - len(line) if acted else 0,
            "removed_positions": rm_pos,
            "default_text": line,
            "audit_event": {"severity": ev.severity,
                            "line_start": ev.line_start, "line_end": ev.line_end,
                            "n_evidence": ev.n_evidence,
                            "n_independent": ev.n_independent,
                            "density": round(ev.density, 3)},
        },
        anomaly_score=ev.max_conf,
        anomaly_reason=";".join(types),
        reason_code=reason_code,
        priority=prio,
    )


def _new_structure(initial: list, final: list) -> int:
    """Final Audit：最终文本里**新增**的结构异常区域数量（§十二）。"""
    def keys(evs):
        return {(e.line_start, e.line_end) for e in evs if e.has_structure}
    return len(keys(final) - keys(initial))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_book(text: str, engine: V3Engine, cfg: PipelineConfig | None = None,
             book_id: str = "", resolver=None, prompt_sink: list | None = None
             ) -> PipelineResult:
    """单本书的全链路处理。text 应已是目标字形（如需繁→简请先转换）。"""
    cfg = cfg or PipelineConfig()
    resolver = resolver or NullResolver()
    t0 = time.time()
    res = PipelineResult(book_id=book_id, cleaned=text)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    orig_offs = _offsets(lines)

    # ================================================================
    # 一、V3 Auto Repair —— 主引擎，无门禁（§三）
    # ================================================================
    v3 = v3_auto_repair(engine, lines, progress=cfg.audit_progress or None)
    cur = [lines[i] if i not in v3 else _remove(lines[i], v3[i][0])
           for i in range(len(lines))]

    # ================================================================
    # 二、Audit —— 只读旁路，复用 V3 决策保证对齐（§三：不参与门控）
    # ================================================================
    layer = AuditLayer(engine, cfg.audit)
    events = layer.scan_book(text, book_id, proposals=v3)
    res.audit_events = events
    for ev in events:
        acted = any(li in v3 for li in range(ev.line_start - 1, ev.line_end))
        ev.auto_repair = acted
        if not acted:
            ev.note = "V3 未处理该区域，交 Review"

    change_log: list = []
    auto_lines = 0
    auto_chars = 0
    for li in sorted(v3):
        rm = v3[li][0]
        if not rm:
            continue
        auto_lines += 1
        auto_chars += len(rm)
        ev = _find_event(events, li)
        change_log.append({
            "location": {"chapter": ev.chapter_id if ev else "全文",
                         "line": li + 1, "start": orig_offs[li],
                         "end": orig_offs[li] + len(lines[li])},
            "original": lines[li],
            "replacement": "",
            "deleted": "".join(lines[li][j] for j in sorted(rm)),
            "result_text": cur[li],
            "method": "rule_engine",
            "engine": "v3",
            "margin": round(v3[li][1], 3),
            "evidence": [e.as_dict() for e in ev.evidence][:8] if ev else [],
            "severity": ev.severity if ev else "",
            "round": 1,
        })

    # ================================================================
    # 三、Review Queue（§八/§九）
    # ================================================================
    offs = orig_offs
    queue: list = []
    queued_lines: set = set()
    low_skipped = 0
    for ev in events:
        if ev.severity == SEV_LOW:
            low_skipped += 1
            continue
        for li in range(ev.line_start - 1, ev.line_end):
            if li in queued_lines or not cur[li]:
                continue
            queue.append(_build_item(engine, ev, cur, offs, li, cfg, lines[li]))
            queued_lines.add(li)
        ev.llm_review = True
    # 同优先级内按异常分数降序：截断时先送最可疑的，而不是行号最小的。
    queue.sort(key=lambda it: (it.priority, -it.anomaly_score, it.line_start))
    to_send = [it for it in queue if it.priority <= cfg.llm_max_priority]
    if cfg.max_llm_items:
        to_send = to_send[:cfg.max_llm_items]

    # ================================================================
    # 四、LLM Review（§七/§十/§十一）
    # ================================================================
    res.cleaned_v3 = "\n".join(cur)      # V3-only 文本（回滚基线）
    if to_send and hasattr(resolver, "prepare"):
        try:
            st = resolver.prepare(to_send, progress=cfg.llm_progress)
            if isinstance(st, dict):
                res.llm_stats = st
        except Exception as e:                              # noqa: BLE001
            res.llm_stats = {"prepare_error": str(e)[:300]}

    llm_lines = llm_chars = llm_added = 0
    kept = uncertain = rejected = 0
    applied_ids: set = set()

    def _apply_item(item: ReviewItem, round_no: int) -> None:
        nonlocal llm_lines, llm_chars, llm_added, kept, uncertain, rejected
        if cfg.cancel is not None and cfg.cancel():
            uncertain += 1
            return
        if prompt_sink is not None:
            prompt_sink.append({"item_id": item.item_id,
                                "prompt_version": "review-v2",
                                "priority": item.priority,
                                "reason_code": item.reason_code,
                                "prompt": build_prompt(item)})
        decision = parse_decision(resolver.review(item))
        item.decision = decision.get("decision", DECISION_UNCERTAIN)
        item.confidence = decision.get("confidence", 0.0)
        item.reason = decision.get("reason", "")
        item.selected_candidate = decision.get("selected_candidate", "")
        item.operations = decision.get("operations", []) or []
        item.model = getattr(resolver, "model", "") or getattr(resolver, "name", "")
        li = item.line_start - 1
        before = cur[li]
        new_text, cid, note = apply_decision(item, decision)
        if item.decision == DECISION_REPAIR and new_text is not None \
                and new_text != before:
            _same, ins, dele = _diff_ops(before, new_text)
            _s2, ins_r, dele_r = _diff_ops(item.suspicious_text, new_text)
            item.resolved_text = new_text
            cur[li] = new_text
            llm_lines += 1
            llm_chars += len(dele)
            llm_added += len(ins)
            applied_ids.add(item.item_id)
            change_log.append({
                "location": {"chapter": item.chapter_id, "line": li + 1,
                             "start": item.start, "end": item.end},
                # 相对原文的改动（便于回滚/比对）
                "original": item.suspicious_text,
                "replacement": ins_r,
                "deleted": dele_r,
                "result_text": new_text,
                "before_llm": before,
                "source_text": item.v3_result.get("original_text", ""),
                "v3_before": item.v3_result.get("cleaned_text", ""),
                "method": "llm",
                "engine": "llm_review",
                "resolver": getattr(resolver, "name", "unknown"),
                "model": item.model,
                "prompt_version": "review-v2",
                "confidence": item.confidence,
                "selected_candidate": cid,
                "operations": item.operations,
                "decision": item.decision,
                "reason": item.reason,
                "evidence": item.evidence[:8],
                "severity": item.severity,
                "round": round_no,
                "note": note,
            })
        elif item.decision == DECISION_KEEP:
            kept += 1
            item.resolved_text = before
        elif item.decision == DECISION_UNCERTAIN:
            uncertain += 1
        else:
            rejected += 1
            change_log.append({
                "location": {"chapter": item.chapter_id, "line": li + 1,
                             "start": item.start, "end": item.end},
                "original": before, "replacement": "",
                "method": "llm", "engine": "llm_review",
                "decision": item.decision, "rejected": True,
                "noop": True, "note": note,
                "selected_candidate": cid,
                "operations": item.operations,
                "model": item.model,
                "prompt_version": "review-v2",
                "confidence": item.confidence, "reason": item.reason,
                "round": round_no,
            })

    for item in to_send:
        _apply_item(item, 1)

    # ================================================================
    # 五、Final Audit（§十二）+ 有界复审（最多 1 轮）
    # ================================================================
    layer_f = layer
    rounds = 0
    final_round_items = 0
    if cfg.reaudit:
        rounds = 1
        res.cleaned = "\n".join(cur)
        # 主引擎一个字都没删、也没有任何 LLM 决策改动正文时，cur 与原文完全一致，
        # 此时终审那次引擎重跑必然给出与 v3 相同的结果，直接复用即可省一遍全量定位。
        fin = v3 if (not v3 and llm_lines == 0) else v3_auto_repair(engine, cur)
        layer_f = AuditLayer(engine, cfg.audit)
        res.final_events = layer_f.scan_book(res.cleaned, book_id,
                                             proposals=fin)

        if cfg.final_review_rounds > 0:
            decided = {it.line_start for it in queue if it.decision}
            cand_items: list = []
            seen2: set = set()
            for ev in res.final_events:
                if ev.severity == SEV_LOW:
                    continue
                for li in range(ev.line_start - 1, ev.line_end):
                    if li in seen2 or not cur[li] or li in decided:
                        continue
                    seen2.add(li)
                    it = _build_item(engine, ev, cur, orig_offs, li, cfg, lines[li])
                    if it.priority <= cfg.llm_max_priority:
                        cand_items.append(it)
            if cand_items and cfg.max_llm_items:
                cand_items = cand_items[:cfg.max_llm_items]
            if cand_items:
                if hasattr(resolver, "prepare"):
                    try:
                        resolver.prepare(cand_items, progress=cfg.llm_progress)
                    except Exception:                        # noqa: BLE001
                        pass
                applied_before = llm_lines
                for it in cand_items:
                    queue.append(it)
                    _apply_item(it, 2)
                final_round_items = len(cand_items)
                rounds += 1
                # 只有真改动了正文才需要再跑一遍引擎和终审扫描：没有改动时
                # cur 与上面那次完全相同，重跑必然得到同样的结果，白花时间。
                if llm_lines > applied_before:
                    res.cleaned = "\n".join(cur)
                    fin = v3_auto_repair(engine, cur)
                    layer_f = AuditLayer(engine, cfg.audit)
                    res.final_events = layer_f.scan_book(res.cleaned, book_id,
                                                         proposals=fin)

    # ================================================================
    # 六、统计与报告（§十四/§十五 E）
    # ================================================================
    res.cleaned = "\n".join(cur)
    res.review_queue = queue
    res.change_log = change_log
    fh = layer_f.stats(res.final_events) if cfg.reaudit else {}
    oh = layer.stats(events)
    res.seconds = round(time.time() - t0, 2)

    chars_deleted = chars_added = 0
    for a, b in zip(lines, cur):
        if a != b:
            _s, ins, dele = _diff_ops(a, b)
            chars_added += len(ins)
            chars_deleted += len(dele)
    unresolved = [e for e in (res.final_events or [])
                  if e.severity in (SEV_HIGH, SEV_MEDIUM)]
    by_prio: dict = {}
    for it in queue:
        by_prio[it.priority] = by_prio.get(it.priority, 0) + 1
    res.report = {
        "book_id": book_id,
        "engine": "semantic_auto_repair + audit + llm_review + final_audit",
        "lm": os.path.basename(getattr(engine, "_lm_path", "") or ""),
        "chars_total": len(text),
        "chars_final": len(res.cleaned),
        "lines_total": len(lines),
        "metrics": {
            "chars_total": len(text),
            "regions_detected": oh["events"],
            "regions_auto_repaired": len([e for e in events if e.auto_repair]),
            "auto_repair_lines": auto_lines,
            "auto_repair_chars": auto_chars,
            "llm_review_items": len(queue),
            "llm_review_sent": len(to_send) + final_round_items,
            "llm_repaired_items": llm_lines,
            "llm_repaired_chars": llm_chars,
            "llm_added_chars": llm_added,
            "unresolved_regions": len(unresolved),
            "chars_deleted": chars_deleted,
            "chars_added": chars_added,
            "residual_regions_final": fh.get("events", 0),
        },
        "auto_repair": {"lines": auto_lines, "chars": auto_chars,
                        "engine": "v3", "gated": False},
        "llm_review": {"items": len(queue), "sent": len(to_send) + final_round_items,
                       "final_round_items": final_round_items,
                       "lines": llm_lines, "chars": llm_chars,
                       "added": llm_added, "kept": kept,
                       "uncertain": uncertain, "rejected": rejected,
                       "by_priority": by_prio,
                       "resolver": getattr(resolver, "name", "unknown"),
                       "stats": res.llm_stats},
        "audit": {
            "events_initial": oh["events"],
            "events_final": fh.get("events", 0),
            "by_severity_initial": oh["by_severity"],
            "by_severity_final": fh.get("by_severity", {}),
            "by_evidence_type": oh["by_evidence_type"],
            "chars_covered_initial": oh["chars_covered"],
            "chars_covered_final": fh.get("chars_covered", 0),
            "low_regions_recorded": low_skipped,
        },
        "final_audit": {
            "rounds": rounds,
            "new_structure_anomalies": _new_structure(events, res.final_events),
            "unresolved_high": len([e for e in unresolved if e.severity == SEV_HIGH]),
            "unresolved_medium": len([e for e in unresolved
                                      if e.severity == SEV_MEDIUM]),
            "chars_delta": chars_added - chars_deleted,
        },
        "unresolved_events": len(unresolved),
        "seconds": res.seconds,
    }
    return res


# ---------------------------------------------------------------------------
def run_files(inputs: list, out_dir: str, engine: V3Engine,
              cfg: PipelineConfig | None = None, resolver=None,
              progress=None) -> list:
    cfg = cfg or PipelineConfig()
    files = []
    for it in inputs:
        if os.path.isdir(it):
            files.extend(list_txt_files(it, recursive=True))
        elif os.path.isfile(it):
            files.append(it)
    out: list = []
    for i, p in enumerate(files):
        if progress:
            progress(i, len(files), os.path.basename(p))
        try:
            text, _enc = read_text(p)
        except OSError as e:
            out.append({"src": p, "error": str(e)})
            continue
        nl = newline_of(text)
        if cfg.simplify:
            from .t2s import to_simplified
            text = to_simplified(text)
        prompts: list = []
        r = run_book(text, engine, cfg, book_id=os.path.basename(p),
                     resolver=resolver, prompt_sink=prompts)
        paths = r.write_outputs(out_dir, os.path.basename(p), nl)
        out.append({"src": p, **paths, "report": r.report,
                    "prompts": prompts, "result": r})
    return out


def main(argv=None) -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(
        prog="adclean.pipeline",
        description="V3 Auto Repair → Review Queue → LLM → Final Audit"
                    "（原文件永不修改）")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--out", default="cleaned_audit")
    p.add_argument("--lm", default="", help="clean n-gram 缓存路径")
    p.add_argument("--audit-min-conf", type=float, default=AuditConfig.min_conf)
    p.add_argument("--llm-max-priority", type=int, default=2,
                   help="送审优先级上限（1=引擎删了常用字 2=证据不足 3=只记录）")
    p.add_argument("--max-llm-items", type=int, default=0)
    p.add_argument("--simplify", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="先做繁→简转换（默认开启，用 --no-simplify 关闭）。"
                        "开启后繁体字会被换成简体字，属于字形改写；"
                        "关闭则保留原字形，但简繁混排的书可能把繁体正文误判成插入字")
    p.add_argument("--no-final-review", action="store_true",
                   help="关闭 Final Audit 之后的那 1 轮复审")
    p.add_argument("--dump-prompts", default="",
                   help="把每个 review item 的 prompt 落盘（供外部 LLM）")
    p.add_argument("--resolve", default="",
                   help="外部决策 JSON（{item_id: {decision,...}}），用于离线回填")
    p.add_argument("--names", default=",".join(DEFAULT_NAMES),
                   help="人名/专名白名单（逗号分隔），这些字永不作为可疑字")
    p.add_argument("--engine", default="locate",
                   choices=("locate", "semantic", "v3"),
                   help="locate=多特征字级定位器（默认，误删最少）；"
                        "semantic=V4 句子级语义；v3=旧字频引擎")
    p.add_argument("--cost-scale", type=float, default=0.0,
                   help="V4 每删一字的代价倍数（0=用默认 2.0，正读+反读）")
    p.add_argument("--near-k", type=int, default=-1,
                   help="定位器：邻域内异常位置数少于此值就不删（默认 4；0=关闭）")
    p.add_argument("--near-w", type=int, default=-1,
                   help="定位器：邻域半径（默认 20）")
    p.add_argument("--trim", type=float, default=-1.0,
                   help="定位器：边界回撤阈值（默认 0.5；0=关闭）")
    args = p.parse_args(argv)

    names = tuple(x for x in (args.names or "").split(",") if x)
    overrides = {}
    if args.cost_scale:
        overrides["cost_scale"] = args.cost_scale
    if args.near_k >= 0:
        overrides["near_k"] = args.near_k
    if args.near_w >= 0:
        overrides["near_w"] = args.near_w
    if args.trim >= 0:
        overrides["trim"] = args.trim
    engine = build_engine(args.lm, names, overrides=overrides, kind=args.engine)
    sys.stderr.write("engine: %s   n-gram: %s\n" % (args.engine, engine._lm_path))
    if args.engine == "v3":
        sys.stderr.write("frozen v3 config: %s\n"
                         % ", ".join("%s=%s" % (k, getattr(engine.cfg, k))
                                     for k in ("score_mode", "insert_prior",
                                               "candidate_mode", "max_rounds")))
    elif args.engine == "locate":
        sys.stderr.write(
            "v1 判据: 40 维局部搭配 + 跨书负例模型（只删不改）｜"
            "上下文异常数 ±%d≥%d｜边界回撤 %.2f\n"
            % (engine.loc.near_w, engine.loc.near_k, engine.loc.trim))
    else:
        sys.stderr.write("v4 判据: 句子级语义正常性（正读+反读），每删一字代价 %.3f\n"
                         % engine.cleaner._penalty)

    sys.stderr.write("字形: %s\n" % ("先做繁→简转换（默认）" if args.simplify
                                 else "保留原字形（--no-simplify）"))

    resolver = None
    if args.resolve:
        from .review import MappingResolver
        resolver = MappingResolver.from_json(args.resolve)

    cfg = PipelineConfig(simplify=bool(args.simplify),
                         llm_max_priority=args.llm_max_priority,
                         max_llm_items=args.max_llm_items,
                         final_review_rounds=0 if args.no_final_review else 1)
    cfg.audit.min_conf = args.audit_min_conf

    def prog(done, total, msg):
        sys.stderr.write("\r[%d/%d] %-50s" % (done + 1, total, msg[:50]))
        sys.stderr.flush()

    outs = run_files(args.inputs, args.out, engine, cfg, resolver, prog)
    sys.stderr.write("\n")
    if args.dump_prompts:
        allp = [x for o in outs for x in o.get("prompts", [])]
        with open(args.dump_prompts, "w", encoding="utf-8") as f:
            json.dump(allp, f, ensure_ascii=False, indent=1)
        sys.stderr.write("prompts → %s (%d 条)\n" % (args.dump_prompts, len(allp)))
    tot_a = tot_l = tot_q = 0
    for o in outs:
        if o.get("error"):
            sys.stderr.write("  %s 失败 %s\n" % (o["src"], o["error"]))
            continue
        m = o["report"]["metrics"]
        tot_a += m["auto_repair_chars"]
        tot_l += m["llm_repaired_chars"]
        tot_q += m["llm_review_items"]
        sys.stderr.write(
            "  %s\n     异常区域 %d → %d | auto %d 行/%d 字 | 队列 %d 项"
            "（送审 %d）| LLM 修 %d 项/%d 字 | 残留 %d\n"
            % (os.path.basename(o["src"]), m["regions_detected"],
               m["residual_regions_final"], m["auto_repair_lines"],
               m["auto_repair_chars"], m["llm_review_items"],
               m["llm_review_sent"], m["llm_repaired_items"],
               m["llm_repaired_chars"], m["unresolved_regions"]))
    sys.stderr.write("\n完成：auto %d 字 / LLM %d 字 / 队列 %d 项 → %s\n"
                     % (tot_a, tot_l, tot_q, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
