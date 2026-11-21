# -*- coding: utf-8 -*-
"""adclean.gui —— 双击即用的原生窗口外壳（无浏览器地址栏）。

用 pywebview 承载 adclean.server 的 Web UI；命令行传入的 .txt / 目录会预载到界面。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

from .server import AdcleanHTTPServer, Handler, _configure_stdio


def _resolve_drop(arg: str) -> str:
    """把拖进来的参数规整成绝对路径（文件或目录）。"""
    if not arg:
        return ""
    p = os.path.abspath(arg)
    return p if os.path.exists(p) else ""


def start_server(port: int = 0, host: str = "127.0.0.1", prefer: int = 8777):
    """直接绑定服务端口并返回 (httpd, actual_port)，避免端口探测 TOCTOU 竞态。"""
    if port:
        httpd = AdcleanHTTPServer((host, port), Handler)
    else:
        try:
            httpd = AdcleanHTTPServer((host, prefer), Handler)
        except OSError:
            httpd = AdcleanHTTPServer((host, 0), Handler)
    actual_port = int(httpd.server_address[1])
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, actual_port


class Api:
    """暴露给前端 JS 的原生能力。"""

    def __init__(self, dropped: str = ""):
        self.dropped = dropped
        self.window = None

    def get_dropped(self) -> str:
        """返回启动时拖进来的路径（没有则空串）。"""
        return self.dropped or ""

    def pick_files(self) -> list[str]:
        """原生多选「选择文件」对话框；返回真实路径列表，未选择则空列表。"""
        try:
            import webview
            r = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("文本文件 (*.txt)", "所有文件 (*.*)"),
            )
            if r:
                if isinstance(r, (list, tuple)):
                    return [str(x) for x in r if x]
                return [str(r)]
        except Exception:                                        # noqa: BLE001
            pass
        return []

    def pick_file(self) -> list[str]:
        """兼容接口：同样开启多选，返回路径列表。"""
        return self.pick_files()

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
        """在资源管理器里定位输出文件或打开输出目录（禁止直接执行任意文件）。"""
        if not path:
            return 0
        ap = os.path.abspath(path)
        if not os.path.exists(ap):
            return 0
        try:
            if os.name == "nt":
                if os.path.isdir(ap):
                    os.startfile(ap)                             # noqa: S606
                else:
                    subprocess.Popen(["explorer", f"/select,{ap}"])
            else:
                folder = ap if os.path.isdir(ap) else os.path.dirname(ap)
                subprocess.Popen(["xdg-open", folder])
            return 1
        except OSError:
            return 0


BANNER = r"""
   adclean — 中文小说 TXT 广告清理
   ---------------------------------------------------------------
   默认档位 = 局部搭配定位器（只删不改，本地离线可复核）。
   直接拖 .txt 到窗口里，或点"选择文件"。
"""


def _selftest(window):
    """自动用 JS 探针检查页面是否活着，结果打到 stdout。"""
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
    _t.sleep(2)
    try:
        window.destroy()
    except Exception:                                            # noqa: BLE001
        pass


def main(argv=None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(prog="adclean.gui", description="adclean 桌面版")
    ap.add_argument("path", nargs="?", default="",
                    help="要清理的 .txt 或文件夹（也可直接把文件拖到 exe 上）")
    ap.add_argument("--port", type=int, default=0, help="0=自动选空闲端口")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--selftest", action="store_true",
                    help="自动用 JS 探针检查界面，然后退出（排障用）")
    a = ap.parse_args(argv)

    dropped = _resolve_drop(a.path)
    httpd, port = start_server(a.port, a.host)
    url = "http://%s:%d/" % (a.host, port)

    print(BANNER)
    print("  窗口地址: %s" % url)
    if dropped:
        print("  已预载: %s" % dropped)
    print("")

    time.sleep(0.2)

    try:
        import webview
    except ImportError:
        httpd.shutdown()
        httpd.server_close()
        sys.stderr.write(
            "\n[错误] 缺少 pywebview，无法打开原生窗口。\n"
            "      请安装：pip install pywebview\n"
            "      （或改用 python -m adclean.server 走浏览器界面）\n"
        )
        return 2

    api = Api(dropped)
    window = webview.create_window(
        "adclean — 小说广告清理", url,
        js_api=api, width=1260, height=840,
        min_size=(960, 640), text_select=True,
    )
    api.window = window
    try:
        try:
            if a.selftest:
                webview.start(_selftest, (window,), gui="edgechromium", debug=False)
            else:
                webview.start(gui="edgechromium", debug=False)
        except Exception:                                        # noqa: BLE001
            webview.start(debug=False)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
