# -*- coding: utf-8 -*-
"""PyInstaller onedir 打包，产出 dist/adclean/ 双击即用的桌面版（--windowed）。

用 onedir 而非 onefile：onefile 每次启动都要把整包解到临时目录，启动会慢十几秒。
若同目录存在 `.adclean-lm-*.json` 缓存会作为 sidecar 复制到 dist；未提供时自动按书构建自适应语言模型。

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
LM_CANDIDATES = (
    ".adclean-lm-a5.json",
    ".adclean-lm-a0c.json",
    ".adclean-lm-a4.json",
    ".adclean-lm-c1.json",
    ".adclean-lm.json",
)
MODEL_FILES = (
    "locate_m1.txt",
    "locate_p1.txt",
    "locate_p2.txt",
    "locate_meta.json",
)

HIDDEN = [
    "lightgbm", "numpy", "scipy",
    "webview", "webview.platforms.edgechromium",
    "clr_loader", "pythonnet", "bottle",
]
EXCLUDE = [
    # adclean 用不到的大包；不排除会把整个 site-packages 收进产物
    "sklearn", "scikit_learn",
    "torch", "torchvision", "torchaudio", "cv2", "transformers", "tokenizers",
    "faiss", "faiss_cpu", "onnxruntime", "PIL", "pandas", "matplotlib",
    "IPython", "notebook", "jupyter", "nbformat", "nbconvert",
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "tensorflow", "keras", "jax", "sympy", "networkx", "numba", "llvmlite",
    "pyarrow", "polars", "duckdb", "bs4", "lxml", "openpyxl", "docx", "pptx",
    "pytest", "setuptools", "pip", "wheel",
]


def _collect_data_args() -> list[str]:
    """收集 static/ 递归子目录及 data/ 下的原生 LightGBM 模型与词表。"""
    args: list[str] = []
    static_root = os.path.join(HERE, "adclean", "static")
    for cur_dir, _dirs, files in os.walk(static_root):
        if not files:
            continue
        rel = os.path.relpath(cur_dir, HERE).replace("\\", "/")
        for fn in files:
            src = os.path.join(cur_dir, fn)
            args += ["--add-data", f"{src}{os.pathsep}{rel}"]

    data_root = os.path.join(HERE, "adclean", "data")
    for cur_dir, _dirs, files in os.walk(data_root):
        if not files:
            continue
        rel = os.path.relpath(cur_dir, HERE).replace("\\", "/")
        for fn in files:
            src = os.path.join(cur_dir, fn)
            args += ["--add-data", f"{src}{os.pathsep}{rel}"]
    return args


def _dist_in_use(dist_dir: str) -> bool:
    """目标目录里的 exe 是不是正被占用（用户还开着窗口）。"""
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
    distpath = ""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--distpath" and i + 1 < len(argv):
            distpath = argv[i + 1]
    if not distpath:
        default = os.path.join(HERE, "dist")
        if _dist_in_use(os.path.join(default, NAME)):
            distpath = os.path.join(HERE, "dist_new")
            print(
                "[注意] %s 里的 adclean.exe 正被占用（大概还开着窗口），"
                "本次改装到 %s，不会打断你正在用的程序。" % (default, distpath)
            )
    entry = os.path.join(HERE, "launcher.py")
    if not os.path.exists(entry):
        print("找不到入口", entry)
        return 1

    data_dir = os.path.join(HERE, "adclean", "data")
    missing_models = [f for f in MODEL_FILES if not os.path.exists(os.path.join(data_dir, f))]
    if missing_models:
        print("[提示] 未检测到原生模型文件 %s，将按 adclean/data 现有内容打包。" % missing_models)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        "--name", NAME,
        "--windowed",
        "--paths", HERE,
    ]
    args.extend(_collect_data_args())
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

    lm_note = (
        f"已内置外部语言模型缓存：{copied[0]}"
        if copied
        else "未附带外部 .adclean-lm-*.json：程序将自动为每本小说构建自适应语言模型，开箱即用。"
    )
    with open(os.path.join(dist, "使用说明.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "adclean 桌面版\n"
            "============================================\n"
            "用法一：双击 adclean.exe，点击「选择 txt 文件 / 选择文件夹」，\n"
            "        或直接把 .txt / 文件夹拖入窗口。\n"
            "用法二：把 .txt 或文件夹直接拖到 adclean.exe 图标上，会自动打开\n"
            "        窗口并预载。\n\n"
            "输出位置：默认放在原文件旁边的 cleaned_audit 文件夹，也可以在界面上\n"
            "          自行指定。原文件永不被修改。\n\n"
            f"语言模型：{lm_note}\n\n"
            "默认引擎：深度清洗（36 维局部搭配特征 + 两阶段 LightGBM 定位器），只删不改，\n"
            "          本地离线完成；如需对疑难段落二次复核，可在界面开启 LLM 深度复核。\n\n"
            "字形处理：默认先把繁体字换成简体字（可在界面开关控制）。\n\n"
            "出问题看日志：%LOCALAPPDATA%\\adclean\\adclean.log\n"
        )
    print("\n完成 →", dist)
    print("  exe:", os.path.join(dist, NAME + ".exe"))
    print("  n-gram sidecar:", copied or "（无，自动走 auto_fit 模式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
