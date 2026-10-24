# Self-hosted fonts

Served from `/static/fonts/` and declared in `web/static/css/styles.css` (docs/31 §1.3, D-21: no
third-party font CDN). `.woff2` files are served with a one-year immutable cache (`web/app.py::_StaticFiles`),
so a file whose content changes must get a new name.

The site sets one family, Geist, and Geist Mono for identifiers only (queue IDs, docket numbers, keys), as
the brand system chose (docs/31-brand-identity.md §4, owner 2026-10-23). They replaced IBM Plex Sans, IBM
Plex Mono and Newsreader, which are no longer shipped.

## Geist and Geist Mono

Copyright (c) 2023 Vercel, in collaboration with basement.studio. SIL Open Font License 1.1, with no
Reserved Font Name: the licence's copyright line names none, so a subset may keep the name (OFL §3; brand
assumption BR-2). Licence text: `OFL-Geist.txt`, the package's own `LICENSE.txt`, copied unchanged. Upstream:
https://github.com/vercel/geist-font (licence: https://github.com/vercel/geist-font/blob/main/LICENSE.TXT).

| Files here | Made from | Unicode range |
|---|---|---|
| `geist-latin.woff2` (33,404 bytes) | `Geist-Variable.woff2` (69,652 bytes) | Google Fonts `latin`, plus U+2190-2194, U+2248, U+2264-2265 |
| `geist-latin-ext.woff2` (17,492 bytes) | `Geist-Variable.woff2` | Google Fonts `latin-ext` |
| `geist-mono-latin.woff2` (34,808 bytes) | `GeistMono-Variable.woff2` (71,368 bytes) | as `geist-latin.woff2` |
| `geist-mono-latin-ext.woff2` (15,352 bytes) | `GeistMono-Variable.woff2` | Google Fonts `latin-ext` |
| `OFL-Geist.txt` | `package/LICENSE.txt` | |

**Source.** The npm package `geist` 1.7.2 (released 2026-06-01; the latest stable release on 2026-10-23,
1.8.2, was two days old and not taken), tarball https://registry.npmjs.org/geist/-/geist-1.7.2.tgz with
integrity `sha512-Gu5lDFa3pLRyoBlBPf0QIFHVdWAnpco7fS1bJm41jyLPFoguBgiubseUN2oLXMgqZ7uxAxDoXcHMhCY/fOTTgg==`
(checked on download). The two source files are `package/dist/fonts/geist-sans/Geist-Variable.woff2`
(sha256 `a369fcf5628ea2aa4e1b9e2ec6a5b3624e365bda588e1f0f2f12b564f728fbb8`) and
`package/dist/fonts/geist-mono/GeistMono-Variable.woff2` (sha256
`fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab`), each variable on one axis, `wght`
100-900. The tarball was unpacked only; no package script was run.

**Subset.** fontTools 4.60.1 `subset`, the way Google Fonts splits a family: the codepoints of each range
that the font maps, every OpenType layout feature kept (`tnum` among them), every name record kept (the
copyright and licence lines included), the variation tables kept (one file carries every weight), WOFF2 out.
Equivalent command for the latin file:

```
pyftsubset Geist-Variable.woff2 --flavor=woff2 --layout-features='*' --name-IDs='*' --name-languages='*' \
  --notdef-outline --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2190-2194,U+2212,U+2215,U+2248,U+2264-2265,U+FEFF,U+FFFD" \
  --output-file=geist-latin.woff2
```

The latin-ext files take Google Fonts' `latin-ext` list (`U+0100-02BA, U+02BD-02C5, U+02C7-02CC,
U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020,
U+20A0-20AB, U+20AD-20C4, U+2113, U+2C60-2C7F, U+A720-A7FF`); the `unicode-range` of each face in
`styles.css` is the same list, so a browser fetches latin-ext only for a page that prints one of its
characters (EU tender titles, for one). The extra latin codepoints are the arrows, approximately equal,
less- and greater-or-equal signs the site prints. The subscript two (U+2082) is left out: the labels
print "CO2" (`services/labels.py`), so nothing sets it.

**Fallback faces.** "Geist Fallback" and "Geist Mono Fallback" in `styles.css` are local Arial (or Liberation
Sans) and Courier New (or Liberation Mono) scaled by `size-adjust` to Geist's average advance, weighted by the
character frequencies of the site's own text, with Geist's ascent, descent and line gap divided by the same
factor, so a late font moves nothing (the method and numbers are in the comment above those faces).

## The outlined wordmark

The top bar's wordmark is not set in a font at all: `web/static/img/wordmark.svg` holds it as outlined paths
cut from `geist-latin.woff2` ("infra" at `wght` 700, "mation" at 400, overlaps removed, HarfBuzz kerning,
-0.03em tracking), so a late or missing font never moves it or its dot (`docs/31-brand-identity.md` §5.1).
`python -m web.og_card --wordmark` re-cuts it and prints the copy the top bar inlines; that one step needs
fontTools (4.67.0, with brotli 1.2.1 for WOFF2), skia-pathops 0.9.2 and uharfbuzz 0.57.0, which are not site
dependencies. The OFL covers the font files, not what is made with them: a logo set in Geist and outlined is
artwork, not a modified font (SIL's OFL FAQ, https://openfontlicense.org/ofl-faq/).
