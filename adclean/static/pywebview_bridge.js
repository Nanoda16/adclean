/* adclean —— 桌面外壳（pywebview）桥接脚本。
 * 只在原生窗口里生效；用浏览器打开时整段静默跳过。
 * 1. 优先调用多选 pick_files()，兼容单选 pick_file() 与文件夹 pick_dir()。
 * 2. 同时接管 #drop 的点击与原生拖拽：若拿到真实本地路径则直接加入，若 WebView2 未暴露路径则无缝走上传接口。
 */
(function () {
  "use strict";

  var hooked = false;

  function api() {
    return (window.pywebview && window.pywebview.api) || null;
  }

  function basename(p) {
    return String(p || "").split(/[\\/]/).filter(Boolean).pop() || String(p || "");
  }

  function callPage(fn, args) {
    if (typeof window[fn] === "function") {
      window[fn].apply(null, args);
      return true;
    }
    console.warn("[adclean] 页面函数 " + fn + " 不存在，界面可能未正常初始化");
    return false;
  }

  function browse(path) {
    var url = path
      ? "/api/browse?path=" + encodeURIComponent(path)
      : "/api/drives";
    if (typeof window.api === "function") return window.api(url);
    return fetch(url).then(function (r) { return r.json(); });
  }

  function listNonRecursive(folder) {
    var url = "/api/list?folder=" + encodeURIComponent(folder) + "&recursive=false";
    if (typeof window.api === "function") return window.api(url);
    return fetch(url).then(function (r) { return r.json(); });
  }

  function normalizePaths(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw.filter(Boolean).map(String);
    if (typeof raw === "string") {
      var trimmed = raw.trim();
      if (!trimmed) return [];
      if (trimmed.charAt(0) === "[") {
        try {
          var parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed)) return parsed.filter(Boolean).map(String);
        } catch (_e) { /* fall through */ }
      }
      return [trimmed];
    }
    return [];
  }

  /** 把一个或多个绝对路径（文件或目录）加载进文稿队列。 */
  function preloadPaths(raw) {
    var paths = normalizePaths(raw);
    if (!paths.length) return Promise.resolve();

    // 单个目录
    if (paths.length === 1 && !/\.txt$/i.test(paths[0])) {
      var dirPath = paths[0];
      return listNonRecursive(dirPath).then(function (j) {
        var files = (j && j.files) || [];
        if (!files.length) {
          var msg = document.getElementById("pickMsg");
          if (msg) msg.textContent = "所选文件夹内没有 .txt 文件";
          return;
        }
        callPage("addServerFiles", [files, (j && j.sizes) || {}]);
      }).catch(function () {
        return browse(dirPath).then(function (j) {
          var files = (j.files || []).filter(function (f) { return /\.txt$/i.test(f.name); });
          var sizes = {};
          var fulls = files.map(function (f) {
            var full = dirPath.replace(/[\\/]+$/, "") + "\\" + f.name;
            sizes[full] = f.size;
            return full;
          });
          callPage("addServerFiles", [fulls, sizes]);
        });
      });
    }

    // 一个或多个 txt 文件：按目录聚合拉取真实大小
    var txtPaths = paths.filter(function (p) { return /\.txt$/i.test(p); });
    if (!txtPaths.length) return Promise.resolve();

    var dirs = {};
    txtPaths.forEach(function (p) {
      var d = p.replace(/[\\/][^\\/]*$/, "");
      dirs[d] = true;
    });

    var sizeMap = {};
    var lookups = Object.keys(dirs).map(function (d) {
      return browse(d).then(function (j) {
        (j.files || []).forEach(function (f) {
          sizeMap[d.replace(/[\\/]+$/, "") + "\\" + f.name] = f.size;
          sizeMap[d.replace(/[\\/]+$/, "") + "/" + f.name] = f.size;
        });
      }).catch(function () { /* 忽略大小查询异常 */ });
    });

    return Promise.all(lookups).then(function () {
      var sizes = {};
      txtPaths.forEach(function (p) {
        if (sizeMap[p] !== undefined) sizes[p] = sizeMap[p];
        else {
          var b = basename(p);
          var d = p.replace(/[\\/][^\\/]*$/, "").replace(/[\\/]+$/, "");
          if (sizeMap[d + "\\" + b] !== undefined) sizes[p] = sizeMap[d + "\\" + b];
        }
      });
      callPage("addServerFiles", [txtPaths, sizes]);
    });
  }

  function pickFiles() {
    var a = api();
    if (!a) return;
    if (typeof a.pick_files === "function") {
      Promise.resolve(a.pick_files()).then(preloadPaths);
      return;
    }
    if (typeof a.pick_file === "function") {
      Promise.resolve(a.pick_file()).then(preloadPaths);
    }
  }

  function pickDir() {
    var a = api();
    if (!a || !a.pick_dir) return;
    Promise.resolve(a.pick_dir()).then(preloadPaths);
  }

  function pickOut() {
    var a = api();
    if (!a || !a.pick_dir) return;
    Promise.resolve(a.pick_dir()).then(function (p) {
      if (p && document.getElementById("outDir")) {
        document.getElementById("outDir").value = p;
      }
    });
  }

  function openOut() {
    var a = api();
    var v = (document.getElementById("outDir") || {}).value || "";
    if (a && a.open_path && v) a.open_path(v);
  }

  /**
   * 桌面窗口拖拽处理：若 File 对象附带本地真实 path 则直接加入，
   * 否则交还浏览器上传流自动落盘到临时目录，彻底杜绝拖入死胡同。
   */
  function handleBridgeDrop(ev) {
    var dt = ev && ev.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return Promise.resolve(false);
    var fileList = [].slice.call(dt.files);
    var localPaths = [];
    var uploadFiles = [];

    fileList.forEach(function (f) {
      if (f && f.path && /\.txt$/i.test(f.path)) {
        localPaths.push(f.path);
      } else if (f && /\.txt$/i.test(f.name || "")) {
        uploadFiles.push(f);
      }
    });

    if (localPaths.length && !uploadFiles.length) {
      return preloadPaths(localPaths).then(function () { return true; });
    }
    if (uploadFiles.length && typeof window.handleBrowserUploadFiles === "function") {
      var p1 = localPaths.length ? preloadPaths(localPaths) : Promise.resolve();
      return p1.then(function () {
        return window.handleBrowserUploadFiles(uploadFiles);
      }).then(function () { return true; });
    }
    return Promise.resolve(false);
  }

  function hook() {
    if (hooked || !api()) return;
    hooked = true;
    window.__adcleanBridgeHooked = true;

    var on = function (id, fn) {
      var el = document.getElementById(id);
      if (el) { el.onclick = fn; }
    };
    on("btnPickFiles", pickFiles);
    on("btnPickDir", pickDir);
    on("btnPickOut", pickOut);
    on("btnOpenOut", openOut);

    var drop = document.getElementById("drop");
    if (drop) {
      drop.onclick = function (ev) {
        ev.preventDefault();
        pickFiles();
      };
    }
    window.__adcleanBridgeDrop = handleBridgeDrop;

    var hint = document.getElementById("dropHint");
    if (hint) {
      hint.textContent = "支持多选 .txt 文件、选择整个文件夹或直接拖入窗口；原文件永不改动。";
    }
    var srv = document.getElementById("btnBrowseSrv");
    if (srv) srv.textContent = "浏览本机目录…";

    var a = api();
    if (a && a.get_dropped) {
      Promise.resolve(a.get_dropped()).then(preloadPaths);
    }

    document.title = "adclean — 中文小说出版校对室";
  }

  // 无条件注册：pywebviewready 可能在脚本执行之后才触发
  window.addEventListener("pywebviewready", hook);
  if (document.readyState === "complete") setTimeout(hook, 200);
  else window.addEventListener("load", function () { setTimeout(hook, 200); });

  var tries = 0;
  var timer = setInterval(function () {
    if (hooked || ++tries > 75) { clearInterval(timer); return; }
    hook();
  }, 200);
})();
