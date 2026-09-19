/* Mini-map for the asset and company pages (docs/30 §3.1 asset/company page note, midstream
 * slice 2026-09-19). The page already carries a server-rendered SVG of the same geometry inside
 * `.mini-map` (no JS needed); when this script runs it replaces that SVG with a MapLibre map at
 * a fixed fit of the same features -- lines for pipelines, points for everything else, nearby
 * proposals as small family-coloured dots -- on the shared basemap (basemap.js). If MapLibre or
 * the data block is missing the SVG simply stays.
 */
(function () {
  "use strict";
  var container = document.getElementById("asset-map");
  var dataEl = document.getElementById("asset-map-data");
  var Basemap = window.InfraqueBasemap;
  if (!container || !dataEl || typeof maplibregl === "undefined" || !Basemap) return;

  var data;
  try { data = JSON.parse(dataEl.textContent || "{}"); } catch (e) { return; }
  var features = (data.features || []).filter(function (f) { return f && f.geometry; });
  if (!features.length) return;

  var cssVar = Basemap.cssVar;
  var mapColors = Basemap.mapColors();
  var TYPE_TOKEN = {
    gas_pipeline: "--asset-gas-pipeline", gas_processing_plant: "--asset-gas-processing",
    gas_storage: "--asset-gas-storage", lng_terminal: "--asset-lng-terminal",
    ethanol_plant: "--asset-ethanol", rng_project: "--asset-rng"
  };
  var proposalColor = cssVar("--family-progress-text") || "#2f6480";
  function colorFor(p) {
    if (p.kind === "proposal") return cssVar("--family-" + (p.family || "neutral") + "-text") || proposalColor;
    var token = TYPE_TOKEN[p.asset_type];
    if (token) return cssVar(token) || "#6d6d6d";
    return cssVar("--plant-" + (p.plant_family || "other")) || cssVar("--plant-other") || "#6d6d6d";
  }
  features.forEach(function (f) {
    f.properties = f.properties || {};
    f.properties.color = colorFor(f.properties);
    f.properties.line_class = f.properties.line_class || "unknown";
  });

  function bboxOf(fs) {
    var b = [Infinity, Infinity, -Infinity, -Infinity];
    function take(c) { b[0] = Math.min(b[0], c[0]); b[1] = Math.min(b[1], c[1]); b[2] = Math.max(b[2], c[0]); b[3] = Math.max(b[3], c[1]); }
    function walk(coords) {
      if (typeof coords[0] === "number") take(coords); else coords.forEach(walk);
    }
    fs.forEach(function (f) { walk(f.geometry.coordinates); });
    return b;
  }
  var bbox = bboxOf(features);
  if (!isFinite(bbox[0])) return;
  // A single point (or a very short line) still needs a readable frame: pad to ~0.3 degrees.
  if (bbox[2] - bbox[0] < 0.3) { bbox[0] -= 0.15; bbox[2] += 0.15; }
  if (bbox[3] - bbox[1] < 0.3) { bbox[1] -= 0.15; bbox[3] += 0.15; }

  var TILE_URL = container.getAttribute("data-tile-url") || "";
  var TILE_MODE = container.getAttribute("data-tile-mode") || "dev";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Replace the static SVG with the canvas container only once MapLibre is known to be present.
  container.innerHTML = "";
  container.classList.add("mini-map--live");
  var map = new maplibregl.Map({
    container: container,
    style: Basemap.fallbackStyle(mapColors),
    bounds: [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
    fitBoundsOptions: { padding: 32, maxZoom: 11 },
    attributionControl: false,
    scrollZoom: false,
    dragRotate: false,
    pitchWithRotate: false
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  window.__assetMap = map; // for the browser smoke test only

  var basemapFailed = false;
  function onFail() { basemapFailed = true; }

  map.on("load", function () {
    Basemap.addBasemap(map, { tileMode: TILE_MODE, tileUrl: TILE_URL, colors: mapColors, onFail: onFail });
    map.addSource("asset", { type: "geojson", data: { type: "FeatureCollection", features: features } });
    var isLine = ["==", ["geometry-type"], "LineString"];
    var isAssetPoint = ["all", ["==", ["geometry-type"], "Point"], ["!=", ["get", "kind"], "proposal"]];
    var isProposal = ["all", ["==", ["geometry-type"], "Point"], ["==", ["get", "kind"], "proposal"]];
    var lineWidth = ["interpolate", ["linear"], ["zoom"], 3, 1.2, 6, 2, 9, 3, 12, 4.5];
    map.addLayer({
      id: "asset-lines-casing", type: "line", source: "asset", filter: isLine,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": mapColors.land, "line-width": ["+", lineWidth, 3], "line-opacity": 0.9 }
    });
    map.addLayer({
      id: "asset-lines", type: "line", source: "asset",
      filter: ["all", isLine, ["!=", ["get", "line_class"], "intrastate"]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth }
    });
    map.addLayer({
      id: "asset-lines-intrastate", type: "line", source: "asset",
      filter: ["all", isLine, ["==", ["get", "line_class"], "intrastate"]],
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-dasharray": [3, 2] }
    });
    map.addLayer({
      id: "asset-points", type: "circle", source: "asset", filter: isAssetPoint,
      paint: { "circle-color": ["get", "color"], "circle-radius": 6, "circle-stroke-width": 1.5, "circle-stroke-color": mapColors.land }
    });
    map.addLayer({
      id: "nearby-proposals", type: "circle", source: "asset", filter: isProposal,
      paint: { "circle-color": ["get", "color"], "circle-radius": 4.5, "circle-stroke-width": 1, "circle-stroke-color": "#ffffff" }
    });
    map.addLayer({
      id: "asset-labels", type: "symbol", source: "asset", minzoom: 6,
      filter: ["!=", ["get", "kind"], "proposal"],
      layout: {
        "symbol-placement": ["case", isLine, "line", "point"], "text-field": ["get", "name"], "text-size": 10,
        "text-font": ["Noto Sans Medium"], "text-offset": [0, 1], "text-anchor": "top", "text-optional": true
      },
      paint: { "text-color": ["get", "color"], "text-halo-color": mapColors.land, "text-halo-width": 1.2 }
    });

    var popup = null;
    function esc(v) { return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
    function show(e) {
      var p = e.features[0].properties;
      var html = "<strong>" + esc(p.name) + "</strong>" + (p.subtitle ? "<br>" + esc(p.subtitle) : "");
      if (popup) popup.remove();
      popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 }).setLngLat(e.lngLat).setHTML(html).addTo(map);
    }
    function hide() { if (popup) { popup.remove(); popup = null; } }
    ["asset-lines", "asset-lines-intrastate", "asset-points", "nearby-proposals"].forEach(function (id) {
      map.on("mouseenter", id, function (e) { map.getCanvas().style.cursor = "pointer"; show(e); });
      map.on("mousemove", id, function (e) { if (popup) popup.setLngLat(e.lngLat); });
      map.on("mouseleave", id, function () { map.getCanvas().style.cursor = ""; hide(); });
      map.on("click", id, function (e) {
        var url = e.features[0].properties.url;
        if (url && /^\//.test(String(url))) window.location.href = url;
      });
    });
    if (reduceMotion) map.stop();
  });
})();
