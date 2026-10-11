# -*- coding: utf-8 -*-
"""adclean.review —— LLM Review Queue 与 LLM Resolver 接口。

决策 dict 字段：decision / operations / selected_candidate / final_text /
confidence / reason；优先级 operations > selected_candidate > final_text。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

DECISION_REPAIR = "repair"
DECISION_KEEP = "keep"
DECISION_UNCERTAIN = "uncertain"
VALID_DECISIONS = (DECISION_REPAIR, DECISION_KEEP, DECISION_UNCERTAIN)

# 允许的 operation 类型（§十）
OP_DELETE = "delete"
OP_REPLACE = "replace"
OP_INSERT = "insert"
VALID_OPS = (OP_DELETE, OP_REPLACE, OP_INSERT)

# ---------------------------------------------------------------------------
# 只删不改
# ---------------------------------------------------------------------------
# 打开后 operations 只接受 delete，且不接受 LLM 直接给出的 final_text。
DELETE_ONLY = True
# LLM 可以触及的最大范围外扩（相对 Review Region）；0 = 严格不出区域（§七）
REGION_PAD = 0


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class CandidateRepair:
    id: str
    delete_ranges: list            # [[start, end), ...] 绝对字符偏移
    score: float = 0.0
    removed_text: str = ""
    source: str = "rule_engine"    # v3_engine | revert | keep | pool_hint | llm
    # 整区域替换文本（可选）：非空时**直接**作为修复结果，忽略 delete_ranges。
    # 用于「撤销 V3 改动，恢复原文」这类候选。
    text: str = ""

    def as_dict(self) -> dict:
        d = {"id": self.id, "delete_ranges": [list(r) for r in self.delete_ranges],
             "score": round(self.score, 3), "source": self.source}
        if self.removed_text:
            d["removed_text"] = self.removed_text
        if self.text:
            d["text"] = self.text
        return d


@dataclass
class ReviewItem:
    item_id: str
    book_id: str
    chapter_id: str
    line_start: int
    line_end: int
    start: int
    end: int
    severity: str
    context_before: str
    suspicious_text: str
    context_after: str
    evidence: list = field(default_factory=list)
    candidate_repairs: list = field(default_factory=list)
    # --- §八：V3 结果 / 异常分数 / 异常原因 ---
    v3_result: dict = field(default_factory=dict)
    anomaly_score: float = 0.0
    anomaly_reason: str = ""
    # 系统默认结果（= V3 清洗之后这一行的文本）。decision=keep 时采用它。
    default_text: str = ""
    # 强证据位置（行内下标）：不可见字符 / 非 BMP 表意字符。
    strong_positions: list = field(default_factory=list)
    # 该 item 因为什么进队列（§九 优先级）：v3_left / v3_acted_weak / v3_acted_strong
    reason_code: str = ""
    priority: int = 3              # 1 最高
    # 由 resolver / pipeline 填
    decision: str = ""
    selected_candidate: str = ""
    operations: list = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    model: str = ""
    resolved_text: str = ""

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "book_id": self.book_id,
            "chapter_id": self.chapter_id,
            "lines": [self.line_start, self.line_end],
            "start": self.start,
            "end": self.end,
            "severity": self.severity,
            "reason_code": self.reason_code,
            "priority": self.priority,
            "context_before": self.context_before,
            "suspicious_text": self.suspicious_text,
            "context_after": self.context_after,
            "original_text": self.suspicious_text,
            "default_text": self.default_text,
            "evidence": self.evidence,
            "anomaly_score": round(self.anomaly_score, 3),
            "anomaly_reason": self.anomaly_reason,
            "strong_positions": self.strong_positions,
            "v3_result": self.v3_result,
            "candidate_repairs": [c.as_dict() for c in self.candidate_repairs],
            "decision": self.decision,
            "selected_candidate": self.selected_candidate,
            "operations": self.operations,
            "confidence": self.confidence,
            "reason": self.reason,
            "model": self.model,
        }

    def candidate(self, cid: str):
        for c in self.candidate_repairs:
            if c.id == cid:
                return c
        return None


# ---------------------------------------------------------------------------
# Resolver 接口
# ---------------------------------------------------------------------------
class LLMResolver(Protocol):
    """LLM 解析器协议（§十五 D）：接收 ReviewItem，返回结构化决策 dict；
    review() 与 resolve() 都必须可用且等价。
    """

    def review(self, item: ReviewItem) -> dict:       # pragma: no cover
        ...

    def resolve(self, item: ReviewItem) -> dict:      # pragma: no cover
        ...


class _ReviewAlias:
    """给所有 resolver 加上 `review()` 入口（§十五 D 要求的稳定接口）。"""

    def review(self, item: ReviewItem) -> dict:
        return self.resolve(item)


class NullResolver(_ReviewAlias):
    """不做任何判断（默认）：全部记为 uncertain，保持引擎结果。"""

    name = "null"

    def resolve(self, item: ReviewItem) -> dict:
        return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                "reason": "无 resolver（未接入 LLM），保持原文"}


class MappingResolver(_ReviewAlias):
    """从 {item_id: decision_dict} 映射取决策（离线审查 / 外部回填 / 测试）。"""

    def __init__(self, mapping: dict, name: str = "mapping") -> None:
        self.mapping = mapping or {}
        self.name = name

    @classmethod
    def from_json(cls, path: str, name: str | None = None) -> "MappingResolver":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):        # [{item_id, decision, ...}, ...]
            data = {d["item_id"]: d for d in data}
        return cls(data, name or "json:" + path)

    def resolve(self, item: ReviewItem) -> dict:
        d = self.mapping.get(item.item_id)
        if not d:
            return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                    "reason": "映射中无该 item"}
        return d


class SimulatedResolver(_ReviewAlias):
    """模拟 LLM 审查器（§十五 D：没有真实 API 时的 mock，非生产）。

    策略：区域内有尚未被引擎删掉的强证据字符 → 用 operations 精确删掉；
    其余 → keep，绝不擅自撤销引擎的修复。
    """

    name = "simulated"

    def __init__(self, freq_of=None, common_freq: int = 2000) -> None:
        self.freq_of = freq_of or (lambda ch: 0)
        self.common_freq = common_freq

    def resolve(self, item: ReviewItem) -> dict:
        v3 = item.v3_result or {}
        removed_pos = set(v3.get("removed_positions") or [])
        text = item.suspicious_text
        ops = []
        for p in item.strong_positions:
            if p not in removed_pos and 0 <= p < len(text):
                ops.append({"type": "delete", "start": item.start + p,
                            "end": item.start + p + 1})
        if not ops:
            return {"decision": DECISION_KEEP, "confidence": 0.8,
                    "reason": "模拟审查器：没有强证据，认可系统默认结果"}
        return {"decision": DECISION_REPAIR, "confidence": 0.86,
                "reason": "删除区域内尚未被处理的强证据字符（%d 处）" % len(ops),
                "operations": ops}


class CallableResolver(_ReviewAlias):
    """把任意函数包成 resolver：fn(item: ReviewItem) -> dict。"""

    def __init__(self, fn: Callable[[ReviewItem], dict], name: str = "callable") -> None:
        self.fn = fn
        self.name = name

    def resolve(self, item: ReviewItem) -> dict:
        try:
            d = self.fn(item)
        except Exception as e:                        # noqa: BLE001
            return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                    "reason": "resolver 异常：%s" % e}
        if isinstance(d, str):
            d = parse_decision(d)
        return d or {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                     "reason": "无法解析 resolver 输出"}


# ---------------------------------------------------------------------------
# 结构化输出解析 / 校验
# ---------------------------------------------------------------------------
def parse_decision(obj) -> dict:
    """把 LLM 输出（dict 或 JSON 字符串）规整成标准决策 dict；解析失败 → uncertain。"""
    if obj is None:
        return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                "reason": "空输出"}
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("```"):
            s = s.strip("`")
            i = s.find("{")
            j = s.rfind("}")
            if i >= 0 and j > i:
                s = s[i:j + 1]
        try:
            obj = json.loads(s)
        except Exception:                              # noqa: BLE001
            return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                    "reason": "输出不是合法 JSON", "raw": obj[:200]}
    if not isinstance(obj, dict):
        return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                "reason": "输出不是对象"}
    d = str(obj.get("decision", "")).strip().lower()
    if d not in VALID_DECISIONS:
        return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                "reason": "decision 非法：%r" % obj.get("decision"),
                "raw": json.dumps(obj, ensure_ascii=False)[:200]}
    try:
        conf = float(obj.get("confidence", 0.0))
    except Exception:                                  # noqa: BLE001
        conf = 0.0
    out = {"decision": d, "confidence": max(0.0, min(1.0, conf)),
           "reason": str(obj.get("reason", ""))[:400]}
    if obj.get("selected_candidate"):
        out["selected_candidate"] = str(obj["selected_candidate"])
    if obj.get("final_text"):
        out["final_text"] = str(obj["final_text"])
    ops = obj.get("operations")
    if isinstance(ops, list):
        out["operations"] = [o for o in ops if isinstance(o, dict)]
    return out


def _op_apply(region: str, region_start: int, ops: list) -> tuple:
    """把 operations 落到区域文本上。

    返回 (new_region_text, note)；ops 非法（越界 / 类型未知）时 new 为 None。
    """
    chars = list(region)
    # 先校验再执行：任何一条越界都整条拒绝。
    norm = []
    for o in ops:
        t = str(o.get("type", "")).strip().lower()
        if t not in VALID_OPS:
            return None, "未知 operation 类型 %r" % o.get("type")
        if DELETE_ONLY and t != OP_DELETE:
            return None, ("只删不改：拒绝 %s 操作（可在高级设置里关闭该约束）" % t)
        try:
            s = int(o.get("start", 0))
            e = int(o.get("end", s)) if t != OP_INSERT else s
        except Exception:                              # noqa: BLE001
            return None, "operation 坐标不是整数"
        if t == OP_INSERT:
            s = e = int(o.get("start", 0))
        if s > e:
            s, e = e, s
        lo = region_start - REGION_PAD
        hi = region_start + len(region) + REGION_PAD
        if s < lo or e > hi:
            return None, ("operation 越出 Review Region（[%d,%d) 不在 [%d,%d) 内）"
                          % (s, e, lo, hi))
        norm.append((t, s, e, str(o.get("text", ""))))
    # 从后往前应用，保证偏移不失效
    for t, s, e, text in sorted(norm, key=lambda x: -x[1]):
        a, b = s - region_start, e - region_start
        if t == OP_DELETE:
            del chars[a:b]
        elif t == OP_REPLACE:
            chars[a:b] = list(text)
        else:                                          # OP_INSERT
            chars[a:a] = list(text)
    return "".join(chars), ""


# ---------------------------------------------------------------------------
# Prompt 构造（外部 LLM 用）
# ---------------------------------------------------------------------------
PROMPT_VERSION = "review-v2"

PROMPT_TEMPLATE = """你是中文小说文本校对助手。系统已经用一个规则引擎清洗过整本书，\
下面这块区域是**规则引擎之后仍然可疑**或**规则引擎改动过但证据不足**的部分。

