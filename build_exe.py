# -*- coding: utf-8 -*-
"""PyInstaller onedir 打包，产出 dist/adclean/ 双击即用的桌面版（--windowed）。

用 onedir 而非 onefile：onefile 每次启动都要把整包解到临时目录，启动会慢十几秒。

n-gram 缓存（约 100MB）作为 sidecar 放在 exe 旁边，不打进 exe；
resolve_lm_path() 按 exe 目录 → 当前目录的顺序找 `.adclean-lm-*.json`。

用法：
    python build_exe.py            # 生成 dist/adclean/adclean.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "adclean"
LM_CANDIDATES = (".adclean-lm-a5.json", ".adclean-lm-a0c.json",
                 ".adclean-lm-a4.json", ".adclean-lm-c1.json",
                 ".adclean-lm.json")

HIDDEN = [
    "lightgbm", "lightgbm.sklearn", "sklearn", "sklearn.utils._typedefs",
    "numpy", "scipy", "webview", "webview.platforms.edgechromium",
    "clr_loader", "pythonnet", "bottle",
]
EXCLUDE = [
    # adclean 用不到的大包；不排除会把整个 site-packages 收进产物
    "torch", "torchvision", "torchaudio", "cv2", "transformers", "tokenizers",
    "faiss", "faiss_cpu", "onnxruntime", "PIL", "pandas", "matplotlib",
    "IPython", "notebook", "jupyter", "nbformat", "nbconvert",
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "tensorflow", "keras", "jax", "sympy", "networkx", "numba", "llvmlite",
    "pyarrow", "polars", "duckdb", "bs4", "lxml", "openpyxl", "docx", "pptx",
    "pytest", "setuptools", "pip", "wheel",
]


def _dist_in_use(dist_dir: str) -> bool:
    """目标目录里的 exe 是不是正被占用（用户还开着窗口）。

    Windows 上运行中的 exe **可以改名但不能改写**，所以判定必须尝试以"写"方式打开。
    """
    exe = os.path.join(dist_dir, NAME + ".exe")
    if not os.path.exists(exe):
        return False
    try:
        with open(exe, "r+b"):
            pass
        return False
    except OSError:
        return True


def main() -> int:
    # --distpath 可改装到别的目录：exe 正在运行时 dist/ 会被占用，删不掉
    distpath = ""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--distpath" and i + 1 < len(argv):
            distpath = argv[i + 1]
    if not distpath:
        default = os.path.join(HERE, "dist")
        if _dist_in_use(os.path.join(default, NAME)):
            distpath = os.path.join(HERE, "dist_new")
            print("[注意] %s 里的 adclean.exe 正被占用（大概还开着窗口），"
                  "本次改装到 %s，不会打断你正在用的程序。" % (default, distpath))
    entry = os.path.join(HERE, "launcher.py")
    if not os.path.exists(entry):
        print("找不到入口", entry)
        return 1
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        "--name", NAME,
        # --windowed 双击不弹黑框，但没有 stdout/stderr（由 launcher.py 重定向到日志）
        "--windowed",
        "--paths", HERE,
        # 静态资源与数据文件
        "--add-data", os.path.join(HERE, "adclean", "static") + os.pathsep + "adclean/static",
        "--add-data", os.path.join(HERE, "adclean", "data") + os.pathsep + "adclean/data",
    ]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    for e in EXCLUDE:
        args += ["--exclude-module", e]
    if distpath:
        args += ["--distpath", distpath]
    args.append(entry)

    print("运行:", " ".join(args[:8]), "...")
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        return r.returncode

    dist = os.path.join(distpath or os.path.join(HERE, "dist"), NAME)
    # n-gram 缓存以 sidecar 形式放在 exe 旁边
    copied = []
    for name in LM_CANDIDATES:
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            dst = os.path.join(dist, name)
            if not os.path.exists(dst):
                print("复制 n-gram 缓存（sidecar）:", name)
                shutil.copy2(src, dst)
            copied.append(name)
            break
    with open(os.path.join(dist, "使用说明.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "adclean 桌面版\n"
            "============================================\n"
            "用法一：双击 adclean.exe，然后点\"选择 txt 文件 / 选择文件夹\"，\n"
            "        或把 .txt / 文件夹直接拖进窗口。\n"
            "用法二：把 .txt 或文件夹直接拖到 adclean.exe 图标上，会自动打开\n"
            "        窗口并载入。\n\n"
            "输出位置：默认放在原文件旁边的 cleaned 文件夹，也可以在界面上\n"
            "          自己选（点\"选择…\"）。原文件不会被改动。\n\n"
            f"运行必需的同目录文件：{copied[0] if copied else '(缺少 n-gram 缓存!)'}\n"
            "  —— 语言模型缓存，约 100MB。删了还能启动，但清不了广告；\n"
            "     从原发布包整个目录拷回来即可，不要只拷 exe。\n\n"
            "默认档位：深度清洗（40 维局部搭配 + 跨书负例模型），只删不改，\n"
            "          不联网、不需要 API Key、不上传任何内容。\n\n"
            "出问题看日志：%LOCALAPPDATA%\\adclean\\adclean.log\n"
        )
    print("\n完成 →", dist)
    print("  exe:", os.path.join(dist, NAME + ".exe"))
    print("  n-gram sidecar:", copied or "（未找到！请手动把 .adclean-lm-*.json 放进该目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
