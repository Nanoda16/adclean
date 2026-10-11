# -*- coding: utf-8 -*-
"""`adclean` 统一入口：双击 / 拖入 → 原生窗口，带其它参数 → 命令行。

无参数、`--gui [路径]`、或只有一个存在的路径参数 → 打开桌面窗口；
其余 → 等价于 python -m adclean.pipeline。

命令行用法：
    python -m adclean "D:\\小说目录" -o "D:\\小说_已清洗"
    python -m adclean book.txt --engine locate
    python -m adclean book.txt --dump-prompts prompts.json
"""
from __future__ import annotations

import os
import sys


def _looks_like_gui(argv: list) -> bool:
    if not argv:
        return True
    if argv[0] == "--gui":
        return True
    if len(argv) == 1 and not argv[0].startswith("-") and os.path.exists(argv[0]):
        return True
    return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _looks_like_gui(argv):
        from .gui import main as gui_main
        rest = argv[1:] if argv[:1] == ["--gui"] else argv
        return gui_main(rest)
    from .pipeline import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
