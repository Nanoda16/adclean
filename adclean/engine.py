# -*- coding: utf-8 -*-
"""adclean.engine —— 中文小说 TXT 广告（QQ群 / TG 频道）校正核心引擎。

判据：相邻字对的语料共现自然度。删掉一段的增益 = score(左,右) − Σ score(段内相邻对)，
增益超过 gain_threshold 即判定为插入广告并删除。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import charclass as cc

MASK = "\u3000"

# 绝不删除：标点与空白
NEVER_REMOVE = set(
    "，。！？、；：“”‘’（）《》【】〈〉「」『』—…·～"
    "\u3000 \t\r\n"
) | {chr(c) for c in range(0x21, 0x7F)}


# =========================================================================== 模型
class Model:
    """语料自学习：字符频次 + 相邻对频次。"""

    def __init__(self) -> None:
        self.char_freq: Counter = Counter()
        self.bigram_freq: Counter = Counter()
        self.total = 0
        self.rare_threshold = 200
        # 汉字真实共现达到此次数 ⇒ 正文用字（永不删）
        self.min_cooccur = 10
        self.rare_cjk: set[str] = set()
        self.char_maxbigram: dict[str, int] = {}
        self.ad_chars: set[str] = set()
        self.ad_evidence_map: dict[str, dict] = {}
        self.names: set = set()
        self.name_chars: set = set()
        self.char_files: dict[str, set] = {}
        self.n_files = 0
        self.min_cooccur = 10
        self.contaminated_lines = 0
        self._score: dict[str, float] = {}

    # -- 构建 --------------------------------------------------------------
    def fit(self, texts, rare_threshold: int = 200) -> None:
        self.rare_threshold = rare_threshold
        cf, bf = self.char_freq, self.bigram_freq

        all_lines: list[str] = []
        char_files: dict[str, set] = {}
        for fi, text in enumerate(texts):
            ls = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            all_lines.extend(ls)
            for line in ls:
                if line:
                    for ch in line:
                        cf[ch] += 1
                        s = char_files.get(ch)
                        if s is None:
                            char_files[ch] = {fi}
                        else:
                            s.add(fi)
        self.char_files = char_files
        self.n_files = len(texts)
        self.total = sum(cf.values())

        # 先判定污染行；污染行不参与 bigram 统计
        pred = contaminated_predicate()
        bad_lines = 0
        for line in all_lines:
            if not line:
                continue
            bad = pred(line)
            if bad:
                bad_lines += 1
            prev = "\n"
            for ch in line:
                if not bad and ch != MASK:
                    bf[prev + ch] += 1
                prev = ch
            if not bad:
                bf[prev + "\n"] += 1

        self.contaminated_lines = bad_lines
        self.rare_cjk = {ch for ch, nn in cf.items() if cc.is_cjk(ch) and nn <= rare_threshold}
        # 每个字在整个语料里最强的一次搭配次数。
        mb: dict[str, int] = {}
        for bg, nn in bf.items():
            a, b = bg[0], bg[1]
            if nn > mb.get(a, 0):
                mb[a] = nn
            if nn > mb.get(b, 0):
                mb[b] = nn
        self.char_maxbigram = mb

        # 第 2 遍：学习广告伪装字表
        self.learn_ad_chars(all_lines, rare_threshold)
        # 第 3 遍：学习"高频专名"，用于保护人名/地名里的字
        self.learn_names(all_lines)

    def learn_names(self, lines, min_count: int = 20, min_len: int = 2,
                    max_len: int = 4, min_files: int = 1) -> None:
        """从语料里学出高频专名（人名/地名/组织名）并保护其中的字。

        统计出现 >= min_count 次的 2~4 字 n-gram，取不被更长的高频 n-gram 包含的那些。
        """

        from collections import Counter as _C
        grams = [_C() for _ in range(max_len + 1)]
        for line in lines:
            if not line:
                continue
            seg = "".join(ch for ch in line
                          if cc.is_cjk(ch) or ch in "·-")
            if len(seg) < min_len:
                continue
            for L in range(min_len, max_len + 1):
                for i in range(len(seg) - L + 1):
                    g = seg[i:i + L]
                    if all(cc.is_cjk(c) for c in g):
                        grams[L][g] += 1
        names: set = set()
        protected: set = set()
        for L in range(max_len, min_len - 1, -1):
            for g, n in grams[L].items():
                if n < min_count:
                    continue
                if any(g in bigger for bigger in names if len(bigger) > len(g)):
                    continue
                names.add(g)
                protected.update(g)
        self.names = names
        self.name_chars = protected

    def is_name_char(self, ch: str) -> bool:
        return ch in self.name_chars

    def learn_ad_chars(self, lines, rare_threshold: int = 200,
                       window: int = 30, min_rare_in_window: int = 3,
                       min_occ: int = 8, min_share: float = 0.6):
        """识别"广告伪装字表"：证据为可疑窗口密度、集中度 share、孤立度 ratio。"""
        f_all: Counter = Counter()
        f_sus: Counter = Counter()
        n_sus_windows = 0
        half = max(window // 2, 1)
        for line in lines:
            if not line:
                continue
            for ch in line:
                f_all[ch] += 1
            if is_traditional_line(line):
                continue            # 繁体正文段落，不参与广告字学习
            segs = ([line] if len(line) <= window
                    else [line[i:i + window] for i in range(0, len(line), half)])
            for seg in segs:
                rare_cnt = 0
                for ch in seg:
                    if cc.is_cjk(ch) and ch in self.char_freq \
                            and self.char_freq[ch] <= rare_threshold \
                            and not self.has_real_partner(ch, 0.5):
                        rare_cnt += 1
                if rare_cnt >= min_rare_in_window:
                    n_sus_windows += 1
                    for ch in seg:
                        f_sus[ch] += 1

        ev: dict[str, dict] = {}
        for ch, n_all in f_all.items():
            ev[ch] = {
                "share": f_sus.get(ch, 0) / n_all,
                "ratio": (self.char_maxbigram.get(ch, 0) / n_all) if n_all else 0.0,
                "freq": n_all,
            }
        self.ad_evidence_map = ev

        ad: set[str] = set()
        for c in cc.AD_LIKE_CJK:
            e = ev.get(c)
            if e and e["freq"] >= 10 and e["share"] >= 0.25:
                ad.add(c)
        for ch, e in ev.items():
            if cc.is_cjk(ch):
                continue                      # 汉字必须走上面的"已知伪装字"白名单
            if cc.is_invisible(ch) or cc.in_suspect_block(ch):
                ad.add(ch)
                continue
            if e["freq"] < min_occ:
                continue
            if e["share"] >= min_share:
                ad.add(ch)
        # 安全底线：任何"在正文里成词出现"或"在全语料出现较多"的汉字都撤回
        for ch in list(ad):
            if not cc.is_cjk(ch):
                continue
            if self.has_real_partner(ch, 0.5) or self.char_freq.get(ch, 0) >= 250:
                ad.discard(ch)
                continue
            # 跨多本出现 ⇒ 正文用字；单文件时无跨文件信息，跳过此判据
            if self.n_files > 1 and len(self.char_files.get(ch, ())) >= 4:
                ad.discard(ch)
                continue
            # 最强搭配 >= 自身出现次数 ⇒ 正文用字，只是生僻
            f = self.char_freq.get(ch, 0)
            if f and self.char_maxbigram.get(ch, 0) >= f:
                ad.discard(ch)
        self.ad_chars = ad
        self.sus_windows = n_sus_windows

    # -- 评分 --------------------------------------------------------------
    def pair_score(self, a: str, b: str, alpha: float = 2.0) -> float:
        """相邻两字的自然度分数；从未共现返回 -alpha。"""
        n = self.bigram_freq.get(a + b, 0)
        if n:
            return math.log(n)
        return -alpha

    def count(self, a: str, b: str) -> int:
        return self.bigram_freq.get(a + b, 0)

    def significance(self, a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        n = self.bigram_freq.get(a + b, 0)
        if n == 0:
            return 0.0
        ca = self.char_freq.get(a, 0)
        cb = self.char_freq.get(b, 0)
        if ca == 0 or cb == 0:
            return 0.0
        e = ca * cb / max(self.total, 1)
        return n / e if e > 0 else float("inf")

    def is_rare(self, ch: str) -> bool:
        return ch in self.rare_cjk

    def has_real_partner(self, ch: str, floor: float) -> bool:
        """该字是否"本来就属于正文用字"：最强搭配 >= min_cooccur，或 >= 自身频次 * floor。"""
        f = self.char_freq.get(ch, 0)
        if f <= 0:
            return False
        mb = self.char_maxbigram.get(ch, 0)
        if mb >= self.min_cooccur:
            return True
        return mb >= f * floor

    def ad_evidence(self, ch: str) -> dict:
        """该字的"广告证据"统计（share / ratio / freq）。"""
        return self.ad_evidence_map.get(ch, {})

    def is_ad_char(self, ch: str) -> bool:
        """该字是否判定为广告伪装字；没有证据的字一律保留。"""
        return ch in self.ad_chars

    def stats(self) -> dict:
        return {
            "chars": len(self.char_freq),
            "bigrams": len(self.bigram_freq),
            "total_chars": self.total,
            "rare_cjk": len(self.rare_cjk),
            "contaminated_lines": self.contaminated_lines,
            "ad_chars": len(self.ad_chars),
            "suspicious_windows": self.sus_windows,
        }

    # -- 模型缓存（批量处理大目录时避免重复学习）--------------------------
    def save(self, path: str) -> None:
        import json
        data = {
            "rare_threshold": self.rare_threshold,
            "total": self.total,
            "char_freq": dict(self.char_freq),
            "bigram_freq": dict(self.bigram_freq),
            "char_maxbigram": self.char_maxbigram,
            "rare_cjk": sorted(self.rare_cjk),
            "ad_chars": sorted(self.ad_chars),
            "contaminated_lines": self.contaminated_lines,
            "sus_windows": self.sus_windows,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "Model":
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        m = cls()
        m.rare_threshold = data.get("rare_threshold", 200)
        m.total = data.get("total", 0)
        m.char_freq = Counter(data.get("char_freq", {}))
        m.bigram_freq = Counter(data.get("bigram_freq", {}))
        m.char_maxbigram = data.get("char_maxbigram", {})
        m.rare_cjk = set(data.get("rare_cjk", []))
        m.ad_chars = set(data.get("ad_chars", []))
        m.contaminated_lines = data.get("contaminated_lines", 0)
        m.sus_windows = data.get("sus_windows", 0)
        return m


# =========================================================================== 粗判
_SUSPECT_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")

# 用于识别繁体正文段落的常用繁体字
TRADITIONAL_ONLY = set(
    "們個來這說時會後對麼為國學實體發開關進過還遠邊點電頭顯風飛馬鳥魚龍"
    "沒無與將張強當稱種經給紅結線繪續繼續緣級紀約紙級終組細織綠緩編練縣縮"
    "兒內兩劍動務勝勢區協單賣買貴費資質賞購賽貝賬貸購員問間聞閉閒鬧陽陰陳陸"
    "際隨險隱雖雙難雲霧靈靜韓順須預領顆題願類顧顯風飛飯飲飼餓館駐駕騎驚體鮮"
    "韓響頁頂項順須頌預領頭顆題顏願顧顯風飛"
)


def is_traditional_line(line: str, min_hits: int = 2, ratio: float = 0.15) -> bool:
    """粗判一行是否为"繁体正文"（源文本简繁混排，与广告无关，必须保护）。"""
    if len(line) < 8:
        return False
    cjk = 0
    hits = 0
    for ch in line:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
            if ch in TRADITIONAL_ONLY:
                hits += 1
    if cjk < 8:
        return False
    return hits >= min_hits and hits / cjk >= ratio


def is_ad_candidate(ch: str, freq: int, share: float, ratio: float,
                    n_files: int, in_whitelist: bool,
                    min_occ: int = 10, min_share: float = 0.25,
                    max_freq: int = 250, max_files: int = 4) -> bool:
    """单字是否判为"广告伪装字"：属候选集且 freq ∈ [min_occ, max_freq)、n_files < max_files、share >= min_share。"""
    if not in_whitelist:
        return False
    if freq < min_occ or freq >= max_freq:
        return False
    if n_files >= max_files:
        return False
    if share < min_share:
        return False
    return True


def contaminated_predicate():
    """构造"某行是否疑似含广告"判定函数（只用结构性异常做种子，不依赖广告字先验）。"""
    def pred(line: str) -> bool:
        if _SUSPECT_RE.search(line):
            return True
        for ch in line:
            if cc.is_invisible(ch) or cc.in_suspect_block(ch):
                return True
        return False
    return pred


# =========================================================================== 配置
@dataclass
class Config:
    drop_unusual_blocks: bool = True
    rare_threshold: int = 200
    # 极罕见阈值：全语料出现次数 <= 此值的汉字才可能被判为广告伪装字删除。
    very_rare_freq: int = 8
    # 配对自然度：从未共现的惩罚
    alpha: float = 2.0
    # 未知搭配（全语料从未共现）的评分下限
    unknown_penalty: float = -4.0
    # 删除增益阈值
    gain_threshold: float = 1.0
    # 最长删除片段
    max_span: int = 16
    # 单次迭代最多考察多长的候选区域
    max_region: int = 24
    # 每行最多迭代删多少次
    max_edits_per_line: int = 60
    # 次罕用字阈值：语料出现次数低于此值的汉字也纳入候选
    common_threshold: int = 12000
    # 足迹之间最多夹多少个字符仍视为同一段
    bridge: int = 3
    # 清洗残留所需的删除增益
    cleanup_gain: float = 0.5
    # 正文用字的绝对搭配次数下限
    keep_min_count: int = 50
    # 稀有汉字用其中很小众的下限
    rare_lexicon_floor: int = 8
    # 全局保护：最强搭配次数 / 总出现次数 >= 此值 ⇒ 正文用字，永不删
    partner_ratio: float = 0.30
    # 全局保护：真实共现达到此次数 ⇒ 正文用字，永不删
    min_cooccur: int = 10
    # 用户白名单：这些字符永不删除
    whitelist: frozenset = frozenset()
    # 搭配次数 >=3 且显著性高于此倍数
    keep_min_ratio: float = 8.0
    drop_empty_lines: bool = True

    # ---------------- 统一增益判据的权重 ----------------
    # 候选片段向锚点两侧扩张的字符数
    expand: int = 6
    # ---- 激进模式（aggressive，CLI --aggressive / Web UI 强力模式）----
    # 默认关闭：只删高置信度广告伪装字；打开后改用"异常分 + 搭配质量"统一增益判据，误删率更高。
    aggressive: bool = False
    # 逐字清洗：删除单个字符所需的收益（仅激进模式）
    single_gain: float = 2.0
    # 异常分：结构性异常字符（数学字母/卢恩/零宽…）
    anom_structural: float = 4.0
    # 异常分：拉丁/数字/符号等非中文字符
    anom_latin: float = 1.4
    # 异常分：全语料罕见到不可能出现在正文里的汉字
    anom_rare: float = 2.0
    # 异常分：在语料里完全没有搭配（maxbigram 极低）的汉字
    anom_nopair: float = 2.0
    # 异常分：数字同音字（群号广告的强信号，哪怕该字本身常用）
    anom_homo: float = 1.0
    # 异常分：第一遍学习到的广告伪装字
    anom_learned: float = 1.0
    # 已学习伪装字只有"本身不算常用"时才加分
    learned_freq: int = 1200
    # 专名保护：出现在高频专名里、且全语料出现次数 >= 此值的字，视为正文用字
    name_char_freq: int = 30
    # 片段级保护：片段中出现频次 >= 此值的汉字时，不整段删除
    common_char_freq: int = 800
    common_char_penalty: float = 3.0
    # 拉丁/西里尔等字母串的长度保护：>= 此长度的连续字母视为正常外文词
    latin_word_len: int = 5
    # "没有搭配"的判定：maxbigram <= 此值
    nopair_bigram: int = 6
    # "有稳定搭配"的判定：maxbigram >= 此值 ⇒ 该字是正文用字，永不参与删除
    real_partner_bigram: int = 30
    # "稀有"的判定：全语料出现次数 <= 此值
    anom_rare_freq: int = 200
    # "搭配很弱"的判定：maxbigram < 此值
    weak_bigram: int = 60
    # 数字同音字加分只在"该字不算常用"时生效
    homo_freq: int = 4000


# =========================================================================== 结果
@dataclass
class Edit:
    start: int
    end: int
    removed: str
    gain: float = 0.0
    reason: str = "insertion"

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "removed": self.removed,
                "gain": round(self.gain, 3), "reason": self.reason}


@dataclass
class LineResult:
    original: str
    cleaned: str
    edits: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.edits)

    @property
    def removed(self) -> str:
        return "".join(e.removed for e in self.edits)


# =========================================================================== 连接度
def _connectivity(model: Model, chars: list, keep: list, i: int, alpha: float) -> float:
    """字符 i 与左右存活邻居的配对分数之和（越大越像正文）。"""
    s = 0.0
    if i > 0 and keep[i - 1]:
        s += model.pair_score(chars[i - 1], chars[i], alpha)
    if i + 1 < len(chars) and keep[i + 1]:
        s += model.pair_score(chars[i], chars[i + 1], alpha)
    return s


def _breach(model: Model, chars: list, keep: list, i: int, alpha: float) -> float:
    """删掉 i 的收益 = 左右接起来的分数 - 原来的两段分数。"""
    left = chars[i - 1] if i > 0 and keep[i - 1] else None
    right = chars[i + 1] if i + 1 < len(chars) and keep[i + 1] else None
    old = 0.0
    if left is not None:
        old += model.pair_score(left, chars[i], alpha)
    if right is not None:
        old += model.pair_score(chars[i], right, alpha)
    new = model.pair_score(left, right, alpha) if (left is not None and right is not None) else 0.0
    return new - old


# =========================================================================== 核心
def clean_line(line: str, model: Model, cfg: Config) -> LineResult:
    model.min_cooccur = cfg.min_cooccur
    wl = cfg.whitelist
    edits: list = []

    # ---- L1 不可见字符（零宽/方向控制/组合符/私用区）----
    buf = []
    for ch in line:
        if cc.is_invisible(ch):
            edits.append(Edit(-1, -1, ch, 99.0, "invisible"))
        else:
            buf.append(ch)
    s = "".join(buf)
    if not s:
        return LineResult(line, s, edits)

    chars = list(s)
    n = len(chars)

    # ---- L2 结构异常字符（数学字母/卢恩/哥特/切罗基等）：无条件删除 ----
    if cfg.drop_unusual_blocks:
        chars = [ch for ch in chars if not cc.in_suspect_block(ch)]

    # ---- L3+ 策略分派 ----
    if cfg.aggressive:
        chars = _clean_aggressive(chars, model, cfg, edits, wl)
    else:
        chars = _clean_conservative(chars, model, cfg, edits, wl)

    # ---- 组装 ----
    return LineResult(line, "".join(chars), edits)


def _clean_conservative(chars: list, model: Model, cfg: Config,
                        edits: list, wl: frozenset) -> list:
    """保守策略：L3 候选区 → L4 整段试删 → L5 残留清洗。"""
    n = len(chars)
    keep = [True] * n
    freq = model.char_freq
    maxbg = model.char_maxbigram

    # ---- L3 候选区间 ----
    foot = [False] * n
    for i, ch in enumerate(chars):
        if ch in NEVER_REMOVE:
            continue
        if not cc.is_cjk(ch):
            foot[i] = True
        elif model.is_name_char(ch) and freq.get(ch, 0) >= cfg.name_char_freq:
            continue                       # 专名里的字，保护
        elif model.is_ad_char(ch):
            foot[i] = True
        elif freq.get(ch, 0) <= cfg.very_rare_freq \
                and not model.has_real_partner(ch, cfg.partner_ratio) \
                and maxbg.get(ch, 0) < freq.get(ch, 0):
            foot[i] = True
    cand = list(foot)
    last_foot = None
    for i in range(n):
        if foot[i]:
            if last_foot is not None and i - last_foot - 1 <= cfg.bridge:
                for t in range(last_foot, i + 1):
                    cand[t] = True
            last_foot = i
    base = list(cand)
    for i in range(n):
        if base[i]:
            for d in (-1, 1):
                j = i + d
                for _ in range(cfg.expand):
                    if 0 <= j < n and not cand[j] and chars[j] not in NEVER_REMOVE:
                        cand[j] = True
                        j += d
                    else:
                        break
    cand_mask = list(cand)

    # ---- L4 迭代整段试删 ----
    # 保守策略用独立的评分尺度 cons_alpha，阈值 gain_threshold 与之配套。
    cons_alpha = -2.0
    for _ in range(cfg.max_edits_per_line):
        best = None
        i = 0
        while i < n:
            if not cand[i] or not keep[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and cand[j + 1]:
                j += 1
            if j - i + 1 > cfg.max_region:
                j = i + cfg.max_region - 1
            for a in range(i, j + 1):
                if not keep[a] or not cand[a]:
                    continue
                hi = min(j, a + cfg.max_span - 1)
                lch = chars[a - 1] if a > 0 and keep[a - 1] else None
                for b in range(a + 1, hi + 2):
                    if not keep[b - 1]:
                        break
                    right = chars[b] if b < n and keep[b] else None
                    if lch is None and right is None:
                        continue
                    # 片段必须含"伪装字"才允许整段删除
                    if not any(keep[t] and model.is_ad_char(chars[t])
                               for t in range(a, b)):
                        continue
                    if wl and any(chars[t] in wl for t in range(a, b)):
                        continue
                    inside = 0.0
                    for t in range(a + 1, b):
                        inside += model.pair_score(chars[t - 1], chars[t], cons_alpha)
                    old = inside
                    if lch is not None:
                        old += model.pair_score(lch, chars[a], cons_alpha)
                    if right is not None:
                        old += model.pair_score(chars[b - 1], right, cons_alpha)
                    new = model.pair_score(lch, right, cons_alpha) if (
                        lch is not None and right is not None) else 0.0
                    gain = new - old
                    if gain > cfg.gain_threshold and (best is None or gain > best[0]):
                        best = (gain, a, b)
            i = j + 1
        if best is None:
            break
        gain, a, b = best
        for t in range(a, b):
            keep[t] = False
        edits.append(Edit(a, b, "".join(chars[a:b]), gain, "insertion"))
        for d in (-1, 1):
            t = a - 1 if d < 0 else b
            steps = 0
            while 0 <= t < n and steps < cfg.expand + 1:
                if keep[t]:
                    cand_mask[t] = True
                    break
                t += d
                steps += 1

    # ---- L5 残留清洗 ----
    near_deleted = [False] * n
    for i in range(n):
        if keep[i]:
            continue
        for d in (-1, 1):
            t = i + d
            while 0 <= t < n and not keep[t]:
                t += d
            if 0 <= t < n:
                near_deleted[t] = True
    for _ in range(cfg.max_edits_per_line):
        victim = None
        best_gain = None
        for i in range(n):
            if not keep[i] or not cand_mask[i]:
                continue
            ch = chars[i]
            if ch in NEVER_REMOVE or (wl and ch in wl):
                continue
            if not model.is_ad_char(ch):
                f = freq.get(ch, 0)
                if not (cc.is_cjk(ch) and f <= cfg.very_rare_freq
                        and not model.has_real_partner(ch, cfg.partner_ratio)
                        and maxbg.get(ch, 0) < f):
                    continue
            left = chars[i - 1] if i > 0 and keep[i - 1] else None
            right = chars[i + 1] if i + 1 < n and keep[i + 1] else None
            if left is None or right is None:
                continue
            best_n = max(model.count(left, ch), model.count(ch, right))
            floor = cfg.rare_lexicon_floor if model.is_rare(ch) else cfg.keep_min_count
            if best_n >= floor:
                continue
            g = _breach(model, chars, keep, i, cons_alpha)
            if g > cfg.cleanup_gain and (best_gain is None or g > best_gain):
                best_gain = g
                victim = i
        if victim is None:
            break
        keep[victim] = False

    return [c for c, k in zip(chars, keep) if k]


def _clean_aggressive(chars: list, model: Model, cfg: Config,
                      edits: list, wl: frozenset) -> list:
    """异常分 + 搭配质量的统一增益判据（能清掉夹杂常用字的碎片）。"""
    freq = model.char_freq
    maxbg = model.char_maxbigram
    a_cfg, b_cfg, c_cfg = cfg.anom_rare, cfg.anom_nopair, cfg.anom_homo

    def anom(ch: str) -> float:
        if cc.is_invisible(ch) or cc.in_suspect_block(ch):
            return cfg.anom_structural
        if ch in NEVER_REMOVE or cc.is_cjk_punct(ch) or cc.is_space(ch):
            return 0.0
        if wl and ch in wl:
            return 0.0
        f = freq.get(ch, 0)
        if not cc.is_cjk(ch):
            return cfg.anom_latin
        mb = maxbg.get(ch, 0)
        if mb >= cfg.real_partner_bigram:
            return 0.0
        # 专名保护：该字出现在高频专名里（人名/地名）⇒ 视为正文用字
        if model.is_name_char(ch) and f >= cfg.name_char_freq:
            return 0.0
        s = 0.0
        if mb <= cfg.nopair_bigram:
            s += b_cfg
            if f <= cfg.anom_rare_freq:
                s += a_cfg
        elif f <= cfg.anom_rare_freq:
            s += a_cfg * 0.3
        if model.is_ad_char(ch) and (f <= cfg.learned_freq or ch in cc.AD_LIKE_CJK):
            s += cfg.anom_learned
        if ch in cc.DIGIT_HOMOPHONE_CHARS and f <= cfg.homo_freq:
            s += c_cfg
        return s

    for _ in range(cfg.max_edits_per_line):
        k = len(chars)
        if k < 3:
            break
        an = [anom(ch) for ch in chars]
        anchors = [i for i in range(k) if an[i] > 0]
        if not anchors:
            break
        best = None
        for s in anchors:
            for a in range(max(0, s - cfg.expand), s + 1):
                for b in range(s + 1, min(k, a + cfg.max_span) + 1):
                    # 片段保护 1：含较常用汉字 ⇒ 原文骨架，不整段删
                    if any(cc.is_cjk(chars[t]) and freq.get(chars[t], 0) >= cfg.common_char_freq
                           for t in range(a, b)):
                        continue
                    # 片段保护 2：连续 ASCII 字母 ⇒ 正常外文词
                    longest = run = 0
                    for t in range(a, b):
                        c2 = chars[t]
                        if c2.isascii() and c2.isalpha():
                            run += 1
                            longest = max(longest, run)
                        else:
                            run = 0
                    if longest >= cfg.latin_word_len:
                        continue
                    # 片段保护 3：连续西里尔/希腊字母 ⇒ 正常外文词（Верный）
                    f_run = f_long = 0
                    for t in range(a, b):
                        o = ord(chars[t])
                        if 0x0400 <= o <= 0x04FF or 0x0370 <= o <= 0x03FF:
                            f_run += 1
                            f_long = max(f_long, f_run)
                        else:
                            f_run = 0
                    if f_long >= 3:
                        continue
                    inside = 0.0
                    for t in range(a + 1, b):
                        inside += model.pair_score(chars[t - 1], chars[t], cfg.alpha)
                    left = chars[a - 1] if a > 0 else None
                    right = chars[b] if b < k else None
                    old = inside
                    if left is not None:
                        old += model.pair_score(left, chars[a], cfg.alpha)
                    if right is not None:
                        old += model.pair_score(chars[b - 1], right, cfg.alpha)
                    new = model.pair_score(left, right, cfg.alpha) if (
                        left is not None and right is not None) else 0.0
                    gain = sum(an[a:b]) - old + new
                    if gain > cfg.gain_threshold and (best is None or gain > best[0]):
                        best = (gain, a, b)
        if best is None:
            break
        gain, a, b = best
        edits.append(Edit(a, b, "".join(chars[a:b]), gain, "insertion"))
        del chars[a:b]

    # 逐字清洗：残余"孤立嫌疑字"（只删单字，不整段删）
    for _ in range(cfg.max_edits_per_line):
        k = len(chars)
        if k < 3:
            break
        victim = None
        vgain = None
        for i in range(1, k - 1):
            sc = anom(chars[i])
            if sc <= 0 or (wl and chars[i] in wl):
                continue
            old = 0.0
            for x, y in ((chars[i - 1], chars[i]), (chars[i], chars[i + 1])):
                old += model.pair_score(x, y, 2.0)
            gain = sc + model.pair_score(chars[i - 1], chars[i + 1], 2.0) - old
            if gain > cfg.single_gain and (vgain is None or gain > vgain):
                vgain, victim = gain, i
        if victim is None:
            break
        edits.append(Edit(victim, victim + 1, chars[victim], vgain, "insertion"))
        del chars[victim]

    return chars


def _merge_edits(chars: list, keep: list) -> list:
    edits = []
    start = None
    for i, k in enumerate(keep):
        if not k:
            if start is None:
                start = i
        else:
            if start is not None:
                edits.append(Edit(start, i, "".join(chars[start:i]), 0.0, "insertion"))
                start = None
    if start is not None:
        edits.append(Edit(start, len(chars), "".join(chars[start:]), 0.0, "insertion"))
    return edits


# =========================================================================== 全文
@dataclass
class TextResult:
    original: str
    cleaned: str
    changed_lines: int = 0
    removed_chars: int = 0
    invisible_chars: int = 0
    samples: list = field(default_factory=list)


def clean_text(text: str, model: Model, cfg: Config,
               sample_limit: int = 40, progress=None) -> TextResult:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out_lines = []
    changed = removed = invisible = 0
    samples = []
    for idx, line in enumerate(lines):
        if line:
            r = clean_line(line, model, cfg)
            if r.changed:
                changed += 1
                removed += sum(len(e.removed) for e in r.edits)
                invisible += sum(1 for e in r.edits if e.reason == "invisible")
                if len(samples) < sample_limit:
                    samples.append({
                        "line": idx + 1,
                        "before": r.original,
                        "after": r.cleaned,
                        "removed": r.removed,
                    })
            out_lines.append(r.cleaned if r.changed else line)
        else:
            out_lines.append(line)
        if progress and idx % 5000 == 0:
            progress(idx + 1, len(lines))
    return TextResult("\n".join(lines), "\n".join(out_lines),
                      changed, removed, invisible, samples)
