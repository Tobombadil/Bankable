/* The site finder page (web/templates/site_finder.html; web/site_finder.py). Average reader first:
 * every change of criteria applies at once, the URL keeps up (history.replaceState, only what
 * differs from the defaults, in the server's own order), and a click on a row or on a band opens its
 * details beside the map (a bottom sheet below 720px) and moves the map to it.
 *
 * The page works without this file: the criteria are a GET form, a row's name links to the view
 * with its band open, the headings and the pager are links. This file fetches the same server
 * fragments the page renders (`/api/site-finder/rows`, `/api/site-finder/band`), so no wording lives
 * here, and draws the map: the bands that match (`/api/site-finder/geo`), the OpenStreetMap lines
 * coloured and sized by voltage class and the substations (`/api/site-finder/grid`), the open
 * band's own outline, floodplain cut out, and from zoom 12 the Texas land parcels in view
 * (`/api/site-finder/parcels`): an outline per parcel, a click names its acres, its holding and the
 * appraisal district's record. No owner name exists to show.
 */
(function () {
  "use strict";

  var configEl = document.getElementById("sf-config");
  var form = document.getElementById("sf-criteria");
  var rowsEl = document.getElementById("sf-rows");
  var drawer = document.getElementById("sf-drawer");
  var drawerBody = document.getElementById("sf-drawer-body");
  if (!configEl || !form || !rowsEl || !drawer || !drawerBody) return;
  var config = JSON.parse(configEl.textContent || "{}");
  var defaults = config.defaults || {};
  var PATH = config.path || "/site-finder";
  var ORDER = ["min_kv", "adjacent_mi", "max_substation_mi", "exclude_flood", "include_unknown_voltage", "exclude_protected", "state", "county", "min_acres", "max_owners"];
  var OPTIONAL = { state: true, county: true, min_acres: true, max_owners: true };
  var PARCELS_MIN_ZOOM = Number(config.parcels_min_zoom || 12);
  var FLAGS = { exclude_flood: true, include_unknown_voltage: true, exclude_protected: true };
  var CONUS = [[-125, 24], [-66.5, 49.5]];

  var mq = function (q) { return window.matchMedia ? window.matchMedia(q) : { matches: false }; };
  var coarse = mq("(pointer: coarse)");
  var narrow = mq("(max-width: 719px)");
  var reduceMotion = mq("(prefers-reduced-motion: reduce)").matches;

  if (coarse.matches) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-touch-words]"), function (el) {
      el.textContent = el.getAttribute("data-touch-words");
    });
  }

  // ------------------------------------------------------------------ criteria and the URL
  var adjacentChoices = (config.adjacent_choices || [0.25, 0.5, 1, 2]).map(Number);
  function snapAdjacent(v) {
    var best = adjacentChoices[0];
    adjacentChoices.forEach(function (d) {
      if (Math.abs(d - v) < Math.abs(best - v) || (Math.abs(d - v) === Math.abs(best - v) && d < best)) best = d;
    });
    return best;
  }
  function num(v) { return String(Number(v)); }

  var adjacent = document.getElementById("sf-adjacent");
  var substation = document.getElementById("sf-substation");
  function showRange(input, outId, value) {
    var out = document.getElementById(outId);
    if (out) out.textContent = num(value) + " mi";
    input.setAttribute("aria-valuetext", num(value) + " miles");
  }
  if (adjacent) {
    adjacent.addEventListener("input", function () {
      var v = snapAdjacent(Number(adjacent.value));
      if (Number(adjacent.value) !== v) adjacent.value = String(v);
      showRange(adjacent, "sf-adjacent-value", v);
    });
  }
  if (substation) {
    substation.addEventListener("input", function () { showRange(substation, "sf-substation-value", substation.value); });
  }

  function readForm() {
    var c = {};
    ORDER.forEach(function (name) {
      if (FLAGS[name]) {
        var box = form.querySelector('input[type="checkbox"][name="' + name + '"]');
        c[name] = !!(box && box.checked);
      } else {
        var el = form.elements[name];
        c[name] = el ? String(el.value || "") : "";
      }
    });
    c.adjacent_mi = num(snapAdjacent(Number(c.adjacent_mi || defaults.adjacent_mi)));
    c.max_substation_mi = num(c.max_substation_mi || defaults.max_substation_mi);
    c.min_kv = num(c.min_kv || defaults.min_kv);
    return c;
  }

  var state = {
    criteria: readForm(),
    sort: (config.criteria && config.criteria.sort) || defaults.sort,
    cursor: new URLSearchParams(window.location.search).get("cursor"),
    band: new URLSearchParams(window.location.search).get("band")
  };

  function isDefault(name, value) {
    if (FLAGS[name]) return value === !!defaults[name];
    if (OPTIONAL[name]) return !value;
    return Number(value) === Number(defaults[name]);
  }
  // The query in web/site_finder.py `Criteria.url_items` order: criteria, sort, then cursor and band.
  function items(opts) {
    opts = opts || {};
    var out = [];
    ORDER.forEach(function (name) {
      var v = state.criteria[name];
      if (isDefault(name, v)) return;
      out.push([name, FLAGS[name] ? (v ? "true" : "false") : String(v)]);
    });
    if (opts.sort !== false && state.sort && state.sort !== defaults.sort) out.push(["sort", state.sort]);
    if (opts.cursor && state.cursor) out.push(["cursor", state.cursor]);
    if (opts.band && state.band) out.push(["band", state.band]);
    return out;
  }
  function query(list) {
    return list.map(function (kv) { return encodeURIComponent(kv[0]) + "=" + encodeURIComponent(kv[1]); }).join("&");
  }
  function pageUrl() {
    var q = query(items({ cursor: true, band: true }));
    return PATH + (q ? "?" + q : "");
  }
  function syncUrl() {
    try { window.history.replaceState(null, "", pageUrl()); } catch (e) { /* a sandboxed frame */ }
  }

  // --------------------------------------------------------------------------- fetching
  var pending = {};
  function fetchText(key, url) {
    if (pending[key] && pending[key].abort) pending[key].abort();
    var ctrl = window.AbortController ? new AbortController() : null;
    pending[key] = ctrl;
    return fetch(url, { credentials: "same-origin", signal: ctrl ? ctrl.signal : undefined }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok, status: r.status, text: t }; });
    });
  }
  function fetchJson(key, url) {
    return fetchText(key, url).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return JSON.parse(res.text);
    });
  }
  function quiet(err) { if (!err || err.name !== "AbortError") showMapNote("Part of the map could not be loaded. Move the map to try again."); }

  // ------------------------------------------------------------------------- the list
  function refreshRows(opts) {
    opts = opts || {};
    var q = query(items({ cursor: true, band: true }));
    rowsEl.setAttribute("aria-busy", "true");
    return fetchText("rows", "/api/site-finder/rows" + (q ? "?" + q : "")).then(function (res) {
      var doc = new DOMParser().parseFromString(res.text, "text/html");
      var summary = doc.getElementById("sf-summary");
      var here = document.getElementById("sf-summary");
      if (summary && here) here.replaceWith(document.importNode(summary, true));
      var list = doc.getElementById("sf-list");
      rowsEl.innerHTML = list ? list.outerHTML : res.text;
      rowsEl.removeAttribute("aria-busy");
      applyPlaces(doc.getElementById("sf-place-options"));
      markSelected();
      if (opts.scroll) {
        var top = document.getElementById("sf-results");
        if (top) top.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      }
      if (opts.fit) fitToRows();
    }).catch(function (err) { if (!err || err.name !== "AbortError") rowsEl.removeAttribute("aria-busy"); });
  }

  function markSelected() {
    Array.prototype.forEach.call(rowsEl.querySelectorAll("tr[data-band]"), function (tr) {
      var on = tr.getAttribute("data-band") === state.band;
      tr.classList.toggle("is-selected", on);
      var link = tr.querySelector(".sf-band-link");
      if (link) { if (on) link.setAttribute("aria-current", "true"); else link.removeAttribute("aria-current"); }
    });
  }

  function paramFrom(href, name) {
    try { return new URL(href, window.location.href).searchParams.get(name); } catch (e) { return null; }
  }

  rowsEl.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button > 0) return;
    if (a.classList.contains("sf-band-link")) {
      e.preventDefault();
      openBand(a.getAttribute("data-band"), { fit: true, opener: a });
    } else if (a.classList.contains("sort-link")) {
      e.preventDefault();
      state.sort = paramFrom(a.href, "sort") || defaults.sort;
      state.cursor = null;
      syncUrl();
      refreshRows();
    } else if (a.hasAttribute("data-sf-page")) {
      e.preventDefault();
      state.cursor = paramFrom(a.href, "cursor");
      syncUrl();
      refreshRows({ scroll: true });
    }
  });

  // ------------------------------------------------------------------------- criteria changes
  var countySelect = document.getElementById("sf-county");
  var stateSelect = document.getElementById("sf-state");
  function filterCounties() {
    if (!countySelect) return;
    var st = stateSelect ? stateSelect.value : "";
    Array.prototype.forEach.call(countySelect.options, function (opt) {
      if (!opt.value) return;
      var show = !st || opt.getAttribute("data-state") === st;
      opt.hidden = !show;
      opt.disabled = !show;
    });
    var sel = countySelect.options[countySelect.selectedIndex];
    if (sel && sel.disabled) countySelect.value = "";
  }
  if (stateSelect) stateSelect.addEventListener("change", filterCounties);
  filterCounties();

  // The state and county choices for the criteria just applied (the rows fragment carries them), so a
  // county a looser criterion brings in can be picked without reloading the page. Setting the options
  // fires no change event, so this never loops back into criteriaChanged.
  function applyPlaces(tpl) {
    if (!tpl || !tpl.content) return;
    [["sf-state", "state"], ["sf-county", "county"]].forEach(function (pair) {
      var here = document.getElementById(pair[0]);
      var fresh = tpl.content.querySelector('select[data-for="' + pair[0] + '"]');
      if (!here || !fresh) return;
      here.innerHTML = fresh.innerHTML;
      var want = state.criteria[pair[1]] || "";
      var has = Array.prototype.some.call(here.options, function (o) { return o.value === want; });
      here.value = has ? want : "";
    });
    filterCounties();
    var need = tpl.getAttribute("data-counties-need-state") === "true" && !(state.criteria.county);
    var hint = document.getElementById("sf-county-hint");
    if (need && !hint && countySelect) {
      hint = document.createElement("span");
      hint.className = "sf-hint";
      hint.id = "sf-county-hint";
      hint.textContent = "Choose a state to list its counties.";
      countySelect.insertAdjacentElement("afterend", hint);
    } else if (!need && hint) {
      hint.parentNode.removeChild(hint);
    }
    if (countySelect) {
      if (need) countySelect.setAttribute("aria-describedby", "sf-county-hint");
      else countySelect.removeAttribute("aria-describedby");
    }
  }

  var changeTimer = null;
  function criteriaChanged() {
    window.clearTimeout(changeTimer);
    changeTimer = window.setTimeout(function () {
      state.criteria = readForm();
      state.cursor = null;
      syncUrl();
      updateKey();
      var placeChanged = lastPlace !== state.criteria.state + "|" + state.criteria.county;
      lastPlace = state.criteria.state + "|" + state.criteria.county;
      refreshRows({ fit: placeChanged });
      reloadBands();
      if (state.band) loadBand(state.band, { fit: false });
    }, 120);
  }
  var lastPlace = state.criteria.state + "|" + state.criteria.county;
  form.addEventListener("change", criteriaChanged);
  form.addEventListener("submit", function (e) { e.preventDefault(); criteriaChanged(); });

  function updateKey() {
    var el = document.querySelector(".sf-key__band-words");
    if (el) el.textContent = state.criteria.adjacent_mi + " mi";
    // "Owners, at most" applies with a minimum acreage: say so beside it while there is none.
    var owners = document.getElementById("sf-max-owners");
    var hint = document.getElementById("sf-owners-hint");
    var needs = !state.criteria.min_acres;
    if (owners && needs && !hint) {
      hint = document.createElement("span");
      hint.className = "sf-hint";
      hint.id = "sf-owners-hint";
      hint.textContent = "Applies with a minimum acreage.";
      owners.insertAdjacentElement("afterend", hint);
    } else if (!needs && hint) {
      hint.parentNode.removeChild(hint);
    }
    if (owners) {
      if (needs) owners.setAttribute("aria-describedby", "sf-owners-hint");
      else owners.removeAttribute("aria-describedby");
    }
  }

  // ----------------------------------------------------------------------------- the drawer
  var expandBtn = document.getElementById("sf-drawer-expand");
  var hideTimer = null;
  var opener = null;
  function syncExpand() {
    if (!expandBtn) return;
    if (!narrow.matches) { expandBtn.hidden = true; return; }
    if (drawer.classList.contains("is-expanded")) { expandBtn.hidden = false; return; }
    expandBtn.hidden = !(drawer.scrollHeight > drawer.clientHeight + 1);
  }
  function setExpanded(on) {
    drawer.classList.toggle("is-expanded", on);
    if (expandBtn) {
      expandBtn.textContent = on ? "Collapse" : "Expand";
      expandBtn.setAttribute("aria-expanded", on ? "true" : "false");
    }
  }
  if (expandBtn) expandBtn.addEventListener("click", function () { setExpanded(!drawer.classList.contains("is-expanded")); });

  function showDrawer() {
    window.clearTimeout(hideTimer);
    drawer.hidden = false;
    drawer.removeAttribute("inert");
    void drawer.offsetWidth;
    drawer.classList.add("is-open");
    drawer.scrollTop = 0;
    syncExpand();
  }
  function closeDrawer() {
    state.band = null;
    syncUrl();
    markSelected();
    setSelected(null);
    setExpanded(false);
    drawer.classList.remove("is-open");
    drawer.setAttribute("inert", "");
    hideTimer = window.setTimeout(function () { drawer.hidden = true; }, reduceMotion ? 0 : 160);
    if (opener && document.body.contains(opener)) opener.focus();
    opener = null;
  }
  drawer.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("#sf-drawer-close, #sf-drawer-back") : null;
    if (!a) return;
    e.preventDefault();
    closeDrawer();
  });
  drawer.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });

  function loadBand(id, opts) {
    var q = query(items({ sort: false }).concat([["band", id]]));
    return fetchText("band", "/api/site-finder/band?" + q).then(function (res) {
      drawerBody.innerHTML = res.text;
      var mapJson = drawerBody.querySelector(".sf-band-map");
      var data = null;
      try { data = mapJson ? JSON.parse(mapJson.textContent) : null; } catch (e) { data = null; }
      setSelected(data);
      showDrawer();
      if (opts && opts.fit && data && data.bbox) fitBand(data.bbox);
      if (opts && opts.focus) {
        var close = document.getElementById("sf-drawer-close");
        if (close) close.focus({ preventScroll: true });
      }
    }).catch(function () { /* superseded */ });
  }
  function openBand(id, opts) {
    if (!id) return;
    opts = opts || {};
    opener = opts.opener || null;
    state.band = id;
    syncUrl();
    markSelected();
    var frame = document.getElementById("sf-frame");
    if (frame) {
      var r = frame.getBoundingClientRect();
      if (r.top < 0 || r.top > window.innerHeight * 0.5) frame.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    }
    loadBand(id, { fit: opts.fit !== false, focus: true });
  }

  // ------------------------------------------------------------------------------ the map
  var Basemap = window.InfraqueBasemap;
  var mapEl = document.getElementById("sf-map");
  var map = null;
  var noteEl = null;
  function showMapNote(text) {
    if (!mapEl) return;
    if (!noteEl) {
      noteEl = document.createElement("p");
      noteEl.className = "sf-map-note";
      noteEl.setAttribute("role", "status");
      mapEl.parentNode.insertBefore(noteEl, mapEl.nextSibling);
    }
    noteEl.textContent = text || "";
    noteEl.hidden = !text;
  }
  if (!mapEl || typeof maplibregl === "undefined" || !Basemap) {
    if (mapEl) mapEl.classList.add("is-unavailable");
    return;
  }

  var css = Basemap.cssVar;
  function colour(name, fallback) { return css(name) || fallback; }
  var C = {
    k69: colour("--sf-kv-69", "#c99a2e"),
    k115: colour("--sf-kv-115", "#a87a10"),
    k230: colour("--sf-kv-230", "#8a6300"),
    k345: colour("--sf-kv-345", "#6b4a00"),
    k500: colour("--sf-kv-500", "#4a3200"),
    unknown: colour("--sf-kv-unknown", "#7d858c"),
    band: colour("--sf-band", "#1d7f8f"),
    selected: colour("--sf-selected", "#0b5563"),
    parcel: colour("--sf-parcel", "#7a5a2f"),
    surface: colour("--surface", "#ffffff")
  };
  var KV = ["get", "kv"];
  var BY_CLASS = ["step", KV, C.unknown, 69, C.k69, 100, C.k115, 162, C.k230, 288, C.k345, 400, C.k500];

  function summaryBounds() {
    var el = document.getElementById("sf-summary");
    var raw = el ? el.getAttribute("data-bounds") : "";
    try {
      var b = raw ? JSON.parse(raw) : null;
      return b && b.length === 4 ? [[b[0], b[1]], [b[2], b[3]]] : null;
    } catch (e) { return null; }
  }
  var openMap = drawerBody.querySelector(".sf-band-map");
  var openData = null;
  try { openData = openMap ? JSON.parse(openMap.textContent) : null; } catch (e) { openData = null; }
  var startBounds = openData && openData.bbox ? [[openData.bbox[0], openData.bbox[1]], [openData.bbox[2], openData.bbox[3]]] : (summaryBounds() || CONUS);

  map = new maplibregl.Map({
    container: "sf-map",
    style: Basemap.fallbackStyle(Basemap.mapColors()),
    bounds: startBounds,
    fitBoundsOptions: { padding: 40, maxZoom: 11 },
    attributionControl: false,
    cooperativeGestures: coarse.matches
  });
  map.dragRotate.disable();
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  window.__sfMap = map; // for the Playwright test only
  window.__sfReady = false;

  function drawerPadding() {
    var pad = { top: 40, right: 40, bottom: 40, left: 40 };
    if (narrow.matches) {
      var rect = mapEl.getBoundingClientRect();
      var sheetTop = window.innerHeight * 0.55;
      pad.bottom = Math.max(40, Math.min(rect.height * 0.6, rect.bottom - sheetTop + 24));
    } else {
      pad.right = Math.min(mapEl.clientWidth * 0.5, (drawer.offsetWidth || 340) + 40);
    }
    return pad;
  }
  // The map may never have loaded (MapLibre blocked or failed): the list and the drawer work without it.
  function fitBand(bbox) {
    if (!map) return;
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: drawerPadding(), maxZoom: 13, duration: reduceMotion ? 0 : 600 });
  }
  function fitToRows() {
    if (!map) return;
    var b = summaryBounds();
    if (b) map.fitBounds(b, { padding: 40, maxZoom: 11, duration: reduceMotion ? 0 : 600 });
  }

  // A band is drawn as its line at the band's true width: twice the distance chosen, in pixels at
  // the map's latitude, never thinner than 5px so a band shows at a state-wide view.
  function bandWidth() {
    var lat = map.getCenter().lat * Math.PI / 180;
    var metres = Number(state.criteria.adjacent_mi) * 1609.344 * 2;
    var stops = ["interpolate", ["exponential", 2], ["zoom"]];
    for (var z = 0; z <= 22; z += 2) {
      var mpp = 40075016.686 * Math.cos(lat) / (512 * Math.pow(2, z));
      stops.push(z, Math.max(5, metres / mpp));
    }
    return stops;
  }
  function empty() { return { type: "FeatureCollection", features: [] }; }
  var hover = null;

  map.on("load", function () {
    Basemap.addBasemap(map, {
      tileMode: config.tile_mode || mapEl.getAttribute("data-tile-mode"),
      tileUrl: config.tile_url || mapEl.getAttribute("data-tile-url"),
      colors: Basemap.mapColors()
    });
    map.addSource("sf-bands", { type: "geojson", data: empty() });
    map.addSource("sf-lines", { type: "geojson", data: empty() });
    map.addSource("sf-subs", { type: "geojson", data: empty() });
    map.addSource("sf-selected", { type: "geojson", data: empty() });
    map.addSource("sf-parcels", { type: "geojson", data: empty() });
    // Parcels under everything else: a nearly clear fill (so a click inside one finds it) and an outline.
    map.addLayer({
      id: "sf-parcels-fill", type: "fill", source: "sf-parcels", minzoom: PARCELS_MIN_ZOOM,
      paint: { "fill-color": C.parcel, "fill-opacity": 0.04 }
    });
    map.addLayer({
      id: "sf-parcels-line", type: "line", source: "sf-parcels", minzoom: PARCELS_MIN_ZOOM,
      paint: { "line-color": C.parcel, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.6, 16, 1.4], "line-opacity": 0.85 }
    });
    map.addLayer({
      id: "sf-bands", type: "line", source: "sf-bands",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": C.band, "line-opacity": ["interpolate", ["linear"], ["zoom"], 9, 0.32, 13, 0.16], "line-width": bandWidth() }
    });
    map.addLayer({
      id: "sf-selected-fill", type: "fill", source: "sf-selected", filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": C.selected, "fill-opacity": 0.28 }
    });
    map.addLayer({
      id: "sf-selected-line", type: "line", source: "sf-selected",
      paint: { "line-color": C.selected, "line-width": ["case", ["==", ["get", "role"], "centreline"], 1.5, 2.25] }
    });
    var width = ["interpolate", ["linear"], ["zoom"],
      4, ["step", KV, 0.6, 100, 0.8, 162, 1.1, 288, 1.4, 400, 1.8],
      12, ["step", KV, 1.4, 100, 2, 162, 2.6, 288, 3.3, 400, 4.2]];
    map.addLayer({
      id: "sf-lines", type: "line", source: "sf-lines", filter: [">=", KV, 0],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": BY_CLASS, "line-width": width }
    });
    map.addLayer({
      id: "sf-lines-unknown", type: "line", source: "sf-lines", filter: ["<", KV, 0],
      paint: { "line-color": C.unknown, "line-width": width, "line-dasharray": ["literal", [2, 2]] }
    });
    map.addLayer({
      id: "sf-subs", type: "circle", source: "sf-subs", minzoom: 8,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3, 13, 6],
        "circle-color": BY_CLASS, "circle-stroke-color": C.surface, "circle-stroke-width": 1.25
      }
    });
    if (openData) setSelected(openData);
    // Ready (for the Playwright test) once the first data has landed and the map has drawn it.
    // A failed basemap tile can keep MapLibre from ever calling the map idle, so a second later counts.
    loadView().then(function () {
      var ready = function () { window.__sfReady = true; };
      map.once("idle", ready);
      map.triggerRepaint();
      window.setTimeout(ready, 1000);
    });
  });

  function bboxParam() {
    var b = map.getBounds();
    var clamp = function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); };
    return [clamp(b.getWest(), -180, 179.99), clamp(b.getSouth(), -89.99, 89.9), clamp(b.getEast(), -179.99, 180), clamp(b.getNorth(), -89.9, 89.99)]
      .map(function (v) { return v.toFixed(4); }).join(",");
  }
  function zoomParam() { return String(Math.max(0, Math.round(map.getZoom()))); }

  function reloadBands() {
    if (!map || !map.getSource("sf-bands")) return Promise.resolve();
    map.setPaintProperty("sf-bands", "line-width", bandWidth());
    var q = query(items({ sort: false }).concat([["bbox", bboxParam()], ["zoom", zoomParam()]]));
    return fetchJson("geo", "/api/site-finder/geo?" + q).then(function (env) {
      map.getSource("sf-bands").setData(env.data || empty());
      var meta = (env.meta && env.meta.site_finder) || {};
      if (meta.bands_in_view > meta.bands_drawn) {
        showMapNote("Showing the " + meta.bands_drawn.toLocaleString() + " longest of " + meta.bands_in_view.toLocaleString() + " bands in view. Zoom in to see the rest.");
      } else {
        showMapNote("");
      }
    }).catch(quiet);
  }
  function flatten(env) {
    var fc = (env && env.data) || empty();
    var out = [];
    (fc.features || []).forEach(function (f) {
      var p = f.properties || {};
      if (p.feature_kind === "asset_cluster" || !f.geometry) return;
      var attrs = p.attributes || {};
      var kv = p.voltage_kv != null ? p.voltage_kv : attrs.voltage_kv;
      var unknown = p.voltage_unknown != null ? p.voltage_unknown : attrs.voltage_unknown;
      out.push({
        type: "Feature", geometry: f.geometry,
        properties: { kv: unknown || kv == null ? -1 : Number(kv), name: p.name || "", operator: p.operator_name || attrs.operator || "" }
      });
    });
    return { type: "FeatureCollection", features: out };
  }
  function reloadGrid() {
    var where = "&bbox=" + bboxParam() + "&zoom=" + zoomParam();
    var jobs = [fetchJson("lines", "/api/site-finder/grid?layer=lines" + where).then(function (env) {
      if (map.getSource("sf-lines")) map.getSource("sf-lines").setData(flatten(env));
    }).catch(quiet)];
    if (map.getZoom() >= 7.5) {
      jobs.push(fetchJson("subs", "/api/site-finder/grid?layer=substations" + where).then(function (env) {
        if (map.getSource("sf-subs")) map.getSource("sf-subs").setData(flatten(env));
      }).catch(quiet));
    } else if (map.getSource("sf-subs")) {
      map.getSource("sf-subs").setData(empty());
    }
    return Promise.all(jobs);
  }
  // Texas land parcels in view, from zoom 12 (the API's own floor): outlines only; no owner exists to show.
  function reloadParcels() {
    var src = map.getSource("sf-parcels");
    if (!src) return Promise.resolve();
    if (map.getZoom() < PARCELS_MIN_ZOOM - 0.25) {
      src.setData(empty());
      return Promise.resolve();
    }
    return fetchJson("parcels", "/api/site-finder/parcels?bbox=" + bboxParam() + "&zoom=" + zoomParam()).then(function (env) {
      if (map.getSource("sf-parcels")) map.getSource("sf-parcels").setData((env && env.data) || empty());
    }).catch(quiet);
  }
  function loadView() { return Promise.all([reloadBands(), reloadGrid(), reloadParcels()]); }
  var moveTimer = null;
  map.on("moveend", function () {
    window.clearTimeout(moveTimer);
    moveTimer = window.setTimeout(loadView, 250);
  });

  function setSelected(data) {
    var src = map && map.getSource ? map.getSource("sf-selected") : null;
    if (!src) return;
    var features = [];
    if (data && data.geometry) features.push({ type: "Feature", geometry: data.geometry, properties: { role: "band" } });
    if (data && data.centreline) features.push({ type: "Feature", geometry: data.centreline, properties: { role: "centreline" } });
    src.setData({ type: "FeatureCollection", features: features });
  }

  // A click on a band opens it; on a line or a substation, a card names it.
  var popup = new maplibregl.Popup({ closeButton: false, closeOnClick: true, offset: 8, className: "sf-popup" });
  function voltageWords(kv) { return kv < 0 ? "voltage not stated" : (Math.round(kv * 10) / 10) + " kV"; }
  function card(f, kind) {
    var p = f.properties || {};
    var div = document.createElement("div");
    var strong = document.createElement("strong");
    strong.textContent = p.name || (kind === "substation" ? "Unnamed substation" : "Unnamed line");
    div.appendChild(strong);
    var line = document.createElement("span");
    line.className = "sf-popup__meta";
    line.textContent = (kind === "substation" ? "Substation, " : "Line, ") + voltageWords(Number(p.kv)) + (p.operator ? ", " + p.operator : "");
    div.appendChild(document.createElement("br"));
    div.appendChild(line);
    return div;
  }
  function acresWords(v) {
    var n = Number(v);
    if (!isFinite(n)) return "";
    return (n < 10 ? n.toFixed(1) : Math.round(n).toLocaleString()) + " ac";
  }
  // A parcel's card: its acres, its holding (one owner's contiguous parcels) and the appraisal district's
  // record, where the owner is read. Built from text nodes only.
  function parcelCard(f) {
    var p = f.properties || {};
    var rec = p.appraisal_record;
    if (typeof rec === "string") { try { rec = JSON.parse(rec); } catch (e) { rec = null; } }
    var div = document.createElement("div");
    var strong = document.createElement("strong");
    strong.textContent = "Parcel, " + acresWords(p.acres);
    div.appendChild(strong);
    var meta = document.createElement("span");
    meta.className = "sf-popup__meta";
    var parcels = Number(p.holding_parcels) || 1;
    meta.textContent = "Holding " + acresWords(p.holding_acres) + " in " + parcels + " parcel" + (parcels === 1 ? "" : "s") + (p.land_use_label ? "; " + p.land_use_label : "");
    div.appendChild(document.createElement("br"));
    div.appendChild(meta);
    // Right-of-way strips, exempt and utility land are drawn but left out of the acreage figures.
    var KINDS = { strip: "a right-of-way strip", exempt: "exempt land", utility: "utility land" };
    if (KINDS[p.holding_kind]) {
      var kind = document.createElement("span");
      kind.className = "sf-popup__meta";
      kind.textContent = "Left out of the acreage figures: " + KINDS[p.holding_kind] + ".";
      div.appendChild(document.createElement("br"));
      div.appendChild(kind);
    }
    if (rec && rec.url) {
      div.appendChild(document.createElement("br"));
      var a = document.createElement("a");
      a.href = rec.url;
      a.rel = "noopener nofollow";
      a.target = "_blank";
      a.textContent = rec.kind === "record" ? "Appraisal record" : "Appraisal district" + (rec.property_id ? " (property id " + rec.property_id + ")" : "");
      div.appendChild(a);
    }
    var note = document.createElement("span");
    note.className = "sf-popup__meta";
    note.textContent = "Owner names are on the appraisal district's record; Inframation does not store them.";
    div.appendChild(document.createElement("br"));
    div.appendChild(note);
    return div;
  }
  map.on("click", function (e) {
    var bands = map.queryRenderedFeatures(e.point, { layers: ["sf-bands"] });
    if (bands.length) {
      popup.remove();
      openBand(bands[0].properties.id, { fit: true });
      return;
    }
    var hit = map.queryRenderedFeatures([[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]], { layers: ["sf-subs", "sf-lines", "sf-lines-unknown"] });
    if (hit.length) {
      popup.setLngLat(e.lngLat).setDOMContent(card(hit[0], hit[0].layer.id === "sf-subs" ? "substation" : "line")).addTo(map);
      return;
    }
    var parcel = map.getLayer("sf-parcels-fill") ? map.queryRenderedFeatures(e.point, { layers: ["sf-parcels-fill"] }) : [];
    if (parcel.length) popup.setLngLat(e.lngLat).setDOMContent(parcelCard(parcel[0])).addTo(map);
  });
  map.on("mousemove", function (e) {
    if (coarse.matches) return;
    var hit = map.queryRenderedFeatures([[e.point.x - 3, e.point.y - 3], [e.point.x + 3, e.point.y + 3]], { layers: ["sf-bands", "sf-subs", "sf-lines", "sf-lines-unknown"] });
    map.getCanvas().style.cursor = hit.length ? "pointer" : "";
    var top = hit.filter(function (f) { return f.layer.id !== "sf-bands"; })[0];
    if (top) {
      var key = top.layer.id + ":" + (top.properties.name || "") + ":" + top.properties.kv;
      if (key !== hover) popup.setLngLat(e.lngLat).setDOMContent(card(top, top.layer.id === "sf-subs" ? "substation" : "line")).addTo(map);
      else popup.setLngLat(e.lngLat);
      hover = key;
    } else if (hover) {
      hover = null;
      popup.remove();
    }
  });
  if (narrow.addEventListener) narrow.addEventListener("change", syncExpand);
})();
