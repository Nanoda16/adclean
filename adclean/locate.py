# -*- coding: utf-8 -*-
"""adclean.locate —— 多特征字级污染定位器（v1 默认统计引擎）。

处理流程：书级门禁 → 段落级筛查 → 字级定位（两遍打分）→ 精度规则。
只删不改，绝不替换、绝不插入。
"""
from __future__ import annotations

import collections
import math
import os
import pickle
import re
import unicodedata

import numpy as np

from . import charclass as cc
from .v2lm import BOS, EOS, LanguageModel

# ---------------------------------------------------------------- 特征定义
BODY_PUNCT = set("，。、！？；：“”‘’（）【】《》〈〉「」『』〔〕—…·～‰　・ー─━·.")
DECOR = set("☆★♪♫♥❤♦◆●○■□▲△▼▽◇◎→←↑↓↗↘⇔※〓〰〱〲〳〴〵〶〷〸〹〺〻〼〽〾〿"
            "①②③④⑤⑥⑦⑧⑨⑩αβγδεζηθικλμνξοπρστυφχψω"
            "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя")
HALF = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
CJK_SYM = re.compile(r"[\u3000-\u303f\u31c0-\u31ef\ufe30-\ufe4f\uff01-\uff5e]")

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

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT_MODEL = os.path.join(DATA_DIR, "locate_model.pkl")
PINYIN_TSV = os.path.join(DATA_DIR, "pinyin.tsv")

# ---------------------------------------------------------------- 确定性痕迹
TAGRE = re.compile(r"<img|<Book|https?://|本文件由会员|资源来源于网络|"
                   r"QQ\s*群[:：]|书友群|加群|企鹅群|更多精彩|最新章节|"
                   r"请记住本站|备用网址|防走失|txt下载|全文阅读")
STRUCT_ALL = re.compile(r"^\s*(?:<|https?://|www\.)|"
                        r"本文件由会员|资源来源于网络|网址[:：]|QQ\s*群[:：]")


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
                    parts = ln.rstrip("\n").split("\t")
                    if len(parts) == 2 and parts[0]:
                        self.syl[parts[0]] = parts[1]
        self.group = collections.defaultdict(list)
        for c, s in self.syl.items():
            self.group[s].append(c)

    def __len__(self) -> int:
        return len(self.syl)


def _l1(x: float) -> float:
    return math.log1p(x)


