# -*- coding: utf-8 -*-
"""adclean.locate —— 多特征字级污染定位器（v1 默认统计引擎）。

处理流程：书级门禁 → 段落级筛查 → 字级定位（两遍打分）→ 精度规则。
只删不改，绝不替换、绝不插入。使用 LightGBM 原生文本模型 + SHA-256 校验，无 pickle / sklearn 依赖。
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import unicodedata

import lightgbm as lgb
import numpy as np

from . import charclass as cc
from .patterns import (BODY_PUNCT, CJK_SYM_RE, DECOR_LOCATE, HALF_PUNCT,
                       STRUCT_ALL, TAGRE, JobCancelledError)
from .v2lm import BOS, EOS, LanguageModel

# ---------------------------------------------------------------- 特征定义
DECOR = DECOR_LOCATE
HALF = HALF_PUNCT
CJK_SYM = CJK_SYM_RE

FEATS = [
    "不可见", "非BMP", "拉丁数字", "半角标点", "装饰符", "非常用CJK符号", "数字同音字", "稀见同音字",
    "字频log", "左搭配log", "右搭配log", "接缝log", "trigram最小",
    "接缝相对分", "左搭配PMI", "右搭配PMI", "接缝PMI",
    "同字接缝均值", "本位置接缝偏离", "同字搭配均值", "同字频次log",
    "左同音", "右同音", "邻域同音密度", "伪装度", "邻域伪装度",
    "左成词", "右成词", "接缝成词", "三字词中间",
    "行搭配断裂", "行非正文占比", "行内位置", "行内最大接缝",
    "单向Δ", "双向Δ",
    "同音替换拟合", "形近替换拟合", "同部首邻", "笔画数",
]
N_FEAT = len(FEATS)
IDX = {n: i for i, n in enumerate(FEATS)}
FAMILY = {
    "A 类别": [IDX[n] for n in ("不可见", "非BMP", "拉丁数字", "半角标点", "装饰符",
                              "非常用CJK符号", "数字同音字", "稀见同音字")],
    "B 局部搭配": [IDX[n] for n in ("字频log", "左搭配log", "右搭配log", "接缝log", "trigram最小")],
    "B2 标准化搭配": [IDX[n] for n in ("接缝相对分", "左搭配PMI", "右搭配PMI", "接缝PMI")],
    "C 全书累积": [IDX[n] for n in ("同字接缝均值", "本位置接缝偏离", "同字搭配均值", "同字频次log")],
    "D 拼音": [IDX[n] for n in ("左同音", "右同音", "邻域同音密度", "伪装度", "邻域伪装度")],
    "E 词级": [IDX[n] for n in ("左成词", "右成词", "接缝成词", "三字词中间")],
    "F 段落语境": [IDX[n] for n in ("行搭配断裂", "行非正文占比", "行内位置", "行内最大接缝")],
    "G 语义": [IDX[n] for n in ("单向Δ", "双向Δ")],
    "I 替换机制拟合": [IDX[n] for n in ("同音替换拟合", "形近替换拟合", "同部首邻", "笔画数")],
}

# 放弃 I 族与笔画/部首特征
DROP = [IDX[n] for n in ("同音替换拟合", "形近替换拟合", "同部首邻", "笔画数")]
KEEP = [c for c in range(N_FEAT) if c not in set(DROP)]
STAGE1_FAMS = ("A 类别", "B 局部搭配", "B2 标准化搭配", "C 全书累积", "F 段落语境")
S1 = [c for c in KEEP if any(c in FAMILY[f] for f in STAGE1_FAMS)]
_S1_POS_IN_KEEP = [KEEP.index(c) for c in S1]
_KEEP_INDEX = {c: pos for pos, c in enumerate(KEEP)}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT_MODEL = os.path.join(DATA_DIR, "locate_meta.json")
PINYIN_TSV = os.path.join(DATA_DIR, "pinyin.tsv")


def _cat(ch: str) -> str:
    return unicodedata.category(ch)


def invisible_at(line: str) -> set:
    return {i for i, c in enumerate(line)
            if cc.is_invisible(c) or c in "\u200b\u200c\u200d\u2060\ufeff"
            or _cat(c) in ("Cf", "Cc", "Co", "Cs", "Cn")}


def nonbmp_at(line: str) -> set:
    out = set()
    for i, c in enumerate(line):
        if ord(c) > 0xFFFF or _cat(c) in ("Co", "Cs"):
            out.add(i)
    return out


# ---------------------------------------------------------------- 颜文字保护
KAO_CORE = set("╯╰╭╮╱╲╳┻┳┠┨┯┷┿╂│┌┐└┘├┤┬┴┼︵︶︷︸︹︺﹁﹂﹃﹄"
               "⊙□△▲○●◯☆★♡♥′‵´｀゜゛ω∀зε・ー～^_-")
KAO_OK_EXTRA = set("()[]{}<>/\\|.,;:*+=~-`\"'!?·、。")
BOX = re.compile(r"[\u2500-\u257f\u2580-\u259f\u25a0-\u25ff\u2190-\u21ff"
                 r"\u2600-\u26ff\u2700-\u27bf\u3000-\u303f\uff01-\uff5e]")
KANA = re.compile(r"[\u3040-\u30ff]")


def _is_digit_homophone_cjk(ch: str) -> bool:
    return ch in cc.DIGIT_HOMOPHONE_CHARS and 0x4E00 <= ord(ch) <= 0x9FFF


def _is_kao_char(ch: str) -> bool:
    o = ord(ch)
    if 0x4E00 <= o <= 0x9FFF:
        return False
    if ch.isspace():
        return False
    if ch in KAO_OK_EXTRA:
        return True
    if ch.isascii() and ch.isalnum():
        return True
    if BOX.match(ch) or KANA.match(ch):
        return True
    if 0x0400 <= o <= 0x04FF:
        return True
    return False


def kao_mask(line: str) -> set:
    """颜文字/装饰符整组的位置（这些位置永不删）。"""
    n = len(line)
    prot: set = set()
    i = 0
    while i < n:
        ch = line[i]
        if _is_kao_char(ch) and not _is_digit_homophone_cjk(ch):
            j = i
            core = False
            while j < n and _is_kao_char(line[j]) \
                    and not _is_digit_homophone_cjk(line[j]):
                if line[j] in KAO_CORE:
                    core = True
                j += 1
            if j - i >= 4 and core:
                prot.update(range(i, j))
            i = j
        else:
            i += 1
    return prot


# ---------------------------------------------------------------- token 完整性
TOKEN = re.compile(r"[0-9A-Za-z]+|[０-９Ａ-Ｚａ-ｚ]+")


def token_fix(line: str, flagged: set) -> set:
    """字母数字 token 完整性：只被标中一部分的整串保留，全标中才整串删。

    纯数字长度≥2 / 纯字母长度≥3 的串必须全部被标中才删；
    其余（混合字母数字、长度 2 的字母串）按 0.8 比例。
    """
    flagged = set(flagged)
    for m in TOKEN.finditer(line):
        lo, hi = m.start(), m.end()
        if hi - lo < 2:
            continue
        run = line[lo:hi]
        inside = {i for i in range(lo, hi) if i in flagged}
        if not inside:
            continue
        is_digits = all(c.isdigit() or c in "０１２３４５６７８９" for c in run)
        is_alpha = all(c.isalpha() and c.isascii() for c in run)
        need_all = (is_digits and hi - lo >= 2) or (is_alpha and hi - lo >= 3)
        ratio = len(inside) / (hi - lo)
        if (need_all and ratio >= 1.0) or ((not need_all) and ratio >= 0.8):
            flagged.update(range(lo, hi))
        else:
            flagged.difference_update(inside)
    return flagged


# ---------------------------------------------------------------- 拼音
class _Pinyin:
    def __init__(self, path: str = PINYIN_TSV):
        self.syl: dict[str, str] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for ln in fh:
                    parts = ln.rstrip("\r\n").split("\t")
                    if len(parts) == 2 and parts[0]:
                        self.syl[parts[0]] = parts[1]
        self.group = collections.defaultdict(list)
        for c, s in self.syl.items():
            self.group[s].append(c)

    def __len__(self) -> int:
        return len(self.syl)


def _l1(x: float) -> float:
    return math.log1p(x)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_native_models(model_path: str) -> tuple[lgb.Booster, lgb.Booster, lgb.Booster, float, float]:
    """校验 SHA-256 并加载原生 LightGBM Booster 模型，完全不依赖 pickle/sklearn。"""
    meta_path = model_path or DEFAULT_MODEL
    if os.path.isdir(meta_path):
        meta_path = os.path.join(meta_path, "locate_meta.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    base_dir = os.path.dirname(os.path.abspath(meta_path))
    models_info = meta["models"]
    boosters: dict[str, lgb.Booster] = {}
    for key in ("m1", "p1", "p2"):
        info = models_info[key]
        mfile = os.path.join(base_dir, info["file"])
        if not os.path.isfile(mfile):
            raise FileNotFoundError(f"缺少定位器模型文件: {mfile}")
        expected_sha = info.get("sha256", "")
        if expected_sha:
            actual_sha = _sha256_file(mfile)
            if actual_sha.lower() != str(expected_sha).lower():
                raise ValueError(
                    f"模型文件 {info['file']} SHA-256 校验失败: "
                    f"期望 {expected_sha}，实际 {actual_sha}"
                )
        booster = lgb.Booster(model_file=mfile)
        expected_dim = info.get("num_features")
        if expected_dim is not None and booster.num_feature() != int(expected_dim):
            raise ValueError(
                f"模型 {key} 特征维度不匹配: 期望 {expected_dim}，实际 {booster.num_feature()}"
            )
        boosters[key] = booster
    return (
        boosters["m1"],
        boosters["p1"],
        boosters["p2"],
        float(meta["thr_line"]),
        float(meta["thr_char"]),
    )


def _predict_pos_proba(model, X: np.ndarray) -> np.ndarray:
    """直接调用底层 LightGBM Booster 预测正类概率。"""
    if isinstance(model, lgb.Booster):
        return np.asarray(model.predict(X), dtype=np.float64)
    booster = getattr(model, "booster_", None)
    if booster is not None:
        return np.asarray(booster.predict(X), dtype=np.float64)
    if hasattr(model, "predict_proba"):
        return np.asarray(model.predict_proba(X)[:, 1], dtype=np.float64)
    return np.asarray(model.predict(X), dtype=np.float64)


def _name_positions(line: str, names) -> set[int]:
    """返回行内命中人名/专名白名单的全部字符下标集合。"""
    if not names or not line:
        return set()
    pos: set[int] = set()
    for name in names:
        if not name:
            continue
        start = 0
        step = len(name)
        while True:
            idx = line.find(name, start)
            if idx < 0:
                break
            pos.update(range(idx, idx + step))
            start = idx + 1
    return pos


class Locator:
    """多特征字级定位器（只读共享，线程安全）。用法：

        loc = Locator(lm)                    # 载入 LM + 原生 LightGBM 模型
        removed = loc.clean_lines(lines)     # {行号: {位置, ...}}
    """

    # 规则开关（默认值）
    near_w: int = 20
    near_k: int = 4          # 邻域内异常位置数 < 4 → 不删（上下文异常数规则）
    trim: float = 0.5        # 被判串两端用"一遍分数"复核，< 0.5 就撤（边界回撤）
    max_lines: int = 0       # >0 时只处理前 N 行（调试用）

    def __init__(self, lm: LanguageModel, model_path: str = "",
                 pinyin_path: str = PINYIN_TSV, *,
                 _shared: dict | None = None):
        self._init_lm(lm)
        if _shared is not None:
            self.py = _shared["py"]
            self.SYL = self.py.syl
            self.GROUP = self.py.group
            self.m_line = _shared["m_line"]
            self.p1 = _shared["p1"]
            self.p2 = _shared["p2"]
            self.thr_line = _shared["thr_line"]
            self.thr_char = _shared["thr_char"]
        else:
            self.py = _Pinyin(pinyin_path)
            self.SYL = self.py.syl
            self.GROUP = self.py.group
            self.m_line, self.p1, self.p2, self.thr_line, self.thr_char = (
                _load_native_models(model_path)
            )
        uni_freq = self.uni
        self.GMAX = {s: max(uni_freq.get(x, 0) for x in g)
                     for s, g in self.GROUP.items() if g}

    def _init_lm(self, lm: LanguageModel) -> None:
        self.lm = lm
        self.uni, self.bi, self.tri, self.total = lm.uni, lm.bi, lm.tri, lm.total
        self.logp = lm.logp

    def clone_with_lm(self, lm: LanguageModel) -> "Locator":
        """复用已加载的只读 Booster 与拼音表，以新语言模型构造隔离副本。"""
        obj = Locator(
            lm,
            _shared={
                "py": self.py,
                "m_line": self.m_line,
                "p1": self.p1,
                "p2": self.p2,
                "thr_line": self.thr_line,
                "thr_char": self.thr_char,
            },
        )
        obj.near_w = self.near_w
        obj.near_k = self.near_k
        obj.trim = self.trim
        obj.max_lines = self.max_lines
        return obj

    # ------------------------------------------------------------ 基础量
    def seam_of(self, chars, i):
        n = len(chars)
        ch = chars[i]
        L = chars[i - 1] if i else BOS
        R = chars[i + 1] if i + 1 < n else EOS
        return _l1(self.bi.get(L + R, 0)) - max(_l1(self.bi.get(L + ch, 0)),
                                                _l1(self.bi.get(ch + R, 0)))

    def py_disguise(self, ch: str) -> float:
        s = self.SYL.get(ch)
        if not s:
            return 0.0
        comm = self.GMAX.get(s)
        if comm is None:
            return 0.0
        return _l1(comm) - _l1(self.uni.get(ch, 0))

    def full_book_stats(self, lines):
        ssum = collections.defaultdict(float)
        bsum = collections.defaultdict(float)
        cnt = collections.Counter()
        bi, l1 = self.bi, _l1
        is_cjk = cc.is_cjk
        for line in lines:
            if not line:
                continue
            chars = list(line)
            n = len(chars)
            for i, ch in enumerate(chars):
                if not (is_cjk(ch) or (ch.isascii() and ch.isalnum())):
                    continue
                L = chars[i - 1] if i else BOS
                R = chars[i + 1] if i + 1 < n else EOS
                bl = l1(bi.get(L + ch, 0))
                br = l1(bi.get(ch + R, 0))
                ssum[ch] += l1(bi.get(L + R, 0)) - max(bl, br)
                bsum[ch] += max(bl, br)
                cnt[ch] += 1
        return ({c: ssum[c] / cnt[c] for c in cnt},
                {c: bsum[c] / cnt[c] for c in cnt}, cnt)

    # ------------------------------------------------------------ 特征
    def build_features(self, a, ai, MS, MB, CN, *, stage1_only: bool = False) -> np.ndarray:
        chars = list(a[ai])
        n = len(chars)
        uni, bi, tri, total = self.uni, self.bi, self.tri, self.total
        l1, logp = _l1, self.logp
        br = 0.0
        tot = 0
        prev = BOS
        for ch in chars:
            if cc.is_cjk(ch) and cc.is_cjk(prev):
                tot += 1
                if bi.get(prev + ch, 0) == 0:
                    br += 1
            prev = ch
        br = br / tot if tot else 0.0
        nb = 0
        for ch in chars:
            o = ord(ch)
            if o > 0xFFFF or (ch.isascii() and (ch.isalnum() or ch in HALF)):
                nb += 1
            elif ch in DECOR or (CJK_SYM.match(ch) and ch not in BODY_PUNCT) \
                    or cc.is_invisible(ch):
                nb += 1
        nb = nb / max(1, n)

        seams = [self.seam_of(chars, i) for i in range(n)]
        line_max = max(seams) if seams else 0.0
        SYL = self.SYL
        if not stage1_only:
            syls = [SYL.get(c) for c in chars]
            disguises = [self.py_disguise(c) for c in chars]
        else:
            syls = None
            disguises = None

        X = np.zeros((n, N_FEAT), dtype=np.float64)
        for i, ch in enumerate(chars):
            o = ord(ch)
            L = chars[i - 1] if i else BOS
            R = chars[i + 1] if i + 1 < n else EOS
            L2 = chars[i - 2] if i >= 2 else BOS
            R2 = chars[i + 2] if i + 2 < n else EOS
            f = uni.get(ch, 0)
            bl, brr = bi.get(L + ch, 0), bi.get(ch + R, 0)
            bj = bi.get(L + R, 0)
            fv = X[i]
            fv[IDX["不可见"]] = 1.0 if (cc.is_invisible(ch) or o in
                                     (0x200b, 0x200c, 0x200d, 0x2060, 0xfeff)) else 0.0
            fv[IDX["非BMP"]] = 1.0 if o > 0xFFFF else 0.0
            fv[IDX["拉丁数字"]] = 1.0 if (ch.isascii() and ch.isalnum()) else 0.0
            fv[IDX["半角标点"]] = 1.0 if (ch.isascii() and ch in HALF) else 0.0
            fv[IDX["装饰符"]] = 1.0 if ch in DECOR else 0.0
            fv[IDX["非常用CJK符号"]] = 1.0 if (CJK_SYM.match(ch) and ch not in BODY_PUNCT) else 0.0
            fv[IDX["数字同音字"]] = 1.0 if ch in cc.DIGIT_HOMOPHONE_CHARS else 0.0
            fv[IDX["稀见同音字"]] = 1.0 if (ch in cc.DIGIT_HOMOPHONE_CHARS and f <= 2000) else 0.0
            fv[IDX["字频log"]] = l1(f)
            fv[IDX["左搭配log"]] = l1(bl)
            fv[IDX["右搭配log"]] = l1(brr)
            fv[IDX["接缝log"]] = l1(bj)
            fv[IDX["trigram最小"]] = l1(min(tri.get(L2 + L + ch, 0), tri.get(L + ch + R, 0),
                                            tri.get(ch + R + R2, 0)))
            fv[IDX["接缝相对分"]] = seams[i]
            fv[IDX["左搭配PMI"]] = l1(bl) - math.log1p(uni.get(L, 0) * max(f, 1) / total)
            fv[IDX["右搭配PMI"]] = l1(brr) - math.log1p(uni.get(R, 0) * max(f, 1) / total)
            fv[IDX["接缝PMI"]] = l1(bj) - math.log1p(uni.get(L, 0) * uni.get(R, 0) / total)
            fv[IDX["同字接缝均值"]] = MS.get(ch, 0.0)
            fv[IDX["本位置接缝偏离"]] = seams[i] - MS.get(ch, 0.0)
            fv[IDX["同字搭配均值"]] = MB.get(ch, 0.0)
            fv[IDX["同字频次log"]] = l1(CN.get(ch, 0))
            fv[IDX["行搭配断裂"]] = br
            fv[IDX["行非正文占比"]] = nb
            fv[IDX["行内位置"]] = i / max(1, n)
            fv[IDX["行内最大接缝"]] = line_max
            if stage1_only:
                continue
            sl = syls[i]
            fv[IDX["左同音"]] = 1.0 if (sl and SYL.get(L) == sl) else 0.0
            fv[IDX["右同音"]] = 1.0 if (sl and SYL.get(R) == sl) else 0.0
            lo, hi = max(0, i - 10), min(n, i + 11)
            fv[IDX["邻域同音密度"]] = (syls[lo:hi].count(sl) / 10.0) if sl else 0.0
            fv[IDX["伪装度"]] = disguises[i]
            fv[IDX["邻域伪装度"]] = sum(disguises[lo:hi]) / (hi - lo)
            TH = 60
            fv[IDX["左成词"]] = 1.0 if bi.get(L + ch, 0) >= TH else 0.0
            fv[IDX["右成词"]] = 1.0 if bi.get(ch + R, 0) >= TH else 0.0
            fv[IDX["接缝成词"]] = 1.0 if bi.get(L + R, 0) >= TH else 0.0
            fv[IDX["三字词中间"]] = 1.0 if tri.get(L + ch + R, 0) >= 5 else 0.0
            d1 = (logp(L2, L, R) + logp(L, R, R2)
                  - logp(L2, L, ch) - logp(L, ch, R) - logp(ch, R, R2))
            d2 = d1 + (logp(R2, R, L) + logp(R, L, L2)
                       - logp(R2, R, ch) - logp(R, ch, L) - logp(ch, L, L2))
            fv[IDX["单向Δ"]] = d1
            fv[IDX["双向Δ"]] = d2
        return X

    def _complete_keep_features(self, line: str, cached_s1: np.ndarray) -> np.ndarray:
        """基于已缓存的一遍 S1(25维) 特征补齐 D/E/G(11维)，输出完整 KEEP(36维) 矩阵。"""
        chars = list(line)
        n = len(chars)
        F = np.zeros((n, len(KEEP)), dtype=np.float64)
        F[:, _S1_POS_IN_KEEP] = cached_s1

        uni, bi, tri = self.uni, self.bi, self.tri
        logp = self.logp
        SYL = self.SYL
        syls = [SYL.get(c) for c in chars]
        disguises = [self.py_disguise(c) for c in chars]

        k_lhom = _KEEP_INDEX[IDX["左同音"]]
        k_rhom = _KEEP_INDEX[IDX["右同音"]]
        k_nhom = _KEEP_INDEX[IDX["邻域同音密度"]]
        k_disg = _KEEP_INDEX[IDX["伪装度"]]
        k_ndisg = _KEEP_INDEX[IDX["邻域伪装度"]]
        k_lw = _KEEP_INDEX[IDX["左成词"]]
        k_rw = _KEEP_INDEX[IDX["右成词"]]
        k_jw = _KEEP_INDEX[IDX["接缝成词"]]
        k_tw = _KEEP_INDEX[IDX["三字词中间"]]
        k_d1 = _KEEP_INDEX[IDX["单向Δ"]]
        k_d2 = _KEEP_INDEX[IDX["双向Δ"]]
        TH = 60

        for i, ch in enumerate(chars):
            L = chars[i - 1] if i else BOS
            R = chars[i + 1] if i + 1 < n else EOS
            L2 = chars[i - 2] if i >= 2 else BOS
            R2 = chars[i + 2] if i + 2 < n else EOS
            fv = F[i]
            sl = syls[i]
            fv[k_lhom] = 1.0 if (sl and SYL.get(L) == sl) else 0.0
            fv[k_rhom] = 1.0 if (sl and SYL.get(R) == sl) else 0.0
            lo, hi = max(0, i - 10), min(n, i + 11)
            fv[k_nhom] = (syls[lo:hi].count(sl) / 10.0) if sl else 0.0
            fv[k_disg] = disguises[i]
            fv[k_ndisg] = sum(disguises[lo:hi]) / (hi - lo)
            fv[k_lw] = 1.0 if bi.get(L + ch, 0) >= TH else 0.0
            fv[k_rw] = 1.0 if bi.get(ch + R, 0) >= TH else 0.0
            fv[k_jw] = 1.0 if bi.get(L + R, 0) >= TH else 0.0
            fv[k_tw] = 1.0 if tri.get(L + ch + R, 0) >= 5 else 0.0
            d1 = (logp(L2, L, R) + logp(L, R, R2)
                  - logp(L2, L, ch) - logp(L, ch, R) - logp(ch, R, R2))
            d2 = d1 + (logp(R2, R, L) + logp(R, L, L2)
                       - logp(R2, R, ch) - logp(R, ch, L) - logp(ch, L, L2))
            fv[k_d1] = d1
            fv[k_d2] = d2
        return F

    # ------------------------------------------------------------ 书级门禁
    def book_gate(self, text_or_lines):
        """书级门禁：只在**确定性证据**成立时才让统计引擎介入。

        支持传入全书字符串或已切分的行列表，避免无意义的 join + split 开销。
        """
        from .ads import ad_line_reason
        lines = text_or_lines.split("\n") if isinstance(text_or_lines, str) else text_or_lines
        inv = 0
        nb = 0
        tag = 0
        cnt: collections.Counter = collections.Counter()
        text_len = max(0, len(lines) - 1) if not isinstance(text_or_lines, str) else len(text_or_lines)
        is_inv = cc.is_invisible
        for ln in lines:
            if not isinstance(text_or_lines, str):
                text_len += len(ln)
            if not ln:
                continue
            if ad_line_reason(ln) or STRUCT_ALL.search(ln):
                tag += 1
            for c in ln:
                if is_inv(c) or c in "\u200b\u200c\u200d\u2060\ufeff":
                    inv += 1
                if ord(c) > 0xFFFF:
                    nb += 1
                cnt[c] += 1
        rare = 0
        for c, k in cnt.items():
            if 0x4E00 <= ord(c) <= 0x9FFF and self.uni.get(c, 0) <= 120 \
                    and self.lm.best_pair.get(c, 0) <= 2:
                rare += k
        rare10k = 10000 * rare / max(1, text_len)
        reasons = []
        if inv >= 20:
            reasons.append("不可见字符 %d" % inv)
        if nb >= 5:
            reasons.append("非BMP %d" % nb)
        if tag >= 5:
            reasons.append("广告行 %d" % tag)
        if rare10k >= 5.0:
            reasons.append("稀见插入字密度 %.1f/万" % rare10k)
        return reasons, dict(invisible=inv, nonbmp=nb, tagline=tag,
                             rare_per_10k=rare10k)

    # ------------------------------------------------------------ 主流程
    def clean_lines(self, lines, progress=None, ad_line=None, names=(),
                    cancel_check=None):
        """返回 ({行号: {位置}}, 元信息) —— 只删不改，完全无状态。

        ad_line: 可选的 `callable(line)->str|None`，命中则整行删除
                 （adclean.ads.ad_line_reason）。
        names:   人名/专名白名单，命中的字位在字级定位阶段受保护。
        """
        if cancel_check is not None and cancel_check():
            raise JobCancelledError("任务已取消")
        reasons, prof = self.book_gate(lines)
        deleted: dict[int, set] = collections.defaultdict(set)
        struct_lines = set()

        # ① 确定性删除：整行广告 + 不可见/非BMP
        for li, line in enumerate(lines):
            if cancel_check is not None and (li % 512 == 0) and cancel_check():
                raise JobCancelledError("任务已取消")
            if not line.strip():
                continue
            if ad_line is not None and ad_line(line):
                deleted[li] = set(range(len(line)))
                struct_lines.add(li)
                continue
            if STRUCT_ALL.search(line) and len(line) <= 200:
                deleted[li] = set(range(len(line)))
                struct_lines.add(li)
                continue
            inv = invisible_at(line) | nonbmp_at(line)
            if inv:
                deleted[li] |= inv
        if not reasons:
            return dict(deleted), dict(gated=False, reasons=[], **prof)

        if cancel_check is not None and cancel_check():
            raise JobCancelledError("任务已取消")

        # ② 全书累积
        MS, MB, CN = self.full_book_stats(lines)

        # ③ 段落级筛查（仅计算 S1 所需的 25 维特征，并缓存命中行的 S1 切片供阶段 ④ 复用）
        # progress 回调约定是 progress(done, total) 两个参数，不可改签名。
        flag = set()
        s1_cache: dict[int, np.ndarray] = {}
        buf, bl = [], []
        idxs = [li for li, x in enumerate(lines) if x.strip()]
        if self.max_lines:
            idxs = idxs[:self.max_lines]
        work_total = max(1, len(idxs) * 2)
        done = 0
        for k, li in enumerate(idxs):
            if cancel_check is not None and (k % 256 == 0) and cancel_check():
                raise JobCancelledError("任务已取消")
            F = self.build_features(lines, li, MS, MB, CN, stage1_only=True)[:, S1]
            buf.append(F)
            bl.append(li)
            if len(buf) >= 1500:
                P = _predict_pos_proba(self.m_line, np.vstack(buf))
                o = 0
                for b_, Fx in zip(bl, buf):
                    if float(P[o:o + len(Fx)].max()) >= self.thr_line:
                        flag.add(b_)
                        if b_ not in struct_lines:
                            s1_cache[b_] = Fx
                    o += len(Fx)
                buf, bl = [], []
                done = k + 1
                if progress:
                    progress(done, work_total)
        if buf:
            P = _predict_pos_proba(self.m_line, np.vstack(buf))
            o = 0
            for b_, Fx in zip(bl, buf):
                if float(P[o:o + len(Fx)].max()) >= self.thr_line:
                    flag.add(b_)
                    if b_ not in struct_lines:
                        s1_cache[b_] = Fx
                o += len(Fx)
            done = len(idxs)
            if progress:
                progress(done, work_total)

        # ④ 字级二遍 + 两条精度规则
        todo = sorted(flag - struct_lines)
        step2 = max(1, len(todo) // 200) if todo else 1
        for j, li in enumerate(todo):
            if cancel_check is not None and (j % 32 == 0) and cancel_check():
                raise JobCancelledError("任务已取消")
            line = lines[li]
            cached = s1_cache.pop(li, None)
            if cached is not None:
                F = self._complete_keep_features(line, cached)
            else:
                F = self.build_features(lines, li, MS, MB, CN)[:, KEEP]
            s1 = _predict_pos_proba(self.p1, F)
            n = len(line)
            nlv = np.zeros((n, 1))
            nrv = np.zeros((n, 1))
            nlv[1:, 0] = s1[:-1]
            nrv[:-1, 0] = s1[1:]
            s2 = _predict_pos_proba(self.p2, np.hstack([F, nlv, nrv]))
            hit = {i for i in range(n) if s2[i] >= self.thr_char}

            if self.near_k > 0 and hit:
                # 上下文异常数：孤立的单个可疑字不删
                arr = np.array([1 if i in hit else 0 for i in range(n)])
                csum = np.concatenate([[0], np.cumsum(arr)])
                idxs2 = sorted(hit)
                keep = set()
                for i in idxs2:
                    lo = max(0, i - self.near_w)
                    hi = min(n, i + self.near_w + 1)
                    if int(csum[hi] - csum[lo]) - 1 >= self.near_k:
                        keep.add(i)
                hit = keep

            if self.trim > 0 and hit:
                # 边界回撤：用不看邻居的一遍分数复核被判串的两端
                hit = {i for i in hit
                       if not ((i - 1 not in hit or i + 1 not in hit)
                               and s1[i] < self.trim)}

            force_del = invisible_at(line) | nonbmp_at(line)
            # 颜文字整组保护 → 人名/专名白名单保护 → 字母数字 token 完整性 → 确定性不可见/非BMP字符永远删
            hit -= kao_mask(line)
            if names:
                hit -= _name_positions(line, names)
            hit = token_fix(line, hit)
            hit |= force_del
            if hit:
                deleted[li] = set(hit)
            if progress and (j % step2 == 0 or j == len(todo) - 1):
                progress(len(idxs) + j + 1, work_total)
        if progress:
            progress(work_total, work_total)      # 收尾，保证进度条到 100%
        return dict(deleted), dict(gated=True, reasons=reasons, **prof)

    def clean_text(self, lines, ad_line=None, progress=None, names=(),
                   cancel_check=None):
        """返回 (新行列表, 元信息)。"""
        deleted, meta = self.clean_lines(
            lines, progress=progress, ad_line=ad_line,
            names=names, cancel_check=cancel_check)
        out = []
        for li, line in enumerate(lines):
            d = deleted.get(li)
            out.append("".join(c for i, c in enumerate(line)
                               if not (d and i in d)))
        meta["deleted_chars"] = sum(len(v) for v in deleted.values())
        meta["changed_lines"] = len(deleted)
        return out, meta


__all__ = ["Locator", "LocatorEngine", "FEATS", "KEEP", "S1", "IDX", "FAMILY",
           "N_FEAT", "DEFAULT_MODEL", "invisible_at", "nonbmp_at"]


# ---------------------------------------------------------------- 引擎适配
class _LineResult:
    __slots__ = ("original", "cleaned", "removed", "reason")

    def __init__(self, line: str, removed):
        self.original = line
        self.removed = tuple(sorted(removed))
        rs = set(self.removed)
        self.cleaned = "".join(c for i, c in enumerate(line) if i not in rs)
        self.reason = "locate"

    @property
    def changed(self) -> bool:
        return bool(self.removed)


class LocatorEngine:
    """把 :class:`Locator` 包装成与 V4/V3 引擎同接口的对象（无状态请求隔离）。

    接口：clean_line / clean_book / _line_score / _protect_mask / cfg / lm / _lm_path
    """

    def __init__(self, lm: LanguageModel, model_path: str = "",
                 overrides: dict | None = None, lm_path: str = "",
                 *, _loc: Locator | None = None):
        from types import SimpleNamespace
        self.loc = _loc if _loc is not None else Locator(lm, model_path)
        self.lm = lm
        self._lm_path = lm_path
        self.cfg = SimpleNamespace(
            kana_safe=True,          # 假名不报（审查层用）
            use_structure=False,     # 结构证据由 V4 引擎出；这里不重复
            names=(),
        )
        for k, v in (overrides or {}).items():
            if hasattr(self.loc, k):
                setattr(self.loc, k, v)
            else:
                setattr(self.cfg, k, v)

    def with_context(self, *, lm: LanguageModel | None = None,
                     names=None, lm_path: str | None = None) -> "LocatorEngine":
        """构造请求级轻量视图，复用只读 Booster 模型且绝不改写共享实例状态。"""
        eff_lm = self.lm if lm is None else lm
        eff_loc = self.loc.clone_with_lm(eff_lm) if lm is not None else self.loc
        eff_names = tuple(names) if names is not None else getattr(self.cfg, "names", ())
        eng = LocatorEngine(
            eff_lm,
            lm_path=self._lm_path if lm_path is None else lm_path,
            _loc=eff_loc,
        )
        eng.cfg.kana_safe = getattr(self.cfg, "kana_safe", True)
        eng.cfg.use_structure = getattr(self.cfg, "use_structure", False)
        eng.cfg.names = eff_names
        return eng

    # -------- 主干：整本书跑一遍（完全无状态，绝不写共享缓存或原地改写 self.cfg）
    def clean_book(self, lines, progress=None, *,
                   names=None, whitelist_words=None,
                   cancel_check=None) -> dict:
        from .ads import ad_line_reason
        if names is not None or whitelist_words is not None:
            merged: list[str] = []
            for seq in (names or (), whitelist_words or ()):
                for w in seq:
                    if w and w not in merged:
                        merged.append(w)
            eff_names = tuple(merged)
        else:
            eff_names = getattr(self.cfg, "names", ())

        deleted, _meta = self.loc.clean_lines(
            lines, progress=progress, ad_line=ad_line_reason,
            names=eff_names, cancel_check=cancel_check)
        out: dict = {}
        text_score = self.lm.text_score
        for li, ds in deleted.items():
            if not ds:
                continue
            line = lines[li]
            before = text_score(line)
            if len(ds) >= len(line):
                after = 0.0
            else:
                kept = "".join(c for i, c in enumerate(line) if i not in ds)
                after = text_score(kept) if kept else 0.0
            out[li] = (frozenset(ds), float(after - before))
        return out

    # -------- 单行接口（完全无状态，不依赖 id(line) 或整书缓存）
    def clean_line(self, line: str, trace=None, *, names=None) -> _LineResult:
        from .ads import ad_line_reason
        rm: set[int] = set()
        if ad_line_reason(line):
            rm = set(range(len(line)))
        elif STRUCT_ALL.search(line) and len(line) <= 200:
            rm = set(range(len(line)))
        else:
            rm |= invisible_at(line) | nonbmp_at(line)
        return _LineResult(line, rm)

    # -------- 自然度分数（越高越像中文；供 audit 出证据/算 margin）
    def _line_score(self, chars, removed) -> float:
        rs = set(removed)
        kept = "".join(c for i, c in enumerate(chars) if i not in rs)
        if not kept:
            return 0.0
        return self.lm.text_score(kept)

    # -------- 保护掩码（审查层用：这些位置不参与"可疑"统计）
    # 必须是 list[bool]：audit.statistical_conf 会按位置下标访问
    def _protect_mask(self, chars, names=None) -> list:
        n = len(chars)
        mask = [False] * n
        eff_names = getattr(self.cfg, "names", ()) if names is None else names
        if not eff_names or n == 0:
            return mask
        line = "".join(chars)
        for idx in _name_positions(line, eff_names):
            mask[idx] = True
        return mask

    def suspicion(self, chars, mask, i):
        return 0.0
