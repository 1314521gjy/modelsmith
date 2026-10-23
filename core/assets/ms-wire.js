/* ModelSmith v3 wiring layer (R2a/R2b) — final implementation, staged in _tmp.
 * Single file, zero dependencies, ES5. Injected by core before </body>.
 * Never touches design/prototype/** on disk.
 * Inactive unless window.__MS_TOKEN__ exists; ?demo=1 forces off.
 * Demo timers keep running (page IIFEs); this script overwrites the same nodes
 * every 300ms while a real job is active, and freezes final values on terminal.
 * ---------------------------------------------------------------------------
 * CODEMAP BANNER (comments only; no code tokens changed)
 * Map: E:\视频\modelsmith\docs\codemap\MAP-ui-wire.md
 * Role: injected after v3 IIFEs before </body> by core.inject_v3_bootstrap.
 * Gate: __MS_TOKEN__ required; ?demo=1 inert; core sets __MS_API__.
 * Dispatch: pageName() -> PAGES -> initHome/initLlmModel/initImageModel.
 * Transport: POST /api/jobs; GET /jobs/{id}, /logs, /metrics_history,
 *            /samples/<rel>, /artifacts, /star, /eval, /report, /open,
 *            /models, /models/{id}/download*, /data/hint, /pick, /env/doctor.
 * Boundary: v3 logic closure-private; only window.__* hooks + existing DOM;
 *           already-scheduled demo timers cannot be cleared.
 * Task-97: /api/vram poll, env panel, training-glow, truthful artifacts[].
 * Known pitfall: planInts() reads el.value BEFORE el.textContent (600 vs old fallback).
 * Frozen v3 HTML: design/prototype/v3/*.html is read-only from this layer.
 */
