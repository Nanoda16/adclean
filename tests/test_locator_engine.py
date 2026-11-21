# -*- coding: utf-8 -*-
"""定位器引擎的接口契约测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adclean.locate import LocatorEngine                        # noqa: E402
from adclean.v2lm import LanguageModel                          # noqa: E402

CLEAN = [
    "林晚叹了口气坐到沙发旁边的书桌前，同时取出放在抽屉里的代办清单列表。",
    "她冲到林晚身前，张开双手试图护住林晚，同时拼尽全力努力的开口叫住众人。",
    "墙壁上镶嵌的显示屏突然亮起，一只玩偶般猫头鹰出现在了屏幕里侧。",
    "在林晚眼前的是不知为何被白色油漆完全覆盖的牢房，以及牢房地上两具女性的尸体。",
    "调色盘少女语气失落，彩虹般的双眸也眼泪汪汪，仿佛下一秒就会往外掉小彩虹糖似的。",
    "林晚也没有示弱，默不作声笑着点点头作为回应。",
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
    r = eng.clean_line("林晚\u200b也没有示弱。")
    assert hasattr(r, "removed") and isinstance(r.removed, tuple)
    assert 2 in r.removed                      # 不可见字符确定性删除
    r2 = eng.clean_line("林晚也没有示弱。")
    assert r2.removed == ()


def test_protect_mask_is_indexable():
    """audit.statistical_conf 会按位置下标访问 mask，必须是序列不是 set。"""
    eng, _lines = _engine()
    mask = eng._protect_mask(list("林晚也没有示弱。"))
    assert len(mask) == 8
    assert mask[0] is False


def test_native_lightgbm_models_and_sha256_check(tmp_path):
    """locate.py 不依赖 pickle/sklearn，且在模型文件被篡改时拒绝加载。"""
    import json
    import shutil
    import sys as _sys
    import pytest
    import adclean.locate as loc_mod
    from adclean.locate import KEEP, S1, Locator

    assert "pickle" not in loc_mod.__dict__
    assert not os.path.exists(os.path.join(loc_mod.DATA_DIR, "locate_model.pkl"))
    assert os.path.exists(loc_mod.DEFAULT_MODEL)

    # 验证 _complete_keep_features 与完整 build_features[:, KEEP] 完全一致
    lines = _book()
    lm = LanguageModel().fit(lines)
    loc = Locator(lm)
    MS, MB, CN = loc.full_book_stats(lines)
    for li in (0, 1200, 1500):
        full_keep = loc.build_features(lines, li, MS, MB, CN)[:, KEEP]
        s1_only = loc.build_features(lines, li, MS, MB, CN, stage1_only=True)[:, S1]
        recon = loc._complete_keep_features(lines[li], s1_only)
        assert (full_keep == recon).all()

    # SHA-256 篡改检测
    for fn in ("locate_meta.json", "locate_m1.txt", "locate_p1.txt", "locate_p2.txt"):
        shutil.copy2(os.path.join(loc_mod.DATA_DIR, fn), tmp_path / fn)
    with open(tmp_path / "locate_m1.txt", "ab") as f:
        f.write(b"\n# tampered\n")
    with pytest.raises(ValueError, match="SHA-256"):
        Locator(lm, model_path=str(tmp_path / "locate_meta.json"))
    # 若 meta 存在且完好则可加载
    assert json.loads((tmp_path / "locate_meta.json").read_text(encoding="utf-8"))["thr_line"] > 0


def test_duplicate_and_interned_lines_do_not_collide():
    """消除 id(line) 缓存碰撞：驻留/重复字符串在 clean_line 与 clean_book 中绝不串线。"""
    s1 = "".join(["重", "复", "行"])
    s2 = s1  # 刻意共享同一个字符串对象引用
    lines = [s1, "正常小说句子。", s2, "另一行\u200b带不可见"]
    lm = LanguageModel().fit(lines * 20)
    eng = LocatorEngine(lm)
    out = eng.clean_book(lines)
    assert 0 not in out and 2 not in out
    assert 3 in out and 3 in out[3][0]
    assert eng.clean_line(s1).removed == ()
    assert eng.clean_line("重复行\u200b").removed == (3,)
    assert not hasattr(eng, "_index") and not hasattr(eng, "_cache")


def test_concurrent_clean_book_is_stateless_and_isolated():
    """多线程并发调用共享 LocatorEngine.clean_book 时，白名单与结果互不踩踏。"""
    from concurrent.futures import ThreadPoolExecutor

    base = _book()
    lm = LanguageModel().fit(base)
    eng = LocatorEngine(lm)
    eng.cfg.names = ("初始名",)

    def worker(tag: str):
        wl = (tag,)
        res = eng.clean_book(base, names=wl)
        return eng.cfg.names, len(res)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(worker, ["林晚", "陈默", "林晚", "陈默"]))
    for cfg_names, cnt in results:
        assert cfg_names == ("初始名",), "共享 eng.cfg 被原地改写"
        assert cnt >= 25


def test_clean_book_cancel_check():
    """cancel_check 返回 True 时立即抛出 JobCancelledError。"""
    import pytest
    from adclean.pipeline import JobCancelledError
    eng, lines = _engine()
    with pytest.raises(JobCancelledError):
        eng.clean_book(lines, cancel_check=lambda: True)


def test_locator_does_not_bypass_residual_prose_protection():
    """STRUCT_ALL 不得绕过 ads._has_residual_prose 保护误杀含 QQ群/网址/OCR 的正常对话行。"""
    from adclean.ads import ad_line_reason
    from adclean.locate import Locator
    prose_lines = [
        "林默指着纸条上的号码说：“这是我的QQ群：123456，明天讨论战术。”",
        "苏明指着屏幕说：“这是我们的备用网址：https://example.com，别记错了。”",
        "警方通过OCR技术还原了现场的文字，终于找到了突破口。",
    ]
    ad_line = "QQ群：123456789，欢迎加入书友交流群！"
    book = CLEAN * 30 + prose_lines + [ad_line] * 6
    lm = LanguageModel().fit(book)
    loc = Locator(lm)
    deleted, _meta = loc.clean_lines(book, ad_line=ad_line_reason)
    base_idx = len(CLEAN) * 30
    # 绝不能整行误杀任何一句正常剧情对话
    for offset, ln in enumerate(prose_lines):
        idx = base_idx + offset
        assert deleted.get(idx) != set(range(len(ln))), ("正常剧情对话被整行误杀", ln)
    # 含 QQ 群号的正常对话行、含 OCR 的剧情行在定位器整本扫描下均不被删除
    assert base_idx not in deleted, (prose_lines[0], deleted.get(base_idx))
    assert (base_idx + 2) not in deleted, (prose_lines[2], deleted.get(base_idx + 2))
    # 含网址的剧情句中文正文必须完整保留
    kept_url_line = "".join(c for i, c in enumerate(prose_lines[1])
                            if i not in deleted.get(base_idx + 1, set()))
    assert "苏明指着屏幕说" in kept_url_line and "别记错了" in kept_url_line

    eng = LocatorEngine(lm)
    out = eng.clean_book(book)
    for offset, ln in enumerate(prose_lines):
        idx = base_idx + offset
        if idx in out:
            assert len(out[idx][0]) < len(ln), ("clean_book 整行误杀剧情行", ln)
    for ln in prose_lines:
        assert eng.clean_line(ln).removed == ()
    assert len(eng.clean_line(ad_line).removed) == len(ad_line)

