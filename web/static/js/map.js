/* Map page (docs/04 D-8...D-15). MapLibre GL JS pinned to 5.24.0 (see home_map.html).
 * All proposal data is already loaded as static JSON (web/build_data.py output) -- there is no
 * `/v1/proposals/geo` service this sprint, so clustering happens client-side via MapLibre's
 * built-in cluster support rather than server-computed clusters. Documented in web/README.md.
 */
(function () {
  "use strict";

  var FAMILIES = ["neutral", "progress", "committed", "success", "danger"];
  var TECH_LABEL = {
    solar: "SOL", wind: "WND", storage: "BES", wind_storage: "W+S",
    gas: "GAS", nuclear: "NUC", hydro: "HYD", transmission: "TRN",
    geothermal: "GEO", hydrogen: "H2", other: "OTH"
  };
  var IN_VIEW_LIMIT = 500;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function familyColors() {
    var out = {};
    FAMILIES.forEach(function (f) { out[f] = cssVar("--family-" + f + "-text") || "#5b6b7c"; });
    return out;
  }

  function techLabel(tech) {
    if (!tech) return "?";
    return TECH_LABEL[tech] || tech.slice(0, 3).toUpperCase();
  }

  function readFilters() {
    var params = new URLSearchParams(window.location.search);
    return {
      technology: params.get("technology") || "",
      lifecycle_state: params.get("lifecycle_state") || "",
      jurisdiction: params.get("jurisdiction") || ""
    };
  }

  function writeFilters(filters) {
    var params = new URLSearchParams();
    Object.keys(filters).forEach(function (k) { if (filters[k]) params.set(k, filters[k]); });
    var qs = params.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "");
    window.history.replaceState(null, "", url);
    var listLink = document.getElementById("mf-view-list");
    if (listLink) listLink.href = "/proposals" + (qs ? "?" + qs : "");
  }

  function matchesFilters(props, filters) {
    if (filters.technology && props.technology !== filters.technology) return false;
    if (filters.lifecycle_state && props.lifecycle_state !== filters.lifecycle_state) return false;
    if (filters.jurisdiction && props.state !== filters.jurisdiction) return false;
    return true;
  }

  function chipHtml(family, label) {
    return (
      '<span class="chip chip--' + family + '">' +
      '<svg class="chip__icon" aria-hidden="true" width="12" height="12"><use href="#icon-' + family + '"></use></svg>' +
      '<span class="chip__label">' + label.replace(/_/g, " ") + "</span></span>"
    );
  }

  fetch("/static/data/proposals.geojson")
    .then(function (r) { return r.json(); })
    .then(function (geojson) {
      fetch("/static/data/stats.json")
        .then(function (r) { return r.json(); })
        .then(function (stats) { init(geojson, stats); });
    });

  function init(geojson, stats) {
    var allFeatures = geojson.features.filter(function (f) { return f.properties.feature_type === "proposal"; });
    var aggFeatures = geojson.features.filter(function (f) { return f.properties.feature_type === "state_aggregate"; });
    var colors = familyColors();
    var filters = readFilters();

    document.getElementById("mf-technology").value = filters.technology;
    document.getElementById("mf-lifecycle").value = filters.lifecycle_state;
    document.getElementById("mf-jurisdiction").value = filters.jurisdiction;
    writeFilters(filters);

    var map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "&copy; OpenStreetMap contributors"
          }
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }]
      },
      center: [-98.5, 39.8],
      zoom: 3.2,
      attributionControl: false
    });
    map.dragRotate.disable();
    map.touchZoomRotate.disableRotation();
    window.__map = map; // exposed for the Playwright smoke test only

    document.getElementById("zoom-in").addEventListener("click", function () { map.zoomIn({ duration: 200 }); });
    document.getElementById("zoom-out").addEventListener("click", function () { map.zoomOut({ duration: 200 }); });
    document.getElementById("zoom-reset").addEventListener("click", function () {
      map.flyTo({ center: [-98.5, 39.8], zoom: 3.2, duration: 600 });
    });

    function filteredCollection() {
      return {
        type: "FeatureCollection",
        features: allFeatures.filter(function (f) { return matchesFilters(f.properties, filters); })
      };
    }

    map.on("load", function () {
      map.addSource("proposals", {
        type: "geojson",
        data: filteredCollection(),
        cluster: true,
        clusterRadius: 50,
        clusterMaxZoom: 11,
        clusterProperties: {
          neutral_count: ["+", ["case", ["==", ["get", "lifecycle_family"], "neutral"], 1, 0]],
          progress_count: ["+", ["case", ["==", ["get", "lifecycle_family"], "progress"], 1, 0]],
          committed_count: ["+", ["case", ["==", ["get", "lifecycle_family"], "committed"], 1, 0]],
          success_count: ["+", ["case", ["==", ["get", "lifecycle_family"], "success"], 1, 0]],
          danger_count: ["+", ["case", ["==", ["get", "lifecycle_family"], "danger"], 1, 0]],
          capacity_mw_sum: ["+", ["coalesce", ["get", "capacity_mw"], 0]]
        }
      });
      map.addSource("state-aggregates", { type: "geojson", data: { type: "FeatureCollection", features: aggFeatures } });

      var maxExpr = [
        "max",
        ["get", "neutral_count"], ["get", "progress_count"], ["get", "committed_count"],
        ["get", "success_count"], ["get", "danger_count"]
      ];
      var dominantColor = [
        "case",
        ["==", ["get", "danger_count"], maxExpr], colors.danger,
        ["==", ["get", "committed_count"], maxExpr], colors.committed,
        ["==", ["get", "success_count"], maxExpr], colors.success,
        ["==", ["get", "progress_count"], maxExpr], colors.progress,
        colors.neutral
      ];

      map.addLayer({
        id: "clusters", type: "circle", source: "proposals", filter: ["has", "point_count"],
        paint: {
          "circle-color": dominantColor,
          "circle-radius": ["step", ["get", "point_count"], 14, 10, 18, 50, 24, 200, 32],
          "circle-stroke-width": 2, "circle-stroke-color": "#ffffff"
        }
      });
      map.addLayer({
        id: "cluster-count", type: "symbol", source: "proposals", filter: ["has", "point_count"],
        layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 12, "text-font": ["Noto Sans Bold"] },
        paint: { "text-color": "#ffffff" }
      });
      map.addLayer({
        id: "points", type: "circle", source: "proposals", filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-color": ["match", ["get", "lifecycle_family"],
            "neutral", colors.neutral, "progress", colors.progress, "committed", colors.committed,
            "success", colors.success, "danger", colors.danger, colors.neutral],
          "circle-radius": 7,
          "circle-stroke-width": ["case", ["get", "restricted_precision"], 2, 1],
          "circle-stroke-color": "#ffffff"
        }
      });
      map.addLayer({
        id: "point-labels", type: "symbol", source: "proposals", filter: ["!", ["has", "point_count"]],
        layout: { "text-field": ["get", "tech_label"], "text-size": 8, "text-offset": [0, 0], "text-font": ["Noto Sans Bold"] },
        paint: { "text-color": "#ffffff" }
      });
      map.addLayer({
        id: "state-agg", type: "circle", source: "state-aggregates",
        paint: { "circle-color": colors.neutral, "circle-radius": 10, "circle-stroke-width": 2, "circle-stroke-color": colors.committed }
      });
      map.addLayer({
        id: "state-agg-label", type: "symbol", source: "state-aggregates",
        layout: { "text-field": ["get", "count"], "text-size": 11, "text-font": ["Noto Sans Bold"] },
        paint: { "text-color": "#ffffff" }
      });

      // tech_label is derived client-side (not worth a build_data.py column for a display-only string)
      var withLabels = filteredCollection();
      withLabels.features.forEach(function (f) { f.properties.tech_label = techLabel(f.properties.technology); });
      map.getSource("proposals").setData(withLabels);

      map.on("click", "clusters", function (e) {
        var features = map.queryRenderedFeatures(e.point, { layers: ["clusters"] });
        var clusterId = features[0].properties.cluster_id;
        map.getSource("proposals").getClusterExpansionZoom(clusterId).then(function (zoom) {
          map.easeTo({ center: features[0].geometry.coordinates, zoom: zoom, duration: 600 });
        });
      });
      map.on("mouseenter", "clusters", function () { map.getCanvas().style.cursor = "pointer"; showClusterTooltip.apply(null, arguments); });
      map.on("mouseleave", "clusters", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
      map.on("click", "points", function (e) { openDrawer(e.features[0].properties); });
      map.on("mouseenter", "points", function () { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "points", function () { map.getCanvas().style.cursor = ""; });
      map.on("click", "state-agg", function (e) { openStateDrawer(e.features[0].properties); });

      var moveTimer = null;
      map.on("moveend", function () { window.clearTimeout(moveTimer); moveTimer = window.setTimeout(updateInView, 200); });
      updateInView();
    });

    var tooltip = null;
    function showClusterTooltip(e) {
      var f = e.features[0];
      var p = f.properties;
      hideTooltip();
      tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
        .setLngLat(f.geometry.coordinates)
        .setHTML(
          "<strong>" + p.point_count + " proposals</strong><br>" +
          "Neutral " + p.neutral_count + " &middot; Progress " + p.progress_count + " &middot; " +
          "Committed " + p.committed_count + " &middot; Success " + p.success_count + " &middot; Danger " + p.danger_count +
          "<br>Capacity sum: " + Math.round(p.capacity_mw_sum).toLocaleString() + " MW"
        )
        .addTo(map);
    }
    function hideTooltip() { if (tooltip) { tooltip.remove(); tooltip = null; } }

    // ---- filters ----
    function applyFilters() {
      filters = {
        technology: document.getElementById("mf-technology").value,
        lifecycle_state: document.getElementById("mf-lifecycle").value,
        jurisdiction: document.getElementById("mf-jurisdiction").value
      };
      writeFilters(filters);
      var coll = filteredCollection();
      coll.features.forEach(function (f) { f.properties.tech_label = techLabel(f.properties.technology); });
      if (map.getSource("proposals")) map.getSource("proposals").setData(coll);
      updateInView();
    }
    document.getElementById("mf-technology").addEventListener("change", applyFilters);
    document.getElementById("mf-lifecycle").addEventListener("change", applyFilters);
    document.getElementById("mf-jurisdiction").addEventListener("change", applyFilters);
    document.getElementById("mf-clear").addEventListener("click", function () {
      document.getElementById("mf-technology").value = "";
      document.getElementById("mf-lifecycle").value = "";
      document.getElementById("mf-jurisdiction").value = "";
      applyFilters();
    });

    // ---- accessible in-view list (D-14): computed from the filtered feature array + current
    // bounds, independent of what MapLibre has clustered/rendered, so it always names every
    // matching record rather than only the ones drawn as individual glyphs. ----
    var listEl = document.getElementById("in-view-items");
    var liveRegion = document.getElementById("map-live-region");
    var countEl = document.getElementById("map-result-count");
    var template = document.getElementById("in-view-item-template");

    function updateInView() {
      var bounds = map.getBounds();
      var inView = allFeatures.filter(function (f) {
        var p = f.properties;
        if (!matchesFilters(p, filters)) return false;
        var lng = f.geometry.coordinates[0], lat = f.geometry.coordinates[1];
        return bounds.getWest() <= lng && lng <= bounds.getEast() && bounds.getSouth() <= lat && lat <= bounds.getNorth();
      });
      countEl.textContent = filteredCollection().features.length + " proposals match these filters.";
      countEl.removeAttribute("aria-hidden");
      listEl.innerHTML = "";
      if (inView.length > IN_VIEW_LIMIT) {
        var li = document.createElement("li");
        li.textContent = "~" + inView.length + " proposals in view -- zoom in or narrow the filters to list them individually.";
        listEl.appendChild(li);
      } else {
        inView.slice(0, IN_VIEW_LIMIT).forEach(function (f) {
          var p = f.properties;
          var node = template.content.cloneNode(true);
          node.querySelector(".chip-slot").innerHTML = chipHtml(p.lifecycle_family, p.lifecycle_state);
          var a = node.querySelector(".name-link");
          a.textContent = p.name;
          a.href = "/proposals/" + p.slug;
          var meta = (p.technology || "—") + " · " + (p.state || p.jurisdiction || "—") +
            (p.capacity_mw ? " · " + p.capacity_mw.toFixed(1) + " MW" : "");
          node.querySelector(".meta").textContent = meta;
          listEl.appendChild(node);
        });
      }
      liveRegion.textContent = inView.length + " proposals in view.";
    }

    // ---- unplaced (D-8): always listed, independent of map viewport ----
    var unplacedContainer = document.getElementById("unplaced-container");
    (stats.unplaced || []).forEach(function (group) {
      var details = document.createElement("details");
      details.className = "unplaced-group";
      var summary = document.createElement("summary");
      summary.textContent = "Unplaced (" + group.group_label + "): " + group.count;
      details.appendChild(summary);
      var ul = document.createElement("ul");
      ul.className = "in-view-list";
      group.records.slice(0, 200).forEach(function (r) {
        var li = document.createElement("li");
        li.innerHTML = chipHtml(r.lifecycle_family, r.lifecycle_state) +
          ' <a href="/proposals/' + r.slug + '">' + r.name + "</a>" +
          '<span class="meta">' + (r.technology || "—") +
          (r.capacity_mw ? " · " + r.capacity_mw.toFixed(1) + " MW" : "") + "</span>";
        ul.appendChild(li);
      });
      details.appendChild(ul);
      unplacedContainer.appendChild(details);
    });

    // ---- drawer (D-12) ----
    var drawer = buildDrawer();
    var lastFocused = null;
    function openDrawer(p) {
      lastFocused = document.activeElement;
      drawer.render(p);
      drawer.open();
    }
    function openStateDrawer(p) {
      lastFocused = document.activeElement;
      drawer.renderState(p);
      drawer.open();
    }
    function buildDrawer() {
      var el = document.createElement("div");
      el.id = "map-drawer";
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-modal", "true");
      el.setAttribute("aria-label", "Record detail");
      el.style.cssText =
        "position:fixed;top:0;right:0;height:100%;width:min(24rem,90vw);background:var(--surface);" +
        "border-left:1px solid var(--border);box-shadow:var(--shadow-1);padding:1.5rem;overflow-y:auto;" +
        "transform:translateX(100%);transition:transform var(--motion-base) ease-out;z-index:50;";
      el.innerHTML = '<button type="button" id="drawer-close" aria-label="Close">&times; Close</button><div id="drawer-body"></div>';
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
      function open() { el.style.transform = "translateX(0)"; closeBtn.focus(); }
      function close() {
        el.style.transform = "translateX(100%)";
        if (lastFocused) lastFocused.focus();
      }
      function render(p) {
        body.innerHTML =
          "<h2>" + p.name + "</h2>" + chipHtml(p.lifecycle_family, p.lifecycle_state) +
          "<dl>" +
          "<dt>Technology</dt><dd>" + (p.technology || "—") + "</dd>" +
          "<dt>Capacity</dt><dd>" + (p.capacity_mw ? p.capacity_mw.toFixed(1) + " MW" : "—") + "</dd>" +
          "<dt>Location</dt><dd>" + (p.county || "—") + ", " + (p.state || p.jurisdiction || "—") +
          (p.restricted_precision ? " (county level, source licence)" : "") + "</dd>" +
          "<dt>Source</dt><dd><a href=\"" + p.source_url + "\" rel=\"noopener nofollow\">" + p.source_name + "</a>, retrieved " +
          (p.retrieved_at ? p.retrieved_at.slice(0, 10) : "unknown") + "</dd>" +
          "</dl><p><a href=\"/proposals/" + p.slug + "\">Open full record &rarr;</a></p>";
      }
      function renderState(p) {
        body.innerHTML =
          "<h2>" + p.state + " (state aggregate)</h2>" +
          "<p>" + p.count + " proposals lack a matched county centroid and are grouped at the state level.</p>" +
          "<dl><dt>Capacity sum</dt><dd>" + Math.round(p.capacity_mw_sum).toLocaleString() + " MW</dd></dl>" +
          '<p><a href="/proposals?jurisdiction=' + p.state + '">View these proposals as a list &rarr;</a></p>';
      }
      return { open: open, close: close, render: render, renderState: renderState };
    }
  }
})();
