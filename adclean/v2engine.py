# -*- coding: utf-8 -*-
"""V2 恢复引擎：污染检测 + 最小编辑恢复（KEEP / DELETE / REPLACE）。

KEEP 的 score 记为基线 0，只有 Δ >= 阈值才允许自动改。
"""

from __future__ import annotations

import math
import os
import unicodedata
from dataclasses import dataclass, field

from . import charclass as cc
from .v2lm import LanguageModel, BOS, EOS, UNSEEN_LOG

HOMO_ALL = cc.DIGIT_HOMOPHONE_CHARS | {"〇"}

# 正常创作里常见的装饰/标点：永不通过“结构异常”通道删除
DECOR_SAFE = set(
    "~～*＊-—–―−_＿/／|｜\\、。，．,．!！?？;；:：'\"‘’“”"
    "()（）[]【】{}｛｝〈〉《》「」『』…⋯·・♪♫♬❤◎○●□■△▲▽▼☆★"
) | {"\u3000", "\u00a0"}


def _is_kana(ch: str) -> bool:
    """日文假名（平假名/片假名/半角片假名）。正文里常出现，绝不单独删。"""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF) or (0x31F0 <= o <= 0x31FF) or (0xFF66 <= o <= 0xFF9D)


def _is_decor_block(ch: str) -> bool:
    """装饰性 Unicode 区块（制表符/方块/几何图形/箭头/数学符号等），不是广告。"""
    o = ord(ch)
    return (0x2500 <= o <= 0x257F or      # Box Drawing
            0x2580 <= o <= 0x259F or      # Block Elements
            0x25A0 <= o <= 0x25FF or      # Geometric Shapes
            0x2190 <= o <= 0x21FF or      # Arrows
            0x2200 <= o <= 0x22FF or      # Mathematical Operators
            0x2600 <= o <= 0x27BF or      # Misc Symbols / Dingbats
            0x2E00 <= o <= 0x2E7F or      # Supplemental Punctuation
            0xFE30 <= o <= 0xFE4F or      # CJK Compatibility Forms
            0xFF00 <= o <= 0xFFEF or      # Halfwidth/Fullwidth Forms
            0x1F300 <= o <= 0x1FAFF)      # Emoji


def _is_cjk_symbol(ch: str) -> bool:
    """CJK 符号和标点区（U+3000–U+303F）里的非常规符号。"""
    o = ord(ch)
    return 0x3000 <= o <= 0x303F and ch not in DECOR_SAFE

# 相邻字对“够强”的门槛：搭配次数达到此数即视为正常用字。
STRONG_PAIR = 10

# 默认保护字表（见 data/protected_chars.txt）
_PROTECTED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'data', 'protected_chars.txt')


