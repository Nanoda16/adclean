# -*- coding: utf-8 -*-
"""前端 UI 资源、安全红线、Editorial 视觉规范与 JS 模块导出回归测试。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(HERE, "adclean", "static")
INDEX = os.path.join(STATIC_DIR, "index.html")
EDITORIAL_CSS = os.path.join(STATIC_DIR, "styles", "editorial.css")
MOTION_JS = os.path.join(STATIC_DIR, "js", "motion.js")
API_JS = os.path.join(STATIC_DIR, "js", "api.js")
DIFF_JS = os.path.join(STATIC_DIR, "js", "diff.js")
APP_JS = os.path.join(STATIC_DIR, "js", "app.js")
BRIDGE = os.path.join(STATIC_DIR, "pywebview_bridge.js")

sys.path.insert(0, HERE)

INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
SCRIPT_SRC_RE = re.compile(r'<script[^>]*\bsrc="([^"]+)"', re.I)
CSS_HREF_RE = re.compile(r'<link[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"', re.I)

EXPECTED_STATIC_FILES = [
    INDEX,
    EDITORIAL_CSS,
    MOTION_JS,
    API_JS,
    DIFF_JS,
    APP_JS,
    BRIDGE,
]

JS_FILES = [
    MOTION_JS,
    API_JS,
    DIFF_JS,
    APP_JS,
    BRIDGE,
]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _html() -> str:
    return _read(INDEX)


def test_all_expected_frontend_assets_exist():
    """新版模块化前端资源文件必须全部存在且非空。"""
    for path in EXPECTED_STATIC_FILES:
        assert os.path.isfile(path), "缺少前端资源文件：%s" % path
        assert os.path.getsize(path) > 0, "前端资源文件为空：%s" % path


def test_referenced_static_assets_exist():
    """页面引用的本地 js/css 必须真实存在，否则 Web UI / 桌面版会静默失效。"""
    html = _html()
    refs = SCRIPT_SRC_RE.findall(html) + CSS_HREF_RE.findall(html)
    assert refs, "页面未引用任何静态脚本或样式"
    for src in refs:
        if src.startswith(("http://", "https://", "//")):
            continue
        p = os.path.join(STATIC_DIR, src.lstrip("/"))
        assert os.path.exists(p), "页面引用了不存在的静态资源：%s" % src


def test_no_error_collector_regressions():
    """错误收集器必须在页面最前面（保证 gui.py _selftest 探针生效）。"""
    h = _html()
    assert "__adcleanErrors" in h, "页面缺少 JS 错误收集器"
    assert h.index("__adcleanErrors") < h.index("<script", h.index("__adcleanErrors") + 5)


def test_inline_scripts_are_valid_js(tmp_path):
    """页面里每段内联 <script> 都必须能通过 node --check 语法检查。"""
    node = shutil.which("node")
    blocks = INLINE_SCRIPT_RE.findall(_html())
    assert blocks, "页面里缺少内联错误收集脚本"
    if not node:
        pytest.skip("没有 node，跳过 JS 语法检查")
    for i, code in enumerate(blocks):
        f = tmp_path / ("block%d.js" % i)
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True)
        assert r.returncode == 0, (
            "第 %d 段内联脚本语法错误：\n%s" % (i, r.stderr.decode("utf-8", "replace")))


@pytest.mark.parametrize("js_path", JS_FILES, ids=lambda p: os.path.basename(p))
def test_external_js_files_are_valid_syntax(js_path):
    """所有外部 JS 模块必须通过 node --check 语法检查。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 语法检查")
    r = subprocess.run([node, "--check", js_path], capture_output=True)
    assert r.returncode == 0, "%s 语法错误：\n%s" % (
        os.path.basename(js_path), r.stderr.decode("utf-8", "replace"))


def test_no_hardcoded_private_names_in_static():
    """静态资源中禁止残留旧版硬编码私人测试人名。"""
    forbidden = ("常榕", "黑部奈叶香", "樱羽艾玛")
    for root_dir, _dirs, files in os.walk(STATIC_DIR):
        for fn in files:
            if not fn.endswith((".html", ".css", ".js")):
                continue
            full = os.path.join(root_dir, fn)
            content = _read(full)
            for name in forbidden:
                assert name not in content, "%s 中仍残留私人测试人名：%s" % (fn, name)


def test_no_dangerous_innerhtml_patterns():
    """前端代码中禁止出现 innerHTML 赋值或拼接，全面防止 DOM XSS。"""
    pattern = re.compile(r"\binnerHTML\s*[+=]")
    for path in [INDEX] + JS_FILES:
        raw = _read(path)
        # 剥离块注释与单行注释后再检测代码正文
        code = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
        m = pattern.search(code)
        assert not m, "%s 中存在危险的 innerHTML 赋值：%r" % (
            os.path.basename(path), code[max(0, m.start() - 20):m.end() + 30])


