# -*- coding: utf-8 -*-
"""无界面校验**打包产物**（dist/adclean）里的页面脚本与静态资源。

从 _internal/adclean/static/index.html 抽出内联 <script> 交给 node --check
做语法校验，并检查引用的静态资源与若干必须存在的标记；退出码非 0 表示有问题。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist", "adclean")
STATIC = os.path.join(DIST, "_internal", "adclean", "static")

INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
SRC = re.compile(r'<script[^>]*\bsrc="([^"]+)"', re.I)


def main() -> int:
    if not os.path.isdir(DIST):
        print("还没打包：%s 不存在" % DIST)
        return 1
    idx = os.path.join(STATIC, "index.html")
    h = open(idx, encoding="utf-8").read()
    ok = True

    print("=== 打包产物检查 ===")
    print("  adclean.exe          :", os.path.exists(os.path.join(DIST, "adclean.exe")))
    print("  n-gram sidecar       :", os.path.exists(
        os.path.join(DIST, ".adclean-lm-a5.json")))
    print("  错误收集器 __adcleanErrors :", "__adcleanErrors" in h)
    print("  reduce 已修复        :", "residual_regions_final)||0)})," in h)
    print("  桥接脚本标签         :", "pywebview_bridge.js" in h)
    print("  桥接脚本文件         :", os.path.exists(
        os.path.join(STATIC, "pywebview_bridge.js")))
    print("  拼音表 / 模型        :", os.path.exists(
        os.path.join(DIST, "_internal", "adclean", "data", "pinyin.tsv")),
        os.path.exists(os.path.join(DIST, "_internal", "adclean", "data",
                                    "locate_model.pkl")))

    for src in SRC.findall(h):
        if src.startswith(("http", "//")):
            continue
        p = os.path.join(STATIC, src.lstrip("/"))
        if not os.path.exists(p):
            print("  [缺失] 页面引用但不存在:", src)
            ok = False

    # 桌面外壳桥接脚本：必须按 id 挂钩、无条件注册监听、带轮询兜底
    br = os.path.join(STATIC, "pywebview_bridge.js")
    if os.path.exists(br):
        b = open(br, encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", "", b, flags=re.S)
        checks = {
            "按 id 挂钩 btnPickFiles": "btnPickFiles" in code,
            "按 id 挂钩 btnPickOut": "btnPickOut" in code,
            "无条件注册 pywebviewready": "pywebviewready" in code,
            "轮询兜底": "setInterval" in code,
            "无提前 return 判空":
                not re.search(r"if\s*\(\s*!\s*window\.pywebview[^)]*\)\s*return",
                              code[:code.find("pywebviewready")]),
        }
        for k, v in checks.items():
            print("  桥接 %-26s: %s" % (k, v))
            if not v:
                ok = False
    else:
        print("  [缺失] pywebview_bridge.js")
        ok = False

    # 文案卫生：正文不许出现 markdown 星号或旧引擎名
    body = re.sub(r"<script.*?</script>", "", h, flags=re.S)
    for k, v in (("正文无 markdown 星号", "**" not in body),
                 ("正文无 V4/V3 字样", ("V4" not in body) and ("V3" not in body))):
        print("  %-26s: %s" % (k, v))
        if not v:
            ok = False

    node = shutil.which("node")
    blocks = INLINE.findall(h)
    print("\n=== node --check 内联脚本（%d 段）===" % len(blocks))
    if not node:
        print("  没有 node，跳过")
    else:
        with tempfile.TemporaryDirectory() as td:
            for i, code in enumerate(blocks):
                f = os.path.join(td, "blk%d.js" % i)
                open(f, "w", encoding="utf-8").write(code)
                r = subprocess.run([node, "--check", f], capture_output=True)
                err = r.stderr.decode("utf-8", "replace").strip()
                print("  blk%d: %s" % (i, "OK" if r.returncode == 0 else "FAIL"))
                if r.returncode != 0:
                    ok = False
                    print("   ", err[:400])
    print("\n结论:", "打包产物可用 ✅" if ok else "有问题 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
