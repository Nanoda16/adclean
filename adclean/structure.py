# -*- coding: utf-8 -*-
"""StructureDetector v1：只产出**结构异常锚点**，不做任何删除决策。

1. 只报客观结构错误（如 `（…）】` 括号类型不匹配），不碰语义、不判广告。
2. confidence 与统计锚点同量纲（0~10）：conf >= min_conf 才产出锚点。
3. span 是"受影响范围"（半开区间），不是"要删的范围"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .v2engine import DECOR_SAFE, _is_cjk_symbol

# 成对括号（含常用变体）
OPEN_CLOSE = {
    "（": "）", "【": "】", "《": "》", "「": "」", "『": "』",
    "(": ")", "[": "]", "{": "}",
    "〈": "〉", "〔": "〕", "〖": "〗", "〘": "〙", "〚": "〛",
}
CLOSE_OPEN = {v: k for k, v in OPEN_CLOSE.items()}

# CJK 符号区里**合法常用**的，不视为可疑
SYMBOL_SAFE = set("〆々〇〜ー・゠、。〳〴") | DECOR_SAFE

# 列表标记：数字/CJK 数词 + 分隔符（必须在行首或空白之后）
_LIST_RE = re.compile(r"(?:^|[\s\u3000])(\d{1,3}|[〇零一二三四五六七八九十]+)([、.．])")
_CJK_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9}

CONF = {
    "mismatch": 4.0,
    "extra_close": 3.5,
    "unclosed_open": 2.6,
    "unbalanced": 1.0,          # 仅记录
    "double_open": 2.0,
    "empty_pair": 1.2,          # 仅记录
    "list_order": 1.6,
    "struct_symbol": 2.0,
}


@dataclass
class Anomaly:
    pos: int                    # 主位置
    kind: str                   # 异常类型
    conf: float                 # 置信度（与统计可疑度同量纲）
    span: tuple                 # (lo, hi) 受影响范围，半开

    def as_dict(self) -> dict:
        return {"pos": self.pos, "kind": self.kind,
                "conf": self.conf, "span": list(self.span)}


def _cjk_num(s: str) -> int:
    """把 CJK 数词串转成整数（只处理 <=99 的常见情形）。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CJK_NUM.get(a, 1) if a else 1
        ones = _CJK_NUM.get(b, 0) if b else 0
        return tens * 10 + ones
    v = 0
    for ch in s:
        v = v * 10 + _CJK_NUM.get(ch, 0)
    return v


class StructureDetector:
    """检测结构异常，返回 Anomaly 列表。不修改任何文本。"""

    def __init__(self, min_conf: float = 0.0) -> None:
        self.min_conf = min_conf

    # ------------------------------------------------------------------
    def detect(self, chars: list, mask: list | None = None) -> list:
        n = len(chars)
        if mask is None:
            mask = [False] * n
        out: list = []
        out += self._brackets(chars, mask)
        out += self._doubled_and_empty(chars, mask)
        out += self._list_order(chars, mask)
        out += self._symbols(chars, mask)
        out = [a for a in out if a.conf >= self.min_conf]
        out.sort(key=lambda a: a.pos)
        return out

    # ---- R1/R2/R3/R4：栈式括号配对 ------------------------------------
    def _brackets(self, chars: list, mask: list) -> list:
        n = len(chars)
        out: list = []
        stack: list = []                     # (pos, open_ch)
        counts: dict = {}
        for i, ch in enumerate(chars):
            if mask[i]:
                continue
            if ch in OPEN_CLOSE:
                counts[ch] = counts.get(ch, 0) + 1
                stack.append((i, ch))
            elif ch in CLOSE_OPEN:
                o = CLOSE_OPEN[ch]
                counts[ch] = counts.get(ch, 0) + 1
                if not stack:
                    out.append(Anomaly(i, "多余的右括号「%s」（无对应「%s」）"
                                       % (ch, o), CONF["extra_close"], (i, i + 1)))
                    continue
                top_pos, top_ch = stack[-1]
                if OPEN_CLOSE[top_ch] == ch:
                    stack.pop()
                else:
                    # 类型不匹配：`【…）` 这种
                    span = (top_pos, i + 1)
                    out.append(Anomaly(top_pos, "括号类型不匹配「%s…%s」（应为「%s」）"
                                       % (top_ch, ch, OPEN_CLOSE[top_ch]),
                                       CONF["mismatch"], span))
                    out.append(Anomaly(i, "括号类型不匹配「%s…%s」（「%s」未闭合）"
                                       % (top_ch, ch, top_ch),
                                       CONF["mismatch"], span))
                    stack.pop()
        for pos, ch in stack:
            out.append(Anomaly(pos, "左括号未闭合「%s」（缺「%s」）"
                               % (ch, OPEN_CLOSE[ch]),
                               CONF["unclosed_open"], (pos, n)))
        # R4 家族数量不平衡（仅记录；具体位置已由上面的规则给出）
        for op, cl in OPEN_CLOSE.items():
            if counts.get(op, 0) != counts.get(cl, 0):
                first = next((i for i, c in enumerate(chars)
                              if c == op and not mask[i]), None)
                if first is not None:
                    out.append(Anomaly(first, "括号数量不平衡「%s」%d 个 /「%s」%d 个"
                                       % (op, counts.get(op, 0), cl, counts.get(cl, 0)),
                                       CONF["unbalanced"], (first, first + 1)))
        return out

    # ---- R5：连续相同左括号 / 空括号对 ---------------------------------
    def _doubled_and_empty(self, chars: list, mask: list) -> list:
        n = len(chars)
        out: list = []
        for i in range(n - 1):
            if mask[i] or mask[i + 1]:
                continue
            a, b = chars[i], chars[i + 1]
            if a in OPEN_CLOSE and b == a:
                out.append(Anomaly(i, "连续相同左括号「%s%s」" % (a, b),
                                   CONF["double_open"], (i, i + 2)))
            elif a in OPEN_CLOSE and OPEN_CLOSE[a] == b:
                out.append(Anomaly(i, "空括号对「%s%s」" % (a, b),
                                   CONF["empty_pair"], (i, i + 2)))
        return out

    # ---- R6：列表编号不递增 -------------------------------------------
    def _list_order(self, chars: list, mask: list) -> list:
        line = "".join(chars)
        marks = []
        for m in _LIST_RE.finditer(line):
            pos = m.start(2)
            if mask[pos] if pos < len(mask) else False:
                continue
            marks.append((pos, _cjk_num(m.group(1))))
        if len(marks) < 2:
            return []
        vals = [v for _p, v in marks]
        inc = all(vals[k + 1] == vals[k] + 1 for k in range(len(vals) - 1))
        if inc:
            return []
        return [Anomaly(p, "列表编号不递增（%s）" % ",".join(str(v) for v in vals),
                        CONF["list_order"], (p, p + 1)) for p, _v in marks]

    # ---- R7：可疑结构符号 ---------------------------------------------
    def _symbols(self, chars: list, mask: list) -> list:
        out: list = []
        for i, ch in enumerate(chars):
            if mask[i] or ch in OPEN_CLOSE or ch in CLOSE_OPEN:
                continue
            if ch in SYMBOL_SAFE:
                continue
            if _is_cjk_symbol(ch) or (0x3000 <= ord(ch) <= 0x303F):
                out.append(Anomaly(i, "可疑结构符号「%s」(U+%04X)" % (ch, ord(ch)),
                                   CONF["struct_symbol"], (i, i + 1)))
        return out
