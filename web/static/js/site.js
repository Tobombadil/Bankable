/* Every public page: the two disclosures the 400px layout collapses to (docs/31 §3 and §5.9;
 * designer audit 2026-09-30 D-4) and the capacity range check (§5.9, D-11).
 *
 * - "Menu" (`.nav-toggle`) shows and hides the primary links and the search box below 720px.
 * - "Filters" (`[data-filter-toggle]`) does the same for a filter bar marked `data-collapse`, and
 *   names how many filters are set ("Filters (2 active)") so a collapsed bar still says the view is
 *   narrowed.
 *
 * Both are disclosure buttons (`aria-expanded`), not dialogs: the panel opens in place and pushes
 * the page down, so nothing is covered and focus never lands behind an overlay. Without this
 * script the buttons stay hidden and the panels stay open (styles.css keys every collapsed rule on
 * the `.js` class base.html sets before first paint). Above 720px the buttons are hidden and the
 * panels are always shown, whatever their state.
 */
(function () {
  "use strict";

  function setOpen(button, panel, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    panel.classList.toggle("is-open", open);
  }

  function wire(button, panel) {
    button.addEventListener("click", function () {
      setOpen(button, panel, button.getAttribute("aria-expanded") !== "true");
    });
    // Escape from inside an open panel closes it and returns focus to its button.
    panel.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || button.getAttribute("aria-expanded") !== "true") return;
      if (window.getComputedStyle(button).display === "none") return; // wide layout: nothing to close
      setOpen(button, panel, false);
      button.focus();
    });
  }

  var navButton = document.querySelector(".nav-toggle");
  var navPanel = document.getElementById("site-nav-panel");
  if (navButton && navPanel) wire(navButton, navPanel);

  // How many filters a bar has set. A control counts when its value differs from its default: an
  // empty select or text box, an unchecked box, or the value in `data-default` (opportunity status
  // defaults to "open"; the map's placement boxes default to checked). Sort orders and anything in
  // a hidden group (the asset types while the layer is off) are not filters.
  function activeCount(form) {
    var n = 0;
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.type === "hidden" || el.type === "submit" || el.type === "button") return;
      if (el.hasAttribute("data-not-filter") || el.closest("[hidden]")) return;
      var def = el.getAttribute("data-default");
      if (el.type === "checkbox") {
        if (el.checked !== (def === "checked")) n += 1;
      } else if (el.value && el.value.trim() !== "" && el.value !== (def || "")) {
        n += 1;
      }
    });
    // Filters a link set with no control on the page (map.js keeps and counts them).
    return n + (parseInt(form.getAttribute("data-extra-active") || "0", 10) || 0);
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-filter-toggle]"), function (button) {
    var form = document.getElementById(button.getAttribute("aria-controls"));
    if (!form) return;
    var countEl = button.querySelector("[data-filter-count]");
    function label() {
      var n = activeCount(form);
      if (countEl) countEl.textContent = n ? " (" + n + " active)" : "";
    }
    wire(button, form);
    form.addEventListener("change", label);
    form.addEventListener("input", label);
    // map.js changes controls itself ("Clear all", a linked filter); it says so with this event.
    form.addEventListener("filters:changed", label);
    // A submitted (list) form has done its job: close it so the results it asked for are in view.
    form.addEventListener("submit", function () {
      if (window.getComputedStyle(button).display !== "none") setOpen(button, form, false);
    });
    // "Show the map" at the foot of an open bar: close it and go back to its button, below which
    // the results now sit.
    Array.prototype.forEach.call(form.querySelectorAll("[data-filter-done]"), function (done) {
      done.addEventListener("click", function () {
        setOpen(button, form, false);
        button.focus();
      });
    });
    label();
  });

  // docs/31 §5.9: a minimum above its maximum is said in words beside the pair, as the reader
  // types, instead of returning an unexplained empty list. The server renders the same message
  // when a page arrives with a bad pair (web/empty_state.py `range_error`).
  Array.prototype.forEach.call(document.querySelectorAll("[data-range-error]"), function (msg) {
    var ids = msg.getAttribute("data-range-error").split(" ");
    var low = document.getElementById(ids[0]);
    var high = document.getElementById(ids[1]);
    if (!low || !high) return;
    function check() {
      var lo = low.value === "" ? null : Number(low.value);
      var hi = high.value === "" ? null : Number(high.value);
      var bad = lo !== null && hi !== null && isFinite(lo) && isFinite(hi) && lo > hi;
      msg.hidden = !bad;
      if (bad) msg.textContent = "Minimum (" + low.value + ") is above the maximum (" + high.value + "), so nothing can match. Swap them or clear one.";
      [low, high].forEach(function (el) {
        if (bad) {
          el.setAttribute("aria-invalid", "true");
          el.setAttribute("aria-describedby", msg.id);
        } else {
          el.removeAttribute("aria-invalid");
          el.removeAttribute("aria-describedby");
        }
      });
    }
    low.addEventListener("input", check);
    high.addEventListener("input", check);
  });
})();
