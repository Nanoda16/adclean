# -*- coding: utf-8 -*-
"""界面脚本的回归测试：

  1. 用 node --check 逐个校验页面里的内联 <script>（没有 node 就跳过）；
  2. 校验页面引用的本地静态资源确实存在、且服务端有路由。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(HERE, "adclean", "static", "index.html")
sys.path.insert(0, HERE)

INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
SRC = re.compile(r'<script[^>]*\bsrc="([^"]+)"', re.I)


def _html() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read()


def test_inline_scripts_are_valid_js(tmp_path):
    """页面里每个内联 <script> 都必须能通过语法检查。"""
    node = shutil.which("node")
    blocks = INLINE.findall(_html())
    assert blocks, "页面里没有内联脚本？文件可能被改坏了"
    if not node:
        pytest.skip("没有 node，跳过 JS 语法检查")
    for i, code in enumerate(blocks):
        f = tmp_path / ("block%d.js" % i)
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True)
        assert r.returncode == 0, (
            "第 %d 段内联脚本语法错误：\n%s" % (i, r.stderr.decode("utf-8", "replace")))


def test_no_error_collector_regressions():
    """错误收集器必须在页面最前面。"""
    h = _html()
    assert "__adcleanErrors" in h, "页面缺少 JS 错误收集器"
    assert h.index("__adcleanErrors") < h.index("<script", h.index("__adcleanErrors") + 5)


def test_referenced_static_assets_exist():
    """页面引用的本地 js/css 必须真的存在，否则桌面版会静默失效。"""
    for src in SRC.findall(_html()):
        if src.startswith(("http://", "https://", "//")):
            continue
        p = os.path.join(HERE, "adclean", "static", src.lstrip("/"))
        assert os.path.exists(p), "页面引用了不存在的静态资源：%s" % src


def test_bridge_asset_has_route():
    """桥接脚本必须有服务端路由，否则桌面版拿不到它。"""
    srv = open(os.path.join(HERE, "adclean", "server.py"), encoding="utf-8").read()
    assert "pywebview_bridge.js" in srv, "server.py 没有为桥接脚本开路由"


BRIDGE = os.path.join(HERE, "adclean", "static", "pywebview_bridge.js")


def test_bridge_script_is_valid_js(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node")
    r = subprocess.run([node, "--check", BRIDGE], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_bridge_registers_listener_before_any_guard():
    """桥接脚本必须在**任何** window.pywebview 判空之前就注册 pywebviewready。"""
    js = open(BRIDGE, encoding="utf-8").read()
    # 剥掉注释，避免注释里的旧写法被误判
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    i = code.find("pywebviewready")
    assert i > 0, "桥接脚本没有监听 pywebviewready"
    head = code[:i]
    bad = re.search(r"if\s*\(\s*!\s*window\.pywebview[^)]*\)\s*(?:\{\s*)?return", head)
    assert not bad, "注册 pywebviewready 之前就提前 return 了：%r" % (
        head[max(0, bad.start() - 40):bad.end() + 20] if bad else "")


def test_bridge_does_not_match_buttons_by_text():
    """按钮必须按 id 挂钩，不能靠文案匹配（文案一改桥接就静默失效）。"""
    js = open(BRIDGE, encoding="utf-8").read()
    assert "btnPickFiles" in js and "btnPickDir" in js and "btnPickOut" in js
    h = _html()
    for bid in ("btnPickFiles", "btnPickDir", "btnPickOut"):
        assert 'id="%s"' % bid in h, "页面缺少稳定 id: %s" % bid


def test_deleted_column_uses_visible_text():
    """删除列必须走 visibleText：删掉的是零宽字符时不能显示成一片空白。"""
    h = _html()
    assert "function visibleText" in h, "缺少 visibleText"
    assert "visibleText(it.v3_removed)" in h, "队列预览的删除列没用 visibleText"
    assert "visibleText(e.deleted)" in h, "修改抽样的删除列没用 visibleText"


def _extract_visible_text_js() -> str:
    """抽出 escapeHtml / INVIS_LABEL / visibleText 三个定义。"""
    lines = _html().split("\n")
    out = []

    def block(start_pred, end_pred):
        i = next((k for k, l in enumerate(lines) if start_pred(l)), None)
        assert i is not None, start_pred
        j = next((k for k in range(i, len(lines)) if end_pred(lines[k])), None)
        assert j is not None, end_pred
        return lines[i:j + 1]

    out += block(lambda l: l.startswith("function escapeHtml("),
                 lambda l: l.strip().endswith("}[c])); }"))
    out += block(lambda l: l.startswith("const INVIS_LABEL"),
                 lambda l: l.strip() == "};")
    out += block(lambda l: l.startswith("function visibleText("),
                 lambda l: l == "}")
    return "\n".join(out)


def test_visible_text_labels_invisible_chars(tmp_path):
    """零宽字符要变成可读标签，普通汉字原样保留。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node")
    src = _extract_visible_text_js()
    f = tmp_path / "vt.js"
    f.write_text(src + """
const r = visibleText("常榕\\u200b也\\u200d没有\\ufeff示弱。");
console.log(JSON.stringify(r));
""", encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8", "replace")
    for label in ("[零宽空格]", "[零宽连接]", "[BOM]"):
        assert label in out, "没有把不可见字符标出来：%s" % out
    assert "常榕" in out and "示弱" in out, "普通汉字被改坏了：%s" % out