def load_default_protected() -> frozenset:
    out = set()
    try:
        with open(_PROTECTED_PATH, encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith('#'):
                    out.update(s)
    except OSError:
        pass
    return frozenset(out)


DEFAULT_PROTECTED = load_default_protected()


@dataclass
class V2Config:
    # --- 打分权重 ---
    w_context: float = 0.5         # 上下文（左右重新接上）权重
    w_pollution: float = 0.6       # 污染证据权重（鼓励删可疑字）
    edit_cost: float = 0.8         # 每次编辑的固定代价
    # --- 决策阈值（Δ = 改后分数 - KEEP 分数）---
    auto_cut: float = 3.5          # >= 自动改（保留给强污染证据）
    review_cut: float = 1.5        # >= 待审核（不改，只记录）
    # --- 垃圾字画像 ---
    weak_pair: int = 1             # 左/右搭配次数 <= 此值才算"极弱"
    weak_tri: int = 3              # trigram 次数 <= 此值才算"无像样搭配"
    garbage_pair: int = 1          # 全书最强搭配 <= 此值 → 从未成词（垃圾字通道）
    garbage_neighbor: int = 2      # 邻居本身是垃圾字的判据（内容垃圾门）
    gain_garbage: float = 0.0      # 垃圾字通道的删除收益门槛
    lang_cut: float = 2.0          # 弱附着通道的删除收益门槛
    review_cut: float = 1.0        # 低于此值只进待审核
    struct_gain: float = 3.0       # 结构型垃圾（非汉字夹在汉字中）的收益门槛
    # “接合质量”门槛：删后左右直接相邻的三元组 logp(L2,L,R) 低于此值不许删。
    # 默认 -99 = 关闭（保持原有行为不变）。
    join_logp_min: float = -99.0
    homo_run_min: int = 4          # 数字同音字密码：窗口内同音字个数门槛
    homo_win: int = 15             # 同音字密度窗口半径
    dense_rare_freq: int = 3000    # 稀疏异常字进入候选的频次上限
    struct_win: int = 15           # 异常符号密度窗口半径
    struct_rare_freq: int = 60     # 异常符号"本身少见"的频次上限
    struct_run_min: int = 3        # 异常符号密度门槛
    latin_rare_freq: int = 400     # 夹在汉字里的英文/数字：全书出现上限
    # --- 密集污染区整体清除 ---
    dense_win: int = 30            # 污染密集窗口大小
    dense_min: int = 5             # 窗口内污染字符达到此数才动手
    dense_gain: float = 1.0        # 整批删除的收益门槛
    dense_purge: bool = False      # 密集区整批删除总开关（开启有害，默认关）
    mid_weak_pair: int = 9         # "中度弱附着"上限（只入待审核，不自动删）
    strong_gain: float = 5.0       # 中度弱附着时的收益门槛
    # --- 聚集区（连续一串注入）---
    cluster_gain: float = 1.0      # 整段一起删的收益门槛
    weak_absorb: int = 1           # 吸收时"与邻居无关联"的搭配次数上限
    loose_pair: int = 4            # 吸收时"濒临悬空"的最强搭配上限
    mutual_ratio: float = 0.50     # “互为首选的固定词”判定比例
    mutual_min: int = 5            # 固定搭配至少出现这么多次才算数
    protect_names: bool = False    # 保护“与固定邻居反复共现”的专名用字。默认关闭：会过度保护，
                                   # 专名误删请用 --whitelist 精确豁免。
    word_piece_ratio: float = 0.40  # "最强搭配 / 自身频次" 达到此值 → 判为真词的一部分
    word_ctx_pair: int = 5         # 左右两字本身成词的门槛（保护生僻真词）
    word_ctx_share: float = 0.02   # 与某侧字的搭配占该字频次的比例门槛
    use_word_ctx: bool = False     # 默认关闭：会误保护成词邻居
    name_neighbor_min: int = 3     # 与某个非高频邻居至少共现这么多次 → 判为专名用字
    # --- 候选生成 ---
    very_rare_freq: int = 8        # 全书出现次数 <= 此值算极罕见
    foot_rare_freq: int = 120      # 悬空字进入候选的频次上限
    bridge: int = 3                # 两个可疑字之间夹 <= 此数目的字，视为同一污染段
    max_span: int = 12             # 单个候选段最长长度
    expand: int = 2                # 段两侧各扩张多少个字一起评估
    # --- REPLACE ---
    replace_margin: float = 2.0    # 替换收益需比删除再多这么多才替换
    replace_topk: int = 6
    # --- 安全 ---
    whitelist: frozenset = frozenset()
    keep_min_count: int = 50       # 全书出现 >= 此值的字永不因"悬空"被删（只靠上下文）

    def __post_init__(self) -> None:
        if not self.whitelist:
            self.whitelist = DEFAULT_PROTECTED
        elif not DEFAULT_PROTECTED <= self.whitelist:
            self.whitelist = frozenset(self.whitelist) | DEFAULT_PROTECTED


@dataclass
class V2Edit:
    kind: str                      # delete | replace
    pos: int                       # 行内字符下标
    origin: str                    # 原字
    repl: str = ""                 # 替换成什么
    delta: float = 0.0
    lang: float = 0.0
    context: float = 0.0
    pollution: float = 0.0
    cost: float = 0.0
    confidence: str = "auto"       # auto | review
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "pos": self.pos, "origin": self.origin,
            "repl": self.repl, "delta": round(self.delta, 3),
            "lang": round(self.lang, 3), "context": round(self.context, 3),
            "pollution": round(self.pollution, 3), "cost": round(self.cost, 3),
            "confidence": self.confidence, "reason": self.reason,
        }


