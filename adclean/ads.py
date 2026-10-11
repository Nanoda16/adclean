# -*- coding: utf-8 -*-
"""adclean.ads —— 整行广告的确定性识别（纯模式规则，不含任何字频判据）。

**整行都必须是广告**才在这里删：只要行里还夹着正文就一律放过，
交给统计引擎逐字判断（宁可漏，不可误删正文）。

规则覆盖的形态：HTML/标签行、URL 行、上传者水印、来源声明、网址栏、
群号行、章尾求票、章尾引导、本章完、含域名短行。

对外只有一个函数 :func:`ad_line_reason`，命中返回规则名，未命中返回 ``None``。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 域名
_DOMAIN = (r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,30}[A-Za-z0-9])?\.)"
           r"(?:com|net|cc|org|cn|top|xyz|vip|info|me|tv|app|site|club|online|"
           r"la|io|co|us|biz|pro)(?![A-Za-z])")

# ---------------------------------------------------------------- 正则规则
# (规则名, 正则, 行长度上限)
RULES: list[tuple[str, re.Pattern, int | None]] = [
    ("HTML/标签行", re.compile(
        r"^\s*(?:</?(?:img|p|div|span|br|a|font|table|tr|td)\b"
        r"|<\s*Book\b|\{[^}]{0,40}\"book_id\"\s*:)", re.I), None),
    ("URL 行", re.compile(r"^\s*(?:https?://|www\.)\S*\s*$", re.I), None),
    ("上传者水印", re.compile(
        r"本文件由会员|由会员.{0,30}上传|资源来源于网络|仅供学习交流|"
        r"本书由.{0,20}(?:整理|制作|上传)|扫校|OCR"), 200),
    ("网址栏", re.compile(r"(?:备用)?网址\s*[:：]|备用域名\s*[:：]"), 200),
    ("群号行", re.compile(
        r"(?:QQ|qq|企鹅|书友|交流|读者|粉丝)\s*群\s*[:：]?\s*\d{5,}"
        r"|群号\s*[:：]?\s*\d{5,}"), 200),
    ("本章完", re.compile(
        r"^\s*[（(【\[]?\s*本章完\s*[）)】\]]?\s*[!！。.~～…]*\s*$"), 20),
    ("含域名短行", re.compile(_DOMAIN), 60),
]

# 去掉这些之后还有多少"非广告"残留 —— 残留超限就不算广告行
_PUNCT = re.compile(r"[\s、，。！？~～…,.!?;；:：()（）\[\]【】《》\"'“”‘’<>/\\|@#$%^&*+=—\-_·]+")
_TAIL_WORDS = re.compile(
    r"(?:月票|推荐票|评价票|订阅|收藏|鲜花|打赏|求票|求个?|大家的?|各位|"
    r"读者|书友|感谢|谢谢|支持|拜托|麻烦|希望|喜欢|请|本站|域名|备用|"
    r"最新|章节|更新|更多|精彩|小说|内容|访问|阅读|完|章|了|的|和|与|及|"
    r"尽在|敬请|期待|谢谢大家)")

_POLL_KEY = re.compile(r"月票|推荐票|评价票|订阅|收藏|鲜花|打赏")
_TAIL_KEY = re.compile(
    r"请记住本站|记住本站|本站域名|备用域名|更多精彩.{0,6}小说|"
    r"最新.{0,4}章节.{0,6}(?:更新|请|访问)|感谢.{0,12}(?:支持|阅读).{0,12}"
    r"(?:求|订阅|收藏|月票)|请收藏本站|请牢记")


def _residue(s: str) -> str:
    return _TAIL_WORDS.sub("", _PUNCT.sub("", s))


def _is_poll_line(s: str) -> bool:
    """整行只在求票，且除求票话术外几乎没有别的内容。

    **必须含"求"字**，以此排除作者感言与正文对话。
    """
    if len(s) > 40 or not _POLL_KEY.search(s) or "求" not in s:
        return False
    return len(_residue(s)) <= 3


def _is_tail_line(s: str) -> bool:
    """整行是章节尾引流话术。"""
    if len(s) > 80 or not _TAIL_KEY.search(s):
        return False
    return len(_residue(s)) <= 8


def ad_line_reason(line: str) -> str | None:
    """命中整行广告规则则返回规则名，否则 None。"""
    s = line.strip().strip("\u3000")
    if not s:
        return None
    for name, rx, maxlen in RULES:
        if maxlen is not None and len(s) > maxlen:
            continue
        if name == "含域名短行":
            # 短行 + 域名 + 除域名外几乎没有正文
            m = rx.search(s)
            if not m:
                continue
            rest = _PUNCT.sub("", s[:m.start()] + s[m.end():])
            if len(rest) <= 12 and re.search(
                    r"更多|精彩|小说|更新|章节|本站|记住|网址|域名|下载|阅读|"
                    r"欢迎|加入|群|备用", s):
                return name
            continue
        if rx.search(s):
            return name
    if _is_poll_line(s):
        return "章尾求票"
    if _is_tail_line(s):
        return "章尾引导"
    return None


# ---------------------------------------------------------------- 自检
_MUST_HIT = [
    '<img src="https://c1.example.com/a.jpg">',
    "<Book>12</Book>",
    "https://www.example-novel.com/book/12.html",
    "www.example-novel.com",
    '本文件由会员“12345”上传，资源来源于网络，仅供学习交流。',
    "QQ群：123456789，欢迎加入书友交流群！",
    "网址：www.example-novel.com 备用网址：www.example2.com",
    "资源来源于网络",
    "求月票、求推荐票！！！",
    "请记住本站域名，章节更新最快。",
    "更多精彩小说，尽在 www.example-novel.com。",
    "本章完，感谢各位读者的支持，求收藏求订阅！",
    "（本章完）",
]
_MUST_MISS = [
    "他求月票的样子很可笑。",
    "“我一定会记住本站的规矩。”少年郑重地说。",
    "她的话还没说完，章节更新就被打断了。",
    "李明的QQ群里，大家正在讨论明天的比赛。",
    "这个网址他记了很多年，网址栏里还留着她的名字。",
    "“本章完”三个字出现在稿纸的最后一页，那是他写的第一本小说。",
    "他抬手把窗帘拉开，阳光洒满了整间屋子。",
    # 作者话/正文，不含"求"，不得删
    "打赏的感谢我没说？",
    "订阅在掉了- -",
    "有月票的支持下，谢谢喵。",
    "感谢大家的间贴月票推荐票！",
]


def selfcheck() -> list[str]:
    """返回不通过的用例（空列表 = 全部通过）。"""
    bad = []
    for s in _MUST_HIT:
        if not ad_line_reason(s):
            bad.append("该命中却未命中: %s" % s)
    for s in _MUST_MISS:
        r = ad_line_reason(s)
        if r:
            bad.append("不该命中却命中(%s): %s" % (r, s))
    return bad


if __name__ == "__main__":
    errs = selfcheck()
    print("自检：%d 条用例，%d 条不通过" % (len(_MUST_HIT) + len(_MUST_MISS),
                                          len(errs)))
    for e in errs:
        print("  ", e)