def test_editorial_css_tokens_and_motion_support():
    """校验 editorial.css 遵循纸墨出版校对风格并全量支持 reduced-motion。"""
    css = _read(EDITORIAL_CSS)
    required_tokens = [
        "#F9F8F6",
        "#FFFFFF",
        "#F3F1EC",
        "#1C1C1C",
        "#816d70",
        "#b42318",
        "#0f6f52",
        "#9a5b13",
        "border-radius: 0",
        "noise-texture",
        "scroll-progress",
        "underline-reveal",
        "sliding-tabs",
        "spring-drawer",
        "prefers-reduced-motion: reduce",
    ]
    for tok in required_tokens:
        assert tok in css, "editorial.css 缺少规范样式标记：%s" % tok


def test_stable_ids_and_bridge_hooks():
    """核心交互元素与桌面壳桥接必须通过稳定 id 绑定。"""
    html = _html()
    for elem_id in (
        "drop",
        "btnPickFiles",
        "btnPickDir",
        "btnClearQueue",
        "fileList",
        "btnPickOut",
        "btnOpenOut",
        "outDir",
        "chkSimplified",
        "names",
        "btnRunTask",
        "btnCancelTask",
        "reviewDrawer",
        "stickyActionBar",
    ):
        assert 'id="%s"' % elem_id in html, "index.html 缺少稳定 id：%s" % elem_id

    bridge_js = _read(BRIDGE)
    for hook_id in ("btnPickFiles", "btnPickDir", "btnPickOut", "btnOpenOut", "pick_files", "__adcleanBridgeDrop"):
        assert hook_id in bridge_js, "pywebview_bridge.js 缺少关键桥接钩子：%s" % hook_id


def test_bridge_registers_listener_before_any_guard():
    """桥接脚本必须在任何 window.pywebview 判空之前注册 pywebviewready。"""
    js = _read(BRIDGE)
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    i = code.find("pywebviewready")
    assert i > 0, "桥接脚本没有监听 pywebviewready"
    head = code[:i]
    bad = re.search(r"if\s*\(\s*!\s*window\.pywebview[^)]*\)\s*(?:\{\s*)?return", head)
    assert not bad, "注册 pywebviewready 之前提前 return：%r" % (
        head[max(0, bad.start() - 40):bad.end() + 20] if bad else "")


def test_bridge_asset_has_route():
    """server.py 必须具备完整的静态资源目录路由以承载多文件前端。"""
    srv = _read(os.path.join(HERE, "adclean", "server.py"))
    assert "_serve_static" in srv and "STATIC_REAL" in srv, (
        "server.py 缺少静态目录路由 _serve_static / STATIC_REAL")


def test_diff_and_motion_and_api_exports(tmp_path):
    """通过 Node 验证 diff.js / motion.js / api.js 的核心导出与逻辑正确性。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过运行时模块校验")

    script = tmp_path / "verify_modules.js"
    script.write_text(
        f"""
const assert = require("assert");
const diff = require({DIFF_JS!r});
const motion = require({MOTION_JS!r});
const api = require({API_JS!r});

// 1. 不可见字符标记 + 普通汉字保留
const vt = diff.visibleText("林晚\\u200b也\\u200d没有\\ufeff退缩。");
assert.ok(vt.plain.includes("[零宽空格]"));
assert.ok(vt.plain.includes("[零宽连接]"));
assert.ok(vt.plain.includes("[BOM]"));
assert.ok(vt.plain.includes("林晚") && vt.plain.includes("退缩"));

// 2. 字符级 Diff
const ops = diff.computeCharDiff("正文前段【广告】正文后段", "正文前段正文后段");
assert.deepStrictEqual(ops, [
  {{ op: "eq", text: "正文前段" }},
  {{ op: "del", text: "【广告】" }},
  {{ op: "eq", text: "正文后段" }}
]);

// 3. Motion 模块核心接口
for (const fn of [
  "prefersReducedMotion",
  "initScrollProgress",
  "initUnderlineReveal",
  "createSlidingTabs",
  "animateOdometer",
  "triggerLineReveal",
  "staggerFadeInUp",
  "createSpringDrawer"
]) {{
  assert.strictEqual(typeof motion[fn], "function", "missing motion export: " + fn);
}}

// 4. API 模块核心接口
for (const fn of [
  "requestJSON",
  "uploadFile",
  "clearUploads",
  "getDrives",
  "browsePath",
  "listFolder",
  "getLlmConfig",
  "saveLlmConfig",
  "fetchLlmModels",
  "probeLlm",
  "startPipeline",
  "getJob",
  "cancelJob",
  "getJobItems",
  "postDecisions",
  "applyJob",
  "rollbackJob",
  "getJobDiff",
  "getDownloadUrl",
  "getExportZipUrl",
  "revealOutput"
]) {{
  assert.strictEqual(typeof api[fn], "function", "missing api export: " + fn);
}}
console.log("ALL_JS_EXPORTS_OK");
""",
        encoding="utf-8",
    )
    r = subprocess.run([node, str(script)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "ALL_JS_EXPORTS_OK" in r.stdout.decode("utf-8", "replace")
