/* Map page (docs/04 D-8...D-15). MapLibre GL JS pinned to 5.24.0 (see home_map.html).
 * Clustering is computed server-side by `GET /v1/proposals/geo` (docs/23 §3.1), proxied through
 * this app's own `/api/proposals/geo` so the browser never needs to know the API's host. This
 * script renders exactly what comes back on every pan/zoom/filter change; it does not cluster.
 * The basemap (three MAP_TILE_URL modes + the same-origin fallback outline) lives in basemap.js,
 * shared with the asset/company page mini-maps (asset_map.js).
 */
(function () {
  "use strict";

  var Basemap = window.InfraqueBasemap;

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
  // Plant technology families. The API classifies plants with pipeline.normalize.classify_tech's
  // finer vocabulary (gas_cc, gas_ct, wind_offshore, pumped_storage, waste ...); the map draws
  // eleven families so the legend stays readable and every class lands in a named colour rather
  // than a catch-all (owner, 2026-09-15: biomass/waste must be toggleable and labelled).
  var PLANT_FAMILY_ORDER = [
    "solar", "wind", "gas", "oil", "coal", "nuclear", "hydro", "storage", "biomass", "geothermal", "other"
  ];
  // family -> classify_tech classes (services/api/context_routes.py TECHNOLOGY_VOCAB); the plant
  // type filter sends these classes, so an unknown one would be a 400 from the API -- guarded by
  // web/test_map_layers.py::test_plant_family_classes_are_all_in_the_api_vocabulary.
  var PLANT_FAMILY_CLASSES = {
    solar: ["solar", "solar_storage", "solar_thermal"],
    wind: ["wind", "wind_offshore", "wind_storage"],
    gas: ["gas_cc", "gas_ct", "gas_steam", "gas_ice", "gas_other", "fuel_cell", "hydrogen"],
    oil: ["oil"],
    coal: ["coal"],
    nuclear: ["nuclear"],
    hydro: ["hydro"],
    storage: ["storage", "pumped_storage"],
    biomass: ["biomass", "waste"],
    geothermal: ["geothermal"],
    other: ["other", "unknown", "load", "transmission"]
  };
  var PLANT_TECH_FAMILY = {};
  PLANT_FAMILY_ORDER.forEach(function (family) {
    PLANT_FAMILY_CLASSES[family].forEach(function (cls) { PLANT_TECH_FAMILY[cls] = family; });
  });
  // Legacy/loose spellings some proposal rows carry; harmless for plants.
  PLANT_TECH_FAMILY.gas = "gas"; PLANT_TECH_FAMILY.natural_gas = "gas";
  PLANT_TECH_FAMILY.hydroelectric = "hydro"; PLANT_TECH_FAMILY.battery = "storage"; PLANT_TECH_FAMILY.batteries = "storage";
  var PLANT_TECH_TOKEN = {};
  PLANT_FAMILY_ORDER.forEach(function (family) { PLANT_TECH_TOKEN[family] = "--plant-" + family; });
  var PLANT_FAMILY_LABEL = {
    solar: "SOL", wind: "WND", gas: "GAS", oil: "OIL", coal: "COL", nuclear: "NUC",
    hydro: "HYD", storage: "BES", biomass: "BIO", geothermal: "GEO", other: "OTH"
  };
  var PLANT_FAMILY_NAME = {
    solar: "Solar", wind: "Wind", gas: "Gas", oil: "Oil", coal: "Coal", nuclear: "Nuclear",
    hydro: "Hydro", storage: "Storage", biomass: "Biomass / waste", geothermal: "Geothermal", other: "Other"
  };

  // ---- existing-asset types (docs/00-PLAN.md 2026-09-19 owner decision, option (a)) ----
  // One row per `asset.asset_type` the map can draw (services/db/models.py ASSET_TYPES names).
  // `shape` is the SDF icon registered in `addPlantsLayers` -- the type is carried by shape +
  // label, never by hue alone (D-5). `token` is the CSS custom property for the hue (docs/31
  // §1.6); power plants keep their per-technology family palette. `line: true` types arrive as
  // `feature_kind: "asset_line"` LineString/MultiLineString features from `/v1/assets/geo`.
  var ASSET_TYPES = {
    power_plant: { label: "Power plant", plural: "power plants", shape: "asset-square", token: null, line: false },
    gas_pipeline: { label: "Gas pipeline", plural: "gas pipelines", shape: null, token: "--asset-gas-pipeline", line: true },
    gas_processing_plant: { label: "Gas processing plant", plural: "gas processing plants", shape: "asset-diamond", token: "--asset-gas-processing", line: false },
    gas_storage: { label: "Gas storage", plural: "gas storage sites", shape: "asset-ring", token: "--asset-gas-storage", line: false },
    lng_terminal: { label: "LNG terminal", plural: "LNG terminals", shape: "asset-triangle", token: "--asset-lng-terminal", line: false },
    ethanol_plant: { label: "Ethanol plant", plural: "ethanol plants", shape: "asset-hexagon", token: "--asset-ethanol", line: false },
    rng_project: { label: "RNG project", plural: "RNG projects", shape: "asset-pentagon", token: "--asset-rng", line: false }
  };
  var ASSET_TYPE_ORDER = Object.keys(ASSET_TYPES);
  // The types with data behind them today (ethanol and RNG since the second midstream slice,
  // 2026-09-19); the checkbox list in home_map.html disables anything listed but not here
  // ("coming"). An unknown `asset_type=` value in the URL is dropped, never sent to the API.
  var ASSET_TYPES_LIVE = ["power_plant", "gas_pipeline", "gas_processing_plant", "gas_storage", "lng_terminal", "ethanol_plant", "rng_project"];
  // RNG technology families (us.epa.lmop: lfg_electricity | rng | lfg_direct_use; us.epa.agstar:
  // farm_digester) -> the words the tooltip, drawer and in-view row show. Same table as
  // web/app.py RNG_TECHNOLOGY_LABELS.
  var RNG_TECH_LABEL = {
    lfg_electricity: "Landfill gas to electricity", lfg_direct_use: "Landfill gas direct use",
    rng: "Renewable natural gas", farm_digester: "Farm digester"
  };
  var FUEL_ASSET_TYPES = ["ethanol_plant", "rng_project"];
  var CAPACITY_UNIT_LABEL = { "mmgal/yr": "MMgal/yr", mmscfd: "MMscf/d", "cu-ft/day": "cu ft/day" };
  var AGSTAR_HERD_KEYS = ["dairy", "swine", "cattle", "poultry"];
  var IN_VIEW_LIMIT = 500;
  var IN_VIEW_ASSET_LIMIT = 200;
  var WORLD_BBOX = [-179, -85, 179, 85];
  var WORLD_CENTER = [-98.5, 39.8];
  var WORLD_ZOOM = 3.2;

  var cssVar = Basemap.cssVar;

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

  function assetTypeColors() {
    var out = {};
    ASSET_TYPE_ORDER.forEach(function (type) {
      var token = ASSET_TYPES[type].token;
      if (token) out[type] = cssVar(token) || "#6d6d6d";
    });
    return out;
  }

  function familyOf(state) { return LIFECYCLE_FAMILY[state] || "neutral"; }

  function plantFamilyOf(tech) {
    if (!tech) return "other";
    return PLANT_TECH_FAMILY[String(tech).toLowerCase()] || "other";
  }

  function techLabel(tech) {
    if (!tech) return "?";
    return TECH_LABEL[tech] || tech.slice(0, 3).toUpperCase();
  }

  function assetTypeOf(p) {
    return ASSET_TYPES[p.asset_type] ? p.asset_type : "power_plant";
  }
  function assetTypeLabel(type) {
    return ASSET_TYPES[type] ? ASSET_TYPES[type].label : String(type || "asset").replace(/_/g, " ");
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

  // A midstream field may sit at the top level of the feature's properties or inside its
  // `attributes` bag (data lane, 2026-09-19: "attributes such as operator, interstate/intrastate,
  // diameter, length_miles, states") -- read whichever is present, first match wins; absent means
  // the row is not rendered at all (the "None" gate rule, blockers sprint).
  function attrOf(p, keys) {
    var attributes = propObj(p.attributes) || {};
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (p[k] != null && p[k] !== "") return p[k];
      if (attributes[k] != null && attributes[k] !== "") return attributes[k];
    }
    return null;
  }

  // "interstate" | "intrastate" | null from whichever spelling the source carries.
  function lineClassOf(p) {
    var raw = attrOf(p, ["line_class", "interstate", "pipeline_type", "type_of_pipeline", "system_type", "class"]);
    if (raw === true) return "interstate";
    if (raw === false) return "intrastate";
    if (raw == null) return null;
    var s = String(raw).toLowerCase();
    if (s.indexOf("intra") !== -1) return "intrastate";
    if (s.indexOf("inter") !== -1) return "interstate";
    if (s.indexOf("gather") !== -1) return "gathering";
    return null;
  }

  function statesOf(p) {
    var raw = attrOf(p, ["states", "states_crossed", "state_codes"]);
    if (raw == null) return null;
    var list = Array.isArray(raw) ? raw : String(raw).split(/[,;]\s*/);
    list = list.map(function (s) { return String(s).trim(); }).filter(Boolean);
    return list.length ? list.join(", ") : null;
  }

  // `null` for an absent value, never "0": `Number(null)` and `Number("")` are both 0, so without
  // the guard an absent field rendered a real-looking zero (found driving the drawer, 2026-09-19 --
  // the "None" gate means no row at all, not a zero).
  function fmtNumber(value, digits) {
    if (value == null || value === "") return null;
    var n = Number(value);
    if (!isFinite(n)) return null;
    return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  }
  // "55 MMgal/yr", "1.725 MMscf/d": a number with the source's unit word, or null when absent --
  // the same rule web/app.py::_quantity applies on the asset page.
  function quantity(value, unit, digits) {
    var text = fmtNumber(value, digits == null ? 1 : digits);
    if (text == null) return null;
    var label = CAPACITY_UNIT_LABEL[String(unit || "").toLowerCase()] || unit;
    return label ? text + " " + label : text;
  }
  function yearOf(value) {
    var n = Number(value);
    return isFinite(n) && n > 0 ? String(Math.trunc(n)) : null;
  }
  function rngTechLabel(technology) {
    if (!technology) return null;
    return RNG_TECH_LABEL[technology] || String(technology).replace(/_/g, " ");
  }
  function isFuelType(type) { return FUEL_ASSET_TYPES.indexOf(type) !== -1; }
  // The promoted rows for an ethanol plant or RNG project, `[label, value, numeric]`, from the
  // feature's properties merged with the asset's detail row when the drawer has fetched it
  // (`/api/assets/{public_id}`; geo point features carry no capacity_value/unit or attributes).
  // Mirrors web/app.py::_fuel_fields so the drawer and the asset page agree row for row.
  function fuelRows(p) {
    var type = assetTypeOf(p);
    var unit = String(p.capacity_unit || "").toLowerCase();
    var rows = [];
    function add(label, value, numeric) { if (value) rows.push([label, value, !!numeric]); }
    if (type === "ethanol_plant") {
      var nameplate = attrOf(p, ["nameplate_capacity_mmgal_yr"]);
      if (nameplate == null && unit === "mmgal/yr") nameplate = p.capacity_value;
      add("Nameplate capacity", quantity(nameplate, "MMgal/yr"), true);
      add("Feedstock", feedstockOf(p));
      var padd = attrOf(p, ["padd"]);
      if (padd != null) add("PADD", /^padd/i.test(String(padd)) ? String(padd) : "PADD " + padd);
      var asOf = yearOf(attrOf(p, ["as_of_year"])) || attrOf(p, ["data_period"]);
      add("Capacity as of", asOf ? String(asOf) : null, true);
      return rows;
    }
    if (type === "rng_project") {
      var digester = p.technology === "farm_digester";
      var projectType = attrOf(p, ["lfg_energy_project_type", "project_type"]) || (digester ? null : p.technology_raw);
      add("Project type", projectType ? String(projectType) : null);
      add("Technology", rngTechLabel(p.technology));
      add("Rated capacity", quantity(attrOf(p, ["rated_mw", "capacity_mw"]), "MW"), true);
      var flow = attrOf(p, ["lfg_flow_to_project_mmscfd"]);
      if (flow == null && unit === "mmscfd") flow = p.capacity_value;
      add("LFG flow to project", quantity(flow, "MMscf/d", 3), true);
      var biogas = attrOf(p, ["biogas_generation_estimate_cuft_day"]);
      if (biogas == null && unit === "cu-ft/day") biogas = p.capacity_value;
      add("Biogas generation (est.)", quantity(biogas, "cu ft/day", 0), true);
      var endUse = attrOf(p, ["biogas_end_uses", "lfg_use_details", "project_type_category"]);
      add("Biogas end use", endUse ? String(endUse) : null);
      if (digester) {
        var digesterType = attrOf(p, ["digester_type"]) || p.technology_raw;
        add("Digester type", digesterType ? String(digesterType) : null);
      } else {
        add("Host landfill", hostLandfillOf(p));
      }
      add("Feedstock", feedstockOf(p));
      add("Start year", yearOf(attrOf(p, ["project_start_year", "year_operational", "commissioned_year"])), true);
      add("Shutdown year", yearOf(attrOf(p, ["year_shutdown"])), true);
    }
    return rows;
  }
  function feedstockOf(p) {
    var named = attrOf(p, ["feedstock", "animal_farm_types", "feedstock_raw"]);
    if (named) return String(named);
    var herds = [];
    AGSTAR_HERD_KEYS.forEach(function (key) {
      var head = Number(attrOf(p, [key]));
      if (isFinite(head) && head > 0) herds.push(key.charAt(0).toUpperCase() + key.slice(1) + " (" + fmtNumber(head, 0) + " head)");
    });
    return herds.length ? herds.join("; ") : null;
  }
  // LMOP names are composed "Project #N - <Landfill name>" by the loader (services/ingest,
  // us.epa.lmop); read the landfill back out when the record carries no landfill column.
  function hostLandfillOf(p) {
    var named = attrOf(p, ["landfill_name", "host_landfill"]);
    if (named) return String(named);
    var m = /^Project\s+#\d+\s+-\s+(.+)$/.exec(String(p.name || ""));
    return m ? m[1].trim() : null;
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

  // ADR 0008 placement grades: the three checkboxes' allowed values, in the fixed order the URL
  // and the API's `placement` csv both use.
  var PLACEMENT_GRADES = ["exact", "region", "none"];
  var DEFAULT_PLACEMENT = ["exact", "region"];

  function readPlacement(params) {
    if (!params.has("placement")) return DEFAULT_PLACEMENT.slice();
    var raw = (params.get("placement") || "").split(",").map(function (s) { return s.trim(); });
    return PLACEMENT_GRADES.filter(function (g) { return raw.indexOf(g) !== -1; });
  }

  // `asset_type=` csv (docs/23 §3.1: the API's own csv filter). Absent -> every live type, so a
  // first "Existing assets" toggle shows pipelines beside plants; unknown names are dropped.
  function readAssetTypes(params) {
    if (!params.has("asset_type")) return ASSET_TYPES_LIVE.slice();
    var raw = (params.get("asset_type") || "").split(",").map(function (s) { return s.trim(); });
    var kept = ASSET_TYPES_LIVE.filter(function (t) { return raw.indexOf(t) !== -1; });
    return kept.length ? kept : ASSET_TYPES_LIVE.slice();
  }

  function readFilters() {
    var params = new URLSearchParams(window.location.search);
    var layersParam = params.get("layers") || "";
    return {
      technology: params.get("technology") || "",
      jurisdiction: params.get("jurisdiction") || "",
      include_withdrawn: params.get("include_withdrawn") === "1",
      layers: layersParam ? layersParam.split(",").filter(Boolean) : [],
      region: params.get("region") || "",
      plant_technology: PLANT_FAMILY_CLASSES[params.get("plant_technology") || ""] ? params.get("plant_technology") : "",
      asset_types: readAssetTypes(params),
      placement: readPlacement(params)
    };
  }

  function writeFilters(filters) {
    var params = new URLSearchParams();
    if (filters.technology) params.set("technology", filters.technology);
    if (filters.jurisdiction) params.set("jurisdiction", filters.jurisdiction);
    if (filters.include_withdrawn) params.set("include_withdrawn", "1");
    if (filters.layers && filters.layers.length) params.set("layers", filters.layers.join(","));
    if (filters.region) params.set("region", filters.region);
    if (filters.plant_technology) params.set("plant_technology", filters.plant_technology);
    // Written whenever the assets layer is on (same round-trip reasoning as `placement` below).
    if (filters.layers && filters.layers.indexOf("plants") !== -1) {
      params.set("asset_type", (filters.asset_types && filters.asset_types.length ? filters.asset_types : ASSET_TYPES_LIVE).join(","));
    }
    // Always written (task item 1: "written to the URL as placement=exact,region") -- unlike the
    // other filters above, omitting it would leave the default state ambiguous with "not yet
    // loaded", and the URL-reflects-view rule wants every load of this page to round-trip.
    params.set("placement", (filters.placement && filters.placement.length ? filters.placement : DEFAULT_PLACEMENT).join(","));
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
    // ADR 0008 placement grades (docs/23 §3.1): csv of exact|region|none, API default
    // "exact,region" -- sent explicitly rather than relying on that default so the map always
    // requests exactly what the three checkboxes show, including when "none" is checked (its
    // only visible effect: `totals.unplaced` is then populated, see `render()`'s unplaced note).
    params.set("placement", (filters.placement && filters.placement.length ? filters.placement : DEFAULT_PLACEMENT).join(","));
    return "/api/proposals/geo?" + params.toString();
  }

  // ADR 0008 task item 2 + midstream slice: the existing-assets fetch points at `/v1/assets/geo`
  // with the picked types as an `asset_type` csv. The API applies `technology` across every type
  // it returns, so a plant-type family (which only power plants carry) is sent on a separate
  // power-plant-only request and the other types are fetched without it -- `assetsGeoUrls`
  // returns one or two URLs, merged by `refetchAssets`.
  function assetsGeoUrls(filters, bbox, zoom) {
    var types = filters.asset_types && filters.asset_types.length ? filters.asset_types : ASSET_TYPES_LIVE;
    var family = filters.plant_technology && PLANT_FAMILY_CLASSES[filters.plant_technology] ? filters.plant_technology : "";
    function url(typeList, technologyCsv) {
      var params = new URLSearchParams();
      params.set("bbox", bbox.join(","));
      params.set("zoom", String(zoom));
      params.set("asset_type", typeList.join(","));
      if (technologyCsv) params.set("technology", technologyCsv);
      return "/api/assets/geo?" + params.toString();
    }
    if (!family || types.indexOf("power_plant") === -1) return [url(types, "")];
    var others = types.filter(function (t) { return t !== "power_plant"; });
    var urls = [url(["power_plant"], PLANT_FAMILY_CLASSES[family].join(","))];
    if (others.length) urls.push(url(others, ""));
    return urls;
  }

  // `/api/geo/regions` (ADR 0008): batched per level, cached for the page's session -- a region
  // polygon is fetched once per (level, region_id) and never re-requested for the life of the tab.
  var regionPolygonCache = {}; // "level:region_id" -> GeoJSON geometry

  function ensureRegionPolygons(regionFeatures, done) {
    var missingByLevel = {};
    regionFeatures.forEach(function (f) {
      var level = f.properties.region_level;
      var id = f.properties.region_id;
      var key = level + ":" + id;
      if (regionPolygonCache[key]) return;
      missingByLevel[level] = missingByLevel[level] || [];
      if (missingByLevel[level].indexOf(id) === -1) missingByLevel[level].push(id);
    });
    var levels = Object.keys(missingByLevel);
    if (!levels.length) { done(); return; }
    var remaining = levels.length;
    levels.forEach(function (level) {
      // docs/23 §3.1: `ids` csv, max 500 -- one request per level, capped defensively even
      // though a single viewport is never expected to name more than a handful of regions.
      var ids = missingByLevel[level].slice(0, 500);
      fetch("/api/geo/regions?level=" + encodeURIComponent(level) + "&ids=" + encodeURIComponent(ids.join(",")))
        .then(function (r) { return r.json(); })
        .then(function (envelope) {
          (envelope.data && envelope.data.features || []).forEach(function (feat) {
            var key = level + ":" + (feat.properties.region_id != null ? feat.properties.region_id : feat.properties.id);
            regionPolygonCache[key] = feat.geometry;
          });
        })
        .catch(function () { /* this batch's polygons stay unrendered until a future fetch succeeds */ })
        .then(function () { remaining -= 1; if (remaining === 0) done(); });
    });
  }

  // Every value interpolated into markup below comes from upstream registers (queue names,
  // operator names, source URLs), so it is untrusted: escape text, and only allow http(s) or
  // site-relative URLs in href attributes (web audit 2026-09-18, DOM injection in the drawer).
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function safeUrl(value) {
    var v = String(value == null ? "" : value).trim();
    if (/^https?:\/\//i.test(v) || (v.charAt(0) === "/" && v.charAt(1) !== "/" && v.charAt(1) !== "\\")) return esc(v);
    return "#";
  }
  function chipHtml(family, label) {
    return (
      '<span class="chip chip--' + esc(family) + '">' +
      '<svg class="chip__icon" aria-hidden="true" width="12" height="12"><use href="#icon-' + esc(family) + '"></use></svg>' +
      '<span class="chip__label">' + esc((label || "unknown").replace(/_/g, " ")) + "</span></span>"
    );
  }
  // The asset's own page: a `slug` when the feature carries one, else the API's `url` (already
  // relativised by the proxy where it is the placeholder host), else the site's by-id redirect
  // (`/assets/by-id/{public_id}`, web/app.py) -- geo features carry only the public id.
  function assetPageUrl(p) {
    if (p.slug) return "/assets/" + encodeURIComponent(String(p.slug));
    if (p.url && /\/assets\//.test(String(p.url))) return safeUrl(p.url);
    if (p.public_id) return "/assets/by-id/" + encodeURIComponent(String(p.public_id));
    return null;
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
  var plantTypeSelect = document.getElementById("mf-plant-technology");
  var plantTypeField = document.getElementById("mf-plant-technology-field");
  var assetTypesField = document.getElementById("mf-asset-types");
  var assetTypeBoxes = Array.prototype.slice.call(document.querySelectorAll('#mf-asset-types input[name="asset_type"]'));
  plantTypeSelect.value = filters.plant_technology;
  assetTypeBoxes.forEach(function (box) {
    box.checked = !box.disabled && filters.asset_types.indexOf(box.value) !== -1;
  });
  function currentAssetTypes() {
    var picked = assetTypeBoxes.filter(function (box) { return box.checked && !box.disabled; }).map(function (box) { return box.value; });
    return picked.filter(function (t) { return ASSET_TYPES_LIVE.indexOf(t) !== -1; });
  }
  function syncAssetControls() {
    var on = plantsToggle.checked;
    assetTypesField.hidden = !on;
    plantTypeField.hidden = !on || filters.asset_types.indexOf("power_plant") === -1;
  }
  syncAssetControls();
  var placementCheckboxes = {
    exact: document.getElementById("mf-placement-exact"),
    region: document.getElementById("mf-placement-region"),
    none: document.getElementById("mf-placement-none")
  };
  PLACEMENT_GRADES.forEach(function (grade) {
    placementCheckboxes[grade].checked = filters.placement.indexOf(grade) !== -1;
  });
  writeFilters(filters);

  var colors = familyColors();
  var plantColors = plantFamilyColors();
  var assetColors = assetTypeColors();
  var mapColors = Basemap.mapColors();
  // ADR 0008 region features: a single hue (not a status-family colour, same reasoning as the
  // plant palette) so a highlighted region never reads as a lifecycle state.
  var regionColors = {
    fill: cssVar("--region-fill") || "#5b6b7c",
    line: cssVar("--region-line") || "#5b6b7c"
  };

  function assetColor(p) {
    var type = assetTypeOf(p);
    if (type === "power_plant") return plantColors[plantFamilyOf(p.technology)] || plantColors.other;
    return assetColors[type] || plantColors.other;
  }
  function clusterColor(p) {
    var type = ASSET_TYPES[p.dominant_asset_type] ? p.dominant_asset_type : "power_plant";
    if (type === "power_plant") return plantColors[plantFamilyOf(p.dominant_technology)] || plantColors.other;
    return assetColors[type] || plantColors.other;
  }

  var map = new maplibregl.Map({
    container: "map",
    style: Basemap.fallbackStyle(mapColors),
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
  var latestMeta = {};  // the envelope's `meta` (unplaced_count lives there, not in totals)
  var latestRegionFeatures = [];
  var latestAssets = { type: "FeatureCollection", features: [], totals: {} };
  var latestPlantsTotal = 0;
  var latestAssetsClustered = false;

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

  // ADR 0008 region features: rebuilds the "regions" source from the region-kind features in the
  // latest proposals/geo response, once their polygons are cached (`ensureRegionPolygons`). Every
  // region also contributes a Point feature (its representative point, always available
  // immediately) so the count label can render before the polygon fetch finishes; fill/outline
  // layers below are filtered to `["geometry-type"] == "Polygon"`, the label layer to `"Point"`.
  function updateRegionsLayer(regionFeatures) {
    if (!map.getSource("regions")) return;
    var maxCount = 0;
    regionFeatures.forEach(function (f) { maxCount = Math.max(maxCount, f.properties.count || 0); });
    var out = [];
    regionFeatures.forEach(function (f) {
      var key = f.properties.region_level + ":" + f.properties.region_id;
      var ratio = maxCount > 0 ? (f.properties.count || 0) / maxCount : 1;
      // 0.15..0.7 fill-opacity range: even the smallest region in view stays visible, the busiest
      // never obscures the layers drawn above it (proposals points/clusters, the assets layer).
      var opacity = 0.15 + ratio * 0.55;
      out.push({
        type: "Feature", geometry: f.geometry,
        properties: { region_level: f.properties.region_level, region_id: f.properties.region_id, name: f.properties.name, count: f.properties.count, region_opacity: opacity }
      });
      var polygon = regionPolygonCache[key];
      if (polygon) {
        out.push({
          type: "Feature", geometry: polygon,
          properties: { region_level: f.properties.region_level, region_id: f.properties.region_id, name: f.properties.name, count: f.properties.count, region_opacity: opacity }
        });
      }
    });
    map.getSource("regions").setData({ type: "FeatureCollection", features: out });
  }

  function refetch() {
    fetch(geoUrl(filters, currentBbox(), currentZoom()))
      .then(function (r) { return r.json(); })
      .then(function (envelope) {
        var fc = envelope.data;
        var pointFeatures = [];
        var regionFeatures = [];
        fc.features.forEach(function (f) {
          if (f.properties.feature_kind === "region") {
            regionFeatures.push(f);
            return;
          }
          if (f.properties.feature_kind === "cluster") {
            f.properties.family = familyOf(f.properties.dominant_lifecycle_state);
          } else {
            f.properties.family = familyOf(f.properties.lifecycle_state);
            f.properties.tech_label = techLabel(f.properties.technology);
          }
          pointFeatures.push(f);
        });
        latestCollection = fc;
        latestMeta = envelope.meta || {};
        latestRegionFeatures = regionFeatures;
        if (map.getSource("proposals")) {
          map.getSource("proposals").setData({ type: "FeatureCollection", features: pointFeatures });
        }
        ensureRegionPolygons(regionFeatures, function () { updateRegionsLayer(regionFeatures); });
        render();
      })
      .catch(function () {
        // docs/31 §6 error state: keep the last-known view rather than blanking it.
      });
    if (plantsToggle.checked) refetchAssets();
  }

  // Annotates one asset feature in place with what the layers and the in-view list read:
  // `color` (hue by type / plant family), `icon` (SDF shape name), `plant_label`, `line_class`.
  function decorateAssetFeature(f) {
    var p = f.properties;
    var kind = p.feature_kind;
    if (kind === "asset_cluster" || kind === "plant_cluster") {
      p.color = clusterColor(p);
      return;
    }
    var type = assetTypeOf(p);
    p.asset_type = type;
    p.color = assetColor(p);
    if (kind === "asset_line" || (f.geometry && /LineString$/.test(f.geometry.type))) {
      p.feature_kind = "asset_line";
      p.line_class = lineClassOf(p) || "unknown";
      return;
    }
    if (kind === "plant") p.feature_kind = "asset";
    p.icon = ASSET_TYPES[type].shape || "asset-square";
    if (type === "power_plant") {
      p.plant_family = plantFamilyOf(p.technology);
      p.plant_label = PLANT_FAMILY_LABEL[p.plant_family];
    } else {
      p.plant_label = "";
    }
  }

  // How many existing assets the current response actually puts in the view: a cluster stands for
  // `count` of them, every drawn point or line for one.
  function assetsInView(features) {
    var total = 0;
    features.forEach(function (f) {
      var kind = f.properties.feature_kind;
      if (kind === "asset_cluster" || kind === "plant_cluster") total += Number(f.properties.count) || 0;
      else total += 1;
    });
    return total;
  }

  var assetsFetchSeq = 0;
  function refetchAssets() {
    var seq = ++assetsFetchSeq;
    var urls = assetsGeoUrls(filters, currentBbox(), currentZoom());
    Promise.all(urls.map(function (u) {
      return fetch(u).then(function (r) { return r.json(); }).then(function (envelope) { return envelope.data || {}; });
    }))
      .then(function (collections) {
        if (seq !== assetsFetchSeq) return; // a newer viewport's answer already landed
        var features = [];
        var records = 0;
        var clustered = false;
        collections.forEach(function (fc) {
          (fc.features || []).forEach(function (f) { decorateAssetFeature(f); features.push(f); });
          records += (fc.totals || {}).records || 0;
          if ((fc.totals || {}).clustered) clustered = true;
        });
        latestAssets = { type: "FeatureCollection", features: features, totals: { records: records, clustered: clustered } };
        // `totals.records` from `/v1/assets/geo` is the dataset-wide total for the type filter --
        // it does not narrow to the bbox (measured 2026-09-19: 1536 for a viewport holding one
        // asset), unlike the proposals geo endpoint's. "In view" must mean in view, so the count
        // beside the list is computed from what actually came back: a cluster contributes its own
        // `count`, every other feature one. Unplaced rows (state-grade ethanol capacity, county-
        // grade AgSTAR digesters) are in neither, which is correct -- they are not in the view.
        latestPlantsTotal = assetsInView(features);
        latestAssetsClustered = clustered;
        if (map.getSource("plants")) map.getSource("plants").setData({ type: "FeatureCollection", features: features });
        render();
      })
      .catch(function () {
        // Same error-state rule as the proposals fetch: keep whatever was last drawn.
      });
  }

  function assetMeta(p) {
    var parts = [];
    if (p.asset_type === "rng_project" && rngTechLabel(p.technology)) parts.push(rngTechLabel(p.technology));
    if (p.operator_name) parts.push(p.operator_name);
    var states = statesOf(p);
    if (states) parts.push(states);
    else if (p.state_code) parts.push(p.state_code);
    if (p.feature_kind === "asset_line") {
      var miles = attrOf(p, ["length_miles", "miles"]);
      if (miles != null && fmtNumber(miles, 0)) parts.push(fmtNumber(miles, 0) + " mi");
      if (p.line_class && p.line_class !== "unknown") parts.push(p.line_class);
    } else if (p.capacity_mw) {
      parts.push(Number(p.capacity_mw).toFixed(1) + " MW");
    } else if (isFuelType(p.asset_type)) {
      // Geo point features carry no capacity_value; a nameplate shows here only when the
      // feature (or a merged detail row) has one -- never a placeholder.
      var nameplate = attrOf(p, ["nameplate_capacity_mmgal_yr"]);
      if (nameplate == null && String(p.capacity_unit || "").toLowerCase() === "mmgal/yr") nameplate = p.capacity_value;
      if (nameplate != null && quantity(nameplate, "MMgal/yr")) parts.push(quantity(nameplate, "MMgal/yr"));
    }
    return parts.join(" · ");
  }

  // One in-view row per project, not per EIA-860M generator unit: the same (name, sponsor,
  // county) key as web/app.py::group_nearby_proposals -- geo features carry no sponsor, so that
  // part of the key is null here and only equal names in one county merge. The group carries
  // `unit_count`, the summed capacity, and the first (nearest-rendered) unit's link.
  function groupProposalFeatures(features) {
    var groups = {};
    var out = [];
    features.forEach(function (f) {
      var p = f.properties;
      var key = JSON.stringify([p.name || null, p.sponsor || null, p.county_name || null]);
      var g = groups[key];
      var mw = Number(p.capacity_mw);
      if (!g) {
        g = { properties: p, unit_count: 1, capacity_mw: isFinite(mw) && p.capacity_mw != null ? mw : null };
        groups[key] = g;
        out.push(g);
        return;
      }
      g.unit_count += 1;
      if (isFinite(mw) && p.capacity_mw != null) g.capacity_mw = (g.capacity_mw || 0) + mw;
    });
    return out;
  }

  function appendGroupName(text) {
    var li = document.createElement("li");
    li.className = "in-view-list__group-name";
    li.setAttribute("role", "presentation");
    li.textContent = text;
    listEl.appendChild(li);
  }

  function render() {
    var totals = latestCollection.totals || {};
    countEl.textContent =
      (totals.records || 0) + " proposal" + (totals.records === 1 ? "" : "s") + " match these filters" +
      (totals.clustered ? " (grouped into clusters at this zoom)." : ".");
    countEl.removeAttribute("aria-hidden");

    var individual = latestCollection.features.filter(function (f) {
      return f.properties.feature_kind !== "cluster" && f.properties.feature_kind !== "region";
    });
    listEl.innerHTML = "";
    if (totals.clustered) {
      var li = document.createElement("li");
      li.textContent = "Zoom in or narrow the filters to list proposals individually; " +
        (totals.records || 0) + " match in clustered form above.";
      listEl.appendChild(li);
    } else {
      groupProposalFeatures(individual).slice(0, IN_VIEW_LIMIT).forEach(function (g) {
        var p = g.properties;
        var node = template.content.cloneNode(true);
        node.querySelector(".chip-slot").innerHTML = chipHtml(p.family, p.lifecycle_state);
        var a = node.querySelector(".name-link");
        a.textContent = p.name;
        a.href = p.url || "#";
        if (g.unit_count > 1) {
          var units = document.createElement("span");
          units.className = "in-view-list__units tnum";
          units.textContent = "× " + g.unit_count + " units";
          a.parentNode.insertBefore(units, a.nextSibling);
          a.parentNode.insertBefore(document.createTextNode(" "), units);
        }
        var meta = (p.technology || "—") + " · " + (p.state_code || p.county_name || "—") +
          (g.capacity_mw ? " · " + g.capacity_mw.toFixed(1) + " MW" : "");
        node.querySelector(".meta").textContent = meta;
        listEl.appendChild(node);
      });
    }

    // Region records (ADR 0008 placement grade "region") in the list as well as on the map, so a
    // keyboard or screen-reader user reaches them without a pointer (web audit 2026-09-18).
    if (latestRegionFeatures.length) {
      appendGroupName("Regions (" + latestRegionFeatures.length + ")");
      latestRegionFeatures.slice(0, IN_VIEW_LIMIT).forEach(function (f) {
        var p = f.properties;
        var item = document.createElement("li");
        var a = document.createElement("a");
        a.href = regionListUrl(p);
        a.textContent = (p.name || p.region_id) + " (" + String(p.region_level || "region") + ")";
        item.appendChild(a);
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = Number(p.count || 0) + " proposal" + (Number(p.count) === 1 ? "" : "s") + " placed at this region";
        item.appendChild(meta);
        listEl.appendChild(item);
      });
    }

    // Existing assets in view (points and lines alike): name linked to the asset page where the
    // feature carries one, a Details button that opens the same drawer a map click does.
    var assetsIndividual = [];
    if (plantsToggle.checked) {
      assetsIndividual = latestAssets.features.filter(function (f) {
        return f.properties.feature_kind === "asset" || f.properties.feature_kind === "asset_line";
      });
      if (latestAssetsClustered || assetsIndividual.length) {
        appendGroupName("Existing assets (" + latestPlantsTotal + ")");
      }
      if (latestAssetsClustered) {
        var cl = document.createElement("li");
        cl.textContent = "Zoom in to list existing assets individually; " + latestPlantsTotal + " are grouped in clusters above.";
        listEl.appendChild(cl);
      }
      assetsIndividual.slice(0, IN_VIEW_ASSET_LIMIT).forEach(function (f) {
        var p = f.properties;
        var item = document.createElement("li");
        var kind = document.createElement("span");
        kind.className = "in-view-list__kind";
        kind.textContent = assetTypeLabel(p.asset_type);
        item.appendChild(kind);
        var pageUrl = assetPageUrl(p);
        if (pageUrl) {
          var a = document.createElement("a");
          a.className = "name-link";
          a.href = pageUrl;
          a.textContent = p.name || p.public_id || "Unnamed asset";
          item.appendChild(a);
        } else {
          var strong = document.createElement("strong");
          strong.textContent = p.name || p.public_id || "Unnamed asset";
          item.appendChild(strong);
        }
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "details-btn";
        btn.textContent = "Details";
        btn.setAttribute("aria-label", "Details for " + (p.name || "this asset"));
        btn.addEventListener("click", function () { openAssetDrawer(p); });
        item.appendChild(btn);
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = assetMeta(p);
        item.appendChild(meta);
        listEl.appendChild(item);
      });
    }

    // Task item 2: "Live region adds 'N existing plants in view'" -- appended as a second
    // sentence so the proposals count (the accessible path's primary content) is never dropped
    // when the assets layer is on.
    var liveText = (totals.records || 0) + " proposals in view.";
    if (plantsToggle.checked) {
      var lines = latestAssets.features.filter(function (f) { return f.properties.feature_kind === "asset_line"; }).length;
      liveText += " " + latestPlantsTotal + " existing assets in view" + (lines ? " (" + lines + " pipeline" + (lines === 1 ? "" : "s") + ")" : "") + ".";
    }
    liveRegion.textContent = liveText;

    // ADR 0008: "none" grade (no usable location) is counted only in `totals.unplaced`, never
    // drawn -- checking the "None" placement checkbox has no effect on what is fetched or drawn
    // (a none-grade proposal has no geometry either way); its only visible effect is whether this
    // total is requested from the API at all, which is exactly the "count text" the task brief
    // says it is limited to.
    var unplacedCount = Number(latestMeta.unplaced_count || (latestCollection.totals || {}).unplaced || 0);
    if (unplacedCount > 0) {
      unplacedNote.hidden = false;
      unplacedNote.textContent = "Unplaced (" + unplacedCount + "): no usable county or state on these sources' records; view them in the list instead of on the map.";
    } else {
      unplacedNote.hidden = true;
    }
  }

  // ---- ADR 0008 region features (placement grade "region") ----
  // Added below "clusters" (beforeId), then `addPlantsLayers()` runs next with the same beforeId
  // -- each subsequent `addLayer(..., "clusters")` call lands directly below "clusters" and above
  // whatever was already inserted there, so the final bottom-to-top order is: fallback, regions,
  // assets, proposal clusters/points. That satisfies "region polygons draw beneath exact points
  // and clusters and beneath the assets layer" without the two layers needing to know about each
  // other's existence.
  function addRegionLayers() {
    map.addSource("regions", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "region-fill", type: "fill", source: "regions",
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": regionColors.fill, "fill-opacity": ["get", "region_opacity"] }
    }, "clusters");
    map.addLayer({
      id: "region-outline", type: "line", source: "regions",
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "line-color": regionColors.line, "line-width": 1, "line-opacity": 0.6 }
    }, "clusters");
    map.addLayer({
      id: "region-labels", type: "symbol", source: "regions",
      filter: ["==", ["geometry-type"], "Point"],
      layout: { "text-field": ["get", "count"], "text-size": 11, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": regionColors.line, "text-halo-color": mapColors.land, "text-halo-width": 1.5 }
    }, "clusters");

    map.on("click", "region-fill", function (e) { onRegionClick(e.features[0].properties); });
    map.on("mouseenter", "region-fill", function (e) {
      map.getCanvas().style.cursor = "pointer";
      showRegionTooltip(e);
    });
    map.on("mouseleave", "region-fill", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
  }

  // Task item 1's click rule: county -> the filtered list; state -> the filtered list; country ->
  // pan the map via the matching quick-view region button when this deploy has one for it (so a
  // country click stays on the map, matching that button's own behaviour), else the filtered list.
  function regionListUrl(p) {
    if (p.region_level === "county") return "/proposals?county_fips=" + encodeURIComponent(p.region_id);
    return "/proposals?jurisdiction=" + encodeURIComponent(p.region_id);
  }
  function onRegionClick(p) {
    if (p.region_level === "county" || p.region_level === "state") {
      window.location.href = regionListUrl(p);
    } else {
      var btn = document.querySelector('.region-btn[data-region="' + String(p.region_id).toLowerCase() + '"]');
      if (btn) { btn.click(); } else { window.location.href = regionListUrl(p); }
    }
  }

  function showRegionTooltip(e) {
    var p = e.features[0].properties;
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(e.lngLat)
      .setHTML("<strong>" + esc(p.name || p.region_id) + "</strong><br>" + Number(p.count || 0) + " proposals")
      .addTo(map);
  }

  // ---- existing assets layer (task item 2; midstream slice 2026-09-19) ----
  // Every icon is an SDF image drawn on a canvas so `icon-color` can carry the per-feature hue:
  // square (power plant, unchanged), diamond (gas processing), ring (gas storage), triangle (LNG
  // terminal), hexagon (ethanol), pentagon (RNG) -- docs/31 §1.6.
  function buildShapeIcon(shape, size) {
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#000000";
    ctx.strokeStyle = "#000000";
    var c = size / 2;
    var r = size / 2 - 1;
    function polygon(sides, rotation) {
      ctx.beginPath();
      for (var i = 0; i < sides; i++) {
        var angle = rotation + (Math.PI * 2 * i) / sides;
        var x = c + r * Math.cos(angle), y = c + r * Math.sin(angle);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.fill();
    }
    if (shape === "asset-diamond") polygon(4, 0);
    else if (shape === "asset-triangle") polygon(3, -Math.PI / 2);
    else if (shape === "asset-hexagon") polygon(6, 0);
    else if (shape === "asset-pentagon") polygon(5, -Math.PI / 2);
    else if (shape === "asset-ring") {
      ctx.lineWidth = Math.max(2, size / 5);
      ctx.beginPath();
      ctx.arc(c, c, r - ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      ctx.fillRect(0, 0, size, size);
    }
    return ctx.getImageData(0, 0, size, size);
  }

  var ASSET_LAYER_IDS = [
    "plant-clusters", "plant-cluster-count", "asset-lines-casing", "asset-lines", "asset-lines-intrastate",
    "asset-lines-hit", "asset-line-labels", "plant-points", "plant-labels"
  ];

  function addPlantsLayers() {
    // "plant-square" is the pre-2026-09-19 image name, kept as an alias of the square so nothing
    // that references it breaks; every feature now names its icon in `properties.icon`.
    map.addImage("plant-square", buildShapeIcon("asset-square", 8), { sdf: true });
    ["asset-square", "asset-diamond", "asset-ring", "asset-triangle", "asset-hexagon", "asset-pentagon"].forEach(function (shape) {
      map.addImage(shape, buildShapeIcon(shape, 16), { sdf: true });
    });
    map.addSource("plants", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

    var isCluster = ["any", ["==", ["get", "feature_kind"], "asset_cluster"], ["==", ["get", "feature_kind"], "plant_cluster"]];
    var isPoint = ["all", ["==", ["geometry-type"], "Point"], ["!", isCluster]];
    var isLine = ["==", ["geometry-type"], "LineString"];
    // Line width by zoom (docs/31 §1.6): hairline at the country view, a readable stroke once
    // counties resolve; the casing beneath is the land colour so a pipeline stays legible over
    // region fills and basemap roads.
    var lineWidth = ["interpolate", ["linear"], ["zoom"], 3, 0.8, 6, 1.4, 9, 2.4, 12, 4];
    var casingWidth = ["interpolate", ["linear"], ["zoom"], 3, 2.2, 6, 3.2, 9, 4.8, 12, 7];

    // Beneath the proposals layers (`addLayer(..., "clusters")`, task item 2): inserted right
    // above the basemap and below every proposals layer added further down.
    map.addLayer({
      id: "plant-clusters", type: "circle", source: "plants",
      filter: isCluster,
      paint: {
        "circle-radius": ["step", ["get", "count"], 10, 10, 14, 50, 18],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-width": 1,
        "circle-stroke-color": ["get", "color"],
        "circle-stroke-opacity": 0.6
      }
    }, "clusters");
    // "Noto Sans Medium", not "Bold": once the pmtiles basemap points `glyphs` at the real
    // Protomaps assets host, a font name that host doesn't carry 404s and the label silently
    // never renders -- confirmed against the real host, which hosts only Regular/Medium/Italic.
    map.addLayer({
      id: "plant-cluster-count", type: "symbol", source: "plants",
      filter: isCluster,
      layout: { "text-field": ["get", "count"], "text-size": 10, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": ["get", "color"], "text-opacity": 0.7 }
    }, "clusters");
    map.addLayer({
      id: "asset-lines-casing", type: "line", source: "plants",
      filter: isLine,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": mapColors.land, "line-width": casingWidth, "line-opacity": 0.9 }
    }, "clusters");
    // Interstate (and unclassified) pipelines solid, intrastate dashed: `line-dasharray` is not
    // data-driven in this MapLibre, hence two layers over the same source rather than one.
    map.addLayer({
      id: "asset-lines", type: "line", source: "plants",
      filter: ["all", isLine, ["!=", ["get", "line_class"], "intrastate"]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": 0.85 }
    }, "clusters");
    map.addLayer({
      id: "asset-lines-intrastate", type: "line", source: "plants",
      filter: ["all", isLine, ["==", ["get", "line_class"], "intrastate"]],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": 0.85, "line-dasharray": [3, 2] }
    }, "clusters");
    // Invisible, wide hit target so a hairline pipeline is still a >=24px pointer target
    // (SC 2.5.8); hover and click handlers bind to this layer, not the drawn ones.
    map.addLayer({
      id: "asset-lines-hit", type: "line", source: "plants",
      filter: isLine,
      paint: { "line-color": "rgba(0,0,0,0)", "line-width": 16, "line-opacity": 0 }
    }, "clusters");
    map.addLayer({
      id: "asset-line-labels", type: "symbol", source: "plants", minzoom: 7,
      filter: isLine,
      layout: {
        "symbol-placement": "line", "text-field": ["get", "name"], "text-size": 10,
        "text-font": ["Noto Sans Medium"], "text-max-angle": 30, "text-padding": 12
      },
      paint: { "text-color": ["get", "color"], "text-halo-color": mapColors.land, "text-halo-width": 1.2 }
    }, "clusters");
    map.addLayer({
      id: "plant-points", type: "symbol", source: "plants",
      filter: isPoint,
      layout: {
        "icon-image": ["coalesce", ["get", "icon"], "asset-square"],
        "icon-size": ["case", ["==", ["get", "asset_type"], "power_plant"], 0.3, 0.55],
        "icon-allow-overlap": true
      },
      paint: { "icon-color": ["get", "color"], "icon-opacity": 0.7 }
    }, "clusters");

    // Labels appear once the icons are far enough apart to read (zoom 9+): the family code
    // beneath a plant, the name beneath a midstream point.
    map.addLayer({
      id: "plant-labels", type: "symbol", source: "plants", minzoom: 9,
      filter: isPoint,
      layout: {
        "text-field": ["case", ["==", ["get", "asset_type"], "power_plant"], ["coalesce", ["get", "plant_label"], ""], ["coalesce", ["get", "name"], ""]],
        "text-size": 8, "text-font": ["Noto Sans Medium"],
        "text-anchor": "top", "text-offset": [0, 0.6], "text-allow-overlap": false
      },
      paint: { "text-color": ["get", "color"], "text-opacity": 0.85, "text-halo-color": mapColors.land, "text-halo-width": 1 }
    }, "clusters");

    map.on("click", "plant-points", function (e) { openAssetDrawer(e.features[0].properties); });
    map.on("mouseenter", "plant-points", function (e) { map.getCanvas().style.cursor = "pointer"; showAssetTooltip(e, e.features[0].geometry.coordinates); });
    map.on("mouseleave", "plant-points", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("click", "asset-lines-hit", function (e) { openAssetDrawer(e.features[0].properties); });
    map.on("mouseenter", "asset-lines-hit", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mousemove", "asset-lines-hit", function (e) { showAssetTooltip(e, e.lngLat); });
    map.on("mouseleave", "asset-lines-hit", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("click", "plant-clusters", function (e) {
      var f = e.features[0];
      map.easeTo({ center: f.geometry.coordinates, zoom: f.properties.expands_to_zoom || (map.getZoom() + 2), duration: motionMs(600) });
    });
  }

  function setPlantsLayerVisible(on) {
    var picked = on ? filters.asset_types : [];
    ASSET_LAYER_IDS.forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    });
    plantsLegend.hidden = !on;
    plantsLegend.setAttribute("aria-hidden", on ? "false" : "true");
    // The legend names only the types drawn: one group per checked type.
    Array.prototype.forEach.call(plantsLegend.querySelectorAll("[data-legend-type]"), function (group) {
      group.hidden = picked.indexOf(group.getAttribute("data-legend-type")) === -1;
    });
  }

  map.on("load", function () {
    Basemap.addBasemap(map, { tileMode: TILE_MODE, tileUrl: TILE_URL, colors: mapColors, onFail: reportBasemapFailedOnce });

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
    // Region and assets layers are added here -- after "clusters" exists (`addLayer(...,
    // "clusters")` requires the reference layer to already be in the style) but before the
    // remaining proposals layers, so both render beneath every proposals layer, clusters
    // included; region layers are added first so they end up beneath the assets layer too (see
    // `addRegionLayers`'s comment for why insertion order alone gives that stacking).
    addRegionLayers();
    addPlantsLayers();
    setPlantsLayerVisible(plantsToggle.checked);
    map.addLayer({
      id: "cluster-count", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "cluster"],
      layout: { "text-field": ["get", "count"], "text-size": 12, "text-font": ["Noto Sans Medium"] },
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
      layout: { "text-field": ["get", "tech_label"], "text-size": 8, "text-font": ["Noto Sans Medium"] },
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
    var lines = Object.keys(counts).sort().map(function (k) { return esc(k.replace(/_/g, " ")) + " " + Number(counts[k] || 0); });
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(f.geometry.coordinates)
      .setHTML(
        "<strong>" + Number(p.count || 0) + " proposals</strong><br>" + lines.join(" &middot; ") +
        (p.capacity_mw_sum ? "<br>Capacity sum: " + Math.round(p.capacity_mw_sum).toLocaleString() + " MW" : "")
      )
      .addTo(map);
  }
  // Hover on any existing asset: name, type (with the RNG technology family) and operator
  // (task: "hover shows name and operator").
  function showAssetTooltip(e, lngLat) {
    var p = e.features[0].properties;
    var family = p.asset_type === "rng_project" ? rngTechLabel(p.technology) : null;
    var html = "<strong>" + esc(p.name || "Unnamed asset") + "</strong><br>" + esc(assetTypeLabel(p.asset_type)) +
      (family ? " &middot; " + esc(family) : "") +
      (p.line_class && p.line_class !== "unknown" ? " &middot; " + esc(p.line_class) : "") +
      (p.operator_name ? "<br>Operator: " + esc(p.operator_name) : "");
    if (tooltip && tooltip.__assetId === (p.public_id || p.name)) { tooltip.setLngLat(lngLat); return; }
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 })
      .setLngLat(lngLat)
      .setHTML(html)
      .addTo(map);
    tooltip.__assetId = p.public_id || p.name;
  }
  function hideTooltip() { if (tooltip) { tooltip.remove(); tooltip = null; } }

  // ---- filters ----
  function currentPlacement() {
    return PLACEMENT_GRADES.filter(function (grade) { return placementCheckboxes[grade].checked; });
  }

  function applyFilters() {
    var picked = currentAssetTypes();
    filters = {
      technology: document.getElementById("mf-technology").value,
      jurisdiction: document.getElementById("mf-jurisdiction").value,
      include_withdrawn: document.getElementById("mf-include-withdrawn").checked,
      layers: filters.layers,
      region: filters.region,
      plant_technology: plantTypeSelect.value,
      asset_types: picked.length ? picked : ASSET_TYPES_LIVE.slice(),
      placement: currentPlacement()
    };
    writeFilters(filters);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(plantsToggle.checked);
    refetch();
  }
  plantTypeSelect.addEventListener("change", applyFilters);
  assetTypeBoxes.forEach(function (box) {
    box.addEventListener("change", function () {
      applyFilters();
      sendUiEvent("map.layer_toggled", { layer: "assets", on: box.checked, asset_type: box.value });
    });
  });
  document.getElementById("mf-technology").addEventListener("change", applyFilters);
  document.getElementById("mf-jurisdiction").addEventListener("change", applyFilters);
  document.getElementById("mf-include-withdrawn").addEventListener("change", applyFilters);
  plantsToggle.addEventListener("change", function () {
    var on = plantsToggle.checked;
    filters.layers = on ? ["plants"] : [];
    writeFilters(filters);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(on);
    else { plantsLegend.hidden = !on; }
    sendUiEvent("map.layer_toggled", { layer: "plants", on: on });
    if (on) refetchAssets();
    render();
  });
  // Task item 1: one `map.layer_toggled` beacon per checkbox change, `layer: "placement"` --
  // the existing allowed event name, not a new one, per the task brief.
  PLACEMENT_GRADES.forEach(function (grade) {
    placementCheckboxes[grade].addEventListener("change", function () {
      applyFilters();
      sendUiEvent("map.layer_toggled", { layer: "placement", on: placementCheckboxes[grade].checked });
    });
  });
  document.getElementById("mf-clear").addEventListener("click", function () {
    document.getElementById("mf-technology").value = "";
    document.getElementById("mf-jurisdiction").value = "";
    document.getElementById("mf-include-withdrawn").checked = false;
    plantTypeSelect.value = "";
    assetTypeBoxes.forEach(function (box) { box.checked = !box.disabled && ASSET_TYPES_LIVE.indexOf(box.value) !== -1; });
    placementCheckboxes.exact.checked = true;
    placementCheckboxes.region.checked = true;
    placementCheckboxes.none.checked = false;
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
  // Point assets other than power plants render at once from the feature, then again with the
  // asset's detail row merged in (`/api/assets/{public_id}`, web/app.py): geo point features
  // carry no capacity_value/unit or attributes, and an ethanol plant's nameplate or a landfill
  // project's LFG flow lives there. A failed fetch leaves the first render standing.
  var drawerDetailSeq = 0;
  var assetDetailCache = {};
  function openAssetDrawer(rawProps) {
    lastFocused = document.activeElement;
    drawer.renderAsset(rawProps);
    drawer.open();
    var type = assetTypeOf(rawProps);
    if (type === "power_plant" || rawProps.feature_kind === "asset_line" || !rawProps.public_id) return;
    var seq = ++drawerDetailSeq;
    var id = String(rawProps.public_id);
    function merge(detail) {
      if (seq !== drawerDetailSeq || !detail) return;
      var merged = {};
      Object.keys(rawProps).forEach(function (k) { merged[k] = rawProps[k]; });
      ["capacity_value", "capacity_unit", "attributes", "commissioned_year", "technology_raw", "county_name", "operator_name"].forEach(function (k) {
        if (detail[k] != null && detail[k] !== "") merged[k] = detail[k];
      });
      drawer.renderAsset(merged);
    }
    if (assetDetailCache[id]) { merge(assetDetailCache[id]); return; }
    fetch("/api/assets/" + encodeURIComponent(id))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (envelope) {
        var detail = envelope && envelope.data;
        if (detail) assetDetailCache[id] = detail;
        merge(detail);
      })
      .catch(function () { /* the feature's own rows stay */ });
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
    function row(label, valueHtml, numeric) {
      if (valueHtml == null || valueHtml === "") return "";
      return "<div class=\"drawer-fields__row\"><dt>" + esc(label) + "</dt><dd" + (numeric ? " class=\"tnum\"" : "") + ">" + valueHtml + "</dd></div>";
    }
    function sourceHtml(source) {
      if (!source) return "";
      return "<p class=\"drawer-source\"><span class=\"drawer-source__label\">Source</span>" +
        "<a href=\"" + safeUrl(source.source_url) + "\" rel=\"noopener nofollow\">" + esc(source.source_name) + "</a>, retrieved " +
        "<span class=\"tnum\">" + esc(source.retrieved_at ? String(source.retrieved_at).slice(0, 10) : "unknown") + "</span>" +
        (source.licence_name ? " &middot; " + esc(source.licence_name) : "") + "</p>";
    }
    function render(p, source) {
      body.innerHTML =
        "<h2>" + esc(p.name) + "</h2>" + chipHtml(familyOf(p.lifecycle_state), p.lifecycle_state) +
        "<dl class=\"drawer-fields\">" +
        "<div class=\"drawer-fields__row\"><dt>Technology</dt><dd>" + esc(p.technology || "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>Capacity</dt><dd class=\"tnum\">" + (p.capacity_mw ? Number(p.capacity_mw).toFixed(1) + " MW" : "—") + "</dd></div>" +
        "<div class=\"drawer-fields__row\"><dt>Location</dt><dd>" + esc(p.county_name || "—") + ", " + esc(p.state_code || "—") +
        (p.precision_note ? " (" + esc(p.precision_note) + ")" : "") + "</dd></div>" +
        "</dl>" +
        sourceHtml(source) +
        "<a class=\"drawer-open-link\" href=\"" + safeUrl(p.url || "#") + "\">Open full record &rarr;</a>";
    }
    // One drawer for every existing-asset type (task: a pipeline click "opens the same drawer as
    // points with the asset's fields"): the rows present depend on the type and on which fields
    // the feature actually carries -- an absent value drops its row rather than printing "—".
    function renderAsset(p) {
      var type = assetTypeOf(p);
      var isLine = p.feature_kind === "asset_line";
      var techs = propObj(p.technologies) || {};
      var techRows = Object.keys(techs).sort().map(function (k) {
        return row(k.replace(/_/g, " "), Number(techs[k]).toFixed(1) + " MW", true);
      }).join("");
      var source = propObj(p.source);
      var commissionedYear = p.commissioned_year || p.earliest_operating_year;
      var miles = attrOf(p, ["length_miles", "miles"]);
      var diameter = attrOf(p, ["diameter_in", "diameter_inches", "diameter", "diameter_mix"]);
      var states = statesOf(p);
      var lineClass = isLine ? lineClassOf(p) : null;
      var fuel = isFuelType(type);
      var family = type === "rng_project" ? rngTechLabel(p.technology) : null;
      var subtitle = type === "power_plant"
        ? esc(PLANT_FAMILY_NAME[plantFamilyOf(p.technology)] || "Other")
        : esc(assetTypeLabel(type)) + (family ? " &middot; " + esc(family) : "") + (lineClass ? " &middot; " + esc(lineClass) : "");
      var location = [p.county_name, p.state_code].filter(Boolean).map(esc).join(", ");
      var pageUrl = assetPageUrl(p);
      // Ethanol and RNG take their promoted rows (fuelRows) in place of the plant-shaped
      // technology / capacity / first-year rows, as on the asset page.
      var typeRows = fuel
        ? fuelRows(p).map(function (r) { return row(r[0], esc(r[1]), r[2]); }).join("")
        : (type === "power_plant" ? (techRows || row("Technology", p.technology ? esc(p.technology) : null)) : row("Technology", p.technology ? esc(p.technology) : null)) +
          row("Capacity", p.capacity_mw ? Number(p.capacity_mw).toFixed(1) + " MW" : null, true) +
          row("Length", miles != null && fmtNumber(miles, 0) ? fmtNumber(miles, 0) + " miles" : null, true) +
          row("Diameter", diameter != null ? esc(typeof diameter === "number" ? fmtNumber(diameter, 1) + " in" : String(diameter)) : null, true) +
          row("States", states ? esc(states) : null);
      body.innerHTML =
        "<h2>" + esc(p.name || "Unnamed asset") + "</h2>" +
        "<p class=\"reuse-badge\">Existing asset &middot; " + subtitle + "</p>" +
        "<dl class=\"drawer-fields\">" +
        row("Operator", p.operator_name ? esc(p.operator_name) : null) +
        typeRows +
        row("Status", p.status ? esc(String(p.status).replace(/_/g, " ")) : null) +
        (fuel ? "" : row("First operating year", commissionedYear ? esc(commissionedYear) : null, true)) +
        row("Location", location || null) +
        "</dl>" +
        sourceHtml(source) +
        (pageUrl ? "<a class=\"drawer-open-link\" href=\"" + pageUrl + "\">Open asset page &rarr;</a>" : "");
    }
    return { open: open, close: close, render: render, renderAsset: renderAsset, renderPlant: renderAsset };
  }
})();
