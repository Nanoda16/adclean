# -*- coding: utf-8 -*-
"""
字符分类：识别"中文小说里不该出现或极少出现"的字符，以及广告锚点。

本模块只给出**候选类别**，不直接决定删除；正常小说里确实会出现日文歌词、
俄语舰名、颜文字、希腊字母，最终是否删除由 engine 的评分决定。
"""
from __future__ import annotations

import unicodedata

# ---------------------------------------------------------------- 不可见 / 控制字符
# 这些字符在任何情况下都不该留在正文里，可无条件删除。
BIDI_CHARS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c")


def is_invisible(ch: str) -> bool:
    """零宽、方向控制、组合用附加符、变体选择符、私用区。

    必须排除 `\\r` `\\n`：删除它们会把整本书压成一行。
    """
    o = ord(ch)
    if ch in ("\r", "\n"):
        return False
    if ch in BIDI_CHARS:
        return True
    if 0x200B <= o <= 0x200F:      # ZWSP/ZWNJ/ZWJ/LRM/RLM
        return True
    if 0x2060 <= o <= 0x2064:      # word joiner / invisible ops / invisible separator
        return True
    if 0x206A <= o <= 0x206F:      # deprecated format chars
        return True
    if o in (0xFEFF, 0x00AD, 0x180E):
        return True
    if 0xFE00 <= o <= 0xFE0F:      # variation selectors
        return True
    if 0xE0100 <= o <= 0xE01EF:
        return True
    if 0xE000 <= o <= 0xF8FF:      # private use
        return True
    if 0xF0000 <= o <= 0x10FFFD:
        return True
    if 0x0300 <= o <= 0x036F:      # combining diacritical marks
        return True
    if 0x1AB0 <= o <= 0x1AFF or 0x1DC0 <= o <= 0x1DFF:
        return True
    if 0x20D0 <= o <= 0x20FF:
        return True
    cat = unicodedata.category(ch)
    if cat in ("Cf", "Mn", "Me"):
        return True
    if cat == "Cc":
        # 控制字符：除换行外都视为不可见噪声（制表符按空白处理，见 is_space）
        return ch not in ("\t",)
    return False


# ---------------------------------------------------------------- 明确可疑的 Unicode 区段
# 中文小说正文几乎不可能自然出现这些区段；出现即高度可疑。
SUSPECT_BLOCKS = (
    (0x1D400, 0x1D7FF),   # Mathematical Alphanumeric Symbols
    (0x1D000, 0x1D3FF),   # Musical / Counting rod / Math symbols
    (0x16A0, 0x16FF),     # Runic
    (0x10330, 0x1034F),   # Gothic
    (0x13A0, 0x13FF),     # Cherokee
    (0xAB70, 0xABBF),     # Cherokee Supplement
    (0x1400, 0x167F),     # Unified Canadian Aboriginal Syllabics
    (0x16F00, 0x16F9F),   # Miao / Bamum supplement
    (0xA500, 0xA63F),     # Vai
    (0x0250, 0x02AF),     # IPA Extensions
    (0x1D00, 0x1D7F),     # Phonetic Extensions
    (0x2C60, 0x2C7F),     # Latin Extended-C
    (0xA720, 0xA7FF),     # Latin Extended-D
    (0xAB00, 0xAB6F),     # Latin Extended-E
    (0x1E00, 0x1EFF),     # Latin Extended Additional
    (0x0180, 0x024F),     # Latin Extended-B
    (0x0530, 0x058F),     # Armenian
    (0x10A0, 0x10FF),     # Georgian
    (0x1200, 0x137F),     # Ethiopic
    (0x0E00, 0x0E7F),     # Thai
    (0x0900, 0x097F),     # Devanagari
    (0x0590, 0x05FF),     # Hebrew
    (0x2E80, 0x2EFF),     # CJK Radicals Supplement
    (0x31C0, 0x31EF),     # CJK Strokes
    (0x3000, 0x303F),     # CJK punctuation — 由标点规则单独处理
)


def in_suspect_block(ch: str) -> bool:
    o = ord(ch)
    for lo, hi in SUSPECT_BLOCKS:
        if lo <= o <= hi:
            if 0x3000 <= o <= 0x303F:
                return False   # 中日标点常被正常使用，不在此判定
            return True
    return False