@dataclass
class V2LineResult:
    original: str
    cleaned: str
    edits: list = field(default_factory=list)
    reviews: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.cleaned != self.original

    @property
    def removed(self) -> str:
        return "".join(e.origin for e in self.edits)


@dataclass
class V2TextResult:
    original: str
    cleaned: str
    changed_lines: int = 0
    removed_chars: int = 0
    replaced_chars: int = 0
    review_chars: int = 0
    edits: list = field(default_factory=list)      # [(行号, V2LineResult)]
    invisible_chars: int = 0

    @property
    def delta_chars(self) -> int:
        return len(self.original) - len(self.cleaned)


# ---------------------------------------------------------------------------
# 污染证据
# ---------------------------------------------------------------------------

_PLAIN_ASCII = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    ".,!?;:'\"()[]{}<>-_/\\|@#$%^&*+=~` \t"
)


def char_pollution(ch: str, lm: LanguageModel, cfg: V2Config) -> float:
    """单字符的“污染证据强度”，0 表示完全正常。"""
    if cc.is_invisible(ch):
        return 4.0
    cat = unicodedata.category(ch)
    if cat in ("Cf", "Cc", "Co", "Cs", "Cn"):
        return 4.0
    dev = 0.0
    if cc.is_cjk(ch):
        if cc.in_suspect_block(ch):          # 异体字/古字区
            dev += 1.5
        if ch in cc.AD_LIKE_CJK:
            dev += 0.5
    elif cc.is_cjk_punct(ch) or cc.is_space(ch) or ch in _PLAIN_ASCII:
        dev += 0.0
    else:
        dev += 2.0                            # 假名 / 西里尔 / 希腊 / 数学字母 / 装饰
        if cat.startswith("M") or cat in ("Sk", "So", "Sm"):
            dev += 0.5
    n = lm.uni.get(ch, 0)
    if n == 0:
        dev += 0.5
    elif n <= cfg.very_rare_freq:
        dev += 0.8
    elif n <= 60:
        dev += 0.3
    return dev


