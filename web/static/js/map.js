/* Map page (docs/04 D-8...D-15). MapLibre GL JS pinned to 5.24.0 (see home_map.html).
 * Clustering is computed server-side by `GET /v1/proposals/geo` (docs/23 §3.1), proxied through
 * this app's own `/api/proposals/geo` so the browser never needs to know the API's host. This
 * script renders exactly what comes back on every pan/zoom/filter change; it does not cluster.
 * The basemap (three MAP_TILE_URL modes + the same-origin fallback outline) lives in basemap.js,
 * shared with the asset/company page mini-maps (asset_map.js).
 */

// docs/17 M-6 (lane m-layout): the words around the map, wired before the map so they work even
// when MapLibre cannot start (the map's own code stops at `new maplibregl.Map` then). Below 720px
// the lede is its first sentence and "About this map" opens the rest; the lifecycle notice's short
// form has a "Why?" that opens the notice. The open state lives on `#map-notice`, which map.js
// refills after each filter change, so the notice stays open across one (`refreshNotice`). Both
// are disclosure buttons; without scripts styles.css shows everything and neither button.
(function () {
  "use strict";
  var lede = document.getElementById("map-intro-lede");
  var about = document.getElementById("map-intro-about");
  if (lede && about) {
    about.addEventListener("click", function () {
      var open = about.getAttribute("aria-expanded") !== "true";
      about.setAttribute("aria-expanded", open ? "true" : "false");
      lede.classList.toggle("is-open", open);
    });
  }
  var notice = document.getElementById("map-notice");
  if (notice) {
    notice.addEventListener("click", function (e) {
      var why = e.target && e.target.closest ? e.target.closest(".map-notice__why") : null;
      if (!why) return;
      var open = !notice.classList.contains("is-why-open");
      notice.classList.toggle("is-why-open", open);
      why.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  // "Click a cluster..." (the lede and the key's rows) says "Tap" where the primary pointer is a
  // finger (a phone, a tablet), and follows a change of pointer (a tablet given a mouse).
  var howTos = Array.prototype.slice.call(document.querySelectorAll("[data-touch-words]"));
  var coarse = window.matchMedia ? window.matchMedia("(pointer: coarse)") : null;
  if (howTos.length && coarse) {
    var clickWords = howTos.map(function (el) { return el.textContent; });
    var syncWords = function () {
      howTos.forEach(function (el, i) {
        el.textContent = coarse.matches ? el.getAttribute("data-touch-words") : clickWords[i];
      });
    };
    syncWords();
    if (coarse.addEventListener) coarse.addEventListener("change", syncWords);
  }
})();

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
    gas: ["gas_cc", "gas_ct", "gas_steam", "gas_ice", "gas_other", "fuel_cell", "hydrogen", "lng_terminal", "gas_pipeline"],
    oil: ["oil"],
    coal: ["coal"],
    nuclear: ["nuclear"],
    hydro: ["hydro", "marine"],
    storage: ["storage", "pumped_storage"],
    biomass: ["biomass", "waste"],
    geothermal: ["geothermal"],
    other: ["other", "unknown", "load", "transmission", "grid_equipment", "chp", "pipeline", "ccs"]
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
  // docs/17 M-7: with no view in the link the map fits the contiguous US (the lower 48) to the
  // canvas with 16px to spare, rather than opening at a fixed centre and zoom. The fixed z3.2 showed
  // North Dakota to the Gulf on a 358px phone canvas, with California and the Northeast cut off.
  var CONUS_BOUNDS = [[-124.8, 24.4], [-66.9, 49.4]];
  var HOME_PADDING_PX = 16;
  // The landing's front strip (docs/31-brand-identity.md M-3) sits at the canvas's bottom left, on the
  // map's own ground: the national view is fitted so that no part of the lower 48 lies under it
  // (`homeCamera`). These are the places along the southern and western edges a fit can bring under
  // the strip first: the Pacific coast, the Mexican border, the Gulf coast and the Florida Keys.
  var CONUS_EDGE = [
    [-124.4, 40.4], [-122.4, 37.8], [-118.2, 34.0], [-117.1, 32.6], [-114.6, 32.7], [-110.9, 31.3],
    [-106.5, 31.8], [-103.3, 29.0], [-100.9, 29.4], [-99.5, 27.5], [-97.4, 25.9], [-97.4, 27.8],
    [-94.0, 29.6], [-89.4, 29.0], [-84.4, 29.9], [-81.8, 24.55], [-80.2, 25.8]
  ];
  // How far a mark reaches past its point, so a fit keeps whole marks clear of the strip.
  var MARK_REACH_PX = 10;
  // Per-place marks (owner, 2026-10-24: "every active project a bright dot"): above 500 placed
  // records in a view the API groups them on a grid keyed by `zoom` (services/api/geo.py
  // `_grid_cell`), so the map asks with a zoom six steps finer than its own. A cell is then about two
  // pixels wide at the national view: each mark is one place (or a few at one place), sized by area
  // with its count, never a numbered cluster. Measured on the national view of the dev store (6,009
  // active records), 2026-10-24: 1,657 features (1,334 places, 323 regions), 846 KB, 68.5 KB gzipped
  // at 1440x900; 1,396 features, 64.8 KB gzipped at 390x844 (docs/04 D-13: at most 2,000 and 300 KB
  // gzipped; production sends it gzipped, infra/compose/Caddyfile).
  var MARK_ZOOM_STEPS = 6;
  var MARK_ZOOM_MAX = 12;
  function markZoom(zoom) { return Math.min(MARK_ZOOM_MAX, zoom + MARK_ZOOM_STEPS); }
  // The camera that fit, once the map exists (`homeCamera`); the URL never carries it.
  var homeView = null;

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
    if (!homeView) return false;
    return Math.abs(view.center[0] - homeView.center[0]) < 0.001 && Math.abs(view.center[1] - homeView.center[1]) < 0.001 &&
      Math.abs(view.zoom - homeView.zoom) < 0.01;
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
  // The proposal field rows a kind cannot carry (`#proposal-fields`, web/viewmodels.py
  // PROPOSAL_FIELDS_NOT_APPLICABLE): the drawer leaves out what the record page leaves out, and
  // only while the record has no value for it.
  var PROPOSAL_FIELDS = (function () {
    var el = document.getElementById("proposal-fields");
    try { return (el && JSON.parse(el.textContent)) || {}; } catch (e) { return {}; }
  })();
  function fieldApplies(kind, field, value) {
    if (value !== null && value !== undefined && value !== "") return true;
    var rows = (PROPOSAL_FIELDS.not_applicable || {})[kind] || [];
    return rows.indexOf(field) === -1;
  }
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

  // ---- fmtMWNumber: begin (web/test_formatting.py runs this block in node) ----
  // A capacity as the server prints it (web/formatting.py `mw`, docs/31 §4; UX-8): thousands
  // separators, at most one decimal, no trailing ".0" -- "4,800", "1,150.5", never "4800.0". It
  // rounds as Python's round(x, 1) does: the exact binary value to the nearest tenth, and a true
  // tie (only x.25 and x.75 are exact ties) to the even tenth, so 2.25 prints "2.2" and 0.35
  // (stored as 0.34999...) "0.3". Intl's toLocaleString rounds the shortest decimal half up ("2.3",
  // "0.4"), which is how this file disagreed with the server before. Text that is not a number
  // prints as itself, as on the server.
  var MW_NUMBER = /^\s*[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?\s*$/i;
  function fmtMWNumber(value) {
    if (value == null) return "";
    if (typeof value === "boolean") return value ? "True" : "False";
    if (typeof value !== "number" && !MW_NUMBER.test(String(value))) return String(value);
    var n = Number(value);
    if (n !== n) return "nan";
    if (!isFinite(n)) return n > 0 ? "inf" : "-inf";
    var a = Math.abs(n);
    var text;
    if (Number.isInteger(a * 4) && !Number.isInteger(a * 2)) text = Math.floor(a) + (a % 1 === 0.25 ? ".2" : ".8");
    else if (a >= 1e21) text = BigInt(a).toString(); // toFixed answers in exponent form from 1e21
    else text = a.toFixed(1).replace(/\.0$/, "");
    var parts = text.split(".");
    text = parts[0].replace(/\B(?=(\d{3})+$)/g, ",") + (parts.length > 1 ? "." + parts[1] : "");
    return n < 0 && text !== "0" ? "-" + text : text;
  }
  // ---- fmtMWNumber: end ----
  function fmtMW(value) { return fmtMWNumber(value) + " MW"; }
  // An ISO date as the server prints it (web/formatting.py `display_date`, docs/31 §4): "13 Sep 2026".
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDate(value) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ""));
    if (!m || Number(m[2]) < 1 || Number(m[2]) > 12) return String(value || "");
    return Number(m[3]) + " " + MONTHS[Number(m[2]) - 1] + " " + m[1];
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
      var rated = attrOf(p, ["rated_mw", "capacity_mw"]);
      add("Rated capacity", fmtNumber(rated, 1) == null ? null : fmtMW(rated), true);
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
  // resolve_proposal_lifecycle_param); since UX-1 the Status control sets it. Anything else is not
  // forwarded.
  var FORM_CONTROLLED_FILTERS = ["technology", "kind", "jurisdiction", "placement"];
  // Owner, 2026-10-20 ("make all the data render immediately"): the existing-asset and the retired
  // layers are on when a link names no `layers=`. The lifecycle default stays active proposals:
  // withdrawn ones are most of any queue and would bury what is live.
  var DEFAULT_LAYERS = ["plants", "retired"];
  var PASSTHROUGH_FILTERS = (document.getElementById("map-filters").getAttribute("data-passthrough") || "")
    .split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  var URL_ONLY_FILTERS = PASSTHROUGH_FILTERS.filter(function (name) {
    return FORM_CONTROLLED_FILTERS.indexOf(name) === -1;
  });

  // UX-1 (audit 2026-10-07): the Status control. Each box's value is its group's lifecycle states
  // as a csv (`_macros.html` status_filter). The URL carries the shortest form of the choice, as
  // the server writes it (web/viewmodels.py lifecycle_query_items): nothing for the default
  // (active), `include_withdrawn=1` for active plus withdrawn, else one `lifecycle_state` csv.
  var statusFieldset = document.querySelector("#map-filters .filter-field--status");
  var statusBoxes = Array.prototype.slice.call(document.querySelectorAll('#map-filters input[name="lifecycle_state"]'));
  // Owner, 2026-10-11 (web/unlisted.py): records no register lists any more are hidden from a view
  // of active states; this box (`unlisted=include`) shows them. The URL, the list link and the
  // site's own geo and notice routes carry it; those routes never send it to the API, they send
  // `listed=true` instead while the view hides them. The feed does not carry it (a change feed keeps
  // every record); the alert link may, and the saved search leaves it out (an alert watches every
  // record either way, web/alerts.py).
  var unlistedBox = document.querySelector('#map-filters input[name="unlisted"]');
  function readUnlisted(params) {
    return (params.get("unlisted") || "").toLowerCase() === "include";
  }
  function csvList(value) {
    return String(value || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  }
  var DEFAULT_STATES = csvList(statusFieldset && statusFieldset.getAttribute("data-default-states"));
  var WITHDRAWN_STATES = csvList(statusFieldset && statusFieldset.getAttribute("data-withdrawn-states"));
  function sameSet(a, b) {
    return a.length === b.length && a.every(function (x) { return b.indexOf(x) !== -1; });
  }
  // `lifecycle_state=none` (web/viewmodels.py NO_LIFECYCLE_STATE): every box unticked. It names no
  // state, so no proposal is drawn, and a reload or a shared link keeps the choice.
  var NO_STATUS = "none";
  function readStatuses(params) {
    var states = [];
    params.getAll("lifecycle_state").forEach(function (value) {
      csvList(value).forEach(function (s) { if (states.indexOf(s) === -1) states.push(s); });
    });
    if (states.length === 1 && states[0] === NO_STATUS) return [];
    states = states.filter(function (s) { return s !== NO_STATUS; });
    if (states.length) return states;
    return params.get("include_withdrawn") === "1" ? DEFAULT_STATES.concat(WITHDRAWN_STATES) : DEFAULT_STATES.slice();
  }
  // No box checked means no proposals. It used to mean the default, so unticking the last of the
  // three default boxes ticked all three again (owner report 2026-10-22: "anytime I do all three
  // each repopulates as clicked"). "Clear all" is the way back to the default.
  function currentStatuses() {
    var states = [];
    statusBoxes.forEach(function (box) {
      if (box.checked) csvList(box.value).forEach(function (s) { if (states.indexOf(s) === -1) states.push(s); });
    });
    return states;
  }
  function noStatusChosen() { return !(filters.statuses && filters.statuses.length); }
  function appendStatuses(params, states) {
    if (!states) return;
    if (!states.length) {
      params.set("lifecycle_state", NO_STATUS);
      return;
    }
    if (sameSet(states, DEFAULT_STATES)) return;
    if (sameSet(states, DEFAULT_STATES.concat(WITHDRAWN_STATES))) {
      params.set("include_withdrawn", "1");
      return;
    }
    params.set("lifecycle_state", states.join(","));
  }
  // The boxes and the disclosure's "Status: …" summary follow a choice made elsewhere (a link, Back,
  // "Clear all", the notice's "N built" link).
  function syncStatusControl(states) {
    statusBoxes.forEach(function (box) {
      box.checked = csvList(box.value).every(function (s) { return states.indexOf(s) !== -1; });
    });
    // The summary in the server's words (web/viewmodels.py proposal_status_choices): "active" while
    // the default groups are all on, then whatever else is; otherwise the groups' short names.
    var summary = document.querySelector("#mf-status [data-status-summary]");
    if (!summary) return;
    var defaultsOn = statusBoxes.filter(function (b) { return b.hasAttribute("data-default"); })
      .every(function (b) { return b.checked; });
    var words = defaultsOn ? ["active"] : [];
    statusBoxes.forEach(function (b) {
      if (b.checked && !(defaultsOn && b.hasAttribute("data-default"))) words.push(b.getAttribute("data-status-short") || b.value);
    });
    // No box ticked: nothing is shown, as the server's summary says (`proposal_status_choices`).
    if (!words.length) words.push("none");
    if (unlistedBox && unlistedBox.checked) words.push("no longer listed");
    summary.textContent = words.join(", ");
  }

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
    appendStatuses(params, filters.statuses);
    if (filters.unlisted) params.set("unlisted", "include");
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
      statuses: readStatuses(params),
      unlisted: readUnlisted(params),
      url_only: readUrlOnlyFilters(params),
      layers: params.has("layers") ? layersParam.split(",").filter(Boolean) : DEFAULT_LAYERS.slice(),
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
    // Written only when it differs from the default, so a plain link keeps every layer on and a
    // view with a layer turned off round-trips (`layers=` alone: none of them).
    if (!sameSet(filters.layers || [], DEFAULT_LAYERS)) params.set("layers", (filters.layers || []).join(","));
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
    // "Open these in the list" (it replaced "View as: List", designer D-8; owner 2026-10-22) carries
    // the filters and the viewport, so the list's own "View as: Map" link can bring the reader back
    // to this place. The list cannot narrow to the map's bounds, so it lists every proposal the
    // filters match. The map's default placement is a drawing choice, not a filter: the list shows
    // unplaced proposals too, which is where the unplaced note sends readers.
    var listHrefNow = listHref(qs);
    Array.prototype.forEach.call(document.querySelectorAll("#map-list-link, [data-list-link]"), function (a) {
      a.href = listHrefNow;
    });
    // "Save this search as an alert" carries the current filters (web/alerts.py drops the map's
    // own default placement and the layer/asset keys, which are not proposal filters). The page it
    // opens also offers the same search as an RSS feed (owner, 2026-10-22: the map's "RSS feed"
    // link moved there).
    var alertLink = document.getElementById("mf-save-alert");
    if (alertLink) alertLink.href = "/alerts/new?entity=proposal&origin=map" + (filterQs ? "&" + filterQs : "");
    // With no status ticked there is nothing to list or to watch, so List and the save link wait.
    var viewActions = document.getElementById("map-status-actions");
    if (viewActions) viewActions.hidden = noStatusChosen();
    if (alertLink) alertLink.hidden = noStatusChosen();
    // The collapsed "Filters" button names how many are set (site.js); filters that arrived in
    // the link with no control here count too.
    var form = document.getElementById("map-filters");
    if (form) {
      form.setAttribute("data-extra-active", String(Object.keys(filters.url_only || {}).length));
      if (typeof Event === "function") form.dispatchEvent(new Event("filters:changed"));
    }
    // "Sign in" carries the page the reader is on, the view included, as followed (site.js
    // `[data-signin]`, Navigation v2): nothing to rewrite here any more.
  }

  // `/proposals` with the map's query (`qs`), less what only the map reads.
  function listHref(qs) {
    var listParams = new URLSearchParams(qs);
    if (listParams.get("placement") === DEFAULT_PLACEMENT.join(",")) listParams.delete("placement");
    ["layers", "region", "plant_technology", "asset_type"].forEach(function (k) { listParams.delete(k); });
    var listQs = listParams.toString();
    return "/proposals" + (listQs ? "?" + listQs : "");
  }

  function geoUrl(filters, bbox, zoom) {
    var params = new URLSearchParams();
    params.set("bbox", bbox.join(","));
    params.set("zoom", String(markZoom(zoom)));
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
  if (unlistedBox) unlistedBox.checked = filters.unlisted;
  syncStatusControl(filters.statuses);
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
  var assetsMore = document.getElementById("mf-assets-more");
  function syncAssetControls() {
    var on = plantsToggle.checked;
    assetsMore.hidden = !on;
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
  // docs/31-brand-identity.md M-1 and M-2: the projects are the map's only saturated layer. A project in
  // process is a filled disc in --dot (the one data hue), a contracted one a filled navy square, an
  // announced one a hollow ring; existing infrastructure (plants, pipelines, lines, retired plants) draws
  // faint beneath them, at 24% opacity on light and 32% on dark, so it is there on arrival and recedes.
  // Faint marks, not faint words: its labels (cluster counts, names from zoom 7) draw at full strength,
  // where every plant and asset colour clears 4.5:1 on --map-land (WCAG 2.2 SC 1.4.3 outranks M-2;
  // web/test_brand.py blends each colour at this opacity over the land and checks it).
  var dotColor = cssVar("--dot") || colors.progress;
  var CTX_OPACITY = mapColors.flavor === "dark" ? 0.32 : 0.24;
  var CTX_TEXT_OPACITY = 1;
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

  // A touch is a finger: a tap's slop (`onMapClick`) and "Tap" for "Click" in the words.
  var coarsePointer = window.matchMedia ? window.matchMedia("(pointer: coarse)") : null;
  function isCoarsePointer() { return !!(coarsePointer && coarsePointer.matches); }

  // M-7: the padding a fitted view keeps: 16px all round, and the bands of the canvas the landing's own
  // furniture covers (docs/30 §11.6, §11.8), so a fit never puts the lower 48, a region or a place
  // under them: the floating bar along the top; from 720px the zoom column down the left and the panel
  // over the top right (open, its width on the right, so it never covers the East Coast; collapsed,
  // its height at the top, so it never covers Maine); below 720px the sheet along the bottom. A band
  // counts only while at least half the canvas is left beside it. The drawer, which opens in the
  // panel's place from 720px, counts as the open panel. `fitPadding` is the same for a jump.
  function homePadding() {
    var pad = { top: HOME_PADDING_PX, right: HOME_PADDING_PX, bottom: HOME_PADDING_PX, left: HOME_PADDING_PX };
    var m = mapEl.getBoundingClientRect();
    if (!(m.width > 0 && m.height > 0)) return pad;
    function box(el) {
      if (!el || el.hidden) return null;
      var k = el.getBoundingClientRect();
      var overCanvas = k.width > 0 && k.height > 0 && k.left < m.right && k.right > m.left && k.top < m.bottom && k.bottom > m.top;
      return overCanvas ? k : null;
    }
    function band(side, px, room) {
      if (px > pad[side] && room - px - HOME_PADDING_PX >= room / 2) pad[side] = px;
    }
    var bar = box(document.querySelector(".map-float"));
    if (bar) band("top", Math.round(bar.bottom - m.top) + HOME_PADDING_PX, m.height);
    var phone = isPhone();
    var rail = phone ? null : box(document.querySelector(".map-overlay__rail"));
    if (rail) band("left", Math.round(rail.right - m.left) + HOME_PADDING_PX, m.width);
    var drawerEl = document.getElementById("map-drawer");
    var cover = !phone && drawerEl && !drawerEl.hidden ? drawerEl : document.getElementById("map-panel");
    var k = box(cover);
    if (!k) return pad;
    if (phone) band("bottom", Math.round(m.bottom - k.top) + HOME_PADDING_PX, m.height);
    else if (cover.classList.contains("is-collapsed")) band("top", Math.round(k.bottom - m.top) + HOME_PADDING_PX, m.height);
    else band("right", Math.round(m.right - k.left) + HOME_PADDING_PX, m.width);
    return pad;
  }
  function fitPadding(extra) {
    var pad = homePadding();
    var more = extra || 0;
    return { top: pad.top + more, right: pad.right + more, bottom: pad.bottom + more, left: pad.left + more };
  }

  // With no view in the link the camera here is a placeholder: the fit to the lower 48 replaces it
  // (`firstHome`, below) before the first frame, once the panel is open or closed for this width.
  // docs/30 §11.8: one finger pans the landing's map, because there is no page below it to scroll
  // (the record pages' mini-maps keep the cooperative two-finger gesture, asset_map.js).
  var map = new maplibregl.Map({
    container: "map",
    style: Basemap.fallbackStyle(mapColors),
    center: filters.view ? filters.view.center : [-95.85, 37.9],
    zoom: filters.view ? filters.view.zoom : 3,
    attributionControl: false,
    cooperativeGestures: false
  });
  map.dragRotate.disable();
  map.touchZoomRotate.disableRotation();
  window.__map = map; // exposed for the Playwright smoke test only
  window.__mapIdle = false;
  map.once("idle", function () { window.__mapIdle = true; });

  // The map's credits (D-13, D-27) are the page's own disclosure at the canvas's bottom right
  // (`#map-credits`, home_map.html), not MapLibre's attribution control: "© OpenStreetMap" and its "i"
  // from first paint at every width, and every credit, the footer's links and the endorsement inside.
  // A `<details>` opens and closes without this script; here Escape closes it and returns focus to its
  // "i", and a click or tap anywhere else closes it, as "Jump to" does.
  var creditsBox = document.getElementById("map-credits");
  if (creditsBox) {
    var creditsToggle = creditsBox.querySelector("summary");
    creditsBox.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && creditsBox.open) {
        e.preventDefault();
        creditsBox.open = false;
        if (creditsToggle) creditsToggle.focus();
      }
    });
    document.addEventListener("click", function (e) {
      if (creditsBox.open && !creditsBox.contains(e.target)) creditsBox.open = false;
    });
  }

  // Where `cam` would draw `ll` on this canvas, without moving the map: Web Mercator at MapLibre's
  // 512px tiles, the camera's centre at the canvas's middle (what `cameraForBounds` returns).
  function projectWith(cam, ll, width, height) {
    var scale = 512 * Math.pow(2, cam.zoom);
    function x(lng) { return (lng + 180) / 360 * scale; }
    function y(lat) {
      var s = Math.sin(lat * Math.PI / 180);
      return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale;
    }
    var c = maplibregl.LngLat.convert(cam.center);
    return [x(ll[0]) - x(c.lng) + width / 2, y(ll[1]) - y(c.lat) + height / 2];
  }
  // The front strip's box on the canvas, while it lies over it.
  var stripEl = document.getElementById("map-strip");
  function stripBox() {
    if (!stripEl) return null;
    var m = mapEl.getBoundingClientRect();
    // Measured as it stands at the national view, which is what the fit is for: away from it a phone
    // takes the strip off the screen (`.is-away`), and Reset fits the view it is about to show it in.
    var away = stripEl.classList.contains("is-away");
    if (away) stripEl.classList.remove("is-away");
    var s = stripEl.getBoundingClientRect();
    if (away) stripEl.classList.add("is-away");
    if (!(s.width > 0 && s.height > 0) || s.left >= m.right || s.right <= m.left || s.top >= m.bottom || s.bottom <= m.top) return null;
    return { left: s.left - m.left, right: s.right - m.left, top: s.top - m.top, bottom: s.bottom - m.top };
  }
  function underStrip(cam, strip, width, height) {
    return CONUS_EDGE.some(function (ll) {
      var p = projectWith(cam, ll, width, height);
      return p[0] + MARK_REACH_PX > strip.left && p[0] - MARK_REACH_PX < strip.right &&
        p[1] + MARK_REACH_PX > strip.top && p[1] - MARK_REACH_PX < strip.bottom;
    });
  }

  // The national view for this canvas, as a camera: written to `homeView`, so `isDefaultView` knows
  // it and the URL stays clean while the map shows it. The lower 48 is fitted inside the padding and,
  // where the front strip would cover a part of it (southern California and the Texas border at
  // 1440px; the whole width under the map on a phone), lifted above the strip, 12px at a time, while
  // two thirds of the canvas's height is left for it. At the fitted view the strip's text stands on
  // the map's ground alone, as on the share card.
  function homeCamera() {
    var pad = homePadding();
    var cam = map.cameraForBounds(CONUS_BOUNDS, { padding: pad });
    var strip = stripBox();
    var w = mapEl.clientWidth;
    var h = mapEl.clientHeight;
    for (var i = 0; cam && strip && i < 60 && underStrip(cam, strip, w, h); i++) {
      if (h - pad.top - (pad.bottom + 12) < h / 3) break;
      pad.bottom += 12;
      cam = map.cameraForBounds(CONUS_BOUNDS, { padding: pad });
    }
    if (!cam) return null;
    var c = maplibregl.LngLat.convert(cam.center);
    homeView = { center: [c.lng, c.lat], zoom: cam.zoom };
    return homeView;
  }
  function goHome(duration) {
    var cam = homeCamera();
    // Already there (Reset on the national view, a key toggle that changes no padding): no move, so
    // no refetch either.
    if (cam && !isAtHome()) map.flyTo({ center: cam.center, zoom: cam.zoom, duration: duration });
  }
  // The map shows the national view it last fitted: nobody has moved it since the load or Reset.
  function isAtHome() {
    if (!homeView || map.isMoving()) return false;
    var c = map.getCenter();
    return isDefaultView({ center: [c.lng, c.lat], zoom: map.getZoom() });
  }

  document.getElementById("zoom-in").addEventListener("click", function () { map.zoomIn({ duration: motionMs(200) }); });
  document.getElementById("zoom-out").addEventListener("click", function () { map.zoomOut({ duration: motionMs(200) }); });
  // "Reset" fits the lower 48 again, for the canvas as it is now (a turned phone, a resized window).
  document.getElementById("zoom-reset").addEventListener("click", function () { goHome(motionMs(600)); });

  // Away from the national view (docs/30 §11.6): "Reset" is drawn, and the front strip gains the
  // plain surface (`.is-away`, styles.css), since a project may now be drawn under its words; on a
  // phone it gives the canvas back until the map is home again. Both are absolutely placed, so neither
  // moves anything else. Asked once the map has settled, never while it moves.
  var resetGroup = document.querySelector(".map-controls--reset");
  function syncAway() {
    if (map.isMoving()) return;
    var away = !!homeView && !isAtHome();
    if (stripEl) stripEl.classList.toggle("is-away", away);
    if (resetGroup) resetGroup.hidden = !away;
  }
  if (resetGroup) resetGroup.hidden = true;
  map.on("moveend", syncAway);
  map.once("load", syncAway);

  // ---- docs/17 M-1: "Jump to", at every width on the landing (docs/30 §11.6) ----
  // The region jumps and "Whole map" in one list beside the zoom column, opened by "Jump to", a
  // disclosure button. A choice, Escape, a tap elsewhere or focus moving on closes it; a choice or
  // Escape returns focus to "Jump to".
  var jumpToggle = document.getElementById("map-jump-toggle");
  var jumpList = document.getElementById("map-regions");
  function jumpIsOpen() { return !!(jumpList && jumpList.classList.contains("is-open")); }
  function setJumpOpen(open, refocus) {
    if (!jumpToggle || !jumpList) return;
    jumpToggle.setAttribute("aria-expanded", open ? "true" : "false");
    jumpList.classList.toggle("is-open", open);
    if (!open && refocus) jumpToggle.focus();
  }
  if (jumpToggle && jumpList) {
    var jumpGroup = jumpToggle.parentNode;
    jumpToggle.hidden = false;
    jumpToggle.addEventListener("click", function () { setJumpOpen(!jumpIsOpen()); });
    jumpGroup.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && jumpIsOpen()) { e.preventDefault(); setJumpOpen(false, true); }
    });
    jumpGroup.addEventListener("focusout", function (e) {
      if (jumpIsOpen() && e.relatedTarget && !jumpGroup.contains(e.relatedTarget)) setJumpOpen(false);
    });
    document.addEventListener("click", function (e) {
      if (jumpIsOpen() && !jumpGroup.contains(e.target)) setJumpOpen(false);
    });
    var jumpHome = document.getElementById("map-jump-home");
    if (jumpHome) {
      jumpHome.addEventListener("click", function () {
        goHome(motionMs(600));
        setJumpOpen(false, true);
      });
    }
  }

  // ---- task item 3: regional quick views ----
  document.querySelectorAll(".region-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var bbox = btn.getAttribute("data-bbox").split(",").map(Number);
      var code = btn.getAttribute("data-region");
      map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { duration: motionMs(600), padding: fitPadding(8) });
      filters.region = code;
      writeFilters(filters);
      sendUiEvent("map.region_jumped", { region: code });
      if (jumpIsOpen()) setJumpOpen(false, true);
    });
  });
  // Restore a `region=` view from a shared/reloaded URL on load, instantly (this is page setup,
  // not a user-initiated jump, so it does not re-emit `map.region_jumped`). A `center`/`zoom` in
  // the link is more precise than the region it started from, so it wins. Fitted once the panel is
  // open or closed for this width (below, beside the first home view), clear of it as a jump is.
  var viewRestored = !!filters.view;
  var restoreBbox = null;
  if (filters.region && !filters.view) {
    var restoreBtn = document.querySelector('.region-btn[data-region="' + filters.region + '"]');
    if (restoreBtn) {
      viewRestored = true;
      restoreBbox = restoreBtn.getAttribute("data-bbox").split(",").map(Number);
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
  var panelCountEl = document.getElementById("map-panel-count");
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
  // Audit 2026-10-07 UX-5: the fill used to scale 0.15..0.7 against the busiest region in view, so
  // a lone state-placed proposal painted a whole state slate grey over the lines and plants a
  // planner had turned on. Now only a county is shaded, stepped by its own count (1, 2-9, 10-49,
  // 50+), never past 0.4; a state or country is a dashed outline over a near-transparent wash
  // (`region_grade` "broad"), since one record placed "somewhere in Indiana" says nothing about
  // where in it. The legend row (`#region-legend`) names both.
  var REGION_BROAD_OPACITY = 0.04;
  // `fam_<family>`: how many of a cluster's or a region's projects are in each status family, flat on
  // the feature so a key row's hover can light the marks holding that family (`highlightMatch`).
  function addFamilyCounts(props, counts) {
    var obj = propObj(counts) || {};
    Object.keys(obj).forEach(function (state) {
      var key = "fam_" + familyOf(state);
      props[key] = (props[key] || 0) + (Number(obj[state]) || 0);
    });
  }
  // docs/31-brand-identity.md §5.3 (owner, 2026-10-24: "every active project a bright dot"): a place
  // that holds several projects, or a county's projects below zoom 6, is drawn as one mark per status
  // family at that point, each sized by area with its own count (`mark_count`), so a county of 30 in
  // process and 4 announced reads as a large evergreen disc inside a small ring, never as a numbered
  // ring in the colour most of them share. `extra` carries what a click on the mark acts on: the
  // place's zoom, or the region its summary opens. The feature's own `count` stays the place's whole
  // count, which the hover card and the region's summary say.
  var MARK_FAMILIES = ["neutral", "progress", "committed", "success", "danger"];
  function familyMarks(f, extra) {
    var p = f.properties;
    var byFamily = {};
    var counts = propObj(p.lifecycle_state_counts) || {};
    Object.keys(counts).forEach(function (state) {
      var n = Number(counts[state]) || 0;
      if (n > 0) byFamily[familyOf(state)] = (byFamily[familyOf(state)] || 0) + n;
    });
    if (!Object.keys(byFamily).length) byFamily[familyOf(p.dominant_lifecycle_state || "unknown")] = Number(p.count) || 1;
    return MARK_FAMILIES.filter(function (family) { return byFamily[family]; }).map(function (family) {
      var props = {
        feature_kind: "mark", family: family, mark_count: byFamily[family], count: Number(p.count) || byFamily[family],
        lifecycle_state_counts: p.lifecycle_state_counts, capacity_mw_sum: p.capacity_mw_sum
      };
      Object.keys(extra || {}).forEach(function (k) { props[k] = extra[k]; });
      return { type: "Feature", geometry: f.geometry, properties: props };
    });
  }
  // A county's marks open the county's summary, as a tap on its shading does from zoom 6.
  function regionMarkProps(f) {
    var p = f.properties;
    return { region: true, region_level: p.region_level, region_id: p.region_id, name: regionName(p) };
  }
  // docs/31-brand-identity.md §5.3 and §10: below zoom 6 a county is a few pixels across, and its
  // projects are drawn as the status marks at its middle (`familyMarks`), sized by area with the count
  // and without numerals, as every other place's are. From zoom 6, where the county itself can be
  // seen, it is shaded and numbered as before, and the marks give way to it. A state or a country
  // placement keeps its dashed outline at every zoom: no one point stands for "somewhere in Indiana".
  var COUNTY_SHADED_FROM_ZOOM = 6;
  function countiesShaded() { return map.getZoom() >= COUNTY_SHADED_FROM_ZOOM; }
  function regionOpacity(level, count) {
    if (level !== "county") return REGION_BROAD_OPACITY;
    if (!countiesShaded()) return 0;
    var n = Number(count) || 0;
    return n >= 50 ? 0.4 : n >= 10 ? 0.3 : n >= 2 ? 0.2 : 0.12;
  }
  function updateRegionsLayer(regionFeatures) {
    if (!map.getSource("regions")) return;
    var out = [];
    regionFeatures.forEach(function (f) {
      var key = f.properties.region_level + ":" + f.properties.region_id;
      var county = f.properties.region_level === "county";
      var props = {
        region_level: f.properties.region_level, region_id: f.properties.region_id, name: regionName(f.properties),
        count: f.properties.count, region_opacity: regionOpacity(f.properties.region_level, f.properties.count),
        outline_opacity: county && !countiesShaded() ? 0 : 0.6,
        region_grade: county ? "area" : "broad",
        // For the summary a tap opens (docs/17 M-2/M-5): the region's statuses and stated capacity.
        lifecycle_state_counts: f.properties.lifecycle_state_counts, capacity_mw_sum: f.properties.capacity_mw_sum
      };
      addFamilyCounts(props, f.properties.lifecycle_state_counts);
      out.push({ type: "Feature", geometry: f.geometry, properties: props });
      var polygon = regionPolygonCache[key];
      if (polygon) out.push({ type: "Feature", geometry: polygon, properties: props });
    });
    map.getSource("regions").setData({ type: "FeatureCollection", features: out });
    var legendRow = document.getElementById("region-legend");
    if (legendRow) legendRow.hidden = !regionFeatures.length;
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
    var listLink = document.getElementById("map-list-link");
    var listTarget = (listLink && listLink.getAttribute("href")) || "/proposals";
    if (!hasLoadedOnce) {
      countEl.textContent = "Proposals could not be loaded.";
      countEl.removeAttribute("aria-hidden");
      if (panelCountEl) panelCountEl.textContent = "Projects could not be loaded";
    }
    if (!mapErrorEl) return;
    mapErrorEl.innerHTML =
      "<p><strong>Couldn\u2019t load proposals for this view.</strong> " + esc(title) +
      (problem.request_id ? " (request <span class=\"mono\">" + esc(problem.request_id) + "</span>)" : "") + ". " +
      (hasLoadedOnce ? "The map still shows the last view that loaded. " : "") +
      "<button type=\"button\" class=\"map-error__retry\">Retry</button> " +
      "<a href=\"" + esc(safeUrl(listTarget) || "/proposals") + "\">Try the list view</a></p>";
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
    drawerAnchor = hit.geometry.coordinates;
    try { openDrawer(hit.properties); } finally { drawerAnchor = null; }
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
        var countyMarks = !countiesShaded();
        fc.features.forEach(function (f) {
          if (f.properties.feature_kind === "region") {
            regionFeatures.push(f);
            // Below zoom 6 a county's projects are marks at its middle (`COUNTY_SHADED_FROM_ZOOM`).
            if (countyMarks && f.properties.region_level === "county") {
              Array.prototype.push.apply(pointFeatures, familyMarks(f, regionMarkProps(f)));
            }
            return;
          }
          if (f.properties.feature_kind === "cluster") {
            f.properties.family = familyOf(f.properties.dominant_lifecycle_state);
            addFamilyCounts(f.properties, f.properties.lifecycle_state_counts);
            // A place holding several projects is one mark per status family, never a numbered ring.
            Array.prototype.push.apply(pointFeatures, familyMarks(f, { expands_to_zoom: f.properties.expands_to_zoom }));
            return;
          }
          f.properties.family = familyOf(f.properties.lifecycle_state);
          f.properties.tech_label = techLabel(f.properties.technology);
          pointFeatures.push(f);
        });
        latestCollection = fc;
        latestMeta = envelope.meta || {};
        updateLifecycleLegend((fc.totals || {}).lifecycle_state_counts || {});
        // Busiest first, so the in-view list leads with the county holding 37, not a state holding 1.
        latestRegionFeatures = regionFeatures.slice().sort(function (a, b) {
          return (Number(b.properties.count) || 0) - (Number(a.properties.count) || 0);
        });
        if (map.getSource("proposals")) {
          map.getSource("proposals").setData({ type: "FeatureCollection", features: pointFeatures });
        }
        ensureRegionPolygons(regionFeatures, function () { updateRegionsLayer(regionFeatures); });
        scheduleRender();
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

  // docs/17 §1: under a fast zoom the asset requests for views already left kept running, and the
  // one API worker answered `/api/assets/geo` with 503. As the proposals fetch does, a new view
  // aborts the request it supersedes (an aborted fetch is silent), and an answer that is not 2xx
  // keeps the last-known layer instead of drawing an empty one from the problem body.
  function supersede(previous) {
    if (previous) previous.abort();
    return typeof AbortController === "function" ? new AbortController() : null;
  }
  function geoJson(r) {
    if (r.ok) return r.json();
    var err = new Error("asset geo request failed");
    err.status = r.status;
    throw err;
  }

  var retiredFetchSeq = 0;
  var retiredAbort = null;
  function refetchRetired() {
    var seq = ++retiredFetchSeq;
    retiredAbort = supersede(retiredAbort);
    fetch(retiredGeoUrl(currentBbox(), currentZoom()), retiredAbort ? { signal: retiredAbort.signal } : undefined)
      .then(geoJson)
      .then(function (envelope) {
        if (seq !== retiredFetchSeq) return;
        var fc = envelope.data || {};
        var features = (fc.features || []).filter(function (f) { return f.properties.feature_kind !== "asset_line"; });
        features.forEach(decorateRetiredFeature);
        latestRetired = { type: "FeatureCollection", features: features };
        latestRetiredTotal = assetsInView(features);
        latestRetiredClustered = !!(fc.totals || {}).clustered;
        if (map.getSource("retired-plants")) map.getSource("retired-plants").setData(latestRetired);
        scheduleRender();
      })
      .catch(function () { /* aborted for a newer view, or failed: keep the last-known layer (docs/31 §6) */ });
  }

  // Annotates one asset feature in place with what the layers and the in-view list read:
  // `color` (hue by type / plant family), `icon` (SDF shape name), `plant_label`, `line_class`.
  function decorateAssetFeature(f) {
    var p = f.properties;
    var kind = p.feature_kind;
    if (kind === "asset_cluster" || kind === "plant_cluster") {
      p.color = clusterColor(p);
      // A key row's hover highlights the clusters whose most common plant is of its family.
      if (!ASSET_TYPES[p.dominant_asset_type] || p.dominant_asset_type === "power_plant") p.plant_family = plantFamilyOf(p.dominant_technology);
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
  var assetsAbort = null;
  function refetchAssets() {
    var seq = ++assetsFetchSeq;
    assetsAbort = supersede(assetsAbort);
    var init = assetsAbort ? { signal: assetsAbort.signal } : undefined;
    var urls = assetsGeoUrls(filters, currentBbox(), currentZoom());
    Promise.all(urls.map(function (u) {
      return fetch(u, init).then(geoJson).then(function (envelope) { return envelope.data || {}; });
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
        // `count`, every other feature one (and while the open panel covers part of the canvas,
        // only what lies left of it, `inViewNow`). Unplaced rows (state-grade ethanol capacity,
        // county-grade AgSTAR digesters) are in neither, which is correct -- they are not in the view.
        latestPlantsTotal = assetsInView(features);
        latestAssetsClustered = clustered;
        if (map.getSource("plants")) map.getSource("plants").setData({ type: "FeatureCollection", features: features });
        scheduleRender();
      })
      .catch(function () {
        // Aborted for a newer view (silent), or failed: keep whatever was last drawn, as the proposals fetch does.
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
      parts.push(fmtMW(p.capacity_mw));
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
        g = { properties: p, geometry: f.geometry, unit_count: 1, capacity_mw: isFinite(mw) && p.capacity_mw != null ? mw : null };
        groups[key] = g;
        out.push(g);
        return;
      }
      g.unit_count += 1;
      if (isFinite(mw) && p.capacity_mw != null) g.capacity_mw = (g.capacity_mw || 0) + mw;
    });
    return out;
  }

  // ---- the panel: what is in view, and the key (owner, 2026-10-22; docs/30 §11.6-§11.8) ----
  // One panel over the map's top right from 720px, translucent (styles.css `.map-panel`), its box set
  // by the viewport alone (S-1), holding the in-view overview or list and the key as two tabs under a
  // head that says what the view holds. One click on the head collapses it to the count and a row of
  // marks and counts, and back; it is open by default from 1080px and collapsed from 720px to 1079px,
  // and the reader's choice is remembered in this browser. Below 720px it is a bottom sheet over the
  // map (§11.8): a peek (the count and the marks' row), half the map, or the map less 56px, so the
  // search stays in sight; a tap on the head moves between the peek and half, and a drag of the head
  // rests at whichever of the three is nearest. The rail's "Key" opens its Key tab. Without this
  // script the panel shows its key, open, and the head says nothing has loaded.
  var panel = document.getElementById("map-panel");
  var panelToggle = document.getElementById("map-panel-toggle");
  var panelKeyBtn = document.getElementById("map-panel-keybtn");
  var panelBody = document.getElementById("map-panel-body");
  var panelScroll = document.getElementById("map-panel-scroll");
  var tabInView = document.getElementById("map-tab-inview");
  var tabKey = document.getElementById("map-tab-key");
  var tabpanelInView = document.getElementById("map-tabpanel-inview");
  var tabpanelKey = document.getElementById("map-tabpanel-key");
  var mapKey = document.getElementById("map-key");
  var mapKeyToggle = document.getElementById("map-key-toggle");
  var narrowKey = window.matchMedia ? window.matchMedia("(max-width: 719px)") : null;
  var widePanel = window.matchMedia ? window.matchMedia("(min-width: 1080px)") : null;
  function isPhone() { return !!(narrowKey && narrowKey.matches); }
  // Navigation v2's top bar is sticky (styles.css `.top-bar`, docs/30 §11.2), so what the map brings
  // into view goes under it, not behind it: where its bottom edge is now, or (`stuck`) where it will be
  // once the page has scrolled past anything above it, such as the beta bar.
  var topBar = document.querySelector(".top-bar");
  function topBarBottom(stuck) {
    if (!topBar) return 0;
    if (stuck) return (parseFloat(window.getComputedStyle(topBar).top) || 0) + topBar.offsetHeight;
    return Math.max(0, topBar.getBoundingClientRect().bottom);
  }
  // The reader's own choice, from 720px only: a phone's bar always starts closed (docs/17 M-1).
  var PANEL_STORE = "inframation.mapPanel";
  function storedPanelChoice() {
    try {
      var value = window.localStorage.getItem(PANEL_STORE);
      return value === "open" || value === "closed" ? value : null;
    } catch (e) {
      return null; // storage blocked (a private window, a sandbox): the width decides
    }
  }
  function storePanelChoice(open) {
    try { window.localStorage.setItem(PANEL_STORE, open ? "open" : "closed"); } catch (e) { /* not kept */ }
  }
  function panelDefaultOpen() {
    if (isPhone()) return false;
    var stored = storedPanelChoice();
    if (stored) return stored === "open";
    return !!(widePanel && widePanel.matches);
  }
  function panelIsOpen() { return !!panel && !panel.classList.contains("is-collapsed"); }
  var panelTab = "inview";
  // The scroll of each tab, so a tab switch or a drawer opening in the panel's place loses neither.
  var tabScroll = { inview: 0, key: 0 };
  function syncKeyToggle() {
    if (mapKeyToggle) mapKeyToggle.setAttribute("aria-expanded", panelIsOpen() && panelTab === "key" ? "true" : "false");
  }
  function selectTab(name, focus) {
    if (!tabInView || !tabKey) return;
    // A collapsed body has no box, so its scroll reads 0: the place kept on collapsing stands.
    if (panelScroll && panelIsOpen()) tabScroll[panelTab] = panelScroll.scrollTop;
    panelTab = name === "key" ? "key" : "inview";
    var key = panelTab === "key";
    tabInView.setAttribute("aria-selected", key ? "false" : "true");
    tabKey.setAttribute("aria-selected", key ? "true" : "false");
    tabInView.tabIndex = key ? -1 : 0;
    tabKey.tabIndex = key ? 0 : -1;
    tabpanelInView.hidden = key;
    tabpanelKey.hidden = !key;
    if (panelScroll) panelScroll.scrollTop = tabScroll[panelTab] || 0;
    if (focus) (key ? tabKey : tabInView).focus();
    syncKeyToggle();
  }
  // `remember`: the reader chose (from 720px, kept in this browser). While the map still shows the
  // national view, opening or closing fits it again for the canvas the panel now leaves (M-7, as
  // the key did), so an open panel never lies over the East Coast. What is in view changes with it
  // (from 720px the open panel covers the canvas's right side, `inViewNow`), so it is counted again
  // from the answers in hand, before the tab's scroll is put back.
  function setPanelOpen(open, remember) {
    if (!panel) return;
    var refit = isAtHome();
    // Collapsing hides the body, which loses its scroll: keep the tab's place and put it back on opening.
    if (!open && panelIsOpen() && panelScroll) tabScroll[panelTab] = panelScroll.scrollTop;
    panel.classList.toggle("is-collapsed", !open);
    // A phone's sheet opens at half the map; only a drag takes it to the full height.
    if (!open) panel.classList.remove("is-full");
    panelToggle.setAttribute("aria-expanded", open ? "true" : "false");
    panelBody.hidden = !open;
    if (hasLoadedOnce) scheduleRender();
    if (open && panelScroll) panelScroll.scrollTop = tabScroll[panelTab] || 0;
    if (panelKeyBtn) panelKeyBtn.hidden = open;
    if (remember && !isPhone()) storePanelChoice(open);
    syncKeyToggle();
    // The phone's sheet lies over the map's foot, not beside it: the national view is fitted above its
    // peek, and opening it further covers the map rather than refitting it.
    if (refit && !isPhone()) goHome(motionMs(300));
  }
  // The phone's sheet (docs/30 §11.8): three resting heights, the peek, half the map and the map less
  // 56px, and a drag of its head rests at the nearest, or the next one along when flung. A drag that
  // moves less than 6px is a tap, and leaves the head's button to open or close the sheet.
  var SHEET_SLOP_PX = 6;
  function sheetHeights() {
    var frame = mapEl.getBoundingClientRect().height;
    var peek = parseFloat(window.getComputedStyle(panel).getPropertyValue("--sheet-peek")) || 112;
    return [peek, Math.round(frame / 2), Math.round(frame - 56)];
  }
  function restSheet(index) {
    panel.style.height = "";
    if (index === 0) { setPanelOpen(false, false); return; }
    if (!panelIsOpen()) setPanelOpen(true, false);
    panel.classList.toggle("is-full", index === 2);
  }
  (function wireSheetDrag() {
    var head = panel ? panel.querySelector(".map-panel__head") : null;
    if (!head) return;
    var start = null;
    var dragged = false;
    // The drag is not a press: the click a finger lifted after a drag makes, on the head's button,
    // must not toggle the sheet as well. Only that click: the flag lapses straight after it.
    var swallowClick = false;
    head.addEventListener("click", function (ev) {
      if (!swallowClick) return;
      swallowClick = false;
      ev.stopPropagation();
      ev.preventDefault();
    }, true);
    head.addEventListener("pointerdown", function (e) {
      if (!isPhone() || (e.pointerType === "mouse" && e.button !== 0)) return;
      start = { y: e.clientY, height: panel.getBoundingClientRect().height, at: Date.now() };
      dragged = false;
    });
    head.addEventListener("pointermove", function (e) {
      if (!start) return;
      var dy = start.y - e.clientY;
      if (!dragged && Math.abs(dy) < SHEET_SLOP_PX) return;
      if (!dragged) { dragged = true; head.setPointerCapture(e.pointerId); panel.classList.add("is-dragging"); }
      var h = sheetHeights();
      panel.style.height = Math.max(h[0], Math.min(h[2], start.height + dy)) + "px";
    });
    function end(e) {
      if (!start) return;
      var was = start;
      start = null;
      panel.classList.remove("is-dragging");
      if (!dragged) return;
      var h = sheetHeights();
      var now = panel.getBoundingClientRect().height;
      var speed = (now - was.height) / Math.max(1, Date.now() - was.at); // px per ms, up is positive
      var nearest = 0;
      h.forEach(function (v, i) { if (Math.abs(v - now) < Math.abs(h[nearest] - now)) nearest = i; });
      if (Math.abs(speed) > 0.5) {
        var from = h.reduce(function (best, v, i) { return Math.abs(v - was.height) < Math.abs(h[best] - was.height) ? i : best; }, 0);
        nearest = Math.max(0, Math.min(2, from + (speed > 0 ? 1 : -1)));
      }
      restSheet(nearest);
      dragged = false;
      if (e && e.type === "pointerup") {
        swallowClick = true;
        window.setTimeout(function () { swallowClick = false; }, 350);
      }
    }
    head.addEventListener("pointerup", end);
    head.addEventListener("pointercancel", end);
  })();
  // The Key tab, from the rail's "Key" (below 720px) or the collapsed header's "Key": a press on an
  // open Key tab closes the panel again.
  function toggleKey() {
    if (panelIsOpen() && panelTab === "key") {
      setPanelOpen(false, true);
      return;
    }
    selectTab("key");
    setPanelOpen(true, true);
  }
  var panelChosen = false;
  if (panel && panelToggle) {
    panel.setAttribute("data-ready", "");
    selectTab("inview");
    setPanelOpen(panelDefaultOpen(), false);
    panelToggle.addEventListener("click", function () {
      panelChosen = true;
      var open = !panelIsOpen();
      // The header's button says what is in view, so opening the panel from it shows "In view"
      // (a phone's bar reopened on the Key tab after the rail's "Key" had closed it). The Key tab
      // has its own button beside it while the panel is collapsed.
      if (open) selectTab("inview");
      setPanelOpen(open, true);
    });
    if (panelKeyBtn) panelKeyBtn.addEventListener("click", function () { panelChosen = true; toggleKey(); });
    if (mapKeyToggle) {
      mapKeyToggle.hidden = false;
      mapKeyToggle.addEventListener("click", function () { panelChosen = true; toggleKey(); });
    }
    [tabInView, tabKey].forEach(function (tab) {
      tab.addEventListener("click", function () { selectTab(tab === tabKey ? "key" : "inview"); });
      // The tab pattern's arrows: one tab stop, Left and Right move between the two.
      tab.addEventListener("keydown", function (e) {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Home" && e.key !== "End") return;
        e.preventDefault();
        selectTab(panelTab === "key" ? "inview" : "key", true);
      });
    });
    // The skip link to the results opens the panel on "In view" first.
    Array.prototype.forEach.call(document.querySelectorAll('a[href="#in-view-list"]'), function (link) {
      link.addEventListener("click", function () {
        selectTab("inview");
        if (!panelIsOpen()) setPanelOpen(true, false);
      });
    });
    // Until the reader chooses, the default follows the width (a turned phone, a resized window);
    // a phone's bar always starts closed, and from 720px a stored choice stands.
    var followWidth = function () {
      if (isPhone()) { if (!panelChosen) setPanelOpen(false, false); return; }
      if (!panelChosen) setPanelOpen(panelDefaultOpen(), false);
    };
    if (narrowKey && narrowKey.addEventListener) narrowKey.addEventListener("change", followWidth);
    if (widePanel && widePanel.addEventListener) widePanel.addEventListener("change", followWidth);
    // From 720px a collapsed header lies over the canvas's top, and the national view is fitted
    // under it. Should it change height while nobody has moved the map (its words wrap to a third
    // line), the view is fitted again, so the header never ends over the East Coast.
    var collapsedHeadAtHome = 0;
    var refitUnderHeader = function () {
      if (isPhone() || panelIsOpen() || !isAtHome()) return;
      var h = panel.getBoundingClientRect().height;
      if (collapsedHeadAtHome && Math.abs(h - collapsedHeadAtHome) > 1) goHome(motionMs(300));
      collapsedHeadAtHome = h;
    };
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(function () { refitUnderHeader(); }).observe(panel);
    }
  }
  // M-7: the first view, once the panel is open or closed for this width (its box is part of the
  // padding where it lies over the canvas). A view or region in the link wins, as before; the home
  // camera is still worked out, so moving back to it clears `center`/`zoom` from the URL.
  var firstHome = homeCamera();
  if (!viewRestored && firstHome) map.jumpTo({ center: firstHome.center, zoom: firstHome.zoom });
  if (restoreBbox) {
    map.jumpTo({ center: [(restoreBbox[0] + restoreBbox[2]) / 2, (restoreBbox[1] + restoreBbox[3]) / 2] });
    map.fitBounds([[restoreBbox[0], restoreBbox[1]], [restoreBbox[2], restoreBbox[3]]], { duration: 0, padding: fitPadding(8) });
  }
  // A canvas with no size yet has no fit (`cameraForBounds` gives none), and without one every move
  // would write `center`/`zoom` to the URL. Fit once it has a size.
  if (!firstHome) {
    map.on("resize", function fitOnceSized() {
      var cam = homeView || homeCamera();
      if (!cam) return;
      map.off("resize", fitOnceSized);
      if (!viewRestored) map.jumpTo({ center: cam.center, zoom: cam.zoom });
    });
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

  // docs/17 M-5: a cluster's status breakdown and capacity sum were on a hover card only, which a
  // touch screen never shows (D-10). The panel's header and a region's summary say them in words:
  // one part per lifecycle family, with the key's glyph, then the stated capacity. GW from 1,000 MW,
  // through the one MW formatter.
  // The words are the Status filter's own names, as the key's rows and the overview's use them
  // (review 2026-10-22: the header said "built" and "announced or unknown" in words of its own): a
  // family's part names the filter's groups its states fall in, "Announced", "Status unknown" or
  // "Announced or Status unknown". The key's own words for the family stand in should none be found.
  var KEY_WORDS = {};
  Array.prototype.forEach.call(document.querySelectorAll("#lifecycle-legend [data-legend-family]"), function (row) {
    var words = row.querySelector(".legend__words");
    if (!words) return;
    var copy = words.cloneNode(true);
    Array.prototype.forEach.call(copy.querySelectorAll(".legend__detail"), function (n) { n.remove(); });
    KEY_WORDS[row.getAttribute("data-legend-family")] = copy.textContent.replace(/\s+/g, " ").trim();
  });
  function familyWords(family, stateCounts) {
    var present = {};
    Object.keys(stateCounts).forEach(function (state) {
      if ((Number(stateCounts[state]) || 0) > 0 && familyOf(state) === family) present[statusGroupOf(state)] = true;
    });
    var names = STATUS_GROUP_ORDER.filter(function (g) { return present[g]; })
      .map(function (g) { return STATUS_GROUP_LABEL[g]; })
      .filter(Boolean);
    return names.length ? names.join(" or ") : (KEY_WORDS[family] || family);
  }
  var REGION_PLACED_BY = { county: "by county", state: "by state", country: "by country" };
  function fmtCapacity(mw) { return mw >= 1000 ? fmtMWNumber(mw / 1000) + " GW" : fmtMW(mw); }
  function breakdownParts(stateCounts, capacityMw) {
    var byFamily = {};
    Object.keys(stateCounts).forEach(function (state) {
      var n = Number(stateCounts[state]) || 0;
      if (n > 0) byFamily[familyOf(state)] = (byFamily[familyOf(state)] || 0) + n;
    });
    var parts = FAMILIES.filter(function (f) { return byFamily[f]; })
      .sort(function (a, b) { return byFamily[b] - byFamily[a] || FAMILIES.indexOf(a) - FAMILIES.indexOf(b); })
      .map(function (f) {
        return "<span class=\"in-view-summary__family in-view-summary__family--" + f + "\">" +
          "<svg class=\"legend__icon\" width=\"12\" height=\"12\" aria-hidden=\"true\"><use href=\"#icon-" + f + "\"></use></svg>" +
          "<span class=\"tnum\">" + fmtCount(byFamily[f]) + "</span> " + esc(familyWords(f, stateCounts)) + "</span>";
      });
    if (capacityMw > 0) parts.push("<span class=\"tnum\">" + esc(fmtCapacity(capacityMw)) + "</span>");
    return parts;
  }
  // ---- what "in view" means while the panel floats over the map (review 2026-10-22) ----
  // From 720px the open panel covers the canvas's right side (340px of 1,400 at 1440x900), so what
  // lies under it is not in view: the marks are still drawn there (a pan brings them out without a
  // wait), but the header, the overview, the list and the busiest areas count only what the reader
  // sees, left of the panel. A collapsed panel is its header over the top right corner, and a
  // phone's panel lies under the map: both leave the whole canvas in view. The drawer, which opens
  // in the panel's place, covers the same box, so opening it changes nothing.
  // `coveredFromX`: the canvas x where the open panel starts, or null when nothing covers the canvas.
  function coveredFromX() {
    if (!panel || isPhone() || !panelIsOpen()) return null;
    var m = mapEl.getBoundingClientRect();
    var k = panel.getBoundingClientRect();
    if (!(k.width > 0 && k.height > 0) || k.left >= m.right || k.right <= m.left || k.top >= m.bottom || k.bottom <= m.top) return null;
    return Math.max(0, k.left - m.left);
  }
  // A point is in view left of `edge`; a line or an area while any of its vertices is.
  function leftOf(geometry, edge) {
    if (edge == null) return true;
    if (!geometry || !geometry.coordinates) return false;
    if (geometry.type === "Point") return map.project(geometry.coordinates).x < edge;
    var found = false;
    (function walk(c) {
      if (found || !c || !c.length) return;
      if (typeof c[0] === "number") { found = map.project(c).x < edge; return; }
      for (var i = 0; i < c.length && !found; i++) walk(c[i]);
    })(geometry.coordinates);
    return found;
  }
  // The latest answers, cut to what the reader sees. Everything that says what is in view reads this.
  function inViewNow() {
    var edge = coveredFromX();
    var keep = function (f) { return leftOf(f.geometry, edge); };
    var assets = plantsToggle.checked ? latestAssets.features.filter(keep) : [];
    var retired = retiredToggle.checked ? latestRetired.features.filter(keep) : [];
    return {
      features: latestCollection.features.filter(keep),
      regions: latestRegionFeatures.filter(keep),
      assets: assets,
      retired: retired,
      plantsTotal: edge == null ? latestPlantsTotal : assetsInView(assets),
      retiredTotal: edge == null ? latestRetiredTotal : assetsInView(retired)
    };
  }
  // The same cut as a box, for `/api/map/areas`: the map's bounds left of the open panel.
  function inViewBbox() {
    var edge = coveredFromX();
    if (edge == null) return currentBbox();
    var sw = map.unproject([0, mapEl.clientHeight]);
    var ne = map.unproject([edge, 0]);
    return [sw.lng, sw.lat, ne.lng, ne.lat];
  }

  // ---- the panel's head and its "In view" overview (docs/30 §11.6, §11.7) ----
  // What the view holds is counted from what is drawn and not under the panel (`inViewNow`; audit
  // 2026-09-30 F4: `totals.records` counts every match anywhere and stays on the filter sheet's count
  // line): a place's mark stands for its count, a point for one, a county or a state outline for the
  // projects the source places there. A touch screen never sees a mark's hover card, so the head
  // carries its breakdown (docs/17 M-5, D-10).
  var panelBar = document.getElementById("map-panel-bar");
  var panelFamilies = document.getElementById("map-panel-families");
  var overviewEl = document.getElementById("map-overview");
  var overviewTech = document.getElementById("map-overview-tech");
  var NO_STATUS_WORDS = "No status chosen: projects are hidden.";
  // The Status filter's own groups and words (`_macros.html` status_filter, from the server's
  // PROPOSAL_STATUS_GROUPS), read from its boxes, so the panel names each status as it does.
  var STATUS_GROUP_ORDER = [];
  var STATUS_GROUP_LABEL = {};
  statusBoxes.forEach(function (box) {
    var key = box.getAttribute("data-status-group") || box.value;
    var label = box.parentNode ? box.parentNode.cloneNode(true) : null;
    if (label) Array.prototype.forEach.call(label.querySelectorAll("input, .filter-field__note"), function (n) { n.remove(); });
    STATUS_GROUP_ORDER.push(key);
    STATUS_GROUP_LABEL[key] = label ? label.textContent.replace(/\s+/g, " ").trim() : key;
  });
  var GROUP_OF_STATE = { announced: "announced", contracted: "contracted", built: "built", unknown: "unknown" };
  function statusGroupOf(state) {
    if (GROUP_OF_STATE[state]) return GROUP_OF_STATE[state];
    if (WITHDRAWN_STATES.indexOf(state) !== -1) return "withdrawn";
    if (DEFAULT_STATES.indexOf(state) !== -1) return "in_process";
    return "unknown";
  }
  var GROUP_FAMILY = { announced: "neutral", in_process: "progress", contracted: "committed", built: "success", withdrawn: "danger", unknown: "neutral" };

  // The GW beside the count is generating and storage capacity, as every other figure on the site
  // sums it (services/api/capacity_kinds.py): a data centre's MW is demand, a line's is transfer
  // capability and a pipeline's throughput, and none of them is added. A place's mark and a region
  // carry that sum by status (`capacity_mw_by_lifecycle_state`, services/api/geo.py); an older answer
  // without it falls back to every stated MW.
  var NOT_CAPACITY_KINDS = ["load", "transmission", "pipeline", "lng", "ccs", "hydrogen"];
  function groupCapacity(p) {
    var byState = propObj(p.capacity_mw_by_lifecycle_state);
    if (!byState) return Number(p.capacity_mw_sum) || 0;
    return Object.keys(byState).reduce(function (sum, s) { return sum + (Number(byState[s]) || 0); }, 0);
  }
  function pointCapacity(p) {
    if (NOT_CAPACITY_KINDS.indexOf(p.kind) !== -1) return 0;
    return Number(p.capacity_mw) || 0;
  }

  // Everything the head and the overview say, for what is in view (`inViewNow`).
  function viewTotals(shown) {
    var t = { total: 0, capacity: 0, states: {}, techs: {}, families: {}, regionPlaced: 0 };
    function add(table, key, n) { if (n > 0) table[key] = (table[key] || 0) + n; }
    function addCounts(table, counts, fallback, n) {
      var obj = propObj(counts);
      if (obj && Object.keys(obj).length) Object.keys(obj).forEach(function (k) { add(table, k, Number(obj[k]) || 0); });
      else if (fallback != null) add(table, fallback, n);
    }
    shown.features.forEach(function (f) {
      var p = f.properties;
      if (p.feature_kind === "cluster") {
        var n = Number(p.count) || 0;
        t.total += n;
        addCounts(t.states, p.lifecycle_state_counts, p.dominant_lifecycle_state || "unknown", n);
        addCounts(t.techs, p.technology_counts, null, n);
        t.capacity += groupCapacity(p);
      } else if (p.feature_kind === "proposal") {
        t.total += 1;
        add(t.states, p.lifecycle_state || "unknown", 1);
        add(t.techs, p.technology || "", 1);
        t.capacity += pointCapacity(p);
      }
    });
    shown.regions.forEach(function (f) {
      var p = f.properties;
      var n = Number(p.count) || 0;
      t.total += n;
      t.regionPlaced += n;
      addCounts(t.states, p.lifecycle_state_counts, "unknown", n);
      addCounts(t.techs, p.technology_counts, null, n);
      t.capacity += groupCapacity(p);
    });
    Object.keys(t.states).forEach(function (s) { add(t.families, familyOf(s), t.states[s]); });
    return t;
  }

  function familyGlyph(family, size) {
    return "<svg class=\"legend__icon\" width=\"" + size + "\" height=\"" + size + "\" aria-hidden=\"true\"><use href=\"#icon-" + family + "\"></use></svg>";
  }

  // S-4: a figure in the panel moves from the value shown to the new one over 200ms (--motion-base),
  // easing out; a new value retargets from the value on screen; every frame is rounded and separated
  // as the final one is; the first value is set at once, never counted up from zero; under reduced
  // motion every value is set at once. Each figure's box holds its widest value (styles.css), so a
  // digit more moves nothing beside it.
  var FIGURE_MS = 200;
  function setFigure(el, to, format) {
    if (!el) return;
    var from = el.__figure;
    el.__figure = to;
    if (el.__frame) { window.cancelAnimationFrame(el.__frame); el.__frame = 0; }
    if (from == null || reduceMotion || from === to || typeof window.requestAnimationFrame !== "function") {
      el.__shown = to;
      el.textContent = format(to);
      return;
    }
    var shown = el.__shown != null ? el.__shown : from;
    var t0 = null;
    function step(now) {
      if (t0 == null) t0 = now;
      var k = Math.min(1, (now - t0) / FIGURE_MS);
      var v = k < 1 ? shown + (to - shown) * (1 - Math.pow(1 - k, 3)) : to;
      el.__shown = v;
      el.textContent = format(v);
      el.__frame = k < 1 ? window.requestAnimationFrame(step) : 0;
    }
    el.__frame = window.requestAnimationFrame(step);
  }

  // The head: "N projects in view · X GW", a thin bar by status family in the map's colours (in the
  // key's order, so it reads the same way every time), and under it one row per status the filter
  // shows, with the map's mark, its name and its count (drawn from first paint; a zero is a muted
  // "0"), so the bar is never colour alone (D-5). The noun "projects" is said to a screen reader only, at
  // every width ("4,139 in view · 742.7 GW"): it keeps the first screen in its word budget with the map's
  // credits counted (docs/31-brand-identity.md §6), and it reads apart from the strip's "4,167 projects",
  // which counts the whole country's list, not what the map draws in view.
  var countParts = null;
  function headCountParts() {
    if (countParts) return countParts;
    panelCountEl.textContent = "";
    countParts = { n: document.createElement("span"), noun: document.createElement("span"), rest: document.createTextNode(" in view"), cap: document.createElement("span") };
    countParts.n.className = "map-panel__n tnum";
    countParts.noun.className = "map-panel__noun";
    countParts.cap.className = "map-panel__cap tnum";
    // One line of text inside the count's box, which is a flex box while collapsed (styles.css): its
    // parts stay inline, so the line reads, and is read out, as one sentence.
    var line = document.createElement("span");
    line.appendChild(countParts.n);
    line.appendChild(countParts.noun);
    line.appendChild(countParts.rest);
    line.appendChild(countParts.cap);
    panelCountEl.appendChild(line);
    return countParts;
  }
  function renderPanelHeader(t) {
    if (!panelCountEl) return;
    if (noStatusChosen()) {
      countParts = null;
      panelCountEl.textContent = NO_STATUS_WORDS;
      if (panelBar) { panelBar.textContent = ""; panelBar.hidden = true; }
      if (panelFamilies) panelFamilies.hidden = true;
      return;
    }
    if (!hasLoadedOnce) return;
    var parts = headCountParts();
    setFigure(parts.n, t.total, fmtCount);
    parts.noun.textContent = t.total === 1 ? " project" : " projects";
    parts.noun.classList.add("visually-hidden");
    setFigure(parts.cap, t.capacity, function (mw) { return mw > 0 ? " · " + fmtCapacity(mw) : ""; });
    if (panelBar) {
      panelBar.textContent = "";
      FAMILIES.forEach(function (f) {
        if (!t.families[f]) return;
        var seg = document.createElement("span");
        seg.className = "map-panel__seg map-panel__seg--" + f;
        seg.style.flexGrow = String(t.families[f]);
        panelBar.appendChild(seg);
      });
      // An empty view keeps the bar's box (an empty track), so the head keeps its height.
      panelBar.hidden = false;
    }
    if (panelFamilies) {
      panelFamilies.hidden = false;
      var groups = {};
      Object.keys(t.states).forEach(function (s) { var g = statusGroupOf(s); groups[g] = (groups[g] || 0) + t.states[s]; });
      Array.prototype.forEach.call(panelFamilies.querySelectorAll("li[data-status-group]"), function (row) {
        var n = groups[row.getAttribute("data-status-group")] || 0;
        setFigure(row.querySelector("[data-count]"), n, fmtCount);
        row.classList.toggle("is-zero", n === 0);
      });
    }
  }
  // The status rows follow the Status filter: one per ticked group, in its order (a filter change may
  // change the panel's stack; a move never does, S-2).
  function syncFamilyRows() {
    if (!panelFamilies) return;
    var ticked = statusBoxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute("data-status-group") || b.value; });
    var rows = {};
    Array.prototype.forEach.call(panelFamilies.querySelectorAll("li[data-status-group]"), function (row) { rows[row.getAttribute("data-status-group")] = row; });
    var same = Object.keys(rows).length === ticked.length && ticked.every(function (k) { return rows[k]; });
    if (same) return;
    panelFamilies.textContent = "";
    ticked.forEach(function (key) {
      var family = GROUP_FAMILY[key] || "neutral";
      var row = rows[key];
      if (!row) {
        var label = STATUS_GROUP_LABEL[key] || key;
        row = document.createElement("li");
        row.className = "map-panel__family in-view-summary__family--" + family;
        row.setAttribute("data-status-group", key);
        row.setAttribute("data-family", family);
        row.innerHTML = familyGlyph(family, 12) + "<span class=\"map-panel__fname\">" + esc(label) +
          "</span> <span class=\"map-panel__fcount tnum\" data-count></span>";
      }
      panelFamilies.appendChild(row);
    });
  }

  // S-3: "In view" is an overview at national and multi-state views and the list zoomed in (owner,
  // 2026-10-22: the panel was "confusing while zoomed all the way out"), with room between the two
  // ways, so a view near the switch does not flip on small moves. `n` is the projects in view (what
  // lies left of the open panel), `zoom` the map's. A clustered answer (more than 500 placed at a site
  // in view, services/api/geo.py SPLIT_THRESHOLD) cannot be listed. Asked once per committed update.
  function nextMode(mode, n, zoom, clustered) {
    if (mode === "overview") {
      return n <= 25 || (zoom >= 5.5 && n <= 250 && !clustered) ? "list" : "overview";
    }
    return (n > 40 && zoom < 4.5) || n > 350 || clustered ? "overview" : "list";
  }
  var inViewMode = "overview";
  function setInViewMode(mode) {
    inViewMode = mode;
    if (overviewEl) overviewEl.hidden = mode !== "overview";
    listEl.hidden = mode === "overview";
    if (panel) panel.setAttribute("data-mode", mode);
  }

  // S-5: a ranked list keeps its rows and their order on an update, the values changing in place,
  // unless a row would move two or more places in the exact order, or a row's value now exceeds the
  // row above it by more than 10%; then it sorts exactly, once. A row that leaves is replaced in its
  // slot by the best-ranked newcomer; other newcomers join at the end. `previous` is the keys shown,
  // `values` the new value of every key, `limit` how many rows the list keeps.
  function reorder(previous, values, limit) {
    var exact = Object.keys(values).filter(function (k) { return values[k] > 0; })
      .sort(function (a, b) { return values[b] - values[a] || (a < b ? -1 : a > b ? 1 : 0); })
      .slice(0, limit);
    if (!previous || !previous.length) return exact;
    var newcomers = exact.filter(function (k) { return previous.indexOf(k) === -1; });
    var kept = previous.map(function (k) { return values[k] > 0 ? k : newcomers.shift() || null; })
      .filter(function (k) { return k != null; });
    while (kept.length < limit && newcomers.length) kept.push(newcomers.shift());
    kept = kept.slice(0, limit);
    var resort = kept.some(function (k, i) {
      var j = exact.indexOf(k);
      return j === -1 || Math.abs(j - i) >= 2 || (i > 0 && values[k] > values[kept[i - 1]] * 1.1);
    });
    return resort ? exact : kept;
  }
  // The panel's rules, for the e2e suite's case tables (docs/30 S-3, S-4, S-5).
  window.__mapPanelRules = { nextMode: nextMode, reorder: reorder, setFigure: setFigure };
  // From first paint the overview's slots hold their places (S-2, S-7): its rows wait as skeletons.
  setInViewMode(inViewMode);

  // The overview's technology rows: four kept from first paint, each the technology's name, a thin bar
  // of its count against the top row's (3:1 or more on the panel; the top row in ink, the dataviz rule
  // for a ranked list in a narrow card) and the count. A technology the view does not hold leaves its
  // row empty, at the same height. Their order holds while the reader's pointer or focus is on the
  // panel; a re-sort waits until 400ms after the pointer leaves (S-5).
  var TECH_ROWS = 4;
  var techShown = [];
  var panelHeld = false;
  var resortTimer = null;
  if (panel) {
    panel.addEventListener("pointerenter", function () { panelHeld = true; window.clearTimeout(resortTimer); });
    panel.addEventListener("pointerleave", function () {
      panelHeld = false;
      window.clearTimeout(resortTimer);
      resortTimer = window.setTimeout(function () { if (hasLoadedOnce) scheduleRender(); }, 400);
    });
  }
  function techRow(li) {
    if (!li.__parts) {
      li.textContent = "";
      var name = document.createElement("span");
      name.className = "map-overview__name";
      var meter = document.createElement("span");
      meter.className = "map-overview__meter";
      meter.setAttribute("aria-hidden", "true");
      var fill = document.createElement("span");
      fill.className = "map-overview__fill";
      meter.appendChild(fill);
      var num = document.createElement("span");
      num.className = "map-overview__count tnum";
      li.appendChild(name);
      li.appendChild(meter);
      li.appendChild(num);
      li.__parts = { name: name, fill: fill, num: num };
    }
    return li.__parts;
  }
  function renderOverview(t) {
    if (!overviewTech) return;
    var holding = panelHeld || (panel && panel.contains(document.activeElement) && document.activeElement !== document.body);
    var order = holding && techShown.length
      ? techShown.map(function (k) { return t.techs[k] > 0 ? k : null; }).filter(function (k) { return k != null; })
      : reorder(techShown, t.techs, TECH_ROWS);
    techShown = order;
    var top = order.reduce(function (m, k) { return Math.max(m, t.techs[k] || 0); }, 0);
    Array.prototype.forEach.call(overviewTech.children, function (li, i) {
      var key = order[i];
      li.classList.toggle("is-empty", key == null);
      li.classList.toggle("is-top", i === 0 && key != null);
      if (key == null) {
        li.__parts = null;
        li.textContent = "";
        return;
      }
      var parts = techRow(li);
      parts.name.textContent = key ? technologyName(key) : "Technology not stated";
      parts.fill.style.width = Math.max(4, Math.round((t.techs[key] / (top || 1)) * 100)) + "%";
      setFigure(parts.num, t.techs[key], fmtCount);
    });
  }
  // A filter change sorts the lists exactly (S-5).
  function resetRankedOrder() { techShown = []; }
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
    // A state reads as its name ("Indiana"), not its code ("US-IN"): the server's table (UX-5).
    var regionNames = SERVER_LABELS.region || {};
    if (Object.prototype.hasOwnProperty.call(regionNames, id)) return regionNames[id];
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

  // The list, zoomed in (it is the old "In view" panel's, kept: owner 2026-10-22): projects first,
  // then region records, existing assets and retired plants as their own labelled groups, a page of
  // each. Hovering a row rings its mark on the map (`bindRowHover`).
  function renderList(totals, shown) {
    var individual = shown.features.filter(function (f) {
      return f.properties.feature_kind !== "cluster" && f.properties.feature_kind !== "region";
    });
    if (totals.clustered) {
      var li = document.createElement("li");
      li.textContent = "Zoom in or narrow the filters to list proposals individually; " +
        fmtCount(totals.records) + " match in clustered form above.";
      listEl.appendChild(li);
    } else {
      var groups = groupProposalFeatures(individual).slice(0, IN_VIEW_LIMIT);
      if (!groups.length) {
        var none = document.createElement("li");
        none.className = "in-view-list__empty";
        none.textContent = noStatusChosen() ? NO_STATUS_WORDS : (totals.records || 0) === 0
          ? "No proposals match these filters. The note above names each one; Clear all removes them."
          : "No proposals in this part of the map. Zoom out or move the map to see the " + fmtCount(totals.records) + " that match.";
        if (!shown.regions.length) listEl.appendChild(none);
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
          (g.capacity_mw ? " · " + fmtMW(g.capacity_mw) : "");
        node.querySelector(".meta").textContent = meta;
        // D-13: the drawer (sources, licence) was reachable only by clicking a dot on the canvas.
        // Every proposal row now has the same Details button the asset rows have.
        var details = node.querySelector(".details-btn");
        if (details) {
          details.setAttribute("aria-label", "Details for " + (p.name || "this proposal"));
          details.addEventListener("click", function () { openDrawer(p); });
        }
        bindRowHover(node.querySelector("li"), g.geometry);
        listEl.appendChild(node);
      });
      appendShowMore("proposals", shownProposals, groups.length, "proposals");
    }

    // Region records (ADR 0008 placement grade "region") in the list as well as on the map, so a
    // keyboard or screen-reader user reaches them without a pointer (web audit 2026-09-18).
    if (shown.regions.length) {
      appendGroupName("Regions (" + fmtCount(shown.regions.length) + ")");
      var regionRows = shown.regions.slice(0, IN_VIEW_LIMIT);
      var shownRegions = Math.min(inViewShown.regions, regionRows.length);
      regionRows.slice(0, shownRegions).forEach(function (f, index) {
        var p = f.properties;
        var item = document.createElement("li");
        markRow(item, "regions", index);
        var a = document.createElement("a");
        a.href = regionListUrl(p);
        a.textContent = regionName(p);
        item.appendChild(a);
        // The summary a tap on the region opens, from the keyboard (docs/17 M-2, D-14).
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "details-btn";
        btn.textContent = "Details";
        btn.setAttribute("aria-label", "Details for " + regionName(p));
        btn.addEventListener("click", function () { openRegionDrawer(p, regionName(p)); });
        item.appendChild(btn);
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = plural(Number(p.count) || 0, "proposal") + " placed at this region";
        item.appendChild(meta);
        bindRowHover(item, regionPolygonCache[p.region_level + ":" + p.region_id] || f.geometry);
        listEl.appendChild(item);
      });
      appendShowMore("regions", shownRegions, regionRows.length, "regions");
    }

    // Existing assets in view (points and lines alike): name linked to the asset page where the
    // feature carries one, a Details button that opens the same drawer a map click does.
    var assetsIndividual = [];
    if (plantsToggle.checked) {
      assetsIndividual = shown.assets.filter(function (f) {
        return f.properties.feature_kind === "asset" || f.properties.feature_kind === "asset_line";
      });
      if (latestAssetsClustered || assetsIndividual.length) {
        appendGroupName("Existing assets (" + fmtCount(shown.plantsTotal) + ")");
      }
      if (latestAssetsClustered) {
        var cl = document.createElement("li");
        cl.textContent = "Zoom in to list existing assets individually; " + fmtCount(shown.plantsTotal) + " are grouped in clusters above.";
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
        bindRowHover(item, f.geometry);
        listEl.appendChild(item);
      });
      appendShowMore("assets", shownAssets, assetRows.length, "assets");
    }

    // Lane R1: retired and retiring plants in view, one row each, the status in words.
    if (retiredToggle.checked) {
      var retiredIndividual = shown.retired.filter(function (f) { return f.properties.feature_kind === "asset"; });
      if (latestRetiredClustered || retiredIndividual.length) {
        appendGroupName("Retired & retiring plants (" + fmtCount(shown.retiredTotal) + ")");
      }
      if (latestRetiredClustered) {
        var rcl = document.createElement("li");
        rcl.textContent = "Zoom in to list retired and retiring plants individually; " + fmtCount(shown.retiredTotal) + " are grouped in clusters above.";
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
          p.capacity_mw ? fmtMW(p.capacity_mw) : null,
          PLANT_FAMILY_NAME[plantFamilyOf(p.technology)] || null,
          retirementPhrase(p),
          p.state_code || null
        ].filter(Boolean).join(" · ");
        item.appendChild(meta);
        bindRowHover(item, f.geometry);
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
  }

  // ADR 0008: "none" grade (no usable location) is counted only in `meta.unplaced_count`, never
  // drawn -- checking the "None" placement checkbox has no effect on what is fetched or drawn (a
  // none-grade proposal has no geometry either way).
  function unplacedNow() {
    if (noStatusChosen()) return 0;
    return Number(latestMeta.unplaced_count || (latestCollection.totals || {}).unplaced || 0);
  }
  // The count line says how the two numbers a first-time reader sees relate (review 2026-10-22: "6009
  // proposals match these filters" above the map and "4,138 projects in view" on the panel, with the
  // 1,000 that are not on the map named only at the foot of the overview): every match anywhere, in
  // the panel's noun, then how many of them no map can place, on a line of its own from 720px.
  // Below 720px the count shares one row with the notice's short form (docs/17 M-6), so the second
  // sentence is not drawn there and the panel's "In view" tab opens with it instead
  // (`#unplaced-note`, styles.css 8.3).
  function renderCountLine(records, unplaced) {
    countEl.textContent = fmtCount(records) + (records === 1 ? " project matches" : " projects match") + " these filters.";
    if (unplaced > 0) {
      var part = document.createElement("span");
      part.className = "map-status__unplaced";
      part.textContent = " " + fmtCount(unplaced) + " of them can\u2019t be placed on the map (the Projects list has " +
        (unplaced === 1 ? "it" : "them") + ").";
      countEl.appendChild(part);
    }
    countEl.removeAttribute("aria-hidden");
  }

  // S-6: one commit per settled move. While the map moves (a drag, a pinch, a wheel, a flight)
  // nothing in the panel changes; the answers that land are committed together in the next frame once
  // it has settled, so a view's answers arriving one after another redraw the panel once, not four
  // times. "Show more" and a tab or a collapse render at once (they are the reader's own press).
  var renderQueued = false;
  var renderAfterMove = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    var run = function () {
      renderQueued = false;
      if (map.isMoving()) { renderAfterMove = true; return; }
      render();
    };
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run);
    else window.setTimeout(run, 16);
  }
  map.on("moveend", function () {
    if (!renderAfterMove) return;
    renderAfterMove = false;
    scheduleRender();
  });

  function render() {
    var totals = latestCollection.totals || {};
    var unplacedCount = unplacedNow();
    // Only once proposals have loaded: an asset layer (on by default since 2026-10-20) that arrives
    // first, or after a failed proposals fetch, would otherwise say "0 projects match" over
    // "Loading projects..." or "Proposals could not be loaded."
    if (hasLoadedOnce) renderCountLine(Number(totals.records) || 0, unplacedCount);
    // The skeleton lines hold the panel's rows only until the first answer (S-7; styles.css 8.5).
    if (hasLoadedOnce && panel) panel.setAttribute("data-loaded", "");
    // The panel: its head, then "In view" as an overview or a list for this view (owner,
    // 2026-10-22). Both are built from the answers in hand, so the switch costs no request; both
    // count only what the open panel leaves in sight (`inViewNow`).
    var shown = inViewNow();
    var view = viewTotals(shown);
    renderPanelHeader(view);
    if (noStatusChosen()) setInViewMode("list");
    else if (hasLoadedOnce) setInViewMode(nextMode(inViewMode, view.total, map.getZoom(), !!totals.clustered));
    listEl.innerHTML = "";
    showHoverRing(null);
    if (inViewMode === "list") renderList(totals, shown);
    else renderOverview(view);

    // What the live region says is what the view holds (audit 2026-09-30 F4). `totals.records`
    // counts every proposal matching the filters anywhere, so it stays on the count line above
    // ("match these filters"); "in view" is counted from the features drawn and not under the
    // panel: a cluster stands for its `count`, a point for one, and region-placed proposals are
    // named separately.
    var proposalsInView = 0;
    shown.features.forEach(function (f) {
      var kind = f.properties.feature_kind;
      if (kind === "cluster") proposalsInView += Number(f.properties.count) || 0;
      else if (kind === "proposal") proposalsInView += 1;
    });
    var regionPlaced = 0;
    shown.regions.forEach(function (f) { regionPlaced += Number(f.properties.count) || 0; });
    var liveText = plural(proposalsInView, "proposal") + " in view";
    if (regionPlaced) liveText += ", and " + plural(regionPlaced, "more") + " placed by county or region";
    liveText += ".";
    var linesNoteText = "";
    if (plantsToggle.checked) {
      var linesByType = {};
      shown.assets.forEach(function (f) {
        if (f.properties.feature_kind !== "asset_line") return;
        var type = assetTypeOf(f.properties);
        linesByType[type] = (linesByType[type] || 0) + 1;
      });
      // Each line type by its own name: a transmission line is not a pipeline.
      var lineParts = Object.keys(linesByType).map(function (type) {
        var n = linesByType[type];
        return fmtCount(n) + " " + (n === 1 ? ASSET_TYPES[type].label.toLowerCase() : ASSET_TYPES[type].plural);
      });
      liveText += " " + plural(shown.plantsTotal, "existing asset") + " in view" + (lineParts.length ? " (" + lineParts.join(", ") + ")" : "") + ".";
      var lt = latestAssets.totals || {};
      if (lt.line_count && lt.lines_shown != null && lt.lines_shown < lt.line_count) {
        linesNoteText = "Showing the longest " + fmtCount(lt.lines_shown) + " of " + fmtCount(lt.line_count) +
          " lines in this view; zoom in to see them all.";
        liveText += " " + linesNoteText;
      }
    }
    // Said under the list of existing assets it qualifies; the overview names no line, so there it is
    // the live region's alone.
    if (linesNote) {
      linesNote.hidden = !linesNoteText || inViewMode !== "list";
      linesNote.textContent = linesNoteText;
    }
    if (retiredToggle.checked) liveText += " " + plural(shown.retiredTotal, "retired or retiring plant") + " in view.";
    liveRegion.textContent = liveText;

    // Below 720px the count line has no room for what is not on the map, so "In view" opens with it
    // (drawn only there: from 720px the count line says it).
    if (unplacedCount > 0 && hasLoadedOnce) {
      unplacedNote.hidden = false;
      unplacedNote.textContent = fmtCount(unplacedCount) + " of the " + fmtCount(totals.records) +
        " that match these filters can\u2019t be placed on the map (the Projects list has " + (unplacedCount === 1 ? "it" : "them") + ").";
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
    // A county not yet shaded (below zoom 6, `regionOpacity` 0) is left out of the fill altogether, so
    // bare map neither shows a pointer nor opens a county's summary: its marks at its middle do that.
    map.addLayer({
      id: "region-fill", type: "fill", source: "regions",
      filter: ["all", ["==", ["geometry-type"], "Polygon"], [">", ["get", "region_opacity"], 0]],
      paint: { "fill-color": regionColors.fill, "fill-opacity": ["get", "region_opacity"] }
    }, "clusters");
    map.addLayer({
      id: "region-outline", type: "line", source: "regions",
      filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "region_grade"], "area"]],
      paint: { "line-color": regionColors.line, "line-width": 1, "line-opacity": ["coalesce", ["get", "outline_opacity"], 0.6] }
    }, "clusters");
    // State and country placements: dashed outline only (UX-5), the legend's "dashed outline".
    map.addLayer({
      id: "region-outline-broad", type: "line", source: "regions",
      filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "region_grade"], "broad"]],
      paint: { "line-color": regionColors.line, "line-width": 1.2, "line-opacity": 0.7, "line-dasharray": [3, 2] }
    }, "clusters");
    // A region's count from zoom 6, where its shape can be seen; below it the marks carry the count by
    // their area, without numerals (docs/31-brand-identity.md §10).
    map.addLayer({
      id: "region-labels", type: "symbol", source: "regions", minzoom: COUNTY_SHADED_FROM_ZOOM,
      filter: ["==", ["geometry-type"], "Point"],
      layout: { "text-field": ["get", "count"], "text-size": 11, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": regionColors.line, "text-halo-color": mapColors.land, "text-halo-width": 1.5 }
    }, "clusters");

    // A click or tap is `onMapClick`'s (one action per tap, docs/17 M-2); hover stays per layer.
    map.on("mouseenter", "region-fill", function (e) {
      map.getCanvas().style.cursor = "pointer";
      showRegionTooltip(e);
    });
    map.on("mouseleave", "region-fill", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
  }

  // A region's own list: county -> `county_fips`, state or country -> `jurisdiction`. Since docs/17
  // M-2 a tap on a region no longer goes there at once (a tap on a plant in a shaded county used to
  // leave the map): it opens the region's summary in the drawer, which links here.
  function regionListUrl(p) {
    if (p.region_level === "county") return "/proposals?county_fips=" + encodeURIComponent(p.region_id);
    return "/proposals?jurisdiction=" + encodeURIComponent(p.region_id);
  }
  // A country with a quick-view button of its own (task item 3) can still be shown on the map from
  // its summary, as a click on it used to do.
  function regionJumpButton(p) {
    if (p.region_level !== "country" || p.region_id == null) return null;
    return document.querySelector('.region-btn[data-region="' + String(p.region_id).toLowerCase().replace(/[^a-z0-9_-]/g, "") + '"]');
  }

  function showRegionTooltip(e) {
    if (!hoverAllowed()) return;
    var p = mostSpecificRegion(e.point, e.features, 0).properties; // the region a click here opens
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(e.lngLat)
      .setHTML("<strong>" + esc(p.name || p.region_id) + "</strong><br>" + esc(plural(Number(p.count) || 0, "proposal")))
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
      ctx.arc(6, 6, 4.6, 0, Math.PI * 2); ctx.fill();
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
  // M-1: a project's own mark, on a 16px box. In process: a filled disc in --dot; contracted: a filled
  // square in the committed (navy) colour; announced (neutral): a hollow ring with a 1.5px edge. Each
  // carries a 1px edge of the land colour so overlapping marks stay apart. Built and withdrawn keep the
  // chip (the family's pale fill, its ring and its glyph): they show only when a reader asks for them.
  var MARKER_PX = 16;
  function drawFamilyMarker(ctx, family, text, fill) {
    var c = MARKER_PX / 2;
    ctx.strokeStyle = mapColors.land;
    if (family === "progress") {
      ctx.beginPath(); ctx.arc(c, c, 5.6, 0, Math.PI * 2);
      ctx.fillStyle = dotColor; ctx.fill();
      ctx.lineWidth = 1; ctx.stroke();
    } else if (family === "committed") {
      ctx.fillStyle = text; ctx.fillRect(c - 4.6, c - 4.6, 9.2, 9.2);
      ctx.lineWidth = 1; ctx.strokeRect(c - 4.6, c - 4.6, 9.2, 9.2);
    } else if (family === "neutral") {
      ctx.beginPath(); ctx.arc(c, c, 5, 0, Math.PI * 2);
      ctx.fillStyle = mapColors.land; ctx.globalAlpha = 0.9; ctx.fill(); ctx.globalAlpha = 1;
      ctx.lineWidth = 1.5; ctx.strokeStyle = text; ctx.stroke();
    } else {
      ctx.beginPath(); ctx.arc(c, c, 7.2, 0, Math.PI * 2);
      ctx.fillStyle = fill; ctx.fill();
      ctx.lineWidth = 1.4; ctx.strokeStyle = text; ctx.stroke();
      drawFamilyGlyph(ctx, family, 0.85, c - 5.1, c - 5.1, text);
    }
  }
  // A place's mark at its count's size: radius 2.4 + 1.25√n CSS px, so one project is a 7px dot and a
  // place of a hundred about 15px, area growing with the count (docs/31-brand-identity.md §5.3). Drawn
  // ahead for eight steps of count, so the ring's edge and the surface ring stay 1.5px and 1px at every
  // size instead of scaling with an icon. Each mark carries a ring of the land colour, so a small mark
  // over a larger one at the same place stays apart from it (dataviz: a 2px surface ring on
  // overlapping marks; 1px here, at 7px marks).
  var MARK_STEP_FROM = [1, 2, 3, 5, 9, 17, 33, 65];
  var MARK_STEP_AT = [1, 2, 3.5, 6.5, 12.5, 24.5, 48.5, 96];
  function markRadius(n) { return Math.min(14, 2.4 + 1.25 * Math.sqrt(n)); }
  function markStepExpr() {
    var expr = ["step", ["coalesce", ["get", "mark_count"], 1], 0];
    for (var i = 1; i < MARK_STEP_FROM.length; i++) expr.push(MARK_STEP_FROM[i], i);
    return expr;
  }
  function drawPlaceMark(ctx, family, r, c) {
    var land = mapColors.land;
    var text = colors[family];
    ctx.lineJoin = "round";
    if (family === "progress") {
      ctx.beginPath(); ctx.arc(c, c, r, 0, Math.PI * 2);
      ctx.fillStyle = dotColor; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = land; ctx.stroke();
    } else if (family === "committed") {
      var s = r * Math.sqrt(Math.PI);  // the square of the disc's area
      ctx.fillStyle = text; ctx.fillRect(c - s / 2, c - s / 2, s, s);
      ctx.lineWidth = 1; ctx.strokeStyle = land; ctx.strokeRect(c - s / 2, c - s / 2, s, s);
    } else if (family === "neutral") {
      ctx.beginPath(); ctx.arc(c, c, r - 0.75, 0, Math.PI * 2);
      ctx.lineWidth = 3.5; ctx.strokeStyle = land; ctx.stroke();
      ctx.lineWidth = 1.5; ctx.strokeStyle = text; ctx.stroke();
    } else {
      var fill = cssVar("--family-" + family + "-fill") || land;
      var rr = Math.max(r, 4.5);
      ctx.beginPath(); ctx.arc(c, c, rr, 0, Math.PI * 2);
      ctx.fillStyle = fill; ctx.fill();
      ctx.lineWidth = 1.4; ctx.strokeStyle = text; ctx.stroke();
      if (rr >= 6) drawFamilyGlyph(ctx, family, (rr * 1.4) / 12, c - rr * 0.7, c - rr * 0.7, text);
    }
  }
  function addMarkImages() {
    FAMILIES.forEach(function (family) {
      MARK_STEP_AT.forEach(function (n, step) {
        var r = markRadius(n);
        var size = Math.ceil(2 * r + 4);
        var img = familyImage(size, function (ctx) { drawPlaceMark(ctx, family, r, size / 2); });
        map.addImage("mark-" + family + "-" + step, img.image, { pixelRatio: img.ratio });
      });
    });
  }
  function addFamilyImages() {
    FAMILIES.forEach(function (family) {
      var text = colors[family];
      var fill = cssVar("--family-" + family + "-fill") || mapColors.land;
      var marker = familyImage(MARKER_PX, function (ctx) { drawFamilyMarker(ctx, family, text, fill); });
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
    // Line width by zoom (docs/31 §1.6; docs/31-brand-identity.md M-2): 0.75px at the country view,
    // 1.5px once counties resolve, faint context under the projects; the casing beneath is the land
    // colour, as faint, so a line keeps an edge over region fills and basemap roads.
    var lineWidth = ["interpolate", ["linear"], ["zoom"], 3, 0.75, 8, 1, 11, 1.5];
    var casingWidth = ["interpolate", ["linear"], ["zoom"], 3, 1.75, 8, 2.5, 11, 3.5];
    var isPowerPlant = ["==", ["get", "asset_type"], "power_plant"];

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
        "circle-stroke-opacity": CTX_OPACITY
      }
    }, "clusters");
    // "Noto Sans Medium", not "Bold": once the pmtiles basemap points `glyphs` at the real
    // Protomaps assets host, a font name that host doesn't carry 404s and the label silently
    // never renders -- confirmed against the real host, which hosts only Regular/Medium/Italic.
    map.addLayer({
      id: "plant-cluster-count", type: "symbol", source: "plants",
      filter: isCluster,
      layout: { "text-field": ["get", "count"], "text-size": 10, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": ["get", "color"], "text-opacity": CTX_TEXT_OPACITY }
    }, "clusters");
    map.addLayer({
      id: "asset-lines-casing", type: "line", source: "plants",
      filter: isLine,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": mapColors.land, "line-width": casingWidth, "line-opacity": CTX_OPACITY }
    }, "clusters");
    // Interstate (and unclassified) pipelines solid, intrastate dashed: `line-dasharray` is not
    // data-driven in this MapLibre, hence two layers over the same source rather than one.
    var isTransmission = ["==", ["get", "asset_type"], "transmission_line"];
    map.addLayer({
      id: "asset-lines", type: "line", source: "plants",
      filter: ["all", isLine, ["!=", ["get", "line_class"], "intrastate"], ["!", isTransmission]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": CTX_OPACITY }
    }, "clusters");
    map.addLayer({
      id: "asset-lines-intrastate", type: "line", source: "plants",
      filter: ["all", isLine, ["==", ["get", "line_class"], "intrastate"]],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": CTX_OPACITY, "line-dasharray": [3, 2] }
    }, "clusters");
    // Transmission lines: dash-dot, same width ramp and casing as pipelines (docs/31 §1.6).
    map.addLayer({
      id: "asset-lines-transmission", type: "line", source: "plants",
      filter: ["all", isLine, isTransmission],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": lineWidth, "line-opacity": CTX_OPACITY, "line-dasharray": [4, 1.5, 1, 1.5] }
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
      paint: { "text-color": ["get", "color"], "text-opacity": CTX_TEXT_OPACITY, "text-halo-color": mapColors.land, "text-halo-width": 1.2 }
    }, "clusters");
    map.addLayer({
      id: "plant-points", type: "symbol", source: "plants",
      filter: isPoint,
      layout: {
        "icon-image": ["coalesce", ["get", "icon"], "asset-square"],
        "icon-size": [
          "interpolate", ["linear"], ["zoom"],
          3, ["case", isPowerPlant, 0.12, 0.3], 8, ["case", isPowerPlant, 0.22, 0.45], 12, ["case", isPowerPlant, 0.35, 0.6]
        ],
        "icon-allow-overlap": true
      },
      paint: { "icon-color": ["get", "color"], "icon-opacity": CTX_OPACITY }
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
      paint: { "text-color": ["get", "color"], "text-opacity": CTX_TEXT_OPACITY, "text-halo-color": mapColors.land, "text-halo-width": 1 }
    }, "clusters");

    // Clicks and taps on these layers are `onMapClick`'s (docs/17 M-2): a plant or a line opens the
    // asset drawer, a cluster zooms in. Hover stays per layer.
    map.on("mouseenter", "plant-points", function (e) { map.getCanvas().style.cursor = "pointer"; showAssetTooltip(e, e.features[0].geometry.coordinates); });
    map.on("mouseleave", "plant-points", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("mouseenter", "asset-lines-hit", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mousemove", "asset-lines-hit", function (e) { showAssetTooltip(e, e.lngLat); });
    map.on("mouseleave", "asset-lines-hit", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
  }

  var RETIRED_LAYER_IDS = ["retired-clusters", "retired-cluster-count", "retired-points", "retired-labels"];

  // Lane R1: its own source and layers, added after the existing-assets layers and still below
  // every proposals layer (same `addLayer(..., "clusters")` stacking rule as `addPlantsLayers`).
  // A retired-plants cluster is a DASHED ring on the land colour, never the solid ring a proposals
  // cluster draws (2026-10-07: at national zoom the two read alike, magenta against navy, which is
  // hue alone and close under deuteranopia -- D-5). Drawn at 2x like the family images; the
  // diameter steps (20/28/36 px) are `icon-size` 0.5/0.7/0.9 of this 40 px image.
  var RETIRED_CLUSTER_PX = 40;
  function addRetiredClusterImage() {
    var img = familyImage(RETIRED_CLUSTER_PX, function (ctx) {
      var c = RETIRED_CLUSTER_PX / 2;
      ctx.beginPath(); ctx.arc(c, c, c - 2.5, 0, Math.PI * 2);
      ctx.fillStyle = mapColors.land; ctx.globalAlpha = 0.9; ctx.fill(); ctx.globalAlpha = 1;
      ctx.lineWidth = 3.2; ctx.strokeStyle = retiredColors.retired;
      ctx.setLineDash([5.2, 3.4]);
      ctx.stroke();
    });
    map.addImage("retired-cluster", img.image, { pixelRatio: img.ratio });
  }
  function addRetiredLayers() {
    map.addImage("retired-crossed", buildShapeIcon("retired-crossed", 24), { sdf: true });
    map.addImage("retiring-dot", buildShapeIcon("retiring-dot", 24), { sdf: true });
    addRetiredClusterImage();
    map.addSource("retired-plants", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    var isCluster = ["any", ["==", ["get", "feature_kind"], "asset_cluster"], ["==", ["get", "feature_kind"], "plant_cluster"]];
    var isPoint = ["all", ["==", ["geometry-type"], "Point"], ["!", isCluster]];
    map.addLayer({
      id: "retired-clusters", type: "symbol", source: "retired-plants",
      filter: isCluster,
      layout: {
        "icon-image": "retired-cluster",
        "icon-size": ["step", ["get", "count"], 0.5, 10, 0.7, 50, 0.9],
        "icon-allow-overlap": true, "icon-ignore-placement": true
      },
      paint: { "icon-opacity": CTX_OPACITY }
    }, "clusters");
    map.addLayer({
      id: "retired-cluster-count", type: "symbol", source: "retired-plants",
      filter: isCluster,
      layout: { "text-field": ["get", "count"], "text-size": 10, "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": ["get", "color"], "text-opacity": CTX_TEXT_OPACITY }
    }, "clusters");
    map.addLayer({
      id: "retired-points", type: "symbol", source: "retired-plants",
      filter: isPoint,
      layout: {
        "icon-image": ["coalesce", ["get", "icon"], "retired-crossed"],
        "icon-size": ["coalesce", ["get", "ring_size"], 0.6],
        "icon-allow-overlap": true
      },
      paint: { "icon-color": ["get", "color"], "icon-opacity": CTX_OPACITY }
    }, "clusters");
    map.addLayer({
      id: "retired-labels", type: "symbol", source: "retired-plants", minzoom: 8,
      filter: isPoint,
      layout: {
        "text-field": ["coalesce", ["get", "name"], ""], "text-size": 9, "text-font": ["Noto Sans Medium"],
        "text-anchor": "top", "text-offset": [0, 0.9], "text-allow-overlap": false
      },
      paint: { "text-color": ["get", "color"], "text-opacity": CTX_TEXT_OPACITY, "text-halo-color": mapColors.land, "text-halo-width": 1.2 }
    }, "clusters");
    // Clicks and taps are `onMapClick`'s (docs/17 M-2); hover stays per layer.
    map.on("mouseenter", "retired-points", function (e) { map.getCanvas().style.cursor = "pointer"; showAssetTooltip(e, e.features[0].geometry.coordinates); });
    map.on("mouseleave", "retired-points", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("mouseenter", "retired-clusters", function (e) { map.getCanvas().style.cursor = "pointer"; showRetiredClusterTooltip(e); });
    map.on("mouseleave", "retired-clusters", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
  }

  // Says what a dashed ring is without a click, as a proposals cluster's tooltip does (D-10).
  function showRetiredClusterTooltip(e) {
    if (!hoverAllowed()) return;
    var f = e.features[0];
    var p = f.properties;
    var mw = Number(p.capacity_mw_sum) || 0;
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(f.geometry.coordinates)
      .setHTML(
        "<strong>" + esc(plural(Number(p.count) || 0, "retired or retiring plant")) + "</strong>" +
        (mw ? "<br>Nameplate " + fmtMW(mw) : "") +
        "<br>Click to zoom in"
      )
      .addTo(map);
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

  // ---- one tap, one action (docs/17 M-2) ----
  // MapLibre runs a layer's click handler for every layer with a feature under the point. With one
  // handler per layer, a tap on a proposal cluster zoomed in and also opened the transmission line
  // beneath it, and a tap on a plant in a shaded county left the map for that county's list. One
  // handler now asks what lies under the click and acts on the first hit in this order, nearest
  // the point within a tier (a region tier picks its own way, `mostSpecificRegion`). A finger is
  // wider than a cursor, so for a touch the question covers a box 8px each side of the tap; for a
  // mouse it is the point itself, as before.
  var TAP_SLOP_PX = 8;
  var CLICK_ORDER = [
    { layers: ["points"], act: function (f) { openDrawer(f.properties); } },
    { layers: ["clusters"], act: openMark },
    { layers: ["plant-points", "plant-clusters"], act: openAssetOrZoom },
    { layers: ["retired-points", "retired-clusters"], act: openAssetOrZoom },
    { layers: ["asset-lines-hit"], act: function (f) { openAssetDrawer(f.properties); } },
    {
      layers: ["region-fill"], pick: mostSpecificRegion,
      act: function (f) { openRegionDrawer(f.properties, f.properties.name); }
    }
  ];
  // A state or country placement is a near-transparent wash over the whole area, so a county shaded
  // inside it lies beneath that wash, and the topmost polygon under a tap was the state's (lane
  // m-behaviour review: a tap in Calaveras County opened California). The most specific level wins:
  // county, then state, then country. Among regions of that level, one that holds the point itself
  // beats one that only meets the touch box; after that, the topmost.
  var REGION_LEVEL_RANK = { county: 0, state: 1, country: 2 };
  function regionRank(f) {
    var rank = REGION_LEVEL_RANK[f.properties.region_level];
    return rank == null ? 3 : rank;
  }
  function mostSpecificRegion(point, features, slop) {
    var rank = Math.min.apply(null, features.map(regionRank));
    var level = features.filter(function (f) { return regionRank(f) === rank; });
    if (level.length > 1 && slop) {
      var under = map.queryRenderedFeatures(point, { layers: ["region-fill"] })
        .filter(function (f) { return regionRank(f) === rank; });
      if (under.length) return under[0];
    }
    return level[0];
  }
  function isClusterKind(kind) { return kind === "cluster" || kind === "asset_cluster" || kind === "plant_cluster"; }
  // The cluster lands in the middle of the canvas the panel leaves (`fitPadding`), not under it: an
  // offset, not `padding`, which MapLibre would keep for every later move.
  function zoomIntoCluster(f) {
    var pad = fitPadding(0);
    map.easeTo({
      center: f.geometry.coordinates, zoom: f.properties.expands_to_zoom || (map.getZoom() + 2),
      offset: [(pad.left - pad.right) / 2, (pad.top - pad.bottom) / 2], duration: motionMs(600)
    });
  }
  // A place's mark: a county's (below zoom 6) opens the county's summary, as a tap on its shading
  // does from zoom 6; any other place's zooms in to it.
  function openMark(f) {
    if (f.properties.region) openRegionDrawer(f.properties, f.properties.name);
    else zoomIntoCluster(f);
  }
  function openAssetOrZoom(f) {
    if (isClusterKind(f.properties.feature_kind)) zoomIntoCluster(f);
    else openAssetDrawer(f.properties);
  }
  // The click's own pointer where the browser names it; else the page's primary pointer.
  function clickIsTouch(e) {
    var type = e.originalEvent && e.originalEvent.pointerType;
    if (type) return type === "touch";
    return isCoarsePointer();
  }
  function nearest(point, features) {
    var best = features[0];
    var bestDistance = Infinity;
    features.forEach(function (f) {
      if (!f.geometry || f.geometry.type !== "Point") return;
      var at = map.project(f.geometry.coordinates);
      var d = (at.x - point.x) * (at.x - point.x) + (at.y - point.y) * (at.y - point.y);
      if (d < bestDistance) { best = f; bestDistance = d; }
    });
    return best;
  }
  function onMapClick(e) {
    var layers = [];
    CLICK_ORDER.forEach(function (tier) {
      tier.layers.forEach(function (id) { if (map.getLayer(id)) layers.push(id); });
    });
    var slop = clickIsTouch(e) ? TAP_SLOP_PX : 0;
    var where = slop ? [[e.point.x - slop, e.point.y - slop], [e.point.x + slop, e.point.y + slop]] : e.point;
    var hits = map.queryRenderedFeatures(where, { layers: layers });
    for (var i = 0; i < CLICK_ORDER.length; i++) {
      var tier = CLICK_ORDER[i];
      var inTier = hits.filter(function (f) { return tier.layers.indexOf(f.layer.id) !== -1; });
      if (inTier.length) {
        var picked = tier.pick ? tier.pick(e.point, inTier, slop) : nearest(e.point, inTier);
        // Where the drawer's record is on the map, so the phone's bottom sheet can ease it into
        // view above itself (docs/17 M-4): a point's own position, else where the tap landed.
        drawerAnchor = picked.geometry && picked.geometry.type === "Point"
          ? picked.geometry.coordinates : [e.lngLat.lng, e.lngLat.lat];
        try { tier.act(picked); } finally { drawerAnchor = null; }
        return;
      }
    }
  }

  // ---- hover highlights (owner, 2026-10-22: "easier to understand") ----
  // Hovering a row of the key lights the marks it names and dims the rest; hovering a row of the
  // in-view list rings its mark. Mouse only: a touch never hovers, and the key's words and the
  // drawer say the same. The dimmed opacities are the drawn ones times HIGHLIGHT_DIM.
  // A tap on a phone is followed by the browser's emulated mouse events, so `mouseenter` lit a key
  // row's marks (or ringed a list row's) on a tap and, with no mouse to leave the row, left the map
  // dimmed (review 2026-10-22). Pointer events name their device: only a mouse's pointer lights
  // anything, and not within a moment of a touch (`hoverAllowed`, below).
  function mouseHover(e) { return !!e && e.pointerType === "mouse" && hoverAllowed(); }
  var HIGHLIGHT_DIM = 0.15;
  var HIGHLIGHT_PAINT = [
    ["clusters", "icon-opacity"],
    ["points", "icon-opacity"], ["points-precision", "circle-stroke-opacity"], ["point-labels", "text-opacity"],
    ["region-fill", "fill-opacity"], ["region-outline", "line-opacity"], ["region-outline-broad", "line-opacity"],
    ["region-labels", "text-opacity"],
    ["plant-clusters", "circle-stroke-opacity"], ["plant-cluster-count", "text-opacity"],
    ["asset-lines", "line-opacity"], ["asset-lines-intrastate", "line-opacity"], ["asset-lines-transmission", "line-opacity"],
    ["asset-line-labels", "text-opacity"], ["plant-points", "icon-opacity"], ["plant-labels", "text-opacity"],
    ["retired-clusters", "icon-opacity"], ["retired-cluster-count", "text-opacity"],
    ["retired-points", "icon-opacity"], ["retired-labels", "text-opacity"]
  ];
  var PROPOSAL_POINT_LAYERS = ["points", "points-precision", "point-labels"];
  // A place's marks, every shape in the one layer (`addMarkImages`).
  var PROPOSAL_CLUSTER_LAYERS = ["clusters"];
  var REGION_LAYERS = ["region-fill", "region-outline", "region-outline-broad", "region-labels"];
  var highlightBase = null;
  var highlightOn = "";
  // What a key row (`data-highlight="kind:value"`) lights on one layer: true (all of it), false (none)
  // or an expression picking its features.
  function highlightMatch(id, spec) {
    var cut = spec.indexOf(":");
    var kind = cut === -1 ? spec : spec.slice(0, cut);
    var value = cut === -1 ? "" : spec.slice(cut + 1);
    var has = function (key) { return [">", ["coalesce", ["get", key], 0], 0]; };
    var is = function (key, v) { return ["==", ["get", key], v]; };
    if (kind === "family") {
      // A place's mark is one family's already; a region's shading holds several.
      if (PROPOSAL_POINT_LAYERS.indexOf(id) !== -1 || PROPOSAL_CLUSTER_LAYERS.indexOf(id) !== -1) return is("family", value);
      if (REGION_LAYERS.indexOf(id) !== -1) return has("fam_" + value);
      return false;
    }
    if (kind === "clusters") return PROPOSAL_CLUSTER_LAYERS.indexOf(id) !== -1;
    if (kind === "precision") return id === "points-precision" || (id === "points" ? is("precision_reason", "licence") : false);
    if (kind === "regions") {
      if (PROPOSAL_CLUSTER_LAYERS.indexOf(id) !== -1) return ["==", ["coalesce", ["get", "region"], false], true];
      return REGION_LAYERS.indexOf(id) !== -1;
    }
    if (kind === "plant") {
      if (id === "plant-points" || id === "plant-labels") return ["all", is("asset_type", "power_plant"), is("plant_family", value)];
      if (id === "plant-clusters" || id === "plant-cluster-count") return is("plant_family", value);
      return false;
    }
    if (kind === "pipeline") {
      var intrastate = value === "intrastate";
      if (id === (intrastate ? "asset-lines-intrastate" : "asset-lines")) return is("asset_type", "gas_pipeline");
      if (id === "asset-line-labels") return ["all", is("asset_type", "gas_pipeline"), intrastate ? is("line_class", "intrastate") : ["!=", ["get", "line_class"], "intrastate"]];
      return false;
    }
    if (kind === "asset") {
      if (/^asset-lines|^asset-line-labels$|^plant-points$|^plant-labels$/.test(id)) return is("asset_type", value);
      if (id === "plant-clusters" || id === "plant-cluster-count") return is("dominant_asset_type", value);
      return false;
    }
    if (kind === "retired") {
      if (value === "cluster") return id === "retired-clusters" || id === "retired-cluster-count";
      if (id === "retired-points" || id === "retired-labels") return is("status", value);
      return false;
    }
    return true;
  }
  function dimmed(base) {
    if (typeof base === "number") return base * HIGHLIGHT_DIM;
    if (Array.isArray(base)) return ["*", base, HIGHLIGHT_DIM];
    return HIGHLIGHT_DIM;
  }
  // `spec` null restores every layer as drawn.
  function setHighlight(spec) {
    if (!map.getLayer("points")) return;
    if (!highlightBase) {
      highlightBase = {};
      HIGHLIGHT_PAINT.forEach(function (lp) {
        if (map.getLayer(lp[0])) highlightBase[lp[0] + "|" + lp[1]] = map.getPaintProperty(lp[0], lp[1]);
      });
    }
    if ((spec || "") === highlightOn) return;
    highlightOn = spec || "";
    HIGHLIGHT_PAINT.forEach(function (lp) {
      var id = lp[0];
      if (!map.getLayer(id)) return;
      var base = highlightBase[id + "|" + lp[1]];
      var value = base;
      if (spec) {
        var full = base == null ? 1 : base;
        var match = highlightMatch(id, spec);
        value = match === true ? full : match === false ? dimmed(full) : ["case", match, full, dimmed(full)];
      }
      map.setPaintProperty(id, lp[1], value);
    });
  }
  if (mapKey) {
    Array.prototype.forEach.call(mapKey.querySelectorAll("[data-highlight]"), function (row) {
      row.addEventListener("pointerenter", function (e) { if (mouseHover(e)) setHighlight(row.getAttribute("data-highlight")); });
      row.addEventListener("pointerleave", function () { setHighlight(null); });
    });
    mapKey.addEventListener("pointerleave", function () { setHighlight(null); });
  }
  // The ring round a list row's mark (a point's ring, a line's or a region's outline drawn over it).
  function showHoverRing(geometry) {
    var source = map.getSource && map.getSource("hover-ring");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: geometry ? [{ type: "Feature", geometry: geometry, properties: {} }] : []
    });
  }
  function bindRowHover(row, geometry) {
    if (!row || !geometry) return;
    row.addEventListener("pointerenter", function (e) { if (mouseHover(e)) showHoverRing(geometry); });
    row.addEventListener("pointerleave", function () { showHoverRing(null); });
  }
  function addHoverRingLayers() {
    var accent = cssVar("--link") || "#0b1023";
    map.addSource("hover-ring", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "hover-ring-halo", type: "circle", source: "hover-ring", filter: ["==", ["geometry-type"], "Point"],
      paint: { "circle-radius": 15, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 6, "circle-stroke-color": mapColors.land, "circle-stroke-opacity": 0.9 }
    });
    map.addLayer({
      id: "hover-ring", type: "circle", source: "hover-ring", filter: ["==", ["geometry-type"], "Point"],
      paint: { "circle-radius": 15, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 2.5, "circle-stroke-color": accent }
    });
    map.addLayer({
      id: "hover-outline", type: "line", source: "hover-ring", filter: ["!=", ["geometry-type"], "Point"],
      paint: { "line-color": accent, "line-width": 3, "line-opacity": 0.9 }
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

    // A place's projects (owner, 2026-10-24: "every active project a bright dot"): one mark per status
    // family (`familyMarks`), in the shapes of M-1 (in process a disc in --dot, contracted a square,
    // announced a ring), sized by area with the family's count and never numbered. One symbol layer
    // holds every shape so the larger mark is drawn first and a smaller one at the same place stays on
    // top of it (`symbol-sort-key`); three circle layers could only stack by family.
    addMarkImages();
    map.addLayer({
      id: "clusters", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "mark"],
      layout: {
        "icon-image": ["concat", "mark-", ["get", "family"], "-", ["to-string", markStepExpr()]],
        // A phone's national view (about zoom 2) holds the lower 48 in 370px: its marks are drawn at
        // 60%, reaching full size by zoom 3.5, where a desktop's national view starts.
        "icon-size": ["interpolate", ["linear"], ["zoom"], 2, 0.6, 3.5, 1],
        "icon-allow-overlap": true, "icon-ignore-placement": true,
        "symbol-sort-key": ["-", 0, ["get", "mark_count"]]
      },
      paint: { "icon-opacity": 1 }
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
    // Licence-limited precision (D-9) keeps its own mark: a second, wider ring round the chip.
    map.addLayer({
      id: "points-precision", type: "circle", source: "proposals",
      filter: ["all", ["==", ["get", "feature_kind"], "proposal"], ["==", ["get", "precision_reason"], "licence"]],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 8.5, 8, 9.5, 12, 11.5],
        "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 1, "circle-stroke-color": colorExpr
      }
    });
    map.addLayer({
      id: "points", type: "symbol", source: "proposals",
      filter: ["==", ["get", "feature_kind"], "proposal"],
      layout: {
        "icon-image": ["concat", "family-marker-", ["get", "family"]],
        "icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.85, 8, 1, 12, 1.2],
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
      paint: { "text-color": cssVar("--text") || "#0b1023", "text-halo-color": mapColors.land, "text-halo-width": 1.4 }
    });

    // Over everything, never a click target (`onMapClick` does not ask for it).
    addHoverRingLayers();

    map.on("mouseenter", "clusters", function (e) { map.getCanvas().style.cursor = "pointer"; showClusterTooltip(e); });
    map.on("mouseleave", "clusters", function () { map.getCanvas().style.cursor = ""; hideTooltip(); });
    map.on("click", onMapClick);
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
  // Hover cards are for a mouse. A tap on a phone is followed by the browser's emulated mouse
  // events, so a tap on a transmission line opened its card, and with no mouse to leave the line it
  // stayed on screen over the map (owner report 2026-10-22). No card opens within a moment of a
  // touch, and the next touch anywhere on the page, or the map moving, closes the one that shows.
  // The drawer a tap opens carries the same words.
  var TOUCH_GHOST_MS = 800;
  var lastTouchAt = 0;
  function hoverAllowed() { return Date.now() - lastTouchAt > TOUCH_GHOST_MS; }
  document.addEventListener("touchstart", function () {
    lastTouchAt = Date.now();
    hideTooltip();
  }, { passive: true, capture: true });
  document.addEventListener("touchend", function () { lastTouchAt = Date.now(); }, { passive: true, capture: true });
  map.on("movestart", function () { hideTooltip(); });
  function showClusterTooltip(e) {
    if (!hoverAllowed()) return;
    var f = e.features[0];
    var p = f.properties;
    var counts = propObj(p.lifecycle_state_counts) || {};
    var lines = Object.keys(counts).sort().map(function (k) { return esc(lifecycleName(k)) + " " + fmtCount(counts[k]); });
    hideTooltip();
    tooltip = new maplibregl.Popup({ closeButton: false, closeOnClick: false })
      .setLngLat(f.geometry.coordinates)
      .setHTML(
        (p.region && p.name ? esc(p.name) + "<br>" : "") +
        "<strong>" + esc(plural(Number(p.count) || 0, "proposal")) + "</strong><br>" + lines.join(" &middot; ") +
        (p.capacity_mw_sum ? "<br>Capacity sum: " + fmtMW(p.capacity_mw_sum) : "")
      )
      .addTo(map);
  }
  // Hover on any existing asset: name, type (with the RNG technology family) and operator
  // (task: "hover shows name and operator").
  function showAssetTooltip(e, lngLat) {
    if (!hoverAllowed()) return;
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
  // The fragment holds both parts of the notice (`_map_notice.html`), each in a `data-notice-part`
  // wrapper: "under" goes under the floating bar, "sheet" to the head of the filter sheet.
  var noticeEl = document.getElementById("map-notice");
  var noticeUnderEl = document.getElementById("map-notice-under");
  var noticeSeq = 0;
  function noticePart(holder, name) {
    var part = holder.querySelector('[data-notice-part="' + name + '"]');
    return part ? part.innerHTML : "";
  }
  function refreshNotice() {
    if (!noticeEl) return;
    var seq = ++noticeSeq;
    fetch(noticeUrl(filters))
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        if (seq !== noticeSeq) return;
        var holder = document.createElement("div");
        holder.innerHTML = html;
        noticeEl.innerHTML = noticePart(holder, "sheet");
        if (noticeUnderEl) noticeUnderEl.innerHTML = noticePart(holder, "under");
        // A notice the reader opened with "Why?" below 720px stays open (docs/17 M-6); the new
        // button says so.
        var why = noticeEl.querySelector(".map-notice__why");
        if (why) why.setAttribute("aria-expanded", noticeEl.classList.contains("is-why-open") ? "true" : "false");
      })
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
      statuses: currentStatuses(),
      unlisted: unlistedBox ? unlistedBox.checked : false,
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
    syncStatusControl(filters.statuses);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(plantsToggle.checked);
    // A new set of filters is a new question: the key's rows follow the statuses ticked, and the
    // ranked bars start their order afresh (S-5 keeps it only within one set of filters).
    syncFamilyRows();
    resetRankedOrder();
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
  statusBoxes.forEach(function (box) { box.addEventListener("change", applyFilters); });
  if (unlistedBox) unlistedBox.addEventListener("change", applyFilters);
  // The notice's "N built" link turns Built on in place rather than reloading the map (UX-1), and
  // its "Show them" link (records no register lists any more) ticks that box the same way.
  [noticeEl, noticeUnderEl].forEach(function (holder) {
    if (!holder) return;
    holder.addEventListener("click", function (e) {
      var target = e.target && e.target.closest ? e.target : null;
      var link = target ? target.closest("a.view-notice__show") : null;
      var builtBox = document.getElementById("mf-status-built");
      if (link && builtBox) {
        e.preventDefault();
        builtBox.checked = true;
        applyFilters();
        return;
      }
      var show = target ? target.closest("a[data-unlisted-show]") : null;
      if (show && unlistedBox) {
        e.preventDefault();
        unlistedBox.checked = true;
        applyFilters();
        return;
      }
      // "No status chosen ... Clear all" runs "Clear all" in place (owner, 2026-10-22).
      var clear = target ? target.closest("a[data-clear-all]") : null;
      if (clear) {
        e.preventDefault();
        document.querySelector("#mf-clear").click();
      }
    });
  });
  plantsToggle.addEventListener("change", function () {
    var on = plantsToggle.checked;
    setLayerOn("plants", on);
    writeFilters(filters);
    syncAssetControls();
    if (map.getLayer("plant-points")) setPlantsLayerVisible(on);
    else { plantsLegend.hidden = !on; plantsLegend.setAttribute("aria-hidden", on ? "false" : "true"); }
    sendUiEvent("map.layer_toggled", { layer: "plants", on: on });
    if (on) refetchAssets();
    scheduleRender();
  });
  retiredToggle.addEventListener("change", function () {
    var on = retiredToggle.checked;
    setLayerOn("retired", on);
    writeFilters(filters);
    if (map.getLayer("retired-points")) setRetiredLayerVisible(on);
    else { retiredLegend.hidden = !on; retiredLegend.setAttribute("aria-hidden", on ? "false" : "true"); }
    sendUiEvent("map.layer_toggled", { layer: "retired", on: on });
    if (on) refetchRetired();
    scheduleRender();
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
    if (unlistedBox) unlistedBox.checked = false;
    syncStatusControl(DEFAULT_STATES);
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
  // Where the record the drawer opens for lies on the map: set by `onMapClick` and
  // `openFocusedProposal` for the length of the call, null when a row of the in-view list opens it
  // (the reader is reading the list, and a move would redraw the row focus returns to).
  var drawerAnchor = null;
  // Focus goes back on close to what had it on open, unless that was in the drawer itself (a tap on
  // the map while the drawer is open shows the next record in it).
  function rememberFocus() {
    var active = document.activeElement;
    if (!active || !drawer.el.contains(active)) lastFocused = active;
  }
  // docs/17 M-4: below 720px the drawer is a bottom sheet over the lower part of the screen, and the
  // feature tapped may lie under it. The map eases it to the middle of the map left showing above
  // the sheet, keeping its distance from the left edge. It moves only when the feature is not
  // already well inside that band (no move, no refetch) and not at all when under 64px of map
  // shows. 600ms (--motion-map), instant under reduced motion. From 720px nothing moves, as before.
  function revealAboveSheet(lngLat, sheetHeight) {
    if (!narrowKey || !narrowKey.matches || !lngLat) return;
    var r = mapEl.getBoundingClientRect();
    // On the landing the floating bar lies over the canvas's top (docs/30 §11.8): the part of the map
    // left showing starts under it.
    var floatBar = document.querySelector(".map-float");
    var fb = floatBar ? floatBar.getBoundingClientRect() : null;
    var top = Math.max(r.top, topBarBottom(false), fb && fb.height > 0 ? fb.bottom : 0);
    var bottom = Math.min(r.bottom, window.innerHeight - sheetHeight);
    if (bottom - top < 64) return;
    var p = map.project(lngLat);
    var margin = 24;
    var y = r.top + p.y;
    var xInside = p.x >= margin && p.x <= r.width - margin;
    if (xInside && y >= top + margin && y <= bottom - margin) return;
    var x = xInside ? p.x : r.width / 2;
    map.easeTo({
      center: lngLat,
      offset: [x - r.width / 2, (top + bottom) / 2 - r.top - r.height / 2],
      duration: motionMs(600)
    });
  }
  function openDrawer(rawProps) {
    rememberFocus();
    var provenance = propObj(rawProps.provenance) || [];
    drawer.render(rawProps, provenance[0] || null);
    drawer.open(drawerAnchor);
  }
  // Point assets other than power plants render at once from the feature, then again with the
  // asset's detail row merged in (`/api/assets/{public_id}`, web/app.py): geo point features
  // carry no capacity_value/unit or attributes, and an ethanol plant's nameplate or a landfill
  // project's LFG flow lives there. A failed fetch leaves the first render standing.
  var drawerDetailSeq = 0;
  var assetDetailCache = {};
  function openAssetDrawer(rawProps) {
    rememberFocus();
    drawer.renderAsset(rawProps);
    drawer.open(drawerAnchor);
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
      drawer.refit();
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
  // A region's summary (docs/17 M-2): its name, how many proposals the source places there and how,
  // their statuses and stated capacity, and its list. From a tap on the region, or the Details
  // button on its in-view row (the keyboard path, D-14).
  function openRegionDrawer(rawProps, name) {
    rememberFocus();
    drawer.renderRegion(rawProps, name || regionName(rawProps));
    drawer.open(drawerAnchor);
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
    // docs/17 M-4: below 720px the drawer is a bottom sheet (styles.css 8.3) and "Expand" takes it
    // to the full height and back. From 720px it opens in the panel's place over the map (owner,
    // 2026-10-22: the drawer and the panel never overlap), "Expand" is not drawn, and "Back to what's
    // in view" closes it as Close does; the panel is then as the reader left it (tab, scroll, open or
    // collapsed). Close stays where focus lands on open.
    el.innerHTML = '<div class="map-drawer__bar">' +
      '<button type="button" id="drawer-back" class="map-drawer__back">&larr; Back to what&rsquo;s in view</button>' +
      '<button type="button" id="drawer-expand" class="map-drawer__expand" aria-controls="map-drawer" aria-expanded="false">Expand</button>' +
      '<button type="button" id="drawer-close" class="map-drawer__close" aria-label="Close">&times; Close</button>' +
      '</div><div id="drawer-body"></div>';
    (document.getElementById("map-frame") || document.body).appendChild(el);
    var body = el.querySelector("#drawer-body");
    var closeBtn = el.querySelector("#drawer-close");
    var expandBtn = el.querySelector("#drawer-expand");
    var backBtn = el.querySelector("#drawer-back");
    closeBtn.addEventListener("click", close);
    backBtn.addEventListener("click", close);
    // Its state is `aria-expanded` as well as its words (review 2026-10-22).
    function setExpanded(on) {
      el.classList.toggle("is-expanded", on);
      expandBtn.textContent = on ? "Collapse" : "Expand";
      expandBtn.setAttribute("aria-expanded", on ? "true" : "false");
    }
    // "Expand" is drawn only when it shows more: a proposal's Technology row, or rows the sheet cuts
    // off. On a region summary or an asset whose rows fit, it would only make the sheet full height.
    // Measured while the drawer is open (it has no size while `hidden`).
    function syncExpand() {
      if (el.classList.contains("is-expanded")) { expandBtn.hidden = false; return; }
      var more = !!body.querySelector(".drawer-fields__row--more") || el.scrollHeight > el.clientHeight + 1;
      if (!more && document.activeElement === expandBtn) closeBtn.focus();
      expandBtn.hidden = !more;
    }
    expandBtn.addEventListener("click", function () { setExpanded(!el.classList.contains("is-expanded")); });
    el.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
      if (e.key === "Tab") {
        // Only what is drawn: "Expand" is in the markup at every width but drawn below 720px only.
        var focusables = Array.prototype.filter.call(el.querySelectorAll("button, a[href]"), function (node) {
          return node.getClientRects().length > 0;
        });
        var first = focusables[0], last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    // Open/close duration comes from CSS (docs/31 §1.5 --motion-base/--motion-fast): adding
    // `is-open` transitions in at 200ms, removing it transitions out at 150ms (the base rule's own
    // duration) -- see the .map-drawer / .map-drawer.is-open rule in styles.css.
    var hideTimer = null;
    var openAnchor = null;
    // `anchor`: where the record lies on the map, when a tap on the map opened it (docs/17 M-4).
    function open(anchor) {
      window.clearTimeout(hideTimer);
      setExpanded(false);
      el.hidden = false;
      el.removeAttribute("inert");
      el.setAttribute("aria-modal", "true");
      el.scrollTop = 0;
      // From 720px the drawer takes the panel's place (styles.css hides a covered panel there).
      if (panel) panel.classList.add("is-covered");
      setHighlight(null);
      void el.offsetWidth; // start the slide-in from the closed position, not from `hidden`
      el.classList.add("is-open");
      syncExpand();
      closeBtn.focus();
      openAnchor = anchor || null;
      if (anchor) revealAboveSheet(anchor, el.offsetHeight);
    }
    // A point asset's drawer is drawn again once its detail row lands (`openAssetDrawer`), and the
    // sheet can grow then (up to 45% of the screen) over the feature it just eased into view: so the
    // feature is eased above the new height, and "Expand" follows what the sheet now holds.
    function refit() {
      if (el.hidden || !el.classList.contains("is-open")) return;
      syncExpand();
      if (openAnchor && !el.classList.contains("is-expanded")) revealAboveSheet(openAnchor, el.offsetHeight);
    }
    function close() {
      if (el.hidden) return;
      openAnchor = null;
      el.classList.remove("is-open");
      el.removeAttribute("aria-modal");
      // Inert at once (nothing in it can take focus while it slides out), hidden once the 150ms
      // close transition has run; instant under reduced motion.
      el.setAttribute("inert", "");
      hideTimer = window.setTimeout(function () { el.hidden = true; }, motionMs(160));
      if (panel) panel.classList.remove("is-covered");
      // Without scrolling: the panel comes back scrolled as the reader left it.
      if (lastFocused && document.contains(lastFocused)) lastFocused.focus({ preventScroll: true });
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
        " &middot; retrieved <span class=\"tnum\">" + esc(source.retrieved_at ? fmtDate(source.retrieved_at) : "unknown") + "</span>" +
        (source.attribution_text ? "<br><span class=\"drawer-source__credit\">" + esc(source.attribution_text) + "</span>" : "") + "</p>";
    }
    function render(p, source) {
      el.setAttribute("aria-label", "Record detail");
      body.innerHTML =
        "<h2>" + esc(p.name) + "</h2>" + chipHtml(familyOf(p.lifecycle_state), p.lifecycle_state) +
        "<dl class=\"drawer-fields\">" +
        // On a phone's bottom sheet this row waits for "Expand" (docs/17 M-4: name, status,
        // capacity, place, source and the record link come first).
        "<div class=\"drawer-fields__row drawer-fields__row--more\"><dt>Technology</dt><dd>" + esc(technologyName(p.technology) || "—") + "</dd></div>" +
        // A kind that cannot carry a capacity (PROPOSAL_FIELDS_NOT_APPLICABLE) and a large load with no
        // MW (lane L11) get no Capacity row rather than a row of "—".
        (!fieldApplies(p.kind, "capacity_mw", p.capacity_mw) ? "" :
          p.technology === "load" && !p.capacity_mw ? "" :
          "<div class=\"drawer-fields__row\"><dt>Capacity</dt><dd class=\"tnum\">" + (p.capacity_mw ? fmtMW(p.capacity_mw) : "—") + "</dd></div>") +
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
      el.setAttribute("aria-label", "Record detail");
      var type = assetTypeOf(p);
      var isLine = p.feature_kind === "asset_line";
      var techs = propObj(p.technologies) || {};
      var techRows = Object.keys(techs).sort().map(function (k) {
        return row(technologyName(k), fmtMW(techs[k]), true);
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
          row("Capacity", p.capacity_mw ? fmtMW(p.capacity_mw) : null, true) +
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
    // A region is many records, each with its own source: the summary says where those are rather
    // than naming one (D-2), and leads to the list, where every row carries its source.
    function renderRegion(p, name) {
      // Named for what it holds: a summary of many records, not one record's detail.
      el.setAttribute("aria-label", "Region summary");
      var how = REGION_PLACED_BY[p.region_level] || "by area";
      var parts = breakdownParts(propObj(p.lifecycle_state_counts) || {}, Number(p.capacity_mw_sum) || 0);
      var jump = regionJumpButton(p);
      body.innerHTML =
        "<h2>" + esc(name) + "</h2>" +
        "<p class=\"drawer-region\"><strong class=\"tnum\">" + esc(plural(Number(p.count) || 0, "proposal")) + "</strong> the source places " +
        esc(how) + ", not at a site." + (parts.length ? "<br>" + parts.join(" &middot; ") : "") + "</p>" +
        "<p class=\"drawer-source\"><span class=\"drawer-source__label\">Sources</span>" +
        "Each proposal’s source, licence and retrieved date are on the list and on its record.</p>" +
        (jump ? "<button type=\"button\" class=\"button-secondary drawer-region__jump\">Show " + esc(jump.textContent.trim()) + " on the map</button><br>" : "") +
        "<a class=\"drawer-open-link\" href=\"" + safeUrl(regionListUrl(p)) + "\">Open the list &rarr;</a>";
      var jumpBtn = body.querySelector(".drawer-region__jump");
      if (jumpBtn) jumpBtn.addEventListener("click", function () { close(); jump.click(); });
    }
    return {
      el: el, open: open, close: close, refit: refit,
      render: render, renderAsset: renderAsset, renderPlant: renderAsset, renderRegion: renderRegion
    };
  }

  // ---- the search field (owner, 2026-10-22; web/map_search.py) ----
  // One box on the map finds a project, a company or a place and moves the map to it. Suggestions
  // come as the reader types, after two characters and a 250ms pause, from `/api/map/search` (the
  // site's own search API, and the state and county outlines the map already shades: never a
  // third-party geocoder) and from the page's own region quick views. A tap, a click or Enter picks
  // one. A project opens its drawer and the map flies to it, with a ring round it whether or not
  // the current filters draw it; a company's projects that the filters draw are fitted, or its page
  // opens when none is; a place is fitted. Escape or the clear button empties the box and lifts the
  // ring. Without scripts the box is the site search (`/search`).
  var searchForm = document.getElementById("map-search");
  var searchInput = document.getElementById("map-search-input");
  var searchList = document.getElementById("map-search-list");
  var searchClear = document.getElementById("map-search-clear");
  var searchEmpty = document.getElementById("map-search-empty");
  var searchCount = document.getElementById("map-search-count");
  var SEARCH_MIN = 2;
  var SEARCH_PAUSE_MS = 250;
  var searchTimer = null;
  var searchSeq = 0;
  var searchAbort = null;
  var searchOptions = [];
  var searchActive = -1;
  var searchShownFor = "";
  var searchPendingPick = false;
  var pendingPin = null;
  function searchWords(text) {
    return String(text || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").match(/[a-z0-9]+/g) || [];
  }
  // The quick views the page offers ("United States", "United Kingdom", "Europe"), by name or code.
  var REGION_ALIASES = { us: ["usa", "america"], gb: ["uk", "britain"] };
  var searchRegions = Array.prototype.map.call(document.querySelectorAll(".region-btn"), function (btn) {
    var code = btn.getAttribute("data-region") || "";
    var codes = [code].concat(REGION_ALIASES[code] || []);
    return {
      kind: "region", label: btn.textContent.trim(), detail: "Region", button: btn, codes: codes,
      words: searchWords(btn.textContent).concat(codes)
    };
  });
  // How well a quick view answers what was typed, on the server's scale (`web/map_search.py`
  // `word_rank`): 0 its whole name, code or alias ("uk"), 1 its first words, 2 its first letters, 3 a word.
  // (Not `regionRank`: that name is the map's own, for which region level a tap lands on.)
  function quickViewRank(r, tokens) {
    var name = searchWords(r.label).join(" ");
    var typed = tokens.join(" ");
    if (name === typed || (tokens.length === 1 && r.codes.indexOf(tokens[0]) !== -1)) return 0;
    if ((name + " ").indexOf(typed + " ") === 0) return 1;
    if (name.indexOf(typed) === 0) return 2;
    return 3;
  }
  function regionMatches(query) {
    var tokens = searchWords(query);
    if (!tokens.length) return [];
    return searchRegions.filter(function (r) {
      return tokens.every(function (t) { return r.words.some(function (w) { return w.indexOf(t) === 0; }); });
    }).map(function (r) {
      return { kind: "region", label: r.label, detail: r.detail, button: r.button, rank: quickViewRank(r, tokens) };
    });
  }

  function closeList() {
    if (!searchList) return;
    searchList.hidden = true;
    searchInput.setAttribute("aria-expanded", "false");
    searchInput.removeAttribute("aria-activedescendant");
    searchActive = -1;
  }
  function openList() {
    if (!searchOptions.length) return;
    searchList.hidden = false;
    searchInput.setAttribute("aria-expanded", "true");
  }
  function hideEmpty() { if (searchEmpty) { searchEmpty.hidden = true; searchEmpty.textContent = ""; } }
  function showEmpty(query, failed) {
    if (!searchEmpty) return;
    searchEmpty.innerHTML = "";
    searchEmpty.appendChild(document.createTextNode(failed
      ? "Search is not answering just now. "
      : "Nothing on the map matches “" + query + "”. "));
    var a = document.createElement("a");
    a.href = "/search?q=" + encodeURIComponent(query);
    a.textContent = "Search the whole site";
    searchEmpty.appendChild(a);
    searchEmpty.hidden = false;
  }
  function syncClear() { if (searchClear) searchClear.hidden = !searchInput.value; }
  function setActive(index) {
    var nodes = searchList.querySelectorAll("[role='option']");
    Array.prototype.forEach.call(nodes, function (node, i) {
      node.setAttribute("aria-selected", i === index ? "true" : "false");
      node.classList.toggle("is-active", i === index);
    });
    searchActive = index;
    if (index >= 0 && nodes[index]) {
      searchInput.setAttribute("aria-activedescendant", nodes[index].id);
      nodes[index].scrollIntoView({ block: "nearest" });
    } else {
      searchInput.removeAttribute("aria-activedescendant");
    }
  }
  function optionNode(opt, index) {
    var node = document.createElement("div");
    node.className = "map-search__option";
    node.id = "map-search-option-" + index;
    node.setAttribute("role", "option");
    node.setAttribute("aria-selected", "false");
    node.setAttribute("data-index", String(index));
    // One list, every row saying what it is in the same place (lane stacked-lists, 2026-10-24; owner: "it's
    // companies and it's locations ... in different stacked lists are really confusing"): the name, then
    // its kind in words at the row's right end, read with it as "Pecos County, TX, County".
    var top = document.createElement("span");
    top.className = "map-search__top";
    var label = document.createElement("span");
    label.className = "map-search__label";
    label.textContent = opt.label;
    top.appendChild(label);
    var kind = document.createElement("span");
    kind.className = "map-search__kind";
    kind.innerHTML = "<span class=\"visually-hidden\">, </span>";
    kind.appendChild(document.createTextNode(searchKindWord(opt)));
    top.appendChild(kind);
    node.appendChild(top);
    // A project's status and facts, as the chips everywhere else print it (glyph and words, never hue
    // alone). A place or a company needs no second line: its kind is on the first.
    if (opt.kind === "project") {
      var detail = document.createElement("span");
      detail.className = "map-search__detail";
      if (opt.item.feature) detail.innerHTML = chipHtml(opt.item.family || familyOf(opt.item.feature.lifecycle_state), opt.item.feature.lifecycle_state) + " ";
      detail.appendChild(document.createTextNode(opt.detail || ""));
      node.appendChild(detail);
    }
    return node;
  }
  // The kind of a suggestion in a word or two: a place's own noun as the server names it ("State",
  // "County", "Parish", "Borough", "Census area", "City").
  function searchKindWord(opt) {
    if (opt.kind === "project") return "Project";
    if (opt.kind === "company") return "Company";
    if (opt.kind === "region") return "Region";
    if (opt.item && opt.item.noun) return String(opt.item.noun);
    return opt.kind === "state" ? "State" : "County";
  }
  var SEARCH_KIND_NOUNS = { place: ["place", "places"], company: ["company", "companies"], project: ["project", "projects"] };
  function renderSuggestions(query, options) {
    searchList.innerHTML = "";
    searchOptions = [];
    options.forEach(function (opt) {
      searchList.appendChild(optionNode(opt, searchOptions.length));
      searchOptions.push(opt);
    });
    // How many, and of which kinds, for a screen reader: the list itself has no headings to count.
    if (searchCount) {
      var tally = { place: 0, company: 0, project: 0 };
      options.forEach(function (o) { tally[o.kind === "company" || o.kind === "project" ? o.kind : "place"] += 1; });
      var parts = Object.keys(tally).filter(function (k) { return tally[k]; }).map(function (k) {
        return tally[k] + " " + SEARCH_KIND_NOUNS[k][tally[k] === 1 ? 0 : 1];
      });
      // Naming what was typed makes each answer new text, so a live region reads it even when the
      // count is the same as the last search's.
      searchCount.textContent = options.length
        ? options.length + " suggestion" + (options.length === 1 ? "" : "s") + " for “" + query + "”: " + parts.join(", ") + "."
        : "";
    }
    setActive(-1);
    if (searchOptions.length) openList(); else closeList();
  }
  // One ranked list (lane stacked-lists, fix round) in the server's own order: every suggestion carries
  // its `rank` on one scale for every kind (`web/map_search.py` `word_rank`: a whole name or a state's
  // code, then a name that begins with the words typed, then with the letters, then a word of it, then
  // the rest). Within a rank, places, then companies, then projects, each as the search answered. So
  // "ny" lists New York above "NY CDG 1 LLC" and both above Nye County, and Enter takes the first.
  var RANK_UNKNOWN = 4;
  function rankSuggestions(groups) {
    return [].concat.apply([], groups).map(function (o, i) {
      return [typeof o.rank === "number" ? o.rank : RANK_UNKNOWN, i, o];
    }).sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; }).map(function (row) { return row[2]; });
  }
  function showResults(query, body, failed) {
    var places = regionMatches(query).concat((body.places || []).map(function (p) {
      return { kind: p.kind, label: p.label, detail: p.detail, item: p, rank: p.rank };
    }));
    var companies = (body.companies || []).map(function (c) { return { kind: "company", label: c.label, detail: c.detail, item: c, rank: c.rank }; });
    var projects = (body.projects || []).map(function (p) { return { kind: "project", label: p.label, detail: p.detail, item: p, rank: p.rank }; });
    renderSuggestions(query, rankSuggestions([places, companies, projects]));
    searchShownFor = query;
    if (searchOptions.length) hideEmpty(); else showEmpty(query, failed);
    if (searchPendingPick) {
      searchPendingPick = false;
      if (searchOptions.length) pick(searchOptions[0]);
    }
  }
  function runSearch() {
    window.clearTimeout(searchTimer);
    var query = searchInput.value.trim();
    var seq = ++searchSeq;
    if (searchAbort) searchAbort.abort();
    searchAbort = typeof AbortController === "function" ? new AbortController() : null;
    fetch("/api/map/search?q=" + encodeURIComponent(query), searchAbort ? { signal: searchAbort.signal } : undefined)
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("search " + r.status)); })
      .then(function (body) { if (seq === searchSeq) showResults(query, body || {}, false); })
      .catch(function (err) {
        if ((err && err.name === "AbortError") || seq !== searchSeq) return;
        showResults(query, {}, true); // the quick views still match
      });
  }
  function clearSearch() {
    window.clearTimeout(searchTimer);
    if (searchAbort) searchAbort.abort();
    searchSeq++;
    searchPendingPick = false;
    searchInput.value = "";
    searchOptions = [];
    searchShownFor = "";
    closeList();
    hideEmpty();
    if (searchCount) searchCount.textContent = "";
    setSearchPin(null);
    syncClear();
  }

  // What to keep clear of: from 720px the panel, or the drawer that opens in its place
  // (`fitPadding`); below 720px the bottom sheet, by its laid-out height, not its box on screen,
  // which is still sliding in.
  function drawerCover() {
    if (!isPhone()) return fitPadding(8);
    var pad = { top: 24, right: 24, bottom: 24, left: 24 };
    if (drawer.el.hidden || !drawer.el.classList.contains("is-open")) return pad;
    var r = mapEl.getBoundingClientRect();
    var coveredY = Math.max(0, r.bottom - (window.innerHeight - drawer.el.offsetHeight));
    if (r.height - coveredY > 120) pad.bottom += coveredY;
    return pad;
  }
  function fitTo(bbox, maxZoom) {
    if (!bbox || bbox.length !== 4 || bbox.some(function (n) { return !isFinite(n); })) return;
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: drawerCover(), maxZoom: maxZoom, duration: motionMs(600) });
  }
  // The point lands in the middle of what the drawer leaves showing: an offset, not `padding`,
  // which MapLibre would keep for every later move.
  function flyToPoint(lnglat, minZoom) {
    var pad = drawerCover();
    map.flyTo({
      center: lnglat, zoom: Math.max(map.getZoom(), minZoom),
      offset: [(pad.left - pad.right) / 2, (pad.top - pad.bottom) / 2], duration: motionMs(600)
    });
  }
  function setSearchPin(lnglat) {
    pendingPin = lnglat;
    var source = map.getSource("search-pin");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: lnglat ? [{ type: "Feature", geometry: { type: "Point", coordinates: lnglat }, properties: {} }] : []
    });
  }
  map.on("load", function () {
    // Drawn over every other layer, and never a click target (`onMapClick` does not ask for it).
    map.addSource("search-pin", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "search-pin-halo", type: "circle", source: "search-pin",
      paint: { "circle-radius": 16, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 6, "circle-stroke-color": mapColors.land, "circle-stroke-opacity": 0.9 }
    });
    map.addLayer({
      id: "search-pin", type: "circle", source: "search-pin",
      paint: { "circle-radius": 16, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 2.5, "circle-stroke-color": cssVar("--link") || "#0b1023" }
    });
    if (pendingPin) setSearchPin(pendingPin);
  });

  function goToProject(item) {
    drawerAnchor = null;
    openDrawer(item.feature || { name: item.label, url: item.url, lifecycle_state: null });
    if (item.lnglat) {
      setSearchPin(item.lnglat);
      flyToPoint(item.lnglat, 11);
    } else {
      setSearchPin(null);
      fitTo(item.bbox, 10);
    }
  }
  // A company's projects under the current filters, from the map's own geo route at a zoom that
  // draws them apart; their extent is the view. None drawn: the company's page.
  var companySeq = 0;
  function goToCompany(item) {
    var seq = ++companySeq;
    var params = new URLSearchParams();
    appendProposalFilters(params, filters);
    params.set("sponsor_id", item.public_id || "");
    params.set("sponsor_scope", "all");
    params.set("bbox", WORLD_BBOX.join(","));
    params.set("zoom", "7");
    params.set("placement", (filters.placement && filters.placement.length ? filters.placement : DEFAULT_PLACEMENT).join(","));
    function openPage() { if (seq === companySeq && item.url) window.location.assign(item.url); }
    fetch("/api/proposals/geo?" + params.toString())
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("geo " + r.status)); })
      .then(function (envelope) {
        if (seq !== companySeq) return;
        var w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
        (((envelope || {}).data || {}).features || []).forEach(function (f) {
          if (!f.geometry || f.geometry.type !== "Point") return;
          var c = f.geometry.coordinates;
          w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]);
        });
        if (!isFinite(w)) { openPage(); return; }
        if (e - w < 0.05 && n - s < 0.05) flyToPoint([(w + e) / 2, (s + n) / 2], 9);
        else fitTo([w, s, e, n], 9);
      })
      .catch(openPage);
  }
  function pick(opt) {
    closeList();
    hideEmpty();
    searchInput.value = opt.label;
    searchOptions = [];
    searchShownFor = "";
    syncClear();
    // On a phone the keyboard goes, so the map and the sheet have the screen.
    if (isCoarsePointer()) searchInput.blur();
    // Below 720px the bar goes to the top of the screen, 8px under the sticky top bar, so the map under
    // it is what shows above a record's sheet (at 390x844 the sheet left 65px of map when the page
    // stayed where it was). At once, not smoothly: the fit below is worked out for where the map then is.
    if (isPhone()) {
      var barTop = searchForm.getBoundingClientRect().top;
      var under = topBarBottom(true) + 8;
      if (Math.abs(barTop - under) > 2) window.scrollTo(0, Math.max(0, window.scrollY + barTop - under));
    }
    if (opt.kind === "project") { goToProject(opt.item); return; }
    setSearchPin(null);
    drawer.close();
    if (opt.kind === "region") opt.button.click();
    else if (opt.kind === "company") goToCompany(opt.item);
    else fitTo(opt.item.bbox, opt.kind === "county" ? 10 : 7);
  }
  function pickActiveOrFirst() {
    var query = searchInput.value.trim();
    if (query.length < SEARCH_MIN) return;
    if (searchActive >= 0 && searchOptions[searchActive]) { pick(searchOptions[searchActive]); return; }
    if (searchOptions.length && searchShownFor === query) { pick(searchOptions[0]); return; }
    // Typed faster than the pause: ask now and take the first answer.
    searchPendingPick = true;
    runSearch();
  }

  if (searchForm && searchInput && searchList) {
    searchForm.addEventListener("submit", function (e) { e.preventDefault(); pickActiveOrFirst(); });
    searchInput.addEventListener("input", function () {
      syncClear();
      searchPendingPick = false;
      window.clearTimeout(searchTimer);
      if (searchInput.value.trim().length < SEARCH_MIN) {
        searchSeq++;
        if (searchAbort) searchAbort.abort();
        searchOptions = [];
        searchShownFor = "";
        closeList();
        hideEmpty();
        if (searchCount) searchCount.textContent = ""; // no list, so no count left to read
        return;
      }
      searchTimer = window.setTimeout(runSearch, SEARCH_PAUSE_MS);
    });
    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!searchOptions.length) return;
        e.preventDefault();
        openList();
        var step = e.key === "ArrowDown" ? 1 : -1;
        var next = searchActive + step;
        if (next < 0) next = searchOptions.length - 1;
        if (next >= searchOptions.length) next = 0;
        setActive(next);
      } else if (e.key === "Escape") {
        e.preventDefault();
        clearSearch();
      }
    });
    searchInput.addEventListener("focus", function () {
      if (searchOptions.length && searchShownFor === searchInput.value.trim()) openList();
    });
    // A press on a suggestion keeps the focus in the box (no blur before the click lands).
    searchList.addEventListener("mousedown", function (e) { e.preventDefault(); });
    searchList.addEventListener("click", function (e) {
      var node = e.target && e.target.closest ? e.target.closest("[role='option']") : null;
      if (!node) return;
      var opt = searchOptions[Number(node.getAttribute("data-index"))];
      if (opt) pick(opt);
    });
    if (searchClear) {
      searchClear.addEventListener("click", function () { clearSearch(); searchInput.focus(); });
    }
    document.addEventListener("click", function (e) {
      if (!searchForm.contains(e.target)) { closeList(); hideEmpty(); }
    });
    searchForm.addEventListener("focusout", function (e) {
      if (e.relatedTarget && !searchForm.contains(e.relatedTarget)) closeList();
    });
  }
})();
