# -*- coding: utf-8 -*-
"""繁→简 转换表的回归测试。

判据：候选里含源字本身就不转换。
"""
from __future__ import annotations

import os
import sys
import unicodedata

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.t2s import (char_table, has_traditional, phrase_table,
                         table_size, to_simplified)

# 常见**简体**字：转换后必须原样不变
IDENTITY = (
    "蒙瑙的了我你他在有不一人这中大为上个个国说们来时后气关长车东马鸟鱼"
    "风飞龙与写读听见问语谁赞干面条发里么什怎样东西南北天地日月水火木金土"
    "心手足口耳目头脸脑红绿蓝黄银钱财贵贱买卖货质资费价格爱恨怒喜欢痛悲"
    "伤离梦灵团园圆图难权铁钢钟阳阴云电雾声静愿医"
)

# 这几个字在简体中合法，**绝不能**被转换
KEEP_IN_SIMPLIFIED = "覆著藉乾於麽幺"
KEEP_WORDS = ("覆盖", "著名", "显著", "颠覆", "覆灭", "幺妹", "慰藉", "狼藉",
              "乾坤", "乾隆", "於姓")


def test_common_simplified_chars_are_untouched():
    assert to_simplified(IDENTITY) == IDENTITY


def test_simplified_words_are_untouched():
    """这些词在简体里就是这么写的，不得转换。"""
    for w in KEEP_WORDS:
        assert to_simplified(w) == w, (w, to_simplified(w))


def test_known_pairs_convert():
    for trad, simp in (("說", "说"), ("關", "关"), ("們", "们"), ("個", "个"),
                       ("國", "国"), ("學", "学"), ("實", "实"), ("現", "现"),
                       ("點", "点"), ("發", "发"), ("過", "过"), ("樣", "样"),
                       ("種", "种"), ("產", "产"), ("經", "经"), ("應", "应"),
                       ("該", "该"), ("體", "体"), ("樂", "乐"), ("書", "书"),
                       ("車", "车"), ("長", "长"), ("風", "风"), ("龍", "龙"),
                       ("與", "与"), ("鐵", "铁"), ("電", "电"), ("聲", "声"),
                       ("頭", "头"), ("臉", "脸"), ("腦", "脑"), ("紅", "红"),
                       ("錢", "钱"), ("愛", "爱"), ("夢", "梦"), ("靈", "灵"),
                       ("難", "难"), ("權", "权"), ("陽", "阳"), ("雲", "云"),
                       ("霧", "雾"), ("醫", "医"), ("讚", "赞"),
                       ("濛", "蒙"), ("懞", "蒙"), ("矇", "蒙")):
        assert to_simplified(trad) == simp, (trad, simp, to_simplified(trad))


def test_ambiguous_chars_need_phrase_level():
    """歧义字字级不转，但常见**词**必须转对。"""
    assert to_simplified("乾淨") == "干净"
    assert to_simplified("餅乾") == "饼干"
    assert to_simplified("什麽") == "什么"
    assert to_simplified("那麽") == "那么"
    assert to_simplified("由於") == "由于"
    assert to_simplified("藉口") == "借口"
    assert to_simplified("為甚麼") == "为什么"


def test_no_non_bmp_targets():
    """表的**值**绝不能是非 BMP 字符。"""
    bad = {k: v for k, v in char_table().items() if ord(v) > 0xFFFF}
    assert not bad, "表里出现非 BMP 目标: %s" % list(bad.items())[:5]
    badp = {k: v for k, v in phrase_table().items()
            if any(ord(c) > 0xFFFF for c in v)}
    assert not badp, "短语表出现非 BMP 目标: %s" % list(badp.items())[:5]


def test_no_self_mapping():
    assert not [k for k, v in char_table().items() if k == v]
    assert not [k for k, v in phrase_table().items() if k == v]


def test_ambiguous_chars_never_mapped():
    """含源字自身的歧义条目必须被丢掉。"""
    t = char_table()
    for c in KEEP_IN_SIMPLIFIED:
        assert c not in t, (c, t.get(c))


def test_targets_are_letters_or_symbols():
    for k, v in char_table().items():
        assert len(v) == 1
        assert unicodedata.category(v)[0] in ("L", "S"), (k, v)


def test_idempotent():
    """转两遍必须与转一遍相同（否则会反复改写文本）。"""
    s = "說關們個國學實現點發過樣種產經應該體樂書車長風龍與鐵電聲頭臉腦紅錢愛夢靈難權陽雲霧醫讚濛"
    once = to_simplified(s)
    assert to_simplified(once) == once
    assert once != s


def test_table_size_sane():
    """表大小必须在 1000~6000 之间。"""
    assert 1000 <= table_size() <= 6000


def test_has_traditional():
    assert has_traditional("說關")
    assert not has_traditional("说关")
    assert not has_traditional("覆盖著名幺妹慰藉乾坤")


def test_real_world_line_untouched():
    """真实句子过一遍转换必须一字不改。"""
    line = "水沼清子点点头，赞许道。"
    assert to_simplified(line) == line
    assert to_simplified("红玛瑙般的液体") == "红玛瑙般的液体"
    assert to_simplified("灰蒙蒙的天空") == "灰蒙蒙的天空"
    assert to_simplified("荷尔蒙") == "荷尔蒙"


def test_clean_book_is_barely_touched():
    """干净简体书转换后改动必须 <100 字。"""
    # 样例书不在仓库里；要跑这个测试就用环境变量指定一本干净简体书
    p = os.environ.get("ADCLEAN_SAMPLE_BOOK", "")
    if not p or not os.path.exists(p):
        pytest.skip("未设置 ADCLEAN_SAMPLE_BOOK（一本干净简体 txt）")
    with open(p, encoding="utf-8-sig") as f:
        t = f.read()
    u = to_simplified(t)
    changed = sum(1 for a, b in zip(t, u) if a != b)
    assert len(u) == len(t)
    assert changed < 100, "干净简体书被改动了 %d 字（旧表约 172 字）" % changed
    # 这几个词一个都不许动
    for w in ("覆盖", "著名", "显著", "幺", "慰藉", "乾坤"):
        idx = t.find(w)
        if idx >= 0:
            assert u[idx:idx + len(w)] == w, (w, u[idx:idx + len(w)])
