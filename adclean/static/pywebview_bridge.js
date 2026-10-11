/* adclean —— 桌面外壳（pywebview）桥接脚本。
 * 只在原生窗口里生效；用浏览器打开时整段静默跳过。
 * WebView2 里 <input type="file"> 只给文件内容不给路径，所以这里改用原生对话框。
 */
(function () {
  "use strict";

  var hooked = false;

  function api() {
    return (window.pywebview && window.pywebview.api) || null;
  }

  function basename(p) {
    return (p || "").split(/[\\/]/).filter(Boolean).pop() || p;
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

  /** 把一个绝对路径（文件或目录）塞进文件列表。 */
  function preload(p) {
    if (!p) return Promise.resolve();
    if (/\.txt$/i.test(p)) {
      // 单个文件：顺带取真实大小（界面要显示）
      var dir = p.replace(/[\\/][^\\/]*$/, "");
      var base = basename(p);
      return browse(dir).then(function (j) {
        var hit = (j.files || []).filter(function (f) {
          return f.name === base;
        })[0];
        var sz = {};
        if (hit) sz[p] = hit.size;
        callPage("addServerFiles", [[p], sz]);
      }).catch(function () { callPage("addServerFiles", [[p]]); });
    }
    return browse(p).then(function (j) {
      var files = (j.files || []).filter(function (f) {
        return /\.txt$/i.test(f.name);
      });
      if (!files.length) { callPage("addServerFiles", [[]]); return; }
      var sizes = {};
      var paths = files.map(function (f) {
        var full = p.replace(/[\\/]+$/, "") + "\\" + f.name;
        sizes[full] = f.size;
        return full;
      });
      callPage("addServerFiles", [paths, sizes]);
    }).catch(function (e) { console.warn("[adclean] preload 失败", e); });
  }

  function pickFile() {
    var a = api();
    if (!a || !a.pick_file) return;
    Promise.resolve(a.pick_file()).then(preload);
  }

  function pickDir() {
    var a = api();
    if (!a || !a.pick_dir) return;
    Promise.resolve(a.pick_dir()).then(preload);
  }

  function pickOut() {
    var a = api();
    if (!a || !a.pick_dir) return;
    Promise.resolve(a.pick_dir()).then(function (p) {
      if (p) document.getElementById("outDir").value = p;
    });
  }

  function openOut() {
    var a = api();
    var v = (document.getElementById("outDir") || {}).value || "";
    if (a && a.open_path && v) a.open_path(v);
  }

  function hook() {
    if (hooked || !api()) return;
    hooked = true;

    var on = function (id, fn) {
      var el = document.getElementById(id);
      if (el) { el.onclick = fn; }
    };
    on("btnPickFiles", pickFile);
    on("btnPickDir", pickDir);
    on("btnPickOut", pickOut);
    on("btnOpenOut", openOut);

    var drop = document.getElementById("drop");
    if (drop) drop.onclick = function (ev) { ev.preventDefault(); pickFile(); };

    var hint = document.getElementById("dropHint");
    if (hint) {
      hint.textContent = "支持 .txt 单文件，也支持整个文件夹批量处理；"
        + "也可以直接把文件拖到 adclean.exe 图标上。原文件不会被改动。";
    }
    var srv = document.getElementById("btnBrowseSrv");
    if (srv) srv.textContent = "浏览目录…";

    var a = api();
    if (a && a.get_dropped) Promise.resolve(a.get_dropped()).then(preload);

    document.title = "adclean — 小说广告清理";
  }

  // 无条件注册：pywebviewready 可能在脚本执行之后才触发
  window.addEventListener("pywebviewready", hook);
  if (document.readyState === "complete") setTimeout(hook, 200);
  else window.addEventListener("load", function () { setTimeout(hook, 200); });
  // 兜底轮询：pywebviewready 可能因故没触发
  var tries = 0;
  var timer = setInterval(function () {
    if (hooked || ++tries > 75) { clearInterval(timer); return; }
    hook();
  }, 200);
})();
