# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['F:/xwechat_files/txt-ad-cleaner/launcher.py'],
    pathex=['F:/xwechat_files/txt-ad-cleaner'],
    binaries=[],
    datas=[('F:/xwechat_files/txt-ad-cleaner/adclean/static', 'adclean/static'), ('F:/xwechat_files/txt-ad-cleaner/adclean/data', 'adclean/data')],
    hiddenimports=['lightgbm', 'lightgbm.sklearn', 'sklearn', 'sklearn.utils._typedefs', 'numpy', 'scipy', 'webview', 'webview.platforms.edgechromium', 'clr_loader', 'pythonnet', 'bottle'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'cv2', 'transformers', 'tokenizers', 'faiss', 'faiss_cpu', 'onnxruntime', 'PIL', 'pandas', 'matplotlib', 'IPython', 'notebook', 'jupyter', 'nbformat', 'nbconvert', 'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'tensorflow', 'keras', 'jax', 'sympy', 'networkx', 'numba', 'llvmlite', 'pyarrow', 'polars', 'duckdb', 'bs4', 'lxml', 'openpyxl', 'docx', 'pptx', 'pytest', 'setuptools', 'pip', 'wheel'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='adclean',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='adclean',
)
