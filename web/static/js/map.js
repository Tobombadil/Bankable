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
    geothermal: "GEO", hydrogen: "H2", coal: "COL", other: "OTH",
    // Marker badge for kind/technology `load`; the slice(0, 3) fallback printed "LOA".
    load: "LL"
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
    hydro: ["hydro", "marine"],
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
  // Family names come from the server (`#map-labels` `plant_family`, web/labels.py), read below.

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
    rng_project: { label: "RNG project", plural: "RNG projects", shape: "asset-pentagon", token: "--asset-rng", line: false },
    // Grid lane G2 (2026-09-28): LBNL's FERC x HIFLD lines, one feature per line. Drawn dash-dot
    // in its own hue so it never reads as a gas pipeline (solid) or an intrastate one (dashed):
    // the stroke pattern carries the type, the hue is secondary (D-5).
    transmission_line: { label: "Transmission line", plural: "transmission lines", shape: null, token: "--asset-transmission", line: true }
  };
  var ASSET_TYPE_ORDER = Object.keys(ASSET_TYPES);
  // The types with data behind them today (ethanol and RNG since the second midstream slice,
  // 2026-09-19); the checkbox list in home_map.html disables anything listed but not here
  // ("coming"). An unknown `asset_type=` value in the URL is dropped, never sent to the API.
  var ASSET_TYPES_LIVE = ["power_plant", "gas_pipeline", "gas_processing_plant", "gas_storage", "lng_terminal", "ethanol_plant", "rng_project", "transmission_line"];
  // Lane R1 (2026-10-06): power plants that have retired, or will, have their own layer. The
  // existing-assets request asks for every other plant status, so a retired plant is never drawn
  // as an operating one; a retiring plant is still in service and shows in both.
  var EXISTING_PLANT_STATUSES = ["operating", "standby", "retiring", "unknown"];
  var RETIRED_LAYER_STATUSES = ["retired", "retiring"];
  // RNG technology families (us.epa.lmop, us.epa.agstar) are named by the server's technology
  // table (`#map-labels`, web/labels.py), like every other technology.
  var FUEL_ASSET_TYPES = ["ethanol_plant", "rng_project"];
  var CAPACITY_UNIT_LABEL = { "mmgal/yr": "MMgal/yr", mmscfd: "MMscf/d", "cu-ft/day": "cu ft/day" };
  var AGSTAR_HERD_KEYS = ["dairy", "swine", "cattle", "poultry"];
  var IN_VIEW_LIMIT = 500;
  var IN_VIEW_ASSET_LIMIT = 200;
  // Designer D-4 / frontend F6: the in-view list renders a page of rows per group and a "Show
  // more" button, not every row (at 400px it was 11,181px of county names below the map).
  var IN_VIEW_PAGE = 25;
  var WORLD_BBOX = [-179, -85, 179, 85];
  var WORLD_CENTER = [-98.5, 39.8];
  var WORLD_ZOOM = 3.2;

  // ---- the viewport in the URL (frontend audit F7; docs/04 D-17) ----
  // `center=<lng>,<lat>&zoom=<z>` beside the filter parameters, written with replaceState after
  // the map settles and read back on load, so a shared or bookmarked link opens on the place the
  // sender was looking at. The default national view writes nothing, so the home URL stays clean.
  function readView(params) {
    if (!params.has("center") || !params.has("zoom")) return null;
    var c = (params.get("center") || "").split(",").map(Number);
    var z = Number(params.get("zoom"));
    if (c.length !== 2 || !isFinite(c[0]) || !isFinite(c[1]) || !isFinite(z)) return null;
    if (Math.abs(c[0]) > 180 || Math.abs(c[1]) > 85 || z < 0 || z > 22) return null;
    return { center: [c[0], c[1]], zoom: z };
  }
  function isDefaultView(view) {
    return Math.abs(view.center[0] - WORLD_CENTER[0]) < 0.001 && Math.abs(view.center[1] - WORLD_CENTER[1]) < 0.001 &&
      Math.abs(view.zoom - WORLD_ZOOM) < 0.01;
  }
  function appendView(params, view) {
    if (!view || isDefaultView(view)) return;
    params.set("center", view.center[0].toFixed(4) + "," + view.center[1].toFixed(4));
    params.set("zoom", view.zoom.toFixed(2));
  }

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

  // The words the server prints for a token (`#map-labels`, rendered from
  // web/viewmodels.py::map_labels_json), so the in-view list and the drawer name a technology
  // exactly as the proposal list does. No copy of the words lives here: a missing or unreadable
  // tag leaves every token printed as itself, which is the server's own fallback too.
  var SERVER_LABELS = (function () {
    var el = document.getElementById("map-labels");
    try { return (el && JSON.parse(el.textContent)) || {}; } catch (e) { return {}; }
  })();
  function statusName(status) {
    if (!status) return null;
    var names = SERVER_LABELS.asset_status || {};
    return Object.prototype.hasOwnProperty.call(names, status) ? names[status] : String(status).replace(/_/g, " ");
  }
  // "Retired 2024" / "Retiring from 2028" / "Retirement scheduled 2030" from a feature's status and
  // `retirement_year` (geo features carry no attributes); null when the plant has no year.
  function retirementPhrase(p) {
    var year = yearOf(p.retirement_year);
    if (!year) return null;
    if (p.status === "retired") return "Retired " + year;
    if (p.status === "retiring") return "Retiring from " + year;
    return "Unit retirement scheduled " + year;
  }

  // A token no server table names still reads as words, never as itself (web/labels.py `humanise`).
  function humanise(token) {
    var text = String(token).replace(/_/g, " ").trim();
    return text.charAt(0).toUpperCase() + text.slice(1);
  }
  function serverName(table, token) {
    if (token == null || token === "") return null;
    var names = SERVER_LABELS[table] || {};
    return Object.prototype.hasOwnProperty.call(names, token) ? names[token] : humanise(token);
  }
  function technologyName(tech) { return serverName("technology", tech); }
  var PLANT_FAMILY_NAME = SERVER_LABELS.plant_family || {};
  function lifecycleName(state) { return serverName("lifecycle", state || "unknown"); }
  function reuseClassName(cls) { return serverName("reuse_class", cls); }

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

  // "345 kV" from a transmission line's `attributes.voltage_kv`; null when the source states none.
  function voltageLabelOf(p) {
    var kv = Number(attrOf(p, ["voltage_kv"]));
    return isFinite(kv) && kv > 0 ? String(Math.round(kv * 10) / 10) + " kV" : null;
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
    return technologyName(technology);
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

  // Proposal filters the site passes through (web/app.py PROPOSAL_PASSTHROUGH_FILTERS, rendered
  // into the form's `data-passthrough`, so this file keeps no second, drifting list). Four have a
  // control on this page; every other one is read from the URL, kept in it and forwarded to the
  // geo request untouched, so `/?state=US-VA` narrows the map exactly as `/proposals?state=US-VA`
  // narrows the list (2026-09-29: only four were known here and `/?kind=load` drew 5,853
  // proposals under a notice counting 46, then rewrote the URL without `kind`). `lifecycle_state`
  // sits outside that tuple because the server resolves it (web/viewmodels.py
  // resolve_proposal_lifecycle_param) and is kept the same way. Anything else is not forwarded.
  var FORM_CONTROLLED_FILTERS = ["technology", "kind", "jurisdiction", "placement"];
  var PASSTHROUGH_FILTERS = (document.getElementById("map-filters").getAttribute("data-passthrough") || "")
    .split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  var URL_ONLY_FILTERS = PASSTHROUGH_FILTERS.filter(function (name) {
    return FORM_CONTROLLED_FILTERS.indexOf(name) === -1;
  }).concat(["lifecycle_state"]);

  function readUrlOnlyFilters(params) {
    var out = {};
    URL_ONLY_FILTERS.forEach(function (name) {
      var value = params.get(name);
      if (value) out[name] = value;
    });
    return out;
  }

  // The proposal filters (not the layers, region or placement) as query parameters: the URL, the
  // geo request and the notice fragment all carry exactly this set.
  function appendProposalFilters(params, filters) {
    if (filters.technology) params.set("technology", filters.technology);
    if (filters.kind) params.set("kind", filters.kind);
    if (filters.jurisdiction) params.set("jurisdiction", filters.jurisdiction);
    if (filters.include_withdrawn) params.set("include_withdrawn", "1");
    var urlOnly = filters.url_only || {};
    URL_ONLY_FILTERS.forEach(function (name) {
      if (urlOnly[name]) params.set(name, urlOnly[name]);
    });
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
      kind: params.get("kind") || "",
      jurisdiction: params.get("jurisdiction") || "",
      include_withdrawn: params.get("include_withdrawn") === "1",
      url_only: readUrlOnlyFilters(params),
      layers: layersParam ? layersParam.split(",").filter(Boolean) : [],
      region: params.get("region") || "",
      plant_technology: PLANT_FAMILY_CLASSES[params.get("plant_technology") || ""] ? params.get("plant_technology") : "",
      asset_types: readAssetTypes(params),
      placement: readPlacement(params),
      view: readView(params),
      // A record page's "Show on map" link (designer D-8) names the proposal to open on arrival.
      focus: /^prop_[A-Za-z0-9]+$/.test(params.get("focus") || "") ? params.get("focus") : ""
    };
  }

  function writeFilters(filters) {
    var params = new URLSearchParams();
    appendProposalFilters(params, filters);
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
    var filterQs = params.toString();
    appendView(params, filters.view);
    var qs = params.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "");
    window.history.replaceState(null, "", url);
    // "View as: List" (designer D-8) carries the filters and the viewport, so the list's own
    // "View as: Map" link can bring the reader back to this place.
    // The map's default placement is a drawing choice, not a filter: the list shows unplaced
    // proposals too, which is where the unplaced note sends readers.
    var listLink = document.getElementById("view-as-list");
    if (listLink) {
      var listParams = new URLSearchParams(qs);
      if (listParams.get("placement") === DEFAULT_PLACEMENT.join(",")) listParams.delete("placement");
      ["layers", "region", "plant_technology", "asset_type"].forEach(function (k) { listParams.delete(k); });
      var listQs = listParams.toString();
      listLink.href = "/proposals" + (listQs ? "?" + listQs : "");
    }
    // "Save this search as an alert" carries the current filters (web/alerts.py drops the map's
    // own default placement and the layer/asset keys, which are not proposal filters).
    var alertLink = document.getElementById("mf-save-alert");
    if (alertLink) alertLink.href = "/alerts/new?entity=proposal&origin=map" + (filterQs ? "&" + filterQs : "");
    // The collapsed "Filters" button names how many are set (site.js); filters that arrived in
    // the link with no control here count too.
    var form = document.getElementById("map-filters");
    if (form) {
      form.setAttribute("data-extra-active", String(Object.keys(filters.url_only || {}).length));
      if (typeof Event === "function") form.dispatchEvent(new Event("filters:changed"));
    }
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
    appendProposalFilters(params, filters);
    // ADR 0008 placement grades (docs/23 §3.1): csv of exact|region|none, API default
    // "exact,region" -- sent explicitly rather than relying on that default so the map always
    // requests exactly what the three checkboxes show, including when "none" is checked (its
    // only visible effect: `totals.unplaced` is then populated, see `render()`'s unplaced note).
    params.set("placement", (filters.placement && filters.placement.length ? filters.placement : DEFAULT_PLACEMENT).join(","));
    return "/api/proposals/geo?" + params.toString();
  }

  // The lifecycle notice above the filters, re-rendered by the server (web/app.py
  // proposals_notice_fragment) for the filters just applied, so it and the count line describe
  // the same set after a change as they do on first paint.
  function noticeUrl(filters) {
    var params = new URLSearchParams();
    appendProposalFilters(params, filters);
    params.set("placement", (filters.placement && filters.placement.length ? filters.placement : DEFAULT_PLACEMENT).join(","));
    // Not counted: only so the notice's "Remove this filter" links keep the reader's place.
    appendView(params, filters.view);
    return "/api/proposals/notice?" + params.toString();
  }

  // ADR 0008 task item 2 + midstream slice: the existing-assets fetch points at `/v1/assets/geo`
  // with the picked types as an `asset_type` csv. The API applies `technology` across every type
  // it returns, so a plant-type family (which only power plants carry) is sent on a separate
  // power-plant-only request and the other types are fetched without it -- `assetsGeoUrls`
  // returns one or two URLs, merged by `refetchAssets`.
  function assetsGeoUrls(filters, bbox, zoom) {
    var types = filters.asset_types && filters.asset_types.length ? filters.asset_types : ASSET_TYPES_LIVE;
    var family = filters.plant_technology && PLANT_FAMILY_CLASSES[filters.plant_technology] ? filters.plant_technology : "";
    function url(typeList, technologyCsv, statusCsv) {
      var params = new URLSearchParams();
      params.set("bbox", bbox.join(","));
      params.set("zoom", String(zoom));
      params.set("asset_type", typeList.join(","));
      if (technologyCsv) params.set("technology", technologyCsv);
      if (statusCsv) params.set("status", statusCsv);
      return "/api/assets/geo?" + params.toString();
    }
    if (types.indexOf("power_plant") === -1) return [url(types, "", "")];
    // Power plants always go on a request of their own: the status filter (no retired plants
    // here, lane R1) and a plant-type family apply to plants only.
    var others = types.filter(function (t) { return t !== "power_plant"; });
    var urls = [url(["power_plant"], family ? PLANT_FAMILY_CLASSES[family].join(",") : "", EXISTING_PLANT_STATUSES.join(","))];
    if (others.length) urls.push(url(others, "", ""));
    return urls;
  }

  // The "Retired & retiring plants" layer (lane R1): power plants of those two statuses only.
  function retiredGeoUrl(bbox, zoom) {
    var params = new URLSearchParams();
    params.set("bbox", bbox.join(","));
    params.set("zoom", String(zoom));
    params.set("asset_type", "power_plant");
    params.set("status", RETIRED_LAYER_STATUSES.join(","));
    return "/api/assets/geo?" + params.toString();
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
      '<span class="chip__label">' + esc(lifecycleName(label)) + "</span></span>"
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
  document.getElementById("mf-kind").value = filters.kind;
  document.getElementById("mf-jurisdiction").value = filters.jurisdiction;
  document.getElementById("mf-include-withdrawn").checked = filters.include_withdrawn;
  var plantsToggle = document.getElementById("mf-layer-plants");
  plantsToggle.checked = filters.layers.indexOf("plants") !== -1;
  var retiredToggle = document.getElementById("mf-layer-retired");
  retiredToggle.checked = filters.layers.indexOf("retired") !== -1;
  var retiredLegend = document.getElementById("retired-legend");
  // `layers=` is a csv of independent layers ("plants", "retired"); turning one on or off keeps the other.
  function setLayerOn(name, on) {
    var kept = (filters.layers || []).filter(function (l) { return l !== name; });
    if (on) kept.push(name);
    filters.layers = kept;
  }
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
    center: filters.view ? filters.view.center : WORLD_CENTER,
    zoom: filters.view ? filters.view.zoom : WORLD_ZOOM,
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
  // not a user-initiated jump, so it does not re-emit `map.region_jumped`). A `center`/`zoom` in
  // the link is more precise than the region it started from, so it wins.
  if (filters.region && !filters.view) {
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
  var latestRetired = { type: "FeatureCollection", features: [] };
  var latestRetiredTotal = 0;
  var latestRetiredClustered = false;

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
  var linesNote = document.getElementById("lines-note");
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

  var mapErrorEl = document.getElementById("map-error");
  var hasLoadedOnce = false;
  function hideMapError() {
    hasLoadedOnce = true;
    if (mapErrorEl) { mapErrorEl.hidden = true; mapErrorEl.innerHTML = ""; }
  }
  function showMapError(err) {
    var problem = (err && err.problem) || {};
    var title = problem.title || (err && err.status ? "The server answered " + err.status : "The request did not complete");
    var listHref = (document.getElementById("view-as-list") || {}).href || "/proposals";
    if (!hasLoadedOnce) {
      countEl.textContent = "Proposals could not be loaded.";
      countEl.removeAttribute("aria-hidden");
    }
    if (!mapErrorEl) return;
    mapErrorEl.innerHTML =
      "<p><strong>Couldn\u2019t load proposals for this view.</strong> " + esc(title) +
      (problem.request_id ? " (request <span class=\"mono\">" + esc(problem.request_id) + "</span>)" : "") + ". " +
      (hasLoadedOnce ? "The map still shows the last view that loaded. " : "") +
      "<button type=\"button\" class=\"map-error__retry\">Retry</button> " +
      "<a href=\"" + esc(safeUrl(listHref) || "/proposals") + "\">Try the list view</a></p>";
    mapErrorEl.hidden = false;
    mapErrorEl.querySelector(".map-error__retry").addEventListener("click", function () { refetch(); });
  }

  // A record page's "Show on map" link (`?focus=prop_...`) opens that proposal's drawer once it is
  // drawn as a point in the first view that holds it. One shot: the URL drops `focus` on load.
  var focusPending = filters.focus;
  function openFocusedProposal() {
    if (!focusPending) return;
    var hit = latestCollection.features.filter(function (f) {
      return f.properties.feature_kind === "proposal" && f.properties.public_id === focusPending;
    })[0];
    if (!hit) return;
    focusPending = "";
    openDrawer(hit.properties);
  }

  // In-view paging: how many rows each group shows; a new view or filter starts again at a page.
  var inViewShown = {};
  function resetInViewPages() {
    inViewShown = { proposals: IN_VIEW_PAGE, regions: IN_VIEW_PAGE, assets: IN_VIEW_PAGE, retired: IN_VIEW_PAGE };
  }
  resetInViewPages();
  var focusAfterRender = null;
  // "Show N more" after a group's rows; on click the next page renders and focus moves to its
  // first row, so a keyboard reader carries on from where they were.
  function appendShowMore(group, shown, total, nouns) {
    if (total <= shown) return;
    var li = document.createElement("li");
    li.className = "in-view-list__more";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "show-more-btn";
    var next = Math.min(IN_VIEW_PAGE, total - shown);
    btn.textContent = "Show " + next + " more " + nouns + " (" + fmtCount(total - shown) + " not listed)";
    btn.addEventListener("click", function () {
      focusAfterRender = { group: group, index: shown };
      inViewShown[group] = shown + IN_VIEW_PAGE;
      render();
    });
    li.appendChild(btn);
    listEl.appendChild(li);
  }
  function markRow(node, group, index) {
    var li = node.nodeType === 11 ? node.querySelector("li") : node;
    if (li) { li.setAttribute("data-group", group); li.setAttribute("data-index", String(index)); }
  }

  // Audit 2026-09-30 F3: a slow answer for an earlier viewport or filter set used to land after
  // the current one and redraw the map, the list and the count for a view no longer on screen.
  // Each request now carries a sequence number and only the newest is applied, as the assets and
  // notice fetches already did; the superseded request is also aborted where the browser can.
  var proposalsFetchSeq = 0;
  var proposalsAbort = null;
  function refetch() {
    var seq = ++proposalsFetchSeq;
    if (proposalsAbort) proposalsAbort.abort();
    proposalsAbort = typeof AbortController === "function" ? new AbortController() : null;
    resetInViewPages();
    fetch(geoUrl(filters, currentBbox(), currentZoom()), proposalsAbort ? { signal: proposalsAbort.signal } : undefined)
      .then(function (r) {
        if (r.ok) return r.json();
        // An RFC 9457 problem body names the failure and its request id (docs/31 §6).
        return r.json().catch(function () { return {}; }).then(function (problem) {
          var err = new Error("proposals request failed");
          err.problem = problem || {};
          err.status = r.status;
          throw err;
        });
      })
      .then(function (envelope) {
        if (seq !== proposalsFetchSeq) return; // a newer request owns the view
        hideMapError();
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
        updateLifecycleLegend((fc.totals || {}).lifecycle_state_counts || {});
        latestRegionFeatures = regionFeatures;
        if (map.getSource("proposals")) {
          map.getSource("proposals").setData({ type: "FeatureCollection", features: pointFeatures });
        }
        ensureRegionPolygons(regionFeatures, function () { updateRegionsLayer(regionFeatures); });
        render();
        openFocusedProposal();
      })
      .catch(function (err) {
        // docs/31 §6 error state: keep the last-known view rather than blanking it, and say so,
        // with the problem's title and request id and a way to try again (designer D-11).
        if (seq !== proposalsFetchSeq || (err && err.name === "AbortError")) return;
        showMapError(err);
      });
    if (plantsToggle.checked) refetchAssets();
    if (retiredToggle.checked) refetchRetired();
  }

  // Lane R1: hue by status, ring shape by status (a dot for retiring, a cross for retired), size
  // by nameplate MW so the big sites read first. Clusters take the retired hue.
  var retiredColors = {
    retired: cssVar("--asset-retired") || "#7a2e5c",
    retiring: cssVar("--asset-retiring") || "#a3480a"
  };
  function decorateRetiredFeature(f) {
    var p = f.properties;
    if (p.feature_kind === "asset_cluster" || p.feature_kind === "plant_cluster") {
      p.color = retiredColors.retired;
      return;
    }
    if (p.feature_kind === "plant") p.feature_kind = "asset";
    p.asset_type = "power_plant";
    p.color = retiredColors[p.status] || retiredColors.retired;
    p.icon = p.status === "retiring" ? "retiring-dot" : "retired-crossed";
    var mw = Number(p.capacity_mw) || 0;
    p.ring_size = mw >= 1000 ? 1.0 : mw >= 250 ? 0.8 : mw >= 50 ? 0.62 : 0.5;
  }

  var retiredFetchSeq = 0;
  function refetchRetired() {
    var seq = ++retiredFetchSeq;
    fetch(retiredGeoUrl(currentBbox(), currentZoom()))
      .then(function (r) { return r.json(); })
      .then(function (envelope) {
        if (seq !== retiredFetchSeq) return;
        var fc = envelope.data || {};
        var features = (fc.features || []).filter(function (f) { return f.properties.feature_kind !== "asset_line"; });
        features.forEach(decorateRetiredFeature);
        latestRetired = { type: "FeatureCollection", features: features };
        latestRetiredTotal = assetsInView(features);
        latestRetiredClustered = !!(fc.totals || {}).clustered;
        if (map.getSource("retired-plants")) map.getSource("retired-plants").setData(latestRetired);
        render();
      })
      .catch(function () { /* keep the last-known layer (docs/31 §6 error state) */ });
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
      // A transmission line's class is its voltage (tooltip, in-view row and drawer subtitle all
      // print `line_class`); it is never "intrastate", so it never lands in the dashed pipeline layer.
      p.line_class = (type === "transmission_line" ? voltageLabelOf(p) : lineClassOf(p)) || "unknown";
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
        var lineCount = 0;
        var linesShown = 0;
        collections.forEach(function (fc) {
          (fc.features || []).forEach(function (f) { decorateAssetFeature(f); features.push(f); });
          var t = fc.totals || {};
          records += t.records || 0;
          if (t.clustered) clustered = true;
          // `/v1/assets/geo` keeps only the longest LINE_FEATURE_CAP lines in a viewport and says
          // so in `line_count` / `lines_shown` (services/api/assets.py); F4: say it on the page.
          if (t.line_count != null) {
            lineCount += Number(t.line_count) || 0;
            linesShown += Number(t.lines_shown != null ? t.lines_shown : t.line_count) || 0;
          }
        });
        latestAssets = {
          type: "FeatureCollection", features: features,
          totals: { records: records, clustered: clustered, line_count: lineCount, lines_shown: linesShown }
        };
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

  // Designer D-2: the status key lists only the families the filters can draw.
  var lifecycleLegendItems = Array.prototype.slice.call(document.querySelectorAll("[data-legend-family]"));
  function updateLifecycleLegend(counts) {
    var present = {};
    Object.keys(counts).forEach(function (state) { if (Number(counts[state]) > 0) present[familyOf(state)] = true; });
    lifecycleLegendItems.forEach(function (item) { item.hidden = !present[item.getAttribute("data-legend-family")]; });
  }
  function plural(n, noun) { return fmtCount(n) + " " + noun + (n === 1 || /more$/.test(noun) ? "" : "s"); }
  function fmtCount(n) { return Number(n || 0).toLocaleString("en-US"); }
  // US state postal codes by FIPS prefix, so a county reads "Douglas County, GA" rather than
  // "Douglas (county)": there are 30 Douglas counties (designer D-14).
  var STATE_BY_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "72": "PR"
  };
  function regionName(p) {
    var name = p.name || String(p.region_id || "Unnamed region");
    var id = String(p.region_id || "");
    if (p.region_level === "county" && /^\d{5}$/.test(id)) {
      var st = STATE_BY_FIPS[id.slice(0, 2)];
      var noun = st === "LA" ? "Parish" : st === "AK" ? "" : "County";
      var already = /\b(County|Parish|Borough|Census Area|Municipality|city)$/i.test(name);
      return name + (noun && !already ? " " + noun : "") + (st ? ", " + st : "");
    }
    if (p.region_level === "state") return name;
    return name + " (" + humanise(p.region_level || "region").toLowerCase() + ")";
  }

  function appendGroupName(text) {
    var li = document.createElement("li");
    li.className = "in-view-list__group-name";
    // A plain list item, not role="presentation": that role strips the item's listitem semantics,
    // which leaves the <ul> directly containing a non-item (axe `list`, serious; found 2026-09-27
    // once CI scanned a page with real rows).
    li.textContent = text;
    listEl.appendChild(li);
  }

  function render() {
    var totals = latestCollection.totals || {};
    // The in-view list says when the view is clustered; this line stays one short sentence.
    countEl.textContent =
      (totals.records || 0) + " proposal" + (totals.records === 1 ? "" : "s") + " match these filters.";
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
      var groups = groupProposalFeatures(individual).slice(0, IN_VIEW_LIMIT);
      if (!groups.length) {
        var none = document.createElement("li");
        none.className = "in-view-list__empty";
        none.textContent = (totals.records || 0) === 0
          ? "No proposals match these filters. The note above names each one; Clear all removes them."
          : "No proposals in this part of the map. Zoom out or move the map to see the " + fmtCount(totals.records) + " that match.";
        if (!latestRegionFeatures.length) listEl.appendChild(none);
      }
      var shownProposals = Math.min(inViewShown.proposals, groups.length);
      groups.slice(0, shownProposals).forEach(function (g, index) {
        var p = g.properties;
        var node = template.content.cloneNode(true);
        markRow(node, "proposals", index);
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
        var meta = (technologyName(p.technology) || "—") + " · " + (p.state_code || p.county_name || "—") +
          (g.capacity_mw ? " · " + g.capacity_mw.toFixed(1) + " MW" : "");
        node.querySelector(".meta").textContent = meta;
        // D-13: the drawer (sources, licence) was reachable only by clicking a dot on the canvas.
        // Every proposal row now has the same Details button the asset rows have.
        var details = node.querySelector(".details-btn");
        if (details) {
          details.setAttribute("aria-label", "Details for " + (p.name || "this proposal"));
          details.addEventListener("click", function () { openDrawer(p); });
        }
        listEl.appendChild(node);
      });
      appendShowMore("proposals", shownProposals, groups.length, "proposals");
    }

    // Region records (ADR 0008 placement grade "region") in the list as well as on the map, so a
    // keyboard or screen-reader user reaches them without a pointer (web audit 2026-09-18).
    if (latestRegionFeatures.length) {
      appendGroupName("Regions (" + latestRegionFeatures.length + ")");
      var regionRows = latestRegionFeatures.slice(0, IN_VIEW_LIMIT);
      var shownRegions = Math.min(inViewShown.regions, regionRows.length);
      regionRows.slice(0, shownRegions).forEach(function (f, index) {
        var p = f.properties;
        var item = document.createElement("li");
        markRow(item, "regions", index);
        var a = document.createElement("a");
        a.href = regionListUrl(p);
        a.textContent = regionName(p);
        item.appendChild(a);
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = Number(p.count || 0) + " proposal" + (Number(p.count) === 1 ? "" : "s") + " placed at this region";
        item.appendChild(meta);
        listEl.appendChild(item);
      });
      appendShowMore("regions", shownRegions, regionRows.length, "regions");
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
      var assetRows = assetsIndividual.slice(0, IN_VIEW_ASSET_LIMIT);
      var shownAssets = Math.min(inViewShown.assets, assetRows.length);
      assetRows.slice(0, shownAssets).forEach(function (f, index) {
        var p = f.properties;
        var item = document.createElement("li");
        markRow(item, "assets", index);
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
      appendShowMore("assets", shownAssets, assetRows.length, "assets");
    }

    // Lane R1: retired and retiring plants in view, one row each, the status in words.
    if (retiredToggle.checked) {
      var retiredIndividual = latestRetired.features.filter(function (f) { return f.properties.feature_kind === "asset"; });
      if (latestRetiredClustered || retiredIndividual.length) {
        appendGroupName("Retired & retiring plants (" + latestRetiredTotal + ")");
      }
      if (latestRetiredClustered) {
        var rcl = document.createElement("li");
        rcl.textContent = "Zoom in to list retired and retiring plants individually; " + latestRetiredTotal + " are grouped in clusters above.";
        listEl.appendChild(rcl);
      }
      var retiredRows = retiredIndividual.slice(0, IN_VIEW_ASSET_LIMIT);
      var shownRetired = Math.min(inViewShown.retired, retiredRows.length);
      retiredRows.slice(0, shownRetired).forEach(function (f, index) {
        var p = f.properties;
        var item = document.createElement("li");
        markRow(item, "retired", index);
        var kind = document.createElement("span");
        kind.className = "in-view-list__kind";
        kind.textContent = statusName(p.status) || "Plant";
        item.appendChild(kind);
        var pageUrl = assetPageUrl(p);
        var a = document.createElement(pageUrl ? "a" : "strong");
        if (pageUrl) { a.className = "name-link"; a.href = pageUrl; }
        a.textContent = p.name || p.public_id || "Unnamed plant";
        item.appendChild(a);
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "details-btn";
        btn.textContent = "Details";
        btn.setAttribute("aria-label", "Details for " + (p.name || "this plant"));
        btn.addEventListener("click", function () { openAssetDrawer(p); });
        item.appendChild(btn);
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = [
          p.capacity_mw ? Number(p.capacity_mw).toFixed(1) + " MW" : null,
          PLANT_FAMILY_NAME[plantFamilyOf(p.technology)] || null,
          retirementPhrase(p),
          p.state_code || null
        ].filter(Boolean).join(" · ");
        item.appendChild(meta);
        listEl.appendChild(item);
      });
      appendShowMore("retired", shownRetired, retiredRows.length, "plants");
    }
    if (focusAfterRender) {
      var target = listEl.querySelector('[data-group="' + focusAfterRender.group + '"][data-index="' + focusAfterRender.index + '"]');
      var focusable = target && target.querySelector("a, button");
      if (focusable) focusable.focus();
      focusAfterRender = null;
    }

    // What the live region says is what the view holds (audit 2026-09-30 F4). `totals.records`
    // counts every proposal matching the filters anywhere, so it stays on the count line above
    // ("match these filters"); "in view" is counted from the features drawn: a cluster stands
    // for its `count`, a point for one, and region-placed proposals are named separately.
    var proposalsInView = 0;
    latestCollection.features.forEach(function (f) {
      var kind = f.properties.feature_kind;
      if (kind === "cluster") proposalsInView += Number(f.properties.count) || 0;
      else if (kind === "proposal") proposalsInView += 1;
    });
    var regionPlaced = 0;
    latestRegionFeatures.forEach(function (f) { regionPlaced += Number(f.properties.count) || 0; });
    var liveText = plural(proposalsInView, "proposal") + " in view";
    if (regionPlaced) liveText += ", and " + plural(regionPlaced, "more") + " placed by county or region";
    liveText += ".";
    var linesNoteText = "";
    if (plantsToggle.checked) {
      var linesByType = {};
      latestAssets.features.forEach(function (f) {
        if (f.properties.feature_kind !== "asset_line") return;
        var type = assetTypeOf(f.properties);
        linesByType[type] = (linesByType[type] || 0) + 1;
      });
      // Each line type by its own name: a transmission line is not a pipeline.
      var lineParts = Object.keys(linesByType).map(function (type) {
        var n = linesByType[type];
        return n + " " + (n === 1 ? ASSET_TYPES[type].label.toLowerCase() : ASSET_TYPES[type].plural);
      });
      liveText += " " + plural(latestPlantsTotal, "existing asset") + " in view" + (lineParts.length ? " (" + lineParts.join(", ") + ")" : "") + ".";
      var lt = latestAssets.totals || {};
      if (lt.line_count && lt.lines_shown != null && lt.lines_shown < lt.line_count) {
        linesNoteText = "Showing the longest " + fmtCount(lt.lines_shown) + " of " + fmtCount(lt.line_count) +
          " lines in this view; zoom in to see them all.";
        liveText += " " + linesNoteText;
      }
    }
    if (linesNote) {
      linesNote.hidden = !linesNoteText;
      linesNote.textContent = linesNoteText;
    }
    if (retiredToggle.checked) liveText += " " + plural(latestRetiredTotal, "retired or retiring plant") + " in view.";
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
    // Lane R1: hollow rings for retired (crossed) and retiring (centre dot) plants.
    if (shape === "retired-crossed" || shape === "retiring-dot") {
      ctx.lineWidth = Math.max(2, size / 7);
      ctx.beginPath();
      ctx.arc(c, c, r - ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.stroke();
      if (shape === "retiring-dot") {
        ctx.beginPath();
        ctx.arc(c, c, size / 7, 0, Math.PI * 2);
        ctx.fill();
      } else {
        var k = r * 0.55;
        ctx.beginPath();
        ctx.moveTo(c - k, c - k); ctx.lineTo(c + k, c + k);
        ctx.moveTo(c + k, c - k); ctx.lineTo(c - k, c + k);
        ctx.stroke();
      }
      return ctx.getImageData(0, 0, size, size);
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

  // The five family glyphs as canvas paths in the 12-unit box of base.html's `#icon-*` symbols.
  function drawFamilyGlyph(ctx, family, scale, ox, oy, color) {
    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(scale, scale);
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath();
    if (family === "neutral") {
      ctx.lineWidth = 1.8; ctx.arc(6, 6, 4.3, 0, Math.PI * 2); ctx.stroke();
    } else if (family === "progress") {
      ctx.lineWidth = 0.9; ctx.arc(6, 6, 4.3, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(6, 1.7); ctx.arc(6, 6, 4.3, -Math.PI / 2, Math.PI / 2); ctx.closePath(); ctx.fill();
    } else if (family === "committed") {
      ctx.fillRect(2.2, 2.2, 7.6, 7.6);
    } else if (family === "success") {
      ctx.lineWidth = 2; ctx.moveTo(2.2, 6.3); ctx.lineTo(4.6, 8.7); ctx.lineTo(9.8, 3.3); ctx.stroke();
    } else {
      ctx.lineWidth = 1.5; ctx.moveTo(6, 1.2); ctx.lineTo(10.9, 10.4); ctx.lineTo(1.1, 10.4); ctx.closePath(); ctx.stroke();
      ctx.beginPath(); ctx.lineWidth = 1.4; ctx.moveTo(6, 4.6); ctx.lineTo(6, 7.1); ctx.stroke();
      ctx.beginPath(); ctx.arc(6, 8.7, 0.75, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }
  function familyImage(size, paint) {
    var ratio = 2;
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size * ratio;
    var ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    paint(ctx);
    return { image: ctx.getImageData(0, 0, size * ratio, size * ratio), ratio: ratio };
  }
  function addFamilyImages() {
    FAMILIES.forEach(function (family) {
      var text = colors[family];
      var fill = cssVar("--family-" + family + "-fill") || mapColors.land;
      var marker = familyImage(20, function (ctx) {
        ctx.beginPath(); ctx.arc(10, 10, 8.6, 0, Math.PI * 2);
        ctx.fillStyle = fill; ctx.fill();
        ctx.lineWidth = 1.6; ctx.strokeStyle = text; ctx.stroke();
        drawFamilyGlyph(ctx, family, 1.0, 4, 4, text);
      });
      map.addImage("family-marker-" + family, marker.image, { pixelRatio: marker.ratio });
      var glyph = familyImage(12, function (ctx) { drawFamilyGlyph(ctx, family, 1.0, 0, 0, text); });
      map.addImage("family-glyph-" + family, glyph.image, { pixelRatio: glyph.ratio });
    });
  }

  var ASSET_LAYER_IDS = [
    "plant-clusters", "plant-cluster-count", "asset-lines-casing", "asset-lines", "asset-lines-intrastate",
    "asset-lines-transmission", "asset-lines-hit", "asset-line-labels", "plant-points", "plant-labels"
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
    var isTransmission = ["==", ["get", "asset_type"], "transmission_line"];
    map.addLayer({
      id: "asset-lines", type: "line", source: "plants",
      filter: ["all", isLine, ["!=", ["get", "line_class"], "intrastate"], ["!", isTransmission]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": 0.85 }
    }, "clusters");
    map.addLayer({
      id: "asset-lines-intrastate", type: "line", source: "plants",
      filter: ["all", isLine, ["==", ["get", "line_class"], "intrastate"]],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": 0.85, "line-dasharray": [3, 2] }
    }, "clusters");
    // Transmission lines: dash-dot, same width ramp and casing as pipelines (docs/31 §1.6).
    map.addLayer({
      id: "asset-lines-transmission", type: "line", source: "plants",
      filter: ["all", isLine, isTransmission],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": 0.85, "line-dasharray": [4, 1.5, 1, 1.5] }
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

  var RETIRED_LAYER_IDS = ["retired-clusters", "retired-cluster-count", "retired-points", "retired-labels"];

  // Lane R1: its own source and layers, added after the existing-assets layers and still below
  // every proposals layer (same `addLayer(..., "clusters")` stacking rule as `addPlantsLayers`).
  function addRetiredLayers() {
    map.addImage("retired-crossed", buildShapeIcon("retired-crossed", 24), { sdf: true });
    map.addImage("retiring-dot", buildShapeIcon("retiring-dot", 24), { sdf: true });
    map.addSource("retired-plants", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    var isCluster = ["any", ["==", ["get", "feature_kind"], "asset_cluster"], ["==", ["get", "feature_kind"], "plant_cluster"]];
    var isPoint = ["all", ["==", ["geometry-type"], "Point"], ["!", isCluster]];
    map.addLayer({
      id: "retired-clusters", type: "circle", source: "retired-plants",
      filter: isCluster,
      paint: {
        "circle-radius": ["step", ["get", "count"], 10, 10, 14, 50, 18],
        "circle-color": mapColors.land,
        "circle-opacity": 0.85,
        "circle-stroke-width": 2,
        "circle-stroke-color": ["get", "color"]
      }
    }, "clusters");
    map.addLayer({
      id: "retired-cluster-count", type: "symbol", source: "retired-plants",
      filter: isCluster,
      layout: { "text-field": ["get", "count"], "text-size": 10, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": ["get", "color"] }
    }, "clusters");
    map.addLayer({
      id: "retired-points", type: "symbol", source: "retired-plants",
      filter: isPoint,
      layout: {
        "icon-image": ["coalesce", ["get", "icon"], "retired-crossed"],
        "icon-size": ["coalesce", ["get", "ring_size"], 0.6],
        "icon-allow-overlap": true
      },
      paint: { "icon-color": ["get", "color"], "icon-opacity": 0.95 }
    }, "clusters");
    map.addLayer({
      id: "retired-labels", type: "symbol", source: "retired-plants", minzoom: 8,
      filter: isPoint,
      layout: {
        "text-field": ["coalesce", ["get", "name"], ""], "text-size": 9, "text-font": ["Noto Sans Medium"],
        "text-anchor": "top", "text-offset": [0, 0.9], "text-allow-overlap": false
      },
      paint: { "text-color": ["get", "color"], "text-halo-color": mapColors.land, "text-halo-width": 1.2 }
    }, "clusters");
    map.on("click", "retired-points", function (e) { openAssetDrawer(e.features[0].properties); });
    map.on("mouseenter", "retired-points", function (e) { map.getCanvas().style.cursor = "pointer"; showAssetTooltip(e, e.features[0].geometry.coordinates); });
    map.on("mouseleave", "retired-points", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("click", "retired-clusters", function (e) {
      var f = e.features[0];
      map.easeTo({ center: f.geometry.coordinates, zoom: f.properties.expands_to_zoom || (map.getZoom() + 2), duration: motionMs(600) });
    });
  }

  function setRetiredLayerVisible(on) {
    RETIRED_LAYER_IDS.forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    });
    retiredLegend.hidden = !on;
    retiredLegend.setAttribute("aria-hidden", on ? "false" : "true");
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
    addRetiredLayers();
    setRetiredLayerVisible(retiredToggle.checked);
    // Designer D-2 / docs/31 §5.1: lifecycle is carried by shape as well as hue, the same five
    // glyphs the status chips and the legend use (`#icon-*` in base.html). A cluster shows its
    // dominant family's glyph above the count; a point is a small chip: the family's pale fill,
    // a family-coloured ring and the glyph (frontend F11: no white text on a pale dark-mode
    // fill any more; every pair here is a chip pair, >= 6:1 in both themes).
    addFamilyImages();
    map.addLayer({
      id: "cluster-count", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "cluster"],
      layout: {
        "text-field": ["get", "count"], "text-size": 12, "text-font": ["Noto Sans Medium"],
        "text-anchor": "top", "text-offset": [0, -0.15],
        "icon-image": ["concat", "family-glyph-", ["get", "family"]], "icon-anchor": "bottom", "icon-offset": [0, 1],
        "icon-allow-overlap": true, "text-allow-overlap": true, "icon-ignore-placement": true
      },
      paint: { "text-color": colorExpr }
    });
    // Licence-limited precision (D-9) keeps its own mark: a second, wider ring round the chip.
    map.addLayer({
      id: "points-precision", type: "circle", source: "proposals",
      filter: ["all", ["==", ["get", "feature_kind"], "proposal"], ["==", ["get", "precision_reason"], "licence"]],
      paint: { "circle-radius": 11, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 1, "circle-stroke-color": colorExpr }
    });
    map.addLayer({
      id: "points", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "proposal"],
      layout: {
        "icon-image": ["concat", "family-marker-", ["get", "family"]],
        "icon-allow-overlap": true, "icon-ignore-placement": true
      }
    });
    // The technology badge reads once the dots are apart (z9+), beside the marker in body text
    // colour on a land-coloured halo, never inside it.
    map.addLayer({
      id: "point-labels", type: "symbol", source: "proposals", minzoom: 9,
      filter: ["==", ["get", "feature_kind"], "proposal"],
      layout: {
        "text-field": ["get", "tech_label"], "text-size": 9, "text-font": ["Noto Sans Medium"],
        "text-anchor": "left", "text-offset": [0.9, 0], "text-optional": true
      },
      paint: { "text-color": cssVar("--text") || "#16324f", "text-halo-color": mapColors.land, "text-halo-width": 1.4 }
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
    var viewTimer = null;
    map.on("moveend", function () {
      window.clearTimeout(moveTimer);
      moveTimer = window.setTimeout(refetch, 200);
      // F7: the URL follows the view, debounced so a fling writes once, with replaceState so
      // panning does not fill the Back button's history.
      window.clearTimeout(viewTimer);
      viewTimer = window.setTimeout(function () {
        var c = map.getCenter();
        filters.view = { center: [c.lng, c.lat], zoom: map.getZoom() };
        writeFilters(filters);
      }, 300);
    });
    refetch();
  });

  var tooltip = null;
  function showClusterTooltip(e) {
    var f = e.features[0];
    var p = f.properties;
    var counts = propObj(p.lifecycle_state_counts) || {};
    var lines = Object.keys(counts).sort().map(function (k) { return esc(lifecycleName(k)) + " " + Number(counts[k] || 0); });
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
    var retirement = p.status === "retired" || p.status === "retiring" ? retirementPhrase(p) || statusName(p.status) : null;
    var html = "<strong>" + esc(p.name || "Unnamed asset") + "</strong><br>" + esc(assetTypeLabel(p.asset_type)) +
      (retirement ? " &middot; " + esc(retirement) : "") +
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
  // Only the latest response is applied, so a slow answer for an earlier filter set can never
  // overwrite the notice for the current one. A failed fetch leaves the last notice standing.
  var noticeEl = document.getElementById("map-notice");
  var noticeSeq = 0;
  function refreshNotice() {
    if (!noticeEl) return;
    var seq = ++noticeSeq;
    fetch(noticeUrl(filters))
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { if (seq === noticeSeq) noticeEl.innerHTML = html; })
      .catch(function () { /* keep the last-known notice (docs/31 §6 error state) */ });
  }

  function currentPlacement() {
    return PLACEMENT_GRADES.filter(function (grade) { return placementCheckboxes[grade].checked; });
  }

  function applyFilters() {
    var picked = currentAssetTypes();
    filters = {
      technology: document.getElementById("mf-technology").value,
      kind: document.getElementById("mf-kind").value,
      jurisdiction: document.getElementById("mf-jurisdiction").value,
      include_withdrawn: document.getElementById("mf-include-withdrawn").checked,
      url_only: filters.url_only,
      layers: filters.layers,
      region: filters.region,
      plant_technology: plantTypeSelect.value,
      asset_types: picked.length ? picked : ASSET_TYPES_LIVE.slice(),
      placement: currentPlacement(),
      view: filters.view,
      focus: ""
    };
    writeFilters(filters);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(plantsToggle.checked);
    refetch();
    refreshNotice();
  }
  plantTypeSelect.addEventListener("change", applyFilters);
  assetTypeBoxes.forEach(function (box) {
    box.addEventListener("change", function () {
      applyFilters();
      sendUiEvent("map.layer_toggled", { layer: "assets", on: box.checked, asset_type: box.value });
    });
  });
  document.getElementById("mf-technology").addEventListener("change", applyFilters);
  document.getElementById("mf-kind").addEventListener("change", applyFilters);
  document.getElementById("mf-jurisdiction").addEventListener("change", applyFilters);
  document.getElementById("mf-include-withdrawn").addEventListener("change", applyFilters);
  plantsToggle.addEventListener("change", function () {
    var on = plantsToggle.checked;
    setLayerOn("plants", on);
    writeFilters(filters);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(on);
    else { plantsLegend.hidden = !on; }
    sendUiEvent("map.layer_toggled", { layer: "plants", on: on });
    if (on) refetchAssets();
    render();
  });
  retiredToggle.addEventListener("change", function () {
    var on = retiredToggle.checked;
    setLayerOn("retired", on);
    writeFilters(filters);
    if (map.getLayer("retired-points")) setRetiredLayerVisible(on);
    else { retiredLegend.hidden = !on; }
    sendUiEvent("map.layer_toggled", { layer: "retired", on: on });
    if (on) refetchRetired();
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
    document.getElementById("mf-kind").value = "";
    document.getElementById("mf-jurisdiction").value = "";
    document.getElementById("mf-include-withdrawn").checked = false;
    // "Clear all" also drops the filters that arrived in the link with no control on this page:
    // left in place they would go on narrowing the map with nothing on screen saying so.
    filters.url_only = {};
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
    el.setAttribute("aria-label", "Record detail");
    // Closed, the drawer is out of the page entirely (audit 2026-09-30 F5): `hidden` takes it out
    // of the tab order and the accessibility tree, and `aria-modal` is set only while it is open,
    // so a screen reader never meets a modal dialog that is not on screen.
    el.hidden = true;
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
    var hideTimer = null;
    function open() {
      window.clearTimeout(hideTimer);
      el.hidden = false;
      el.removeAttribute("inert");
      el.setAttribute("aria-modal", "true");
      void el.offsetWidth; // start the slide-in from the closed position, not from `hidden`
      el.classList.add("is-open");
      closeBtn.focus();
    }
    function close() {
      if (el.hidden) return;
      el.classList.remove("is-open");
      el.removeAttribute("aria-modal");
      // Inert at once (nothing in it can take focus while it slides out), hidden once the 150ms
      // close transition has run; instant under reduced motion.
      el.setAttribute("inert", "");
      hideTimer = window.setTimeout(function () { el.hidden = true; }, motionMs(160));
      if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
      else map.getCanvas().focus();
    }
    function row(label, valueHtml, numeric) {
      if (valueHtml == null || valueHtml === "") return "";
      return "<div class=\"drawer-fields__row\"><dt>" + esc(label) + "</dt><dd" + (numeric ? " class=\"tnum\"" : "") + ">" + valueHtml + "</dd></div>";
    }
    // D-13 / docs/31 §5.8: the drawer's source line names the licence class beside the source,
    // as the record page's Sources panel does: "EIA-860M · Open licence · retrieved 2026-09-27".
    function sourceHtml(source) {
      if (!source) return "";
      var licence = reuseClassName(source.reuse_class);
      return "<p class=\"drawer-source\"><span class=\"drawer-source__label\">Source</span>" +
        "<a href=\"" + safeUrl(source.source_url) + "\" rel=\"noopener nofollow\">" + esc(source.source_name) + "</a>" +
        (licence ? " &middot; <span class=\"drawer-source__licence\">" + esc(licence) + "</span>" : "") +
        (source.licence_name ? " &middot; " + esc(source.licence_name) : "") +
        " &middot; retrieved <span class=\"tnum\">" + esc(source.retrieved_at ? String(source.retrieved_at).slice(0, 10) : "unknown") + "</span>" +
        (source.attribution_text ? "<br><span class=\"drawer-source__credit\">" + esc(source.attribution_text) + "</span>" : "") + "</p>";
    }
    function render(p, source) {
      body.innerHTML =
        "<h2>" + esc(p.name) + "</h2>" + chipHtml(familyOf(p.lifecycle_state), p.lifecycle_state) +
        "<dl class=\"drawer-fields\">" +
        "<div class=\"drawer-fields__row\"><dt>Technology</dt><dd>" + esc(technologyName(p.technology) || "—") + "</dd></div>" +
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
        return row(technologyName(k), Number(techs[k]).toFixed(1) + " MW", true);
      }).join("");
      var source = propObj(p.source);
      var commissionedYear = p.commissioned_year || p.earliest_operating_year;
      var miles = attrOf(p, ["length_miles", "miles"]);
      var diameter = attrOf(p, ["diameter_in", "diameter_inches", "diameter", "diameter_mix"]);
      var states = statesOf(p);
      var lineClass = isLine ? (type === "transmission_line" ? voltageLabelOf(p) : lineClassOf(p)) : null;
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
        : (type === "power_plant" ? (techRows || row("Technology", p.technology ? esc(technologyName(p.technology)) : null)) : row("Technology", p.technology ? esc(technologyName(p.technology)) : null)) +
          row("Capacity", p.capacity_mw ? Number(p.capacity_mw).toFixed(1) + " MW" : null, true) +
          row("Length", miles != null && fmtNumber(miles, 0) ? fmtNumber(miles, 0) + " miles" : null, true) +
          row("Diameter", diameter != null ? esc(typeof diameter === "number" ? fmtNumber(diameter, 1) + " in" : String(diameter)) : null, true) +
          row("States", states ? esc(states) : null);
      body.innerHTML =
        "<h2>" + esc(p.name || "Unnamed asset") + "</h2>" +
        "<p class=\"reuse-badge\">" + (p.status === "retired" ? "Retired plant" : "Existing asset") + " &middot; " + subtitle + "</p>" +
        "<dl class=\"drawer-fields\">" +
        row("Operator", p.operator_name ? esc(p.operator_name) : null) +
        typeRows +
        row("Status", p.status ? esc(statusName(p.status)) : null) +
        row(p.status === "retired" ? "Retired" : "Retirement scheduled", p.retirement_year ? esc(yearOf(p.retirement_year)) : null, true) +
        (fuel ? "" : row("First operating year", commissionedYear ? esc(commissionedYear) : null, true)) +
        row("Location", location || null) +
        "</dl>" +
        sourceHtml(source) +
        (pageUrl ? "<a class=\"drawer-open-link\" href=\"" + pageUrl + "\">Open asset page &rarr;</a>" : "");
    }
    return { open: open, close: close, render: render, renderAsset: renderAsset, renderPlant: renderAsset };
  }
})();
