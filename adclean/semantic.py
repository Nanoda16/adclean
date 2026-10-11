# -*- coding: utf-8 -*-
"""V4：按句子级语义正常性判定插入字，不使用任何字频/常用度判据。

判据是噪声信道 / MAP 解码：把一行当成「真实句子 + 被插入的字」，求使
Σ log P(c | 前两个字) − λ·(删掉的字数) 最大的保留子序列。

保留的非字频规则只有 Unicode 层面的确定性事实：不可见/控制字符无条件删除；
整行是 HTML/URL/标签的，整行按广告删除。
"""
from __future__ import annotations

import bisect
import math
import unicodedata
from dataclasses import dataclass, field

from . import charclass as cc
from .ads import ad_line_reason
from .v2lm import BOS, EOS, LanguageModel

DEFAULT_NAMES = ("常榕", "艾玛", "黑部奈叶香", "樱羽艾玛")

# 结构性广告：整行匹配则整行删
import re

_TAG_RE = re.compile(
    r'^\s*(?:</?(?:img|p|div|span|br|a|font)\b|https?://|www\.|'
    r'<\s*Book\b|\{[^}]*"book_id"\s*:)', re.I)
_URL_ONLY_RE = re.compile(r'^\s*(?:https?://|www\.)\S+\s*$', re.I)


@dataclass
class SemanticConfig:
    insert_prior: float = 0.005      # 每删一字的代价 = cost_scale * (−log(p/(1−p)) + junk)
    cost_scale: float = 2.0          # 正读+反读两份证据
    junk_logp: float = 0.0
    beam: int = 16
    rounds: int = 4
    decoder: str = "beam"            # "beam"（整句束搜索）| "rounds"（局部改写+修复轮）
    bidir: bool = True               # rounds 解码是否用正读+反读
    max_line: int = 400              # 超长行不整句解码，避免退化
    names: tuple = DEFAULT_NAMES
    delete_invisible: bool = True
    delete_tag_lines: bool = True
    # `(本章完)` 这类章节尾标记不是广告，是否删除由 delete_chapter_marker 控制
    delete_chapter_marker: bool = True
    # --- 下面两个只服务 Audit/Review 层（出证据），**不参与删除决策** ---
    use_structure: bool = True       # 审查层是否收集结构异常证据
    kana_safe: bool = True           # 假名视为正文（审查层不报）


@dataclass
class SemanticResult:
    original: str
    cleaned: str
    removed: tuple = ()
    delta: float = 0.0               # 解码后相对原文的净增益（含删除代价）
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.removed)

    @property
    def removed_text(self) -> str:
        return "".join(self.original[i] for i in self.removed)

    def as_dict(self) -> dict:
        return {
            "original": self.original,
            "cleaned": self.cleaned,
            "removed": list(self.removed),
            "removed_text": self.removed_text,
            "delta": round(self.delta, 4),
            "reason": self.reason,
        }


