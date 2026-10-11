# -*- coding: utf-8 -*-
"""V3 清洗引擎：锚点 → 片段候选 → 合理性打分选优。

候选含"什么都不删"，取分最高者；只有高出 min_margin 才提交。
score(text) = text_score(窗口) / 窗口字数（每字平均，长度中立）。
"""
from __future__ import annotations

import itertools
import math
import os
import unicodedata
from dataclasses import dataclass, field

from . import charclass as cc
from .structure import StructureDetector
from .v2engine import (DEFAULT_PROTECTED, HOMO_ALL, _is_cjk_symbol, _is_kana,
                       load_default_protected)
from .v2lm import BOS, EOS, LanguageModel

STRONG_PAIR = 10


@dataclass
class V3Config:
    # --- 锚点 ---
    anchor_min: float = 1.5        # 可疑度达到此值才算锚点
    max_anchors: int = 14          # 超过此数就按可疑度截断
    bridge: int = 3                # 锚点间距 <= bridge 归入同一污染簇
    # --- 候选片段 ---
    expand_left: int = 6           # 向左扩展上限
    expand_right: int = 6          # 向右扩展上限
    max_candidates: int = 400      # 单簇候选上限
    max_rounds: int = 3            # 残留重扫轮数（1 = 只跑一轮，无迭代）
    # --- 结构异常锚点（StructureDetector）---
    use_structure: bool = True     # 是否把结构异常并入锚点集合
    struct_span_cap: int = 40      # 结构异常组窗口的最大宽度（防止整行候选）
    # --- 候选生成器 v2（beam search）---
    candidate_mode: str = "v1"     # "v1" = 现有逐组 argmax；"beam_v2" = 束搜索
    local_top_k: int = 5           # 每个 group 保留的局部候选数
    beam_size: int = 20            # 束宽
    local_inner_k: int = 8         # 组内先按窗口分取前 k 个"段"，再做组内联合
    local_union_max: int = 3       # 组内最多联合几个段
    local_composite: int = 0       # 组内贪心复合候选的最大段数（0=关闭）
    merge_gap: int = 4             # 相邻 group 间距 <= 此值才合并
    merge_max: int = 2             # 一次最多合并几个 group
    merge_span_cap: int = 32       # 合并后窗口宽度上限
    # --- B: 污染区域 / span coherence evidence ---
    # coherence_bonus = β × Σ_{j∈删除集} den(j)，den(j) = j 的 ±W 邻域内被标记位置占比
    # （标记 = 统计锚点 ∪ 结构异常 span）。默认 0.0 = 关闭。
    coherence_beta: float = 0.0
    coherence_win: int = 10
    # --- B': anchor coverage ---
    # anchor_event = 相邻/重叠的异常证据点合并而成。
    # anchor_bonus = β × 被候选有效覆盖的 event 数，每个 event 只计一次。
    anchor_beta: float = 0.0        # 0 = 关闭
    event_gap: int = 3              # 证据点间距 <= 此值则并入同一 event
    cover_ratio: float = 1.0        # 覆盖 event 内多大比例的证点才算"有效覆盖"
    # --- 决策 ---
    min_margin: float = 0.02       # 比"什么都不删"至少好这么多才动手（每字平均口径）
    score_mode: str = "avg"        # avg | sum | lr
    # --- 似然比模式（score_mode="lr"）的参数 ---
    # 判为"广告插入"当且仅当：
    #   logP_LM(删后) - logP_LM(原) + logP_ad(c) + log(p/(1-p)) > 0
    # logP_ad(c) 为广告字符模型的平均对数概率，p 是该位置为插入的先验概率。
    junk_logp: float = -5.3
    insert_prior: float = 0.02
    # 「接合质量」门：删掉一段后左右直接接上的三元组 logp(L2,L,R) 不得低于此值。
    # 默认 -99 = 关闭。
    join_min: float = -99.0
    # --- 保护 ---
    whitelist_chars: frozenset = field(default_factory=lambda: DEFAULT_PROTECTED)
    whitelist_words: tuple = ()    # 多字人名/专名，如 ("常榕", "樱羽艾玛")
    # --- 安全 ---
    kana_safe: bool = True         # 假名当正文，永不作锚点