# 常被广告当作"数字/字母"使用的装饰符号：单独出现可能正常，连续成串或夹在汉字中可疑。
DECOR = set("⊙△▲▼◆■●☆★◇◎◈※†‡〆〓〰〱〲〳〴〵〶〷〸〹〺〻〼〽〾〿^~`|\\/_=+*#@$%&<>{}[]()〈〉〖〗〘〙〚〛‐‑‒–——⋯…‰′″")

# 会被广告用来当"0-9 / 字母"的 CJK 及符号（仅作加分项，不作唯一依据）
AD_LIKE_CJK = set(
    "罒玖弍峮箘帬裠囷宭踆鏾児侕迩貳爾栮洱倭氿镹鸠疚揪湫僦翏熘遛硫陸箥紦覇坝"
    "衤彡乂氵灬鈴龄翎鈴零令铃"
)

# 数字 0-9 的常见同音/近音字：广告把群号每个数字替换成同音字，
# 因此短时间内出现多个同音字是群号广告的强信号。
HOMOPHONE_DIGITS = {
    "0": "零令铃龄翎玲淋霖临邻林琳伶聆岭〇",
    "1": "衣依伊医姨夷怡椅翼亦忆艺壹揖咿",
    "2": "二贰貳弍爾尔迩洱栮侕饵珥",
    "3": "三伞散叁彡氵杉",
    "4": "四似寺肆思斯私伺饲泗驷祀姒",
    "5": "五伍捂吾舞雾务勿悟戊",
    "6": "六溜熘遛陆柳硫琉鎏鹨锍",
    "7": "七漆柒齐其奇气器弃泣契旗琪棋骑祈祁崎琦",
    "8": "八吧巴芭坝拔罢霸扒紦粑靶",
    "9": "九久玖酒旧究揪鸠疚韭灸咎纠",
}

DIGIT_HOMOPHONE_CHARS = set("".join(HOMOPHONE_DIGITS.values()))


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF)


def is_cjk_punct(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x303F) or (0xFF00 <= o <= 0xFFEF) or ch in "，。！？、；：“”‘’（）《》【】〈〉「」『』—…·～"


ASCII_LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
ASCII_DIGITS = set("0123456789")
ASCII_PUNCT = set(".,!?;:'\"()[]{}<>-_/\\|@#$%^&*+=~`")


def is_space(ch: str) -> bool:
    return ch in " \t\u3000\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"


def classify(ch: str) -> str:
    """返回粗略类别，用于构建候选区间。"""
    if is_invisible(ch):
        return "invisible"
    if is_space(ch):
        return "space"
    if is_cjk(ch):
        return "cjk"
    if is_cjk_punct(ch):
        return "cjkpunct"
    if ch in DECOR:
        return "decor"
    if in_suspect_block(ch):
        return "suspect"
    o = ord(ch)
    # 希腊 / 西里尔 / 假名 / 谚文只在上下文中判定
    if 0x0370 <= o <= 0x03FF or 0x1F00 <= o <= 0x1FFF:
        return "greek"
    if 0x0400 <= o <= 0x052F:
        return "cyrillic"
    if 0x3040 <= o <= 0x30FF:
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
        return "hangul"
    if ch in ASCII_LETTERS:
        return "latin"
    if ch in ASCII_DIGITS:
        return "digit"
    if ch in ASCII_PUNCT:
        return "asciipunct"
    if 0x2460 <= o <= 0x24FF:
        return "enclosed"
    if 0x2190 <= o <= 0x21FF or 0x27F0 <= o <= 0x27FF:
        return "arrow"
    if 0x2600 <= o <= 0x26FF or 0x2700 <= o <= 0x27BF:
        return "symbol"
    if 0x0080 <= o <= 0x00FF:
        return "latinext"
    return "other"


# 视为"可被广告插入"的类别：这些字符允许出现在正常小说里，
# 但夹进汉字词组中间会破坏连贯度，交由 bigram 评分裁决。
INSERTABLE = {
    "invisible", "decor", "suspect", "greek", "cyrillic", "kana", "hangul",
    "latin", "digit", "asciipunct", "enclosed", "arrow", "symbol",
    "latinext", "other",
}
# 绝不作为广告删除的类别（汉字、中日标点、空格）
NEVER_REMOVE = {"cjk", "cjkpunct", "space"}

TRIGGER = INSERTABLE | {"cjk"}   # cjk 需配合稀有度判定