class SemanticCleaner:
    """句子级语义正常性判定器。输入一行，输出该行"最像正常中文"的子序列。"""

    def __init__(self, lm: LanguageModel, cfg: SemanticConfig | None = None) -> None:
        self.lm = lm
        self.cfg = cfg or SemanticConfig()
        self._penalty = self._deletion_penalty()
        self._protect = self._build_protect()

    # ---- 每删一字的代价（插入先验 + 广告似然比） -------------------------
    def _deletion_penalty(self) -> float:
        """每删一字的代价 = cost_scale * (−log(p/(1−p)) − junk_logp)，p = insert_prior。"""
        p = min(max(self.cfg.insert_prior, 1e-6), 1 - 1e-6)
        base = -self.cfg.junk_logp - math.log(p / (1.0 - p))
        return self.cfg.cost_scale * base

    def _build_protect(self) -> list[bool]:
        """返回保护名列表。"""
        return list(self.cfg.names or ())

    # ---- 确定性规则（不是字频） -----------------------------------------
    def _is_invisible(self, ch: str) -> bool:
        if cc.is_invisible(ch):
            return True
        return unicodedata.category(ch) in ("Cf", "Cc", "Co", "Cs", "Cn")

    def _protect_mask(self, chars: list[str]) -> list[bool]:
        n = len(chars)
        mask = [False] * n
        line = "".join(chars)
        for w in (self.cfg.names or ()):
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

    # ---- 整句解码 --------------------------------------------------------
    def decode(self, chars: list[str], mask: list[bool]) -> tuple[float, tuple]:
        """束搜索：最大化 Σlogp(保留) − λ·删除数。返回 (分数, 删除位置元组)。"""
        cfg = self.cfg
        lm = self.lm
        beam = cfg.beam
        pen = self._penalty
        # (a, b, k) -> (score, removed tuple)
        cur: dict[tuple, tuple[float, tuple]] = {(BOS, BOS, 0): (0.0, ())}
        for i, ch in enumerate(chars):
            nxt: dict[tuple, tuple[float, tuple]] = {}
            keep_ok = True
            del_ok = not mask[i]
            for (a, b, k), (sc, rem) in cur.items():
                if keep_ok:
                    s2 = sc + lm.logp(a, b, ch)
                    key = (b, ch, k)
                    old = nxt.get(key)
                    if old is None or s2 > old[0]:
                        nxt[key] = (s2, rem)
                if del_ok:
                    s2 = sc - pen
                    key = (a, b, k + 1)
                    old = nxt.get(key)
                    if old is None or s2 > old[0]:
                        nxt[key] = (s2, rem + (i,))
            if len(nxt) > beam:
                items = sorted(nxt.items(), key=lambda kv: -kv[1][0])[:beam]
                nxt = dict(items)
            cur = nxt
        best = None
        for (a, b, _k), (sc, rem) in cur.items():
            s = sc + lm.logp(a, b, EOS)
            if best is None or s > best[0]:
                best = (s, rem)
        assert best is not None
        return best

    # ---- 双向打分：删掉 i 之后整句是否更像中文 --------------------------
    def _logp_rev(self, x: str, y: str, z: str) -> float:
        """反读模型的 log P(z | x y)，由同一套正向计数推出：tri[z+y+x] / bi[y+x]。"""
        lm = self.lm
        uni, bi, tri, total = lm.uni, lm.bi, lm.tri, lm.total
        if total == 0:
            return -12.0
        p = 0.0
        n_xy = bi.get(y + x, 0)
        if n_xy:
            n_xyz = tri.get(z + y + x, 0)
            if n_xyz:
                p += 0.50 * (n_xyz / n_xy)
        n_y = uni.get(y, 0)
        if n_y:
            n_yz = bi.get(z + y, 0)
            if n_yz:
                p += 0.35 * (n_yz / n_y)
        n_z = uni.get(z, 0)
        if n_z:
            p += 0.15 * (n_z / total)
        return math.log(p) if p > 0 else -12.0

    def _marginal(self, p2: str, p1: str, c: str, q1: str, q2: str) -> float:
        """删掉 c（当前上下文 …p2 p1 [c] q1 q2…）的净增益。bidir=True 时正读+反读都算。"""
        lp = self.lm.logp
        g = (lp(p2, p1, q1) + lp(p1, q1, q2)
             - lp(p2, p1, c) - lp(p1, c, q1) - lp(c, q1, q2))
        if self.cfg.bidir:
            g += (self._logp_rev(q2, q1, p1) + self._logp_rev(q1, p1, p2)
                  - self._logp_rev(q2, q1, c) - self._logp_rev(q1, c, p1)
                  - self._logp_rev(c, p1, p2))
        return g

    def decode_rounds(self, chars: list[str], mask: list[bool],
                      rounds: int = 4) -> tuple[float, tuple]:
        """多轮局部改写：每轮在当前句子的上下文里判断每个字该不该删。"""
        pen = self._penalty
        n = len(chars)
        alive = [True] * n                            # 全部先保留；mask=True 是保护位

        def sym(k: int) -> str:
            return BOS if k < 0 else (EOS if k >= n else chars[k])

        def neighbors(idx: list[int], k: int):
            m = len(idx)
            p1 = idx[k - 1] if k >= 1 else -1
            p2 = idx[k - 2] if k >= 2 else -1
            q1 = idx[k + 1] if k + 1 < m else n
            q2 = idx[k + 2] if k + 2 < m else n
            return p2, p1, q1, q2

        for _rd in range(max(1, rounds)):
            # --- 删除轮：当前句子里每个字，删掉它整句是否更像中文 ---
            idx = [i for i in range(n) if alive[i]]
            if not idx:
                break
            drop = []
            for k, i in enumerate(idx):
                if mask[i]:
                    continue
                p2, p1, q1, q2 = neighbors(idx, k)
                if self._marginal(sym(p2), sym(p1), chars[i],
                                  sym(q1), sym(q2)) > pen:
                    drop.append(i)
            changed = False
            if drop:
                for i in drop:
                    alive[i] = False
                changed = True

            # --- 修复轮：把上一步误删的字放回去 ---
            idx = [i for i in range(n) if alive[i]]
            add = []
            for i in range(n):
                if alive[i]:
                    continue
                k = bisect.bisect_left(idx, i)
                p2, p1, q1, q2 = neighbors(idx, k)
                if self._marginal(sym(p2), sym(p1), chars[i],
                                  sym(q1), sym(q2)) < pen:
                    add.append(i)
            if add:
                for i in add:
                    alive[i] = True
                changed = True
            if not changed:
                break

        removed = tuple(i for i in range(n) if not alive[i])
        kept = "".join(chars[i] for i in range(n) if alive[i])
        return self.lm.text_score(kept) - pen * len(removed), removed

    # ---- 对外 ------------------------------------------------------------
    def clean_line(self, line: str) -> SemanticResult:
        cfg = self.cfg
        if not line:
            return SemanticResult(line, line)

        if cfg.delete_tag_lines:
            reason = ad_line_reason(line)
            if reason == "本章完" and not cfg.delete_chapter_marker:
                reason = None
            if reason is None and (_TAG_RE.match(line) or _URL_ONLY_RE.match(line)):
                reason = "结构性广告行"
            if reason:
                return SemanticResult(line, "", tuple(range(len(line))),
                                      reason=reason)

        chars = list(line)
        n = len(chars)

        # 1) 不可见/控制字符：确定性删除
        forced = set()
        if cfg.delete_invisible:
            for i, ch in enumerate(chars):
                if self._is_invisible(ch):
                    forced.add(i)

        # 2) 其余位置：整句语义解码
        dec = self.decode_rounds if cfg.decoder == "rounds" else self.decode
        if n > cfg.max_line:
            body = [c for i, c in enumerate(chars) if i not in forced]
            idx = [i for i in range(n) if i not in forced]
            mask = self._protect_mask(body)
            _s, rem = dec(body, mask)
            removed = tuple(sorted(forced | {idx[j] for j in rem}))
        else:
            mask = self._protect_mask(chars)
            _s, rem = dec(chars, mask)
            removed = tuple(sorted(set(rem) | forced))

        cleaned = "".join(c for i, c in enumerate(chars) if i not in set(removed))
        base = self.lm.text_score(line)
        new = self.lm.text_score(cleaned)
        delta = new - base - self._penalty * len(removed)
        why = []
        if forced:
            why.append("不可见字符%d" % len(forced))
        if len(removed) > len(forced):
            why.append("语义不成立%d" % (len(removed) - len(forced)))
        return SemanticResult(line, cleaned, removed, delta, "+".join(why))

    def clean_lines(self, lines, progress=None):
        out = []
        for k, line in enumerate(lines):
            out.append(self.clean_line(line))
            if progress and (k % 500 == 0):
                progress(k, len(lines))
        return out