@dataclass
class V3Candidate:
    spans: tuple                   # ((lo,hi), ...) 半开区间
    removed: tuple                 # 被删位置
    score: float = 0.0
    delta: float = 0.0             # 相对"什么都不删"的每字平均增益


@dataclass
class V3LineResult:
    original: str
    cleaned: str
    removed: tuple = ()            # 最终删掉的位置
    anchors: tuple = ()            # ((pos, char, susp), ...)
    candidates: int = 0
    best: float = 0.0
    baseline: float = 0.0          # "什么都不删"的分数
    rounds: tuple = ()             # 每轮删掉的字数
    struct: tuple = ()             # 各轮 StructureDetector 发现的异常
    seen: tuple = ()               # 评估过的候选移除集（仅 trace 模式下收集）
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.cleaned != self.original


class V3Engine:
    def __init__(self, lm: LanguageModel, cfg: V3Config | None = None) -> None:
        self.lm = lm
        self.cfg = cfg or V3Config()
        self.struct = StructureDetector()
        self._wl_pos: list[bool] = []      # 白名单掩码（按行）
        self._den_key = None               # 单条缓存：coherence 密度
        self._den_chars = None
        self._den: list = []
        self._ev_key = None                # 单条缓存：anchor_event
        self._ev_chars = None
        self._ev: tuple = ()

    # ---- B: 污染区域密度 ------------------------------------------------
    def _den_for(self, chars: list) -> list:
        """位置 j 的污染区域密度 den(j) = ±W 邻域内被标记位置的占比。

        标记 = 统计锚点位置 + 结构异常 span 覆盖范围。返回长度 n 的列表。
        """
        key = (id(chars), len(chars))
        if self._den_key == key and self._den_chars is not None:
            return self._den
        n = len(chars)
        mask = [False] * n
        marked = [False] * n
        for it in self.anchors(chars, mask):
            marked[it[0]] = True
        if self.cfg.use_structure:
            for a in self.struct.detect(chars, mask):
                lo = max(0, a.span[0])
                hi = min(n, a.span[1])
                for j in range(lo, hi):
                    marked[j] = True
        pre = [0] * (n + 1)
        for j in range(n):
            pre[j + 1] = pre[j] + (1 if marked[j] else 0)
        W = max(0, self.cfg.coherence_win)
        den = [0.0] * n
        for j in range(n):
            lo, hi = max(0, j - W), min(n, j + W + 1)
            den[j] = (pre[hi] - pre[lo]) / (hi - lo)
        self._den_key = key
        self._den_chars = chars
        self._den = den
        return den

    def coherence_bonus(self, chars: list, removed) -> float:
        """B 的贡献值（正数 = 降低删除成本）。"""
        b = self.cfg.coherence_beta
        if not b or not removed:
            return 0.0
        den = self._den_for(chars)
        return b * sum(den[j] for j in removed)

    # ---- B': anchor coverage -------------------------------------------
    def _events_for(self, chars: list) -> tuple:
        """把相邻/重叠的异常证据点合并成 anchor_event。

        证据点 = 统计锚点 + 结构异常主位置。返回 (证点元组, ...)。
        """
        key = (id(chars), len(chars))
        if self._ev_key == key and self._ev is not None:
            return self._ev
        n = len(chars)
        mask = [False] * n
        pts = set()
        for it in self.anchors(chars, mask):
            pts.add(it[0])
        if self.cfg.use_structure:
            for a in self.struct.detect(chars, mask):
                pts.add(a.pos)
        ev: list = []
        if pts:
            ps = sorted(pts)
            cur = [ps[0]]
            for p in ps[1:]:
                if p - cur[-1] <= self.cfg.event_gap:
                    cur.append(p)
                else:
                    ev.append(tuple(cur))
                    cur = [p]
            ev.append(tuple(cur))
        self._ev_key = key
        self._ev_chars = chars
        self._ev = tuple(ev)
        return self._ev

    def covered_events(self, chars: list, removed) -> int:
        """候选有效覆盖的 event 数（每个 event 至多计一次）。"""
        if not removed:
            return 0
        rm = set(removed)
        n = 0
        for ev in self._events_for(chars):
            need = max(1, int(math.ceil(self.cfg.cover_ratio * len(ev) - 1e-9)))
            if sum(1 for p in ev if p in rm) >= need:
                n += 1
        return n

    def anchor_bonus(self, chars: list, removed) -> float:
        """B' 的贡献值 = β × 被覆盖的 event 数。"""
        b = self.cfg.anchor_beta
        if not b:
            return 0.0
        return b * self.covered_events(chars, removed)

    # ------------------------------------------------------------------
    # 1) 可疑度 / 锚点
    # ------------------------------------------------------------------
    def _protect_mask(self, chars: list) -> list:
        """多字白名单 + 单字保护表 → 逐位是否受保护。"""
        cfg = self.cfg
        n = len(chars)
        mask = [c in cfg.whitelist_chars for c in chars]
        line = "".join(chars)
        for w in cfg.whitelist_words:
            if not w:
                continue
            start = 0
            while True:
                k = line.find(w, start)
                if k < 0:
                    break
                for j in range(k, min(n, k + len(w))):
                    mask[j] = True
                start = k + 1
        return mask

    def _homo_density(self, chars: list, i: int, win: int = 15) -> int:
        lo, hi = max(0, i - win), min(len(chars), i + win + 1)
        return sum(1 for j in range(lo, hi) if chars[j] in HOMO_ALL)

    def suspicion(self, chars: list, mask: list, i: int) -> float:
        """位置 i 的"可疑度"（0 = 完全正常）。"""
        cfg = self.cfg
        n = len(chars)
        ch = chars[i]
        if mask[i] or ch in "\r\n\t" or cc.is_space(ch):
            return 0.0
        cat = unicodedata.category(ch)
        if cc.is_invisible(ch) or cat in ("Cf", "Cc", "Co", "Cs", "Cn"):
            return 10.0
        if cfg.kana_safe and _is_kana(ch):
            return 0.0
        L = chars[i - 1] if i else BOS
        R = chars[i + 1] if i + 1 < n else EOS
        if cc.is_space(L):
            L = BOS
        if cc.is_space(R):
            R = EOS
        lm = self.lm
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
                s += 0.4 * min(self._homo_density(chars, i) / 4.0, 2.0)
            if cc.in_suspect_block(ch):
                s += 1.0
        elif ch.isascii() and ch.isalnum():
            f = lm.uni.get(ch, 0)
            if f <= 400 and (lm.bi.get(L + ch, 0) <= 1 or lm.bi.get(ch + R, 0) <= 1):
                s += 1.2 + 0.4 * min(self._homo_density(chars, i) / 4.0, 2.0)
        else:
            # 标点/装饰：只有 CJK 符号区的非常规符号才算轻微可疑
            if _is_cjk_symbol(ch):
                s += 0.6
        return s

    def anchors(self, chars: list, mask: list):
        sc = [(i, chars[i], self.suspicion(chars, mask, i)) for i in range(len(chars))]
        a = [x for x in sc if x[2] >= self.cfg.anchor_min]
        a.sort(key=lambda x: -x[2])
        a = a[: self.cfg.max_anchors]
        a.sort(key=lambda x: x[0])
        return a

    # ------------------------------------------------------------------
    # 2) 候选生成
    # ------------------------------------------------------------------
    def _cluster(self, anchors) -> list:
        cfg = self.cfg
        groups: list[list] = []
        for it in anchors:
            if groups and it[0] - groups[-1][-1][0] <= cfg.bridge:
                groups[-1].append(it)
            else:
                groups.append([it])
        return groups

    def candidates(self, chars: list, mask: list, group,
                   w_lo: int | None = None, w_hi: int | None = None) -> list:
        """为一组锚点生成候选（片段集合）。

        `group` 可含统计锚点或结构异常锚点，两者走同一生成与打分流程。
        `w_lo/w_hi` 允许调用方指定窗口。返回 {(lo,hi): 位置元组}。
        """
        cfg = self.cfg
        n = len(chars)
        pos = [it[0] for it in group]
        if w_lo is None:
            w_lo = max(0, pos[0] - cfg.expand_left)
        if w_hi is None:
            w_hi = min(n, pos[-1] + cfg.expand_right + 1)
        out = {}

        def add(lo: int, hi: int) -> None:
            lo = max(w_lo, lo)
            hi = min(w_hi, hi)
            if hi <= lo:
                return
            for j in range(lo, hi):
                if mask[j] or chars[j] in "\r\n\t":
                    return                       # 片段不许切进受保护的字
            key = (lo, hi)
            if key not in out:
                out[key] = tuple(range(lo, hi))

        for a in pos:
            for i in range(cfg.expand_left + 1):
                for j in range(cfg.expand_right + 1):
                    add(a - i, a + j + 1)
            # 只删锚点自身
            add(a, a + 1)
        # 组内所有锚点各删自身（可能不连续）
        only = tuple(a for a in pos)
        if len(only) >= 1:
            key = ("multi", only)
            out[key] = only
        # 整窗（首锚点扩展到末锚点，含扩展）
        add(w_lo, w_hi)

        items = list(out.items())
        if len(items) > cfg.max_candidates:
            items = items[: cfg.max_candidates]
        return [v for _k, v in items]

    # ------------------------------------------------------------------
    # 2b) 分组：统计锚点按 bridge 聚类；结构异常各成一组，窗口=受影响范围
    # ------------------------------------------------------------------
    def _build_groups(self, chars: list, mask: list, stat_anc, anomalies) -> list:
        """返回 [(items, w_lo, w_hi, kind), ...]。"""
        cfg = self.cfg
        n = len(chars)
        groups: list = []
        for g in self._cluster(stat_anc):
            pos = [it[0] for it in g]
            w_lo = max(0, pos[0] - cfg.expand_left)
            w_hi = min(n, pos[-1] + cfg.expand_right + 1)
            groups.append((tuple(g), w_lo, w_hi, "stat"))
        for a in anomalies or ():
            if mask[a.pos] or chars[a.pos] in "\r\n\t":
                continue
            s_lo, s_hi = a.span
            w_lo = max(0, s_lo - cfg.expand_left)
            w_hi = min(n, s_hi + cfg.expand_right)
            if w_hi - w_lo > cfg.struct_span_cap:
                c = a.pos
                w_lo = max(0, c - cfg.struct_span_cap // 2)
                w_hi = min(n, w_lo + cfg.struct_span_cap)
            if w_hi <= w_lo:
                continue
            groups.append((((a.pos, chars[a.pos], a.conf),), w_lo, w_hi,
                           "struct:" + a.kind.split("「")[0]))
        return groups

    # ------------------------------------------------------------------
    # 3) 打分
    # ------------------------------------------------------------------
    def _feasible(self, chars: list, removed) -> bool:
        """接合质量门：每一段连续删除，其左右直接接上的三元组必须够像中文。"""
        cfg = self.cfg
        if cfg.join_min <= -90.0:
            return True
        n = len(chars)
        rs = sorted(removed)
        k = 0
        while k < len(rs):
            lo = rs[k]
            hi = lo + 1
            while k + 1 < len(rs) and rs[k + 1] == hi:
                k += 1
                hi += 1
            k += 1
            L = chars[lo - 1] if lo >= 1 else BOS
            L2 = chars[lo - 2] if lo >= 2 else BOS
            R = chars[hi] if hi < n else EOS
            if self.lm.logp(L2, L, R) < cfg.join_min:
                return False
        return True

    def _deletion_penalty(self) -> float:
        """每删一个字的固定代价 = logP_ad + log(p/(1-p))（似然比门槛）。"""
        cfg = self.cfg
        p = min(max(cfg.insert_prior, 1e-6), 1 - 1e-6)
        return cfg.junk_logp + math.log(p / (1.0 - p))

    def _score_window(self, chars: list, w_lo: int, w_hi: int,
                      removed: tuple) -> float:
        removed = set(removed)
        a, b = BOS, BOS
        tot = 0.0
        cnt = 0
        for j in range(w_lo, w_hi):
            if j in removed:
                continue
            c = chars[j]
            tot += self.lm.logp(a, b, c)
            a, b = b, c
            cnt += 1
        tot += self.lm.logp(a, b, EOS)
        if self.cfg.coherence_beta:
            r = [j for j in removed if w_lo <= j < w_hi]
            if r:
                den = self._den_for(chars)
                tot += self.cfg.coherence_beta * sum(den[j] for j in r)
        if self.cfg.anchor_beta:
            r = [j for j in removed if w_lo <= j < w_hi]
            if r:
                tot += self.cfg.anchor_beta * self.covered_events(chars, r)
        if self.cfg.score_mode == "sum":
            return tot
        if self.cfg.score_mode == "lr":
            # 每个被删的字都要付出"插入先验 + 广告模型"的代价
            k = sum(1 for j in removed if w_lo <= j < w_hi)
            return tot + k * self._deletion_penalty()
        return tot / max(1, cnt)

    # ------------------------------------------------------------------
    # 2c) 候选生成器 v2：group merge + 组内局部候选 + 跨 group 束搜索
    # ------------------------------------------------------------------
    def _merge_groups(self, groups: list) -> list:
        """相邻/距离较近的 group 合并，使一个污染事件可由多个相距不远的 span 组成。

        一次最多 merge_max 个 group，合并后窗口不超过 merge_span_cap。
        """
        cfg = self.cfg
        if cfg.merge_max < 2 or cfg.merge_gap < 0 or not groups:
            return groups
        out: list = []
        i = 0
        while i < len(groups):
            items, w_lo, w_hi, kind = groups[i]
            j = i + 1
            cnt = 1
            while j < len(groups) and cnt < cfg.merge_max:
                it2, lo2, hi2, k2 = groups[j]
                if lo2 - w_hi > cfg.merge_gap:
                    break
                if max(w_hi, hi2) - min(w_lo, lo2) > cfg.merge_span_cap:
                    break
                items = tuple(items) + tuple(it2)
                w_lo = min(w_lo, lo2)
                w_hi = max(w_hi, hi2)
                kind = "merge(%s+%s)" % (kind, k2)
                cnt += 1
                j += 1
            out.append((items, w_lo, w_hi, kind))
            i = j
        return out

    def _local_candidates(self, chars: list, mask: list, group,
                          w_lo: int, w_hi: int) -> list:
        """一个 group 的局部候选（保留"不删"）。

        段来源：不删 / 窗口内单字删除 / 锚点驱动的连续 span。
        组内联合不超过 local_union_max 个互不相交的段，最后按窗口分取 local_top_k。
        """
        cfg = self.cfg
        raw: list = [()]
        for j in range(w_lo, w_hi):
            if mask[j] or chars[j] in "\r\n\t":
                continue
            raw.append((j,))                       # 单字删除
        for rm in self.candidates(chars, mask, group, w_lo, w_hi):
            raw.append(tuple(sorted(rm)))          # 连续 span / V1 已有候选

        seen = set()
        pool: list = []
        for it in raw:
            k = frozenset(it)
            if k in seen:
                continue
            seen.add(k)
            pool.append(it)

        cache: dict = {}

        def wsc(it) -> float:
            k = tuple(sorted(it))
            v = cache.get(k)
            if v is None:
                v = self._score_window(chars, w_lo, w_hi, k)
                cache[k] = v
            return v

        scored = []
        for it in pool:
            if it and not self._feasible(chars, it):
                continue
            scored.append((wsc(it), it))
        scored.sort(key=lambda x: -x[0])
        head = [it for _s, it in scored[: max(1, cfg.local_inner_k)]]

        if cfg.local_union_max >= 2 and len(head) >= 2:
            for r in range(2, cfg.local_union_max + 1):
                for combo in itertools.combinations(head, r):
                    u: list = []
                    ok = True
                    for seg in combo:
                        for j in seg:
                            if j in u:
                                ok = False
                                break
                        if not ok:
                            break
                        u.extend(seg)
                    if not ok:
                        continue
                    us = tuple(sorted(u))
                    if us in seen:
                        continue
                    seen.add(us)
                    if not self._feasible(chars, us):
                        continue
                    scored.append((wsc(us), us))

        # 组内贪心复合：沿窗口分逐步吸收互不相交的段。
        # 补 local_union_max 固定阶数联合在分散 span 上的表达不足。
        if cfg.local_composite >= 1 and head:
            comp: tuple = ()
            cur = wsc(())
            for _step in range(cfg.local_composite):
                cs = set(comp)
                best_seg, best_sc = None, cur
                for seg in head:
                    if cs & set(seg):
                        continue
                    c = tuple(sorted(cs | set(seg)))
                    s2 = wsc(c)
                    if s2 > best_sc + 1e-9:
                        best_sc, best_seg = s2, c
                if best_seg is None:
                    break
                comp, cur = best_seg, best_sc
                if comp not in seen:
                    seen.add(comp)
                if self._feasible(chars, comp):
                    scored.append((cur, comp))

        scored.sort(key=lambda x: -x[0])
        top = [it for _s, it in scored[: max(1, cfg.local_top_k)]]
        if () not in top:
            top.insert(0, ())
        return top

    def _beam_pass(self, chars: list, mask: list, groups: list, trace=None):
        """跨 group 束搜索：每加入一个 group 就做 global scoring，只留 beam_size 个。"""
        cfg = self.cfg
        cand_total = 0
        seen: set = {()}
        beam = [(self._line_score(chars, ()), ())]
        for gi, (g, w_lo, w_hi, kind) in enumerate(groups):
            locals_ = self._local_candidates(chars, mask, g, w_lo, w_hi)
            cand_total += len(locals_)
            nxt: dict = {}
            for _bs, b in beam:
                bs = set(b)
                for lc in locals_:
                    if bs and lc and (bs & set(lc)):
                        continue
                    c = tuple(sorted(bs | set(lc)))
                    if c not in nxt:
                        nxt[c] = self._line_score(chars, c)
                        seen.add(c)
            if not nxt:
                continue
            ranked = sorted(nxt.items(), key=lambda kv: -kv[1])[: cfg.beam_size]
            beam = [(s, c) for c, s in ranked]
            if trace is not None:
                trace.append({"stage": gi, "kind": kind, "w": [w_lo, w_hi],
                              "locals": len(locals_), "evaluated": len(nxt),
                              "beam": len(beam), "best": round(beam[0][0], 3)})
        best_s, best = max(beam, key=lambda x: x[0])
        return set(best), cand_total, seen, best_s

    # ------------------------------------------------------------------
    # 4) 单轮：给定"视图文本"及其锚点，返回视图内要删的位置
    # ------------------------------------------------------------------
    def _pass(self, chars: list, mask: list, anc, anomalies=None,
              trace=None) -> tuple:
        cfg = self.cfg
        n = len(chars)
        groups = self._build_groups(chars, mask, anc, anomalies)
        seen: set = {()}

        if cfg.candidate_mode == "beam_v2":
            groups = self._merge_groups(groups)
            removed, cand_total, seen, _bs = self._beam_pass(
                chars, mask, groups, trace)
        else:
            removed = set()
            cand_total = 0
            for gi, (g, w_lo, w_hi, kind) in enumerate(groups):
                base = self._score_window(chars, w_lo, w_hi, ())
                best_sc, best_spans = base, None
                pool: list = []
                for rm in self.candidates(chars, mask, g, w_lo, w_hi):
                    cand_total += 1
                    if not self._feasible(chars, rm):
                        continue
                    sc = self._score_window(chars, w_lo, w_hi, rm)
                    pool.append((sc, rm))
                    seen.add(tuple(sorted(rm)))
                    if sc > best_sc:
                        best_sc, best_spans = sc, rm
                if trace is not None:
                    pool.sort(key=lambda x: -x[0])
                    trace.append({"group": gi, "kind": kind, "lo": w_lo,
                                  "hi": w_hi, "baseline": base, "pool": pool})
                if best_spans is not None:
                    removed.update(best_spans)
            seen.add(tuple(sorted(removed)))

        # 本轮把关：整行口径与"什么都不删"比一次
        base_line = self._line_score(chars, ())
        final = self._line_score(chars, tuple(sorted(removed)))
        note = ""
        if not self._feasible(chars, removed):
            removed = set()
            note = "接合质量不足"
        elif final - base_line < cfg.min_margin:
            removed = set()
            note = "增益不足(Δ%.4f<%.4f)" % (final - base_line, cfg.min_margin)
        return removed, cand_total, note, seen

    # ------------------------------------------------------------------
    # 5) 主流程：多轮残留重扫
    # ------------------------------------------------------------------
    def clean_line(self, line: str, max_rounds: int | None = None,
                   trace: list | None = None) -> V3LineResult:
        """锚点 → 片段候选 → 合理性选优，反复重扫残留，直到不再改动。

        锚点集合 = 统计异常锚点 ∪ 结构异常锚点，走同一条候选生成 / 打分管线。
        返回 V3LineResult。
        """
        cfg = self.cfg
        if not line:
            return V3LineResult(line, line)
        chars0 = list(line)
        n = len(chars0)
        alive = list(range(n))
        rounds = cfg.max_rounds if max_rounds is None else max_rounds
        rounds = max(1, rounds)

        all_anchors: list = []
        all_struct: list = []
        all_seen: set = set()
        cand_total = 0
        notes: list = []
        per_round: list = []

        for rd in range(rounds):
            view = [chars0[i] for i in alive]
            mask = self._protect_mask(view)
            anc = self.anchors(view, mask)
            anomalies = self.struct.detect(view, mask) if cfg.use_structure else []
            for a in anomalies:                     # 视图坐标 → 原文坐标
                d = a.as_dict()
                d["round"] = rd + 1
                d["pos"] = alive[a.pos]
                lo_i = min(a.span[0], len(alive) - 1)
                hi_i = min(a.span[1], len(alive)) - 1
                d["span"] = [alive[lo_i], alive[hi_i] + 1] if hi_i >= 0 \
                    else [alive[a.pos], alive[a.pos] + 1]
                all_struct.append(d)
            if not anc and not anomalies:
                notes.append("第%d轮无锚点" % (rd + 1))
                break
            all_anchors += [(alive[p], c, round(s, 2)) for p, c, s in anc]
            rem, cand, note, seen_r = self._pass(view, mask, anc, anomalies, trace)
            cand_total += cand
            if trace is not None:
                for k in seen_r:                 # 视图坐标 → 原文坐标
                    all_seen.add(tuple(sorted(alive[j] for j in k)))
            per_round.append(len(rem))
            if note:
                notes.append("第%d轮 %s" % (rd + 1, note))
            if not rem:
                break
            dead = {alive[k] for k in rem}
            alive = [i for i in alive if i not in dead]

        dead_set = set(range(n)) - set(alive)
        removed = tuple(sorted(dead_set))
        cleaned = "".join(chars0[i] for i in alive)
        bl = self._line_score(chars0, ())
        bst = self._line_score(chars0, removed)
        if not removed:
            notes.append("无改动")
        return V3LineResult(
            original=line, cleaned=cleaned, removed=removed,
            anchors=tuple(all_anchors), candidates=cand_total,
            best=round(bst, 4), baseline=round(bl, 4),
            rounds=tuple(per_round), struct=tuple(all_struct),
            seen=tuple(sorted(all_seen)),
            note="; ".join(notes))

    def _line_score(self, chars: list, removed: tuple) -> float:
        return self._score_window(chars, 0, len(chars), removed)


# ---------------------------------------------------------------------------
# 整文
# ---------------------------------------------------------------------------
@dataclass
class V3TextResult:
    original: str
    cleaned: str = ""
    changed_lines: int = 0
    removed_chars: int = 0
    total_candidates: int = 0
    edits: list = field(default_factory=list)
    samples: list = field(default_factory=list)


def clean_text_v3(text: str, engine: V3Engine, sample_limit: int = 60) -> V3TextResult:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    res = V3TextResult(original=text)
    for i, line in enumerate(lines):
        if not line:
            out.append("")
            continue
        r = engine.clean_line(line)
        out.append(r.cleaned)
        res.total_candidates += r.candidates
        if r.changed:
            res.changed_lines += 1
            res.removed_chars += len(r.removed)
            if len(res.samples) < sample_limit:
                res.samples.append({
                    "line": i + 1,
                    "before": r.original,
                    "after": r.cleaned,
                    "removed": "".join(r.original[j] for j in r.removed),
                    "anchors": "".join(c for _p, c, _s in r.anchors),
                    "struct": [d for d in r.struct],
                    "rounds": list(r.rounds),
                    "baseline": r.baseline,
                    "best": r.best,
                })
    res.cleaned = "\n".join(out)
    return res


DEFAULT_LM_CANDIDATES = (
    ".adclean-lm-c1.json",      # 语料去污染版
    ".adclean-lm.json",
)


def default_lm_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in DEFAULT_LM_CANDIDATES:
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    return os.path.join(here, ".adclean-lm.json")
