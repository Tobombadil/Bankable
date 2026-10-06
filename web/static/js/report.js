/* "Report a problem" (web/reports.py; designer D-6). Progressive enhancement only: without this
 * script the form posts to /report and the reader lands on a page with the same success or error
 * state. With it, the answer replaces the form where the reader is and focus moves to its status
 * line, so a screen reader hears the outcome. A network failure falls back to the plain post.
 */
(function () {
  "use strict";
  function bind(root) {
    var form = root.querySelector("[data-report-form]");
    if (!form) return;
    form.addEventListener("submit", function (e) {
      if (!window.fetch || !window.FormData) return;
      e.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = "Sending…"; }
      fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        credentials: "same-origin",
        headers: { "X-Report-Fragment": "1" }
      })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var holder = document.createElement("div");
          holder.innerHTML = html;
          var next = holder.querySelector("#report-problem");
          if (!next) throw new Error("unexpected response");
          root.replaceWith(next);
          bind(next);
          var status = next.querySelector("#report-status");
          if (status) status.focus();
        })
        .catch(function () { form.submit(); });
    });
  }
  var el = document.getElementById("report-problem");
  if (el) bind(el);
})();
