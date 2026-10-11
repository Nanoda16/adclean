# -*- coding: utf-8 -*-
"""无界面核对 adclean/static/index.html 的界面文案与结构。"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
h = open(os.path.join(HERE, "adclean", "static", "index.html"),
         encoding="utf-8").read()
body = re.sub(r"<script.*?</script>", "", h, flags=re.S)

checks = {
    "旧版引擎已下线": ("旧版引擎" not in h) and ("tab-old" not in h)
                       and ("nav-old" not in h),
    "无 V4/V3 字样": ("V4" not in body) and ("V3" not in body),
    "正文无 markdown 星号": "**" not in body,
    "解释性长句已删":
        all(p not in body for p in ("40 维局部搭配", "上下文异常数",
                                    "边界回撤", "判据 = ")),
    "保留功能与用法":
        all(p in body for p in ("会删掉的东西", "没送完怎么办", "原文件不会被覆盖")),
    "单次上限默认 0(不限)":
        re.search(r'id="llmMax" value="0"', h) is not None
        and "0=不限" in body,
    "文件大小不再强显 0 B": 'k ? fmt(k) : "—"' in h,
    "三档改动列": all(p in h for p in ("v3_pure_delete", "v3_rewritten",
                                       "visibleText")),
    "繁简开关在页面": "chkSimplified" in body,
    "标语已删": "只删不改" not in body,
    "输出不再直接走下载":
        "/api/download?path=" not in h and "function openPath" in h,
    "LLM 判定保留抽样": "llm_kept_sample" in h,
}
for k, v in checks.items():
    print("  %-24s %s" % (k, v))
print("  结论:", "全部通过" if all(checks.values()) else "有问题")
sys.exit(0 if all(checks.values()) else 1)
