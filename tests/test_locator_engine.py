# -*- coding: utf-8 -*-
"""定位器引擎的接口契约测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.locate import LocatorEngine                        # noqa: E402
from adclean.v2lm import LanguageModel                          # noqa: E402

CLEAN = [
    "常榕叹了口气坐到沙发旁边的书桌前，同时取出放在抽屉里的代办清单列表。",
    "她冲到常榕身前，张开双手试图护住常榕，同时拼尽全力努力的开口叫住众人。",
    "墙壁上镶嵌的显示屏突然亮起，一只玩偶般猫头鹰出现在了屏幕里侧。",
    "在常榕眼前的是不知为何被白色油漆完全覆盖的牢房，以及牢房地上两具女性的尸体。",
    "调色盘少女语气失落，彩虹般的双眸也眼泪汪汪，仿佛下一秒就会往外掉小彩虹糖似的。",
    "常榕也没有示弱，默不作声笑着点点头作为回应。",
]


def _book():
    """造一本"门禁能通过"的小书：干净正文 + 一段被逐字插入的书。

    **必须 >=1500 个非空行**：段落级筛查是每 1500 行一批做预测的，
    进度回调就写在那批处理里面；书太小的话那段代码根本不执行，契约测试会假通过。
    """
    clean = CLEAN * 200                     # 1200 行
    dirty = [ln[:6] + "霖I揪s氿" + ln[6:] for ln in CLEAN * 55]   # 330 行
    return clean + dirty + ["\u200b"] * 25  # 不可见字符让门禁打开


def _engine():
    lines = _book()
    lm = LanguageModel().fit(lines)
    return LocatorEngine(lm), lines


def test_progress_callback_takes_two_args():
    """progress 必须按 (done, total) 两个参数调用。"""
    eng, lines = _engine()
    calls = []
    eng.clean_book(lines, progress=lambda done, total: calls.append((done, total)))
    assert calls, "progress 从未被调用，进度条会一直停在 0"
    for done, total in calls:
        assert isinstance(done, int) and isinstance(total, int), (done, total)
        assert total > 0
        assert 0 <= done <= total, (done, total)
    # 单调不减，且最后一次必须到顶
    dones = [d for d, _t in calls]
    assert dones == sorted(dones), dones
    assert calls[-1][0] == calls[-1][1], calls[-1]


def test_clean_book_returns_pipeline_shape():
    """返回值必须是管线要的 {行号: (frozenset(位置), margin)}。"""
    eng, lines = _engine()
    out = eng.clean_book(lines)
    assert isinstance(out, dict)
    for li, v in out.items():
        assert isinstance(li, int) and 0 <= li < len(lines)
        rm, margin = v
        assert isinstance(rm, frozenset) and rm, v
        assert isinstance(margin, float)
        assert max(rm) < len(lines[li]), "删除位置越界"


def test_clean_line_works_without_book_cache():
    """audit 层会单行调用 clean_line，不能依赖整本书缓存。"""
    eng, _lines = _engine()
    r = eng.clean_line("常榕\u200b也没有示弱。")
    assert hasattr(r, "removed") and isinstance(r.removed, tuple)
    assert 2 in r.removed                      # 不可见字符确定性删除
    r2 = eng.clean_line("常榕也没有示弱。")
    assert r2.removed == ()


def test_protect_mask_is_indexable():
    """audit.statistical_conf 会按位置下标访问 mask，必须是序列不是 set。"""
    eng, _lines = _engine()
    mask = eng._protect_mask(list("常榕也没有示弱。"))
    assert len(mask) == 8
    assert mask[0] is False
