# Connection queues outside the US: what exists and what its terms allow

Research note, lane national-queues, 2026-10-23. Research only: no connector was built for any source
below. Terms are quoted verbatim in `docs/13-legal-data-rights.md` §2.20; this note ranks the datasets.
Great Britain's transmission queue (NESO TEC register, 2,227 rows) is already loaded and is not repeated.

**Method.** One GET per page with the platform's User-Agent, 2 s apart; robots.txt read where the host serves
one; challenge pages recorded and never bypassed. "Rows" are counted from the file where it was fetched,
otherwise not claimed.

## 1. Ranked by value to a project developer or investor

Value weighs: project-level rows (not regional totals), the size and activity of the market, whether
large loads (data centres) are included, how often the data is refreshed, and whether the terms let us show it.

| Rank | Country, operator | Dataset and URL | What it holds | Terms (docs/13 §2.20) | Can we show it? |
|---|---|---|---|---|---|
| 1 | Canada (Ontario), IESO | Connection assessment Application Status, https://www.ieso.ca/en/Sector-Participants/Connection-Process/Application-Status; data file https://www.ieso.ca/-/media/files/IESO/files/applicationstatusdata.json | 2,085 applications (fetched 2026-10-23): applicant, project name, zone, type (New Generation 321, Load 143, Additional Load 53, Transmission 152, others), size, proposed in-service date, assessment status. Monthly ("Last Updated: October 7, 2026") | Express licence to use and reproduce, conditional on IESO's copyright line on every reproduction; limited, non-sublicensable, non-transferable | Yes, derived fields with the IESO line; no onward redistribution right in API or export terms. Manifest `ca.ieso.connection_status`: attribution / derived_only, `status: pending` |
| 2 | Germany, Bundesnetzagentur | Marktstammdatenregister full export, https://www.marktstammdatenregister.de/MaStR/Datendownload | Every registered unit, operating and planned ("in Planung"), with capacity, technology, location and operator; full XML export, latest 2026-10-01 | Datenlizenz Deutschland – Namensnennung 2.0: commercial and non-commercial use with the attribution and licence reference | Yes, raw with credit. Already `de.bnetza.mastr` (attribution / raw_ok, no connector). Planned units are those registered once permitted, so it is a pipeline register rather than a grid queue; operator names include natural persons for small units, so a connector keeps large units and organisations only |
| 3 | Australia, AEMO | NEM Generation Information (and the Connections Scorecard), https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-forecasting-and-planning/forecasting-and-planning-data/generation-information | Project-level proposed, anticipated and committed generation and storage in the National Electricity Market, by region (the row records a 64 GW pipeline at December 2025) | Per a search index of AEMO's Copyright Permissions page: use "for any purpose, but only with accurate and appropriate attribution". Not read first-hand: aemo.com.au answered a Cloudflare challenge | Probably yes with attribution, once a person confirms the page in a browser; the automated route needs AEMO's agreement because the site challenges automated clients. Manifest `au.aemo.connections_scorecard` keeps its stricter derived_only class meanwhile |
| 4 | Canada (Alberta), AESO | Connection Project List, https://www.aeso.ca/grid/transmission-projects/connection-project-reporting/ (monthly Excel, e.g. /assets/Uploads/project-reporting/October-2026-Project-List.xlsx; GIS, KML, Power BI) | Every project in the AESO connection process with an accepted System Access Service Request, plus recently energised and cancelled; updated 2026-10-09; Alberta's data-centre requests sit here | Copies only for "non-commercial, personal or educational purposes, provided that the material is not modified"; any other use needs AESO's written permission | No, not without permission (normalising is modifying). `ca.aeso.connection_list`: restricted / none. A permission letter is worth drafting: high value, one counterparty |
| 5 | Brazil, ANEEL | SIGA generation projects, https://dadosabertos.aneel.gov.br/dataset/siga-sistema-de-informacoes-de-geracao-da-aneel (CSV/XML, daily file available) | Every generation project "in various phases: from stages before authorisation through to revocation" | Open Data Commons ODbL | Yes, with attribution and ODbL's share-alike on the derived database (the open item docs/13 §6 records for OpenStreetMap). Not a manifest row yet |
| 6 | Ireland, EirGrid | Enduring Connection Policy batch results, https://www.eirgrid.ie/industry/becoming-customer/generator-connections (ECP-GSS-2 results .docx, ECP-GSS-1 final batch .docx, ECP-2.5 results .pdf) | Applicants admitted to each connection batch; documents, not a register | "may not be copied, modified, reproduced, republished ... or used for any public or commercial purposes except as ... expressly authorised in writing by EirGrid" | No. `ie.eirgrid.connections`: restricted / none. A re-use request under Ireland's public sector information rules is a question for counsel |
| 7 | Spain, Red Eléctrica | Access capacity by transmission node, https://www.ree.es/es/clientes/generador/acceso-conexion/conoce-la-capacidad-de-acceso (monthly, e.g. 2026_10_01_GRT_generacion.csv/.xlsx); demand access request status under Clientes/Consumidor | Node-level capacity granted and available, not project rows; a separate page lists the status of demand (large-load) access requests | The legal notice (https://www.ree.es/es/aviso-legal) answered HTTP 403; unread | Unknown. Worth reading in a browser: the demand-request list is the closest public European analogue to a data-centre queue |
| 8 | Italy, Terna | Econnextion, https://www.terna.it/it/sistema-elettrico/rete/econnextion | Connection requests for renewables, storage and loads; the detailed platform is for ministries and regions, the public page shows regional totals | Legal notes page fetched; no reuse clause found in its text | Regional totals only; low value for project-level work |

Not examined this session (no dataset URL confirmed): New Zealand (Transpower's former connection-pipeline URL
answers 404), India (CTUIL's site failed certificate verification from this sandbox), Chile (Coordinador
Eléctrico Nacional), Japan (OCCTO publishes access statistics, not project rows), the Netherlands (the national
capacity map shows congestion and queue volumes by area, not projects), and the distribution-level embedded
capacity registers of Great Britain's network operators, which are the largest unread queue in a market we
already cover and should be read next.

## 2. What this changes

- Two manifest rows moved out of `unknown` on terms read today: `ie.eirgrid.connections` and
  `ca.aeso.connection_list` to `restricted`, so the coverage page now says their terms require a licence, not
  that they could not be read. `ca.ieso.connection_status` became attribution / derived_only with
  `status: pending`, the ceiling its terms allow, nothing built. Its required copyright line is the row's
  `notice`, so it renders with every credit; before a connector, the API and export terms must say that
  IESO-derived rows carry no sublicence (the licence is non-sublicensable).
- Recommended order for a connector lane: IESO (one JSON file, permissive with a notice), then MaStR's planned
  units (open licence, largest market), then ANEEL SIGA once the ODbL question is settled. AEMO and AESO need
  a person first: a browser read of AEMO's page and a request for an automated route; a permission letter to
  the AESO.
- Messaging (owner, 2026-10-23): these are the sources that would let the map speak for Canada, Germany,
  Australia and Brazil as it already does for Great Britain.
