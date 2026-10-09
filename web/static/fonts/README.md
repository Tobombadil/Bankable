# Self-hosted fonts

Served from `/static/fonts/` and declared in `web/static/css/styles.css` (docs/31 §1.3, D-21: no
third-party font CDN). Every Plex file here is redistributed unmodified: no subsetting, renaming or
re-encoding. One Newsreader file is a modified instance, which its licence allows (below). `.woff2` files are served with a one-year immutable cache (`web/app.py::_StaticFiles`),
so a file whose content changes must get a new name.

## IBM Plex Sans and IBM Plex Mono

Copyright 2017 IBM Corp., SIL Open Font License 1.1, with Reserved Font Name "Plex". Because a
modified version may not carry the reserved name, only IBM's own released files are shipped, as
IBM split them. Licence text: `LICENSE-IBM-Plex-Sans.txt` and `LICENSE-IBM-Plex-Mono.txt`, each
the package's own `LICENSE.txt`, copied unchanged.

| Files here | Package (npm registry) | Path inside the package |
|---|---|---|
| `IBMPlexSans-{Regular,Medium,SemiBold,Bold}-Latin{1,2,3}.woff2` (12 files) | `@ibm/plex-sans` 1.1.0, tarball integrity `sha512-WPgvO6Yfj2w5YbhyAr1tv95RUz4LRJlqN+CmYvBglabXteufP1D1E9BABMde+ZIKdRbFJDoKF5eQzfhpnbgZcQ==` | `package/fonts/split/woff2/` (same file names) |
| `IBMPlexMono-{Regular,Medium,SemiBold}-Latin{1,2,3}.woff2` (9 files) | `@ibm/plex-mono` 2.5.0, tarball integrity `sha512-STBJIPxPomOYPmBMO7z5TKPJUotAF9u3gAUumTqVgwgrAO+K4FRNh0MlhsoJjKhJKsMbBJR10/bk4inkj/wc1w==` | `package/fonts/split/woff2/` (same file names) |
| `LICENSE-IBM-Plex-Sans.txt` | `@ibm/plex-sans` 1.1.0 | `package/LICENSE.txt` |
| `LICENSE-IBM-Plex-Mono.txt` | `@ibm/plex-mono` 2.5.0 | `package/LICENSE.txt` |

The weights, the order of the Latin3/Latin2/Latin1 faces and their `unicode-range` values in
`styles.css` are copied from IBM's `package/css/ibm-plex-sans-all.css` and
`package/css/ibm-plex-mono-all.css`. The tarballs were downloaded and unpacked only; no package
script (the packages declare a telemetry `postinstall`) was run.

## Newsreader

Copyright 2020 The Newsreader Project Authors, SIL Open Font License 1.1, no Reserved Font Name.
Licence text: `OFL-Newsreader.txt` (from `ofl/newsreader/OFL.txt` in github.com/google/fonts).

| File | Origin |
|---|---|
| `newsreader-latin-600.woff2` | An instance of the Google Fonts `latin` subset, `https://fonts.gstatic.com/s/newsreader/v26/cY9AfjOCX1hbuyalUrK4397yjA.woff2` (132,000 bytes, wght 200-800, opsz 6-72), made with fontTools 4.66.1 `varLib.instancer` at wght 600 with opsz limited to 16-40, glyph set and cmap unchanged: 50,680 bytes. The site sets Newsreader only at 600, at 23-33px (audit 2026-10-07 UX-17). Newsreader has no Reserved Font Name, so the OFL permits a modified version under the same name and licence. |
| `newsreader-latin-ext.woff2` | Google Fonts `latin-ext` subset, `https://fonts.gstatic.com/s/newsreader/v26/cY9AfjOCX1hbuyalUrK439DyjJBG.woff2` |
