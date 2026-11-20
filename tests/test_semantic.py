# -*- coding: utf-8 -*-
"""V4 语义引擎的单元测试。

判据是句子级语义正常性，配置里刻意不含任何字频判据。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 生产语言模型：仓库不含（101MB 超过 GitHub 单文件上限），
# 从 Releases 下载放到仓库根目录即可启用下面两个测试。
LM_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".adclean-lm-a5.json")

from adclean.semantic import (SemanticCleaner, SemanticConfig, SemanticEngine,
                              build_cleaner)
from adclean.v2lm import LanguageModel

CLEAN = [
    "常榕叹了口气坐到沙发旁边的书桌前，同时取出放在抽屉里的代办清单列表。",
    "她冲到常榕身前，张开双手试图护住常榕，同时拼尽全力努力的开口叫住众人。",
    "墙壁上镶嵌的显示屏突然亮起，一只玩偶般猫头鹰出现在了屏幕里侧。",
    "在常榕眼前的是不知为何被白色油漆完全覆盖的牢房，以及牢房地上两具女性的尸体。",
    "调色盘少女语气失落，彩虹般的双眸也眼泪汪汪，仿佛下一秒就会往外掉小彩虹糖似的。",
    "常榕也没有示弱，默不作声笑着点点头作为回应。",
]
# 整行广告：由 adclean.ads 的确定性规则处理，整行删除。
AD_LINES = [
    "本文件由会员上传，资源来源于网络，仅作学习交流使用。",
    "本文件由会员“12345”上传，上传时间:2026-03-02 10:25:04",
    "QQ群：123456789，欢迎加入书友交流群！",
    "网址：www.example-novel.com     QQ群：284251450",
    "求月票、求推荐票！！！",
    "请记住本站域名，章节更新最快。",
    "更多精彩小说，尽在 www.example-novel.com。",
    '<img src="https://c1.example.com/uploads/2026/01/a.jpg">',
    "<Book>12</Book>",
    "（本章完）",
]
POLLUTED = [
    ("墙壁上镶嵌异的'显示奇屏突然亮起，一柳只玩偶般猫头鹰1出现在了屏幕e里r侧。",
     "墙壁上镶嵌的显示屏突然亮起，一只玩偶般猫头鹰出现在了屏幕里侧。"),
    ("在常榕眼前的是不知为何被白色油漆完全覆盖的牢房。",
     "在常榕眼前的是不知为何被白色油漆完全覆盖的牢房。"),
    ("常榕也没霖I是wu揪s$i氿有示弱，默不作声笑着点点头作为回应。",
     "常榕也没有示弱，默不作声笑着点点头作为回应。"),
]


@pytest.fixture(scope="module")
def cleaner():
    lm = LanguageModel().fit(CLEAN * 60)
    cfg = SemanticConfig(insert_prior=0.005)
    c = SemanticCleaner(lm, cfg)
    c._penalty = 10.0
    return c


def test_no_frequency_fields_in_config():
    """配置里不允许出现任何字频字段。"""
    cfg = SemanticConfig()
    for banned in ("common_char_freq", "anchor_min", "min_freq", "max_freq",
                   "homophone_max_freq", "max_anchors", "suspicion"):
        assert not hasattr(cfg, banned), banned


def test_engine_is_dropin_for_pipeline():
    """主链路只用 clean_line / _line_score，语义引擎必须提供同签名接口。"""
    lm = LanguageModel().fit(CLEAN * 60)
    eng = SemanticEngine(lm, SemanticConfig())
    r = eng.clean_line("常榕也没有示弱。")
    assert hasattr(r, "cleaned") and hasattr(r, "removed")
    assert isinstance(eng._line_score(list("常榕也没有示弱。"), ()), float)


def test_clean_line_untouched(cleaner):
    """本来就通顺的句子一个字都不许动。"""
    for s in CLEAN:
        assert cleaner.clean_line(s).removed == (), s


def test_ad_lines_removed_whole(cleaner):
    """整行广告（上传者水印/群号/网址/章尾求票/标签）必须整行删除。"""
    for s in AD_LINES:
        r = cleaner.clean_line(s)
        assert r.cleaned == "", s
        assert set(r.removed) == set(range(len(s))), s


def test_ad_rule_does_not_fire_on_prose():
    """含广告词的**正文**不得被当成广告行。"""
    from adclean.ads import ad_line_reason
    for s in ("他求月票的样子很可笑。",
              "“我一定会记住本站的规矩。”少年郑重地说。",
              "李明的QQ群里，大家正在讨论明天的比赛。",
              "打赏的感谢我没说？",
              "订阅在掉了- -"):
        assert ad_line_reason(s) is None, s


def test_injected_chars_removed(cleaner):
    """句内插入的常用字/字母必须被删。"""
    line = "常榕也没霖I是wu揪s$i氿有示弱。"
    r = cleaner.clean_line(line)
    assert "霖" not in r.cleaned and "揪" not in r.cleaned
    assert "没有示弱" in r.cleaned


def test_protected_names_never_deleted(cleaner):
    """人名白名单里的字永不删除。"""
    cleaner.cfg.names = ("常榕",)
    for s in ("常榕也没有示弱。", "常榕叹了口气坐到沙发旁边。", "她冲到常榕身前。"):
        assert cleaner.clean_line(s).removed == (), s


def test_invisible_chars_always_removed(cleaner):
    """不可见/控制字符是 Unicode 层面的确定性事实，无条件删除。"""
    r = cleaner.clean_line("常榕\u200b也\ufeff没有\u200d示弱。")
    assert r.cleaned == "常榕也没有示弱。"
    assert set(r.removed) == {2, 4, 7}


def test_tag_line_removed(cleaner):
    """HTML/URL 广告行整行删。"""
    for s in ('<img src="https://e1.kuangxiangit.com/x.jpg" alt=\'雪\'>',
              "https://example.com/ad",
              '<Book {"book_name":"x","book_id":"1"} >'):
        assert cleaner.clean_line(s).cleaned == "", s


def test_cost_scale_makes_it_stricter():
    """代价越大越保守，且必须单调。"""
    lm = LanguageModel().fit(CLEAN * 60)
    line = "常榕也没霖I是wu揪s$i氿有示弱。"
    a = SemanticCleaner(lm, SemanticConfig(cost_scale=1.0))
    b = SemanticCleaner(lm, SemanticConfig(cost_scale=4.0))
    assert len(a.clean_line(line).removed) >= len(b.clean_line(line).removed)
    assert a._penalty < b._penalty


def test_idempotent(cleaner):
    """清洗结果再洗一遍不能再变（否则会反复改写文本）。"""
    for src, _gold in POLLUTED:
        once = cleaner.clean_line(src).cleaned
        assert cleaner.clean_line(once).removed == (), once


def test_pipeline_default_equals_direct_engine():
    """pipeline 输出必须与直接清洗逐字节相同（Audit/Review/LLM 都不能削弱它）。"""
    from adclean.audit import AuditConfig
    from adclean.pipeline import PipelineConfig, run_book
    from adclean.review import NullResolver
    lm = LanguageModel().fit(CLEAN * 60)
    eng = SemanticEngine(lm, SemanticConfig())
    text = "\n".join(CLEAN + [POLLUTED[0][0], POLLUTED[2][0]] + CLEAN)
    direct = "\n".join(eng.clean_line(x).cleaned if x else x
                       for x in text.split("\n"))
    cfg = PipelineConfig()
    cfg.audit = AuditConfig(merge_lines=False)
    res = run_book(text, eng, cfg, book_id="b", resolver=NullResolver())
    assert res.cleaned == direct


def test_does_not_delete_common_word(cleaner):
    """常见词里的字不许被删。"""
    r = cleaner.clean_line("在常榕眼前的是不知为何被白色油漆完全覆盖的牢房。")
    assert "覆盖" in r.cleaned


@pytest.mark.skipif(
    not os.path.exists(LM_PATH),
    reason="生产语言模型不存在")
def test_production_lm_keeps_simplified_words():
    """覆/著 在简体里合法，绝不能删。"""
    from adclean.semantic import build_cleaner as bc
    c = bc()
    for s in ("在常榕眼前的是不知为何被白色油漆完全覆盖的牢房。",
              "著名作家写出了显著的成绩。",
              "颠覆常识的说法需要确凿证据，而不是随意揣测。"):
        assert c.clean_line(s).cleaned == s, s
    # 已知限制：极罕见的合法简体字（幺）在孤立句子里仍可能被删
    assert "覆" in c.clean_line("在常榕眼前的是不知为何被白色油漆完全覆盖的牢房。").cleaned


@pytest.mark.skipif(
    not os.path.exists(LM_PATH),
    reason="生产语言模型不存在")
def test_real_line_matches_polished_edition():
    """这一行清洗后必须与精校版逐字对齐。"""
    from adclean.semantic import build_cleaner as bc
    c = bc()
    src = ("墙壁上镶嵌异的'显示奇屏突然亮起，一柳只玩偶般猫头鹰1出现在了屏幕e里r"
           "侧，它就的~话语也暂时%让侕众箘人停止了对于常榕的讨论。")
    out = c.clean_line(src).cleaned
    for junk in ("异", "'", "奇", "柳", "1", "e", "r", "~", "%", "侕", "箘"):
        assert junk not in out, (junk, out)
    assert "墙壁上镶嵌的显示屏突然亮起" in out
    assert "一只玩偶般猫头鹰出现在了屏幕里侧" in out
    assert "的话语也暂时让众人停止了对于常榕的讨论。" in out
