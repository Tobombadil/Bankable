/* Map page (docs/04 D-8...D-15). MapLibre GL JS pinned to 5.24.0 (see home_map.html).
 * Clustering is computed server-side by `GET /v1/proposals/geo` (docs/23 §3.1), proxied through
 * this app's own `/api/proposals/geo` so the browser never needs to know the API's host. This
 * script renders exactly what comes back on every pan/zoom/filter change; it does not cluster.
 */
(function () {
  "use strict";

  var LIFECYCLE_FAMILY = {
    announced: "neutral", unknown: "neutral", closed: "neutral",
    filed: "progress", studied: "progress", permitted: "progress",
    under_construction: "progress", reinstated: "progress",
    contracted: "committed", awarded: "committed",
    built: "success", open: "success",
    withdrawn: "danger", cancelled: "danger", frozen: "danger"
  };
  var FAMILIES = ["neutral", "progress", "committed", "success", "danger"];
  var TECH_LABEL = {
    solar: "SOL", wind: "WND", storage: "BES", wind_storage: "W+S",
    gas: "GAS", nuclear: "NUC", hydro: "HYD", transmission: "TRN",
    geothermal: "GEO", hydrogen: "H2", coal: "COL", other: "OTH"
  };
  // docs/00-PLAN.md 2026-09-14/15 owner decision + task item 2: EIA-860M technology values
  // mapped down to the seven token families the legend and marker fill use. Anything not listed
  // here falls back to "coal" (labelled "Coal/other" in the legend) rather than inventing an
  // eighth colour for every raw EIA fuel code.
  var PLANT_TECH_FAMILY = {
    solar: "solar", wind: "wind", gas: "gas", natural_gas: "gas",
    nuclear: "nuclear", hydro: "hydro", hydroelectric: "hydro",
    storage: "storage", battery: "storage", batteries: "storage",
    coal: "coal", other: "coal"
  };
  var PLANT_TECH_TOKEN = {
    solar: "--plant-solar", wind: "--plant-wind", gas: "--plant-gas",
    nuclear: "--plant-nuclear", hydro: "--plant-hydro", storage: "--plant-storage",
    coal: "--plant-coal"
  };
  var IN_VIEW_LIMIT = 500;
  var WORLD_BBOX = [-179, -85, 179, 85];
  var WORLD_CENTER = [-98.5, 39.8];
  var WORLD_ZOOM = 3.2;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // docs/31 §8: every map animation is instant under prefers-reduced-motion -- MapLibre does not
  // do this on its own, so every flyTo/easeTo/zoomIn/zoomOut call below is given a duration through
  // this helper rather than a literal number.
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function motionMs(ms) { return reduceMotion ? 0 : ms; }

  function familyColors() {
    var out = {};
    FAMILIES.forEach(function (f) { out[f] = cssVar("--family-" + f + "-text") || "#5b6b7c"; });
    return out;
  }

  function plantFamilyColors() {
    var out = {};
    Object.keys(PLANT_TECH_TOKEN).forEach(function (f) {
      out[f] = cssVar(PLANT_TECH_TOKEN[f]) || "#8a8a8a";
    });
    return out;
  }

  function familyOf(state) { return LIFECYCLE_FAMILY[state] || "neutral"; }

  function plantFamilyOf(tech) {
    if (!tech) return "coal";
    return PLANT_TECH_FAMILY[String(tech).toLowerCase()] || "coal";
  }

  function techLabel(tech) {
    if (!tech) return "?";
    return TECH_LABEL[tech] || tech.slice(0, 3).toUpperCase();
  }

  // MapLibre stringifies nested object/array GeoJSON properties when they come back through
  // queryRenderedFeatures; parse defensively so this works whether or not that happened.
  function propObj(value) {
    if (value == null) return null;
    if (typeof value === "string") {
      try { return JSON.parse(value); } catch (e) { return null; }
    }
    return value;
  }

  // ---- measurement (task item 5): navigator.sendBeacon when available, fetch(keepalive) else ----
  function sendUiEvent(name, props) {
    var payload = JSON.stringify({ name: name, props: props || {} });
    if (navigator.sendBeacon) {
      try {
        var ok = navigator.sendBeacon("/api/ui-events", new Blob([payload], { type: "application/json" }));
        if (ok) return;
      } catch (e) { /* fall through to fetch */ }
    }
    try {
      fetch("/api/ui-events", {
        method: "POST", body: payload, keepalive: true,
        headers: { "Content-Type": "application/json" }
      });
    } catch (e) { /* best-effort only -- measurement never blocks the map */ }
  }

  var basemapFailedSent = false;
  function reportBasemapFailedOnce() {
    if (basemapFailedSent) return;
    basemapFailedSent = true;
    sendUiEvent("map.basemap_failed", {});
  }

  function readFilters() {
    var params = new URLSearchParams(window.location.search);
    var layersParam = params.get("layers") || "";
    return {
      technology: params.get("technology") || "",
      jurisdiction: params.get("jurisdiction") || "",
      include_withdrawn: params.get("include_withdrawn") === "1",
      layers: layersParam ? layersParam.split(",").filter(Boolean) : [],
      region: params.get("region") || ""
    };
  }

  function writeFilters(filters) {
    var params = new URLSearchParams();
    if (filters.technology) params.set("technology", filters.technology);
    if (filters.jurisdiction) params.set("jurisdiction", filters.jurisdiction);
    if (filters.include_withdrawn) params.set("include_withdrawn", "1");
    if (filters.layers && filters.layers.length) params.set("layers", filters.layers.join(","));
    if (filters.region) params.set("region", filters.region);
    var qs = params.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "");
    window.history.replaceState(null, "", url);
    var listLink = document.getElementById("mf-view-list");
    if (listLink) listLink.href = "/proposals" + (qs ? "?" + qs : "");
    // Task item 5: the header "Sign in" link carries the current view (chiefly `layers`) as
    // `next` so a sign-up started from the map still knows which layers were on when
    // `web/auth.py::register_submit` posts `auth.registered {layers}` after the round trip.
    var signInLink = document.querySelector('.primary-nav a[href^="/login"]');
    if (signInLink) {
      var here = window.location.pathname + (qs ? "?" + qs : "");
      signInLink.href = "/login?next=" + encodeURIComponent(here);
    }
  }

  function geoUrl(filters, bbox, zoom) {
    var params = new URLSearchParams();
    params.set("bbox", bbox.join(","));
    params.set("zoom", String(zoom));
    if (filters.technology) params.set("technology", filters.technology);
    if (filters.jurisdiction) params.set("jurisdiction", filters.jurisdiction);
    if (filters.include_withdrawn) params.set("include_withdrawn", "1");
    return "/api/proposals/geo?" + params.toString();
  }

  // Task item 2: the `technology` filter applies to plants too; jurisdiction/include_withdrawn do
  // not (plants have no lifecycle state and this context layer is US-only today).
  function plantsGeoUrl(filters, bbox, zoom) {
    var params = new URLSearchParams();
    params.set("bbox", bbox.join(","));
    params.set("zoom", String(zoom));
    if (filters.technology) params.set("technology", filters.technology);
    return "/api/context/plants/geo?" + params.toString();
  }

  function chipHtml(family, label) {
    return (
      '<span class="chip chip--' + family + '">' +
      '<svg class="chip__icon" aria-hidden="true" width="12" height="12"><use href="#icon-' + family + '"></use></svg>' +
      '<span class="chip__label">' + (label || "unknown").replace(/_/g, " ") + "</span></span>"
    );
  }

  var mapEl = document.getElementById("map");
  var TILE_URL = mapEl.getAttribute("data-tile-url") || "";
  var TILE_MODE = mapEl.getAttribute("data-tile-mode") || "dev";

  var filters = readFilters();
  document.getElementById("mf-technology").value = filters.technology;
  document.getElementById("mf-jurisdiction").value = filters.jurisdiction;
  document.getElementById("mf-include-withdrawn").checked = filters.include_withdrawn;
  var plantsToggle = document.getElementById("mf-layer-plants");
  plantsToggle.checked = filters.layers.indexOf("plants") !== -1;
  writeFilters(filters);

  var colors = familyColors();
  var plantColors = plantFamilyColors();
  var mapColors = {
    land: cssVar("--map-land") || "#eae6da",
    water: cssVar("--map-water") || "#cfe0e8",
    border: cssVar("--map-border") || "#b9c4c9"
  };

  // Same-origin fallback basemap (docs/04 D-13, web/README.md "Map basemap"): the tile/pmtiles
  // source is added only after `load` fires (below) so a blocked/slow/failed basemap source can
  // never keep the whole style from ever loading -- this fallback is always in the initial style
  // and is what the user sees whenever the real basemap fails, whichever of the three modes below
  // is configured.
  var map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        "basemap-fallback": { type: "geojson", data: "/static/data/basemap_fallback.geojson" }
      },
      layers: [
        { id: "fallback-water", type: "background", paint: { "background-color": mapColors.water } },
        {
          id: "fallback-land", type: "fill", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "country"],
          paint: { "fill-color": mapColors.land }
        },
        {
          id: "fallback-country-lines", type: "line", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "country"],
          paint: { "line-color": mapColors.border, "line-width": 0.6 }
        },
        {
          id: "fallback-state-lines", type: "line", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "us_state"],
          paint: { "line-color": mapColors.border, "line-width": 0.5 }
        }
      ]
    },
    center: WORLD_CENTER,
    zoom: WORLD_ZOOM,
    attributionControl: false
  });
  map.dragRotate.disable();
  map.touchZoomRotate.disableRotation();
  window.__map = map; // exposed for the Playwright smoke test only
  window.__mapIdle = false;
  map.once("idle", function () { window.__mapIdle = true; });

  document.getElementById("zoom-in").addEventListener("click", function () { map.zoomIn({ duration: motionMs(200) }); });
  document.getElementById("zoom-out").addEventListener("click", function () { map.zoomOut({ duration: motionMs(200) }); });
  document.getElementById("zoom-reset").addEventListener("click", function () {
    map.flyTo({ center: WORLD_CENTER, zoom: WORLD_ZOOM, duration: motionMs(600) });
  });

  // ---- task item 3: regional quick views ----
  document.querySelectorAll(".region-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var bbox = btn.getAttribute("data-bbox").split(",").map(Number);
      var code = btn.getAttribute("data-region");
      map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { duration: motionMs(600), padding: 24 });
      filters.region = code;
      writeFilters(filters);
      sendUiEvent("map.region_jumped", { region: code });
    });
  });
  // Restore a `region=` view from a shared/reloaded URL on load, instantly (this is page setup,
  // not a user-initiated jump, so it does not re-emit `map.region_jumped`).
  if (filters.region) {
    var restoreBtn = document.querySelector('.region-btn[data-region="' + filters.region + '"]');
    if (restoreBtn) {
      var restoreBbox = restoreBtn.getAttribute("data-bbox").split(",").map(Number);
      map.jumpTo({ center: [(restoreBbox[0] + restoreBbox[2]) / 2, (restoreBbox[1] + restoreBbox[3]) / 2] });
      map.fitBounds([[restoreBbox[0], restoreBbox[1]], [restoreBbox[2], restoreBbox[3]]], { duration: 0, padding: 24 });
    }
  }

  var latestCollection = { type: "FeatureCollection", features: [], totals: {} };
  var latestMeta = {};
  var latestPlantsTotal = 0;

  function currentBbox() {
    var b = map.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  }
  function currentZoom() { return Math.round(map.getZoom()); }

  var countEl = document.getElementById("map-result-count");
  var listEl = document.getElementById("in-view-items");
  var liveRegion = document.getElementById("map-live-region");
  var template = document.getElementById("in-view-item-template");
  var unplacedNote = document.getElementById("unplaced-note");
  var plantsLegend = document.getElementById("plants-legend");

  function refetch() {
    fetch(geoUrl(filters, currentBbox(), currentZoom()))
      .then(function (r) { return r.json(); })
      .then(function (envelope) {
        var fc = envelope.data;
        fc.features.forEach(function (f) {
          if (f.properties.feature_kind === "cluster") {
            f.properties.family = familyOf(f.properties.dominant_lifecycle_state);
          } else {
            f.properties.family = familyOf(f.properties.lifecycle_state);
            f.properties.tech_label = techLabel(f.properties.technology);
          }
        });
        latestCollection = fc;
        latestMeta = envelope.meta || {};
        if (map.getSource("proposals")) map.getSource("proposals").setData(fc);
        render();
      })
      .catch(function () {
        // docs/31 §6 error state: keep the last-known view rather than blanking it.
      });
    if (plantsToggle.checked) refetchPlants();
  }

  function refetchPlants() {
    fetch(plantsGeoUrl(filters, currentBbox(), currentZoom()))
      .then(function (r) { return r.json(); })
      .then(function (envelope) {
        var fc = envelope.data;
        fc.features.forEach(function (f) {
          if (f.properties.feature_kind === "plant_cluster") {
            f.properties.plant_family = plantFamilyOf(f.properties.dominant_technology);
          } else {
            f.properties.plant_family = plantFamilyOf(f.properties.technology);
          }
        });
        latestPlantsTotal = (fc.totals || {}).records || 0;
        if (map.getSource("plants")) map.getSource("plants").setData(fc);
        render();
      })
      .catch(function () {
        // Same error-state rule as the proposals fetch: keep whatever was last drawn.
      });
  }

  function render() {
    var totals = latestCollection.totals || {};
    countEl.textContent =
      (totals.records || 0) + " proposal" + (totals.records === 1 ? "" : "s") + " match these filters" +
      (totals.clustered ? " (grouped into clusters at this zoom)." : ".");
    countEl.removeAttribute("aria-hidden");

    var individual = latestCollection.features.filter(function (f) {
      return f.properties.feature_kind !== "cluster";
    });
    listEl.innerHTML = "";
    if (totals.clustered) {
      var li = document.createElement("li");
      li.textContent = "Zoom in or narrow the filters to list proposals individually; " +
        (totals.records || 0) + " match in clustered form above.";
      listEl.appendChild(li);
    } else {
      individual.slice(0, IN_VIEW_LIMIT).forEach(function (f) {
        var p = f.properties;
        var node = template.content.cloneNode(true);
        node.querySelector(".chip-slot").innerHTML = chipHtml(p.family, p.lifecycle_state);
        var a = node.querySelector(".name-link");
        a.textContent = p.name;
        a.href = p.url || "#";
        var meta = (p.technology || "—") + " · " + (p.state_code || p.county_name || "—") +
          (p.capacity_mw ? " · " + p.capacity_mw.toFixed(1) + " MW" : "");
        node.querySelector(".meta").textContent = meta;
        listEl.appendChild(node);
      });
    }
    // Task item 2: "Live region adds 'N existing plants in view'" -- appended as a second
    // sentence so the proposals count (the accessible path's primary content) is never dropped
    // when the plants layer is on.
    var liveText = (totals.records || 0) + " proposals in view.";
    if (plantsToggle.checked) {
      liveText += " " + latestPlantsTotal + " existing plants in view.";
    }
    liveRegion.textContent = liveText;

    var unplacedCount = latestMeta.unplaced_count || 0;
    if (unplacedCount > 0) {
      unplacedNote.hidden = false;
      unplacedNote.textContent = "Unplaced (" + unplacedCount + "): no usable county or state on these sources' records; view them in the list instead of on the map.";
    } else {
      unplacedNote.hidden = true;
    }
  }

  // ---- basemap (task item 1: three MAP_TILE_URL modes) ----
  function addRasterBasemap(template_) {
    map.addSource("osm", {
      type: "raster",
      tiles: [template_],
      tileSize: 256,
      attribution: "&copy; OpenStreetMap contributors"
    });
    // Tinted toward the paper ground and desaturated so tile land/water/borders read as a quiet
    // backdrop the data sits on top of, not a competing full-colour basemap (docs/30 §7 "Basemap").
    map.addLayer({
      id: "osm", type: "raster", source: "osm",
      paint: { "raster-saturation": -0.75, "raster-brightness-min": 0.35, "raster-brightness-max": 1, "raster-contrast": -0.1 }
    });
    map.on("error", function (e) {
      if (e && e.sourceId === "osm") reportBasemapFailedOnce();
    });
  }

  function addPmtilesBasemap() {
    if (typeof pmtiles === "undefined" || typeof basemaps === "undefined") {
      // One or both CDN scripts failed to load (home_map.html only includes them in pmtiles
      // mode) -- the fallback outline layer already in the initial style is what the user sees.
      reportBasemapFailedOnce();
      return;
    }
    try {
      var protocol = new pmtiles.Protocol();
      maplibregl.addProtocol("pmtiles", protocol.tile);
      map.addSource("protomaps", {
        type: "vector",
        url: "pmtiles://" + TILE_URL,
        attribution: "&copy; OpenStreetMap contributors"
      });
      var flavor = basemaps.namedFlavor("light");
      // Mute land/water/roads to the paper ground the same way the raster layer is tinted above,
      // via token-derived colours rather than the flavor's own defaults (task item 1).
      flavor.background = mapColors.land;
      flavor.earth = mapColors.land;
      flavor.water = mapColors.water;
      flavor.major = mapColors.border;
      flavor.minor_a = mapColors.border;
      flavor.minor_b = mapColors.border;
      flavor.highway = mapColors.border;
      flavor.link = mapColors.border;
      flavor.boundaries = mapColors.border;
      var styleLayers = basemaps.layers("protomaps", flavor, { lang: "en" });
      styleLayers.forEach(function (layer) { map.addLayer(layer); });
      map.on("error", function (e) {
        if (e && e.sourceId === "protomaps") reportBasemapFailedOnce();
      });
    } catch (e) {
      reportBasemapFailedOnce();
    }
  }

  function addBasemap() {
    if (TILE_MODE === "pmtiles") {
      addPmtilesBasemap();
    } else if (TILE_MODE === "raster") {
      addRasterBasemap(TILE_URL);
    } else {
      addRasterBasemap("https://tile.openstreetmap.org/{z}/{x}/{y}.png");
    }
  }

  // ---- plants context layer (task item 2) ----
  function buildSquareIcon(size) {
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, size, size);
    return ctx.getImageData(0, 0, size, size);
  }

  function addPlantsLayers() {
    map.addImage("plant-square", buildSquareIcon(8), { sdf: true });
    map.addSource("plants", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

    var plantColorExpr = [
      "match", ["get", "plant_family"],
      "solar", plantColors.solar, "wind", plantColors.wind, "gas", plantColors.gas,
      "nuclear", plantColors.nuclear, "hydro", plantColors.hydro, "storage", plantColors.storage,
      "coal", plantColors.coal, plantColors.coal
    ];

    // Beneath the proposals layers (`addLayer(..., "clusters")`, task item 2): inserted right
    // above the basemap and below every proposals layer added further down.
    map.addLayer({
      id: "plant-clusters", type: "circle", source: "plants",
      filter: ["==", ["get", "feature_kind"], "plant_cluster"],
      paint: {
        "circle-radius": ["step", ["get", "count"], 10, 10, 14, 50, 18],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-width": 1,
        "circle-stroke-color": plantColorExpr,
        "circle-stroke-opacity": 0.6
      }
    }, "clusters");
    map.addLayer({
      id: "plant-cluster-count", type: "symbol", source: "plants",
      filter: ["==", ["get", "feature_kind"], "plant_cluster"],
      layout: { "text-field": ["get", "count"], "text-size": 10, "text-font": ["Noto Sans Bold"] },
      paint: { "text-color": plantColorExpr, "text-opacity": 0.7 }
    }, "clusters");
    map.addLayer({
      id: "plant-points", type: "symbol", source: "plants",
      filter: ["==", ["get", "feature_kind"], "plant"],
      layout: { "icon-image": "plant-square", "icon-size": 0.55, "icon-allow-overlap": true },
      paint: { "icon-color": plantColorExpr, "icon-opacity": 0.6 }
    }, "clusters");

    map.on("click", "plant-points", function (e) { openPlantDrawer(e.features[0].properties); });
    map.on("mouseenter", "plant-points", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "plant-points", function () { map.getCanvas().style.cursor = ""; });
  }

  function setPlantsLayerVisible(on) {
    ["plant-clusters", "plant-cluster-count", "plant-points"].forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    });
    plantsLegend.hidden = !on;
    plantsLegend.setAttribute("aria-hidden", on ? "false" : "true");
  }

  map.on("load", function () {
    addBasemap();

    map.addSource("proposals", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

    var colorExpr = [
      "match", ["get", "family"],
      "neutral", colors.neutral, "progress", colors.progress, "committed", colors.committed,
      "success", colors.success, "danger", colors.danger, colors.neutral
    ];

    // Clusters render as thin rings on the paper ground, not filled discs -- the count is read
    // through the ring, the family colour carried by the ring stroke and the count text alone
    // (docs/30 §7 "Zoom and clustering"; task: "thin-ring counts on the navy family").
    map.addLayer({
      id: "clusters", type: "circle", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "cluster"],
      paint: {
        "circle-color": mapColors.land,
        "circle-radius": ["step", ["get", "count"], 14, 10, 18, 50, 24, 200, 32],
        "circle-stroke-width": 2, "circle-stroke-color": colorExpr
      }
    });
    // Plants layers are added here -- after "clusters" exists (`addLayer(..., "clusters")`
    // requires the reference layer to already be in the style) but before the remaining
    // proposals layers, so plants render beneath every proposals layer, clusters included.
    addPlantsLayers();
    setPlantsLayerVisible(plantsToggle.checked);
    map.addLayer({
      id: "cluster-count", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "cluster"],
      layout: { "text-field": ["get", "count"], "text-size": 12, "text-font": ["Noto Sans Bold"] },
      paint: { "text-color": colorExpr }
    });
    map.addLayer({
      id: "points", type: "circle", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "proposal"],
      paint: {
        "circle-color": colorExpr,
        "circle-radius": 7,
        "circle-stroke-width": ["case", ["==", ["get", "precision_reason"], "licence"], 2, 1],
        "circle-stroke-color": "#ffffff"
      }
    });
    map.addLayer({
      id: "point-labels", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "proposal"],
      layout: { "text-field": ["get", "tech_label"], "text-size": 8, "text-font": ["Noto Sans Bold"] },
      paint: { "text-color": "#ffffff" }
    });

    map.on("click", "clusters", function (e) {
      var f = map.queryRenderedFeatures(e.point, { layers: ["clusters"] })[0];
      var targetZoom = f.properties.expands_to_zoom || (map.getZoom() + 2);
      map.easeTo({ center: f.geometry.coordinates, zoom: targetZoom, duration: motionMs(600) });
    });
    map.on("mouseenter", "clusters", function (e) { map.getCanvas().style.cursor = "pointer"; showClusterTooltip(e); });
    map.on("mouseleave", "clusters", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("click", "points", function (e) { openDrawer(e.features[0].properties); });
    map.on("mouseenter", "points", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "points", function () { map.getCanvas().style.cursor = ""; });

    var moveTimer = null;
    map.on("moveend", function () { window.clearTimeout(moveTimer); moveTimer = window.setTimeout(refetch, 200); });
    refetch();
  });

  var tooltip = null;
  function showClusterTooltip(e) {
    var f = e.features[0];
    var p = f.properties;
    var counts = propObj(p.lifecycle_state_counts) || {};
    var lines = Object.keys(counts).sort().map(function (k) { return k.replace(/_/g, " ") + " " + counts[k]; });
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(f.geometry.coordinates)
      .setHTML(
        "<strong>" + p.count + " proposals</strong><br>" + lines.join(" &middot; ") +
        (p.capacity_mw_sum ? "<br>Capacity sum: " + Math.round(p.capacity_mw_sum).toLocaleString() + " MW" : "")
      )
      .addTo(map);
  }
  function hideTooltip() { if (tooltip) { tooltip.remove(); tooltip = null; } }

  // ---- filters ----
  function applyFilters() {
    filters = {
      technology: document.getElementById("mf-technology").value,
      jurisdiction: document.getElementById("mf-jurisdiction").value,
      include_withdrawn: document.getElementById("mf-include-withdrawn").checked,
      layers: filters.layers,
      region: filters.region
    };
    writeFilters(filters);
    refetch();
  }
  document.getElementById("mf-technology").addEventListener("change", applyFilters);
  document.getElementById("mf-jurisdiction").addEventListener("change", applyFilters);
  document.getElementById("mf-include-withdrawn").addEventListener("change", applyFilters);
  plantsToggle.addEventListener("change", function () {
    var on = plantsToggle.checked;
    filters.layers = on ? ["plants"] : [];
    writeFilters(filters);
    setPlantsLayerVisible(on);
    sendUiEvent("map.layer_toggled", { layer: "plants", on: on });
    if (on) refetchPlants();
    render();
  });
  document.getElementById("mf-clear").addEventListener("click", function () {
    document.getElementById("mf-technology").value = "";
    document.getElementById("mf-jurisdiction").value = "";
    document.getElementById("mf-include-withdrawn").checked = false;
    applyFilters();
  });

  // ---- drawer (D-12) ----
  var drawer = buildDrawer();
  var lastFocused = null;
  function openDrawer(rawProps) {
    lastFocused = document.activeElement;
    var provenance = propObj(rawProps.provenance) || [];
    drawer.render(rawProps, provenance[0] || null);
    drawer.open();
  }
  function openPlantDrawer(rawProps) {
    lastFocused = document.activeElement;
    drawer.renderPlant(rawProps);
    drawer.open();
  }
  function buildDrawer() {
    var el = document.createElement("div");
    el.id = "map-drawer";
    el.className = "map-drawer";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "Record detail");
    el.innerHTML = '<button type="button" id="drawer-close" class="map-drawer__close" aria-label="Close">&times; Close</button><div id="drawer-body"></div>';
    document.body.appendChild(el);
    var body = el.querySelector("#drawer-body");
    var closeBtn = el.querySelector("#drawer-close");
    closeBtn.addEventListener("click", close);
    el.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
      if (e.key === "Tab") {
        var focusables = el.querySelectorAll("button, a[href]");
        var first = focusables[0], last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    // Open/close duration comes from CSS (docs/31 §1.5 --motion-base/--motion-fast): adding
    // `is-open` transitions in at 200ms, removing it transitions out at 150ms (the base rule's own
    // duration) -- see the .map-drawer / .map-drawer.is-open rule in styles.css.
    function open() { el.classList.add("is-open"); closeBtn.focus(); }
    function close() {
      el.classList.remove("is-open");
      if (lastFocused) lastFocused.focus();
    }
    function render(p, source) {
      body.innerHTML =
        "<h2>" + p.name + "</h2>" + chipHtml(familyOf(p.lifecycle_state), p.lifecycle_state) +
        "<dl class=\"drawer-fields\">" +
        "<div class=\"drawer-fields__row\"><dt>Technology</dt><dd>" + (p.technology || "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>Capacity</dt><dd class=\"tnum\">" + (p.capacity_mw ? p.capacity_mw.toFixed(1) + " MW" : "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>Location</dt><dd>" + (p.county_name || "—") + ", " + (p.state_code || "—") +
        (p.precision_note ? " (" + p.precision_note + ")" : "") + "</dd></div>" +
        "</dl>" +
        (source
          ? "<p class=\"drawer-source\"><span class=\"drawer-source__label\">Source</span>" +
            "<a href=\"" + source.source_url + "\" rel=\"noopener nofollow\">" + source.source_name + "</a>, retrieved " +
            "<span class=\"tnum\">" + (source.retrieved_at ? source.retrieved_at.slice(0, 10) : "unknown") + "</span></p>"
          : "") +
        "<a class=\"drawer-open-link\" href=\"" + (p.url || "#") + "\">Open full record &rarr;</a>";
    }
    // Task item 2's drawer content for a plant: name, operator, technology split table, capacity,
    // first operating year, source line (name, retrieved date, licence) -- no "Open full record"
    // link, since a context-layer plant has no record page on this site.
    function renderPlant(p) {
      var techs = propObj(p.technologies) || {};
      var techRows = Object.keys(techs).sort().map(function (k) {
        return "<div class=\"drawer-fields__row\"><dt>" + k.replace(/_/g, " ") + "</dt><dd class=\"tnum\">" +
          Number(techs[k]).toFixed(1) + " MW</dd></div>";
      }).join("");
      var source = propObj(p.source);
      body.innerHTML =
        "<h2>" + p.name + "</h2>" +
        "<p class=\"reuse-badge\">Existing plant &middot; context layer</p>" +
        "<dl class=\"drawer-fields\">" +
        "<div class=\"drawer-fields__row\"><dt>Operator</dt><dd>" + (p.operator_name || "—") + "</dd></div>" +
        (techRows || "<div class=\"drawer-fields__row\"><dt>Technology</dt><dd>" + (p.technology || "—") + "</dd></div>") +
        "<div class=\"drawer-fields__row\"><dt>Capacity</dt><dd class=\"tnum\">" + (p.capacity_mw ? Number(p.capacity_mw).toFixed(1) + " MW" : "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>First operating year</dt><dd class=\"tnum\">" + (p.earliest_operating_year || "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>Location</dt><dd>" + (p.county_name || "—") + ", " + (p.state_code || "—") + "</dd></div>" +
        "</dl>" +
        (source
          ? "<p class=\"drawer-source\"><span class=\"drawer-source__label\">Source</span>" +
            "<a href=\"" + source.source_url + "\" rel=\"noopener nofollow\">" + source.source_name + "</a>, retrieved " +
            "<span class=\"tnum\">" + (source.retrieved_at ? source.retrieved_at.slice(0, 10) : "unknown") + "</span>" +
            (source.licence_name ? " &middot; " + source.licence_name : "") + "</p>"
          : "");
    }
    return { open: open, close: close, render: render, renderPlant: renderPlant };
  }
})();
