# Recorded CDN files for the browser tests

The public map pages load MapLibre GL JS from jsdelivr (`web/templates/home_map.html`, `asset_detail.html`,
`organization_detail.html`). `web/test_e2e.py` used to fetch these files from the live CDN on every run
(audit 2026-10-07 QA-4), so a CDN outage could fail CI. It now serves these recorded copies to the
browser from disk and checks each against the SHA-256 below before serving it. Nothing here is
fetched at test time.

| File | Recorded from | Retrieved | SHA-256 of the decompressed bytes | Licence |
|---|---|---|---|---|
| `maplibre-gl-5.24.0.js.gz` | `https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js` | 2026-10-07 | `45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb` | BSD-3-Clause (header kept in the file) |
| `maplibre-gl-5.24.0.css.gz` | `https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css` | 2026-10-07 | `ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732` | BSD-3-Clause |

The files are compressed with `gzip -n -9`, which keeps them under the 1 MB fixture limit (docs/04 E-6).
The pmtiles and basemaps scripts are not recorded: the default e2e suite aborts them, which tests the
map's fallback path. Only the opt-in PMTiles proof test fetches them, and it is marked `network`.

**When the MapLibre version in the templates changes**, update `MAPLIBRE_VERSION` in `web/test_e2e.py`.
Then record the new files the same way, using `curl -sS -o maplibre-gl-<v>.js <url>`, `sha256sum` and
`gzip -n -9`. Finally update this table and `RECORDED_CDN` in `web/test_e2e.py`. Until then the e2e
suite fails with an error that names the missing recording. It does not fall back to the network.
The lasting fix is to self-host these files under `web/static/vendor/` with SRI. That is a product
change for the frontend lane.
