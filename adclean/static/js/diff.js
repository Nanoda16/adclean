/* ==========================================================================
   Adclean Pure-DOM Character-Level Diff & Invisible Token Renderer
   100% 纯 DOM 构建（createElement / createTextNode / textContent），零 innerHTML
   ========================================================================== */
(function (root) {
  "use strict";

  var INVIS_LABEL = {
    "\u200b": "[零宽空格]",
    "\u200c": "[零宽不连]",
    "\u200d": "[零宽连接]",
    "\u200e": "[左到右标记]",
    "\u200f": "[右到左标记]",
    "\u202a": "[嵌入LRE]",
    "\u202b": "[嵌入RLE]",
    "\u202c": "[结束嵌入]",
    "\u2060": "[词连接符]",
    "\u2061": "[函数应用]",
    "\u2062": "[隐形乘号]",
    "\u2063": "[隐形分隔]",
    "\u2064": "[隐形加号]",
    "\ufeff": "[BOM]",
    "\u00ad": "[软连字符]",
    "\u180e": "[蒙元音分隔]",
    "\u3000": "[全角空格]",
    "\u00a0": "[不换行空格]"
  };

  function labelForChar(ch) {
    if (!ch) return "";
    var label = INVIS_LABEL[ch];
    if (label) return label;
    var cp = ch.codePointAt(0);
    if (cp === 9 || cp === 10 || cp === 13) return "[空白]";
    if (cp < 0x20 || (cp >= 0x7f && cp <= 0x9f)) {
      return "[控制 U+" + cp.toString(16).toUpperCase().padStart(4, "0") + "]";
    }
    return "";
  }

  /**
   * 把字符串拆分为可见片段序列，并同步产出纯文本可读串 plain。
   */
  function visibleText(input) {
    var s = String(input == null ? "" : input);
    var tokens = [];
    var plain = "";
    var textBuf = "";

    for (var _i = 0, chars = Array.from(s); _i < chars.length; _i++) {
      var ch = chars[_i];
      var lbl = labelForChar(ch);
      if (lbl) {
        if (textBuf) {
          tokens.push({ invis: false, text: textBuf });
          textBuf = "";
        }
        tokens.push({ invis: true, text: lbl });
        plain += lbl;
      } else {
        textBuf += ch;
        plain += ch;
      }
    }
    if (textBuf) {
      tokens.push({ invis: false, text: textBuf });
    }
    return { plain: plain, tokens: tokens };
  }

  /**
   * 安全向 DOM 容器追加可见文本（不可见字符渲染为 .invis-token 徽标）。
   */
  function appendVisibleTextNodes(parentEl, input, doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d || !parentEl) return;
    var parsed = visibleText(input);
    for (var i = 0; i < parsed.tokens.length; i++) {
      var tk = parsed.tokens[i];
      if (tk.invis) {
        var span = d.createElement("span");
        span.className = "invis-token";
        span.textContent = tk.text;
        parentEl.appendChild(span);
      } else {
        parentEl.appendChild(d.createTextNode(tk.text));
      }
    }
  }

  function pushOp(list, op, text) {
    if (!text) return;
    if (list.length && list[list.length - 1].op === op) {
      list[list.length - 1].text += text;
    } else {
      list.push({ op: op, text: text });
    }
  }

  /**
   * 字符级 Diff（先剥公共前后缀，核心区间走 LCS；超长行自动降级防卡死）。
   */
  function computeCharDiff(beforeStr, afterStr) {
    var aChars = Array.from(String(beforeStr == null ? "" : beforeStr));
    var bChars = Array.from(String(afterStr == null ? "" : afterStr));
    var aLen = aChars.length;
    var bLen = bChars.length;

    if (aLen === 0 && bLen === 0) return [];
    if (aLen === 0) return [{ op: "ins", text: bChars.join("") }];
    if (bLen === 0) return [{ op: "del", text: aChars.join("") }];

    var prefix = 0;
    while (prefix < aLen && prefix < bLen && aChars[prefix] === bChars[prefix]) {
      prefix++;
    }
    var suffix = 0;
    while (
      suffix < (aLen - prefix) &&
      suffix < (bLen - prefix) &&
      aChars[aLen - 1 - suffix] === bChars[bLen - 1 - suffix]
    ) {
      suffix++;
    }

    var ops = [];
    if (prefix > 0) {
      pushOp(ops, "eq", aChars.slice(0, prefix).join(""));
    }

    var midA = aChars.slice(prefix, aLen - suffix);
    var midB = bChars.slice(prefix, bLen - suffix);
    var m = midA.length;
    var n = midB.length;

    if (m === 0 && n > 0) {
      pushOp(ops, "ins", midB.join(""));
    } else if (n === 0 && m > 0) {
      pushOp(ops, "del", midA.join(""));
    } else if (m > 0 && n > 0) {
      // 超长未对齐块保护阈值：防止 O(m*n) 卡顿
      if (m * n > 140000) {
        pushOp(ops, "del", midA.join(""));
        pushOp(ops, "ins", midB.join(""));
      } else {
        var stride = n + 1;
        var dp = new Int32Array((m + 1) * stride);
        for (var i = m - 1; i >= 0; i--) {
          var rowOff = i * stride;
          var nextOff = (i + 1) * stride;
          for (var j = n - 1; j >= 0; j--) {
            if (midA[i] === midB[j]) {
              dp[rowOff + j] = dp[nextOff + j + 1] + 1;
            } else {
              var down = dp[nextOff + j];
              var right = dp[rowOff + j + 1];
              dp[rowOff + j] = down >= right ? down : right;
            }
          }
        }
        var x = 0, y = 0;
        while (x < m && y < n) {
          if (midA[x] === midB[y]) {
            pushOp(ops, "eq", midA[x]);
            x++;
            y++;
          } else if (dp[(x + 1) * stride + y] >= dp[x * stride + (y + 1)]) {
            pushOp(ops, "del", midA[x]);
            x++;
          } else {
            pushOp(ops, "ins", midB[y]);
            y++;
          }
        }
        while (x < m) {
          pushOp(ops, "del", midA[x++]);
        }
        while (y < n) {
          pushOp(ops, "ins", midB[y++]);
        }
      }
    }

    if (suffix > 0) {
      pushOp(ops, "eq", aChars.slice(aLen - suffix).join(""));
    }
    return ops;
  }

  /**
   * 行内红绿字符级高亮渲染（纯 DOM 构建，零 innerHTML）。
   */
  function renderInlineDiff(containerEl, beforeStr, afterStr, options) {
    if (!containerEl) return;
    var doc = containerEl.ownerDocument || document;
    var opts = options || {};
    var showPlaceholderWhenEmpty = opts.emptyLabel !== undefined ? opts.emptyLabel : "（整行删除）";
    containerEl.textContent = "";

    var ops = computeCharDiff(beforeStr, afterStr);
    if (!ops.length) {
      var emptySpan = doc.createElement("span");
      emptySpan.className = "eyebrow";
      emptySpan.textContent = "（空行）";
      containerEl.appendChild(emptySpan);
      return;
    }

    for (var i = 0; i < ops.length; i++) {
      var seg = ops[i];
      if (seg.op === "eq") {
        appendVisibleTextNodes(containerEl, seg.text, doc);
      } else if (seg.op === "del") {
        var delEl = doc.createElement("del");
        delEl.className = "diff-del";
        appendVisibleTextNodes(delEl, seg.text, doc);
        containerEl.appendChild(delEl);
      } else if (seg.op === "ins") {
        var insEl = doc.createElement("ins");
        insEl.className = "diff-ins";
        appendVisibleTextNodes(insEl, seg.text, doc);
        containerEl.appendChild(insEl);
      }
    }

    if (afterStr === "" && beforeStr !== "" && showPlaceholderWhenEmpty) {
      var tag = doc.createElement("span");
      tag.className = "pill-tag pill-tag--red";
      tag.style.marginLeft = "8px";
      tag.textContent = showPlaceholderWhenEmpty;
      containerEl.appendChild(tag);
    }
  }

  /**
   * 双栏字符级对比渲染（左栏原稿标红删去段，右栏终稿标绿新增段）。
   */
  function renderSplitDiff(beforeEl, afterEl, beforeStr, afterStr) {
    var doc = (beforeEl && beforeEl.ownerDocument) || (afterEl && afterEl.ownerDocument) || document;
    if (beforeEl) beforeEl.textContent = "";
    if (afterEl) afterEl.textContent = "";

    var ops = computeCharDiff(beforeStr, afterStr);
    for (var i = 0; i < ops.length; i++) {
      var seg = ops[i];
      if (seg.op === "eq") {
        if (beforeEl) appendVisibleTextNodes(beforeEl, seg.text, doc);
        if (afterEl) appendVisibleTextNodes(afterEl, seg.text, doc);
      } else if (seg.op === "del" && beforeEl) {
        var delEl = doc.createElement("del");
        delEl.className = "diff-del";
        appendVisibleTextNodes(delEl, seg.text, doc);
        beforeEl.appendChild(delEl);
      } else if (seg.op === "ins" && afterEl) {
        var insEl = doc.createElement("ins");
        insEl.className = "diff-ins";
        appendVisibleTextNodes(insEl, seg.text, doc);
        afterEl.appendChild(insEl);
      }
    }

    if (afterEl && afterStr === "" && beforeStr !== "") {
      var tag = doc.createElement("span");
      tag.className = "pill-tag pill-tag--red";
      tag.textContent = "（本行已整行删除）";
      afterEl.appendChild(tag);
    }
  }

  var api = {
    INVIS_LABEL: INVIS_LABEL,
    visibleText: visibleText,
    appendVisibleTextNodes: appendVisibleTextNodes,
    computeCharDiff: computeCharDiff,
    renderInlineDiff: renderInlineDiff,
    renderSplitDiff: renderSplitDiff
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.AdcleanDiff = api;
    root.visibleText = visibleText;
  }
})(typeof window !== "undefined" ? window : globalThis);
