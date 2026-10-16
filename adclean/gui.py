# -*- coding: utf-8 -*-
"""adclean.gui —— 双击即用的原生窗口外壳（无浏览器地址栏）。

用 pywebview 承载 adclean.server 的 Web UI；命令行传入的 .txt / 目录会预载到界面。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

from .server import AdcleanHTTPServer, Handler


def _free_port(prefer: int = 8777) -> int:
    for p in (prefer, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    return prefer


def _resolve_drop(arg: str) -> str:
    """把拖进来的参数规整成绝对路径（文件或目录）。"""
    if not arg:
        return ""
    p = os.path.abspath(arg)
    return p if os.path.exists(p) else ""


def start_server(port: int, host: str = "127.0.0.1"):
    httpd = AdcleanHTTPServer((host, port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


class Api:
    """暴露给前端 JS 的原生能力。"""

    def __init__(self, dropped: str = ""):
        self.dropped = dropped
        self.window = None

    def get_dropped(self) -> str:
        """返回启动时拖进来的路径（没有则空串）。"""
        return self.dropped or ""

    def pick_file(self) -> str:
        """原生「选择文件」对话框；返回真实路径，未选择则空串。"""
        try:
            import webview
            r = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("文本文件 (*.txt)", "所有文件 (*.*)"))
            if r:
                return r[0] if isinstance(r, (list, tuple)) else str(r)
        except Exception:                                        # noqa: BLE001
            pass
        return ""

    def pick_dir(self) -> str:
        try:
            import webview
            r = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            if r:
                return r[0] if isinstance(r, (list, tuple)) else str(r)
        except Exception:                                        # noqa: BLE001
            pass
        return ""

    def open_path(self, path: str) -> int:
        """在资源管理器里打开输出目录。"""
        if path and os.path.exists(path):
            try:
                os.startfile(path)                               # noqa: S606
                return 1
            except Exception:                                    # noqa: BLE001
                return 0
        return 0


BANNER = r"""
   adclean — 中文小说 TXT 广告清理
   ---------------------------------------------------------------
   默认档位 = 深度清洗（多特征定位器），正文只做删除。
   默认先做一次繁→简字形转换（界面里可取消勾选）。
   直接拖 .txt 到窗口里，或点"选择文件"。
"""


def _selftest(window):
    """自动用 JS 探针检查页面是否"活着"，结果打到 stdout。"""
    import time as _t
    _t.sleep(6)
    probes = [
        "document.readyState",
        "typeof window.api",
        "typeof window.addServerFiles",
        "typeof window.pywebview",
        "typeof window.pywebview.api",
        "document.querySelectorAll('button').length",
        "document.getElementById('fileList') ? 'yes' : 'no'",
        "window.__adcleanErrors ? JSON.stringify(window.__adcleanErrors) : 'none'",
    ]
    for e in probes:
        try:
            print("  %-58s => %s" % (e, window.evaluate_js(e)), flush=True)
        except Exception as ex:                                  # noqa: BLE001
            print("  %-58s !! %r" % (e, ex), flush=True)
    try:
        r = window.evaluate_js(
            "(() => { try { addServerFiles(['C:\\\\t\\\\a.txt']);"
            " return document.getElementById('fileList').innerText.slice(0,80);"
            " } catch (e) { return 'THROW: ' + e.message; } })()")
        print("  addServerFiles 探针 => %r" % r, flush=True)
    except Exception as ex:                                      # noqa: BLE001
        print("  addServerFiles 探针 !! %r" % (ex,), flush=True)
    _t.sleep(2)
    try:
        window.destroy()
    except Exception:                                            # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="adclean.gui",
                                 description="adclean 桌面版")
    ap.add_argument("path", nargs="?", default="",
                    help="要清理的 .txt 或文件夹（也可直接把文件拖到 exe 上）")
    ap.add_argument("--port", type=int, default=0, help="0=自动选空闲端口")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--selftest", action="store_true",
                    help="自动用 JS 探针检查界面，然后退出（排障用）")
    a = ap.parse_args(argv)

    dropped = _resolve_drop(a.path)
    port = a.port or _free_port()
    url = "http://%s:%d/" % (a.host, port)

    print(BANNER)
    print("  窗口地址: %s" % url)
    if dropped:
        print("  已预载: %s" % dropped)
    print("")

    start_server(port, a.host)
    time.sleep(0.2)

    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "\n[错误] 缺少 pywebview，无法打开原生窗口。\n"
            "      请安装：pip install pywebview\n"
            "      （或改用 python -m adclean.server 走浏览器界面）\n")
        return 2

    api = Api(dropped)
    window = webview.create_window("adclean — 小说广告清理", url,
                                   js_api=api, width=1180, height=820,
                                   min_size=(900, 640), text_select=True)
    api.window = window
    # Windows 上用 WebView2 渲染
    try:
        if a.selftest:
            webview.start(_selftest, (window,), gui="edgechromium", debug=False)
        else:
            webview.start(gui="edgechromium", debug=False)
    except Exception:                                            # noqa: BLE001
        webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