def is_dangling(ch: str, lm: LanguageModel) -> bool:
    """该字在全书中是否“悬空”：从未与任何字形成像样的搭配。"""
    return lm.best_pair.get(ch, 0) < STRONG_PAIR


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class V2Engine:
    def __init__(self, lm: LanguageModel, cfg: V2Config | None = None) -> None:
        self.lm = lm
        self.cfg = cfg or V2Config()
        self._dangling_cache: dict[str, bool] = {}
        self._strong_cache: dict[str, int] = {}     # 字 → 最强搭配次数（缓存）

    # ---- 辅助 -----------------------------------------------------------
    def _max_pair(self, ch: str) -> int:
        v = self._strong_cache.get(ch)
        if v is None:
            v = self.lm.best_pair.get(ch, 0)
            self._strong_cache[ch] = v
        return v

    def dangling(self, ch: str) -> bool:
        v = self._dangling_cache.get(ch)
        if v is None:
            v = self._max_pair(ch) < STRONG_PAIR
            self._dangling_cache[ch] = v
        return v

    def _local(self, chars: list, keep: list, lo: int, hi: int) -> str:
        """把 [lo,hi) 区间里 keep=True 的字拼成字符串。"""
        return "".join(chars[j] for j in range(lo, hi) if keep[j])

    # ---- 聚集区吸收 ------------------------------------------------------
    def has_name_structure(self, ch: str, topn: int = 20) -> bool:
        """该字是否像“人名/专名”用字：与某个非高频邻居反复共现。"""
        d = self.lm.nbr.get(ch)
        if not d:
            return False
        top = self._top_chars(topn)
        for n, c in d.items():
            if c >= self.cfg.name_neighbor_min and n not in top:
                return True
        return False

    def _top_chars(self, topn: int) -> set[str]:
        cached = getattr(self, "_top_cache", None)
        if cached is not None and cached[0] == topn:
            return cached[1]
        top = {c for c, _ in sorted(
            self.lm.uni.items(), key=lambda kv: -kv[1])[:topn]}
        self._top_cache = (topn, top)
        return top

    def _is_real_word(self, x: str, y: str) -> bool:
        """x 与 y 是否构成“固定搭配”：C(x,y) 占住双方各自最强搭配的大半
        （mutual_ratio）且该搭配出现 >= mutual_min 次。"""
        c = self.lm.bi.get(x + y, 0)
        if c < self.cfg.mutual_min:
            return False
        mx = self.lm.best_pair.get(x, 0)
        my = self.lm.best_pair.get(y, 0)
        if not mx or not my:
            return False
        return c / mx >= self.cfg.mutual_ratio and c / my >= self.cfg.mutual_ratio

    def is_word_piece(self, ch: str) -> bool:
        """该字是否“某个真词的一部分”：最强搭配占自身频次 >= word_piece_ratio。"""
        f = self.lm.uni.get(ch, 0)
        if f <= 0:
            return False
        bp = self.lm.best_pair.get(ch, 0)
        if bp <= 0:
            return False
        return bp / f >= self.cfg.word_piece_ratio

    def _has_word_context(self, L: str, ch: str, R: str,
                          left_pair: int, right_pair: int) -> bool:
        """该字是否嵌在一个“读得通”的词境里（保护全书只出现一两次的生僻真词）。"""
        if L and L != BOS and R and R != EOS:
            if self.lm.bi.get(L + R, 0) >= self.cfg.word_ctx_pair:
                return True
        if R and R != EOS:
            fr = self.lm.uni.get(R, 0)
            if fr and right_pair / fr >= self.cfg.word_ctx_share:
                return True
        if L and L != BOS:
            fl = self.lm.uni.get(L, 0)
            if fl and left_pair / fl >= self.cfg.word_ctx_share:
                return True
        return False

    def _homo_run(self, chars: list, i: int, n: int) -> int:
        """位置 i 周围窗口内的"数字同音字"个数（含自身）。"""
        lo, hi = max(0, i - self.cfg.homo_win), min(n, i + self.cfg.homo_win + 1)
        return sum(1 for j in range(lo, hi) if chars[j] in HOMO_ALL)

    def _struct_run(self, chars: list, i: int, n: int) -> int:
        """位置 i 周围窗口内“非汉字且非 ASCII”的异常字符个数（含自身）。"""
        lo, hi = max(0, i - self.cfg.struct_win), min(n, i + self.cfg.struct_win + 1)
        c = 0
        for j in range(lo, hi):
            ch = chars[j]
            if not cc.is_cjk(ch) and not (ch.isascii() and ch.isalnum()) \
                    and not cc.is_cjk_punct(ch) and not cc.is_space(ch) \
                    and ch not in "\r\n\t":
                c += 1
        return c

    def _absorbable(self, chars: list, j: int, n: int) -> bool:
        """该字能否被“聚集区整体删除”吸收。"""
        ch = chars[j]
        if ch in "\r\n\t" or ch in self.cfg.whitelist:
            return False
        L = chars[j - 1] if j else BOS
        R = chars[j + 1] if j + 1 < n else EOS
        if self.lm.bi.get(L + ch, 0) > self.cfg.weak_absorb:
            return False
        if self.lm.bi.get(ch + R, 0) > self.cfg.weak_absorb:
            return False
        if self.lm.best_pair.get(ch, 0) > self.cfg.loose_pair:
            return False
        if self.lm.uni.get(ch, 0) > self.cfg.foot_rare_freq:
            return False
        if self._is_real_word(L, ch) or self._is_real_word(ch, R):
            return False
        return True

    # ---- 主入口 ---------------------------------------------------------
    def clean_line(self, line: str) -> V2LineResult:
        cfg = self.cfg
        chars = list(line)
        n = len(chars)
        keep = [True] * n

        # 1) 污染足迹
        foot = [False] * n
        poll = [0.0] * n
        for i, ch in enumerate(chars):
            if ch in "\r\n\t" or cc.is_space(ch):
                continue                        # 空白不参与删除，也当边界
            p = char_pollution(ch, self.lm, cfg)
            poll[i] = p
            if p >= 4.0:
                foot[i] = True                  # 不可见/控制/格式字符：直接删
                continue
            if _is_kana(ch):
                poll[i] = 0.0                   # 假名：正文里可能真是日文，永不删
                continue
            if not (cc.is_cjk(ch) or (ch.isascii() and ch.isalnum())):
                # 标点/装饰符号不单独删，只有 CJK 符号区的非常规符号且密集成串才当候选。
                if _is_cjk_symbol(ch) \
                        and self._struct_run(chars, i, n) >= cfg.struct_run_min:
                    foot[i] = True
                else:
                    poll[i] = 0.0
                continue
            lch = chars[i - 1] if i else BOS
            rch = chars[i + 1] if i + 1 < n else EOS
            if cc.is_space(lch):
                lch = BOS
            if cc.is_space(rch):
                rch = EOS
            lpr = self.lm.bi.get(lch + ch, 0)
            rpr = self.lm.bi.get(ch + rch, 0)
            if cc.is_cjk(ch) and self.dangling(ch) \
                    and self.lm.uni.get(ch, 0) <= cfg.foot_rare_freq:
                foot[i] = True                  # 全书悬空字
            elif cc.is_cjk(ch) and (lpr < STRONG_PAIR or rpr < STRONG_PAIR) \
                    and self.lm.uni.get(ch, 0) <= cfg.dense_rare_freq \
                    and self._homo_run(chars, i, n) >= cfg.homo_run_min:
                # 稀疏异常字：频次不高 + 至少一侧搭配弱 + 局部同音/异常字密集。
                foot[i] = True
            elif ch.isascii() and ch.isalnum() and (lpr < STRONG_PAIR or rpr < STRONG_PAIR):
                # 夹在汉字里的英文/数字：需全书出现不多且局部异常字密集。
                if self.lm.uni.get(ch, 0) <= cfg.latin_rare_freq \
                        and self._homo_run(chars, i, n) >= cfg.homo_run_min:
                    foot[i] = True
            elif not cc.is_cjk(ch) and lpr < STRONG_PAIR and rpr < STRONG_PAIR:
                # 结构型垃圾：非汉字夹在汉字之间，且两边都未成词。
                gain = self.lm.logp(
                    chars[i - 2] if i >= 2 else BOS, lch, rch) \
                    - self.lm.logp(chars[i - 2] if i >= 2 else BOS, lch, ch) \
                    - self.lm.logp(lch, ch, rch)
                if gain >= cfg.struct_gain:
                    foot[i] = True

        if not any(foot):
            return V2LineResult(line, line)

        # 2) 聚成污染段（足迹之间夹 <=bridge 个字也算同段）
        spans: list[tuple[int, int]] = []
        start = None
        last = None
        for i in range(n):
            if foot[i]:
                if start is None:
                    start = i
                elif i - last - 1 > cfg.bridge:
                    spans.append((start, last + 1))
                    start = i
                last = i
        if start is not None:
            spans.append((start, last + 1))

        edits: list[V2Edit] = []
        reviews: list[V2Edit] = []

        # 3) 逐段评估
        for s, e in spans:
            if e - s > cfg.max_span:
                e = s + cfg.max_span
            lo = max(0, s - cfg.expand)
            hi = min(n, e + cfg.expand)
            before = self._local(chars, keep, lo, hi)
            base = self.lm.text_score(before)

            # 3a) 先试"整段+扩张区里所有足迹字一起删"
            cand_idx = [j for j in range(lo, hi)
                        if foot[j] and chars[j] not in "\r\n\t"
                        and chars[j] not in cfg.whitelist]
            if not cand_idx:
                continue

            # 3b) 逐字判定，双通道（定标得出）：
            #   A 垃圾字：全书最强搭配 <= garbage_pair 且此处删除收益 >= 0；
            #   B 弱附着：左右搭配都不成词且收益 >= lang_cut。
            lp = self.lm.logp
            for j in cand_idx:
                if not keep[j]:
                    continue
                ch = chars[j]
                L2 = chars[j - 2] if j >= 2 else BOS
                L = chars[j - 1] if j else BOS
                R = chars[j + 1] if j + 1 < n else EOS
                left_pair = self.lm.bi.get(L + ch, 0)
                right_pair = self.lm.bi.get(ch + R, 0)
                if left_pair >= STRONG_PAIR or right_pair >= STRONG_PAIR:
                    continue                      # 与邻居成词 → 正常用字，不动
                gain = lp(L2, L, R) - lp(L2, L, ch) - lp(L, ch, R)
                jl = lp(L2, L, R)                     # 「接合质量」：删后左右直接相邻
                join_ok = jl >= cfg.join_logp_min
                mixed = max(left_pair, right_pair)          # 最强局部搭配
                # 内容垃圾门：搭配弱之外，内容本身也须是垃圾（从未成词，或紧邻字也是垃圾）。
                nl = self.lm.best_pair.get(L, 0) if L != BOS else 0
                nr = self.lm.best_pair.get(R, 0) if R != EOS else 0
                near_garbage = (L != BOS and nl <= cfg.garbage_neighbor) or \
                               (R != EOS and nr <= cfg.garbage_neighbor)
                garbage = mixed <= cfg.garbage_pair
                fixed = (self._is_real_word(L, ch) or self._is_real_word(ch, R)) \
                    or self.is_word_piece(ch)
                if cfg.use_word_ctx and not fixed \
                        and self._has_word_context(L, ch, R, left_pair, right_pair) \
                        and not (garbage or near_garbage):
                    continue                      # 处在词里，且不是铁证垃圾 → 不动
                nameish = cfg.protect_names and self.has_name_structure(ch)
                if nameish:
                    # 像人名的字：只入待审核，不自动删
                    if not fixed and (garbage or gain >= cfg.lang_cut):
                        reviews.append(V2Edit(
                            kind="delete", pos=j, origin=ch, delta=gain,
                            lang=gain, confidence="review",
                            reason="疑似专名用字(与固定邻居反复共现)，待审核"))
                    continue
                if (garbage or near_garbage) and gain >= cfg.gain_garbage and join_ok:
                    conf, reason = "auto", "全书最强搭配仅%d，此处孤立(Δ%.1f,接合%.1f)" % (
                        mixed, gain, jl)
                elif not fixed and gain >= cfg.lang_cut and join_ok:
                    conf, reason = "auto", "左右均无搭配(左%d/右%d)且删除后语言更通顺(Δ%.1f,接合%.1f)" % (
                        left_pair, right_pair, gain, jl)
                elif not fixed and mixed <= cfg.mid_weak_pair \
                        and gain >= cfg.strong_gain:
                    conf, reason = "review", "中度弱附着+高收益(Δ%.1f)，待审核" % gain
                elif gain >= cfg.review_cut:
                    conf, reason = "review", "疑似插入字，收益偏弱(Δ%.1f)" % gain
                else:
                    continue

                ctx = math.log(1 + self.lm.bi.get(L + R, 0))
                delta = gain + cfg.w_context * ctx
                ed = V2Edit(kind="delete", pos=j, origin=ch, delta=delta,
                            lang=gain, context=cfg.w_context * ctx,
                            pollution=poll[j], confidence=conf, reason=reason)
                (edits if conf == "auto" else reviews).append(ed)

            # 3c) 聚集区整体判据：以段内垃圾字为锚点，向两侧吸收“弱附着”邻居成连续片段，
            #     整段试删、收益 >= cluster_gain 才删；与段内邻居强关联的真词会自动断开。
            anchors = [j for j in range(lo, hi)
                       if foot[j] and chars[j] not in cfg.whitelist
                       and max(self.lm.bi.get(
                           (chars[j - 1] if j else BOS) + chars[j], 0),
                           self.lm.bi.get(chars[j] + (chars[j + 1] if j + 1 < n else EOS), 0)
                       ) <= cfg.garbage_pair]
            if not anchors:
                continue
            for a in anchors:
                if not keep[a]:
                    continue
                grp = {a}
                # 向左吸收
                k = a - 1
                while k >= lo and self._absorbable(chars, k, n):
                    grp.add(k)
                    k -= 1
                # 向右吸收
                k = a + 1
                while k < hi and self._absorbable(chars, k, n):
                    grp.add(k)
                    k += 1
                if len(grp) < 2:
                    continue
                before = self._local(chars, keep, lo, hi)
                trial = list(keep)
                for g in grp:
                    trial[g] = False
                after = self._local(chars, trial, lo, hi)
                gain = self.lm.text_score(after) - self.lm.text_score(before)
                if gain < cfg.cluster_gain:
                    continue
                for g in sorted(grp):
                    if not keep[g]:
                        continue
                    ed = V2Edit(kind="delete", pos=g, origin=chars[g],
                                delta=gain / len(grp), lang=gain / len(grp),
                                pollution=poll[g], confidence="auto",
                                reason="聚集区整段(%d字)一起删除后语言通顺(Δ%.1f)"
                                       % (len(grp), gain))
                    edits.append(ed)

        # 3d) 密集污染区整体清除 —— 默认关闭：开启会把正常字一起吃掉，结果比原文更烂
        #     （代码路径保留给实验，dense_purge=True 时启用）。
        if cfg.dense_purge:
            self._dense_purge(chars, keep, foot, line, n, edits)

        # 4) REPLACE：只给"挖掉后模型能明确填回一个字"的位置
        edits = self._try_replace(chars, edits)

        # 5) 应用
        for ed in edits:
            keep[ed.pos] = False
        cleaned = "".join(chars[i] for i in range(n) if keep[i])
        return V2LineResult(line, cleaned, edits, reviews)

    # ---- 密集污染区整体清除 ----------------------------------------------
    def _dense_purge(self, chars: list, keep: list, foot: list, line: str,
                     n: int, edits: list) -> None:
        """把“互相支撑的一段乱码”整批删掉：仅在污染字符密集的窗口里逐批试删，
        收益不足就二分缩小；收益低或候选太少的窗口完全不动。"""
        cfg = self.cfg
        lp = self.lm.logp
        # 找污染密集窗口
        win = [0] * n
        step = max(cfg.dense_win // 2, 1)
        for s in range(0, n, step):
            e = min(n, s + cfg.dense_win)
            cnt = sum(1 for j in range(s, e) if foot[j])
            if cnt >= cfg.dense_min:
                for j in range(s, e):
                    win[j] += 1
        if not any(win):
            return
        # 收集窗口内的"弱附着且非固定词"字
        cand = []
        for j in range(n):
            if not win[j] or not keep[j] or chars[j] in cfg.whitelist:
                continue
            if chars[j] in "\r\n\t":
                continue
            L = chars[j - 1] if j else BOS
            R = chars[j + 1] if j + 1 < n else EOS
            if self.lm.bi.get(L + chars[j], 0) >= STRONG_PAIR \
                    or self.lm.bi.get(chars[j] + R, 0) >= STRONG_PAIR:
                continue                            # 与邻居成词，是真词
            if self._is_real_word(L, chars[j]) or self._is_real_word(chars[j], R):
                continue
            if not (cc.is_cjk(chars[j]) or chars[j].isascii() and chars[j].isalnum()):
                continue
            cand.append(j)
        if len(cand) < cfg.dense_min:
            return

        def gain_of(sel: list) -> float:
            before = "".join(chars[j] for j in range(n) if keep[j])
            drop = set(sel)
            after = "".join(chars[j] for j in range(n)
                            if keep[j] and j not in drop)
            return self.lm.text_score(after) - self.lm.text_score(before)

        g = gain_of(cand)
        if g >= cfg.dense_gain:
            chosen = cand
        else:
            # 二分缩小：删掉左边或右边一半，看哪半收益更高
            best, best_g = None, cfg.dense_gain
            for _ in range(4):
                if len(cand) < cfg.dense_min:
                    break
                mid = len(cand) // 2
                for part in (cand[:mid], cand[mid:]):
                    if len(part) < 2:
                        continue
                    gg = gain_of(part)
                    if gg > best_g:
                        best, best_g = part, gg
                if best is None:
                    break
                cand = best
                best = None
            chosen = cand if best_g >= cfg.dense_gain else None
        if not chosen:
            return
        g = gain_of(chosen)
        if g < cfg.dense_gain:
            return
        for j in chosen:
            if keep[j]:
                edits.append(V2Edit(
                    kind="delete", pos=j, origin=chars[j], delta=g / len(chosen),
                    lang=g / len(chosen), confidence="auto",
                    reason="密集污染区整批清除(%d字，Δ%.1f)" % (len(chosen), g)))

    # ---- REPLACE --------------------------------------------------------
    def _try_replace(self, chars: list, edits: list) -> list:
        cfg = self.cfg
        n = len(chars)
        out: list[V2Edit] = []
        for ed in edits:
            j = ed.pos
            l1 = chars[j - 1] if j - 1 >= 0 else BOS
            l2 = chars[j - 2] if j - 2 >= 0 else BOS
            r1 = chars[j + 1] if j + 1 < n else EOS
            if not (cc.is_cjk(l1) or l1 == BOS) or not cc.is_cjk(r1):
                out.append(ed)
                continue
            # 挖掉后的局部分数
            basis = self.lm.logp(l2, l1, r1)
            preds = self.lm.predict(l2, l1, r1, topk=cfg.replace_topk)
            if not preds:
                out.append(ed)
                continue
            best_c, best_s = preds[0]
            # 替换后的分数：log P(c|l2,l1) + log P(r1|l1,c)
            repl_score = self.lm.logp(l2, l1, best_c) + self.lm.logp(l1, best_c, r1)
            # 与"删除后"比较：删除的分数是 basis（左右直接相邻）
            if repl_score - basis >= cfg.replace_margin and best_c != chars[j]:
                ed.kind = "replace"
                ed.repl = best_c
                ed.delta += repl_score - basis
                ed.reason = "上下文模型预测应填「%s」" % best_c
            out.append(ed)
        return out

    # ---- 说明 -----------------------------------------------------------
    @staticmethod
    def _why(ch: str, lang_gain: float, left_pair: int, right_pair: int) -> str:
        return ("左右均无搭配(左%d/右%d)且删除后语言更通顺(Δ%.1f)"
                % (left_pair, right_pair, lang_gain))


# ---------------------------------------------------------------------------
# 整文清洗
# ---------------------------------------------------------------------------

def clean_text_v2(text: str, engine: V2Engine, sample_limit: int = 40) -> V2TextResult:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out_lines: list[str] = []
    res = V2TextResult(original=text, cleaned="")
    for i, line in enumerate(lines):
        if not line:
            out_lines.append("")
            continue
        r = engine.clean_line(line)
        out_lines.append(r.cleaned)
        if r.changed:
            res.changed_lines += 1
            res.removed_chars += sum(1 for e in r.edits if e.kind == "delete")
            res.replaced_chars += sum(1 for e in r.edits if e.kind == "replace")
            res.invisible_chars += sum(
                1 for e in r.edits if e.origin and cc.is_invisible(e.origin))
            if len(res.edits) < sample_limit:
                res.edits.append((i + 1, r))
        res.review_chars += len(r.reviews)
    res.cleaned = "\n".join(out_lines)
    return res