@dataclass
class SemanticLineResult:
    """同名同义，供主链路直接替换。"""
    line: str
    cleaned: str
    removed: tuple = ()
    seen: frozenset = frozenset()
    delta: float = 0.0
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.removed)


class SemanticEngine:
    """`SemanticCleaner` 的主链路适配层：暴露 clean_line 与 _line_score。"""

    def __init__(self, lm: LanguageModel, cfg: SemanticConfig | None = None) -> None:
        from .structure import StructureDetector
        self.cleaner = SemanticCleaner(lm, cfg)
        self.lm = lm
        self.cfg = self.cleaner.cfg
        self.struct = StructureDetector()      # 审查层用（结构异常证据）
        self._lm_path = ""
        self.last_trace: dict = {}

    # 审查层用：受保护位置掩码（人名/专名）
    def _protect_mask(self, chars: list) -> list:
        return self.cleaner._protect_mask(chars)

    # 主链路用：逐行清洗
    def clean_line(self, line: str, trace=None, **kw) -> SemanticLineResult:
        r = self.cleaner.clean_line(line)
        self.last_trace = r.as_dict()
        return SemanticLineResult(line=line, cleaned=r.cleaned, removed=r.removed,
                                  seen=frozenset({r.removed}), delta=r.delta,
                                  reason=r.reason)

    # 主链路用：给审计层算 margin（该决策的净增益）
    def _line_score(self, chars: list, removed) -> float:
        s = set(removed)
        kept = "".join(c for i, c in enumerate(chars) if i not in s)
        return self.lm.text_score(kept) - self.cleaner._penalty * len(s)

    def clean_text(self, text: str):
        lines = text.split("\n")
        return [self.clean_line(x) for x in lines]

    def set_cost(self, lam: float) -> None:
        self.cleaner._penalty = float(lam)


def build_cleaner(lm_path: str = "", cfg: SemanticConfig | None = None,
                  names=DEFAULT_NAMES) -> SemanticCleaner:
    from .v2lm import LanguageModel as _LM
    lm = _LM.load(lm_path) if lm_path else _LM.load(_default_lm_path())
    c = cfg or SemanticConfig()
    if names is not None:
        c.names = tuple(names)
    return SemanticCleaner(lm, c)


def _default_lm_path() -> str:
    from .pipeline import resolve_lm_path
    return resolve_lm_path()
