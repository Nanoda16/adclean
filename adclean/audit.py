# -*- coding: utf-8 -*-
"""adclean.audit —— Audit 层：只读、高召回地扫描整本书并输出异常区域（AuditEvent）。

只复用 V3Engine.suspicion(chars, mask, i) 与 StructureDetector.detect(chars, mask)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import charclass as cc
from .v2engine import HOMO_ALL, _is_cjk_symbol, _is_kana
from .v3engine import V3Engine

STRONG_PAIR = 10
BOS = "\u0001"
EOS = "\u0002"


def statistical_conf(lm, chars: list, mask: list, i: int,
                     kana_safe: bool = True, win: int = 15) -> float:
    """位置 i 的统计异常置信度；只用于 Audit 出证据，不参与删除决策。"""
    n = len(chars)
    ch = chars[i]
    if mask[i] or ch in "\r\n\t" or cc.is_space(ch):
        return 0.0
    cat = unicodedata.category(ch)
    if cc.is_invisible(ch) or cat in ("Cf", "Cc", "Co", "Cs", "Cn"):
        return 10.0
    if kana_safe and _is_kana(ch):
        return 0.0
    L = chars[i - 1] if i else BOS
    R = chars[i + 1] if i + 1 < n else EOS
    if cc.is_space(L):
        L = BOS
    if cc.is_space(R):
        R = EOS

    def homo_density() -> int:
        lo, hi = max(0, i - win), min(n, i + win + 1)
        return sum(1 for j in range(lo, hi) if chars[j] in HOMO_ALL)

    s = 0.0
    if cc.is_cjk(ch):
        f = lm.uni.get(ch, 0)
        if f == 0:
            s += 3.0
        elif f <= 8:
            s += 2.0
        elif f <= 60:
            s += 0.8
        elif f <= 400:
            s += 0.3
        lpr = lm.bi.get(L + ch, 0)
        rpr = lm.bi.get(ch + R, 0)
        m = max(lpr, rpr)
        if m <= 1:
            s += 1.5
        elif m < STRONG_PAIR:
            s += 0.6
        if ch in HOMO_ALL:
            s += 0.4 * min(homo_density() / 4.0, 2.0)
        if cc.in_suspect_block(ch):
            s += 1.0
    elif ch.isascii() and ch.isalnum():
        f = lm.uni.get(ch, 0)
        if f <= 400 and (lm.bi.get(L + ch, 0) <= 1 or lm.bi.get(ch + R, 0) <= 1):
            s += 1.2 + 0.4 * min(homo_density() / 4.0, 2.0)
    else:
        if _is_cjk_symbol(ch):
            s += 0.6
    return s

# 证据类型
EV_INVISIBLE = "invisible_char"
EV_STATISTICAL = "statistical_anomaly"
EV_STRUCTURE = "structure_anomaly"
EV_HOMOPHONE = "homophone_density"
EV_DENSITY = "local_density"
# 表意字符但不在任何已知 CJK 区（CJK 扩展 B–H 等）。
EV_COVERAGE_GAP = "coverage_gap"
# 引擎（Auto Repair）自己提出的删除；单独不足以升到 HIGH。
EV_ENGINE_PROPOSAL = "engine_proposal"

SEV_HIGH = "HIGH"
SEV_MEDIUM = "MEDIUM"
SEV_LOW = "LOW"

# 已知合法、不应按"覆盖率缺口"处理的非 CJK 表意/音节区
_LEGIT_LO_RANGES = (
    (0x3040, 0x30FF),    # 平假名/片假名
    (0x31F0, 0x31FF),    # 片假名音扩展
    (0xFF66, 0xFF9D),    # 半角片假名
    (0x1100, 0x11FF),    # 谚文字母
    (0x3130, 0x318F),    # 谚文兼容字母
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),    # 谚文音节
    (0x0E00, 0x0E7F),    # 泰文
    (0x0E80, 0x0EFF),    # 老挝文
    (0x1000, 0x109F),    # 缅甸文
    (0x1780, 0x17FF),    # 高棉文
)


def is_coverage_gap(ch: str) -> bool:
    """是否为"表意/字母字符，但落在所有已知 CJK 区之外"。"""
    if ch.isascii():
        return False
    o = ord(ch)
    if o < 0x0080:
        return False
    if cc.is_cjk(ch) or cc.is_cjk_punct(ch) or cc.is_space(ch) \
            or cc.is_invisible(ch):
        return False
    for lo, hi in _LEGIT_LO_RANGES:
        if lo <= o <= hi:
            return False
    return unicodedata.category(ch).startswith("L")

def strong_positions_of(line: str) -> list:
    """一行内的强证据位置（行内下标）：不可见/控制字符、非 BMP 表意字符。"""
    return [i for i, ch in enumerate(line)
            if cc.is_invisible(ch) or is_coverage_gap(ch)]


_CHAPTER_RE = re.compile(    r"^\s*(第[零一二三四五六七八九十百千两0-9]{1,7}[章节回卷篇]"
    r"|Chapter\s+\d+|CHAPTER\s+\d+|楔子|序章|序言|尾声|后记|番外)"
)


@dataclass
class AuditConfig:
    # --- 证据收集（高召回）---
    min_conf: float = 1.0          # 低于修复引擎的 anchor_min(1.5)
    max_evidence_per_line: int = 0  # 0 = 不截断
    # --- 区域合并 ---
    merge_gap: int = 8             # 同一行内证据点间距 <= 此值 → 同一区域
    pad: int = 6                   # 区域向两侧扩展的字数
    merge_lines: bool = True       # 相邻行都有异常时是否并成一个事件
    max_event_chars: int = 120     # 跨行合并后的事件长度上限
    max_event_lines: int = 4       # 跨行合并后的事件行数上限
    # --- 严重度判据 ---
    high_conf: float = 4.0         # 单个证据达到此置信度 → HIGH
    med_conf: float = 2.5          # 单个证据达到此置信度 → 至少 MEDIUM
    high_anchors: int = 4          # 证据点数量门槛（配合密度）
    high_density: float = 0.5      # 证据密度门槛
    struct_conf: float = 2.5       # 结构异常达此置信度即视为"确有结构问题"
    struct_high_min_evidence: int = 2
    # --- 覆盖率缺口 ---
    gap_conf: float = 4.5          # 不在已知 CJK 区的表意字符：高置信（通常全书 0 次）
    # --- 与 Auto Repair 对齐 ---
    # 本模块永远只是旁路：proposals 只用于对齐与归档，不是门禁。
    engine_scan: bool = True       # 未提供 proposals 时，扫描时顺带跑一遍引擎决策
    proposal_conf: float = 3.0     # 基线分；随 margin 略增，但**上限 < high_conf**
    proposal_conf_cap: float = 3.9
    # --- 同音密度（广告群号的数字近音替代字）---
    # 只统计全书词频极低的同音字，且得分上限 < high_conf（单独不足以升到 HIGH）。
    homophone_max_freq: int = 2000     # 语料词频超过此值的同音字视为正常用字
    homophone_min_count: int = 4       # 至少几个稀见同音字才算广告特征
    homophone_conf_cap: float = 3.5    # 必须 < high_conf


@dataclass
class Evidence:
    type: str
    score: float
    pos: int = 0                   # **该行内**的字符下标（line 字段标明是哪一行）
    detail: str = ""
    line: int = -1                 # 0-based 行号；跨行合并的事件里用来区分归属

    def as_dict(self) -> dict:
        d = {"type": self.type, "score": round(self.score, 2), "pos": self.pos}
        if self.line >= 0:
            d["line"] = self.line
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class AuditEvent:
    """一个异常区域 / 污染事件。"""
    book_id: str
    chapter_id: str
    line_start: int                 # 1-based，含
    line_end: int                   # 1-based，含
    start: int                      # 全书字符偏移（基于 \n 归一化后的文本）
    end: int
    text: str
    severity: str
    evidence: list = field(default_factory=list)
    n_evidence: int = 0
    n_independent: int = 0          # 不含 engine_proposal 的证据数（真正的外部印证）
    density: float = 0.0
    max_conf: float = 0.0
    has_structure: bool = False
    has_invisible: bool = False
    has_coverage_gap: bool = False
    # 以下由 pipeline 填入
    auto_repair: bool = False
    llm_review: bool = False
    candidates: list = field(default_factory=list)
    applied_candidate: str = ""
    note: str = ""

    @property
    def n_chars(self) -> int:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "chapter_id": self.chapter_id,
            "lines": [self.line_start, self.line_end],
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "n_chars": self.n_chars,
            "severity": self.severity,
            "evidence": [e.as_dict() for e in self.evidence],
            "n_evidence": self.n_evidence,
            "n_independent": self.n_independent,
            "density": round(self.density, 3),
            "max_conf": round(self.max_conf, 2),
            "has_structure": self.has_structure,
            "has_invisible": self.has_invisible,
            "has_coverage_gap": self.has_coverage_gap,
            "auto_repair": self.auto_repair,
            "llm_review": self.llm_review,
            "candidate_count": len(self.candidates),
            "applied_candidate": self.applied_candidate,
            "note": self.note,
        }


class AuditLayer:
    """高召回审查层：只读扫描，输出异常区域。"""

    def __init__(self, engine: V3Engine, cfg: AuditConfig | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or AuditConfig()
        # scan_book 填：行号 -> (引擎要删的行内位置, margin)
        self.proposals: dict = {}
        # scan_book 填：行号 -> {强证据命中的行内位置}
        self.strong_positions: dict = {}

    # ------------------------------------------------------------------
    # 逐行取证
    # ------------------------------------------------------------------
    def line_evidence(self, line: str, proposal=None) -> list:
        """收集一行内所有异常证据（不截断）。

        proposal = (removed_frozenset, margin)，由 scan_book 的引擎扫描提供。
        """
        cfg = self.cfg
        chars = list(line)
        if not chars:
            return []
        mask = self.engine._protect_mask(chars)
        out: list = []
        kana_safe = getattr(self.engine.cfg, "kana_safe", True)

        # 1) 统计异常（含不可见/控制字符，其置信度 == 10.0）
        for i, ch in enumerate(chars):
            s = statistical_conf(self.engine.lm, chars, mask, i, kana_safe)
            if s < cfg.min_conf:
                if is_coverage_gap(ch):
                    out.append(Evidence(EV_COVERAGE_GAP, cfg.gap_conf, i,
                                        "U+%04X 不在已知 CJK 区（扩展区/异体）"
                                        % ord(ch)))
                continue
            if s >= 10.0:
                out.append(Evidence(EV_INVISIBLE, s, i, "不可见/控制/格式字符"))
                continue
            detail = []
            f = self.engine.lm.uni.get(ch, 0)
            if f == 0:
                detail.append("全书未出现")
            elif f <= 8:
                detail.append("全书仅 %d 次" % f)
            if ch in cc.DIGIT_HOMOPHONE_CHARS:
                detail.append("数字同音")
            out.append(Evidence(EV_STATISTICAL, s, i, "/".join(detail)))
            if is_coverage_gap(ch):
                out.append(Evidence(EV_COVERAGE_GAP, cfg.gap_conf, i,
                                    "U+%04X 不在已知 CJK 区" % ord(ch)))

        # 2) 结构异常
        if getattr(self.engine.cfg, "use_structure", True):
            struct = getattr(self.engine, "struct", None)
            if struct is not None:
                for a in struct.detect(chars, mask):
                    out.append(Evidence(EV_STRUCTURE, a.conf, a.pos, a.kind))

        # 3) 同音字密度（区域级证据，挂在区域首个证据点上，只数稀见同音字）
        rare_homo = [c for c in chars
                     if c in cc.DIGIT_HOMOPHONE_CHARS
                     and self.engine.lm.uni.get(c, 0) <= cfg.homophone_max_freq]
        if len(rare_homo) >= cfg.homophone_min_count:
            first = min((e.pos for e in out), default=0)
            score = min(cfg.homophone_conf_cap,
                        cfg.homophone_min_count / 2.0 + len(rare_homo) / 2.0)
            out.append(Evidence(EV_HOMOPHONE, score, first,
                                "本行稀见数字同音字 %d 个：%s"
                                % (len(rare_homo), "".join(sorted(set(rare_homo))))))

        # 4) 引擎自己的提案（保证 Audit ↔ Auto Repair 对齐）
        if proposal:
            rm, margin = proposal
            if rm:
                sc = min(cfg.proposal_conf_cap,
                         cfg.proposal_conf + margin / 60.0)
                out.append(Evidence(EV_ENGINE_PROPOSAL, sc, min(rm),
                                    "Auto Repair 引擎提议删除 %d 字（margin %.1f）"
                                    % (len(rm), margin)))

        if cfg.max_evidence_per_line:
            out = out[: cfg.max_evidence_per_line]
        return out

    # ------------------------------------------------------------------
    # 行内证据 → 区域
    # ------------------------------------------------------------------
    def _regions(self, ev: list, n: int) -> list:
        """把证据点按间距合并成行内区域。"""
        cfg = self.cfg
        if not ev:
            return []
        pts = sorted({e.pos for e in ev})
        groups: list = []
        cur = [pts[0]]
        for p in pts[1:]:
            if p - cur[-1] <= cfg.merge_gap:
                cur.append(p)
            else:
                groups.append(cur)
                cur = [p]
        groups.append(cur)
        regions = []
        for g in groups:
            lo = max(0, g[0] - cfg.pad)
            hi = min(n, g[-1] + 1 + cfg.pad)
            regions.append((lo, hi, tuple(g)))
        return regions

    def _severity(self, ev: list, n_ev: int, n_chars: int) -> str:
        cfg = self.cfg
        maxc = max((e.score for e in ev), default=0.0)
        has_struct = any(e.type == EV_STRUCTURE and e.score >= cfg.struct_conf
                         for e in ev)
        has_invis = any(e.type == EV_INVISIBLE for e in ev)
        density = n_ev / max(1, n_chars)
        if has_invis or maxc >= cfg.high_conf \
                or (n_ev >= cfg.high_anchors and density >= cfg.high_density) \
                or (has_struct and n_ev >= cfg.struct_high_min_evidence):
            return SEV_HIGH
        if n_ev >= 2 or has_struct or maxc >= cfg.med_conf:
            return SEV_MEDIUM
        return SEV_LOW

    # ------------------------------------------------------------------
    # 全书扫描
    # ------------------------------------------------------------------
    def scan_book(self, text: str, book_id: str = "", progress=None,
                  proposals: dict | None = None) -> list:
        """扫描整本书。progress 可选：callable(done_lines, total_lines)。

        proposals：V3 主引擎已算好的逐行决策 {line_idx: (frozenset(位置), margin)}，
        传入时直接复用，本层不重跑引擎。
        """
        cfg = self.cfg
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        offs = []
        acc = 0
        for ln in lines:
            offs.append(acc)
            acc += len(ln) + 1

        chapter_of = []
        cur_ch = "全文"
        for ln in lines:
            m = _CHAPTER_RE.match(ln)
            if m:
                cur_ch = m.group(1)
            chapter_of.append(cur_ch)

        # 逐行取证
        self.proposals = {int(k): v for k, v in (proposals or {}).items() if v}
        self.strong_positions = {}
        reuse = proposals is not None
        per_line = []
        n_lines = len(lines)
        step = max(1, n_lines // 100)      # 约 100 次回调
        for i, ln in enumerate(lines):
            prop = self.proposals.get(i)
            if prop is None and not reuse and cfg.engine_scan and ln:
                r = self.engine.clean_line(ln)
                if r.removed:
                    chs = list(ln)
                    sc = self.engine._line_score(chs, tuple(sorted(r.removed)))
                    mg = sc - self.engine._line_score(chs, ())
                    prop = (frozenset(r.removed), mg)
                    self.proposals[i] = prop
            evs = self.line_evidence(ln, prop)
            sp = {e.pos for e in evs if e.type in (EV_INVISIBLE, EV_COVERAGE_GAP)}
            if sp:
                self.strong_positions[i] = sp
            per_line.append(evs)
            if progress and (i % step == 0 or i == n_lines - 1):
                try:
                    progress(i + 1, n_lines)
                except Exception:                            # noqa: BLE001
                    pass

        # 行内区域 → 事件
        raw: list = []
        for i, ev in enumerate(per_line):
            if not ev:
                continue
            for lo, hi, pts in self._regions(ev, len(lines[i])):
                sub = [e for e in ev if lo <= e.pos < hi]
                raw.append((i, lo, hi, sub))
        if not raw:
            return []

        # 跨行合并（相邻行都有事件时）
        events: list = []
        k = 0
        while k < len(raw):
            i, lo, hi, sub = raw[k]
            j = k
            L = 1
            while (cfg.merge_lines and j + 1 < len(raw)
                   and raw[j + 1][0] == raw[j][0] + 1
                   and L < cfg.max_event_lines):
                nxt = raw[j + 1]
                new_hi_off = (offs[nxt[0]] + nxt[2]) - (offs[i] + lo)
                if new_hi_off > cfg.max_event_chars:
                    break
                j += 1
                L += 1
                hi = nxt[2]
            # 组装
            ev_all = []
            for m in range(k, j + 1):
                for e in raw[m][3]:
                    e.line = raw[m][0]
                    ev_all.append(e)
            line_start = raw[k][0] + 1
            line_end = raw[j][0] + 1
            # 末区域用最后一行的 hi（相对那一行）
            abs_lo = offs[raw[k][0]] + raw[k][1]
            abs_hi = offs[raw[j][0]] + raw[j][2]
            body = text[abs_lo:abs_hi]
            n_chars = max(1, abs_hi - abs_lo)
            keys = {(e.type, offs[raw[k][0]] + e.pos) for e in ev_all}
            n_ev = len(keys)
            n_ind = len({x for x in keys if x[0] != EV_ENGINE_PROPOSAL})
            sev = self._severity(ev_all, n_ind, n_chars)
            events.append(AuditEvent(
                book_id=book_id,
                chapter_id=chapter_of[raw[k][0]],
                line_start=line_start, line_end=line_end,
                start=abs_lo, end=abs_hi, text=body, severity=sev,
                evidence=ev_all, n_evidence=n_ev, n_independent=n_ind,
                density=n_ind / n_chars,
                max_conf=max((e.score for e in ev_all), default=0.0),
                has_structure=any(e.type == EV_STRUCTURE for e in ev_all),
                has_invisible=any(e.type == EV_INVISIBLE for e in ev_all),
                has_coverage_gap=any(e.type == EV_COVERAGE_GAP for e in ev_all),
            ))
            k = j + 1
        return events

    # ------------------------------------------------------------------
    def stats(self, events: list) -> dict:
        by = {SEV_HIGH: 0, SEV_MEDIUM: 0, SEV_LOW: 0}
        type_count: dict = {}
        for e in events:
            by[e.severity] = by.get(e.severity, 0) + 1
            for ev in e.evidence:
                type_count[ev.type] = type_count.get(ev.type, 0) + 1
        return {"events": len(events), "by_severity": by,
                "by_evidence_type": type_count,
                "chars_covered": sum(e.n_chars for e in events)}
