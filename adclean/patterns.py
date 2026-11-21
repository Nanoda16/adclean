# -*- coding: utf-8 -*-
"""adclean.patterns —— 共享正则、符号集、保护表、取消异常与基础统计可疑度。

统一存放跨模块共用的常量与只读判定函数，消除对旧引擎（v2engine）的反向耦合。
"""
from __future__ import annotations

import os
import re
import unicodedata

from . import charclass as cc

# ---------------------------------------------------------------- 常量与异常
STRONG_PAIR = 10
BOS = "\u0001"
EOS = "\u0002"


class JobCancelledError(Exception):
    """任务在执行过程中被主动取消时抛出的异常。"""


# ---------------------------------------------------------------- 符号与标点集合
BODY_PUNCT = set("，。、！？；：“”‘’（）【】《》〈〉「」『』〔〕—…·～‰　・ー─━·.")
DECOR_LOCATE = set(
    "☆★♪♫♥❤♦◆●○■□▲△▼▽◇◎→←↑↓↗↘⇔※〓〰〱〲〳〴〵〶〷〸〹〺〻〼〽〾〿"
    "①②③④⑤⑥⑦⑧⑨⑩αβγδεζηθικλμνξοπρστυφχψω"
    "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя"
)
HALF_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
CJK_SYM_RE = re.compile(r"[\u3000-\u303f\u31c0-\u31ef\ufe30-\ufe4f\uff01-\uff5e]")

HOMO_ALL = cc.DIGIT_HOMOPHONE_CHARS | {"〇"}

# 正常创作里常见的装饰/标点：永不通过“结构异常”通道删除
DECOR_SAFE = set(
    "~～*＊-—–―−_＿/／|｜\\、。，．,．!！?？;；:：'\"‘’“”"
    "()（）[]【】{}｛｝〈〉《》「」『』…⋯·・♪♫♬❤◎○●□■△▲▽▼☆★"
) | {"\u3000", "\u00a0"}


def is_kana(ch: str) -> bool:
    """日文假名（平假名/片假名/半角片假名）。正文里常出现，绝不单独删。"""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF) or (0x31F0 <= o <= 0x31FF) or (0xFF66 <= o <= 0xFF9D)


def is_decor_block(ch: str) -> bool:
    """装饰性 Unicode 区块（制表符/方块/几何图形/箭头/数学符号等），不是广告。"""
    o = ord(ch)
    return (
        0x2500 <= o <= 0x257F
        or 0x2580 <= o <= 0x259F
        or 0x25A0 <= o <= 0x25FF
        or 0x2190 <= o <= 0x21FF
        or 0x2200 <= o <= 0x22FF
        or 0x2600 <= o <= 0x27BF
        or 0x2E00 <= o <= 0x2E7F
        or 0xFE30 <= o <= 0xFE4F
        or 0xFF00 <= o <= 0xFFEF
        or 0x1F300 <= o <= 0x1FAFF
    )


def is_cjk_symbol(ch: str) -> bool:
    """CJK 符号和标点区（U+3000–U+303F）里的非常规符号。"""
    o = ord(ch)
    return 0x3000 <= o <= 0x303F and ch not in DECOR_SAFE


# 兼容旧私有别名
_is_kana = is_kana
_is_decor_block = is_decor_block
_is_cjk_symbol = is_cjk_symbol

# ---------------------------------------------------------------- 默认保护字表
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_PROTECTED_PATH = os.path.join(_DATA_DIR, "protected_chars.txt")


def load_default_protected(path: str = _PROTECTED_PATH) -> frozenset[str]:
    """从 data/protected_chars.txt 读取默认保护字符集合（容忍 CRLF/LF）。"""
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.rstrip("\r\n").strip()
                if s and not s.startswith("#"):
                    out.update(s)
    except OSError:
        pass
    return frozenset(out)


DEFAULT_PROTECTED = load_default_protected()

# ---------------------------------------------------------------- 共享广告/结构正则
DOMAIN_PATTERN = (
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,30}[A-Za-z0-9])?\.)"
    r"(?:com|net|cc|org|cn|top|xyz|vip|info|me|tv|app|site|club|online|"
    r"la|io|co|us|biz|pro)(?![A-Za-z])"
)
DOMAIN_RE = re.compile(DOMAIN_PATTERN)
URL_ONLY_RE = re.compile(r"^\s*(?:https?://|www\.)\S*\s*$", re.I)
TAG_LINE_RE = re.compile(
    r"^\s*(?:</?(?:img|p|div|span|br|a|font|table|tr|td)\b"
    r"|<\s*Book\b|\{[^}]{0,60}\"book_id\"\s*:)",
    re.I,
)
STRUCT_TAG_OR_URL_RE = re.compile(
    r"^\s*(?:</?(?:img|p|div|span|br|a|font|table|tr|td)\b|https?://|www\.|"
    r"<\s*Book\b|\{[^}]*\"book_id\"\s*:)",
    re.I,
)
TAGRE = re.compile(
    r"<img|<Book|https?://|本文件由会员|资源来源于网络|"
    r"QQ\s*群[:：]|书友群|加群|企鹅群|更多精彩|最新章节|"
    r"请记住本站|备用网址|防走失|txt下载|全文阅读"
)
STRUCT_ALL = re.compile(
    r"^\s*(?:</?(?:img|p|div|span|br|a|font|table|tr|td)\b"
    r"|<\s*Book\b|\{[^}]{0,60}\"book_id\"\s*:"
    r"|(?:https?://|www\.)\S*\s*$)",
    re.I,
)


# ---------------------------------------------------------------- 共享统计可疑度
def statistical_conf(lm, chars: list, mask: list, i: int,
                     kana_safe: bool = True, win: int = 15) -> float:
    """位置 i 的统计异常置信度；Audit 证据收集与 V3Engine.suspicion 共用。"""
    n = len(chars)
    ch = chars[i]
    if mask[i] or ch in "\r\n\t" or cc.is_space(ch):
        return 0.0
    cat = unicodedata.category(ch)
    if cc.is_invisible(ch) or cat in ("Cf", "Cc", "Co", "Cs", "Cn"):
        return 10.0
    if kana_safe and is_kana(ch):
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
        if is_cjk_symbol(ch):
            s += 0.6
    return s
