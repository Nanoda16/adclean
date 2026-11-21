/* ==========================================================================
   Adclean API Client Layer
   封装前后端全部 REST 契约：文件上传、本机目录浏览、LLM 配置探测、任务控制、审校闭环与导出
   ========================================================================== */
(function (root) {
  "use strict";

  async function requestJSON(url, options) {
    var opt = Object.assign({}, options || {});
    var headers = Object.assign({}, opt.headers || {});
    if (opt.json !== undefined) {
      opt.method = opt.method || "POST";
      headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(opt.json);
      delete opt.json;
    }
    opt.headers = headers;

    var t0 = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
    try {
      var resp = await fetch(url, opt);
      var latencyMs = Math.round(((typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now()) - t0);
      var text = await resp.text();
      var data = null;
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_e) {
        return {
          ok: false,
          status: resp.status,
          latencyMs: latencyMs,
          networkError: false,
          data: null,
          error: "服务端返回非 JSON 响应（HTTP " + resp.status + "）：" + text.slice(0, 180)
        };
      }
      var hasApiError = !resp.ok || (data && data.error) || (data && data.ok === false);
      return {
        ok: !hasApiError,
        status: resp.status,
        latencyMs: latencyMs,
        networkError: false,
        data: data,
        error: hasApiError ? ((data && data.error) || ("请求失败（HTTP " + resp.status + "）")) : ""
      };
    } catch (err) {
      var failLatency = Math.round(((typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now()) - t0);
      return {
        ok: false,
        status: 0,
        latencyMs: failLatency,
        networkError: true,
        data: null,
        error: "与本地服务断联或网络异常：" + ((err && err.message) || String(err))
      };
    }
  }

  /**
   * 浏览器真实文件流式上传：POST /api/upload?filename=...&session_id=...
   */
  function uploadFile(file, sessionId, onProgress) {
    return new Promise(function (resolve) {
      var qs = "filename=" + encodeURIComponent(file.name || "novel.txt");
      if (sessionId) qs += "&session_id=" + encodeURIComponent(sessionId);
      var url = "/api/upload?" + qs;

      if (typeof XMLHttpRequest !== "undefined") {
        var xhr = new XMLHttpRequest();
        xhr.open("POST", url, true);
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        if (xhr.upload && typeof onProgress === "function") {
          xhr.upload.onprogress = function (ev) {
            if (ev.lengthComputable) {
              onProgress(ev.loaded, ev.total);
            }
          };
        }
        xhr.onload = function () {
          var data = null;
          try {
            data = xhr.responseText ? JSON.parse(xhr.responseText) : {};
          } catch (_e) {
            resolve({ ok: false, status: xhr.status, data: null, error: "上传响应解析失败（HTTP " + xhr.status + "）" });
            return;
          }
          var ok = xhr.status >= 200 && xhr.status < 300 && data && data.ok !== false && !data.error;
          resolve({
            ok: ok,
            status: xhr.status,
            data: data,
            error: ok ? "" : ((data && data.error) || ("上传失败（HTTP " + xhr.status + "）"))
          });
        };
        xhr.onerror = function () {
          resolve({ ok: false, status: 0, networkError: true, data: null, error: "文件上传网络中断" });
        };
        xhr.send(file);
        return;
      }

      requestJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file
      }).then(resolve);
    });
  }

  function clearUploads(sessionId) {
    if (!sessionId) return Promise.resolve({ ok: true, data: { ok: true } });
    return requestJSON("/api/upload/clear", { json: { session_id: sessionId } });
  }

  function getDrives() {
    return requestJSON("/api/drives");
  }

  function browsePath(path) {
    return requestJSON("/api/browse?path=" + encodeURIComponent(path || ""));
  }

  function listFolder(folder, recursive) {
    var rec = recursive ? "true" : "false";
    return requestJSON("/api/list?folder=" + encodeURIComponent(folder || "") + "&recursive=" + rec);
  }

  function getLlmConfig() {
    return requestJSON("/api/llm/config");
  }

  function saveLlmConfig(payload) {
    return requestJSON("/api/llm/save", { json: payload || {} });
  }

  function fetchLlmModels(payload) {
    return requestJSON("/api/llm/models", { json: payload || {} });
  }

  function probeLlm(payload) {
    return requestJSON("/api/llm/probe", { json: payload || {} });
  }

  function startPipeline(payload) {
    return requestJSON("/api/pipeline", { json: payload || {} });
  }

  function getJob(jobId) {
    return requestJSON("/api/job?job=" + encodeURIComponent(jobId || ""));
  }

  function cancelJob(jobId) {
    return requestJSON("/api/job/cancel", { json: { job: jobId } });
  }

  function getJobItems(params) {
    var p = params || {};
    var qs = [
      "job=" + encodeURIComponent(p.job || ""),
      "file_id=" + encodeURIComponent(p.file_id || ""),
      "kind=" + encodeURIComponent(p.kind || ""),
      "status=" + encodeURIComponent(p.status || ""),
      "q=" + encodeURIComponent(p.q || ""),
      "page=" + encodeURIComponent(String(p.page || 1)),
      "page_size=" + encodeURIComponent(String(p.page_size || 40))
    ].join("&");
    return requestJSON("/api/job/items?" + qs);
  }

  function postDecisions(payload) {
    return requestJSON("/api/job/decisions", { json: payload || {} });
  }

  function applyJob(payload) {
    return requestJSON("/api/job/apply", { json: payload || {} });
  }

  function rollbackJob(payload) {
    return requestJSON("/api/job/rollback", { json: payload || {} });
  }

  function getJobDiff(params) {
    var p = params || {};
    var qs = [
      "job=" + encodeURIComponent(p.job || ""),
      "file_id=" + encodeURIComponent(p.file_id || ""),
      "only_changed=" + (p.only_changed === false ? "false" : "true"),
      "page=" + encodeURIComponent(String(p.page || 1)),
      "page_size=" + encodeURIComponent(String(p.page_size || 60))
    ].join("&");
    return requestJSON("/api/job/diff?" + qs);
  }

  function getDownloadUrl(jobId, fileId, fallbackPath) {
    if (jobId && fileId) {
      return "/api/download?job=" + encodeURIComponent(jobId) + "&file_id=" + encodeURIComponent(fileId);
    }
    return "/api/download?path=" + encodeURIComponent(fallbackPath || "");
  }

  function getExportZipUrl(jobId) {
    return "/api/job/export-zip?job=" + encodeURIComponent(jobId || "");
  }

  function revealOutput(payload) {
    return requestJSON("/api/reveal", { json: payload || {} });
  }

  // 向后兼容旧版 window.api(url, body) 返回原始 JSON 对象
  async function legacyApi(path, body) {
    var res = await requestJSON(path, body ? { json: body } : {});
    if (res.data) return res.data;
    return { error: res.error || "请求失败" };
  }

  var apiClient = {
    requestJSON: requestJSON,
    uploadFile: uploadFile,
    clearUploads: clearUploads,
    getDrives: getDrives,
    browsePath: browsePath,
    listFolder: listFolder,
    getLlmConfig: getLlmConfig,
    saveLlmConfig: saveLlmConfig,
    fetchLlmModels: fetchLlmModels,
    probeLlm: probeLlm,
    startPipeline: startPipeline,
    getJob: getJob,
    cancelJob: cancelJob,
    getJobItems: getJobItems,
    postDecisions: postDecisions,
    applyJob: applyJob,
    rollbackJob: rollbackJob,
    getJobDiff: getJobDiff,
    getDownloadUrl: getDownloadUrl,
    getExportZipUrl: getExportZipUrl,
    revealOutput: revealOutput
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = apiClient;
  }
  if (root) {
    root.AdcleanAPI = apiClient;
    root.api = legacyApi;
  }
})(typeof window !== "undefined" ? window : globalThis);