(function () {
  "use strict";
  var W = window, D = document;
  if (W.__MS_WIRE_LOADED__) return;
  W.__MS_WIRE_LOADED__ = true;

  var TOKEN = W.__MS_TOKEN__ || "";
  var API = W.__MS_API__ || "/api";
  var SEARCH = String(W.location.search || "");
  var DEMO = /[?&]demo=1(?:&|$)/.test(SEARCH);
  if (DEMO || !TOKEN) return;
  W.__MSWIRE_ACTIVE__ = true;

  var PATH = String(W.location.pathname || "").replace(/\\/g, "/").toLowerCase();
  function pageName() {
    if (PATH === "/" || PATH === "/v3/" || PATH === "/v3/index.html" || PATH === "/index.html") return "home";
    if (PATH.indexOf("/v3/llm.html") >= 0 || PATH === "/llm.html") return "llm-model";
    if (PATH.indexOf("/v3/llm-data.html") >= 0 || PATH === "/llm-data.html") return "llm-data";
    if (PATH.indexOf("/v3/llm-train.html") >= 0 || PATH === "/llm-train.html") return "llm-train";
    if (PATH.indexOf("/v3/image.html") >= 0 || PATH === "/image.html") return "image-model";
    if (PATH.indexOf("/v3/image-data.html") >= 0 || PATH === "/image-data.html") return "image-data";
    if (PATH.indexOf("/v3/image-params.html") >= 0 || PATH === "/image-params.html") return "image-params";
    if (PATH.indexOf("/v3/image-train.html") >= 0 || PATH === "/image-train.html") return "image-train";
    if (PATH.indexOf("/legacy") >= 0) return "legacy";
    return "unknown";
  }
  var PAGE = pageName();

  function $(id) { return D.getElementById(id); }
  function text(el, v) { if (el) el.textContent = String(v == null ? "" : v); }
  function html(el, v) { if (el) el.innerHTML = v; }
  function attr(el, k, v) { if (el) el.setAttribute(k, v); }
  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setDisabled(el, off) {
    if (!el) return;
    if (off) el.setAttribute("aria-disabled", "true"); else el.removeAttribute("aria-disabled");
    if ("disabled" in el) el.disabled = !!off;
  }
  function mmss(s) {
    if (!isFinite(s) || s < 0) return "—";
    s = Math.round(s);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  function stamp() {
    var d = new Date(), p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }
  function basename(p) {
    var a = String(p || "").split(/[\\/]/).filter(Boolean);
    return a.length ? a[a.length - 1] : "";
  }
  function log() {
    if (W.console && W.console.log) {
      var a = ["[ms-wire]"]; for (var i = 0; i < arguments.length; i++) a.push(arguments[i]);
      W.console.log.apply(W.console, a);
    }
  }

  // ---------------- API ----------------
  function api(path, opts) {
    if (String(path).indexOf("/api/") === 0) path = String(path).slice(4);
    opts = opts || {};
    var headers = {}, k;
    for (k in (opts.headers || {})) headers[k] = opts.headers[k];
    headers["X-ModelSmith-Token"] = TOKEN;
    if (opts.body !== undefined && opts.body !== null) headers["Content-Type"] = "application/json";
    var init = { method: opts.method || "GET", headers: headers, cache: "no-store" };
    if (opts.body !== undefined && opts.body !== null) init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
    return W.fetch(API + path, init).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { raw: t }; }
        if (!r.ok) { var err = new Error("HTTP " + r.status); err.status = r.status; err.body = data; throw err; }
        return data;
      });
    }).catch(function (err) {
      if (!err || !err.status || err.status >= 500) markOffline();
      throw err;
    });
  }
  function jobOf(j) { return (j && j.job) ? j.job : (j || {}); }
  function failText(err) {
    if (!err) return "核心未连接";
    if (err.status === 401) return "核心鉴权失败";
    if (err.status === 404) return "接口未就绪";
    return "核心未连接";
  }
  function coreFail(j, fallback) {
    return String((j && (j.error || j.message || j.reason)) || fallback || "核心返回失败");
  }

  // ---------------- job engine ----------------
  var JOB = { id: null, kind: null, timer: null, offset: 0, frozen: false, statusEl: null, logEl: null, ctx: null };
  var MSWIRE_SETINTERVAL = W.setInterval;
  var MSWIRE_SETTIMEOUT = W.setTimeout;
  var FREEZE = { timer: null, fn: null };
  function stopDemoTimers() {
    if (W.__MSWIRE_DEMO_STOPPED__) return;
    W.__MSWIRE_DEMO_STOPPED__ = true;
    W.setTimeout = function () { return 0; };
    W.setInterval = function () { return 0; };
  }
  function armFreeze(fn) {
    if (FREEZE.timer) { W.clearInterval(FREEZE.timer); FREEZE.timer = null; }
    FREEZE.fn = fn || null;
    if (!fn) return;
    fn();
    FREEZE.timer = MSWIRE_SETINTERVAL(function () { try { fn(); } catch (e) {} }, 500);
  }
  function stopPoll() { if (JOB.timer) { W.clearTimeout(JOB.timer); JOB.timer = null; } }
  function appendLog(el, line) {
    if (!el) return;
    var div = D.createElement("div");
    div.textContent = String(line == null ? "" : line);
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }
  function submitJob(kind, params, ctx) {
    if (FREEZE.timer) { W.clearInterval(FREEZE.timer); FREEZE.timer = null; }
    FREEZE.fn = null;
    stopPoll();
    JOB.id = null; JOB.kind = kind; JOB.offset = 0; JOB.frozen = false; JOB.ctx = ctx;
    if (ctx.onStart) ctx.onStart();
    return api("/jobs", { method: "POST", body: { kind: kind, params: params || {} } }).then(function (j) {
      var job = (j && j.job) ? j.job : j;
      var jid = (j && j.job_id) || (job && (job.job_id || job.id));
      if (!jid) throw new Error("核心未返回 job_id");
      job = job || {};
      job.job_id = jid;
      JOB.id = jid;
      setTrainingGlow(true);
      if (ctx.onCreated) ctx.onCreated(job);
      if (ctx.onHistory) {
        api("/jobs/" + encodeURIComponent(job.job_id) + "/metrics_history?since=0").then(function (h) {
          if (JOB.id === job.job_id && !JOB.frozen && ctx.onHistory) ctx.onHistory(h);
        }).catch(function () {});
      }
      // Pages that render a run page pass onJob/onDone; pages that navigate first
      // (decision deck) call pollJob themselves from their run-page hook.
      if (ctx.onJob || ctx.onDone) pollJob(ctx);
      return job;
    }).catch(function (err) {
      setTrainingGlow(false);
      stopVramPoll();
      if (ctx.onError) ctx.onError(failText(err), err);
    });
  }
  function pollJob(ctx) {
    if (!JOB.id || JOB.frozen) return;
    var id = JOB.id;
    W.Promise.all([
      api("/jobs/" + encodeURIComponent(id)),
      api("/jobs/" + encodeURIComponent(id) + "/logs?offset=" + JOB.offset).catch(function () { return null; })
    ]).then(function (rs) {
      if (JOB.id !== id || JOB.frozen) return;
      var job = jobOf(rs[0]), lg = rs[1] || {};
      if (lg.next_offset !== undefined) JOB.offset = lg.next_offset;
      if (ctx.onLogs && lg.lines && lg.lines.length) ctx.onLogs(lg.lines, job);
      if (ctx.onJob) ctx.onJob(job);
      var st = job.state || "";
      if (PAGE === "image-train") applyCaptionWarning(job);
      syncVramState(st);
      if (st === "running" && isTrainPage()) setTrainingGlow(true);
      if (st === "succeeded" || st === "failed" || st === "cancelled" || st === "interrupted") {
        JOB.frozen = true;
        stopPoll();
        stopVramPoll();
        setTrainingGlow(false);
        stopDemoTimers();
        if (ctx.onDone) ctx.onDone(job);
        return;
      }
      JOB.timer = MSWIRE_SETTIMEOUT(function () { pollJob(ctx); }, 300);
    }).catch(function (err) {
      JOB.frozen = true; stopPoll();
      stopVramPoll();
      setTrainingGlow(false);
      if (ctx.onError) ctx.onError(failText(err), err);
    });
  }
  function cancelJob(ctx) {
    ctx = ctx || {};
    function errorDetail(err) {
      if (err && err.body && (err.body.error || err.body.reason)) return String(err.body.error || err.body.reason);
      return failText(err);
    }
    function cancelFailed(msg) {
      if (ctx.logEl) appendLog(ctx.logEl, msg);
      if (ctx.titleEl) text(ctx.titleEl, "训练中");
      if (ctx.cancelEl) setDisabled(ctx.cancelEl, false);
      if (ctx.onError) ctx.onError(msg);
    }
    function sendCancel(id) {
      return api("/jobs/" + encodeURIComponent(id) + "/cancel", { method: "POST" }).then(function (j) {
        if (ctx.onCancelSent) ctx.onCancelSent(j);
        return j;
      }).catch(function (err) {
        cancelFailed("取消失败：" + errorDetail(err));
      });
    }
    if (JOB.id) return sendCancel(JOB.id);
    return api("/jobs?state=running").then(function (j) {
      var list = (j && (j.jobs || j.items)) || (Array.isArray(j) ? j : []);
      var running = [];
      for (var i = 0; i < list.length; i++) {
        var x = list[i] || {};
        if (!x.state || x.state === "running") running.push(x);
      }
      var pick = null;
      for (var r = 0; r < running.length; r++) {
        if (String(running[r].kind || "") === "diffsynth") { pick = running[r]; break; }
      }
      if (!pick && running.length) pick = running[0];
      var id = pick && (pick.job_id || pick.id);
      if (!id) { cancelFailed("当前没有运行中的作业"); return null; }
      JOB.id = id;
      if (pick.kind) JOB.kind = pick.kind;
      return sendCancel(id);
    }).catch(function (err) {
      cancelFailed("取消失败：" + errorDetail(err));
    });
  }

  function bindCancelOnce(el, logEl, titleEl) {
    if (!el || el.__mswireCancelBound) return;
    el.__mswireCancelBound = true;
    // Document capture runs before v3's target listener; v3 sets aria-disabled
    // and the title to "已停训" before a normal target listener would see it.
    D.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("#cancel") : null;
      if (t !== el) return;
      if (el.getAttribute("aria-disabled") === "true" || el.disabled) return;
      e.preventDefault();
      setDisabled(el, true);
      cancelJob({
        logEl: logEl,
        titleEl: titleEl,
        cancelEl: el,
        onCancelSent: function () {
          appendLog(logEl, "已发送取消请求（软停 → 15s 后 taskkill /T /F），已落盘 LoRA 保留");
        }
      });
    }, true);
  }

  function openPath(path, logEl) {
    path = String(path || "").trim();
    if (!path) { appendLog(logEl, "没有可打开的路径"); return; }
    api("/open", { method: "POST", body: { path: path } }).then(function (j) {
      if (j && j.ok === false) { appendLog(logEl, "打开目录失败：" + coreFail(j)); return; }
      appendLog(logEl, "已打开目录：" + path);
    }).catch(function (err) {
      var msg = (err && err.status === 403) ? "核心拒绝（路径不在允许范围）"
        : ((err && err.status === 404) ? "打开目录接口未就绪" : "核心未连接");
      appendLog(logEl, "打开目录失败：" + msg);
    });
  }
  function bindOpenFolder(el, getPath, logEl) {
    if (!el) return;
    el.title = "双击打开所在目录";
    el.addEventListener("dblclick", function () { openPath(getPath(), logEl); });
  }

  // ---------------- common UI ----------------
  function chips() { return [].slice.call(D.querySelectorAll(".top .chip, .acts .chip")); }
  function setEnvChip(state) {
    var list = chips();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].textContent).indexOf("环境") === 0) {
        if (state === "核心未连接" || state === "未就绪") text(list[i], "环境 · " + state);
        else { list[i].title = "核心环境：" + state; list[i].setAttribute("aria-label", "环境：" + state); }
      }
    }
  }
  function envDoctor() {
    api("/env/doctor").then(function (j) {
      var ok = j && (j.ok !== false);
      setEnvChip(ok ? "正常" : "异常");
    }).catch(function (err) {
      setEnvChip(err && err.status === 404 ? "未就绪" : "核心未连接");
    });
  }

  // ---------------- environment panel / vram / glow / captions ----------------
  var ENV = { panel: null, body: null, close: null, last: null, loading: false };
  function envNum(v) { return (v === undefined || v === null || v === "") ? "未连接" : String(v); }
  function envRowsFromDoctor(j) {
    if (!j || j.ok === false) return null;
    var py = j.python || {}, torch = j.torch || {}, cuda = j.cuda || {}, disk = j.disk || {}, ports = j.ports || {};
    var gpu = (cuda.gpus && cuda.gpus[0]) || {};
    var rows = [];
    rows.push("Python: " + envNum(py.version));
    var t = "未连接";
    if (j.torch) {
      if (torch.found) t = "可用 · " + (torch.version || "版本未知");
      else if (torch.error) t = "不可用 · " + String(torch.error);
      else t = "未安装";
    }
    rows.push("torch: " + t);
    var c = "未连接";
    if (j.cuda) {
      c = cuda.status ? String(cuda.status) : "未连接";
      if (cuda.driver_version || gpu.driver_version) c += " · 驱动 " + (cuda.driver_version || gpu.driver_version);
      if (gpu.name) c += " · " + gpu.name;
    }
    rows.push("CUDA/驱动: " + c);
    var vram = "未连接";
    if (gpu.memory_total_mb !== undefined && gpu.memory_used_mb !== undefined) {
      var used = Number(gpu.memory_used_mb) / 1024, total = Number(gpu.memory_total_mb) / 1024;
      if (isFinite(used) && isFinite(total)) vram = used.toFixed(1) + " / " + total.toFixed(1) + " GB";
    }
    rows.push("显存: " + vram);
    var diskv = "未连接";
    if (disk.free_gb !== undefined && disk.total_gb !== undefined) {
      diskv = Number(disk.free_gb).toFixed(1) + " / " + Number(disk.total_gb).toFixed(1) + " GB 可用/总";
    }
    rows.push("磁盘: " + diskv);
    var pp = ports[String(8765)] || ports[String((j.server && j.server.port) || 8765)];
    var pv = "未连接";
    if (pp) pv = pp.listening ? ("监听中" + (pp.is_core ? "（core）" : "")) : "未监听";
    rows.push("8765 端口: " + pv);
    return rows;
  }
  function envUnavailableRows() {
    return ["Python: 未连接", "torch: 未连接", "CUDA/驱动: 未连接", "显存: 未连接", "磁盘: 未连接", "8765 端口: 未连接"];
  }
  function renderEnvPanel() {
    if (!ENV.body) return;
    var rows = envRowsFromDoctor(ENV.last) || envUnavailableRows();
    ENV.body.textContent = rows.join("\n");
  }
  function loadEnvPanel() {
    ENV.loading = true;
    api("/env/doctor").then(function (j) {
      ENV.loading = false;
      ENV.last = j;
      renderEnvPanel();
    }).catch(function () {
      ENV.loading = false;
      ENV.last = null;
      renderEnvPanel();
    });
  }
  function openEnvPanel() {
    if (!ENV.panel) return;
    ENV.panel.hidden = false;
    renderEnvPanel();
    loadEnvPanel();
  }
  function bindEnvPanel() {
    ENV.panel = $("envPanel");
    ENV.body = $("envBody");
    ENV.close = $("envClose");
    var chips = D.querySelectorAll('[data-env="1"]');
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].__mswireEnvBound) continue;
      chips[i].__mswireEnvBound = true;
      chips[i].addEventListener("click", openEnvPanel);
    }
    if (ENV.close && !ENV.close.__mswireEnvBound) {
      ENV.close.__mswireEnvBound = true;
      ENV.close.addEventListener("click", function () { if (ENV.panel) ENV.panel.hidden = true; });
    }
  }

  var VRAM = { active: false, timer: null, busy: false };
  function isTrainPage() { return PAGE === "image-train" || PAGE === "llm-train"; }
  function meterPctText(pct) {
    if (!isFinite(pct)) return "0";
    var n = Math.round(pct * 10) / 10;
    return String(n);
  }
  function meterParts(j, prefix) {
    if (!j || j.ok === false) return null;
    var used = Number(j[prefix + "used_mib"]) / 1024;
    var total = Number(j[prefix + "total_mib"]) / 1024;
    var pct = Number(j[prefix + "pct"]);
    if (!isFinite(used) || !isFinite(total) || total <= 0) return null;
    if (!isFinite(pct)) pct = used / total * 100;
    return { text: used.toFixed(1) + " / " + total.toFixed(1) + " GB · " + meterPctText(pct) + "%", width: meterPctText(pct) + "%", pct: pct };
  }
  function fmtVram(j) {
    var p = meterParts(j, "");
    return p ? p.text : "—";
  }
  function fmtRam(j) {
    var p = meterParts(j, "ram_");
    return p ? p.text : "—";
  }
  function meterHotTarget(bar) {
    if (!bar) return null;
    if (bar.parentNode && bar.parentNode.classList && bar.parentNode.classList.contains("meter")) return bar.parentNode;
    return bar;
  }
  function setMeter(textEl, bar, parts) {
    text(textEl, parts ? parts.text : "—");
    if (bar && bar.style) bar.style.width = parts ? parts.width : "0%";
    var hot = meterHotTarget(bar);
    if (hot && hot.classList) {
      if (parts && parts.pct > 90) hot.classList.add("is-hot");
      else hot.classList.remove("is-hot");
    }
  }
  function stopVramPoll() {
    VRAM.active = false;
    if (VRAM.timer) { W.clearTimeout(VRAM.timer); VRAM.timer = null; }
  }
  function nextVram() {
    if (!VRAM.active) return;
    VRAM.timer = MSWIRE_SETTIMEOUT(pollVram, 2000);
  }
  function pollVram() {
    if (!VRAM.active) return;
    var vramText = $("vramUsed"), vramBar = $("vramBar");
    var ramText = $("ramUsed"), ramBar = $("ramBar");
    if (!vramText && !vramBar && !ramText && !ramBar) { stopVramPoll(); return; }
    api("/vram").then(function (j) {
      if (!VRAM.active) return;
      var v = meterParts(j, ""), r = meterParts(j, "ram_");
      if (!j || j.ok === false) { v = null; r = null; }
      setMeter(vramText, vramBar, v);
      setMeter(ramText, ramBar, r);
      nextVram();
    }, function () {
      setMeter(vramText, vramBar, null);
      setMeter(ramText, ramBar, null);
      if (!VRAM.active) return;
      nextVram();
    });
  }
  function startVramPoll() {
    if (VRAM.active || !isTrainPage() || !$("vramUsed")) return;
    VRAM.active = true;
    pollVram();
  }
  function syncVramState(state) {
    if (state === "running") startVramPoll();
    else if (state === "succeeded" || state === "failed" || state === "cancelled" || state === "interrupted") stopVramPoll();
  }
  function setTrainingGlow(on) {
    if (!D.body) return;
    if (on) D.body.classList.add("training-glow");
    else D.body.classList.remove("training-glow");
  }

  var CAPTION = { shown: false, el: null };
  function captionDoneFor(job) {
    var done = ssGet("ms.caption.done.dir");
    if (!done) return false;
    var p = (job && job.params) || {};
    var dir = String(p.dataset || p.data || W.__imageData || "");
    return !!dir && dir.toLowerCase() === done.toLowerCase();
  }
  function applyCaptionWarning(job) {
    var m = (job && job.metrics) || {}, meta = m.dataset_metadata || {}, n = 0;
    if (captionDoneFor(job)) {
      clearCaptionWarning();
      return;
    }
    var explicitFalse = (m.no_captions === false || meta.no_captions === false);
    if (m.no_captions === true || meta.no_captions === true) {
      n = Number(m.no_captions_images !== undefined ? m.no_captions_images : (meta.no_captions_images !== undefined ? meta.no_captions_images : 1));
      if (!isFinite(n) || n <= 0) n = 1;
    }
    if (!n && Array.isArray(m.warnings)) {
      for (var i = 0; i < m.warnings.length; i++) {
        var x = m.warnings[i];
        if (typeof x === "string" && x.indexOf("no_captions") === 0) {
          n = Number(m.no_captions_images || meta.no_captions_images || 1);
          if (!isFinite(n) || n <= 0) n = 1;
          break;
        }
      }
    }
    if (!n) {
      if (explicitFalse || (job && job.state === "succeeded")) clearCaptionWarning();
      return;
    }
    var el = $("captionWarn") || $("msCaptionWarn");
    if (!el) {
      el = D.createElement("div");
      el.id = "msCaptionWarn";
      el.className = "note note--warn";
      el.style.cssText = "margin:8px 0;padding:10px 14px;border:1px solid rgba(201,123,18,.45);border-radius:12px;background:rgba(240,179,90,.18);color:#6B4A08;font-size:14px;line-height:1.6;font-weight:600;";
      var anchor = $("rounds") || $("log");
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(el, anchor);
      else if (D.body) D.body.appendChild(el);
    }
    el.hidden = false;
    el.textContent = "本次 " + n + " 张图没有字幕，全部用默认模板 → 训出来的 LoRA 基本无意义";
    CAPTION.el = el;
    CAPTION.shown = true;
  }

  function clearCaptionWarning() {
    var el = $("captionWarn") || $("msCaptionWarn");
    if (el) {
      el.hidden = true;
      el.textContent = "";
    }
    CAPTION.el = null;
    CAPTION.shown = false;
  }

  function optionalNumber(qs, name) {
    var raw = qs && qs.get ? qs.get(name) : null;
    if (raw === null || raw === undefined || String(raw).trim() === "") raw = readValue(name, null);
    if (raw === null || raw === undefined || String(raw).trim() === "") return null;
    var n = parseFloat(String(raw).replace(/[^0-9eE.+-]/g, ""));
    return isFinite(n) ? n : null;
  }
  function appendNeedSteps(logEl, model, data, titleEl) {
    text(titleEl, "请回到参数页设置步数");
    if (!logEl) return;
    var d = D.createElement("div");
    d.appendChild(D.createTextNode("训练页没有步数来源，未提交任务。 "));
    var a = D.createElement("a");
    a.href = "image-params.html?model=" + encodeURIComponent(model || "") + "&data=" + encodeURIComponent(data || "");
    a.textContent = "回到参数页设置步数";
    d.appendChild(a);
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  }
  function captionExtValue(qs) {
    var raw = qs && qs.get ? qs.get("caption_ext") : "";
    if (!raw) {
      var el = D.querySelector('[data-pname="caption_ext"]') || D.getElementById("caption_ext") || D.getElementById("captionExt");
      if (el) raw = el.value !== undefined ? el.value : el.textContent;
    }
    return String(raw || "").trim();
  }

  // ---------------- offline detection / heartbeat ----------------
  var OFFLINE = { active: false, fails: 0, timer: null, node: null, disabled: [] };
  function startButtons() {
    return [$("dec2Next"), $("dec5Run")].filter(Boolean);
  }
  function markOffline() {
    if (OFFLINE.active) return;
    OFFLINE.active = true;
    stopPoll();
    stopVramPoll();
    setTrainingGlow(false);
    var list = chips();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].textContent).indexOf("环境") === 0) {
        text(list[i], "环境 · 核心未连接");
        list[i].title = "核心环境：核心未连接";
        list[i].setAttribute("aria-label", "环境：核心未连接");
      }
    }
    var logEl = $("dec3Log") || $("log");
    if (logEl) {
      var d = D.createElement("div");
      d.textContent = "核心未连接";
      logEl.appendChild(d);
      logEl.scrollTop = logEl.scrollHeight;
      OFFLINE.node = d;
    }
    OFFLINE.disabled = [];
    var btns = startButtons();
    for (var b = 0; b < btns.length; b++) {
      if (btns[b].getAttribute("aria-disabled") !== "true") {
        OFFLINE.disabled.push(btns[b]);
        btns[b].setAttribute("aria-disabled", "true");
      }
    }
  }
  function markOnline() {
    if (!OFFLINE.active) return;
    OFFLINE.active = false;
    OFFLINE.fails = 0;
    var list = chips();
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].textContent).indexOf("环境") === 0) {
        text(list[i], "环境");
        list[i].title = "核心环境：正常";
        list[i].setAttribute("aria-label", "环境：正常");
      }
    }
    if (OFFLINE.node && OFFLINE.node.parentNode) OFFLINE.node.parentNode.removeChild(OFFLINE.node);
    OFFLINE.node = null;
    for (var b = 0; b < OFFLINE.disabled.length; b++) {
      OFFLINE.disabled[b].removeAttribute("aria-disabled");
    }
    OFFLINE.disabled = [];
  }
  function healthTick() {
    W.fetch("/health", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("health " + r.status);
      return r.json().catch(function () { return {}; });
    }).then(function (j) {
      if (j && j.ok === false) throw new Error("health not ok");
      OFFLINE.fails = 0;
      if (OFFLINE.active) markOnline();
    }).catch(function () {
      OFFLINE.fails++;
      if (OFFLINE.fails >= 2) markOffline();
    });
  }
  function startHeartbeat() {
    if (OFFLINE.timer) W.clearInterval(OFFLINE.timer);
    W.setTimeout(healthTick, 800);
    OFFLINE.timer = W.setInterval(healthTick, 5000);
  }

  // ---------------- LLM / image model lists ----------------
  function archForClass(cls) {
    cls = String(cls || "");
    if (/^QwenImage/i.test(cls)) return "qwen_image_21";
    if (/^MOSSImage/i.test(cls)) return "moss_image";
    if (/^ZImage/i.test(cls)) return "z_image";
    if (W.console && W.console.log) W.console.log("[ms-wire] unknown model class_name=" + cls + "；arch 默认 qwen_image_21");
    return "qwen_image_21";
  }
  function fillModelSelect(sel, kind, emptyText, refresh) {
    if (!sel) return;
    if (!sel.__mswireRefresh) {
      sel.__mswireRefresh = true;
      sel.title = "双击刷新模型列表";
      sel.addEventListener("dblclick", function () { fillModelSelect(sel, kind, emptyText, true); });
    }
    var murl = kind === "diffusion" ? ("/models" + (refresh ? "?refresh=1" : "")) : ("/models?kind=" + encodeURIComponent(kind) + (refresh ? "&refresh=1" : ""));
    api(murl).then(function (j) {
      var all = (j && j.models) || [];
      var unknowns = all.filter(function (m) { return m.kind === "unknown"; });
      var models = all;
      if (kind === "diffusion") {
        models = all.filter(function (m) {
          if (m.kind === "diffusion") return true;
          if (m.kind !== "diffusion_single") return false;
          return /krea-2-raw/i.test(String(m.id || "") + " " + String(m.path || ""));
        });
      }
      sel.__msLastScan = { scanned_at: (j && j.scanned_at) || "", cache_hit: !!(j && j.cache_hit) };
      var tpl = sel.options && sel.options.length ? sel.options[0] : null;
      if (!tpl) return;
      while (sel.options.length > 1) sel.removeChild(sel.options[sel.options.length - 1]);
      if (!models.length) { tpl.textContent = emptyText || "models\\ 里没发现模型"; return; }
      for (var i = 0; i < models.length; i++) {
        var o = tpl.cloneNode(true);
        o.value = models[i].path || models[i].id || "";
        var inc = models[i].complete === false;
        o.textContent = (models[i].name || models[i].id) + "  ·  " + (models[i].size_gb || Math.round((models[i].size_bytes || 0) / 1073741824 * 100) / 100) + " GB" + (inc ? "  · 不完整（缺 " + ((models[i].missing || []).join(" / ") || "文件") + "）" : "");
        if (kind === "diffusion") o.setAttribute("data-class-name", models[i].class_name || "");
        if (inc) { o.disabled = true; o.setAttribute("data-complete", "false"); }
        else o.setAttribute("data-complete", "true");
        sel.appendChild(o);
      }
      if (sel.options[sel.selectedIndex] && sel.options[sel.selectedIndex].disabled) {
        sel.value = "";
        var pi = sel.__mswirePathInput;
        if (pi) { pi.value = ""; pi.dispatchEvent(new W.Event("input", { bubbles: true })); pi.placeholder = "所选模型不完整，请换一个"; }
        if (W.console && W.console.warn) W.console.warn("[ms-wire] previously selected model is incomplete; cleared");
      }
      if (sel.options.length > 0) sel.options[0].textContent = "从本机模型里选…";
      if (kind === "diffusion") {
        if (!sel.__msAutoTimer) {
          sel.__msAutoTimer = MSWIRE_SETINTERVAL(function () {
            if (D.visibilityState === "visible") fillModelSelect(sel, kind, emptyText, true);
          }, 30000);
        }
      }
      if (sel.__msOnModels) sel.__msOnModels();
    }).catch(function (err) {
      var o = sel.options && sel.options[0];
      if (o) o.textContent = err && err.status === 404 ? "核心未提供模型扫描" : "核心未连接";
    });
  }

  function ssGet(k) { try { return W.sessionStorage.getItem(k) || ""; } catch (e) { return ""; } }
  function ssSet(k, v) { try { W.sessionStorage.setItem(k, String(v || "")); } catch (e) {} }
  function readValue(name, fallback) {
    var el = D.querySelector('[data-pname="' + name + '"]') || D.getElementById(name);
    if (el) {
      var raw = el.value !== undefined && el.value !== null ? el.value : el.textContent;
      var n = parseFloat(String(raw).replace(/[^0-9eE.+-]/g, ""));
      if (isFinite(n)) return n;
    }
    return fallback;
  }
  function readTotal(sel, fallback) {
    var el = $(String(sel).replace(/^#/, ""));
    if (!el) return fallback;
    var nums = String(el.textContent).match(/\d+/g);
    if (nums && nums.length) return Number(nums[nums.length - 1]);
    return fallback;
  }

  function initLlmModel() {
    var sel = $("modelSel"), path = $("modelPath");
    if (sel) sel.__mswirePathInput = path;
    fillModelSelect(sel, "llm", "models\\ 里没发现 LLM 模型");
    if (path) {
      path.addEventListener("input", function () { ssSet("ms.llm.model", path.value); });
      if (ssGet("ms.llm.model") && !path.value) {
        path.value = ssGet("ms.llm.model");
        path.dispatchEvent(new W.Event("input", { bubbles: true }));
      }
    }
    if (sel) sel.addEventListener("change", function () {
      if (!sel.value) return;
      if (path) { path.value = sel.value; path.dispatchEvent(new W.Event("input", { bubbles: true })); }
      W.__llmModelPath = sel.value;
      ssSet("ms.llm.model", sel.value);
    });
    var nx = $("next");
    if (nx) nx.addEventListener("click", function (e) {
      var v = path ? String(path.value || "").trim() : "";
      if (!v) return;
      ssSet("ms.llm.model", v);
      e.preventDefault(); e.stopImmediatePropagation();
      W.location.href = "llm-data.html?model=" + encodeURIComponent(v);
    }, true);
  }
  function initLlmData() {
    var qs = new W.URLSearchParams(SEARCH);
    var model = qs.get("model") || ssGet("ms.llm.model");
    if (model) { W.__llmModelPath = model; ssSet("ms.llm.model", model); }
    var input = $("dataPath");
    if (input) {
      input.addEventListener("input", function () { ssSet("ms.llm.data", input.value); });
      if (ssGet("ms.llm.data") && !input.value) input.value = ssGet("ms.llm.data");
    }
    var nx = $("next");
    if (nx) nx.addEventListener("click", function (e) {
      var d = input ? String(input.value || "").trim() : "";
      if (!d) return;
      ssSet("ms.llm.data", d);
      e.preventDefault(); e.stopImmediatePropagation();
      W.location.href = "llm-train.html?model=" + encodeURIComponent(W.__llmModelPath || ssGet("ms.llm.model")) + "&data=" + encodeURIComponent(d);
    }, true);
    initDataHint(input, D.querySelector(".body .sub"));
  }
  function initImageModel() {
    var sel = $("modelSel"), path = $("modelPath");
    if (sel) sel.__mswirePathInput = path;
    if (sel) sel.__msOnModels = function () {
      var saved = ssGet("ms.image.model");
      if (!saved) return;
      var opts = sel.options || [];
      for (var i = 0; i < opts.length; i++) {
        if (opts[i].value === saved) {
          sel.value = saved;
          if (path) { path.value = saved; path.dispatchEvent(new W.Event("input", { bubbles: true })); }
          initImageArchDisplay(saved);
          return;
        }
      }
    };
    fillModelSelect(sel, "diffusion", "models\\ 里没发现扩散模型");
    if (sel) sel.addEventListener("change", function () {
      if (!sel.value) return;
      if (path) { path.value = sel.value; path.dispatchEvent(new W.Event("input", { bubbles: true })); }
      W.__imageModel = sel.value;
      ssSet("ms.image.model", sel.value);
      initImageArchDisplay(sel.value);
      try { D.body.dispatchEvent(new W.Event("change", { bubbles: true })); } catch (e) {}
    });
    if (path) path.addEventListener("input", function () {
      ssSet("ms.image.model", path.value.trim());
      initImageArchDisplay(path.value.trim());
    });
  }

  function initDataHint(input, sub) {
    if (!input) return;
    var t = null;
    input.addEventListener("input", function () {
      if (t) W.clearTimeout(t);
      t = W.setTimeout(function () {
        var p = String(input.value || "").trim();
        if (!p) return;
        api("/data/hint?path=" + encodeURIComponent(p)).then(function (j) {
          if (!sub) return;
          var lang = j && (j.lang_hint || j.lang);
          if (lang === "zh") sub.textContent = "检测到中文语料：建议换多语言底座";
          else if (j && j.format && j.format !== "laya") sub.textContent = "这不是 Laya 复合格式（format=" + j.format + "）";
          else if (j && (j.lang_hint || j.hint)) sub.textContent = "语料语言：" + (j.lang_hint || j.hint);
        }).catch(function (err) {
          if (sub) sub.title = (err && err.status === 404) ? "数据体检接口未就绪" : "";
        });
      }, 400);
    });
  }

  function pollDownloadStatus(id, opt, panel, bar, msg, tries) {
    if (tries > 240) { text(msg, "下载状态轮询超时"); return; }
    api("/models/" + encodeURIComponent(id) + "/download/status").then(function (j) {
      var ev = (j && j.event) || j || {};
      if (ev.ok === false) {
        opt.classList.remove("is-downloading");
        text(msg, "下载失败：" + coreFail(ev));
        return;
      }
      var type = ev.type || ev.state || "";
      if (type === "attempt") { text(msg, "正在尝试源 " + (ev.source || "?") + "（" + (ev.i || "?") + "/" + (ev.n || "?") + "）"); }
      else if (type === "switch") { text(msg, "源失败，切换：" + (ev.from || "?") + " → " + (ev.to || "?")); }
      else if (type === "progress" || ev.pct !== undefined) {
        var pct = Number(ev.pct || 0);
        if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
        text(msg, "下载中 " + pct.toFixed(1) + "%" +
          (ev.speed_bps ? " · " + (Number(ev.speed_bps) / 1048576).toFixed(2) + " MB/s" : "") +
          (ev.eta_s ? " · 剩余 " + mmss(ev.eta_s) : ""));
      } else if (type === "done" || ev.state === "succeeded") {
        opt.setAttribute("data-state", "ready");
        opt.classList.remove("is-missing", "is-downloading");
        var end = opt.querySelector(".opt__end");
        if (end) end.innerHTML = '<span class="opt__size">已装好</span><span class="tag tag--ok">本机</span>';
        text(msg, "下载完成：" + (ev.path || ""));
        return;
      } else if (type === "failed" || ev.state === "failed") {
        opt.classList.remove("is-downloading");
        text(msg, "下载失败：" + ((ev.errors || []).join("；") || "未知"));
        return;
      }
      W.setTimeout(function () { pollDownloadStatus(id, opt, panel, bar, msg, tries + 1); }, 500);
    }).catch(function (err) {
      opt.classList.remove("is-downloading");
      text(msg, (err && err.status === 404) ? "下载状态接口未就绪" : "核心未连接");
    });
  }
  function initDownloadPanel() {
    D.addEventListener("click", function (e) {
      var start = e.target.closest('.dlpanel [data-act="start"]');
      if (!start) return;
      e.preventDefault(); e.stopImmediatePropagation();
      var opt = start.closest(".opt"), panel = start.closest(".dlpanel");
      if (!opt || !panel) return;
      var id = opt.getAttribute("data-id") || "";
      var srcs = (opt.getAttribute("data-srcs") || "").split("|").filter(Boolean);
      panel.innerHTML = '<div class="dlbar"><i></i></div><div class="dlmsg">正在请求核心下载…</div>';
      var bar = panel.querySelector(".dlbar>i"), msg = panel.querySelector(".dlmsg");
      opt.classList.add("is-downloading");
      api("/models/" + encodeURIComponent(id) + "/download", { method: "POST", body: { sources: srcs, resume: true } }).then(function (j) {
        if (j && j.ok === false) {
          opt.classList.remove("is-downloading");
          text(msg, "下载失败：" + coreFail(j));
          return;
        }
        var ev = (j && j.event) || j;
        if (ev && ev.type === "attempt") text(msg, "开始下载：" + (ev.source || ""));
        pollDownloadStatus(id, opt, panel, bar, msg, 0);
      }).catch(function (err) {
        opt.classList.remove("is-downloading");
        text(msg, (err && err.status === 404) ? "下载接口未就绪（核心尚未实装）" : "核心未连接");
      });
    }, true);
  }

  // ---------------- LLM train ----------------
  function initLlmTrain() {
    var qs = new W.URLSearchParams(SEARCH);    var model = qs.get("model") || W.__llmModelPath || ssGet("ms.llm.model") || "";
    var data = qs.get("data") || W.__llmDataPath || ssGet("ms.llm.data") || "";
    stopDemoTimers();
    if (model) { W.__llmModelPath = model; ssSet("ms.llm.model", model); }
    if (data) { W.__llmDataPath = data; ssSet("ms.llm.data", data); }
    var title = $("title"), logEl = $("log"), doneEl = $("done");
    var stepEl = $("step"), lossEl = $("loss"), barEl = $("bar"), timeEl = $("time");
    var adapterEl = $("adapterPath"), outEl = $("outPath");
    var t0 = Date.now();
    function status(s) { text(title, s); text(logEl && logEl.lastChild, ""); }
    if (!model || !data) {
      status("当前没有训练任务");
      appendLog(logEl, "当前没有训练任务（live 模式不播放演示）。");
      return;
    }
    var lfParams = {
      model: model, data: data, out: "E:\\视频\\modelsmith\\_runs\\ui-lf-" + stamp(),
      template: "qwen",
      lr: readValue("lr", 0.0002),
      epochs: Math.max(1, Math.round(readValue("epochs", 1))),
      max_steps: Math.max(0, Math.round(readValue("max_steps", 0))),
      batch: Math.max(1, Math.round(readValue("batch", readValue("batch_size", 1)))),
      grad_accum: Math.max(1, Math.round(readValue("grad_accum", 1))),
      cutoff_len: Math.max(16, Math.round(readValue("cutoff_len", 512))), logging_steps: 1
    };
    var lfRank = optionalNumber(qs, "rank");
    if (lfRank !== null && lfRank > 0) lfParams.rank = Math.round(lfRank);
    var lfAlpha = optionalNumber(qs, "alpha");
    if (lfAlpha !== null && lfAlpha > 0) lfParams.alpha = Math.round(lfAlpha);
    submitJob("lf", lfParams, {
      onStart: function () { text(title, "正在提交训练…"); },
      onCreated: function (job) {
        text(title, "正在训练");
        appendLog(logEl, "job " + job.job_id + " · kind=lf · " + (job.state || ""));
      },
      onLogs: function (lines) { for (var i = 0; i < lines.length; i++) appendLog(logEl, lines[i]); },
      onHistory: function (h) {
        var rows = (h && h.rows) || [];
        if (!rows.length) return;
        var last = rows[rows.length - 1], d = last.data || last;
        var total = Number(d.total || 0);
        var step = Number(d.step !== undefined ? d.step : (d.steps_done || 0));
        text(stepEl, step + " / " + (total || "—"));
        if (d.loss !== undefined) text(lossEl, Number(d.loss).toFixed(4));
        if (barEl) barEl.style.width = Math.min(100, total ? step / total * 100 : 0) + "%";
        appendLog(logEl, "已回放 " + rows.length + " 条历史指标（metrics_history since=0）");
      },
      onJob: function (job) {
        if (JOB.frozen) return;
        var m = job.metrics || {};
        var total = Number(m.total || (job.params && job.params.max_steps) || 0);
        var step = Number(m.step !== undefined ? m.step : (m.steps_done || 0));
        text(stepEl, step + " / " + (total || "—"));
        if (m.loss !== undefined && m.loss !== null) text(lossEl, Number(m.loss).toFixed(4));
        if (barEl) barEl.style.width = Math.min(100, total ? step / total * 100 : 0) + "%";
        text(timeEl, mmss((Date.now() - t0) / 1000));
      },
      onDone: function (job) {
        var m = job.metrics || {}, out = (job.params && job.params.out) || "";
        if (job.state === "succeeded") {
          var ad = m.artifact ? String(m.artifact) : "";
          armFreeze(function () {
            text(title, "训练完成");
            if (adapterEl) text(adapterEl, ad || "—");
            if (outEl) text(outEl, out);
            show(doneEl);
            setDisabled($("cancel"), true);
            setDisabled($("next"), false);
          });
          appendLog(logEl, "训练完成：产物以 artifacts 接口为准");
        } else if (job.state === "cancelled") {
          text(title, "已取消");
          appendLog(logEl, "已取消：LlamaFactory 没有 resume，下次从头训练。");
          setDisabled($("cancel"), true);
          setDisabled($("next"), false);
        } else {
          text(title, "训练失败");
          appendLog(logEl, "训练失败：" + (job.error || job.state));
          setDisabled($("cancel"), true);
        }
      },
      onError: function (msg) { text(title, msg); appendLog(logEl, msg); }
    });
    bindCancelOnce($("cancel"), logEl, title);
    bindOpenFolder($("outPath"), function () { return $("outPath") ? $("outPath").textContent : ""; }, logEl);
    bindOpenFolder($("adapterPath"), function () { return $("adapterPath") ? $("adapterPath").textContent : ""; }, logEl);
  }

  // ---------------- image train ----------------
  var IMG_TEMPLATE = null;
  var IMG_GUARD = { active: false, obs: null, realShots: 0, busy: false, warned: false };
  function imageSaveMode() {
    var el = $("saveMode");
    return el ? String(el.value || "follow") : (ssGet("ms.image.saveMode") || "follow");
  }
  function readSaveSteps(every) {
    var n = Number(every);
    if (!isFinite(n) || n < 0) n = 0;
    return imageSaveMode() === "epoch" ? 0 : n;
  }
  function applyImageMeta(job, params) {
    var m = (job && job.metrics) || {}, jp = (job && job.params) || {};
    var total = (m.total !== undefined && m.total !== null) ? m.total :
      (jp.steps !== undefined ? jp.steps : (params && params.steps !== undefined ? params.steps : null));
    var every = (m.sample_every !== undefined && m.sample_every !== null) ? m.sample_every :
      (jp.sample_every !== undefined ? jp.sample_every : (params && params.sample_every !== undefined ? params.sample_every : null));
    var saveEvery = (m.save_steps !== undefined && m.save_steps !== null) ? m.save_steps :
      (jp.save_steps !== undefined ? jp.save_steps : (params && params.save_steps !== undefined ? params.save_steps : null));
    var explicitMode = m.save_mode || jp.save_mode || (params && params.save_mode) || null;
    var modeForText = explicitMode;
    applyCaptionWarning(job);
    var nText = (total !== null && total !== undefined) ? total : "未填";
    var mText = (every !== null && every !== undefined) ? every : "未填";
    var saveN = Number(saveEvery);
    if (saveEvery === null || saveEvery === undefined || !isFinite(saveN)) saveN = readSaveSteps(every);
    if (!modeForText) modeForText = saveN > 0 ? "follow" : "epoch";
    var line = "共 " + nText + " 步 · 每 " + mText + " 步出图";
    if (modeForText === "follow") line += "并存一份 LoRA（只留最近 5 份）· 不产终版 epoch 档";
    else line += " · 仅结束时存一份终版 LoRA";
    var spans = D.querySelectorAll(".meta span"), target = null;
    for (var i = 0; i < spans.length; i++) {
      var t = String(spans[i].textContent);
      if (t.indexOf("共") >= 0) { target = spans[i]; break; }
      if (!target && t.indexOf("步") >= 0) target = spans[i];
    }
    if (target) text(target, line);
  }
  function sampleTotal(ev) {
    var n = 0;
    for (var i = 0; i < (ev || []).length; i++) n += ((ev[i].files || ev[i].images || []).length || 0);
    return n;
  }
  function imgRealShots(job) {
    return ((job.metrics && (job.metrics.sample_events || job.metrics.samples)) || []).length;
  }
  function imgEnforce(job) {
    if (!IMG_GUARD.active) return;
    var rounds = $("rounds");
    if (!rounds) return;
    var demoRows = rounds.querySelectorAll(".round:not([data-ms-real='1'])");
    if (demoRows.length && W.console && W.console.warn && !IMG_GUARD.warned) {
      IMG_GUARD.warned = true;
      W.console.warn("[ms-wire] removed " + demoRows.length + " demo .round rows (real sample_events)");
    }
    for (var i = 0; i < demoRows.length; i++) {
      if (demoRows[i].parentNode) demoRows[i].parentNode.removeChild(demoRows[i]);
    }
    if (job) IMG_GUARD.realShots = imgRealShots(job);
    if ($("shots")) text($("shots"), IMG_GUARD.realShots);
  }
  function imgActivateGuard(job) {
    PAGES_IMG_JOB = job;
    var rounds0 = $("rounds");
    if (rounds0 && !IMG_TEMPLATE) {
      var star0 = rounds0.querySelector(".round [data-act]");
      IMG_TEMPLATE = star0 ? star0.closest(".round") : rounds0.querySelector(".round");
    }
    if (IMG_GUARD.active) return;
    IMG_GUARD.active = true;
    var rounds = $("rounds");
    if (!rounds || !W.MutationObserver) return;
    IMG_GUARD.obs = new W.MutationObserver(function () {
      if (IMG_GUARD.busy) return;
      IMG_GUARD.busy = true;
      try { imgEnforce(PAGES_IMG_JOB); } catch (e) {}
      IMG_GUARD.busy = false;
    });
    IMG_GUARD.obs.observe(rounds, { childList: true });
  }
  var PAGES_IMG_JOB = null;
  function renderImageRounds(job, tries) {
    var rounds = $("rounds"); if (!rounds) return;
    var m = job.metrics || {};
    var ev = m.sample_events || m.samples || [];
    if (!ev || !ev.length) {
      imgActivateGuard(job);
      imgEnforce(job);
      return;
    }
    var starRow = rounds.querySelector(".round [data-act='star']");
    var tpl = (starRow ? starRow.closest(".round") : null) || IMG_TEMPLATE || rounds.querySelector(".round");
    if (!tpl) {
      if ((tries || 0) < 20) MSWIRE_SETTIMEOUT(function () { renderImageRounds(job, (tries || 0) + 1); }, 200);
      return;
    }
    IMG_TEMPLATE = tpl;
    imgActivateGuard(job);
    while (rounds.firstChild) rounds.removeChild(rounds.firstChild);
    var keep = ev.slice(-5);
    for (var i = 0; i < keep.length; i++) {
      var row = tpl.cloneNode(true);
      row.setAttribute("data-ms-real", "1");
      var step = keep[i].step || keep[i].at || "?";
      var stepDir = "step-" + String(step).replace(/\D/g, "");
      var t = row.querySelector(".round__t");
      text(t, "第 " + (i + 1) + " 组 · step " + step);
      var lora = row.querySelector(".round__lora");
      var loraFile = keep[i].lora_path ? String(keep[i].lora_path) : "";
      text(lora, "LoRA: " + (loraFile ? basename(loraFile) : "—"));
      var shots = row.querySelectorAll(".shot");
      var files = keep[i].files || keep[i].images || [];
      for (var s = 0; s < shots.length; s++) {
        var cap = shots[s].querySelector(".shot__cap");
        var f = files[s];
        var rel = f === undefined ? "" : (typeof f === "string" ? f : (f.file || basename(f.path || "")));
        rel = String(rel || "").replace(/\\/g, "/");
        if (rel && JOB.id) {
          var urlRel = stepDir + "/" + rel.split("/").pop();
          shots[s].querySelector(".shot__img").style.backgroundImage =
            "url('" + API + "/jobs/" + encodeURIComponent(JOB.id) + "/samples/" +
            urlRel.split("/").map(encodeURIComponent).join("/") + "')";
          text(cap, "#" + (s + 1) + " " + basename(rel));
        } else {
          text(cap, "#" + (s + 1) + " 采样已生成（待核心静态托管）");
        }
      }
      var acts = row.querySelectorAll("[data-act]");
      for (var a = 0; a < acts.length; a++) {
        if (acts[a].getAttribute("data-act") === "pick") acts[a].style.display = "none";
      }
      rounds.appendChild(row);
    }
    IMG_TEMPLATE = tpl;
  }
  function planInts() {
    var out = [], nodes = D.querySelectorAll(".grid3 .v");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var raw = (el.value !== undefined && el.value !== null && String(el.value) !== "")
        ? el.value : el.textContent;
      var n = parseInt(String(raw).replace(/[^0-9]/g, ""), 10);
      if (isFinite(n)) out.push(n);
    }
    return out;
  }
  function initImageSaveControls() {
    if ($("saveMode")) return;
    var host = D.querySelector(".body .grid3") || D.querySelector(".body");
    if (!host) return;
    var row = D.createElement("div");
    row.className = "row";
    row.id = "saveModeRow";
    row.innerHTML = '<label>存档策略 ' +
      '<select id="saveMode">' +
      '<option value="follow">跟随采样：每 N 步存一份，不产终版 epoch 档</option>' +
      '<option value="epoch">仅结束时存一份（无常驻中间档）</option>' +
      '</select></label>';
    host.parentNode.insertBefore(row, host.nextSibling);
    var sm = $("saveMode");
    if (sm) {
      var savedMode = ssGet("ms.image.saveMode");
      if (savedMode === "epoch" || savedMode === "follow") sm.value = savedMode;
      ssSet("ms.image.saveMode", sm.value);
      sm.addEventListener("change", function () { ssSet("ms.image.saveMode", sm.value); });
    }
  }
  function ensureImageArchForm() {
    if ($("archBox")) return;
    var host = D.querySelector(".body .grid3") || D.querySelector(".body");
    if (!host) return;
    var box = D.createElement("div");
    box.className = "archbox"; box.id = "archBox";
    box.innerHTML = '<div id="archError"></div>';
    host.parentNode.insertBefore(box, host.nextSibling);
    if (!$("ms-arch-style")) {
      var st = D.createElement("style"); st.id = "ms-arch-style";
      st.textContent = ".archbox{margin-top:8px;padding:10px 14px;border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.72);display:flex;flex-direction:column;gap:8px}.archbox>.field{display:grid;grid-template-columns:180px 1fr;gap:10px;align-items:center;font-size:12.5px;color:var(--dim)}.archbox input{height:34px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.86);padding:0 10px;font:inherit;color:var(--text);min-width:0}.archbox .field.err input{border-color:#C0392B}.archauto{font-size:13px;color:var(--dim);min-height:1.4em}.archbox .err{grid-column:2;color:#C0392B;font-size:12px;min-height:1em}.archpaths{display:flex;flex-direction:column;gap:8px}.archpaths[hidden]{display:none}.fluxvram{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 12px;margin-top:6px;padding-top:8px;border-top:1px solid var(--line)}.fluxvram__t,.fluxvram__hint{grid-column:1/-1;font-size:12.5px;color:var(--dim)}.fluxvram__hint{font-size:12px}.fluxvram .field{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--dim)}.fluxvram select,.fluxvram input[type=number]{height:34px;border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.86);padding:0 8px;font:inherit;color:var(--text)}.fluxvram input[type=checkbox]{align-self:flex-start;width:18px;height:18px}.fluxvram .field.err input,.fluxvram .field.err select{border-color:#C0392B}.fluxvram .err{color:#C0392B;font-size:12px;min-height:1em}";
      D.head.appendChild(st);
    }
  }
  function collectPromptBoxes() {
    var boxNodes = D.querySelectorAll ? D.querySelectorAll(".promptbox") : [];
    if (boxNodes && boxNodes.length) {
      var out = [];
      for (var i = 0; i < boxNodes.length; i++) {
        var value = String((boxNodes[i] && boxNodes[i].value) || "").trim();
        if (value) out.push(value);
      }
      return out;
    }
    var legacy = $("prompts");
    return String((legacy && legacy.value) || "").split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
  }
  function initImageParams() {
    envDoctor();
    initCaption();
    initImageSaveControls();
    ensureImageArchForm();
    var initQs = new W.URLSearchParams(SEARCH);
    var initModel0 = ssGet("ms.image.model") || initQs.get("model") || "";
    initImageArchDisplay(initModel0);
    var start = $("start");
    if (!start) return;
    start.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      var qs = new W.URLSearchParams(SEARCH);
      var model = ssGet("ms.image.model") || qs.get("model") || "";
      var data = qs.get("data") || ssGet("ms.image.data") || W.__imageData || "";
      var arch = archFromModel(model);
      initImageArchDisplay(model);
      if (arch === "flux2_klein_9b") model = ($("flux2Model") || {}).value ? $("flux2Model").value.trim() : model;
      if (arch === "krea2_raw") model = ($("kreaModel") || {}).value ? $("kreaModel").value.trim() : model;
      var vals = planInts();
      var steps = vals[0] || 0, res = vals[1] || 1024;
      var every = (vals[2] !== undefined && vals[2] !== null) ? vals[2] : 200;
      if (!(every >= 0) || !isFinite(every)) every = 200;
      var saveModeEl = $("saveMode");
      var saveMode = saveModeEl ? saveModeEl.value : imageSaveMode();
      ssSet("ms.image.saveMode", saveMode);
      var saveSteps = readSaveSteps(every);
      var prompts = collectPromptBoxes();
      if (!model || !data) { text($("plan"), "缺少底模/数据路径，请回上一步"); return; }
      if (!validateImageArch(arch)) { text($("plan"), "请先补齐必填路径"); return; }
      if (!steps) { text($("plan"), "请先设置训练步数（steps），未提交任务"); return; }
      var out = "E:\\视频\\modelsmith\\_runs\\ui-diffsynth-" + stamp();
      var params = {
        model: model, dataset: data, metadata: "", out: out,
        arch: arch,
        steps: steps, height: res, width: res,
        lr: readValue("lr", 0.0001), save_steps: saveSteps, max_step_saves_to_keep: 5,
        sample_every: every, sample_prompts: prompts, sample_width: res, sample_height: res,
        save_mode: saveMode
      };
      applyImageMeta(null, { steps: steps, sample_every: every, save_steps: saveSteps });
      var rankVal = optionalNumber(qs, "rank");
      if (rankVal !== null && rankVal > 0) params.rank = Math.round(rankVal);
      var capExt = captionExtValue(qs);
      if (capExt) params.caption_ext = capExt;
      var pArch = params.arch;
      if (pArch === "flux2_klein_9b") params.tokenizer_path = model.replace(/[\\/]+$/, "") + "\\tokenizer";
      if (pArch === "krea2_raw") {
        params.text_encoder = ssGet("ms.image.krea.te") || KREA_TE_DEFAULT;
        params.vae = ssGet("ms.image.krea.vae") || KREA_VAE_DEFAULT;
        params.tokenizer_path = params.text_encoder;
      }
      if (!applyFluxVram(params, pArch)) { text($("plan"), "请检查 FLUX.2 显存参数"); return; }
      applyArchParams(params, pArch);
      text($("plan"), "正在提交训练任务…");
      api("/jobs", { method: "POST", body: { kind: "diffsynth", params: params } }).then(function (j) {
        if (!j || j.ok === false) throw { status: j && j.status, message: coreFail(j, "核心返回失败") };
        var job = (j && j.job) || j || {};
        var id = (j && j.job_id) || job.id || job.job_id;
        if (!id) throw new Error("核心未返回 job_id");
        setTrainingGlow(true);
        ssSet("ms.image.job", id);
        ssSet("ms.image.model", model);
        ssSet("ms.image.data", data);
        W.location.href = "image-train.html?model=" + encodeURIComponent(model) +
          "&data=" + encodeURIComponent(data) + "&steps=" + encodeURIComponent(String(steps)) +
          "&prompts=" + encodeURIComponent(prompts.join("\n"));
      }).catch(function (err) {
        text($("plan"), "提交失败：" + ((err && err.status) ? "HTTP " + err.status : (err && err.message === "核心未返回 job_id" ? err.message : "核心未连接")));
      });
    }, true);
  }
  function initStoredImageTrain(id) {
    var rounds0 = $("rounds");
    if (rounds0) {
      var s0 = rounds0.querySelector(".round [data-act]");
      IMG_TEMPLATE = s0 ? s0.closest(".round") : rounds0.querySelector(".round");
    }
    stopDemoTimers();
    var title = $("title"), logEl = $("log"), doneEl = $("done");
    var stepEl = $("step"), spEl = $("sp"), barEl = $("bar"), timeEl = $("time"), shotsEl = $("shots");
    bindCancelOnce($("cancel"), logEl, title);
    var t0 = Date.now();
    text(title, "正在加载任务…");
    text(stepEl, "0 / —"); text(spEl, "—"); text(timeEl, "—"); text(shotsEl, "0");
    var storedGuard = guardZeroProgress(stepEl, spEl, timeEl, shotsEl, 0);
    JOB.id = id; JOB.kind = "diffsynth"; JOB.offset = 0; JOB.frozen = false;
    pollJob({
      onLogs: function (lines) { for (var i = 0; i < lines.length; i++) appendLog(logEl, lines[i]); },
      onJob: function (job) {
        if (JOB.frozen) return;
        if (storedGuard) { storedGuard.stop(); storedGuard = null; }
        PAGES_IMG_JOB = job;
        applyImageMeta(job);
        var m = job.metrics || {};
        var total = Number(m.total || (job.params && job.params.steps) || 0);
        var step = (m.step === null || m.step === undefined) ? 0 : Number(m.step);
        text(stepEl, step + " / " + (total || "—"));
        text(spEl, (m.sec_per_it !== null && m.sec_per_it !== undefined) ? Number(m.sec_per_it).toFixed(2) : "—");
        if (barEl) barEl.style.width = Math.min(100, total ? step / total * 100 : 0) + "%";
        text(timeEl, (m.elapsed_s !== null && m.elapsed_s !== undefined) ? mmss(m.elapsed_s) : mmss((Date.now() - t0) / 1000));
        var ev = m.sample_events || m.samples || [];
        if (ev.length) { text(shotsEl, ev.length); renderImageRounds(job); }
        else { imgActivateGuard(job); imgEnforce(job); text(shotsEl, "0"); }
      },
      onDone: function (job) {
        var m = job.metrics || {}, out = (job.params && job.params.out) || ssGet("ms.image.out") || "";
        var total = Number(m.total || (job.params && job.params.steps) || 0);
        if (job.state === "succeeded") {
          armFreeze(function () {
            text(title, "训练完成");
            show(doneEl);
            if (total) text(stepEl, total + " / " + total);
            var ev = (job.metrics && (job.metrics.sample_events || job.metrics.samples)) || [];
            if (ev.length) { text(shotsEl, ev.length); renderImageRounds(job); }
            renderDoneLoras(job, out);
            setDisabled($("cancel"), true);
            setDisabled($("finish"), false);
          });
        } else if (job.state === "cancelled") {
          armFreeze(function () {
            text(title, "已停训（保留已落盘 LoRA）");
            show(doneEl);
            renderTerminalArtifacts(job, out);
            setDisabled($("cancel"), true);
            setDisabled($("finish"), false);
          });
        } else {
          var fs = (m.step === null || m.step === undefined) ? 0 : Number(m.step);
          var ft = (m.elapsed_s !== null && m.elapsed_s !== undefined) ? mmss(m.elapsed_s) : "—";
          armFreeze(function () {
            text(title, "训练失败");
            text(stepEl, fs + " / " + total);
            text(spEl, "—");
            if (barEl) barEl.style.width = "0%";
            text(timeEl, ft);
            text(shotsEl, "0");
            imgActivateGuard(job); imgEnforce(job);
            show(doneEl);
            renderTerminalArtifacts(job, out);
            setDisabled($("cancel"), true);
          });
        }
      },
      onError: function (msg) { text(title, msg); appendLog(logEl, msg); }
    });
  }
  function guardZeroProgress(stepEl, spEl, timeEl, shotsEl, total) {
    if (!stepEl || !W.MutationObserver) return null;
    var busy = false;
    function reset() {
      if (busy) return;
      busy = true;
      text(stepEl, "0 / " + (total ? total : "—"));
      text(spEl, "—");
      text(timeEl, "—");
      text(shotsEl, "0");
      busy = false;
    }
    reset();
    var obs = new W.MutationObserver(reset);
    [stepEl, spEl, timeEl, shotsEl].forEach(function (el) {
      if (el) obs.observe(el, { childList: true, characterData: true, subtree: true });
    });
    return { stop: function () { try { obs.disconnect(); } catch (e) {} } };
  }
  var FLUX2_MODEL_DEFAULT = "" /* 不预填：由用户从模型列表选择 */;
  var KREA_RAW_DEFAULT = "" /* 不预填：由用户从模型列表选择 */;
  var KREA_TE_DEFAULT = "" /* 不预填 */;
  var KREA_VAE_DEFAULT = "" /* 不预填 */;
  function archFromModel(model) {
    var p = String(model || "");
    if (!p) return null;
    if (/z[-_]?image/i.test(p)) return "z_image";
    if (/flux2-klein-9b/i.test(p)) return "flux2_klein_9b";
    if (/krea-2-raw/i.test(p)) return "krea2_raw";
    if (/qwen-image-2\.1/i.test(p)) return "qwen_image_21";
    if (/moss-image-sft/i.test(p)) return "moss_image";
    return null;
  }
  function archForModel(model, cls) {
    return archFromModel(model) || archForClass(cls);
  }
  function initImageArchDisplay(model) {
    var arch = archFromModel(model);
    var f = $("flux2Model"), km = $("kreaModel"), te = $("kreaTextEncoder"), vae = $("kreaVae");
    var ab = $("archBox");
    if (ab && !ab.__msArchFilled) {
      ab.__msArchFilled = true;
      if (f && !f.value) f.value = /flux2/i.test(model) ? model : FLUX2_MODEL_DEFAULT;
      if (km && !km.value) km.value = /krea-2-raw/i.test(model) ? model : KREA_RAW_DEFAULT;
      if (te && !te.value) te.value = ssGet("ms.image.krea.te") || KREA_TE_DEFAULT;
      if (vae && !vae.value) vae.value = ssGet("ms.image.krea.vae") || KREA_VAE_DEFAULT;
    }
    var fw = $("flux2Fields"), kw = $("kreaFields");
    if (fw) fw.hidden = arch !== "flux2_klein_9b";
    if (kw) kw.hidden = arch !== "krea2_raw";
    var ae = $("archError");
    if (ae) text(ae, arch ? "" : "无法从底模判定架构，请从模型列表选择");
    return arch;
  }
  function validateImageArch(arch) {
    if (!arch) {
      var ae = $("archError"); if (ae) text(ae, "无法从底模判定架构，请从模型列表选择");
      return false;
    }
    var items = [];
    var first = null;
    for (var i = 0; i < items.length; i++) {
      var input = $(items[i][0]), label = input ? input.closest(".field") : null, err = label ? label.nextElementSibling : null;
      var ok = input && /^[A-Za-z]:[\\/]/.test(String(input.value || "").trim());
      if (label) { if (ok) label.classList.remove("err"); else label.classList.add("err"); }
      if (err && err.classList && err.classList.contains("err")) text(err, ok ? "" : (items[i][1] + " 必填绝对路径"));
      if (!ok && !first) first = input;
    }
    var ae = $("archError");
    if (first) { if (ae) text(ae, "请先补齐必填路径"); first.focus(); return false; }
    if (ae) text(ae, ""); return true;
  }
  function markFluxErr(input, msg) {
    if (!input) return;
    var label = input.closest(".field"), err = label ? (label.querySelector(".err") || label.nextElementSibling) : null;
    if (label) { if (msg) label.classList.add("err"); else label.classList.remove("err"); }
    if (err && err.classList && err.classList.contains("err")) text(err, msg || "");
  }
  function readFluxVram() {
    var s = $("fluxMaxPixels"), c = $("fluxOffload"), r = $("fluxRank");
    if (!s || !c || !r) return null;
    var mp = Number(s.value), rank = Number(r.value), first = null;
    var okMp = isFinite(mp) && mp >= 4096 && mp % 256 === 0;
    var okRank = isFinite(rank) && Math.floor(rank) === rank && rank >= 1 && rank <= 256;
    markFluxErr(s, okMp ? "" : "max_pixels 必须是 256 的倍数且 ≥4096");
    markFluxErr(r, okRank ? "" : "LoRA rank 必须是 1..256 的整数");
    if (!okMp) first = s;
    if (!okRank && !first) first = r;
    if (first) { first.focus(); return null; }
    return { max_pixels: mp, enable_model_cpu_offload: !!c.checked, rank: rank };
  }
  function applyFluxVram(params, arch) {
    // UI 上不再提供 FLUX.2 显存旋钮（用户未授权新增控件）；
    // 需要调 max_pixels / offload 时走 API 的 params.max_pixels、params.enable_model_cpu_offload。
    return true;
  }
  function applyArchParams(params, arch) {
    if (arch === "flux2_klein_9b") {
      var f = $("flux2Model");
      if (f) params.model = f.value.trim();
      var fm = params.model || "";
      if (fm) params.tokenizer_path = fm.replace(/[\\/]+$/, "") + "\\tokenizer";
    } else if (arch === "krea2_raw") {
      var km = $("kreaModel"), te = $("kreaTextEncoder"), vae = $("kreaVae");
      if (km) params.model = km.value.trim();
      if (te) params.text_encoder = te.value.trim();
      if (vae) params.vae = vae.value.trim();
      if (te) params.tokenizer_path = te.value.trim();
      if (te) ssSet("ms.image.krea.te", te.value.trim());
      if (vae) ssSet("ms.image.krea.vae", vae.value.trim());
    }
    return params;
  }
  function initImageTrain() {
    bindResumeButton();
    var storedJob = ssGet("ms.image.job");
    if (storedJob) { initStoredImageTrain(storedJob); return; }
    var qs = new W.URLSearchParams(SEARCH);
    var model = ssGet("ms.image.model") || qs.get("model") || "";
    var data = qs.get("data") || W.__imageData || "";
    var prompts = String(qs.get("prompts") || "").split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    var arch = archFromModel(model);
    initImageArchDisplay(model);
    if (arch === "flux2_klein_9b") model = ($("flux2Model") || {}).value ? $("flux2Model").value.trim() : model;
    if (arch === "krea2_raw") model = ($("kreaModel") || {}).value ? $("kreaModel").value.trim() : model;
    if (!validateImageArch(arch)) {
      stopDemoTimers();
      text($("title"), "参数不完整");
      return;
    }
    var MOSS_TEXT = "该底模当前训练不产出采样图（可训练后离线验证）";
    function mossNote() {
      var r = $("rounds");
      if (r) { while (r.firstChild) r.removeChild(r.firstChild); text(r, MOSS_TEXT); }
      if ($("shots")) text($("shots"), "0");
    }
    if (arch === "moss_image") {
      mossNote();
      W.setInterval(mossNote, 100);
      appendLog($("log"), "MOSS 训练不产出采样图；产物以接口 artifacts 为准。");
    }
    var title = $("title"), logEl = $("log"), doneEl = $("done");
    var stepEl = $("step"), spEl = $("sp"), barEl = $("bar"), timeEl = $("time"), shotsEl = $("shots");
    var samplesNoted = false;
    var OUT = "E:\\视频\\modelsmith\\_runs\\ui-diffsynth-" + stamp();
    var t0 = Date.now();
    var stepsVal = qs.get("steps");
    if (stepsVal === null || stepsVal === undefined || String(stepsVal).trim() === "") stepsVal = readValue("steps", null);
    if (stepsVal === null || stepsVal === undefined || String(stepsVal).trim() === "" || !(Number(stepsVal) > 0)) {
      stopDemoTimers();
      appendNeedSteps(logEl, model, data, title);
      text(stepEl, "— / —");
      if (barEl) barEl.style.width = "0%";
      if (shotsEl) text(shotsEl, "0");
      return;
    }
    if (!model || !data) {
      stopDemoTimers();
      text(stepEl, "0 / —");
      text(spEl, "—");
      text(timeEl, "—");
      if (barEl) barEl.style.width = "0%";
      if (shotsEl) text(shotsEl, "0");
      armFreeze(function () {
        text(title, "当前没有训练任务");
        text(stepEl, "0 / —");
        text(spEl, "—");
        text(timeEl, "—");
        if (barEl) barEl.style.width = "0%";
        if (shotsEl) text(shotsEl, "0");
      });
      var zeroGuard = guardZeroProgress(stepEl, spEl, timeEl, shotsEl, 0);
      W.__MS_ZERO_GUARD__ = zeroGuard;
      return;
    }
    var steps = Math.max(1, Math.round(Number(stepsVal)));
    var sampleEvery = Math.max(0, Math.round(readValue("sample_every", 200)));
    var saveMode = imageSaveMode();
    var saveSteps = readSaveSteps(sampleEvery);
    applyImageMeta(null, { steps: steps, sample_every: sampleEvery, save_steps: saveSteps, save_mode: saveMode });
    var params = {
      model: model, dataset: data, metadata: "", out: OUT,
      arch: arch,
      steps: steps,
      height: Math.max(64, Math.round(readValue("height", 1024))),
      width: Math.max(64, Math.round(readValue("width", 1024))),
      lr: readValue("lr", 0.0001),
      save_steps: saveSteps,
      max_step_saves_to_keep: 5,
      sample_every: sampleEvery,
      sample_prompts: prompts,
      sample_width: Math.max(64, Math.round(readValue("width", 1024))),
      sample_height: Math.max(64, Math.round(readValue("height", 1024))),
      save_mode: saveMode
    };
    var imgRank = optionalNumber(qs, "rank");
    if (imgRank !== null && imgRank > 0) params.rank = Math.round(imgRank);
    var capExt = captionExtValue(qs);
    if (capExt) params.caption_ext = capExt;
    if (!applyFluxVram(params, arch)) { stopDemoTimers(); text(title, "FLUX.2 显存参数不合法"); return; }
    applyArchParams(params, arch);
    submitJob("diffsynth", params, {
      onStart: function () { text(title, "正在提交训练…"); },
      onCreated: function (job) { PAGES_IMG_JOB = job; text(title, "正在训练"); appendLog(logEl, "job " + job.job_id + " · kind=diffsynth · " + (job.state || "")); },
      onLogs: function (lines) { for (var i = 0; i < lines.length; i++) appendLog(logEl, lines[i]); },
      onHistory: function (h) {
        var rows = (h && h.rows) || [];
        if (!rows.length) return;
        var last = rows[rows.length - 1], d = last.data || last;
        var total = Number(d.total || steps);
        var step = Number(d.step || 0);
        text(stepEl, step + " / " + total);
        if (barEl) barEl.style.width = Math.min(100, total ? step / total * 100 : 0) + "%";
        if (d.sec_per_it !== undefined) text(spEl, Number(d.sec_per_it).toFixed(2));
        appendLog(logEl, "已回放 " + rows.length + " 条历史指标（metrics_history since=0）");
      },
      onJob: function (job) {
        if (JOB.frozen) return;
        applyImageMeta(job);
        var m = job.metrics || {};
        var total = (m.total !== undefined && m.total !== null && Number(m.total) > 0) ? Number(m.total) : 0;
        var step = Number(m.step || 0);
        text(stepEl, step + " / " + (total || "—"));
        if (m.sec_per_it !== undefined && m.sec_per_it !== null) text(spEl, Number(m.sec_per_it).toFixed(2)); else text(spEl, "—");
        if (barEl) barEl.style.width = Math.min(100, total ? step / total * 100 : 0) + "%";
        text(timeEl, mmss((Date.now() - t0) / 1000));
        var ev = m.sample_events || m.samples || [];
        if (ev && ev.length) { text(shotsEl, ev.length); renderImageRounds(job); }
        else {
          imgActivateGuard(job);
          imgEnforce(job);
          text(shotsEl, "0");
          if (!samplesNoted) { samplesNoted = true; appendLog(logEl, "本次运行未出采样图（核心未回传 sample_events）。"); }
        }
      },
      onDone: function (job) {
        var m = job.metrics || {}, out = (job.params && job.params.out) || OUT;
        if (job.state === "succeeded") {
          var doneTotal = Number(m.total || (job.params && job.params.steps) || steps || 0);
          armFreeze(function () {
            text(title, "训练完成");
            show(doneEl);
            if (doneTotal) text(stepEl, doneTotal + " / " + doneTotal);
            renderDoneLoras(job, OUT);
            renderImageRounds(job);
            var ev = (job.metrics && (job.metrics.sample_events || job.metrics.samples)) || [];
            if (ev.length && shotsEl) text(shotsEl, ev.length);
            setDisabled($("cancel"), true);
            setDisabled($("finish"), false);
          });
          api("/jobs/" + encodeURIComponent(JOB.id) + "/artifacts").then(function (j) {
            var rows = (j && j.artifacts) || [];
            var stars = (job.metrics && job.metrics.starred_artifacts) || [];
            renderLoraRows(rows, out, stars);
            armFreeze(function () {
              text(title, "训练完成");
              show(doneEl);
              renderLoraRows(rows, out, stars);
              setDisabled($("cancel"), true);
              setDisabled($("finish"), false);
            });
          }).catch(function () {});
          appendLog(logEl, "训练完成：产物写入 " + out);
        } else if (job.state === "cancelled") {
          armFreeze(function () {
            text(title, "已停训（保留已落盘 LoRA）");
            show(doneEl);
            renderTerminalArtifacts(job, OUT);
            setDisabled($("cancel"), true);
            setDisabled($("finish"), false);
          });
        } else {
          var failTotal = Number(m.total || (job.params && job.params.steps) || steps);
          var failStep = (m.step === null || m.step === undefined) ? 0 : Number(m.step);
          if (!isFinite(failStep)) failStep = 0;
          var failTime = (m.elapsed_s !== undefined && m.elapsed_s !== null) ? mmss(m.elapsed_s) : "—";
          armFreeze(function () {
            text(title, "训练失败");
            text(stepEl, failStep + " / " + failTotal);
            text(spEl, "—");
            if (barEl) barEl.style.width = (failTotal && failStep ? Math.min(100, failStep / failTotal * 100) : 0) + "%";
            text(timeEl, failTime);
            text(shotsEl, "0");
            imgActivateGuard(job);
            imgEnforce(job);
            show(doneEl);
            renderTerminalArtifacts(job, OUT);
            setDisabled($("cancel"), true);
          });
          appendLog(logEl, "训练失败：" + (job.error || job.state));
        }
      },
      onError: function (msg) { text(title, msg); appendLog(logEl, msg); }
    });
    bindCancelOnce($("cancel"), logEl, title);
    var loros = $("lorolist");
    if (loros) {
      loros.title = "双击路径打开目录";
      loros.addEventListener("dblclick", function (e) {
        var c = e.target.closest("code");
        if (c) openPath(c.textContent, logEl);
      });
    }
    D.addEventListener("click", function (e) {
      var b = e.target.closest('[data-act="stop"]');
      if (!b) return;
      e.preventDefault(); e.stopImmediatePropagation();
      b.textContent = "已停训保留";
      appendLog(logEl, "已请求停训保留：" + (JOB.id || ""));
      cancelJob({ onCancelSent: function () { appendLog(logEl, "取消请求已发送；已落盘 step LoRA 保留。"); } });
    }, true);
    D.addEventListener("click", function (e) {
      var b = e.target.closest('[data-act="star"]');
      if (!b) return;
      e.preventDefault(); e.stopImmediatePropagation();
      var row = b.closest(".round");
      var step = row ? row.getAttribute("data-step") : "";
      var loraEl = row ? row.querySelector(".round__lora") : null;
      var artifact = loraEl ? String(loraEl.textContent).replace(/^LoRA:\s*/i, "").trim() : "";
      if (!artifact || artifact === "—") {
        b.textContent = "☆ 无产物";
        appendLog(logEl, "标星失败：没有可用产物");
        return;
      }
      api("/jobs/" + encodeURIComponent(JOB.id || "") + "/star", { method: "POST", body: { artifact: artifact, starred: true } }).then(function (j) {
        if (j && j.ok === false) {
          b.textContent = "☆ 标星失败";
          appendLog(logEl, "标星失败：" + coreFail(j));
          return;
        }
        b.textContent = "★ 已标候选";
      }).catch(function (err) {
        b.textContent = (err && err.status === 404) ? "★ 已标候选（本地）" : "★ 本地";
        if (W.console && W.console.warn) W.console.warn("[ms-wire] /star", err && err.status);
      });
    }, true);
  }
  var EMPTY_ARTIFACT_TEXT = "本次没有产出权重（取消/失败前没有存档）";
  var TERMINAL_EMPTY_ARTIFACT_TEXT = "本次没有产出权重（save_steps=0 时中途停止不会存档）";
  var ARTIFACT_VIEW = { rows: [], out: "" };
  function artifactPath(a) {
    if (a && typeof a === "object") return String(a.path || a.name || "");
    return String(a || "");
  }
  function artifactName(a) {
    if (a && typeof a === "object") return String(a.name || basename(a.path || ""));
    return basename(String(a || ""));
  }
  function artifactStarSet(starred) {
    var set = {};
    starred = starred || [];
    for (var i = 0; i < starred.length; i++) {
      var s = starred[i];
      var v = (s && typeof s === "object") ? (s.name || s.path || "") : s;
      v = String(v || "");
      if (v) set[v.toLowerCase()] = true;
      var b = basename(v);
      if (b) set[b.toLowerCase()] = true;
    }
    return set;
  }
  function artifactIsStarred(row, set) {
    var p = String(row.path || "").toLowerCase(), n = String(row.name || "").toLowerCase();
    return !!(set[n] || set[p] || set[basename(row.path).toLowerCase()]);
  }
  function toggleArtifactStar(row, btn) {
    if (!JOB.id || !row) return;
    var next = !row.starred;
    btn.disabled = true;
    api("/jobs/" + encodeURIComponent(JOB.id) + "/star", {
      method: "POST",
      body: { artifact: row.name, starred: next }
    }).then(function (j) {
      if (!j || j.ok === false) throw new Error(coreFail(j, "标星失败"));
      row.starred = next;
      var stars = [];
      for (var i = 0; i < ARTIFACT_VIEW.rows.length; i++) {
        if (ARTIFACT_VIEW.rows[i].starred) stars.push(ARTIFACT_VIEW.rows[i].name);
      }
      renderLoraRows(ARTIFACT_VIEW.rows, ARTIFACT_VIEW.out, stars);
    }).catch(function (err) {
      btn.disabled = false;
      if (W.console && W.console.warn) W.console.warn("[ms-wire] /star", err && err.message);
    });
  }
  function fillResumeChoices(rows) {
    var sel = $("resumeFrom"), btn = $("resumeBtn");
    if (!sel || !btn) return;
    var old = sel.value, seen = {};
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    var ph = D.createElement("option");
    ph.value = ""; ph.textContent = "选择要续训的 LoRA…";
    sel.appendChild(ph);
    for (var i = 0; i < (rows || []).length; i++) {
      var p = rows[i] && rows[i].path;
      if (!p || !/\.safetensors$/i.test(p) || !/^[A-Za-z]:[\\/]/.test(p) || seen[p]) continue;
      seen[p] = true;
      var o = D.createElement("option");
      o.value = p; o.textContent = p.split(/[\\/]/).pop();
      sel.appendChild(o);
    }
    if (old && seen[old]) sel.value = old;
    btn.disabled = !sel.value;
    if (sel.options.length <= 1 && (rows || []).length && $("resumeMsg") && !$("resumeMsg").textContent) {
      text($("resumeMsg"), "缺少输出目录，无法续训");
    }
  }
  function bindResumeButton() {
    var btn = $("resumeBtn"), sel = $("resumeFrom"), msg = $("resumeMsg");
    if (!btn || !sel || btn.__msBound) return;
    btn.__msBound = true;
    sel.addEventListener("change", function () { btn.disabled = !sel.value; });
    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      var path = sel.value;
      if (!path || !/^[A-Za-z]:[\\/]/.test(path)) return;
      var job = PAGES_IMG_JOB;
      if (!job || !job.params) { if (msg) text(msg, "还没有可续训的任务参数"); return; }
      var params = JSON.parse(JSON.stringify(job.params));
      params.resume_from = path;
      params.out = "E:\\视频\\modelsmith\\_runs\\ui-diffsynth-" + stamp();
      text(btn, "正在提交续训…"); btn.disabled = true;
      api("/jobs", { method: "POST", body: { kind: "diffsynth", params: params } }).then(function (j) {
        if (!j || j.ok === false) throw new Error(coreFail(j, "核心返回失败"));
        var id = (j && j.job_id) || (j.job && (j.job.id || j.job.job_id));
        if (msg) text(msg, id ? ("已提交续训 job " + id) : "已提交续训任务");
        text(btn, "已提交续训");
      }).catch(function (err) {
        text(btn, "从这份 LoRA 继续训"); btn.disabled = !sel.value;
        if (msg) text(msg, "续训提交失败：" + ((err && err.message) || "核心未连接"));
      });
    });
  }
  function renderLoraRows(rows, out, starred, emptyText) {
    var list = $("lorolist"); if (!list) return;
    rows = rows || [];
    var set = artifactStarSet(starred);
    var norm = [];
    var baseOut = String(out || "").replace(/[\\/]+$/, "");
    var missingOut = false;
    for (var i = 0; i < rows.length; i++) {
      var p = artifactPath(rows[i]);
      if (!p) continue;
      var name = artifactName(rows[i]);
      if (!/^[A-Za-z]:[\\/]/.test(p) && baseOut && !/^[\\/]/.test(p)) p = baseOut + "\\" + p.replace(/^[\\/]+/, "");
      if (!/^[A-Za-z]:[\\/]/.test(p)) missingOut = true;
      var r = { path: p, name: name, starred: false };
      r.starred = artifactIsStarred(r, set) || !!(rows[i] && rows[i].starred);
      norm.push(r);
    }
    if (missingOut && $("resumeMsg")) text($("resumeMsg"), "缺少输出目录，无法续训");
    norm.sort(function (a, b) {
      if (a.starred === b.starred) return 0;
      return a.starred ? -1 : 1;
    });
    ARTIFACT_VIEW.rows = norm;
    ARTIFACT_VIEW.out = out || "";
    fillResumeChoices(norm);
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!norm.length) {
      var empty = D.createElement("div");
      empty.className = "lorow";
      empty.textContent = emptyText || EMPTY_ARTIFACT_TEXT;
      list.appendChild(empty);
      return;
    }
    for (var k = 0; k < norm.length; k++) {
      var item = norm[k];
      var row = D.createElement("div");
      row.className = "lorow";
      row.setAttribute("data-artifact", item.name);
      var code = D.createElement("code");
      text(code, (item.starred ? "★ " : "") + item.path);
      var btn = D.createElement("button");
      btn.className = "mini";
      btn.type = "button";
      btn.setAttribute("data-artifact", item.name);
      btn.setAttribute("data-starred", item.starred ? "1" : "0");
      btn.title = item.starred ? "取消星标" : "星标候选";
      text(btn, item.starred ? "★" : "☆");
      (function (r, b) {
        b.addEventListener("click", function (e) {
          e.preventDefault(); e.stopImmediatePropagation();
          toggleArtifactStar(r, b);
        });
      })(item, btn);
      row.appendChild(code);
      row.appendChild(btn);
      list.appendChild(row);
    }
  }
  function renderDoneLoras(job, fallbackOut) {
    var m = (job && job.metrics) || {};
    renderLoraRows(m.artifacts || [], fallbackOut, m.starred_artifacts);
  }
  function renderTerminalArtifacts(job, fallbackOut) {
    var m = (job && job.metrics) || {};
    var items = m.artifacts || [];
    var titleEl = D.querySelector ? D.querySelector("#done .done__t") : null;
    if (items.length) {
      if (titleEl) text(titleEl, "已保存的 LoRA（可提前中止后取用）");
      renderLoraRows(items, fallbackOut, m.starred_artifacts);
    } else {
      if (titleEl) text(titleEl, TERMINAL_EMPTY_ARTIFACT_TEXT);
      renderLoraRows([], fallbackOut, m.starred_artifacts, TERMINAL_EMPTY_ARTIFACT_TEXT);
    }
  }

  // ---------------- decision (index deck) ----------------
  var SAMPLE_DATA = "" /* 不预填本机样例数据 */;
  var LAYA_BASE = "" /* 不预填本机模型路径 */;
  var DEC = { job: null, pending: null, logs: [], started: false, runningGuard: false, dataPath: "" };

  function decisionParams(mode) {
    var base = LAYA_BASE;
    if (mode === "retrain") {
      var sel = D.querySelector("#dec5Start .opt[aria-checked='true']");
      var startId = sel ? sel.getAttribute("data-id") : "last";
      if (startId === "scratch") {
        base = DEC.layaModelPath || LAYA_BASE;
      } else {
        var lastOut = "";
        try { lastOut = W.localStorage.getItem("ms.last.laya.out") || ""; } catch (e) {}
        if (!lastOut) return { error: "还没有可续训的上一段训练" };
        base = lastOut;
      }
    } else {
      var opt0 = D.querySelector("#dec1Opts .opt[aria-checked='true']");
      if (opt0 && opt0.getAttribute("data-path")) base = opt0.getAttribute("data-path");
    }
    var opt = D.querySelector("#dec1Opts .opt[aria-checked='true']");
    if (opt) {
      var id = opt.getAttribute("data-id");
      if (mode !== "retrain") {
        if (id === "multilingual") return { error: "多语言底座未下载；请先下载或换底座" };
        if (id === "local-dir" && !opt.getAttribute("data-path")) return { error: "自选模型需要绝对路径（当前 v3 页面没有路径输入框）" };
      }
    }
    var nameEl = $("dec2Name");
    var nm = nameEl ? nameEl.textContent : "";
    var data = DEC.dataPath || "";
    if (!data && nameEl && nm && nm.indexOf("样例") >= 0) data = SAMPLE_DATA;
    if (!data) return { error: "浏览器拿不到绝对路径，需核心在线（或改用示例数据）" };
    var epochs = mode === "retrain" ? 2 : 4;
    var p = {
      data: data, out: "E:\\视频\\modelsmith\\_runs\\ui-laya-" + stamp(), base: base,
      loss: "rlcd",
      epochs: Math.max(1, Math.round(readValue("epochs", mode === "retrain" ? 2 : 4))),
      micro_batch: Math.max(1, Math.round(readValue("micro_batch", 8))),
      grad_accum: Math.max(1, Math.round(readValue("grad_accum", 8))),
      encoder_lr: readValue("encoder_lr", 0.000025),
      head_lr: readValue("head_lr", 0.0001),
      max_len: Math.max(8, Math.round(readValue("max_len", 512))),
      shuffle_options: false,
      label_smoothing: readValue("label_smoothing", 0),
      calib_frac: readValue("calib_frac", 0.1),
      calib_max: Math.max(1, Math.round(readValue("calib_max", 400))),
      target_error: readValue("target_error", 0.1),
      min_abstain_n: Math.max(1, Math.round(readValue("min_abstain_n", 10)))
    };
    if (mode === "retrain") p.train_mode = "continued";
    return { params: p };
  }
  function drawDecisionChart() {
    var chart = $("dec3Chart"); if (!chart) return;
    var pts = DEC.logs;
    var Wc = 600, Hc = 220, PL = 46, PR = 14, PT = 14, PB = 26, MIN = 0.5, MAX = 1.1;
    var maxI = Math.max(1, pts.length ? pts[pts.length - 1].i : 1);
    function sx(i) { return PL + (Wc - PL - PR) * (i / Math.max(maxI, 1)); }
    function sy(v) { var t = (v - MIN) / (MAX - MIN); t = Math.max(0, Math.min(1, t)); return PT + (Hc - PT - PB) * (1 - t); }
    var g = "";
    [0.6, 0.8, 1.0].forEach(function (v) {
      var y = sy(v);
      g += '<line class="grid" x1="' + PL + '" y1="' + y + '" x2="' + (Wc - PR) + '" y2="' + y + '"/>';
      g += '<text class="tick" x="' + (PL - 6) + '" y="' + (y + 3) + '" text-anchor="end">' + v.toFixed(1) + '</text>';
    });
    g += '<line class="axis" x1="' + PL + '" y1="' + (Hc - PB) + '" x2="' + (Wc - PR) + '" y2="' + (Hc - PB) + '"/>';
    g += '<line class="axis" x1="' + PL + '" y1="' + PT + '" x2="' + PL + '" y2="' + (Hc - PB) + '"/>';
    g += '<text class="lbl" x="' + (PL + 4) + '" y="' + (PT - 3) + '">loss · 横轴 micro-step（每 100 条一个点，≠ 更新步）</text>';
    if (pts.length > 1) g += '<polyline class="curve" points="' + pts.map(function (p) { return sx(p.i).toFixed(1) + ',' + sy(p.v).toFixed(1); }).join(' ') + '"/>';
    chart.innerHTML = '<svg viewBox="0 0 ' + Wc + ' ' + Hc + '">' + g + '</svg>';
  }
  function decisionCollectLogs(lines) {
    for (var i = 0; i < lines.length; i++) {
      appendLog($("dec3Log"), lines[i]);
      var m = String(lines[i]).match(/epoch\s+(\d+)\s*\/\s*(\d+)\s+step\s+(\d+)\s+loss\s+([0-9.]+)/i);
      if (m) DEC.logs.push({ i: Number(m[3]), v: Number(m[4]) });
    }
    drawDecisionChart();
  }
  function decisionStart() {
    DEC.started = true;
    var title = $("dec3Title"), logEl = $("dec3Log");
    if (DEC.pending) { text(title, "正在提交训练…"); return; }
    if (!DEC.job) { text(title, "等待提交…"); return; }
    if (["succeeded", "failed", "cancelled", "interrupted"].indexOf(DEC.job.state) >= 0) {
      decisionDone(DEC.job);
      return;
    }
    text(title, "正在训练");
    appendLog(logEl, "job " + DEC.job.job_id + " · kind=laya · " + (DEC.job.state || ""));
    pollJob({
      onLogs: decisionCollectLogs,
      onJob: function (job) {
        if (JOB.frozen) return;
        var m = job.metrics || {};
        var cardNow = D.querySelector(".card[data-flow='decision'] .state");
        if (cardNow && (job.state === "running" || job.state === "queued" || job.state === "starting")) text(cardNow, "训练中");
        var ut = Number(m.updates_total || 0), et = Number(m.epochs_total || (job.params && job.params.epochs) || 4);
        var ep = Number(m.epoch || 1), ms = Number(m.micro_step || 0);
        var ga = Math.max(1, Number(m.grad_accum || 8));
        var done = ut ? Math.min(ut, (ep - 1) * Math.ceil(ut / et) + Math.ceil(ms / ga)) : 0;
        if (job.state === "succeeded") {
          done = ut || (m.updates !== undefined ? Number(m.updates) : done);
          if (m.micro_step !== undefined) ms = Number(m.micro_step);
          if (m.epochs_total !== undefined) et = Number(m.epochs_total);
          if ($("dec3Bar")) $("dec3Bar").style.width = "100%";
        } else if ($("dec3Bar")) {
          $("dec3Bar").style.width = ut ? Math.min(100, done / ut * 100) + "%" : "0%";
        }
        text($("dec3Upd2"), done + " / " + (ut || "—"));
        text($("dec3Micro"), ms + " / —");
        if (m.loss !== undefined) text($("dec3Loss"), Number(m.loss).toFixed(4));
        text($("dec3Ep"), ep + " / " + et);
        text($("dec3Vram"), m.peak_vram_alloc_mib ? (Number(m.peak_vram_alloc_mib) / 1024).toFixed(1) + " GiB" : "—");
        text($("dec3Time"), mmss(m.elapsed_s || 0));
      },
      onDone: function (job) { decisionDone(job); },
      onError: function (msg) { text(title, msg); appendLog(logEl, msg); }
    });
  }
  function decisionDone(job) {
    DEC.job = job;
    var m = job.metrics || {}, out = (job.params && job.params.out) || "";
    var title = $("dec3Title");
    if (job.state === "succeeded") {
      try {
        var lastOut = String(m.output_dir || out || "");
        if (lastOut) W.localStorage.setItem("ms.last.laya.out", lastOut);
        if (job.id || JOB.id) W.localStorage.setItem("ms.last.laya.job", String(job.id || JOB.id));
      } catch (e) {}
      var tbl = $("dec3Tbl"), cells = $("dec3Cells");
      var acc = m.accuracy !== undefined ? Number(m.accuracy).toFixed(4) : "—";
      var loss = m.loss !== undefined ? Number(m.loss).toFixed(4) : "—";
      if (tbl) tbl.innerHTML = '<tr><th>指标</th><th>训练前</th><th>训练后</th><th>变化</th></tr>' +
        '<tr><td>Accuracy</td><td class="en">—</td><td class="en">' + acc + '</td><td class="en">—</td></tr>' +
        '<tr><td>Loss</td><td class="en">—</td><td class="en">' + loss + '</td><td class="en">—</td></tr>';
      if (cells) {
        var evMode = (!job.params.eval_data) ? "after 来自校准切片 = in-sample，不是泛化" : "held-out 评测";
        cells.innerHTML = '<div class="done__cell"><div class="done__k">校准温度</div><div class="done__v">—</div><div class="done__k">核心未返回数值</div></div>' +
          '<div class="done__cell"><div class="done__k">拒答阈值</div><div class="done__v">—</div><div class="done__k">target_error 0.10</div></div>' +
          '<div class="done__cell"><div class="done__k">产物</div><div class="done__v" style="font-size:12px">' + (out ? basename(out) : "—") + '</div><div class="done__k">' + evMode + '</div></div>';
      }
      var finalTitle = "训练完成";
      var finalOut = out;
      var finalTbl = tbl ? tbl.innerHTML : "", finalCells = cells ? cells.innerHTML : "";
      var finalUpd = (m.updates_total !== undefined ? m.updates_total : (m.updates !== undefined ? m.updates : "—")) + " / " + (m.updates_total !== undefined ? m.updates_total : "—");
      var finalMicro = (m.micro_step !== undefined ? m.micro_step : "—") + " / —";
      var finalEp = (m.epoch !== undefined ? m.epoch : "?") + " / " + (m.epochs_total !== undefined ? m.epochs_total : "?");
      var finalLoss = m.loss !== undefined ? Number(m.loss).toFixed(4) : "—";
      var finalTime = mmss(m.elapsed_s || 0);
      armFreeze(function () {
        text(title, finalTitle);
        show($("dec3Done"));
        text($("dec3Out"), finalOut);
        if (tbl) tbl.innerHTML = finalTbl;
        if (cells) cells.innerHTML = finalCells;
        text($("dec3Upd2"), finalUpd);
        text($("dec3Micro"), finalMicro);
        text($("dec3Ep"), finalEp);
        text($("dec3Loss"), finalLoss);
        text($("dec3Time"), finalTime);
        if ($("dec3Bar")) $("dec3Bar").style.width = "100%";
        setDisabled($("dec3Finish"), false);
        setDisabled($("dec3Cancel"), true);
      });
      appendLog($("dec3Log"), "训练完成：update " + (m.updates_total || "—") + " · 产物 " + out);
      if (!job.params.eval_data) appendLog($("dec3Log"), "注意：after 来自校准切片（in-sample，不是泛化数）");
      api("/jobs/" + encodeURIComponent(job.id) + "/eval", { method: "POST", body: { data: "", limit: 96, with_predictions: true } }).then(function (ev) {
        if (!ev || ev.ok === false) {
          var evalErr = "评测失败：" + coreFail(ev, "核心未返回训练报告");
          appendLog($("dec3Log"), evalErr);
          finalTbl = '<tr><td colspan="4">' + evalErr + '</td></tr>';
          finalCells = '<div class="done__cell"><div class="done__k">评测</div><div class="done__v" style="font-size:12px">失败</div><div class="done__k">' + coreFail(ev, "核心未返回训练报告") + '</div></div>';
          if (tbl) tbl.innerHTML = finalTbl;
          if (cells) cells.innerHTML = finalCells;
          armFreeze(function () {
            text(title, finalTitle); show($("dec3Done")); text($("dec3Out"), finalOut);
            if (tbl) tbl.innerHTML = finalTbl; if (cells) cells.innerHTML = finalCells;
            setDisabled($("dec3Finish"), false); setDisabled($("dec3Cancel"), true);
          });
          return;
        }
        var mm = ev.metrics || {};
        function fmt(k, d) { return mm[k] !== undefined && mm[k] !== null ? Number(mm[k]).toFixed(4) : "—"; }
        if (tbl) tbl.innerHTML = '<tr><th>指标</th><th>训练前</th><th>训练后</th><th>变化</th></tr>' +
          '<tr><td>Accuracy</td><td class="en">—</td><td class="en">' + fmt("accuracy") + '</td><td class="en">—</td></tr>' +
          '<tr><td>Loss</td><td class="en">—</td><td class="en">' + fmt("loss") + '</td><td class="en">—</td></tr>' +
          '<tr><td>ECE</td><td class="en">—</td><td class="en">' + fmt("ece") + '</td><td class="en">—</td></tr>' +
          '<tr><td>Brier</td><td class="en">—</td><td class="en">' + fmt("brier_top1") + '</td><td class="en">—</td></tr>';
        if (cells) {
          cells.innerHTML = '<div class="done__cell"><div class="done__k">校准温度</div><div class="done__v">' + fmt("temperature") + '</div><div class="done__k">train_report.json</div></div>' +
            '<div class="done__cell"><div class="done__k">拒答阈值</div><div class="done__v">' + fmt("abstain_threshold") + '</div><div class="done__k">target_error 0.10</div></div>' +
            '<div class="done__cell"><div class="done__k">评测来源</div><div class="done__v" style="font-size:12px">' + (ev.data_source || "calibration") + '</div><div class="done__k">' + (ev.data_source === "calibration" ? "in-sample，不是泛化" : "held-out") + '</div></div>';
        }
        finalTbl = tbl ? tbl.innerHTML : "";
        finalCells = cells ? cells.innerHTML : "";
        armFreeze(function () {
          text(title, finalTitle); show($("dec3Done")); text($("dec3Out"), finalOut);
          if (tbl) tbl.innerHTML = finalTbl; if (cells) cells.innerHTML = finalCells;
          setDisabled($("dec3Finish"), false); setDisabled($("dec3Cancel"), true);
        });
        appendLog($("dec3Log"), "评测完成：data_source=" + (ev.data_source || "?") + " n=" + (ev.n || "?"));
      }).catch(function (err) {
        appendLog($("dec3Log"), (err && err.status === 404) ? "评测接口未就绪（核心尚未实装 /eval）" : "核心未连接");
      });
      setDisabled($("dec3Finish"), false);
      setDisabled($("dec3Cancel"), true);
    } else if (job.state === "cancelled") {
      text(title, "已取消");
      appendLog($("dec3Log"), "已取消：Laya 没有 resume，中断=从头再训；checkpoint_latest 只能拿回上一轮。");
      setDisabled($("dec3Cancel"), true);
      setDisabled($("dec3Finish"), false);
    } else {
      text(title, "训练失败");
      appendLog($("dec3Log"), "训练失败：" + (job.error || job.state));
      setDisabled($("dec3Cancel"), true);
    }
    var cardState = D.querySelector(".card[data-flow='decision'] .state");
    if (cardState) text(cardState, job.state === "succeeded" ? "已完成 · " + basename(out) : (job.state === "cancelled" ? "已取消" : "失败"));
  }
  function applyDataHint(path) {
    var note = $("dec2LangNote");
    if (!note || !path) return;
    api("/data/hint?path=" + encodeURIComponent(path)).then(function (j) {
      if (j && j.lang_hint === "zh") {
        note.hidden = false;
        text(note, "检测到中文语料：建议换多语言底座（lang_hint=zh）");
      } else {
        note.hidden = true;
      }
    }).catch(function (err) {
      if (err && err.status === 404) { note.hidden = true; }
    });
  }
  function pickPath(mode, fallbackInput, onSuccess) {
    return api("/pick", { method: "POST", body: { mode: mode, filter: mode === "file" ? ["jsonl", "csv"] : [] } }).then(function (j) {
      if (!j || j.ok === false || !j.path) {
        var msg = (j && j.cancelled) ? "已取消选择" : ((j && j.reason === "no_desktop") ? "核心无法弹对话框（no_desktop）" : ("选择失败：" + coreFail(j, "核心未返回路径")));
        if (j && j.reason === "no_desktop" && W.console && W.console.warn) W.console.warn("[ms-wire] " + msg);
        if (j && j.reason === "no_desktop" && $("metaState")) text($("metaState"), "浏览器拿不到绝对路径，需核心在线");
        return;
      }
      if (onSuccess) onSuccess(String(j.path));
    }).catch(function (err) {
      if ($("metaState")) text($("metaState"), (err && err.status === 404) ? "浏览器拿不到绝对路径，需核心在线" : "核心未连接");
      if (fallbackInput) fallbackInput.click();
    });
  }
  // ---------------- caption (auto-label) ----------------
  var CAPJOBS = { available: false, backend: "", busy: false, loading: false, notice: "" };
  function captionResult(value) {
    var el = $("captionResult");
    if (!el) return;
    var s = String(value == null ? "" : value);
    if (CAPJOBS.notice) s = s ? (s + " · " + CAPJOBS.notice) : CAPJOBS.notice;
    text(el, s);
  }
  function captionField(id) {
    var el = $(id);
    if (!el) return "";
    return String(el.value !== undefined && el.value !== null ? el.value : (el.textContent || "")).trim();
  }
  function captionDir() {
    var el = $("dataPath");
    var raw = el && el.value !== undefined ? el.value : W.__imageData;
    return String(raw || "").trim();
  }
  function captionLang() {
    var raw = captionField("captionLang").toLowerCase();
    if (raw === "zh" || raw === "cn" || raw.indexOf("中") >= 0) return "zh";
    if (raw === "en" || raw.indexOf("英") >= 0) return "en";
    return "zh";
  }
  function captionIntParam(qs, name, fallback, min, max) {
    var raw = qs && qs.get ? qs.get(name) : null;
    if (raw === null || raw === undefined || String(raw).trim() === "") return { value: fallback, clamped: false };
    var n = Number(String(raw).trim());
    if (!isFinite(n)) return { value: fallback, clamped: false };
    n = Math.round(n);
    var value = Math.min(max, Math.max(min, n));
    return { value: value, clamped: value !== n };
  }
  function captionSetUnavailable(reason, keepOptions) {
    CAPJOBS.available = false;
    CAPJOBS.backend = "";
    var btn = $("btnCaption"), run = $("captionRun"), sel = $("captionBackend");
    setDisabled(btn, true);
    setDisabled(run, true);
    if (sel) {
      sel.disabled = true;
      if (!keepOptions) {
        while (sel.firstChild) sel.removeChild(sel.firstChild);
      }
    }
    var msg = "未找到本地打标引擎（缺 llama-server 或权重）";
    text(btn, msg);
    captionResult(msg + (reason ? " · " + reason : ""));
  }
  function captionApplyBackends(j) {
    var top = (j && typeof j === "object" && !Array.isArray(j)) ? j : {};
    var list = Array.isArray(j) ? j : (top.backends || top.engines || top.items || []);
    if (!list.length && top.id) list = [top];
    var sel = $("captionBackend"), first = "";
    if (sel) {
      while (sel.firstChild) sel.removeChild(sel.firstChild);
    }
    for (var i = 0; i < list.length; i++) {
      var b = list[i] || {};
      var id = String(b.id || b.name || b.backend || "").trim();
      if (!id) continue;
      var available = b.available === true;
      var label = String(b.label || b.name || id);
      var reason = String(b.reason || "").trim();
      if (sel) {
        var opt = D.createElement("option");
        opt.value = id;
        opt.textContent = label + (available ? "" : " — " + (reason || "不可用"));
        opt.disabled = !available;
        opt.setAttribute("aria-disabled", available ? "false" : "true");
        if (reason) opt.title = reason;
        sel.appendChild(opt);
      }
      if (available && !first) first = id;
    }
    var topUnavailable = top.available === false || top.engine_available === false || top.ok === false;
    if (topUnavailable || !list.length || !first) {
      captionSetUnavailable(top.reason || (list.length ? "后端均不可用" : "核心未返回可用后端"), list.length > 0);
      return;
    }
    CAPJOBS.available = true;
    CAPJOBS.backend = first;
    if (sel) {
      sel.disabled = false;
      sel.value = first;
    }
    setDisabled($("btnCaption"), false);
    text($("btnCaption"), "自动打标");
    setDisabled($("captionRun"), false);
    captionResult("");
  }
  function loadCaptionBackends() {
    if (CAPJOBS.loading) return;
    CAPJOBS.loading = true;
    setDisabled($("btnCaption"), true);
    setDisabled($("captionRun"), true);
    captionResult("正在检测本地打标引擎…");
    api("/caption/backend").then(function (j) {
      CAPJOBS.loading = false;
      captionApplyBackends(j);
    }).catch(function (err) {
      CAPJOBS.loading = false;
      captionSetUnavailable(err && err.status === 404 ? "接口未就绪" : "核心未连接", false);
    });
  }
  function startCaptionJob() {
    if (CAPJOBS.busy) return;
    CAPJOBS.notice = "";
    var dir = captionDir();
    if (!dir) { captionResult("请先选择图片文件夹"); return; }
    var sel = $("captionBackend");
    var backend = String((sel && sel.value) || CAPJOBS.backend || "").trim();
    if (!CAPJOBS.available || !backend) {
      captionSetUnavailable("没有可用后端", false);
      return;
    }
    var qs = new W.URLSearchParams(SEARCH);
    var batch = captionIntParam(qs, "batch_size", 6, 1, 8);
    var maxChars = captionIntParam(qs, "max_chars", 40, 10, 200);
    var notices = [];
    if (batch.clamped) notices.push("已夹到 " + batch.value);
    if (maxChars.clamped) notices.push("已夹到 " + maxChars.value);
    CAPJOBS.notice = notices.join("；");
    var params = {
      dir: dir,
      backend: backend,
      trigger: captionField("captionTrigger"),
      prompt: captionField("captionPrompt"),
      lang: captionLang(),
      overwrite: !!($("captionOverwrite") && $("captionOverwrite").checked),
      batch_size: batch.value,
      max_chars: maxChars.value
    };
    CAPJOBS.busy = true;
    setDisabled($("captionRun"), true);
    captionResult("正在提交打标任务…");
    submitJob("caption", params, {
      onCreated: function () { captionResult("打标任务已提交…"); },
      onJob: function (job) {
        var m = (job && job.metrics) || {};
        var done = Number(m.done !== undefined ? m.done : 0);
        var total = Number(m.total !== undefined ? m.total : 0);
        if (total > 0) captionResult("已打标 " + done + "/" + total);
        else if (done > 0) captionResult("已打标 " + done);
      },
      onDone: function (job) {
        CAPJOBS.busy = false;
        setDisabled($("captionRun"), !CAPJOBS.available);
        var m = (job && job.metrics) || {}, msg = "";
        if (job && job.state === "succeeded") {
          var skipped = Number(m.skipped !== undefined ? m.skipped : 0);
          var written = null;
          if (m.written !== undefined && m.written !== null) written = Number(m.written);
          else if (m.images_total !== undefined && m.images_total !== null) written = Math.max(0, Number(m.images_total) - skipped);
          if (m.message !== undefined && m.message !== null && String(m.message).trim() !== "") {
            msg = String(m.message);
          } else {
            if (written !== null && isFinite(written)) msg = "已写 " + written + " 个 .txt（跳过 " + skipped + "）";
            else msg = "打标完成（后端未回传 written）";
            var truncated = Number(m.truncated !== undefined ? m.truncated : 0);
            if (truncated > 0) msg += "，其中 " + truncated + " 条超 40 字已截断";
          }
          captionResult(msg);
          if (written !== null && written > 0) {
            ssSet("ms.caption.done.dir", dir);
            capEditRefreshSummary();
          }
          clearCaptionWarning();
        } else if (job && job.state === "cancelled") {
          captionResult("已取消，已写的保留");
        } else if (job && job.state === "interrupted") {
          captionResult("已中断，已写的保留");
        } else {
          captionResult("打标失败：" + String((job && job.error) || m.error || m.message || "后端未返回错误"));
        }
      },
      onError: function (msg, err) {
        CAPJOBS.busy = false;
        setDisabled($("captionRun"), !CAPJOBS.available);
        var detail = (err && err.body && (err.body.error || err.body.reason)) || msg || "核心未连接";
        captionResult("打标失败：" + detail);
      }
    });
  }
  function initCaption() {
    var btn = $("btnCaption"), panel = $("captionPanel"), run = $("captionRun");
    if (!btn && !panel && !run) return;
    var langEl = $("captionLang");
    if (langEl && langEl.value === "en") langEl.value = "zh";
    if (run && !run.__mswireCaptionBound) {
      run.__mswireCaptionBound = true;
      run.addEventListener("click", function (e) {
        e.preventDefault(); e.stopImmediatePropagation();
        startCaptionJob();
      }, true);
    }
    loadCaptionBackends();
  }

  // ---------------- caption view/edit ----------------
  var CAPEDIT = { rows: [], dir: "", loading: false };
  function capEditMsg(value) {
    var el = $("captionEditMsg");
    if (el) text(el, value);
  }
  function capEditSummary(j) {
    var items = (j && j.items) || [];
    var count = Number(j && j.count !== undefined ? j.count : items.length);
    if (!isFinite(count)) count = items.length;
    var has = 0;
    for (var i = 0; i < items.length; i++) {
      var x = items[i] || {};
      if (x.has_caption === true || (x.has_caption === undefined && String(x.caption || "") !== "")) has++;
    }
    var missing = Math.max(0, count - has);
    var summary = count + " 张图 · " + has + " 个字幕 · " + missing + " 张缺失";
    var el = $("captionEditSummary");
    if (el) text(el, summary);
  }
  function capEditCounter(row) {
    var ta = row.__msCaptionText, num = row.__msCaptionNum;
    if (!ta || !num) return;
    var n = ta.value.length;
    text(num, n + " / 40");
    if (n > 40) row.classList.add("is-over");
    else row.classList.remove("is-over");
  }
  function capEditMissing(row, missing) {
    if (missing) row.classList.add("is-missing");
    else row.classList.remove("is-missing");
    if (row.__msCaptionText) row.__msCaptionText.placeholder = missing ? "（无字幕，输入后保存即创建）" : "";
  }
  function capEditPost(items) {
    var dir = CAPEDIT.dir || captionDir();
    return api("/dataset/captions", {
      method: "POST",
      body: { dir: dir, items: items, ext: ".txt" }
    });
  }
  function capEditSaveRow(row) {
    var ta = row.__msCaptionText;
    if (!ta) return;
    var value = ta.value;
    if (value === row.__msCaptionOriginal) return;
    capEditPost([{ name: row.__msCaptionName, caption: value }]).then(function (j) {
      if (!j || j.ok === false) throw new Error(coreFail(j, "核心拒绝保存"));
      row.__msCaptionOriginal = value;
      row.classList.add("is-saved");
      capEditMissing(row, value === "");
      capEditMsg(value === "" ? "已清空（删除 .txt）" : "已保存 1 条");
    }).catch(function (err) {
      var reason = (err && err.body && (err.body.error || err.body.reason)) || (err && err.message) || "核心未连接";
      capEditMsg("保存失败：" + reason);
    });
  }
  function capEditSaveAll() {
    var changed = [];
    for (var i = 0; i < CAPEDIT.rows.length; i++) {
      var row = CAPEDIT.rows[i], ta = row.__msCaptionText;
      if (!ta || ta.value === row.__msCaptionOriginal) continue;
      changed.push({ row: row, value: ta.value });
    }
    if (!changed.length) { capEditMsg("没有改动"); return; }
    var items = [];
    for (var k = 0; k < changed.length; k++) {
      items.push({ name: changed[k].row.__msCaptionName, caption: changed[k].value });
    }
    capEditPost(items).then(function (j) {
      if (!j || j.ok === false) throw new Error(coreFail(j, "核心拒绝保存"));
      var errors = Array.isArray(j.errors) ? j.errors : [];
      var written = Number(j.written !== undefined ? j.written : changed.length);
      if (!isFinite(written)) written = 0;
      var deleted = Number(j.deleted !== undefined ? j.deleted : 0);
      if (!isFinite(deleted)) deleted = 0;
      for (var x = 0; x < changed.length; x++) {
        var r = changed[x].row;
        r.__msCaptionOriginal = changed[x].value;
        r.classList.add("is-saved");
        capEditMissing(r, changed[x].value === "");
      }
      capEditMsg("已保存 " + written + " 条，删除 " + deleted + " 条，失败 " + errors.length + " 条");
    }).catch(function (err) {
      var reason = (err && err.body && (err.body.error || err.body.reason)) || (err && err.message) || "核心未连接";
      capEditMsg("保存失败：" + reason);
    });
  }
  function capEditRender(j) {
    var items = (j && j.items) || [];
    var list = $("captionEditList");
    if (!list) return;
    list.textContent = "";
    CAPEDIT.rows = [];
    capEditSummary(j);
    for (var i = 0; i < items.length; i++) {
      var x = items[i] || {};
      var name = String(x.name || "");
      var caption = String(x.caption || "");
      var has = x.has_caption === true || (x.has_caption === undefined && caption !== "");
      var row = D.createElement("div");
      row.className = "caprow";
      row.setAttribute("data-name", name);
      var nameEl = D.createElement("span");
      nameEl.className = "caprow__name";
      text(nameEl, name);
      var ta = D.createElement("textarea");
      ta.className = "caprow__text";
      ta.value = caption;
      var num = D.createElement("span");
      num.className = "caprow__num";
      row.appendChild(nameEl);
      row.appendChild(ta);
      row.appendChild(num);
      row.__msCaptionName = name;
      row.__msCaptionText = ta;
      row.__msCaptionNum = num;
      row.__msCaptionOriginal = caption;
      capEditMissing(row, !has);
      capEditCounter(row);
      (function (row, ta) {
        ta.addEventListener("input", function () {
          row.classList.remove("is-saved");
          capEditCounter(row);
        });
        ta.addEventListener("blur", function () { capEditSaveRow(row); });
      })(row, ta);
      list.appendChild(row);
      CAPEDIT.rows.push(row);
    }
  }
  function capEditLoad() {
    var panel = $("captionEditPanel");
    var dir = captionDir();
    if (panel) panel.hidden = false;
    if (!dir) { capEditMsg("请先选择图片文件夹"); return; }
    CAPEDIT.dir = dir;
    capEditMsg("");
    api("/dataset/captions?dir=" + encodeURIComponent(dir)).then(function (j) {
      if (!j || j.ok === false) throw new Error(coreFail(j, "核心拒绝读取"));
      capEditRender(j);
    }).catch(function (err) {
      var reason = (err && err.body && (err.body.error || err.body.reason)) || (err && err.message) || "核心未连接";
      capEditMsg("读取失败：" + reason);
    });
  }
  function capEditRefreshSummary() {
    var summary = $("captionEditSummary");
    if (!summary) return;
    var dir = captionDir();
    if (!dir) return;
    api("/dataset/captions?dir=" + encodeURIComponent(dir)).then(function (j) {
      if (j && j.ok !== false) capEditSummary(j);
    }).catch(function () {});
  }
  function initCaptionEdit() {
    var btn = $("btnCaptionEdit"), panel = $("captionEditPanel"), saveAll = $("captionEditSaveAll");
    if (!btn && !panel && !saveAll) return;
    if (btn && !btn.__mswireCaptionEditBound) {
      btn.__mswireCaptionEditBound = true;
      btn.addEventListener("click", function (e) {
        e.preventDefault(); e.stopImmediatePropagation();
        capEditLoad();
      }, true);
    }
    if (saveAll && !saveAll.__mswireCaptionEditBound) {
      saveAll.__mswireCaptionEditBound = true;
      saveAll.addEventListener("click", function (e) {
        e.preventDefault(); e.stopImmediatePropagation();
        capEditSaveAll();
      }, true);
    }
  }

  function initImageData() {
    envDoctor();
    initDataHint($("dataPath"), D.querySelector(".body .sub"));
    initCaption();
    initCaptionEdit();
    var btn = $("browseBtn"), folder = $("folderPick"), input = $("dataPath");
    if (btn) btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      pickPath("dir", folder, function (p) {
        if (input) { input.value = p; input.dispatchEvent(new W.Event("input", { bubbles: true })); }
      });
    }, true);
    var dz = D.querySelector("#stepCenter .dropzone, .body .dropzone, .body .row[data-pick]");
    if (dz) dz.addEventListener("click", function (e) {
      if (e.target.closest("button,a,input,textarea,label,select")) return;
      e.preventDefault(); e.stopImmediatePropagation();
      pickPath("dir", folder, function (p) {
        if (input) { input.value = p; input.dispatchEvent(new W.Event("input", { bubbles: true })); }
      });
    }, true);
  }
  function pickDataPath(fallbackInput) {
    return api("/pick", { method: "POST", body: { mode: "file", filter: ["jsonl", "csv"] } }).then(function (j) {
      if (!j || j.ok === false || !j.path) {
        if (j && j.cancelled) text($("dec2Meta"), "已取消选择");
        else if (j && j.reason === "no_desktop") text($("dec2Meta"), "核心无法弹对话框（no_desktop），请用示例数据");
        else text($("dec2Meta"), "选择文件失败：" + coreFail(j, "核心未返回路径"));
        return;
      }
      var p = j.path;
      DEC.dataPath = String(p);
      if ($("dec2Name")) text($("dec2Name"), basename(p));
      if ($("dec2Meta")) text($("dec2Meta"), p);
      if ($("dec2PickCard")) show($("dec2PickCard"));
      if ($("dec2Dz")) $("dec2Dz").style.display = "none";
      if ($("dec2Next")) $("dec2Next").removeAttribute("aria-disabled");
      applyDataHint(String(p));
      return p;
    }).catch(function (err) {
      if (err && err.status === 404) {
        text($("dec2Meta"), "文件选择接口未就绪（核心在线后可用）；浏览器只能拿到文件名");
        if (fallbackInput) fallbackInput.click();
      } else {
        text($("dec2Meta"), "核心未连接，无法取绝对路径");
      }
    });
  }
  function initHome() {
    envDoctor();
    // F30 (decision): map the first scanned laya model onto the existing first .opt
    api("/models?kind=laya").then(function (j) {
      var m = (j && j.models && j.models[0]) || null;
      if (!m) return;
      DEC.layaModelPath = m.path || "";
      var opt = D.querySelector("#dec1Opts .opt");
      if (!opt) return;
      opt.setAttribute("data-path", m.path || "");
      opt.setAttribute("data-state", "ready");
      var n = opt.querySelector(".opt__name");
      if (n) text(n, m.name || m.id || "Laya 模型");
      var meta = opt.querySelector(".opt__meta");
      if (meta) text(meta, "本机扫描：" + (m.path || m.id || ""));
    }).catch(function () {});
    // F19/F21 + pick + download + eval/report + continued pages
    var pickBtn = $("dec2Pick"), dz = $("dec2Dz");
    if (pickBtn) pickBtn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      pickDataPath($("dec2File"));
    }, true);
    if (dz) dz.addEventListener("click", function (e) {
      if (e.target.closest("button,a")) return;
      e.preventDefault(); e.stopImmediatePropagation();
      pickDataPath($("dec2File"));
    }, true);
    var fileInput = $("dec2File");
    if (fileInput) fileInput.addEventListener("change", function () {
      DEC.dataPath = "";
      if ($("dec2Meta")) text($("dec2Meta"), "浏览器拿不到绝对路径，需核心在线（请用「选择文件」按钮走核心）");
    });
    var sample = $("dec2Sample");
    if (sample) sample.addEventListener("click", function () {
      DEC.dataPath = SAMPLE_DATA;
    });
    initDownloadPanel();
    // F21: running job guard + F20 backfill on home
    api("/jobs?state=running").then(function (j) {
      var jobs = (j && j.jobs) || (Array.isArray(j) ? j : (j && j.items) || []);
      if (jobs.length) {
        DEC.runningGuard = true;
        var runJob = jobs[0] || {};
        var stateEl = D.querySelector(".card[data-flow='decision'] .state");
        if (stateEl) text(stateEl, "训练中 · " + (runJob.kind || "laya"));
        var note = $("dec2LangNote");
        if (note) { note.hidden = false; text(note, "已有作业在跑：请等它结束再提交（核心一次只跑一个）。"); }
        DEC.runningDisabled = [];
        var btns = startButtons();
        for (var rb = 0; rb < btns.length; rb++) {
          if (btns[rb].getAttribute("aria-disabled") !== "true") {
            DEC.runningDisabled.push(btns[rb]);
            btns[rb].setAttribute("aria-disabled", "true");
          }
        }
        var rid = runJob.id || runJob.job_id;
        if (rid) {
          var watch = W.setInterval(function () {
            api("/jobs/" + encodeURIComponent(rid)).then(function (r) {
              var job = (r && r.job) || r || {};
              var st = job.state || "";
              if (st === "succeeded" || st === "failed" || st === "cancelled" || st === "interrupted") {
                W.clearInterval(watch);
                DEC.runningGuard = false;
                var el2 = D.querySelector(".card[data-flow='decision'] .state");
                if (el2) text(el2, st === "succeeded" ? "已完成 · " + (job.kind || "laya") : (st === "cancelled" ? "已取消" : "失败"));
                if (DEC.runningDisabled) {
                  for (var x = 0; x < DEC.runningDisabled.length; x++) DEC.runningDisabled[x].removeAttribute("aria-disabled");
                  DEC.runningDisabled = [];
                }
              }
            }).catch(function () {});
          }, 2000);
        }
      }
    }).catch(function () {});
    var btn = $("dec2Next");
    if (btn) btn.addEventListener("click", function (e) {
      if (btn.getAttribute("aria-disabled") === "true") return;
      if (DEC.runningGuard) {
        e.preventDefault(); e.stopImmediatePropagation();
        text($("dec2Meta"), "已有作业在跑，暂不能提交（核心一次只跑一个）。");
        return;
      }
      var r = decisionParams("train");
      if (r.error) {
        e.preventDefault(); e.stopImmediatePropagation();
        text($("dec2Meta"), r.error);
        return;
      }
      DEC.logs = [];
      DEC.pending = submitJob("laya", r.params, {
        onCreated: function (job) { DEC.job = job; DEC.pending = null; if (DEC.started) decisionStart(); },
        onHistory: function (h) {
          var arr = (h && (h.rows || h.points || h.events || h.history)) || [];
          for (var i = 0; i < arr.length; i++) {
            var e = arr[i], d = e.data || e;
            var loss = d.loss !== undefined ? d.loss : e.loss;
            var st = d.micro_step !== undefined ? d.micro_step : (d.step !== undefined ? d.step : e.step);
            if (loss !== undefined && st !== undefined) DEC.logs.push({ i: Number(st), v: Number(loss) });
          }
          if (DEC.logs.length) drawDecisionChart();
        },
        onError: function (msg, err) { text($("dec2Meta"), msg + (err && err.status ? " (HTTP " + err.status + ")" : "") + (err && err.message ? " · " + err.message : "")); }
      });
    }, true);
    // replace page demo entry: do not call the demo start
    W.__startRun = function (name, mode) {
      if (name) W.__decDataName = name;
      W.__runMode = mode || "train";
      if (!DEC.pending && !DEC.job) return;
      decisionStart();
    };
    // dec3 cancel
    var cancel = $("dec3Cancel");
    if (cancel) cancel.addEventListener("click", function (e) {
      e.preventDefault();
      cancelJob({ onCancelSent: function () { appendLog($("dec3Log"), "已发送取消请求…"); } });
    }, true);
    bindOpenFolder($("dec3Out"), function () { return $("dec3Out") ? $("dec3Out").textContent : ""; }, $("dec3Log"));
    // dec4 run/report/export
    var run = $("dec4Run"), ai = $("dec4Ai"), exp = $("dec4Export");
    if (run) run.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      text($("dec4Steps"), "正在请求核心评测…");
      api("/jobs/" + encodeURIComponent(DEC.job ? DEC.job.job_id : "") + "/eval", { method: "POST", body: { data: "", limit: 96, with_predictions: true } }).then(function (j) {
        var ev = j || {};
        if (ev.ok === false) {
          text($("dec4Steps"), "评测失败：" + coreFail(ev));
          $("dec4Steps").title = coreFail(ev);
          hide($("dec4Result"));
          return;
        }
        if (ev.output_dir_source === "params" && W.console && W.console.log) W.console.log("[ms-wire] eval output_dir_source=params（不显示在 UI）");
        var m = ev.metrics || {};
        text($("evAcc"), m.accuracy !== undefined ? Number(m.accuracy).toFixed(2) : "—");
        text($("evLoss"), m.loss !== undefined ? Number(m.loss).toFixed(2) : "—");
        text($("evEce"), m.ece !== undefined ? Number(m.ece).toFixed(2) : "—");
        text($("evAbs"), m.abstain_rate !== undefined ? (Number(m.abstain_rate) * 100).toFixed(1) + "%" : "—");
        var tbl = $("dec4Tbl");
        if (tbl) {
          var h = '<tr><th>题目（截断）</th><th>模型答案</th><th>标注</th><th>一致</th><th>置信度</th></tr>';
          var rows = ev.per_question || [];
          for (var i = 0; i < rows.length; i++) {
            h += '<tr><td>' + String(rows[i].q || "").slice(0, 40) + '</td><td>' + String(rows[i].answer || "") + '</td><td>' + String(rows[i].expected || "") + '</td><td class="' + (rows[i].match ? "ok" : "bad") + '">' + (rows[i].match ? "✓" : "✗") + '</td><td>' + (rows[i].confidence !== undefined ? Number(rows[i].confidence).toFixed(2) : "—") + '</td></tr>';
          }
          if (!rows.length) h += '<tr><td colspan="5">本次训练未落逐条预测（' + String(ev.note || "核心未返回 note") + '）</td></tr>';
          tbl.innerHTML = h;
        }
        text($("dec4Steps"), "评测完成 · " + (ev.data_source || "calibration"));
        show($("dec4Result"));
      }).catch(function (err) {
        text($("dec4Steps"), err && err.status === 404 ? "评测接口未就绪（核心尚未实装 /eval）" : "核心未连接");
        hide($("dec4Result"));
      });
    }, true);
    if (ai) ai.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      text($("dec4Steps"), "正在请求核心报告…");
      api("/jobs/" + encodeURIComponent(DEC.job ? DEC.job.job_id : "") + "/report", { method: "POST", body: { language: "zh" } }).then(function (j) {
        if (!j || j.ok === false) {
          text($("dec4Steps"), "报告生成失败：" + coreFail(j));
          $("dec4Steps").title = coreFail(j);
          hide($("dec4Report"));
          if (exp) hide(exp);
          return;
        }
        text($("dec4Report"), j.markdown || "核心未返回报告正文");
        show($("dec4Report"));
        text($("dec4Steps"), "报告已生成 · engine=" + (j.engine || "—"));
        if (exp) show(exp);
      }).catch(function (err) {
        text($("dec4Steps"), err && err.status === 404 ? "报告接口未就绪（核心尚未实装 /report）" : "核心未连接");
        hide($("dec4Report"));
        if (exp) hide(exp);
      });
    }, true);
    if (exp) exp.addEventListener("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation();
      var md = ($("dec4Report") && $("dec4Report").textContent) || "";
      if (!md) { text($("dec4Steps"), "没有可导出的报告"); return; }
      var blob = new W.Blob([md], { type: "text/markdown;charset=utf-8" });
      var a = D.createElement("a"); a.href = W.URL.createObjectURL(blob); a.download = "laya-eval-report.md";
      D.body.appendChild(a); a.click();
    }, true);
    // dec5 continued: submit when start clicked (page will switch to dec3 / call __startRun)
    var run5 = $("dec5Run");
    if (run5) run5.addEventListener("click", function (e) {
      var r = decisionParams("retrain");
      if (r.error) {
        e.preventDefault(); e.stopImmediatePropagation();
        var msgEl = $("dec5FileInfo") || $("dec5Ep") || $("dec3Log");
        if (msgEl) { text(msgEl, r.error); msgEl.title = r.error; }
        return;
      }
      DEC.logs = [];
      DEC.pending = submitJob("laya", r.params, {
        onCreated: function (job) { DEC.job = job; DEC.pending = null; if (DEC.started) decisionStart(); },
        onError: function (msg) { text($("dec3Log"), msg); }
      });
    }, true);
  }

  // ---------------- dispatch ----------------
  var PAGES = {
    home: initHome,
    "llm-model": initLlmModel,
    "llm-data": initLlmData,
    "llm-train": initLlmTrain,
    "image-model": initImageModel,
    "image-data": initImageData,
    "image-params": initImageParams,
    "image-train": initImageTrain
  };
  var MSW = {
    version: "1.1.0",
    page: PAGE,
    api: API,
    active: true,
    booted: false,
    engine: { submit: submitJob, poll: pollJob, cancel: cancelJob },
    frozen: function () { return !!JOB.frozen; }
  };
  W.MSWire = MSW;
  function boot() {
    try {
      bindEnvPanel();
      W.addEventListener("pagehide", function () { stopPoll(); stopVramPoll(); setTrainingGlow(false); });
      log("active page=" + PAGE);
      if (PAGES[PAGE]) PAGES[PAGE]();
      if (PAGE !== "home") envDoctor();
      MSW.booted = true;
      W.__MSWIRE_BOOTED__ = true;
      startHeartbeat();
    } catch (e) {
      if (W.console && W.console.warn) W.console.warn("[ms-wire] boot failed", e);
    }
  }
  if (D.readyState === "loading") D.addEventListener("DOMContentLoaded", boot); else boot();
})();
