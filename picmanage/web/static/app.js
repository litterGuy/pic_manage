/* pic_manage -- progressive enhancement only.
 *
 * Every page works without this file.  What it adds: the mobile sidebar
 * toggle, auto-submitting sort selects, and swipe gestures on the viewer.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    // --- mobile sidebar -------------------------------------------------
    var toggle = document.querySelector(".sidebar-toggle");
    var sidebar = document.getElementById("sidebar");
    if (toggle && sidebar) {
      toggle.addEventListener("click", function () {
        var open = sidebar.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    // --- auto-submitting selects ----------------------------------------
    document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var form = sel.closest("form");
        if (form) form.submit();
      });
    });

    // --- viewer: swipe left/right ---------------------------------------
    var stage = document.querySelector(".stage");
    if (stage) {
      var startX = null;
      var startY = null;
      stage.addEventListener("touchstart", function (e) {
        if (e.target.closest("video, audio, a, button")) {
          startX = startY = null;
          return;
        }
        var t = e.changedTouches[0];
        startX = t.clientX;
        startY = t.clientY;
      }, { passive: true });
      stage.addEventListener("touchend", function (e) {
        if (startX === null) return;
        var t = e.changedTouches[0];
        var dx = t.clientX - startX;
        var dy = t.clientY - startY;
        startX = startY = null;
        if (Math.abs(dx) < 50 || Math.abs(dy) > Math.abs(dx)) return;
        var link = document.querySelector(dx < 0 ? ".nav-next" : ".nav-prev");
        if (link) location.href = link.href;
      }, { passive: true });
    }

    // --- viewer: preload the neighbours so arrow keys feel instant -------
    ["nav-prev", "nav-next"].forEach(function (cls) {
      var a = document.querySelector("." + cls);
      if (!a) return;
      var img = document.createElement("link");
      img.rel = "prefetch";
      img.href = a.href;
      document.head.appendChild(img);
    });
  });
})();