你的任务是判断这块区域应当怎么处理。你可以：
  - keep       保持现状（认为没有问题 / 规则引擎的处理是对的）
  - repair     修改它。允许的操作：
        delete   删除一段连续字符            {{"type":"delete","start":A,"end":B}}
        replace  把一段字符替换成别的字符    {{"type":"replace","start":A,"end":B,"text":"新文本"}}
        insert   在某位置插入字符            {{"type":"insert","start":A,"text":"新文本"}}
  - uncertain  你判断不了（系统会保留原文并记入待人工处理清单，这是**安全**的选择）

【硬约束】
1. 你**只能**修改上面"待判断区域"范围内的字符，坐标不能越界。
   你看到的上下文只用于判断，**不要**对它提出修改。
2. 不要改写句子、不要润色、不要补全。只处理"明显不属于原文"的广告/乱码字符。
3. 人名、专名、生僻词、方言、作者用语、颜文字、外文原文都属于正文，不要动。
4. 不确定就选 uncertain，不要猜。**错删正常文字比留着广告更严重。**

【重要：有些乱码是"真字被替换"而不是"插入了多余字"】
例如 `灰𰰡𰰡的天空` 其实是 `灰蒙蒙的天空`，`红玛𰡻般的液体` 其实是 `红玛瑙般的液体`。
如果某个乱码位置填上一个常见字后语义完全通顺，正确修复是 **replace**，不是 delete。

