# Recorded fixtures for `pipeline/connectors/us_rfp`

Every page here is a real response, fetched on **2026-10-21** with the repo's identified User-Agent
(`Bankable/0.2 (+https://inframation.io/bot; contact https://inframation.io/contact)` after the
browser-like prefix), robots.txt read first for each host, one request at a time at 0.5 rps or slower.
Only the rows `data/vendored/rfp_issuers/issuers.yaml` clears (docs/28 §8.1, tranche 1) were read.
`parse()` runs against these in CI; `fetch()` runs only against the fake session in `test_connector.py`,
which serves these same bytes.

Three kinds of file:

- **Trimmed AEP pages** (`pso_*.html`, `appalachian_power_*.html`, `swepco_*.html`,
  `indiana_michigan_power_*.html`). AEP's terms authorise copying "for your personal use only" and
  permit "limited quotations" with attribution (the `us.rfp.*` AEP rows' `license` in
  `data/sources.yaml`; publication `derived_only`; docs/28 §3.1: derived fields, "never the
  documents"), and this repository is public. So an AEP page is never committed as served. Each file
  holds only the page's content column, the region the connector itself cuts and stores
  (`extract.REGIONS["aep_energy_rfps"]`, from `id="main"` to the related-links block), inside a
  stand-in skeleton: a script tag, a view-state input, navigation, the related-links marker and a
  footer, so the region cut is still tested against the chrome it drops. Inside the column only line
  endings (CRLF to LF) and trailing spaces were changed. The pages' own mojibake (`â€“` for an en
  dash), which the parser repairs, is kept. The parser and the fake-session fetch give the same output
  on these files as on the pages as served (checked when the files were trimmed, 2026-10-22). The
  bundles hold the same regions. `test_the_aep_pages_are_committed_trimmed` fails if a whole page is
  committed again. To re-record: fetch the page, cut it with
  `htmltree.region(markup, *extract._region_spec(row, role))`, and paste the cut into an existing
  file's skeleton.
- **Raw pages** (`ct_deep_*.html`, `nj_bpu_public_notices.html`): whole pages as served. Connecticut's
  disclaimer restricts only the state's arms, seal, logo and tourism imagery. New Jersey's statement
  says "anyone may view, copy or distribute State information found here". Line endings are
  normalised to LF, trailing spaces removed and 15 NJ indents mixing a space before a tab expanded,
  so that the diff is whitespace-clean. Nothing else is edited.
- **Bundles** (`bundle_<issuer>_20261021.json`): the connector's own snapshot from the live run of
  2026-10-21 (`python -m pipeline.connectors run us.rfp.<issuer>`), exactly as the runner stored it:
  page regions only, e-mail addresses and phone numbers already removed by `redact()`.

| Fixture | Issuer | Retrieved from | Notes |
|---|---|---|---|
| `pso_energy_rfps.html` (trimmed) | `us.rfp.pso` | https://www.psoklahoma.com/business/b2b/energy-rfps/ | content column only; 2 items (2025 SPP Capacity RFP, joint with SWEPCO; 2026 All-Source RFP) |
| `pso_allsource2026.html` (trimmed) | `us.rfp.pso` | https://www.psoklahoma.com/business/b2b/energy-rfps/AllSource2026 | content column only; per-RFP page: "Final RFP Issued January 2, 2026", "Proposal Due Date March 16, 2026" |
| `appalachian_power_energy_rfps.html` (trimmed) | `us.rfp.appalachian_power` | https://www.appalachianpower.com/business/b2b/energy-rfps/ | content column only; 6 items, 2 under "Archive", two storage RFPs sharing one per-RFP page |
| `swepco_energy_rfps.html` (trimmed) | `us.rfp.swepco` | https://www.swepco.com/business/b2b/energy-rfps/ | content column only; 7 items, 3 of them gas lateral / firm transportation RFPs (non-generation) |
| `indiana_michigan_power_energy_rfps.html` (trimmed) | `us.rfp.indiana_michigan_power` | https://www.indianamichiganpower.com/business/b2b/energy-rfps/ | content column only; 2 items, one a gas lateral RFP (non-generation) |
| `ct_deep_news_2026_page1.html`, `..._page9.html`, `..._page10.html` | `us.rfp.ct_deep` | https://portal.ct.gov/deep/news-releases/news-releases---2026 (`?page=9`, `?page=10`) | pages 1, 9 and 10 of 10: award, RFI, concession, paddlecraft-rental and grant releases beside the two energy procurements |
| `ct_deep_zero_carbon_rfp_release.html` | `us.rfp.ct_deep` | https://portal.ct.gov/deep/news-releases/news-releases---2026/deep-announces-2026-zero-carbon-request-for-proposals | bids "through February 27, 2026, at 11:59 PM EPT ... and through March 17, 2026, at 12:00 PM EPT"; links to the robots-closed document store |
| `nj_bpu_public_notices.html` | `us.rfp.nj_bpu` | https://www.nj.gov/bpu/newsroom/public/ | 142 notices in the 2026, 2025 and 2024 tables |
| `nj_bpu_gsesp_tranche2_extension.txt` | `us.rfp.nj_bpu` | text of https://www.nj.gov/bpu/pdf/publicnotice/Public%20Notice%20-%20GSESP%20Tranche%202%20Extension.pdf as `pdfplumber` extracts it | **edited**: the commissioners' names in the letterhead, the Secretary's signature and her phone and e-mail lines are replaced by `[... removed]` (no personal data in fixtures). The PDF itself is not committed: the connector never stores notice bytes (docs/28 §8.2) |
| `bundle_pso_20261021.json` ... `bundle_nj_bpu_20261021.json` | each | the live run | see above. `bundle_nj_bpu_20261021.json` is the 22:59 UTC re-run, after the notice-deadline fix: same table region and same notice PDF (SHA-256 `ea91d80f...`), only its derived `due_at` differs (03:59 UTC on 6 October, from "11:59 p.m. ET on October 5, 2026") |
