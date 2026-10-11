# -*- coding: utf-8 -*-
"""PyInstaller 入口（必须放在包**外面**）。

入口不能直接用 `adclean/__main__.py`：它会被当成顶层脚本，相对导入报
`attempted relative import with no known parent package`，所以这里做绝对导入。
`--windowed` 打包后没有 stdout/stderr，因此重定向到日志文件，崩溃时弹消息框。
"""
import multiprocessing
import os
import sys
import traceback


def _log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "adclean")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "adclean.log")
    except OSError:
        # 退回到 exe 同目录
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                            "adclean.log")


def _attach_parent_console() -> bool:
    """`--windowed` 下没有控制台；接管父进程控制台，使终端启动时 CLI 仍有输出。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        if not k.AttachConsole(-1):        # ATTACH_PARENT_PROCESS
            return False
        # windowed 模式下标准句柄无效，需要重新打开
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
        return True
    except Exception:                                            # noqa: BLE001
        return False


def _setup_logging():
    """把 stdout/stderr 指到日志文件（windowed 打包下它们默认是 None）。"""
    path = _log_path()
    try:
        fh = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    fh.write("\n" + "=" * 70 + "\n")
    fh.write("adclean 启动  argv=%r\n" % (sys.argv,))
    sys.stdout = fh
    sys.stderr = fh
    return fh


def _fatal(exc: BaseException, log: str):
    """把崩溃写进日志并弹一个原生消息框（不依赖任何 GUI 库）。"""
    try:
        import traceback as tb
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("崩溃：\n" + "".join(tb.format_exception(exc)) + "\n")
    except OSError:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "adclean 启动失败：\n\n%s\n\n详细日志：\n%s" % (exc, log),
            "adclean", 0x10)
    except Exception:                                            # noqa: BLE001
        pass


if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))

from adclean.__main__ import main      # noqa: E402

if __name__ == "__main__":
    multiprocessing.freeze_support()
    _log = _log_path()
    if getattr(sys, "frozen", False):
        if not _attach_parent_console():
            _setup_logging()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:                                   # noqa: BLE001
        _fatal(e, _log)
        raise SystemExit(1)
