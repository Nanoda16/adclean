/* ==========================================================================
   Adclean Micro-Motion System (inspired by holynova/web-motion-showcase)
   克制、出版校对室质感微动效，全量支持 prefers-reduced-motion: reduce
   ========================================================================== */
(function (root) {
  "use strict";

  function prefersReducedMotion() {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /**
   * 1. scroll-progress：页面顶部 2px 墨黑细进度条
   *    任务运行态可传入 overrideRatio (0..1)；null 时回归平滑响应页面滚动进度。
   */
  function initScrollProgress(barEl) {
    var overrideRatio = null;
    var rafId = 0;

    function computeScrollRatio() {
      if (typeof document === "undefined") return 0;
      var doc = document.documentElement;
      var body = document.body;
      var scrollTop = (window.pageYOffset || doc.scrollTop || (body && body.scrollTop) || 0);
      var scrollHeight = Math.max(doc.scrollHeight || 0, (body && body.scrollHeight) || 0);
      var clientHeight = doc.clientHeight || window.innerHeight || 1;
      var maxScroll = scrollHeight - clientHeight;
      if (maxScroll <= 0) return 0;
      return Math.max(0, Math.min(1, scrollTop / maxScroll));
    }

    function paint() {
      rafId = 0;
      if (!barEl) return;
      var ratio = overrideRatio !== null ? overrideRatio : computeScrollRatio();
      ratio = Math.max(0, Math.min(1, Number(ratio) || 0));
      barEl.style.transform = "scaleX(" + ratio.toFixed(4) + ")";
    }

    function schedulePaint() {
      if (!rafId && typeof window !== "undefined" && window.requestAnimationFrame) {
        rafId = window.requestAnimationFrame(paint);
      } else if (!rafId) {
        paint();
      }
    }

    if (typeof window !== "undefined") {
      window.addEventListener("scroll", schedulePaint, { passive: true });
      window.addEventListener("resize", schedulePaint, { passive: true });
    }
    paint();

    return {
      setOverride: function (ratio, state) {
        overrideRatio = ratio === null || ratio === undefined ? null : Math.max(0, Math.min(1, ratio));
        if (barEl) {
          barEl.classList.toggle("is-cancelled", state === "cancelled");
          barEl.classList.toggle("is-error", state === "error");
        }
        schedulePaint();
      },
      refresh: schedulePaint
    };
  }

  /**
   * 2. underline-reveal：为指定元素集合补充规范 class，悬停自左向右展开下划线
   */
  function initUnderlineReveal(rootEl) {
    var scope = rootEl || (typeof document !== "undefined" ? document : null);
    if (!scope) return;
    var targets = scope.querySelectorAll("[data-underline-reveal]");
    for (var i = 0; i < targets.length; i++) {
      targets[i].classList.add("underline-reveal");
    }
  }

  /**
   * 3. sliding-indicator-tabs：物理平滑滑动底线指示器
   */
  function createSlidingTabs(containerEl, onChange) {
    if (!containerEl) {
      return { setActive: function () {}, refresh: function () {}, getActive: function () { return ""; } };
    }
    var indicator = containerEl.querySelector(".sliding-tabs__indicator");
    var buttons = [].slice.call(containerEl.querySelectorAll("[data-tab]"));
    var activeVal = "";

    function moveIndicator(btn) {
      if (!indicator || !btn) return;
      var left = btn.offsetLeft;
      var width = btn.offsetWidth;
      indicator.style.width = width + "px";
      indicator.style.transform = "translateX(" + left + "px)";
    }

    function setActive(val, silent) {
      var targetBtn = null;
      for (var i = 0; i < buttons.length; i++) {
        var b = buttons[i];
        var match = b.getAttribute("data-tab") === val;
        b.classList.toggle("is-active", match);
        b.setAttribute("aria-selected", match ? "true" : "false");
        if (match) targetBtn = b;
      }
      if (!targetBtn && buttons.length) {
        targetBtn = buttons[0];
        targetBtn.classList.add("is-active");
        targetBtn.setAttribute("aria-selected", "true");
        val = targetBtn.getAttribute("data-tab") || "";
      }
      activeVal = val;
      if (targetBtn) {
        if (typeof window !== "undefined" && window.requestAnimationFrame) {
          window.requestAnimationFrame(function () { moveIndicator(targetBtn); });
        } else {
          moveIndicator(targetBtn);
        }
      }
      if (!silent && typeof onChange === "function") {
        onChange(val);
      }
    }

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var val = btn.getAttribute("data-tab") || "";
        if (val !== activeVal) {
          setActive(val, false);
        }
      });
    });

    if (typeof window !== "undefined") {
      window.addEventListener("resize", function () {
        var cur = containerEl.querySelector("[data-tab].is-active");
        if (cur) moveIndicator(cur);
      });
    }

    var initial = containerEl.querySelector("[data-tab].is-active") || buttons[0];
    if (initial) {
      setActive(initial.getAttribute("data-tab") || "", true);
    }

    return {
      setActive: setActive,
      refresh: function () {
        var cur = containerEl.querySelector("[data-tab].is-active");
        if (cur) moveIndicator(cur);
      },
      getActive: function () { return activeVal; }
    };
  }

  /**
   * 4. rolling-number-odometer：KPI 数字平滑滚动落位
   */
  var odometerTokens = typeof WeakMap !== "undefined" ? new WeakMap() : null;

  function formatNumber(val, decimals, useGrouping, suffix) {
    var num = Number(val);
    if (!isFinite(num)) return "—";
    var fixed = decimals > 0 ? num.toFixed(decimals) : String(Math.round(num));
    if (useGrouping) {
      var parts = fixed.split(".");
      parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      fixed = parts.join(".");
    }
    return suffix ? fixed + suffix : fixed;
  }

  function animateOdometer(el, targetValue, options) {
    if (!el) return;
    var opts = options || {};
    var decimals = Number(opts.decimals || 0);
    var useGrouping = opts.grouping !== false;
    var suffix = opts.suffix || "";
    var duration = Number(opts.duration || 420);

    var numTarget = Number(targetValue);
    if (!isFinite(numTarget)) {
      el.textContent = "—";
      el.dataset.odoVal = "";
      return;
    }

    var prevRaw = el.dataset.odoVal;
    var startVal = prevRaw !== undefined && prevRaw !== "" && isFinite(Number(prevRaw))
      ? Number(prevRaw)
      : 0;
    el.dataset.odoVal = String(numTarget);

    if (prefersReducedMotion() || typeof window === "undefined" || !window.requestAnimationFrame || startVal === numTarget) {
      el.textContent = formatNumber(numTarget, decimals, useGrouping, suffix);
      return;
    }

    var token = (odometerTokens && odometerTokens.get(el) || 0) + 1;
    if (odometerTokens) odometerTokens.set(el, token);

    var finished = false;
    function commitFinal() {
      if (finished) return;
      if (odometerTokens && odometerTokens.get(el) !== token) return;
      finished = true;
      el.textContent = formatNumber(numTarget, decimals, useGrouping, suffix);
    }

    var startTs = null;
    function step(ts) {
      if (finished || (odometerTokens && odometerTokens.get(el) !== token)) return;
      if (startTs === null) startTs = ts;
      var p = Math.min(1, (ts - startTs) / duration);
      // easeOutCubic
      var eased = 1 - Math.pow(1 - p, 3);
      var current = startVal + (numTarget - startVal) * eased;
      el.textContent = formatNumber(current, decimals, useGrouping, suffix);
      if (p < 1) {
        window.requestAnimationFrame(step);
      } else {
        commitFinal();
      }
    }
    window.requestAnimationFrame(step);
    setTimeout(commitFinal, duration + 80);
  }

  /**
   * 5. line-reveal & fade-in-up：网格线展开与卡片轻盈错峰淡入上移
   */
  function triggerLineReveal(rootEl) {
    if (prefersReducedMotion() || !rootEl) return;
    var lines = rootEl.querySelectorAll("[data-line-reveal]");
    for (var i = 0; i < lines.length; i++) {
      lines[i].classList.remove("line-reveal");
      void lines[i].offsetWidth;
      lines[i].classList.add("line-reveal");
    }
  }

  function staggerFadeInUp(elements, stepMs) {
    if (!elements || !elements.length) return;
    var step = Number(stepMs || 45);
    var reduced = prefersReducedMotion();
    for (var i = 0; i < elements.length; i++) {
      var el = elements[i];
      if (!el) continue;
      if (reduced) {
        el.classList.remove("fade-in-up");
        el.style.animationDelay = "";
        continue;
      }
      el.classList.remove("fade-in-up");
      el.style.animationDelay = (i * step) + "ms";
      void el.offsetWidth;
      el.classList.add("fade-in-up");
    }
  }

  /**
   * 6. spring-drawer：右侧弹簧抽屉（带轻遮罩）
   */
  function createSpringDrawer(drawerEl, backdropEl, options) {
    var opts = options || {};
    var opened = false;

    function open() {
      if (!drawerEl) return;
      opened = true;
      drawerEl.classList.add("is-open");
      drawerEl.setAttribute("aria-hidden", "false");
      if (backdropEl) {
        backdropEl.classList.add("is-open");
      }
      if (typeof opts.onOpen === "function") opts.onOpen();
    }

    function close() {
      if (!drawerEl || !opened) return;
      opened = false;
      drawerEl.classList.remove("is-open");
      drawerEl.setAttribute("aria-hidden", "true");
      if (backdropEl) {
        backdropEl.classList.remove("is-open");
      }
      if (typeof opts.onClose === "function") opts.onClose();
    }

    if (backdropEl) {
      backdropEl.addEventListener("click", close);
    }

    return {
      open: open,
      close: close,
      isOpen: function () { return opened; }
    };
  }

  var api = {
    prefersReducedMotion: prefersReducedMotion,
    initScrollProgress: initScrollProgress,
    initUnderlineReveal: initUnderlineReveal,
    createSlidingTabs: createSlidingTabs,
    animateOdometer: animateOdometer,
    triggerLineReveal: triggerLineReveal,
    staggerFadeInUp: staggerFadeInUp,
    createSpringDrawer: createSpringDrawer
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.AdcleanMotion = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
