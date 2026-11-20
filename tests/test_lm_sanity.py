# -*- coding: utf-8 -*-
"""生产语料模型的健全性检查：常用字词频不能为 0，字表不含非 BMP 字符。"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adclean.pipeline import LM_CANDIDATES, resolve_lm_path   # noqa: E402

# 必须存在的常用字（词频不能为 0）
MUST_EXIST = "蒙瑙玛般赞融坑拐骗承若赤真厚布达灰原红丝面"
# 语料里不存在、但也不能被当成"注入字"删掉的常用词字
SHOULD_BE_KNOWN = "焖焗荷尔"


def _load_uni(path: str) -> dict:
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)["uni"]


@pytest.fixture(scope="module")
def uni():
    path = resolve_lm_path()
    if not os.path.exists(path):
        pytest.skip("没有可用的语料模型")
    return path, _load_uni(path)


def test_production_lm_is_rebuilt_clean(uni):
    """生产模型里这些常用字的词频不能为 0。"""
    path, u = uni
    missing = [c for c in MUST_EXIST if u.get(c, 0) == 0]
    assert not missing, (
        "语料模型 %s 里这些常用字词频为 0（历史事故复现）：%s"
        % (os.path.basename(path), "".join(missing)))


def test_lm_has_no_non_bmp_keys(uni):
    """模型的字表里不该出现非 BMP 的扩展区字符。"""
    _path, u = uni
    bad = [c for c in u if ord(c) > 0xFFFF]
    assert not bad, "模型字表里出现非 BMP 字符 %d 个：%s" % (len(bad), bad[:8])


def test_lm_size_sane(uni):
    path, u = uni
    assert len(u) > 3000, (os.path.basename(path), len(u))


def test_lm_candidates_put_rebuilt_model_first():
    """重建后的模型必须排在候选列表最前面。"""
    assert LM_CANDIDATES[0] in (".adclean-lm-a5.json", ".adclean-lm-a0c.json")