class Locator:
    """多特征字级定位器。用法：

        loc = Locator()                      # 载入 LM + 模型
        removed = loc.clean_lines(lines)     # {行号: {位置, ...}}
    """

    # 规则开关（默认值）
    near_w: int = 20
    near_k: int = 4          # 邻域内异常位置数 < 4 → 不删（上下文异常数规则）
    trim: float = 0.5        # 被判串两端用"一遍分数"复核，< 0.5 就撤（边界回撤）
    max_lines: int = 0       # >0 时只处理前 N 行（调试用）

    def __init__(self, lm: LanguageModel, model_path: str = "",
                 pinyin_path: str = PINYIN_TSV):
        self.lm = lm
        self.uni, self.bi, self.tri, self.total = lm.uni, lm.bi, lm.tri, lm.total
        self.logp = lm.logp
        self.py = _Pinyin(pinyin_path)
        self.SYL = self.py.syl
        self.GROUP = self.py.group
        # 每个音节的组内最高字频：加载后 uni/GROUP 不再变动，预先算好，
        # 免得 py_disguise 每次都对整个同音组扫一遍。
        uni_freq = self.uni
        self.GMAX = {s: max(uni_freq.get(x, 0) for x in g)
                     for s, g in self.GROUP.items() if g}
        path = model_path or DEFAULT_MODEL
        with open(path, "rb") as fh:
            d = pickle.load(fh)
        self.m_line, self.p1, self.p2 = d["m1"], d["p1"], d["p2"]
        self.thr_line = d["thr_line"]
        self.thr_char = d["thr_char"]

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
        for line in lines:
            if not line:
                continue
            chars = list(line)
            n = len(chars)
            for i, ch in enumerate(chars):
                if not (cc.is_cjk(ch) or (ch.isascii() and ch.isalnum())):
                    continue
                L = chars[i - 1] if i else BOS
                R = chars[i + 1] if i + 1 < n else EOS
                ssum[ch] += self.seam_of(chars, i)
                bsum[ch] += max(l1(bi.get(L + ch, 0)), l1(bi.get(ch + R, 0)))
                cnt[ch] += 1
        return ({c: ssum[c] / cnt[c] for c in cnt},
                {c: bsum[c] / cnt[c] for c in cnt}, cnt)

    # ------------------------------------------------------------ 特征
    def build_features(self, a, ai, MS, MB, CN) -> np.ndarray:
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
        SYL, GROUP = self.SYL, self.GROUP
        # 字级量按行算一遍就够，窗口特征直接查这两个表，不再逐窗重算。
        syls = [SYL.get(c) for c in chars]
        disguises = [self.py_disguise(c) for c in chars]

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
            sl = SYL.get(ch)
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
            fv[IDX["行搭配断裂"]] = br
            fv[IDX["行非正文占比"]] = nb
            fv[IDX["行内位置"]] = i / max(1, n)
            fv[IDX["行内最大接缝"]] = line_max
            d1 = (logp(L2, L, R) + logp(L, R, R2)
                  - logp(L2, L, ch) - logp(L, ch, R) - logp(ch, R, R2))
            d2 = d1 + (logp(R2, R, L) + logp(R, L, L2)
                       - logp(R2, R, ch) - logp(R, ch, L) - logp(ch, L, L2))
            fv[IDX["单向Δ"]] = d1
            fv[IDX["双向Δ"]] = d2
        return X

    # ------------------------------------------------------------ 书级门禁
    def book_gate(self, text: str):
        """书级门禁：只在**确定性证据**成立时才让统计引擎介入。

        判据全是计数。"广告行"只认整行广告规则 / HTML / URL / 群号 / 水印，
        不能用"书友群"这类词组在正文里的出现次数。
        """
        from .ads import ad_line_reason
        inv = sum(1 for c in text if cc.is_invisible(c)
                  or c in "\u200b\u200c\u200d\u2060\ufeff")
        nb = sum(1 for c in text if ord(c) > 0xFFFF)
        tag = 0
        for ln in text.split("\n"):
            if ln and (ad_line_reason(ln) or STRUCT_ALL.search(ln)):
                tag += 1
        cnt = collections.Counter(text)
        rare = 0
        for c, k in cnt.items():
            if 0x4E00 <= ord(c) <= 0x9FFF and self.uni.get(c, 0) <= 120 \
                    and self.lm.best_pair.get(c, 0) <= 2:
                rare += k
        rare10k = 10000 * rare / max(1, len(text))
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
    def clean_lines(self, lines, progress=None, ad_line=None):
        """返回 {行号: {位置}} —— 只删不改。

        ad_line: 可选的 `callable(line)->str|None`，命中则整行删除
                 （adclean.ads.ad_line_reason）。
        """
        text = "\n".join(lines)
        reasons, prof = self.book_gate(text)
        deleted: dict[int, set] = collections.defaultdict(set)
        struct_lines = set()

        # ① 确定性删除：整行广告 + 不可见/非BMP
        for li, line in enumerate(lines):
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

        # ② 全书累积
        MS, MB, CN = self.full_book_stats(lines)

        # ③ 段落级筛查
        # progress 回调的约定是 progress(done, total) 两个参数，不能改成别的签名。
        # 总工作量折算成 2×行数：前半段=段落级筛查，后半段=字级定位。
        flag = set()
        buf, bl = [], []
        idxs = [li for li, x in enumerate(lines) if x.strip()]
        if self.max_lines:
            idxs = idxs[:self.max_lines]
        work_total = max(1, len(idxs) * 2)
        done = 0
        for k, li in enumerate(idxs):
            F = self.build_features(lines, li, MS, MB, CN)[:, S1]
            buf.append(F)
            bl.append(li)
            if len(buf) >= 1500:
                P = self.m_line.predict_proba(np.vstack(buf))[:, 1]
                o = 0
                for b_, Fx in zip(bl, buf):
                    if float(P[o:o + len(Fx)].max()) >= self.thr_line:
                        flag.add(b_)
                    o += len(Fx)
                buf, bl = [], []
                done = k + 1
                # 每批（1500 行）必报一次，不要加取模条件。
                if progress:
                    progress(done, work_total)
        if buf:
            P = self.m_line.predict_proba(np.vstack(buf))[:, 1]
            o = 0
            for b_, Fx in zip(bl, buf):
                if float(P[o:o + len(Fx)].max()) >= self.thr_line:
                    flag.add(b_)
                o += len(Fx)
            done = len(idxs)
            if progress:
                progress(done, work_total)

        # ④ 字级二遍 + 两条精度规则
        todo = sorted(flag - struct_lines)
        step2 = max(1, len(todo) // 200) if todo else 1
        for j, li in enumerate(todo):
            line = lines[li]
            F = self.build_features(lines, li, MS, MB, CN)[:, KEEP]
            s1 = self.p1.predict_proba(F)[:, 1]
            n = len(line)
            nlv = np.zeros((n, 1))
            nrv = np.zeros((n, 1))
            nlv[1:, 0] = s1[:-1]
            nrv[:-1, 0] = s1[1:]
            s2 = self.p2.predict_proba(np.hstack([F, nlv, nrv]))[:, 1]
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

            keep_mask = invisible_at(line) | nonbmp_at(line)
            # 颜文字整组保护 → 字母数字 token 完整性 → 确定性字符永远删
            hit -= kao_mask(line)
            hit = token_fix(line, hit)
            hit = (hit - keep_mask) | keep_mask
            if hit:
                deleted[li] = set(hit)
            if progress and (j % step2 == 0 or j == len(todo) - 1):
                progress(len(idxs) + j + 1, work_total)
        if progress:
            progress(work_total, work_total)      # 收尾，保证进度条到 100%
        return dict(deleted), dict(gated=True, reasons=reasons, **prof)

    def clean_text(self, lines, ad_line=None, progress=None):
        """返回 (新行列表, 元信息)。"""
        deleted, meta = self.clean_lines(lines, progress=progress, ad_line=ad_line)
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
    """把 :class:`Locator` 包装成与 V4/V3 引擎同接口的对象。

    接口：clean_line / clean_book / _line_score / _protect_mask / cfg / lm / _lm_path
    """

    def __init__(self, lm: LanguageModel, model_path: str = "",
                 overrides: dict | None = None, lm_path: str = ""):
        from types import SimpleNamespace
        self.loc = Locator(lm, model_path)
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
        self._cache: dict | None = None
        self._index: dict | None = None
        self.meta: dict = {}

    # -------- 主干：整本书跑一遍
    def clean_book(self, lines, progress=None) -> dict:
        from .ads import ad_line_reason
        self._cache_lines = lines
        self._index = {}
        for i, x in enumerate(lines):
            self._index.setdefault(id(x), i)
        deleted, meta = self.loc.clean_lines(lines, progress=progress,
                                             ad_line=ad_line_reason)
        self._cache = deleted
        self.meta = meta
        out: dict = {}
        for li, ds in deleted.items():
            if not ds:
                continue
            chs = list(lines[li])
            after = self._line_score(chs, tuple(sorted(ds)))
            before = self._line_score(chs, ())
            out[li] = (frozenset(ds), float(after - before))
        return out

    # -------- 单行接口
    def clean_line(self, line: str, trace=None) -> _LineResult:
        if self._cache is not None and self._index:
            i = self._index.get(id(line))
            if i is not None:
                return _LineResult(line, self._cache.get(i, ()))
        from .ads import ad_line_reason
        rm = set()
        if ad_line_reason(line):
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
    def _protect_mask(self, chars) -> list:
        return [False] * len(chars)

    def suspicion(self, chars, mask, i):
        return 0.0
