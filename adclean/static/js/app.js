/* ==========================================================================
   Adclean Proofreading Workbench Controller
   主状态机、队列管理、任务轮询、人工审校清单、全文 Diff 与导出闭环
   严格零 innerHTML，所有动态文本通过 textContent / AdcleanDiff 安全渲染
   ========================================================================== */
(function (root) {
  "use strict";

  var Motion = root.AdcleanMotion;
  var Diff = root.AdcleanDiff;
  var API = root.AdcleanAPI;

  var $ = function (id) { return document.getElementById(id); };

  var KIND_META = {
    ad_line: { label: "整行广告", cls: "pill-tag--red" },
    auto_delete: { label: "自动定位删除", cls: "pill-tag--amber" },
    llm_delete: { label: "LLM 建议删除", cls: "pill-tag--rose" },
    llm_keep: { label: "LLM 建议保留", cls: "pill-tag--green" },
    uncertain: { label: "待定复核", cls: "pill-tag--amber" }
  };

  var STAGE_LABEL = {
    queued: "排队中",
    engine: "语料与定位清洗",
    t2s: "繁简转换",
    locate: "局部广告定位",
    audit: "旁路审计扫描",
    llm: "LLM 深度复核",
    final: "终稿复核生成",
    done: "校对完成",
    cancelled: "已中止",
    error: "运行异常"
  };

  var state = {
    mode: "auto",              // "auto" | "llm"
    resultTab: "review",       // "review" | "diff" | "report"
    queue: [],                 // [{ path, name, size, source }]
    uploadSessionId: "",
    uploading: false,

    browseCur: "",
    browseFiles: [],

    jobId: "",
    jobData: null,
    polling: false,
    pollTimer: null,
    elapsedTimer: null,
    taskStartMs: 0,

    activeFileId: "",
    itemsPage: 1,
    itemsPageSize: 40,
    itemsTotalPages: 1,
    itemsKind: "",
    itemsStatus: "",
    itemsQuery: "",
    currentItems: [],

    diffPage: 1,
    diffPageSize: 60,
    diffTotalPages: 1,
    diffOnlyChanged: true,

    drawerItemIndex: -1,
    llmLoadedOnce: false,
    llmHasSavedKey: false
  };

  var scrollProgress = null;
  var modeTabsCtrl = null;
  var resultTabsCtrl = null;
  var drawerCtrl = null;
  var searchDebounce = null;

  /* ------------------------------------------------------------------------
     工具函数
     ------------------------------------------------------------------------ */
  function fmtBytes(b) {
    var n = Number(b || 0);
    if (!n) return "—";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function baseName(p) {
    return String(p || "").split(/[\\/]/).filter(Boolean).pop() || String(p || "");
  }

  function joinWinPath(a, b) {
    var cleanA = String(a || "").replace(/[\\/]+$/, "");
    var sep = cleanA.indexOf("/") >= 0 && cleanA.indexOf("\\") < 0 ? "/" : "\\";
    return cleanA + sep + b;
  }

  function setStatusBadge(el, text, cls) {
    if (!el) return;
    el.className = "pill-tag" + (cls ? " " + cls : "");
    el.textContent = text;
  }

  function showLlmMessage(text, tone) {
    var el = $("llmMsg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = tone === "err"
      ? "var(--proof-red)"
      : (tone === "ok" ? "var(--proof-green)" : "var(--ink-secondary)");
  }

  function getActiveFileMeta() {
    var files = (state.jobData && state.jobData.files) || [];
    for (var i = 0; i < files.length; i++) {
      if (files[i].file_id === state.activeFileId) return files[i];
    }
    return files[0] || null;
  }

  /* ------------------------------------------------------------------------
     文件队列管理（浏览器上传 + 本机路径 + 桌面桥接）
     ------------------------------------------------------------------------ */
  function addQueueItems(items) {
    var existing = {};
    state.queue.forEach(function (it) { existing[it.path] = true; });
    items.forEach(function (it) {
      if (it && it.path && !existing[it.path]) {
        existing[it.path] = true;
        state.queue.push({
          path: it.path,
          name: it.name || baseName(it.path),
          size: Number(it.size || 0),
          source: it.source || "local"
        });
      }
    });
    renderQueue();
  }

  // 兼容 pywebview_bridge.js 与 gui.py _selftest 探针
  function addServerFiles(paths, sizes) {
    var list = (paths || []).filter(Boolean).map(function (p) {
      return {
        path: p,
        name: baseName(p),
        size: (sizes && sizes[p]) || 0,
        source: "local"
      };
    });
    if (!list.length) {
      renderQueue();
      return;
    }
    addQueueItems(list);
    var outEl = $("outDir");
    if (outEl && !outEl.value.trim() && list[0].path) {
      var dir = list[0].path.replace(/[\\/][^\\/]*$/, "");
      if (dir && dir !== list[0].path) {
        outEl.value = joinWinPath(dir, "cleaned_audit");
      }
    }
  }

  function removeQueueAt(idx) {
    if (idx < 0 || idx >= state.queue.length) return;
    state.queue.splice(idx, 1);
    renderQueue();
  }

  async function clearQueue() {
    state.queue = [];
    renderQueue();
    if (state.uploadSessionId) {
      var sid = state.uploadSessionId;
      state.uploadSessionId = "";
      await API.clearUploads(sid);
    }
  }

  function renderQueue() {
    var ul = $("fileList");
    var msgEl = $("pickMsg");
    if (!ul) return;
    ul.textContent = "";

    var totalBytes = 0;
    var hasUnknown = false;

    state.queue.forEach(function (item, idx) {
      if (item.size > 0) totalBytes += item.size;
      else hasUnknown = true;

      var li = document.createElement("li");
      li.className = "file-list__item";

      var nameSpan = document.createElement("span");
      nameSpan.className = "file-list__name";
      nameSpan.title = item.path;
      nameSpan.textContent = item.name;

      var sizeSpan = document.createElement("span");
      sizeSpan.className = "file-list__size";
      sizeSpan.textContent = item.size > 0 ? fmtBytes(item.size) : "—";

      var rmBtn = document.createElement("button");
      rmBtn.type = "button";
      rmBtn.className = "file-list__remove";
      rmBtn.title = "移除该文件";
      rmBtn.setAttribute("aria-label", "移除 " + item.name);
      rmBtn.textContent = "×";
      rmBtn.addEventListener("click", function () { removeQueueAt(idx); });

      li.appendChild(nameSpan);
      li.appendChild(sizeSpan);
      li.appendChild(rmBtn);
      ul.appendChild(li);
    });

    if (msgEl) {
      if (!state.queue.length) {
        msgEl.textContent = "尚未导入文稿";
      } else {
        msgEl.textContent = "队列共 " + state.queue.length + " 个文件" +
          (totalBytes ? " · " + fmtBytes(totalBytes) : (hasUnknown ? "（部分大小待定）" : ""));
      }
    }
  }

  async function handleBrowserUploadFiles(fileObjects) {
    var txtFiles = [].slice.call(fileObjects || []).filter(function (f) {
      return f && /\.txt$/i.test(f.name || "");
    });
    if (!txtFiles.length) {
      $("pickMsg").textContent = "未检测到 .txt 文本文件";
      return;
    }

    state.uploading = true;
    var wrap = $("uploadProgressWrap");
    var txtEl = $("uploadProgressText");
    var pctEl = $("uploadProgressPct");
    var fillEl = $("uploadProgressFill");
    if (wrap) wrap.hidden = false;

    var uploaded = [];
    var errors = [];
    for (var i = 0; i < txtFiles.length; i++) {
      var f = txtFiles[i];
      if (txtEl) txtEl.textContent = "正在上传 (" + (i + 1) + "/" + txtFiles.length + ")：" + f.name;
      var res = await API.uploadFile(f, state.uploadSessionId, function (loaded, total) {
        var filePct = total > 0 ? loaded / total : 0;
        var overallPct = Math.round(((i + filePct) / txtFiles.length) * 100);
        if (pctEl) pctEl.textContent = overallPct + "%";
        if (fillEl) fillEl.style.width = overallPct + "%";
      });
      if (res.ok && res.data && res.data.file) {
        if (res.data.session_id) state.uploadSessionId = res.data.session_id;
        uploaded.push(res.data.file);
      } else {
        errors.push(f.name + ": " + (res.error || "上传失败"));
      }
    }

    state.uploading = false;
    if (wrap) wrap.hidden = true;
    if (fillEl) fillEl.style.width = "0%";

    if (uploaded.length) {
      addQueueItems(uploaded);
    }
    if (errors.length) {
      $("pickMsg").textContent = "部分上传失败：" + errors[0];
    }
  }

  async function walkEntry(entry, out) {
    if (!entry) return;
    if (entry.isFile) {
      if (/\.txt$/i.test(entry.name || "")) {
        await new Promise(function (resolve) {
          entry.file(function (file) { out.push(file); resolve(); }, function () { resolve(); });
        });
      }
      return;
    }
    if (entry.isDirectory) {
      var reader = entry.createReader();
      var allEntries = [];
      await new Promise(function (resolve) {
        function readNext() {
          reader.readEntries(function (batch) {
            if (!batch || !batch.length) { resolve(); return; }
            allEntries = allEntries.concat([].slice.call(batch));
            readNext();
          }, function () { resolve(); });
        }
        readNext();
      });
      for (var i = 0; i < allEntries.length; i++) {
        await walkEntry(allEntries[i], out);
      }
    }
  }

  /* ------------------------------------------------------------------------
     本机目录浏览（高级折叠抽屉）
     ------------------------------------------------------------------------ */
  async function browseTo(path) {
    var card = $("browseCard");
    var body = $("browseBody");
    if (card) card.hidden = false;
    if (!body) return;
    body.textContent = "";

    var res = path ? await API.browsePath(path) : await API.getDrives();
    if (!res.ok) {
      var errDiv = document.createElement("div");
      errDiv.className = "browse-entry";
      errDiv.style.color = "var(--proof-red)";
      errDiv.textContent = res.error || "目录读取失败";
      body.appendChild(errDiv);
      return;
    }

    var j = res.data || {};
    if (j.drives) {
      var driveWrap = document.createElement("div");
      driveWrap.className = "btn-row";
      driveWrap.style.padding = "8px";
      driveWrap.style.marginTop = "0";
      j.drives.forEach(function (d) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn--sm";
        b.textContent = d;
        b.addEventListener("click", function () { browseTo(d); });
        driveWrap.appendChild(b);
      });
      body.appendChild(driveWrap);
      if (j.cwd) {
        var cwdEntry = document.createElement("div");
        cwdEntry.className = "browse-entry";
        var lbl = document.createElement("span");
        lbl.textContent = "工作目录：" + j.cwd;
        var goCwd = document.createElement("button");
        goCwd.type = "button";
        goCwd.className = "btn btn--sm";
        goCwd.textContent = "进入";
        goCwd.addEventListener("click", function () { browseTo(j.cwd); });
        cwdEntry.appendChild(lbl);
        cwdEntry.appendChild(goCwd);
        body.appendChild(cwdEntry);
      }
      return;
    }

    state.browseCur = j.path || "";
    state.browseFiles = (j.files || []).map(function (f) { return f.name; });
    if ($("browsePath")) $("browsePath").value = state.browseCur;

    var bar = document.createElement("div");
    bar.className = "browse-entry";
    bar.style.background = "var(--bg-secondary)";

    var leftBtns = document.createElement("div");
    leftBtns.className = "btn-row";
    leftBtns.style.marginTop = "0";

    var upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "btn btn--sm";
    upBtn.textContent = "↑ 上级目录";
    upBtn.addEventListener("click", function () { browseTo(j.parent || ""); });

    var useDirBtn = document.createElement("button");
    useDirBtn.type = "button";
    useDirBtn.className = "btn btn--sm btn--primary";
    useDirBtn.textContent = "导入本目录全部 txt";
    useDirBtn.addEventListener("click", useCurrentBrowseDir);

    leftBtns.appendChild(upBtn);
    leftBtns.appendChild(useDirBtn);
    bar.appendChild(leftBtns);
    body.appendChild(bar);

    (j.dirs || []).forEach(function (d) {
      var row = document.createElement("div");
      row.className = "browse-entry";
      var name = document.createElement("span");
      name.textContent = "[目录] " + d;
      var go = document.createElement("button");
      go.type = "button";
      go.className = "btn btn--sm";
      go.textContent = "打开";
      go.addEventListener("click", function () { browseTo(joinWinPath(state.browseCur, d)); });
      row.appendChild(name);
      row.appendChild(go);
      body.appendChild(row);
    });

    (j.files || []).forEach(function (f) {
      var row = document.createElement("div");
      row.className = "browse-entry";
      var name = document.createElement("span");
      name.textContent = f.name + " (" + fmtBytes(f.size) + ")";
      var addOne = document.createElement("button");
      addOne.type = "button";
      addOne.className = "btn btn--sm";
      addOne.textContent = "加入";
      addOne.addEventListener("click", function () {
        var fp = joinWinPath(state.browseCur, f.name);
        var sz = {};
        sz[fp] = f.size;
        addServerFiles([fp], sz);
      });
      row.appendChild(name);
      row.appendChild(addOne);
      body.appendChild(row);
    });

    if (!(j.dirs || []).length && !(j.files || []).length) {
      var empty = document.createElement("div");
      empty.className = "browse-entry";
      empty.textContent = "此目录下没有子目录或 .txt 文件";
      body.appendChild(empty);
    }
  }

  async function useCurrentBrowseDir() {
    if (!state.browseCur) return;
    var rec = !!($("chkBrowseRecursive") && $("chkBrowseRecursive").checked);
    var res = await API.listFolder(state.browseCur, rec);
    if (!res.ok) {
      $("pickMsg").textContent = res.error || "列举目录失败";
      return;
    }
    var files = (res.data && res.data.files) || [];
    var sizes = (res.data && res.data.sizes) || {};
    if (!files.length) {
      $("pickMsg").textContent = "所选目录没有 .txt 文件";
      return;
    }
    addServerFiles(files, sizes);
    if ($("outDir") && !$("outDir").value.trim()) {
      $("outDir").value = joinWinPath(state.browseCur, "cleaned_audit");
    }
    if ($("browseCard")) $("browseCard").hidden = true;
  }

  /* ------------------------------------------------------------------------
     LLM 配置管理
     ------------------------------------------------------------------------ */
  function collectLlmPayload() {
    var manual = ($("llmModelManual") ? $("llmModelManual").value : "").trim();
    var sel = $("llmModel") ? $("llmModel").value : "";
    return {
      base_url: ($("llmBase") ? $("llmBase").value : "").trim(),
      api_key: $("llmKey") ? $("llmKey").value : "",
      model: manual || sel,
      timeout: Number(($("llmTimeout") || {}).value || 60),
      concurrency: Number(($("llmConc") || {}).value || 2),
      retries: Number(($("llmRetries") || {}).value || 2),
      max_items: Number(($("llmMax") || {}).value || 0),
      temperature: Number(($("llmTemp") || {}).value || 0),
      json_mode: !!($("llmJson") && $("llmJson").checked),
      save_key: !!($("llmSaveKey") && $("llmSaveKey").checked)
    };
  }

  function ensureSelectOption(val) {
    var sel = $("llmModel");
    if (!sel || !val) return;
    var found = false;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === val) { found = true; break; }
    }
    if (!found) {
      var o = document.createElement("option");
      o.value = val;
      o.textContent = val;
      sel.appendChild(o);
    }
  }

  async function loadLlmConfig() {
    var res = await API.getLlmConfig();
    if (!res.ok) {
      showLlmMessage(res.error || "读取配置失败", "err");
      return;
    }
    state.llmLoadedOnce = true;
    var d = res.data || {};
    var c = d.config || {};
    var eng = d.engine || {};

    if ($("llmBase") && c.base_url) $("llmBase").value = c.base_url;
    if (c.model) {
      ensureSelectOption(c.model);
      if ($("llmModel")) $("llmModel").value = c.model;
      if ($("llmModelManual")) $("llmModelManual").value = "";
    }
    if ($("llmTimeout")) $("llmTimeout").value = c.timeout != null ? c.timeout : 60;
    if ($("llmConc")) $("llmConc").value = c.concurrency != null ? c.concurrency : 2;
    if ($("llmRetries")) $("llmRetries").value = c.retries != null ? c.retries : 2;
    if ($("llmMax")) $("llmMax").value = c.max_items != null ? c.max_items : 0;
    if ($("llmTemp")) $("llmTemp").value = c.temperature != null ? c.temperature : 0;
    if ($("llmJson")) $("llmJson").checked = c.json_mode !== false;
    if ($("llmSaveKey")) $("llmSaveKey").checked = c.save_key !== false;

    state.llmHasSavedKey = !!c.has_key;
    var keyBadge = $("llmKeyBadge");
    if (c.has_key) {
      setStatusBadge(keyBadge, "已保存 Key " + (c.key_masked || ""), "pill-tag--green");
    } else {
      setStatusBadge(keyBadge, "未保存密钥", "pill-tag--amber");
    }

    if ($("llmCfgPath")) {
      $("llmCfgPath").textContent = "配置文件：" + (d.config_path || "—");
    }

    var badge = $("engineBadge");
    var badgeTxt = $("engineBadgeText");
    if (badgeTxt) {
      badgeTxt.textContent = eng.loaded
        ? ("语料就绪：" + (eng.lm || "内置模型"))
        : "语料：按需自动构建 / 加载";
    }
    if (badge) {
      badge.classList.toggle("is-ready", !!eng.loaded);
    }
  }

  async function saveLlmConfig() {
    var payload = collectLlmPayload();
    showLlmMessage("正在保存 LLM 配置…");
    var res = await API.saveLlmConfig(payload);
    if (!res.ok) {
      showLlmMessage(res.error || "保存失败", "err");
      return;
    }
    if ($("llmKey")) $("llmKey").value = "";
    showLlmMessage("配置已保存至本机", "ok");
    await loadLlmConfig();
  }

  async function clearLlmKey() {
    showLlmMessage("正在清除本地保存的密钥…");
    var res = await API.saveLlmConfig({ clear_key: true });
    if (!res.ok) {
      showLlmMessage(res.error || "清除失败", "err");
      return;
    }
    if ($("llmKey")) $("llmKey").value = "";
    showLlmMessage("已清除本地保存的 API Key", "ok");
    await loadLlmConfig();
  }

  async function fetchModels() {
    var payload = collectLlmPayload();
    if (!payload.base_url) {
      showLlmMessage("请先填写 API Base URL", "err");
      return;
    }
    showLlmMessage("正在拉取远端模型列表…");
    var res = await API.fetchLlmModels(payload);
    if (!res.ok) {
      showLlmMessage(res.error || "拉取模型失败", "err");
      return;
    }
    var models = (res.data && res.data.models) || [];
    var sel = $("llmModel");
    if (sel) {
      sel.textContent = "";
      models.forEach(function (m) {
        var o = document.createElement("option");
        o.value = m;
        o.textContent = m;
        sel.appendChild(o);
      });
      if (res.data && res.data.current && models.indexOf(res.data.current) >= 0) {
        sel.value = res.data.current;
        if ($("llmModelManual")) $("llmModelManual").value = "";
      } else if (models.length && !($("llmModelManual") && $("llmModelManual").value.trim())) {
        sel.value = models[0];
      }
    }
    showLlmMessage("已获取 " + models.length + " 个模型（耗时 " + res.latencyMs + " ms）", "ok");
  }

  async function probeLlm() {
    var payload = collectLlmPayload();
    if (!payload.base_url || !payload.model) {
      showLlmMessage("请先填写 API 地址并选择或填写模型名", "err");
      return;
    }
    showLlmMessage("正在测试连通性（消耗极少 Token）…");
    var res = await API.probeLlm(payload);
    if (!res.ok) {
      showLlmMessage("探测失败（" + res.latencyMs + " ms）：" + (res.error || "未知错误"), "err");
      return;
    }
    var d = res.data || {};
    var ms = d.latency_ms != null ? d.latency_ms : res.latencyMs;
    showLlmMessage(
      "连接正常 · 延迟 " + ms + " ms · 模型 " + (d.model || payload.model) +
      " · decision=" + (d.decision || "?"),
      "ok"
    );
  }

  /* ------------------------------------------------------------------------
     模式切换（自动清洗 / LLM 深度复核）
     ------------------------------------------------------------------------ */
  function onModeChange(mode) {
    state.mode = mode === "llm" ? "llm" : "auto";
    var panel = $("llmConfigPanel");
    var runBtn = $("btnRunTask");
    if (panel) panel.hidden = state.mode !== "llm";
    if (runBtn && !state.polling) {
      runBtn.textContent = state.mode === "llm" ? "开始 LLM 深度复核" : "开始自动清洗";
    }
    if (state.mode === "llm" && !state.llmLoadedOnce) {
      loadLlmConfig();
    }
  }

  /* ------------------------------------------------------------------------
     任务执行、轮询、中止与断联自动恢复
     ------------------------------------------------------------------------ */
  function setRunningUI(running) {
    var runBtn = $("btnRunTask");
    var cancelBtn = $("btnCancelTask");
    if (runBtn) {
      runBtn.disabled = running;
      if (!running) {
        runBtn.textContent = state.mode === "llm" ? "开始 LLM 深度复核" : "开始自动清洗";
      } else {
        runBtn.textContent = "校对任务运行中…";
      }
    }
    if (cancelBtn) {
      cancelBtn.hidden = !running;
      cancelBtn.disabled = false;
    }
  }

  function startElapsedTimer() {
    stopElapsedTimer();
    state.taskStartMs = Date.now();
    state.elapsedTimer = setInterval(function () {
      var sec = ((Date.now() - state.taskStartMs) / 1000).toFixed(1);
      if ($("runTimer")) $("runTimer").textContent = sec + " s";
    }, 100);
  }

  function stopElapsedTimer(finalSec) {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
    if (finalSec !== undefined && finalSec !== null && $("runTimer")) {
      $("runTimer").textContent = Number(finalSec).toFixed(2) + " s";
    }
  }

  function clearErrorPanel() {
    if ($("jobErrorPanel")) $("jobErrorPanel").hidden = true;
    if ($("btnRetryPoll")) $("btnRetryPoll").hidden = true;
    if ($("jobTraceDetails")) $("jobTraceDetails").hidden = true;
    if ($("jobErrorTrace")) $("jobErrorTrace").textContent = "";
  }

  function showErrorPanel(summaryText, traceText, showRetryBtn) {
    if ($("jobErrorPanel")) $("jobErrorPanel").hidden = false;
    if ($("jobErrorSummary")) $("jobErrorSummary").textContent = summaryText || "任务执行异常";
    if ($("btnRetryPoll")) $("btnRetryPoll").hidden = !showRetryBtn;
    if (traceText) {
      if ($("jobTraceDetails")) {
        $("jobTraceDetails").hidden = false;
        $("jobTraceDetails").open = false;
      }
      if ($("jobErrorTrace")) $("jobErrorTrace").textContent = traceText;
    } else if ($("jobTraceDetails")) {
      $("jobTraceDetails").hidden = true;
    }
  }

  function updateRunProgressUI(job) {
    var st = (job && job.state) || "running";
    var stage = (job && job.stage) || st;
    var pctRaw = job && (job.progress != null ? job.progress : job.percent);
    var pct = Math.max(0, Math.min(100, Number(pctRaw || 0)));

    if ($("runStageLabel")) {
      $("runStageLabel").textContent = STAGE_LABEL[stage] || stage;
    }
    if ($("runMessage")) {
      $("runMessage").textContent = (job && job.message) || "";
    }

    var fill = $("runProgressFill");
    if (fill) {
      // 取消态严禁拉满 100%
      var displayPct = st === "done" ? 100 : pct;
      fill.style.width = displayPct + "%";
      fill.classList.toggle("is-done", st === "done");
      fill.classList.toggle("is-cancelled", st === "cancelled");
      fill.classList.toggle("is-error", st === "error");
    }
    if (scrollProgress) {
      if (st === "done") {
        scrollProgress.setOverride(null, "");
      } else {
        scrollProgress.setOverride(pct / 100, st);
      }
    }
  }

  function updateKpis(job) {
    var s = (job && job.summary) || {};
    var files = (job && job.files) || [];
    Motion.animateOdometer($("kpiLines"), s.total_lines);
    Motion.animateOdometer($("kpiItems"), s.total_items);
    Motion.animateOdometer($("kpiRemoved"), s.removed_chars);
    Motion.animateOdometer($("kpiAccepted"), s.accepted_items);
    Motion.animateOdometer($("kpiTokens"), s.total_tokens || 0);
    Motion.animateOdometer($("kpiElapsed"), s.elapsed_sec != null ? s.elapsed_sec : 0, { decimals: 2, suffix: "s" });

    if ($("kpiFilesSub")) $("kpiFilesSub").textContent = (s.file_count || files.length || 0) + " 个文件";
    if ($("kpiItemsSub")) {
      $("kpiItemsSub").textContent = "整行广告 " + (s.ad_lines || 0) + " · 自动 " + (s.auto_deleted || 0);
    }
    if ($("kpiModeSub")) {
      var lmDesc = s.lm_mode === "file" ? "外部语料文件" : (s.lm_mode === "auto_fit" ? "自动内存语料" : "—");
      $("kpiModeSub").textContent = lmDesc;
    }
    if ($("kpiAcceptedSub")) {
      $("kpiAcceptedSub").textContent = "总计 " + (s.total_items || 0) + " 条可复核";
    }
    if ($("kpiTokensSub")) {
      $("kpiTokensSub").textContent = "Prompt " + (s.prompt_tokens || 0) + " / Comp " + (s.completion_tokens || 0);
    }
    if ($("kpiLlmSub")) {
      $("kpiLlmSub").textContent = "LLM 删 " + (s.llm_deleted || 0) + " / 留 " + (s.llm_kept || 0);
    }

    var engBadgeTxt = $("engineBadgeText");
    var engBadge = $("engineBadge");
    if (engBadgeTxt && s.lm_mode) {
      engBadgeTxt.textContent = s.lm_mode === "file" ? "语料：外部模型文件" : "语料：当前文稿自适应";
      if (engBadge) engBadge.classList.add("is-ready");
    }
  }

  async function startTask() {
    if (state.uploading) {
      $("runMessage").textContent = "文件仍在上传中，请稍候…";
      return;
    }
    if (!state.queue.length) {
      $("runMessage").textContent = "请先在左侧导入至少一个 .txt 文稿。";
      return;
    }

    var llmCfg = null;
    if (state.mode === "llm") {
      llmCfg = collectLlmPayload();
      if (!llmCfg.base_url || !llmCfg.model) {
        showLlmMessage("启动 LLM 复核前请填写 API 地址并选择或填写模型", "err");
        return;
      }
      if (!llmCfg.api_key && !state.llmHasSavedKey) {
        var check = await API.getLlmConfig();
        if (!(check.ok && check.data && check.data.config && check.data.config.has_key)) {
          showLlmMessage("请填写 API Key", "err");
          return;
        }
      }
    }

    clearErrorPanel();
    setRunningUI(true);
    state.polling = true;
    startElapsedTimer();
    updateRunProgressUI({ state: "running", stage: "queued", progress: 2, message: "正在提交校对任务…" });

    var payload = {
      mode: state.mode,
      files: state.queue.map(function (it) { return it.path; }),
      out: ($("outDir") ? $("outDir").value : "").trim(),
      options: {
        simplify: !!($("chkSimplified") && $("chkSimplified").checked),
        names: ($("names") ? $("names").value : "").trim(),
        out_suffix: ($("outSuffix") ? $("outSuffix").value : "").trim(),
        saveConfig: state.mode === "llm",
        llm: llmCfg
      }
    };

    var res = await API.startPipeline(payload);
    if (!res.ok) {
      state.polling = false;
      setRunningUI(false);
      stopElapsedTimer();
      updateRunProgressUI({ state: "error", stage: "error", progress: 0, message: res.error || "任务提交失败" });
      showErrorPanel(res.error || "任务提交失败", "", false);
      return;
    }

    state.jobId = (res.data && res.data.job) || "";
    pollLoop();
  }

  async function pollLoop() {
    if (!state.polling || !state.jobId) return;
    var res = await API.getJob(state.jobId);
    if (!res.ok) {
      if (res.networkError) {
        state.polling = false;
        setRunningUI(false);
        stopElapsedTimer();
        updateRunProgressUI({ state: "error", stage: "error", progress: 0, message: res.error });
        showErrorPanel("与本地服务断联，已恢复操作按钮；可点击右侧按钮重连轮询。", res.error, true);
        return;
      }
      state.polling = false;
      setRunningUI(false);
      stopElapsedTimer();
      updateRunProgressUI({ state: "error", stage: "error", progress: 0, message: res.error });
      showErrorPanel(res.error || "查询任务失败", "", false);
      return;
    }

    var j = res.data || {};
    state.jobData = j;
    updateRunProgressUI(j);
    if (j.summary) updateKpis(j);

    if (j.state === "done") {
      state.polling = false;
      setRunningUI(false);
      stopElapsedTimer(j.elapsed_sec != null ? j.elapsed_sec : (j.summary && j.summary.elapsed_sec));
      onJobCompleted(j);
      return;
    }

    if (j.state === "cancelled") {
      state.polling = false;
      setRunningUI(false);
      stopElapsedTimer(j.elapsed_sec);
      if (j.files && j.files.length) {
        onJobCompleted(j);
      }
      return;
    }

    if (j.state === "error") {
      state.polling = false;
      setRunningUI(false);
      stopElapsedTimer(j.elapsed_sec);
      showErrorPanel(j.error || j.message || "任务运行失败", j.trace || "", false);
      return;
    }

    state.pollTimer = setTimeout(pollLoop, 450);
  }

  async function cancelCurrentTask() {
    if (!state.jobId) return;
    if ($("btnCancelTask")) $("btnCancelTask").disabled = true;
    if ($("runMessage")) $("runMessage").textContent = "正在请求中止任务…";
    await API.cancelJob(state.jobId);
  }

  function resumePolling() {
    if (!state.jobId) return;
    clearErrorPanel();
    state.polling = true;
    setRunningUI(true);
    startElapsedTimer();
    pollLoop();
  }

  /* ------------------------------------------------------------------------
     任务完成后：多文件切换、审校清单、全文 Diff、审计报告与 Sticky Bar
     ------------------------------------------------------------------------ */
  function onJobCompleted(job) {
    var files = (job && job.files) || [];
    if (files.length) {
      var stillExists = files.some(function (f) { return f.file_id === state.activeFileId; });
      if (!stillExists) {
        state.activeFileId = files[0].file_id;
      }
    } else {
      state.activeFileId = "";
    }
    state.itemsPage = 1;
    state.diffPage = 1;

    renderFileSwitcher(files);
    updateStickyActionBar();
    refreshActiveTab();
  }

  function renderFileSwitcher(files) {
    var card = $("fileSwitcherCard");
    var bar = $("fileSwitcherBar");
    if (!card || !bar) return;
    bar.textContent = "";

    if (!files || !files.length) {
      card.hidden = true;
      return;
    }
    card.hidden = false;

    files.forEach(function (f) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "file-chip" + (f.file_id === state.activeFileId ? " is-active" : "");

      if (f.dirty) {
        var dot = document.createElement("span");
        dot.className = "dirty-dot";
        dot.title = "有未应用修改 (dirty)";
        btn.appendChild(dot);
      }

      var nameEl = document.createElement("span");
      nameEl.className = "file-chip__name";
      nameEl.textContent = f.name;
      btn.appendChild(nameEl);

      var cntEl = document.createElement("span");
      cntEl.className = "mono";
      cntEl.style.fontSize = "11px";
      cntEl.style.color = "var(--ink-muted)";
      cntEl.textContent = "(" + (f.accepted_count || 0) + "/" + (f.item_count || 0) + ")";
      btn.appendChild(cntEl);

      btn.addEventListener("click", function () {
        if (state.activeFileId === f.file_id) return;
        state.activeFileId = f.file_id;
        state.itemsPage = 1;
        state.diffPage = 1;
        drawerCtrl.close();
        renderFileSwitcher(files);
        updateStickyActionBar();
        refreshActiveTab();
      });

      bar.appendChild(btn);
    });
  }

  function updateStickyActionBar() {
    var bar = $("stickyActionBar");
    var files = (state.jobData && state.jobData.files) || [];
    if (!bar) return;
    if (!files.length) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;

    var dirtyFiles = files.filter(function (f) { return !!f.dirty; });
    var active = getActiveFileMeta();

    var dot = $("stickyDirtyDot");
    var hint = $("stickyDirtyHint");
    var applyBtn = $("btnApplyChanges");
    var rollAutoBtn = $("btnRollbackAuto");

    if (dirtyFiles.length > 0) {
      if (dot) dot.hidden = false;
      if (hint) hint.textContent = "当前有 " + dirtyFiles.length + " 个文件含未落盘审校修改（dirty）";
      if (applyBtn) {
        applyBtn.disabled = false;
        applyBtn.classList.add("is-dirty-pulse");
      }
    } else {
      if (dot) dot.hidden = true;
      if (hint) hint.textContent = "终稿已与当前人工审校决策同步";
      if (applyBtn) {
        applyBtn.disabled = true;
        applyBtn.classList.remove("is-dirty-pulse");
      }
    }

    if (rollAutoBtn) {
      rollAutoBtn.disabled = !(active && active.auto_path);
    }
  }

  function refreshActiveTab() {
    if (!state.jobId) return;
    if (state.resultTab === "review") {
      loadReviewItems();
    } else if (state.resultTab === "diff") {
      loadFullDiff();
    } else {
      renderReportPanel();
    }
  }

  /* ------------------------------------------------------------------------
     Tab 1: 审校清单（分页 + 类型/状态/关键词筛选 + 批量 & 单条采纳撤回）
     ------------------------------------------------------------------------ */
  async function loadReviewItems() {
    var container = $("reviewItemsContainer");
    if (!container || !state.jobId) return;

    var res = await API.getJobItems({
      job: state.jobId,
      file_id: state.activeFileId,
      kind: state.itemsKind,
      status: state.itemsStatus,
      q: state.itemsQuery,
      page: state.itemsPage,
      page_size: state.itemsPageSize
    });

    if (!res.ok) {
      container.textContent = "";
      var err = document.createElement("div");
      err.className = "empty-state";
      err.textContent = res.error || "加载审校清单失败";
      container.appendChild(err);
      return;
    }

    var d = res.data || {};
    state.currentItems = d.items || [];
    state.itemsTotalPages = Math.max(1, Number(d.total_pages || 1));
    updateKindCounts(d.counts || {});
    renderReviewList(state.currentItems, d.total || state.currentItems.length);
  }

  function updateKindCounts(counts) {
    var pills = document.querySelectorAll("#kindFilters .filter-pill");
    for (var i = 0; i < pills.length; i++) {
      var pill = pills[i];
      var kind = pill.getAttribute("data-kind") || "";
      var cntEl = pill.querySelector("[data-count]");
      if (cntEl) {
        if (!kind) {
          var sum = counts.all != null
            ? counts.all
            : (Number(counts.ad_line || 0) + Number(counts.auto_delete || 0) +
               Number(counts.llm_delete || 0) + Number(counts.llm_keep || 0) + Number(counts.uncertain || 0));
          cntEl.textContent = String(sum);
        } else {
          cntEl.textContent = String(counts[kind] || 0);
        }
      }
    }
  }

  function renderReviewList(items, total) {
    var container = $("reviewItemsContainer");
    var pag = $("reviewPagination");
    var pageInfo = $("reviewPageInfo");
    if (!container) return;
    container.textContent = "";

    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      var t = document.createElement("div");
      t.className = "empty-state__title";
      t.textContent = "当前筛选条件下无审校条目";
      var sub = document.createElement("div");
      sub.textContent = "可尝试切换类型标签、状态筛选或清空搜索关键词。";
      empty.appendChild(t);
      empty.appendChild(sub);
      container.appendChild(empty);
    } else {
      var listEl = document.createElement("div");
      listEl.className = "review-list";

      items.forEach(function (it, idx) {
        var row = document.createElement("div");
        row.className = "review-row" +
          (it.accepted ? "" : " is-rejected") +
          (drawerCtrl.isOpen() && state.drawerItemIndex === idx ? " is-selected" : "");

        // 左侧元信息（行号 + 状态）
        var meta = document.createElement("div");
        meta.className = "review-row__meta";

        var lineEl = document.createElement("div");
        lineEl.className = "review-row__line";
        lineEl.textContent = "L" + (it.line_no || "?");

        var stateBadge = document.createElement("span");
        setStatusBadge(
          stateBadge,
          it.accepted ? "已采纳" : "已撤回",
          it.accepted ? "pill-tag--green" : "pill-tag--amber"
        );
        meta.appendChild(lineEl);
        meta.appendChild(stateBadge);

        // 中部正文（来源/类型/置信度 + 字符级 Diff + 理由）
        var body = document.createElement("div");
        body.className = "review-row__body";

        var badges = document.createElement("div");
        badges.className = "review-row__badges";

        var km = KIND_META[it.kind] || { label: it.kind || "改动", cls: "" };
        var kindTag = document.createElement("span");
        setStatusBadge(kindTag, km.label, km.cls);
        badges.appendChild(kindTag);

        if (it.source) {
          var srcTag = document.createElement("span");
          setStatusBadge(srcTag, "来源: " + it.source, "");
          badges.appendChild(srcTag);
        }
        if (it.confidence !== undefined && it.confidence !== null && it.confidence !== "") {
          var confTag = document.createElement("span");
          confTag.className = "pill-tag mono";
          var numConf = Number(it.confidence);
          confTag.textContent = "置信 " + (isFinite(numConf) ? numConf.toFixed(2) : it.confidence);
          badges.appendChild(confTag);
        }

        var diffBox = document.createElement("div");
        diffBox.className = "review-row__diff";
        Diff.renderInlineDiff(diffBox, it.before || "", it.after || "");

        body.appendChild(badges);
        body.appendChild(diffBox);

        if (it.reason) {
          var reasonEl = document.createElement("div");
          reasonEl.className = "review-row__reason";
          reasonEl.textContent = "依据：" + it.reason;
          body.appendChild(reasonEl);
        }

        // 右侧操作开关
        var actions = document.createElement("div");
        actions.className = "review-row__actions";

        var toggleBtn = document.createElement("button");
        toggleBtn.type = "button";
        toggleBtn.className = "btn btn--sm " + (it.accepted ? "btn--danger" : "btn--accept");
        toggleBtn.textContent = it.accepted ? "撤回恢复" : "采纳改动";
        toggleBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          toggleSingleDecision(it, !it.accepted);
        });

        var detailBtn = document.createElement("button");
        detailBtn.type = "button";
        detailBtn.className = "btn btn--sm";
        detailBtn.textContent = "上下文详情";
        detailBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          openDrawerAt(idx);
        });

        actions.appendChild(toggleBtn);
        actions.appendChild(detailBtn);

        row.appendChild(meta);
        row.appendChild(body);
        row.appendChild(actions);

        row.addEventListener("click", function () { openDrawerAt(idx); });
        listEl.appendChild(row);
      });

      container.appendChild(listEl);
    }

    if (pag) {
      pag.hidden = false;
      if (pageInfo) {
        pageInfo.textContent = "第 " + state.itemsPage + " / " + state.itemsTotalPages + " 页 · 共 " + total + " 条";
      }
      if ($("btnReviewPrev")) $("btnReviewPrev").disabled = state.itemsPage <= 1;
      if ($("btnReviewNext")) $("btnReviewNext").disabled = state.itemsPage >= state.itemsTotalPages;
    }
  }

  async function toggleSingleDecision(item, nextAccepted) {
    if (!state.jobId || !item) return;
    var res = await API.postDecisions({
      job: state.jobId,
      updates: [{ id: item.id, accepted: !!nextAccepted }]
    });
    if (!res.ok) {
      $("runMessage").textContent = "更新决策失败：" + (res.error || "");
      return;
    }
    syncJobPatchFromResponse(res.data);
    await loadReviewItems();
    if (drawerCtrl.isOpen() && state.drawerItemIndex >= 0 && state.currentItems[state.drawerItemIndex]) {
      renderDrawerContent(state.currentItems[state.drawerItemIndex]);
    }
  }

  async function bulkUpdateDecisions(accepted) {
    if (!state.jobId) return;
    var res = await API.postDecisions({
      job: state.jobId,
      bulk: {
        file_id: state.activeFileId,
        kind: state.itemsKind,
        status: state.itemsStatus,
        q: state.itemsQuery,
        accepted: !!accepted
      }
    });
    if (!res.ok) {
      $("runMessage").textContent = "批量更新失败：" + (res.error || "");
      return;
    }
    syncJobPatchFromResponse(res.data);
    await loadReviewItems();
  }

  function syncJobPatchFromResponse(data) {
    if (!data || !state.jobData) return;
    if (data.summary) state.jobData.summary = data.summary;
    if (data.files) state.jobData.files = data.files;
    updateKpis(state.jobData);
    renderFileSwitcher(state.jobData.files || []);
    updateStickyActionBar();
  }

  /* ------------------------------------------------------------------------
     右侧弹簧抽屉（上下文、字符 Diff、快捷键 A / R / J / K / Esc）
     ------------------------------------------------------------------------ */
  function openDrawerAt(idx) {
    if (idx < 0 || idx >= state.currentItems.length) return;
    state.drawerItemIndex = idx;
    renderDrawerContent(state.currentItems[idx]);
    drawerCtrl.open();
    // 高亮选中行
    var rows = document.querySelectorAll("#reviewItemsContainer .review-row");
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.toggle("is-selected", i === idx);
    }
  }

  function renderDrawerContent(item) {
    if (!item) return;
    if ($("drawerTitle")) {
      $("drawerTitle").textContent = "第 " + (item.line_no || "?") + " 行 · " + (baseName((getActiveFileMeta() || {}).name) || "");
    }

    var badges = $("drawerBadges");
    if (badges) {
      badges.textContent = "";
      var km = KIND_META[item.kind] || { label: item.kind || "条目", cls: "" };
      var kTag = document.createElement("span");
      setStatusBadge(kTag, km.label, km.cls);
      badges.appendChild(kTag);

      var sTag = document.createElement("span");
      setStatusBadge(sTag, item.accepted ? "当前已采纳" : "当前已撤回", item.accepted ? "pill-tag--green" : "pill-tag--amber");
      badges.appendChild(sTag);

      if (item.source) {
        var srcTag = document.createElement("span");
        setStatusBadge(srcTag, "来源: " + item.source, "");
        badges.appendChild(srcTag);
      }
      if (item.confidence !== undefined && item.confidence !== null && item.confidence !== "") {
        var cTag = document.createElement("span");
        cTag.className = "pill-tag mono";
        cTag.textContent = "置信度 " + Number(item.confidence).toFixed(2);
        badges.appendChild(cTag);
      }
    }

    if ($("drawerDiff")) {
      Diff.renderInlineDiff($("drawerDiff"), item.before || "", item.after || "");
    }

    var ctxEl = $("drawerContext");
    if (ctxEl) {
      ctxEl.textContent = "";
      var prev = item.context_prev || [];
      var next = item.context_next || [];
      var baseLine = Number(item.line_no || 1);

      prev.forEach(function (lineText, i) {
        var ln = baseLine - (prev.length - i);
        ctxEl.appendChild(buildContextLineNode(ln > 0 ? ln : "—", lineText, false));
      });
      ctxEl.appendChild(buildContextLineNode(baseLine, item.before || "", true));
      next.forEach(function (lineText, i) {
        ctxEl.appendChild(buildContextLineNode(baseLine + i + 1, lineText, false));
      });
    }

    if ($("drawerReason")) {
      $("drawerReason").textContent = item.reason || "（无额外文字理由，由引擎规则/模型特征直接定位）";
    }

    // 不可见字符透视
    var invisWrap = $("drawerInvisWrap");
    var invisEl = $("drawerInvis");
    var parsedBefore = Diff.visibleText(item.before || "");
    if (parsedBefore.plain !== (item.before || "")) {
      if (invisWrap) invisWrap.hidden = false;
      if (invisEl) {
        invisEl.textContent = "";
        Diff.appendVisibleTextNodes(invisEl, item.before || "");
      }
    } else if (invisWrap) {
      invisWrap.hidden = true;
    }

    if ($("drawerBtnAccept")) $("drawerBtnAccept").disabled = !!item.accepted;
    if ($("drawerBtnReject")) $("drawerBtnReject").disabled = !item.accepted;
    if ($("drawerBtnPrev")) $("drawerBtnPrev").disabled = state.drawerItemIndex <= 0;
    if ($("drawerBtnNext")) $("drawerBtnNext").disabled = state.drawerItemIndex >= state.currentItems.length - 1;
  }

  function buildContextLineNode(lineNum, text, isTarget) {
    var row = document.createElement("div");
    row.className = "context-line" + (isTarget ? " is-target" : "");
    var num = document.createElement("span");
    num.className = "context-line__num";
    num.textContent = String(lineNum);
    var txt = document.createElement("span");
    Diff.appendVisibleTextNodes(txt, text || "");
    row.appendChild(num);
    row.appendChild(txt);
    return row;
  }

  /* ------------------------------------------------------------------------
     Tab 2: 全文对比 Diff
     ------------------------------------------------------------------------ */
  async function loadFullDiff() {
    var tbody = $("fullDiffTableBody");
    if (!tbody || !state.jobId) return;

    var res = await API.getJobDiff({
      job: state.jobId,
      file_id: state.activeFileId,
      only_changed: state.diffOnlyChanged,
      page: state.diffPage,
      page_size: state.diffPageSize
    });

    tbody.textContent = "";
    if (!res.ok) {
      var errTr = document.createElement("tr");
      var errTd = document.createElement("td");
      errTd.colSpan = 4;
      errTd.style.textAlign = "center";
      errTd.style.color = "var(--proof-red)";
      errTd.textContent = res.error || "加载全文对比失败";
      errTr.appendChild(errTd);
      tbody.appendChild(errTr);
      return;
    }

    var d = res.data || {};
    var rows = d.lines || d.rows || [];
    state.diffTotalPages = Math.max(1, Number(d.total_pages || 1));

    if (!rows.length) {
      var emptyTr = document.createElement("tr");
      var emptyTd = document.createElement("td");
      emptyTd.colSpan = 4;
      emptyTd.style.textAlign = "center";
      emptyTd.style.padding = "28px";
      emptyTd.style.color = "var(--ink-muted)";
      emptyTd.textContent = "本文稿当前筛选下没有改动行（洁净文稿零误删）。";
      emptyTr.appendChild(emptyTd);
      tbody.appendChild(emptyTr);
    } else {
      rows.forEach(function (r) {
        var tr = document.createElement("tr");
        if (r.changed) tr.className = "is-changed";

        var tdLine = document.createElement("td");
        tdLine.className = "diff-col-line";
        tdLine.textContent = String(r.line_no || "");

        var tdBefore = document.createElement("td");
        var tdAfter = document.createElement("td");
        Diff.renderSplitDiff(tdBefore, tdAfter, r.before || "", r.after || "");

        var tdTag = document.createElement("td");
        tdTag.className = "diff-col-tag";
        (r.kinds || []).forEach(function (k) {
          var km = KIND_META[k] || { label: k, cls: "" };
          var tag = document.createElement("span");
          setStatusBadge(tag, km.label, km.cls);
          tag.style.marginRight = "4px";
          tag.style.marginBottom = "4px";
          tdTag.appendChild(tag);
        });

        tr.appendChild(tdLine);
        tr.appendChild(tdBefore);
        tr.appendChild(tdAfter);
        tr.appendChild(tdTag);
        tbody.appendChild(tr);
      });
    }

    var pag = $("diffPagination");
    var info = $("diffPageInfo");
    if (pag) {
      pag.hidden = false;
      if (info) {
        info.textContent = "第 " + state.diffPage + " / " + state.diffTotalPages + " 页 · 共 " + (d.total || rows.length) + " 行";
      }
      if ($("btnDiffPrev")) $("btnDiffPrev").disabled = state.diffPage <= 1;
      if ($("btnDiffNext")) $("btnDiffNext").disabled = state.diffPage >= state.diffTotalPages;
    }
  }

  /* ------------------------------------------------------------------------
     Tab 3: 导出与审计报告
     ------------------------------------------------------------------------ */
  function renderReportPanel() {
    var box = $("reportSummaryBody");
    if (!box) return;
    box.textContent = "";

    var job = state.jobData;
    var files = (job && job.files) || [];
    if (!job || !files.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "暂无已完成的审计报告。";
      box.appendChild(empty);
      return;
    }

    var s = job.summary || {};
    var metaCard = document.createElement("div");
    metaCard.className = "field-hint";
    metaCard.style.marginBottom = "12px";
    metaCard.textContent =
      "任务编号：" + (job.job || state.jobId) +
      " · 模式：" + (job.mode === "llm" ? "LLM 深度复核" : "自动清洗") +
      " · 语言模型：" + (s.lm_mode === "file" ? "外部模型文件" : "按文稿自动平滑构建") +
      " · LLM 调用：" + (s.llm_called || 0) + " 次（删 " + (s.llm_deleted || 0) + " / 留 " + (s.llm_kept || 0) + " / 待定 " + (s.llm_uncertain || 0) + "）";
    box.appendChild(metaCard);

    var wrap = document.createElement("div");
    wrap.className = "diff-table-wrap";
    var table = document.createElement("table");
    table.className = "diff-table";

    var thead = document.createElement("thead");
    var headTr = document.createElement("tr");
    ["文件名", "编码", "总行数", "审校条目（已采纳/总计）", "净化字数", "输出位置"].forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      headTr.appendChild(th);
    });
    thead.appendChild(headTr);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    files.forEach(function (f) {
      var tr = document.createElement("tr");

      var c1 = document.createElement("td");
      c1.textContent = f.name + (f.dirty ? " [未应用修改]" : "");

      var c2 = document.createElement("td");
      c2.className = "mono";
      c2.textContent = f.encoding || "utf-8";

      var c3 = document.createElement("td");
      c3.className = "mono";
      c3.textContent = String(f.total_lines || 0);

      var c4 = document.createElement("td");
      c4.className = "mono";
      c4.textContent = (f.accepted_count || 0) + " / " + (f.item_count || 0);

      var c5 = document.createElement("td");
      c5.className = "mono";
      c5.textContent = String(f.removed_chars || 0);

      var c6 = document.createElement("td");
      c6.className = "mono";
      c6.style.fontSize = "12px";
      c6.textContent = f.output_path || "—";

      tr.appendChild(c1);
      tr.appendChild(c2);
      tr.appendChild(c3);
      tr.appendChild(c4);
      tr.appendChild(c5);
      tr.appendChild(c6);
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  /* ------------------------------------------------------------------------
     Sticky Bar Actions（应用重生成、回滚、下载、ZIP、定位）
     ------------------------------------------------------------------------ */
  async function applyChanges() {
    if (!state.jobId || !state.jobData) return;
    var btn = $("btnApplyChanges");
    if (btn) btn.disabled = true;
    $("runMessage").textContent = "正在根据当前人工审校重新生成终稿…";

    var res = await API.applyJob({ job: state.jobId });
    if (!res.ok) {
      $("runMessage").textContent = "重生成终稿失败：" + (res.error || "");
      updateStickyActionBar();
      return;
    }
    syncJobPatchFromResponse(res.data);
    $("runMessage").textContent = "终稿已按最新审校决策原子写盘完成。";
    refreshActiveTab();
  }

  async function rollbackTarget(target) {
    if (!state.jobId || !state.activeFileId) return;
    var label = target === "auto" ? "自动清洗初稿" : "原始文本";
    if (!root.confirm("确认将当前文稿回滚至「" + label + "」并重写终稿文件？")) return;

    var res = await API.rollbackJob({
      job: state.jobId,
      file_id: state.activeFileId,
      target: target
    });
    if (!res.ok) {
      $("runMessage").textContent = "回滚失败：" + (res.error || "");
      return;
    }
    syncJobPatchFromResponse(res.data);
    $("runMessage").textContent = "已回滚至「" + label + "」并写盘。";
    refreshActiveTab();
  }

  function triggerDownloadUrl(url, filename) {
    var a = document.createElement("a");
    a.href = url;
    if (filename) a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function downloadCurrentFile() {
    var active = getActiveFileMeta();
    if (!state.jobId || !active) return;
    var url = API.getDownloadUrl(state.jobId, active.file_id, active.output_path);
    triggerDownloadUrl(url, active.name);
  }

  function exportAllZip() {
    if (!state.jobId) return;
    var url = API.getExportZipUrl(state.jobId);
    triggerDownloadUrl(url, "adclean_" + state.jobId + ".zip");
  }

  async function revealCurrentOutput() {
    var active = getActiveFileMeta();
    if (!active) return;
    var path = active.output_path || "";
    if (root.pywebview && root.pywebview.api && root.pywebview.api.open_path && path) {
      root.pywebview.api.open_path(path);
      return;
    }
    var res = await API.revealOutput({ job: state.jobId, file_id: active.file_id, path: path });
    if (!res.ok) {
      $("runMessage").textContent = "定位失败：" + (res.error || "");
    }
  }

  /* ------------------------------------------------------------------------
     事件绑定与初始化
     ------------------------------------------------------------------------ */
  function bindEvents() {
    // 拖拽区与文件选择
    var drop = $("drop");
    if (drop) {
      drop.addEventListener("click", function () {
        if (root.__adcleanBridgeHooked) return;
        if ($("pickFiles")) $("pickFiles").click();
      });
      drop.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          if (root.__adcleanBridgeHooked && typeof drop.onclick === "function") {
            drop.onclick(ev);
            return;
          }
          if ($("pickFiles")) $("pickFiles").click();
        }
      });
      ["dragenter", "dragover"].forEach(function (evtName) {
        drop.addEventListener(evtName, function (ev) {
          ev.preventDefault();
          drop.classList.add("hot");
        });
      });
      ["dragleave", "drop"].forEach(function (evtName) {
        drop.addEventListener(evtName, function (ev) {
          ev.preventDefault();
          drop.classList.remove("hot");
        });
      });
      drop.addEventListener("drop", async function (ev) {
        // 如果桌面壳有原生接管回调，则优先走桥接处理器
        if (typeof root.__adcleanBridgeDrop === "function") {
          var handled = await root.__adcleanBridgeDrop(ev);
          if (handled) return;
        }
        var dt = ev.dataTransfer;
        if (!dt) return;
        var items = dt.items;
        if (items && items.length && items[0].webkitGetAsEntry) {
          var collected = [];
          for (var i = 0; i < items.length; i++) {
            var en = items[i].webkitGetAsEntry();
            if (en) await walkEntry(en, collected);
          }
          if (collected.length) {
            await handleBrowserUploadFiles(collected);
            return;
          }
        }
        await handleBrowserUploadFiles(dt.files || []);
      });
    }

    if ($("btnPickFiles")) {
      $("btnPickFiles").addEventListener("click", function () {
        if (root.__adcleanBridgeHooked) return;
        if ($("pickFiles")) $("pickFiles").click();
      });
    }
    if ($("btnPickDir")) {
      $("btnPickDir").addEventListener("click", function () {
        if (root.__adcleanBridgeHooked) return;
        if ($("pickDir")) $("pickDir").click();
      });
    }
    if ($("btnPickOut")) {
      $("btnPickOut").addEventListener("click", function () {
        if (root.__adcleanBridgeHooked) return;
        var drawer = $("localImportDrawer");
        if (drawer) drawer.open = true;
        browseTo(($("outDir") && $("outDir").value.trim()) || "");
      });
    }
    if ($("pickFiles")) {
      $("pickFiles").addEventListener("change", function (ev) {
        handleBrowserUploadFiles(ev.target.files);
        ev.target.value = "";
      });
    }
    if ($("pickDir")) {
      $("pickDir").addEventListener("change", function (ev) {
        handleBrowserUploadFiles(ev.target.files);
        ev.target.value = "";
      });
    }
    if ($("btnClearQueue")) {
      $("btnClearQueue").addEventListener("click", clearQueue);
    }

    // 本机目录浏览
    if ($("btnBrowseSrv")) {
      $("btnBrowseSrv").addEventListener("click", function () { browseTo(""); });
    }
    if ($("btnBrowseGo")) {
      $("btnBrowseGo").addEventListener("click", function () {
        browseTo(($("browsePath") && $("browsePath").value) || "");
      });
    }
    if ($("btnBrowseDrives")) {
      $("btnBrowseDrives").addEventListener("click", function () { browseTo(""); });
    }

    if ($("btnOpenOut")) {
      $("btnOpenOut").addEventListener("click", function () {
        if (root.__adcleanBridgeHooked) return;
        var outPath = ($("outDir") && $("outDir").value.trim()) || "";
        if (!outPath) return;
        API.revealOutput({ path: outPath });
      });
    }

    // LLM 配置联动：选下拉时自动清空手动覆盖框
    if ($("llmModel")) {
      $("llmModel").addEventListener("change", function () {
        if ($("llmModel").value && $("llmModelManual")) {
          $("llmModelManual").value = "";
        }
      });
    }
    if ($("btnSaveLlm")) $("btnSaveLlm").addEventListener("click", saveLlmConfig);
    if ($("btnClearKey")) $("btnClearKey").addEventListener("click", clearLlmKey);
    if ($("btnModels")) $("btnModels").addEventListener("click", fetchModels);
    if ($("btnProbe")) $("btnProbe").addEventListener("click", probeLlm);

    // 主执行 & 中止 & 重连
    if ($("btnRunTask")) $("btnRunTask").addEventListener("click", startTask);
    if ($("btnCancelTask")) $("btnCancelTask").addEventListener("click", cancelCurrentTask);
    if ($("btnRetryPoll")) $("btnRetryPoll").addEventListener("click", resumePolling);

    // 审校筛选与批量操作
    var kindFilters = $("kindFilters");
    if (kindFilters) {
      kindFilters.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest("[data-kind]") : null;
        if (!btn) return;
        state.itemsKind = btn.getAttribute("data-kind") || "";
        state.itemsPage = 1;
        var all = kindFilters.querySelectorAll("[data-kind]");
        for (var i = 0; i < all.length; i++) {
          all[i].classList.toggle("is-active", all[i] === btn);
        }
        loadReviewItems();
      });
    }
    if ($("filterStatus")) {
      $("filterStatus").addEventListener("change", function () {
        state.itemsStatus = $("filterStatus").value;
        state.itemsPage = 1;
        loadReviewItems();
      });
    }
    if ($("filterQuery")) {
      $("filterQuery").addEventListener("input", function () {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(function () {
          state.itemsQuery = ($("filterQuery").value || "").trim();
          state.itemsPage = 1;
          loadReviewItems();
        }, 260);
      });
    }
    if ($("btnBulkAccept")) $("btnBulkAccept").addEventListener("click", function () { bulkUpdateDecisions(true); });
    if ($("btnBulkReject")) $("btnBulkReject").addEventListener("click", function () { bulkUpdateDecisions(false); });

    if ($("btnReviewPrev")) {
      $("btnReviewPrev").addEventListener("click", function () {
        if (state.itemsPage > 1) { state.itemsPage--; loadReviewItems(); }
      });
    }
    if ($("btnReviewNext")) {
      $("btnReviewNext").addEventListener("click", function () {
        if (state.itemsPage < state.itemsTotalPages) { state.itemsPage++; loadReviewItems(); }
      });
    }

    // 全文 Diff 控件
    if ($("chkOnlyChanged")) {
      $("chkOnlyChanged").addEventListener("change", function () {
        state.diffOnlyChanged = !!$("chkOnlyChanged").checked;
        state.diffPage = 1;
        loadFullDiff();
      });
    }
    if ($("btnRefreshDiff")) {
      $("btnRefreshDiff").addEventListener("click", function () { loadFullDiff(); });
    }
    if ($("btnDiffPrev")) {
      $("btnDiffPrev").addEventListener("click", function () {
        if (state.diffPage > 1) { state.diffPage--; loadFullDiff(); }
      });
    }
    if ($("btnDiffNext")) {
      $("btnDiffNext").addEventListener("click", function () {
        if (state.diffPage < state.diffTotalPages) { state.diffPage++; loadFullDiff(); }
      });
    }

    // Sticky Bar 动作
    if ($("btnApplyChanges")) $("btnApplyChanges").addEventListener("click", applyChanges);
    if ($("btnRollbackAuto")) $("btnRollbackAuto").addEventListener("click", function () { rollbackTarget("auto"); });
    if ($("btnRollbackOrig")) $("btnRollbackOrig").addEventListener("click", function () { rollbackTarget("original"); });
    if ($("btnDownloadCurrent")) $("btnDownloadCurrent").addEventListener("click", downloadCurrentFile);
    if ($("btnExportZip")) $("btnExportZip").addEventListener("click", exportAllZip);
    if ($("btnRevealCurrent")) $("btnRevealCurrent").addEventListener("click", revealCurrentOutput);

    // 抽屉内操作按钮
    if ($("drawerClose")) $("drawerClose").addEventListener("click", function () { drawerCtrl.close(); });
    if ($("drawerBtnAccept")) {
      $("drawerBtnAccept").addEventListener("click", function () {
        var it = state.currentItems[state.drawerItemIndex];
        if (it && !it.accepted) toggleSingleDecision(it, true);
      });
    }
    if ($("drawerBtnReject")) {
      $("drawerBtnReject").addEventListener("click", function () {
        var it = state.currentItems[state.drawerItemIndex];
        if (it && it.accepted) toggleSingleDecision(it, false);
      });
    }
    if ($("drawerBtnPrev")) {
      $("drawerBtnPrev").addEventListener("click", function () {
        if (state.drawerItemIndex > 0) openDrawerAt(state.drawerItemIndex - 1);
      });
    }
    if ($("drawerBtnNext")) {
      $("drawerBtnNext").addEventListener("click", function () {
        if (state.drawerItemIndex < state.currentItems.length - 1) openDrawerAt(state.drawerItemIndex + 1);
      });
    }

    // 全局快捷键：A 采纳 / R 撤回 / J 下一条 / K 上一条 / Esc 关闭
    document.addEventListener("keydown", function (ev) {
      var tag = ev.target && ev.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        if (ev.key === "Escape" && drawerCtrl.isOpen()) drawerCtrl.close();
        return;
      }
      if (!drawerCtrl.isOpen()) return;
      var k = (ev.key || "").toLowerCase();
      var item = state.currentItems[state.drawerItemIndex];
      if (k === "escape") {
        ev.preventDefault();
        drawerCtrl.close();
      } else if (k === "a" && item) {
        ev.preventDefault();
        if (!item.accepted) toggleSingleDecision(item, true);
      } else if (k === "r" && item) {
        ev.preventDefault();
        if (item.accepted) toggleSingleDecision(item, false);
      } else if (k === "j") {
        ev.preventDefault();
        if (state.drawerItemIndex < state.currentItems.length - 1) {
          openDrawerAt(state.drawerItemIndex + 1);
        }
      } else if (k === "k") {
        ev.preventDefault();
        if (state.drawerItemIndex > 0) {
          openDrawerAt(state.drawerItemIndex - 1);
        }
      }
    });
  }

  function init() {
    scrollProgress = Motion.initScrollProgress($("scrollProgressBar"));
    Motion.initUnderlineReveal(document);
    Motion.triggerLineReveal(document);
    Motion.staggerFadeInUp(document.querySelectorAll(".sheet-card"), 45);

    modeTabsCtrl = Motion.createSlidingTabs($("modeTabs"), onModeChange);
    resultTabsCtrl = Motion.createSlidingTabs($("resultTabs"), function (tabName) {
      state.resultTab = tabName || "review";
      if ($("panelReview")) $("panelReview").hidden = state.resultTab !== "review";
      if ($("panelDiff")) $("panelDiff").hidden = state.resultTab !== "diff";
      if ($("panelReport")) $("panelReport").hidden = state.resultTab !== "report";
      refreshActiveTab();
    });

    drawerCtrl = Motion.createSpringDrawer($("reviewDrawer"), $("drawerBackdrop"), {
      onClose: function () {
        var rows = document.querySelectorAll("#reviewItemsContainer .review-row.is-selected");
        for (var i = 0; i < rows.length; i++) rows[i].classList.remove("is-selected");
      }
    });

    bindEvents();
    loadLlmConfig();

    // 若浏览器窗口尺寸变化，重新对齐滑动底线
    root.addEventListener("resize", function () {
      if (modeTabsCtrl) modeTabsCtrl.refresh();
      if (resultTabsCtrl) resultTabsCtrl.refresh();
    });
  }

  root.addServerFiles = addServerFiles;
  root.handleBrowserUploadFiles = handleBrowserUploadFiles;
  root.AdcleanApp = {
    getState: function () { return state; },
    addServerFiles: addServerFiles,
    loadLlmConfig: loadLlmConfig
  };

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
