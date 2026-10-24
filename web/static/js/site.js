/* Every public page: the header of Navigation v2 (docs/30 §11.2, §11.3, §11.8), the filter-bar
 * disclosure the phone layout collapses to (docs/31 §3 and §5.9; designer audit 2026-09-30 D-4) and the
 * capacity range check (§5.9, D-11).
 *
 * - "Menu" (`.nav-toggle`) shows and hides the phone Menu panel (`#site-menu`) below 720px. Its
 *   groups are `<details>`, so they open and shut with no script.
 * - The search button (`.search-toggle`, below 1080px) opens the search field over the top bar;
 *   Escape or "Close" puts the bar back and returns focus to the button, and focus or a click going
 *   anywhere else puts it back too, so focus never lands on a control the field covers.
 * - The account menu (`details.account-menu`) closes on Escape, on a click elsewhere and when focus
 *   leaves it.
 * - The section bar's row brings the current page into view without animation, and fades whichever
 *   edge cuts an item off.
 * - "Sign in" links (`[data-signin]`) carry the page the reader is on, as it is when followed (the
 *   map keeps its view in the URL), so sign-in comes back to it.
 * - "Filters" (`[data-filter-toggle]`) shows and hides a filter bar marked `data-collapse`, and
 *   names how many filters are set ("Filters (2 active)") so a collapsed bar still says the view is
 *   narrowed.
 *
 * Menu and Filters are disclosure buttons (`aria-expanded`), not dialogs: the panel opens in place
 * and pushes the page down, so nothing is covered and focus never lands behind an overlay. Without
 * this script the buttons stay hidden and the panels stay open (styles.css keys every collapsed rule
 * on the `.js` class base.html sets before first paint). Above 720px the buttons are hidden and the
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
    // Escape from inside an open panel, or on its button straight after opening it, closes it and
    // leaves focus on the button.
    var escape = function (e) {
      if (e.key !== "Escape" || button.getAttribute("aria-expanded") !== "true") return;
      if (window.getComputedStyle(button).display === "none") return; // wide layout: nothing to close
      setOpen(button, panel, false);
      button.focus();
    };
    panel.addEventListener("keydown", escape);
    button.addEventListener("keydown", escape);
  }

  // Whether focus moving to `next` leaves `box` (and whatever else is passed). `null` (the window lost
  // focus, or a click on something that takes none) is not leaving: a click is judged on its own.
  function leaves(next, box, also) {
    return !!next && !box.contains(next) && !(also && also.contains(next));
  }

  // Fragments whose content moved to a page of its own (docs/30 §11.10, T-8): "path#id" -> the new URL.
  // The old page keeps the id on a summary block that links onward; a reader arriving on the old
  // fragment goes straight to the new page. The lanes that move content add their rows here.
  // A record's anchors are keyed on "{record}", its one path segment (a slug or an id), and move the
  // reader only to a tab the page links: a record without that tab keeps the reader on its Overview.
  var MOVED_FRAGMENTS = {
    // Record tabs (lane V3, docs/30 §11.5).
    "/proposals/{record}#sources": "/proposals/{record}/sources",
    "/proposals/{record}#report-problem": "/proposals/{record}/sources#report-problem",
    "/proposals/{record}#timeline": "/proposals/{record}/timeline",
    "/proposals/{record}#history": "/proposals/{record}/timeline",
    "/assets/{record}#retirement": "/assets/{record}/retirement",
    "/assets/{record}#grid": "/assets/{record}/grid",
    "/assets/{record}#owners": "/assets/{record}/ownership",
    "/assets/{record}#nearby": "/assets/{record}/nearby",
    "/assets/{record}#sources": "/assets/{record}/sources",
    "/interconnection-points/{record}#projects": "/interconnection-points/{record}/projects",
    "/interconnection-points/{record}#changes": "/interconnection-points/{record}/changes",
    "/interconnection-points/{record}#sources": "/interconnection-points/{record}/sources"
  };
  var record = window.location.pathname.match(/^(\/(?:proposals|assets|interconnection-points)\/)([^\/]+)$/);
  var movedTo = MOVED_FRAGMENTS[(record ? record[1] + "{record}" : window.location.pathname) + window.location.hash];
  if (movedTo && record) {
    movedTo = movedTo.replace("{record}", record[2]);
    if (!document.querySelector('a[href="' + movedTo.split("#")[0] + '"]')) movedTo = null;
  }
  if (movedTo) window.location.replace(movedTo);

  var navButton = document.querySelector(".nav-toggle");
  var navPanel = document.getElementById("site-menu");
  if (navButton && navPanel) wire(navButton, navPanel);

  // The search field over the top bar, below 1080px.
  var searchButton = document.querySelector(".search-toggle");
  var searchForm = document.getElementById("site-search");
  if (searchButton && searchForm) {
    var searchInput = searchForm.querySelector("input[type='search']");
    var searchClose = searchForm.querySelector(".site-search__close");
    var setSearch = function (open, refocus) {
      searchButton.setAttribute("aria-expanded", open ? "true" : "false");
      searchForm.classList.toggle("is-open", open);
      if (open && searchInput) searchInput.focus();
      else if (refocus) searchButton.focus();
    };
    searchButton.addEventListener("click", function () {
      setSearch(searchButton.getAttribute("aria-expanded") !== "true", false);
    });
    if (searchClose) searchClose.addEventListener("click", function () { setSearch(false, true); });
    searchForm.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && searchForm.classList.contains("is-open")) setSearch(false, true);
    });
    // The open field covers the bar, so focus or a click going anywhere else puts the bar back first:
    // Tab past Close, or Shift+Tab out of the field, never lands on a link, the account or Menu (or the
    // search button) hidden under it (SC 2.4.11; review of lane nav-shell, 2026-10-23).
    searchForm.addEventListener("focusout", function (e) {
      if (searchForm.classList.contains("is-open") && leaves(e.relatedTarget, searchForm)) setSearch(false, false);
    });
    document.addEventListener("click", function (e) {
      if (searchForm.classList.contains("is-open") && leaves(e.target, searchForm, searchButton)) {
        setSearch(false, false);
      }
    });
  }

  // The account menu: a `<details>`, closed again on Escape (focus back on its summary), on a click
  // anywhere outside it, and when focus leaves it, since its open list hangs over the section bar's
  // right end (About & sources, the freshness line) and would hide where Tab goes next (SC 2.4.11).
  Array.prototype.forEach.call(document.querySelectorAll("details.account-menu"), function (menu) {
    var summary = menu.querySelector("summary");
    document.addEventListener("click", function (e) {
      if (menu.open && !menu.contains(e.target)) menu.open = false;
    });
    menu.addEventListener("focusout", function (e) {
      if (menu.open && leaves(e.relatedTarget, menu)) menu.open = false;
    });
    menu.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !menu.open) return;
      menu.open = false;
      if (summary) summary.focus();
    });
  });

  // The section bar's row (and any row like it): the current page in view, and a fade on a cut edge.
  Array.prototype.forEach.call(document.querySelectorAll(".section-bar__items"), function (row) {
    var current = row.querySelector("[aria-current]");
    if (current && row.scrollWidth > row.clientWidth) {
      var item = current.parentNode;
      row.scrollLeft = Math.max(0, item.offsetLeft - (row.clientWidth - item.offsetWidth) / 2);
    }
    var edges = function () {
      var rest = row.scrollWidth - row.clientWidth - row.scrollLeft;
      row.classList.toggle("is-cut-start", row.scrollLeft > 1);
      row.classList.toggle("is-cut-end", rest > 1);
    };
    row.addEventListener("scroll", edges, { passive: true });
    window.addEventListener("resize", edges);
    edges();
  });

  // "Sign in" comes back to this page. Not from the sign-in pages themselves, nor from a page whose
  // address carries a one-time token it does not mean to keep: email verification, password reset and
  // an alert's unsubscribe link. An invitation (`/my/invitations/accept?t=`) does come back, token
  // and all, as the page's own sign-in links do (web/my_projects.py `invitation`): signing in is how
  // the reader gets to accept it.
  var NO_RETURN = /^\/(login|register|logout|forgot-password|reset-password|verify|unsubscribe|account)(\/|$)/;
  Array.prototype.forEach.call(document.querySelectorAll("a[data-signin]"), function (link) {
    var carry = function () {
      if (NO_RETURN.test(window.location.pathname)) return;
      link.href = "/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
    };
    ["pointerdown", "focus", "click"].forEach(function (type) { link.addEventListener(type, carry); });
  });

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
