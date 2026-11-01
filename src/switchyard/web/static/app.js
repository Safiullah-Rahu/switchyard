// Switchyard console: theme switching, live refresh, toast dismissal. No framework, no build.
(function () {
  "use strict";

  // ---- theme -------------------------------------------------------------------------
  function currentTheme() {
    try { return localStorage.getItem("sy-theme") || "system"; } catch (e) { return "system"; }
  }
  function applyTheme(choice) {
    if (choice === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = choice;
    try { localStorage.setItem("sy-theme", choice); } catch (e) { /* private mode */ }
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeSet === choice));
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-set]");
    if (button) applyTheme(button.dataset.themeSet);
  });
  applyTheme(currentTheme());

  // ---- toast -------------------------------------------------------------------------
  var toast = document.getElementById("toast");
  if (toast) setTimeout(function () { toast.style.transition = "opacity .3s"; toast.style.opacity = "0"; setTimeout(function () { toast.remove(); }, 320); }, 6000);

  // ---- live refresh: re-render the page content every few seconds while visible ----------
  if (!document.body.hasAttribute("data-live")) return;
  var chip = document.getElementById("live-chip");
  var label = document.getElementById("live-text");
  var last = Date.now();
  if (chip) chip.hidden = false;

  function stamp() {
    if (!label) return;
    var s = Math.round((Date.now() - last) / 1000);
    label.textContent = s < 5 ? "Live · updated just now" : "Live · updated " + s + " s ago";
  }

  function busy() {
    var el = document.activeElement;
    return document.hidden || (el && el.closest("form, details[open], textarea, input"));
  }

  async function refresh() {
    if (busy()) return;
    try {
      var response = await fetch(location.href, { headers: { "X-Live-Refresh": "1" }, credentials: "same-origin" });
      if (!response.ok) return;
      var doc = new DOMParser().parseFromString(await response.text(), "text/html");
      ["page", "nav"].forEach(function (id) {
        var fresh = doc.getElementById(id), stale = document.getElementById(id);
        if (fresh && stale && fresh.innerHTML !== stale.innerHTML) stale.innerHTML = fresh.innerHTML;
      });
      last = Date.now();
      stamp();
    } catch (e) { /* offline for a moment; try again next tick */ }
  }

  setInterval(refresh, 6000);
  setInterval(stamp, 1000);
  stamp();
})();
