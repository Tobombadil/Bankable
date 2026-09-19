/* Shared basemap for every MapLibre map on the site (docs/04 D-13; web/README.md "Map basemap").
 * Extracted from map.js (2026-09-19, midstream slice) so the asset and company page mini-maps
 * (asset_map.js) draw exactly the same three `MAP_TILE_URL` modes and the same same-origin
 * fallback outline as the home map, instead of a second, drifting copy. Loaded before map.js /
 * asset_map.js; exposes one global, `window.InfraqueBasemap`.
 */
(function () {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function mapColors() {
    return {
      land: cssVar("--map-land") || "#eae6da",
      water: cssVar("--map-water") || "#cfe0e8",
      border: cssVar("--map-border") || "#b9c4c9"
    };
  }

  // Same-origin fallback basemap: the tile/pmtiles source is added only after `load` fires
  // (`addBasemap` below) so a blocked/slow/failed basemap source can never keep the whole style
  // from ever loading -- this fallback is always in the initial style and is what the user sees
  // whenever the real basemap fails, whichever of the three modes is configured.
  function fallbackStyle(colors) {
    return {
      version: 8,
      sources: {
        "basemap-fallback": { type: "geojson", data: "/static/data/basemap_fallback.geojson" }
      },
      layers: [
        { id: "fallback-water", type: "background", paint: { "background-color": colors.water } },
        {
          id: "fallback-land", type: "fill", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "country"],
          paint: { "fill-color": colors.land }
        },
        {
          id: "fallback-country-lines", type: "line", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "country"],
          paint: { "line-color": colors.border, "line-width": 0.6 }
        },
        {
          id: "fallback-state-lines", type: "line", source: "basemap-fallback",
          filter: ["==", ["get", "level"], "us_state"],
          paint: { "line-color": colors.border, "line-width": 0.5 }
        }
      ]
    };
  }

  function addRasterBasemap(map, template_, onFail) {
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
      if (e && e.sourceId === "osm") onFail();
    });
  }

  // Protomaps' own hosted fonts/sprites (coordinator follow-up, 2026-09-15: "close the glyphs
  // gap"), pmtiles mode only -- the dev raster mode and the outline fallback never touch these
  // and gain no new network dependency. Quoted verbatim from
  // https://protomaps.github.io/basemaps-assets/ ("Linking to Assets in Styles"):
  // `glyphs:'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf'`. The
  // sprite path (`sprites/v4/<flavor>`, no extension -- MapLibre appends `.json`/`.png`/`@2x`
  // itself) is versioned separately from the npm package (the assets repo's own "for each major
  // version" convention): `v4` is the set that actually contains the icon names
  // `@protomaps/basemaps@5.7.2`'s generated layers reference (e.g. "arrow" for one-way-road
  // markers) -- confirmed by fetching both `sprites/v3/light.json` and `sprites/v4/light.json`
  // and checking which one has the icon names this bundle's own compiled layers use; `v3`'s
  // sheet is a different, older icon set. Kept as one flavor's worth (`light`, matching
  // `namedFlavor("light")` below) rather than every flavor, since this style only ever uses one.
  var PROTOMAPS_GLYPHS_URL = "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf";
  var PROTOMAPS_SPRITE_URL = "https://protomaps.github.io/basemaps-assets/sprites/v4/light";

  function addPmtilesBasemap(map, tileUrl, colors, onFail) {
    if (typeof pmtiles === "undefined" || typeof basemaps === "undefined") {
      // One or both CDN scripts failed to load (the templates only include them in pmtiles
      // mode) -- the fallback outline layer already in the initial style is what the user sees.
      onFail();
      return;
    }
    try {
      var protocol = new pmtiles.Protocol();
      maplibregl.addProtocol("pmtiles", protocol.tile);
      map.addSource("protomaps", {
        type: "vector",
        url: "pmtiles://" + tileUrl,
        attribution: "&copy; OpenStreetMap contributors"
      });
      // Style-wide (not per-source/per-layer): `setGlyphs`/`setSprite` mutate the current style
      // in place, matching the incremental addLayer approach here rather than a full setStyle
      // that would also replace the fallback and data layers.
      map.setGlyphs(PROTOMAPS_GLYPHS_URL);
      map.setSprite(PROTOMAPS_SPRITE_URL);
      var flavor = basemaps.namedFlavor("light");
      // Mute land/water/roads to the paper ground the same way the raster layer is tinted above,
      // via token-derived colours rather than the flavor's own defaults.
      flavor.background = colors.land;
      flavor.earth = colors.land;
      flavor.water = colors.water;
      flavor.major = colors.border;
      flavor.minor_a = colors.border;
      flavor.minor_b = colors.border;
      flavor.highway = colors.border;
      flavor.link = colors.border;
      flavor.boundaries = colors.border;
      var styleLayers = basemaps.layers("protomaps", flavor, { lang: "en" });
      styleLayers.forEach(function (layer) { map.addLayer(layer); });
      map.on("error", function (e) {
        if (e && e.sourceId === "protomaps") onFail();
      });
    } catch (e) {
      onFail();
    }
  }

  // ---- three MAP_TILE_URL modes (docs/40 §2.7 task item 1) ----
  // `opts.tileMode`: "pmtiles" | "raster" | "dev"; `opts.tileUrl`; `opts.colors` (mapColors());
  // `opts.onFail` called at most once per map by the caller's own guard.
  function addBasemap(map, opts) {
    var onFail = opts.onFail || function () {};
    if (opts.tileMode === "pmtiles") {
      addPmtilesBasemap(map, opts.tileUrl, opts.colors, onFail);
    } else if (opts.tileMode === "raster") {
      addRasterBasemap(map, opts.tileUrl, onFail);
    } else {
      addRasterBasemap(map, "https://tile.openstreetmap.org/{z}/{x}/{y}.png", onFail);
    }
  }

  window.InfraqueBasemap = {
    cssVar: cssVar,
    mapColors: mapColors,
    fallbackStyle: fallbackStyle,
    addBasemap: addBasemap
  };
})();