===== 待判断区域（原文）=====
绝对偏移区间 [A={start}, B={end})（换行归一化后全书字符下标，0-based，含头不含尾）
区域内第 i 个字符的绝对偏移 = {start} + i

{segment}

===== 上下文（前，仅供判断，不要修改）=====
{before}

===== 上下文（后，仅供判断，不要修改）=====
{after}

===== 规则引擎（V3）的结果 =====
{v3}

===== 系统检测到的异常证据 =====
{evidence}

===== 系统给出的参考方案（**仅供参考**，你可以提出自己的方案）=====
{candidates}

【输出】只输出一个 JSON 对象，不要任何多余文字、不要 markdown 代码块：
{{"decision": "keep" | "repair" | "uncertain",
  "operations": [],
  "confidence": 0.0,
  "reason": "简要理由"}}
"""


def build_prompt(item: ReviewItem) -> str:
    cand_lines = []
    for c in item.candidate_repairs:
        rm = "、".join("删除[%d,%d)" % (a, b) for a, b in c.delete_ranges) \
            or "不修改"
        label = {"v3_engine": "V3 引擎的改动", "keep": "保持当前文本（系统默认）",
                 "revert": "撤销引擎改动，恢复原文",
                 "pool_hint": "引擎试探（未通过评分，仅参考）"}.get(c.source, c.source)
        if c.text and c.source == "revert":
            cand_lines.append("%s（%s）: 完整恢复为「%s」" % (c.id, label, c.text))
            continue
        cand_lines.append("%s（%s）: %s%s"
                          % (c.id, label, rm,
                             ("  被删内容「%s」" % c.removed_text)
                             if c.removed_text else ""))
    if not cand_lines:
        cand_lines.append("（引擎没有给出任何候选——说明它认为这行不值得改；"
                          "请你自己判断）")
    v3 = item.v3_result or {}
    if v3.get("acted"):
        v3s = ("引擎**改过**这一行：删除了「%s」（%d 个字符）。"
               "上面给你的待判断区域是**原文**；**系统默认结果是引擎清洗后的文本**："
               "「%s」。\n"
               "如果你不修改（keep），系统就采用这个默认结果。"
               "如果你认为引擎删错了，可以选 `restore` 恢复原文，"
               "或用 operations 给出你自己的方案（坐标基于上面的原文）。"
               % (v3.get("removed_text", ""), v3.get("n_removed", 0),
                  v3.get("default_text", v3.get("cleaned_text", ""))))
        if v3.get("original_text") and v3.get("cleaned_text"):
            v3s += "\nV3 修改前：%s" % v3["original_text"]
            v3s += "\nV3 修改后：%s" % v3["cleaned_text"]
    else:
        v3s = "引擎**没有**改动这一行（它认为不需要改），但审查层仍然认为可疑。"
    ev = "；".join("%s=%.2f%s%s" % (e.get("type", "?"), e.get("score", 0.0),
                                   ("@%d" % e["pos"]) if "pos" in e else "",
                                   ("（%s）" % e["detail"]) if e.get("detail") else "")
                  for e in item.evidence) or "无"
    if item.strong_positions:
        ev += ("\n【强证据位置】行内下标 %s（绝对偏移 %s）——"
               "不可见字符/非 BMP 表意字符，在中文小说里几乎必然是污染。"
               % (item.strong_positions,
                  [item.start + p for p in item.strong_positions]))
    _out = PROMPT_TEMPLATE.format(
        start=item.start, end=item.end,
        before=item.context_before or "（无）",
        segment=item.suspicious_text,
        after=item.context_after or "（无）",
        v3=v3s,
        evidence=ev,
        candidates="\n".join(cand_lines),
    ) + "\n【异常分数】%.2f   【原因】%s\n" % (item.anomaly_score,
                                             item.anomaly_reason or "-")
    if DELETE_ONLY:
        _out = _out.replace(
            "  - repair     修改它。允许的操作：\n"
            "        delete   删除一段连续字符            {\"type\":\"delete\",\"start\":A,\"end\":B}\n"
            "        replace  把一段字符替换成别的字符    {\"type\":\"replace\",\"start\":A,\"end\":B,\"text\":\"新文本\"}\n"
            "        insert   在某位置插入字符            {\"type\":\"insert\",\"start\":A,\"text\":\"新文本\"}",
            "  - repair     删除多余字符。**只允许一个操作**：\n"
            "        delete   删除一段连续字符            {\"type\":\"delete\",\"start\":A,\"end\":B}\n"
            "        （禁止 replace / insert：本工具承诺只删不改，绝不改写正文）")
        _i = _out.find("【重要：有些乱码是")
        if _i >= 0:
            _j = _out.find("===== 待判断区域")
            if _j > _i:
                _out = _out[:_i] + _out[_j:]
        _out = _out.replace(
            "4. 不确定就选 uncertain，不要猜。**错删正常文字比留着广告更严重。**",
            "4. 不确定就选 uncertain，不要猜。**错删正常文字比留着广告更严重。**\n"
            "5. 本工具**只删不改**：不要 replace、不要 insert、不要改写或补全任何字。")
    return _out


# ---------------------------------------------------------------------------
# 应用决策
# ---------------------------------------------------------------------------
def render_region(region_text: str, region_start: int, candidate: CandidateRepair) -> str:
    """按候选渲染区域文本。候选带 `text` 时直接返回该文本（整区域替换）。"""
    if candidate.text:
        return candidate.text
    n = len(region_text)
    rm = set()
    for lo, hi in candidate.delete_ranges:
        for j in range(max(lo, region_start), min(hi, region_start + n)):
            rm.add(j - region_start)
    return "".join(ch for i, ch in enumerate(region_text) if i not in rm)


def apply_decision(item: ReviewItem, decision: dict) -> tuple:
    """把结构化决策落到文本。返回 (new_region_text or None, applied_id, note)。

    优先级（§十）：operations > selected_candidate > final_text；
    operations 越界、final_text 长度异常（疑似整段重写）一律整条拒绝。
    """
    d = decision.get("decision", DECISION_UNCERTAIN)
    if d == DECISION_KEEP:
        return (item.default_text or item.suspicious_text), "", "keep"
    if d == DECISION_REPAIR:
        ops = decision.get("operations") or []
        if ops:
            new, note = _op_apply(item.suspicious_text, item.start, ops)
            if new is None:
                return None, "", note
            if new == item.suspicious_text:
                return None, "", "operations 没有产生任何改变"
            return new, "llm_ops", note
        cid = decision.get("selected_candidate", "")
        cand = item.candidate(cid) if cid else None
        if cand is not None:
            return render_region(item.suspicious_text, item.start, cand), cid, ""
        ft = decision.get("final_text")
        if ft and not DELETE_ONLY:
            if len(ft) > 20 * max(1, len(item.suspicious_text)):
                return None, "", "final_text 长度异常（疑似整段重写），拒绝"
            return ft, "llm_free_text", "候选未命中，采用 LLM final_text"
        if ft and DELETE_ONLY:
            return None, "", "只删不改：不接受 LLM 直接给出的 final_text"
        return None, "", "decision=repair 但 selected_candidate 无效，且没有 operations/final_text"
    return None, "", "uncertain"
