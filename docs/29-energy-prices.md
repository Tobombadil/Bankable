# 29 — Energy prices on the map: wholesale, delivered and contract prices

Phase 2 data doc. Owner: legal-compliance agent (lane prices-research). Status: v1, 2026-10-22, research; v1.1,
2026-10-23: **slice 1 is built** (lane price-hubs, §9.1): hub and zone prices for ERCOT, SPP (published only under
the noncommercial posture), CAISO and NYISO (statistics only), Great Britain's system price, and EIA's state retail
prices, on `/prices` and `GET /v1/prices/*`. Their manifest rows carry `runner: prices` and no `status`; every
other new row is still `status: pending` or `status: excluded`. Companion to
`13-legal-data-rights.md` (§1 ISO terms, §6 register rows added for every source below), `26-platform-posture.md`
(the `noncommercial` posture the site runs today), `25-grid-interconnection-and-siting.md` (interconnection points,
the line layer and its POI matcher) and the 2026-10-22 decision rows in `docs/00-PLAN.md` (powered-land site
finder, prospect score, prospecting layers, "Market | My pipeline").

**Owner request, verbatim (2026-10-22):** "let's layer in energy prices across the map whenever possible covering
realtime and or day ahead and purchase agreements wherever we can find them".

**I am not a lawyer and this is not legal advice.** Quoted text is verbatim from the page named, retrieved on
2026-10-22 at the UTC time given (platform user-agent; robots.txt read first where one exists; no Cloudflare or
other challenge was bypassed). Anything marked *Inference* is my reading. §10 lists what needs counsel.

## 0. Summary: what can go on the map soonest

| Layer | Source | Can publish | What |
|---|---|---|---|
| Twelve-month node statistics, Texas | ERCOT (`us.iso.ercot.prices`) | **Yes**, raw-ok | Average day-ahead and real-time price, basis to hub, negative-price share, storage spread for 1,043 resource nodes, 7 hubs and the load zones; **and** a latest-price readout (no delay required) |
| Twelve-month node statistics, California and the western markets CAISO runs | CAISO (`us.iso.caiso.oasis_prices`, `us.iso.caiso.price_map`) | **Yes, derived only** | The same statistics for 14,770 nodes, drawn at county grade (CAISO's node coordinates may derive a county but not be served); no latest-price readout until CAISO authorises it in writing |
| New York zones | NYISO (`us.iso.nyiso.prices`) | **Yes, derived only** | Statistics for the 11 zones; generator nodes in tranche 2 |
| GB system price | Elexon (`gb.elexon.bmrs_prices`) | **Yes**, open with an exact credit | A national readout (GB has one price zone) |
| Average industrial price by utility and county | EIA-861 (`us.eia.861`), EIA retail sales (`us.eia.retail_sales`) | **Yes**, public domain | Average revenue per MWh, labelled as an average, not a tariff |
| Disclosed PPA prices | LBNL solar data file (`us.lbnl.utility_scale_solar`) | **Yes**, CC BY 4.0 | LBNL's levelised PPA estimate for 425 named solar plants, joined to our plants by EIA id; regional wind averages (`us.lbnl.land_based_wind`) |
| Tariff structures | OpenEI URDB (`us.openei.urdb`) | Yes (CC BY applied) | Later: a representative bill needs a load profile |
| Contract terms from filings | FERC EQR (`us.ferc.eqr`) | Yes, public domain | Later: heavy, and the format changes under Order No. 917 |
| MISO, SPP, ISO-NE | | **No, link out** | MISO's terms still unreadable (Cloudflare); SPP's grant excludes commercial publication; ISO-NE's terms restrictive on their face |
| PJM | | **No, link out** | Every LMP route is Data Miner 2; no Redistribution License |
| EU day-ahead (ENTSO-E), EPEX, Nord Pool / N2EX, ICE hubs via EIA, LevelTen, Pexapark, Trio, the Grid Status hosted API | | **Never** (excluded) | Proprietary exchange data, licensed third-party data, or private aggregators |

Soonest on the map: ERCOT node and hub statistics with a live hub readout, CAISO county-grade statistics, and EIA
industrial prices by county. Then LBNL's disclosed solar PPA prices on the plants already on the map, once the
owner decides on the ADR 0008 §4 point in §1.

## 1. Scope, and three corrections to the framing

The coordinator's framing: trailing statistics per pricing node (12-month average day-ahead and real-time LMP, basis
to the hub, share of negative-price hours, the average daily top-4-hour minus bottom-4-hour spread); a latest-price
readout only where terms allow redistribution of real-time data; delivered industrial rates for large loads; PPA
prices from public filings only, never paid indices. That stands. Three things in it need correcting.

1. **Hubs and zones are a handful each, but they are not points.** A hub is an average over many buses (ERCOT's
   HB_NORTH, CAISO's TH_SP15), and a load zone is a settlement region. No ISO zone boundary file was found in this
   lane; SPP's price map is the only one that gives hubs a display point (§2.4). So hub and zone values go in the
   map's floating panel and key, not on the canvas as points. A zone polygon is drawn only if a published boundary
   is found and its terms read.
2. **"Derived only" withholds coordinates, so CAISO and NYISO nodes cannot be drawn where those ISOs draw them.**
   The manifest's `derived_only` rule withholds raw rows and exact coordinates, and serves a county or state
   placement instead (`docs/21` §8, restricted-precision rule). CAISO and NYISO are `attribution-restricted`,
   derived-only, in the register (§1.2, §1.5), and their coordinates are part of what they publish. Statistics are
   derived and may be published. The points, and the raw interval price, may not, until those ISOs authorise it or
   counsel reads the terms the other way. Points can still come from a public-domain source: the EIA-860 plant a
   generator node serves (§5).
3. **ADR 0008 §4 excludes prices from asset attributes.** "No valuation, tariff, contract or throughput economics
   (owner, 2026-09-18). A valuation layer is a separate future decision." The owner's request is that decision for
   market prices, tariffs and PPA prices. The draft decision row (§11) records it. The design keeps prices in their
   own tables, linked to assets, never written into `asset.attributes`.

## 2. Sources and their terms

Each subsection gives: what the source offers; access; the reuse class (this repo's vocabulary, `docs/13` §0);
whether real-time data may be redistributed and with what delay; the attribution; rate limits; how its nodes can be
located. Raw captures are in the lane's scratch space, not committed; re-run the fetches to verify.

### 2.1 ERCOT — permissive; real-time redistribution allowed, no delay

**Offers.**
- Real-time settlement point prices every 15 minutes at resource nodes, hubs and load zones (NP6-905-CD, report
  type 12301, nodal since 2010-11-30).
- Day-ahead hourly settlement point prices (NP4-190-CD, 12331).
- SCED LMPs per settlement point and per electrical bus.
- Annual workbooks of hub and load-zone prices back to 2011 (NP6-785-ER real-time, NP4-180-ER day-ahead).
- The settlement point list and electrical bus mapping (NP4-160-SG, 10008). The 2026-10-21 release (doc
  1288962582, 254,175 bytes) maps 19,514 electrical buses to 1,043 resource nodes at 710 substations, plus the hubs
  HB_HOUSTON, HB_NORTH, HB_PAN, HB_SOUTH, HB_WEST, HB_BUSAVG and HB_HUBAVG and the load zones. It carries no
  coordinates.

Sizes, from the public document lists (2026-10-22): about 8.7 KB per 15-minute real-time csv zip and about 184 KB
per day-ahead csv zip.

**Access.** The public MIS document list (`/misapp/servlets/IceDocListJsonWS`) serves the last 7 days of each report
with no account; the ERCOT queue connector already uses this route. The archive (at least 7 years) is on
api.ercot.com, behind a free account.

**Terms.** ERCOT Website User Agreement, last updated 07/20/2023, https://www.ercot.com/help/terms (19:07:46Z),
clause 5, unchanged from `docs/13` §1.1:

> The publicly available contents of this website may be used, reproduced, and redistributed, provided that the
> contents are not modified and that you maintain all copyright and other notices contained in the contents,
> including this Agreement. Notwithstanding the foregoing, raw data provided in public portions of this website may
> be used, reproduced, and redistributed in compilations, charts, and analyses without maintaining such notices.

Terms of Use for ERCOT API, https://www.ercot.com/help/terms/data-portal (19:08:27Z), the parts that bind us:

> Accounts registered by "bots" or other automated methods are not permitted.

> You may not download the same report from the Data Access Portal more than three times within a 12-month period.
> Reports should be downloaded and stored on the users' own hardware to avoid downloading from the site multiple
> times during a yearly time period.

**Rate limits.** From the developer portal's known limits, https://developer.ercot.com/applications/pubapi/known-limits/
(19:08:58Z):

> The API allows up to 30 requests per minute

> Downloads of historic files via the API and WebUI are limited to 1,000 files at a time.

> Currently, regions outside the United States of America are restricted.

The beta server is in Ashburn, so the geographic limit does not bite.

**Class.** `permissive`; rule `raw-ok`.

**Real-time redistribution.** Allowed, with no delay: clause 5 covers raw data in compilations, charts and
analyses, and sets no lag.

**Attribution.** None is required for compilations. We credit "Source: ERCOT" as policy.

**Node location.** ERCOT publishes no coordinates; see §5.

*Inference:* the three-downloads rule makes our snapshot store a requirement, not a convenience. Every file is
fetched once and kept, and a re-parse reads the snapshot. A person must register the API account, not a script.

### 2.2 CAISO — attribution-restricted; statistics yes, the raw interval price not yet

**Offers.**
- OASIS SingleZip queries:
  - PRC_LMP: day-ahead hourly;
  - PRC_RTPD_LMP: fifteen-minute;
  - PRC_INTVL_LMP: five-minute real-time;
  - ATL_PNODE_MAP: the node-to-aggregate map gridstatus reads as its node list.
- The western areas CAISO prices: WEIM, and EDAM since its 2026 launch.
- A price-map feed behind https://www.caiso.com/todays-outlook/prices. That page embeds
  `wwwmobile.caiso.com/Web.Service.Chart/pricecontourmap.html`, which loads
  https://wwwmobile.caiso.com/Web.Service.Chart/api/v3/ChartService/PriceContourMap1.

One GET of that feed at 20:22:47Z returned:
- 5,027,921 bytes, sha256 `03cdbb00…bfcd2a`;
- 14,770 nodes, each with a latitude and longitude: GEN 3,863, LOAD 10,907;
- 4,177 nodes in the CAISO balancing area, the rest across 24 other WEIM/EDAM areas (BPAT, PACE, PACW, APS, NV,
  SRP and others);
- each node's current day-ahead, fifteen-minute and five-minute LMP and components;
- no trading hubs (TH_NP15 and the like).

CAISO's notice https://www.caiso.com/notices/updates-to-prices-page-in-today-s-outlook-and-iso-today-mobile-app
describes the map's node search and the EDAM areas.

**Access.** No key. OASIS Interface Specification v5.1.2
(https://www.caiso.com/Documents/OASIS-InterfaceSpecification_v5_1_2Clean_Fall2017Release.pdf, 20:23:31Z):

> The "enddatetime" is ignored if the query is to pull "ALL" or "ALL_APNODES" nodes; ie query will return only
> 1-days' worth of data for all nodes at a time based on the "startdatetime" supplied

Its error codes include "1004 Data can be requested for period of 31 days only" and "1017 Please select a maximum
of 10 nodes or use the ALL option". gridstatus queries all nodes one day at a time for day-ahead and one hour at a
time for real-time.

**Terms.** CAISO Terms of Use, https://www.caiso.com/privacy-terms-of-use (19:07:50Z). The page dates the Terms
of Use "Last Updated on: 10/02/2018"; the 05/14/2025 date recorded in `data/sources.yaml` for the queue is the
Privacy Notice's. The text is unchanged from `docs/13` §1.2:

> No material or information from this Website or the California ISO mobile application may be copied,
> reproduced, republished, uploaded, posted, transmitted or distributed except (i) as authorized in these Terms of
> Use, (ii) as expressly authorized in the materials and/or information contained on this Website, or (iii) as
> expressly authorized in writing by the California ISO.

> Most of the materials and information contained on this Website and the California ISO mobile application were
> generated, compiled or assembled from materials and information that are freely available for public use
> consistent with the general policies of the Public Records Act ... and may be used by you provided that you keep
> intact all copyright, trademark and other proprietary notices and that you credit the California ISO when using
> such materials and/or information.

API Terms of Use (08/22/2019), same page:

> You may not use or access the CAISO API for purposes of monitoring the availability, performance or
> functionality of any of CAISO's systems or services or for any other benchmarking or competitive purposes.

> CAISO owns all right, title and interest in and to the CAISO API and CAISO Data.

**Rate limits.** None published. The API terms bar "excessive or unnecessary" use. gridstatus waits 5 seconds
between OASIS calls; we assume one request per 5 seconds.

**Class.** `attribution-restricted`; rule `derived-only` with the credit "Source: California ISO", as for the CAISO
queue.

**Real-time redistribution.** Not settled. The terms set no delay. They allow use of "most" materials with credit
and bar republication "except as authorized". Under the register's derived-only reading, trailing statistics are
derived and publish. A raw interval price shown as "latest" is the source row itself, and waits for written
authorisation, the route (iii) the terms themselves name (§9, lane P0).

**Attribution.** "Source: California ISO".

**Node location.** CAISO is the only ISO found that publishes a coordinate for every pricing node, and those
coordinates are display positions. Against the EIA-860 plants whose generators name a CAISO node (153 pairs):
- the median distance is 0.87 km;
- 120 pairs are within 10 km;
- the largest is 812 km (§5.2).

*Inference:* the "monitoring" clause is aimed at watching CAISO's systems. A price map of CAISO's market outcomes
does not monitor system availability. "Competitive purposes" is undefined; CAISO sells no data product we
compete with. Moderate confidence; listed for counsel (§10).

### 2.3 MISO — terms still not readable; link out

**Offers.**
- Day-ahead and real-time LMPs at commercial pricing nodes (CPNODEs) and hubs.
- Market reports on docs.misoenergy.org.
- A key-free public JSON API. One read of the current five-minute ex-post interval at 20:21:10Z returned 2,628
  CPNODEs and eight hubs: ARKANSAS, ILLINOIS, INDIANA, LOUISIANA, MICHIGAN, MINN, MS, TEXAS. Nothing was stored.
- A Data Exchange at data-exchange.misoenergy.org. It shows a sign-up page and no terms; its `/terms` path
  answered 404.

**Terms.** https://www.misoenergy.org/meet-miso/legal-and-privacy/ answered HTTP 403 with a Cloudflare challenge at
19:07:50Z and again at 20:21:05Z, as on 2026-09-12 and 2026-10-11 (`docs/13` §1.7). It was not bypassed. This
time robots.txt also answered 403.

**Class.** `unknown`; rule `link-out-only`.

**Node location.** Measured for later: 536 of the 2,628 CPNODEs (20%) are named by an EIA-860 generator (§5.2).

*Inference:* a key-free API is not a licence. CLAUDE.md requires MISO's terms to be read and recorded first. The
step is unchanged: a person opens the page in a browser, saves a PDF, and the clauses go into `docs/13` §1.7.

### 2.4 SPP — a noncommercial grant in substance; link out pending an owner decision

**Offers.**
- Day-ahead and real-time balancing-market LMPs by settlement location and by bus, served by
  portal.spp.org's file-browser API (a JavaScript app; gridstatus uses the API).
- ArcGIS price-contour services at pricecontourmap.spp.org. The services `PCM/DA_Features` and
  `PCM/RTBM_Features` (20:28:18Z–20:28:36Z) carry point features for the hubs (SPPNORTH_HUB, SPPSOUTH_HUB,
  SWPW_HUB), interfaces and DC ties, with hourly or five-minute LMP and components. They carry no nodes. The DA
  service describes itself as "Settlement Locations(hubs and interfaces only) and Reserve Zones".

**Terms.** https://www.spp.org/terms-conditions/ (19:07:51Z), unchanged from `docs/13` §1.4:

> All materials posted or otherwise available on this website are the exclusive copyrighted material of the
> author(s) or SPP. Permission is implicitly granted to copy and distribute (via computer network or printed form) in
> whole or in part (with appropriate citation) EXCEPT when such materials will be used, in whole or in part, within a
> commercial publication (printed or otherwise) or when the author(s) or SPP will be quoted in commercial materials,
> forums or publications. Any commercial use of these materials requires prior, express written authorization from
> the author(s) or a duly authorized officer of SPP.

The External Systems Acceptable Use Policy (version 3.0, latest revision 6/27/2014), linked from that page, was
read for the first time (20:20:25Z, sha256 `641ba7ff…2c9a2658`). Its scope covers "public data, networks,
data/file publication systems". The clause that binds us:

> External Users expecting to utilize SPP systems and communications systems in a manner that is inconsistent with
> this Policy (e.g., polling of systems at a frequency higher than necessary for basic reconciliation of data) must
> receive approval from SPP in advance of such activities.

**Class.** `restricted` in the manifest, to match `us.iso.spp.gen_queue`; rule `link-out-only`.

*Inference:* the clause grants copying "with appropriate citation" except for commercial publication, which is a
noncommercial grant like the Texas RRC's (`docs/13` §6.2). Under the owner's noncommercial posture it could be read
as `noncommercial`. That reading applies to the SPP queue row as much as to prices. Moving them, together or not
at all, is the owner's call, with the cost set out in §6. High confidence on the text, moderate on how SPP would
apply it to derived statistics.

**Node location.** SPP publishes display points for its three hubs only. Nodes were not measured.

### 2.5 NYISO — attribution-restricted; zones now, generator nodes later

**Offers.**
- Day-ahead and real-time LBMP, zonal (11 zones plus external proxies) and per generator: daily csv, plus monthly
  zip archives from 1999-11. The P-2A, P-2B and P-24A index pages were read at 20:43Z.
- `generator.csv` (20:21:12Z): 832 rows, 813 active. 563 of the active rows carry NYISO's own latitude and
  longitude. 162 of the 250 without are external proxy buses (zones PJM, NPX, H Q, O H), so NYISO places 563 of
  the 651 in-state generators, 86%.

**Terms.** NYISO Legal Notice, https://www.nyiso.com/legal-notice (19:07:52Z), unchanged from `docs/13` §1.5. The
only express prohibition is that "Downloading, republishing, retransmitting, reproducing, or other use of any image
or video on this website as a stand-alone file is strictly prohibited".

**Class.** `attribution-restricted`, derived-only, credit "Source: NYISO".

**Real-time redistribution.** No clause addresses it, and none sets a delay. Under derived-only the latest raw
price is held back, as for CAISO; a raw-ok move needs counsel, as the register already says.

**Rate limits.** None published; 0.5 rps assumed.

**Node location.** NYISO's own coordinates, used to derive a county under derived-only, plus EIA-860 points (§5).

### 2.6 ISO New England — restrictive on its face; link out

**Offers.**
- Day-ahead hourly LMPs; real-time five-minute and hourly LMPs.
- More than 1,000 pnodes, per the pricing-node table page
  (https://www.iso-ne.com/markets-operations/settlements/pricing-node-tables, 20:21:16Z): "New England has over
  1,000 pnodes". The table gives load zone, reserve zone, RSP area and dispatch zone, and no coordinates.
- ISO Express CSVs on www.iso-ne.com, and a web-services API that needs an ISO Express account. The account terms
  were not read.

**Terms.** https://www.iso-ne.com/legal-privacy (19:07:52Z), unchanged from `docs/13` §1.6:

> By using this website, you signify your assent to these Terms and Conditions.

> You are also hereby put on notice that the Content is protected by copyright under United States laws. Any
> duplication of the Content or non-personal use may violate copyright, trademark, and other laws.

The same page defines the Content as the documents "collectively with all information and data on this website".

**Class.** `restricted`; `link-out-only` until ISO-NE permits it in writing or counsel reads the clause otherwise
(`docs/13` §7 item 6). The same page's throttling rules apply to any request.

### 2.7 PJM — link-out only; the terms that make it so

**Offers.** Day-ahead and real-time LMPs at pnodes, hubs and zones, served through Data Miner 2. gridstatus's
`PJM.get_lmp` needs `PJM_API_KEY`, a Data Miner 2 subscription key. No price route outside Data Miner was found.

**Terms.** API portal "Acceptable Terms of Use", https://apiportal.pjm.com/content/html_widgets/39vgb.html
(20:21:17Z):

> Information and data contained in Data Miner 2 is for internal use only and redistribution of information and or
> data contained in or derived from Data Miner 2 is strictly prohibited without an effective PJM-issued
> Redistribution License.

The Data Miner 2 Terms of Use PDF (20:21:20Z, md5 `f2e554a41e5b40c9852fcfed05ca6fa5`, byte-identical to the
2026-09-12 copy) says, clause 1:

> Members may republish data, however non-members are specifically prohibited from republishing data.

**Class.** `restricted`; `link-out-only`. "Derived from" in the portal text reaches statistics too, so no PJM number
appears in any tier until a licence exists (CLAUDE.md). Link out to PJM's own LMP pages.

### 2.8 Elexon BMRS / Insights — open with an exact credit; MID held back

**Offers.**
- System sell and buy prices per half-hour settlement period. For 2026-10-21 the API returned 48 periods
  (20:29:09Z).
- Market Index Data (MID) per provider (20:29:07Z: providers `APXMIDP` and `N2EXMIDP`).
- Imbalance volumes and many other datasets.

No key: "All our APIs are public and no API key is required" (https://developer.data.elexon.co.uk/, 20:29:06Z).

**Terms.** "Copyright: Licence to use BMRS open data",
https://www.elexon.co.uk/bsc/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/ (20:29:03Z):

> Elexon Limited hereby grants You a worldwide, royalty-free, perpetual, non-exclusive licence to Use the BMRS Data
> subject to the conditions below.

> When You Use BMRS Data, You must always use the following attribution statement to acknowledge the source of the
> information: Contains BMRS data © Elexon Limited copyright and database right [year]. Where possible You must
> also contain a link to this licence.

> You are free to: copy, publish, distribute and transmit the BMRS Data; adapt the BMRS Data; exploit the BMRS
> Data, including commercially, or by including it in your own product or application

> These are important conditions of this licence and if You fail to comply with them the rights granted to you
> under this licence, or any similar licence granted by the Elexon, will end automatically.

> This licence does not cover the use of: personal data; third party rights that Elexon is not authorised to
> licence; Information subject to other intellectual property rights, including patents, trademarks, and design
> rights

The quote joins the list items the page sets on separate lines.

BMRS API terms ("Copyright: Licence to use BMRS APIs", page modified 2023-07-07, read through
`/bsc/wp-json/wp/v2/pages/115218`, 20:30:01Z) give, as an example of misuse:

> invoking the API more than once in a half hour period for data that is only updated on a half hourly basis

**Class.** `open-attribution`; rule `raw-ok` with the credit "Contains BMRS data © Elexon Limited copyright and
database right <year>". Real-time redistribution is allowed, including commercially, with no delay.

**MID.** Held back (`gb.elexon.market_index_data`, `restricted`). MID is the volume-weighted price of trades on
EPEX SPOT's and Nord Pool's GB markets (the two providers above). The licence carves out "third party rights that
Elexon is not authorised to licence", and EPEX's conditions (§2.10) claim derived data. Ask Elexon one question:
is MID inside the open data licence?

**Node location.** GB has a single price zone, so this is a national readout.

### 2.9 ENTSO-E Transparency Platform — day-ahead prices are not on the free re-use list

**Offers.** Day-ahead prices per bidding zone (Transparency Regulation Art. 12.1.d), with load, generation, flows,
balancing and imbalance data. The RESTful API needs a free security token requested by email. A rate of 400
requests a minute is reported by third parties only and was not verified.

**Terms.** The "Legal Terms and Conditions" article, read through the Zendesk API at 20:32:24Z (article updated
2026-09-22), attaches the General Terms and Conditions. The page itself is
https://transparencyplatform.zendesk.com/hc/en-us/articles/40921911218961-Legal-Terms-and-Conditions; its HTML
answered 403. The version used here was approved 29 March 2023 and applies from 1 November 2023 (sha256
`665d0862…6352b`). Clause 3.1, on any re-use:

> not cause prejudice to the copyright or related right on a Transparency Platform Data, which may be owned by the
> concerned Primary Owner of Data. In case of a risk to cause prejudice to said right, the Data User shall seek the
> prior agreement of the holder of the copyright or related right.

The List of Data Available for Free Re-use (last modified 18 October 2023, sha256 `b21717e8…08ff9a`):

> Data Users may freely copy, redistribute, and adapt the listed data for any purpose, by giving appropriate credit
> (attribution) to its source and indicating if they have made any changes, with no need to seek for the prior
> agreement of the respective Primary Owner of Data.

The list's Transparency Regulation references are 12.1.a, b, c and g (allocations and flows), 13.x, the 17.x
balancing items and imbalance prices (data item 27). Day-ahead prices, Art. 12.1.d, are not on it.

**Class.** `restricted`; `link-out-only`, status excluded. *Inference:* the day-ahead price's Primary Owner is the
exchange that cleared it, so the agreement needed is EPEX's or Nord Pool's (§2.10). This answers `docs/13` §7 item
11 for this one data item. Imbalance prices are CC BY 4.0 and could be a later, separate row.

### 2.10 EPEX SPOT, N2EX and Nord Pool — excluded: proprietary, licensed per user

GB's day-ahead auction prices come from EPEX SPOT and from N2EX, Nord Pool's GB market; most EU day-ahead
prices come from EPEX SPOT and Nord Pool.

**EPEX SPOT.** General Conditions of Data Use applicable to EPEX SPOT Information Products, dated 1 November 2025
(20:30:54Z, sha256 `194b6346…4fba036`):

> Data created or derived from Information Product is still deemed to be Information Product if (i) the quotes,
> prices, turnover figures, indices or other data originally marketed by EPEX SPOT can be determined through
> calculation or automated process and/or (ii) the alteration is formed in a way that the derived data can be used
> instead of the quotes, prices, turnover figures, indices or other data originally marketed by EPEX SPOT (i.e. as
> substitute).

The licensed External Usage covers "Internet/TV Broadcasting, display on a website".

**Nord Pool.** Terms and conditions for use of website,
https://www.nordpoolgroup.com/en/About-us/terms-and-conditions-for-useofwebsite/ (20:30:42Z):

> You may not republish, retransmit, redistribute or otherwise make the contents of this website available to any
> other party or make the same available on any website, on-line service or bulletin board of your own or of any
> other party ... without our express prior written consent from Nord Pool.

> Nord Pool does not permit automatic extraction of data or other usage that reduces the performance of the website.

Nord Pool's redistribution-licence FAQ (20:29:13Z) lists "Displaying Market Data on public / private User
Interfaces" and "Derived market data which is then redistributed in the market" as licences carrying "a license
fee per each of its customer/user".

**Why excluded.** Both treat display and derivatives as licensed products sold per user. A twelve-month average of
their auction prices is, on EPEX's definition, still their product. Excluded from every tier, including
second-hand through ENTSO-E, Elexon MID or third-party sites (`eu.epex.market_data`, `eu.nordpool.market_data`,
status excluded).

### 2.11 EIA wholesale hub prices — ICE data, link out

**Offers.** Daily next-day bilateral index prices at seven electricity hubs, with their gas hubs: Mid-C, PJM West,
SP15, NP15, Palo Verde, Mass Hub, Indiana Hub, and ERCOT North from 2014. These are traded indices, not ISO LMPs.

**Terms.** https://www.eia.gov/electricity/wholesale/ (20:33:55Z):

> The market data provided here are republished, with permission, from data collected by the Intercontinental
> Exchange (ICE)

> If a certain hub or time period does not appear, EIA does not have access to it through the above-mentioned
> agreement. The data and methodology would have to be obtained directly from ICE.

EIA Copyrights and Reuse, https://www.eia.gov/about/copyrights_reuse.php (20:33:56Z):

> You may see on our website documents, illustrations, photographs, or other information resources contributed or
> licensed by private individuals, companies, or organizations that may be protected by U.S. and foreign copyright
> laws. Transmission or reproduction of protected items beyond that allowed by fair use as defined in the copyright
> laws requires the written permission of the copyright owners.

**Class.** `restricted`; `link-out-only` (`us.eia.wholesale_ice`, status excluded). The permission is EIA's, not
ours, and 17 U.S.C. §105 does not reach ICE's data. Yes, the ICE-sourced data carries redistribution limits.

### 2.12 EIA retail and industrial prices — public domain

**Offers.**
- EIA-861 annual, https://www.eia.gov/electricity/data/eia861/ (20:33:56Z): revenue, sales and customers by
  utility, state and sector, and utility service territories by county. "Release date: October 7, 2026 (final
  data)" for 2025; next release August 2027 (early release).
- EIA-861M monthly: July 2026 data released 2026-09-24.
- Electric Power Monthly Table 5.6.A, average price by state and sector.
- The API v2 route `electricity/retail-sales`.

**Terms.** EIA Copyrights and Reuse (20:33:56Z):

> U.S. government publications are in the public domain and are not subject to copyright protection. You may use
> and/or distribute any of our data, files, databases, reports, graphs, charts, and other information products that
> are on our website or that you receive through our email distribution service. However, if you use or reproduce
> any of our information products, you should use an acknowledgment, which includes the publication date

**Rate limits.** API documentation (20:33:59Z): "our API only returns a maximum of 5,000 rows", and programs
"must throttle the number of the requests they make per second and per hour". A free key is required.

**Class.** `public-domain`; rule `raw-ok`.

*Inference, moderate confidence:* average industrial revenue per MWh is the honest public proxy for a delivered
industrial rate. It is not what a 100 MW data centre would pay: large loads take transmission-voltage tariffs or
special contracts. In retail-choice states (including ERCOT's competitive areas), EIA-861 reports energy-only and
delivery-only service separately from bundled service. There a single "average delivered price" must combine a
retailer average with the wires company's delivery average, or say that no bundled average exists. Lane P5 checks
this against the 2025 files before any number is drawn.

### 2.13 OpenEI Utility Rate Database — open; the stricter of two statements applied

**Offers.** Tariff structures (energy, demand and fixed charges, time-of-use) for "over 3,700 U.S. utilities",
"checked and updated annually". Bulk `usurdb.csv.gz` and `usurdb.json.gz`, and an API at api.openei.org with a free
key.

**Terms.** Two statements:
- https://apps.openei.org/USURDB/ (20:34:28Z): "Content is available under Creative Commons Zero unless otherwise
  noted."
- The OEDI record for the same database, https://data.openei.org/submissions/5 (20:34:31Z): "Content is available
  under Creative Commons Attribution 4.0 unless otherwise noted". Its License field renders client-side.

The OpenEI Terms of Use (effective July 31, 2023, https://openei.org/wiki/OpenEI:General_disclaimer) now answer
200. On 2026-09-28 they answered 404, which closes `docs/25` §2.6 item 4. The API key terms and rate limits on
developer.nrel.gov were not read: the proxy answered 502.

**Class.** `open-attribution`, CC BY 4.0 applied as the stricter statement; credit "Source: Utility Rate Database
(URDB), OpenEI".

*Inference:* a tariff is a structure, not a price. A $/MWh figure needs a load shape and voltage. That is a later
calculation (§7.4), not a tranche-1 layer.

### 2.14 FERC Electric Quarterly Reports — public domain; the filing route to PPA prices

**Offers.** Contract data (buyer, product, term, rate, point of delivery) and transaction data, filed quarterly by
sellers of wholesale power. Long-term PPAs sold to utilities and other wholesale buyers appear here.

**Terms.** 18 CFR §35.10b (eCFR, 20:35:45Z):

> Each public utility as well as each non-public utility with more than a de minimis market presence shall file an
> updated Electric Quarterly Report with the Commission covering all services it provides pursuant to this part

> The term does not include an entity that engages in purchases or sales of wholesale electric energy or
> transmission services within the Electric Reliability Council of Texas

The section is amended by "Order 917, 91 FR 14347, Mar. 24, 2026". Order No. 917 moves filing to XBRL-CSV and adds
RTO/ISO transaction reports. FERC has not set an implementation timeline.

**Access.** The www.ferc.gov EQR pages answered a Cloudflare challenge at 20:35:07Z, not bypassed. The EQR Report
Viewer (https://eqrreportviewer.ferc.gov/, 20:35:08Z) answered 200 and lists Q3 2026. It says:

> Provide your name and Email Address, and you will be emailed a link to a .zip file of the data.

**Class.** `public-domain`; `raw-ok`. Filers' contact names, titles, phone numbers and emails are stripped at
ingest (`docs/13` §5).

*Inference:* ERCOT-internal contracts are outside the EQR, and so are most corporate virtual PPAs, which are
financial rather than sales for resale. So the EQR covers utility offtake well and Texas poorly. An all-companies
quarter is gigabytes and the format is about to change. This is tranche 3.

### 2.15 LBNL utility-scale solar and land-based wind data files — CC BY 4.0, with a trap

emp.lbl.gov answered a Cloudflare block (20:35:09Z). The same files are on eta-publications.lbl.gov and were read
there.

**Solar.** "Utility-Scale Solar, 2025 Data Update", `2025_utility-scale_solar_data_update.xlsx`: 56,943,952 bytes,
sha256 `b17720b1…fccd087`, 59 sheets. Introduction sheet:

> Utility-Scale Solar, 2025 Data Update Copyright (c) 202<>, The Regents of the University of California, through
> Lawrence Berkeley National Laboratory (subject to receipt of any required approvals from the U.S. Dept. of
> Energy). All rights reserved. This work is openly licensed via CC BY 4.0.

What it holds:
- The sheet `Individual_Project_Data` lists 1,775 projects with EIA ID, name and coordinates.
- 425 of them carry "Levelized PV PPA 2024$/MWh": CAISO 149, West (non-ISO) 121, Southeast (non-ISO) 57,
  PJM 28, MISO 26, NYISO 16, ERCOT 8, SPP 7, ISO-NE 7, HI 6. The median is 51.0 $/MWh.
- The stated sources are "EIA, FERC, SEC, trade press, developers/owners".

**Wind.** "Land-Based Wind Energy Technology Data Update, 2025 Edition": 24,270,133 bytes, sha256
`1fdafd4a…11776f19`, 63 sheets.

> These data fall under Copyright (c) 202, The Regents of the University of California, through Lawrence Berkeley
> National Laboratory (subject to receipt of any required approvals from the U.S. Department of Energy). All rights
> reserved. This work is openly licensed via CC BY 4.0: https://creativecommons.org/licenses/by/4.0/

Its `Wind PPAs by Plant` sheet is anonymised: execution date, capacity, term, region and price, "Source: Berkeley
Lab, FERC". So wind PPA prices are regional, not per plant.

**The trap.** Both files embed private indices:
- the sheets "LevelTen Solar PPA Prices", "Trio Solar PPA Prices", "LevelTen & Trio 2024 PPA Index",
  "LevelTen Wind PPA Prices", "Trio Wind PPA Prices";
- LevelTen and Trio columns in the market-value sheets.

A CC BY notice from LBNL cannot license LevelTen's or Trio's index. These are dropped at ingest, as GEM's CC BY-NC
rows are (`docs/13` §2.2).

**Class.** `open-attribution`; `raw-ok`, with credit and a statement of changes.

*Inference:* LBNL's figure is a levelised real-dollar estimate derived from contract terms, not the contract price
as filed. It is shown as "LBNL's levelised estimate".

### 2.16 State commission PPA approvals; LevelTen, Pexapark and Trio (excluded)

**State commissions.** Approvals are public dockets, but price terms are often confidential. One example, read only
second-hand: PG&E's filings describe CPUC D.06-06-066 as keeping renewable contract pricing confidential "until
three years from the date that the contract states that deliveries are to begin, or until one year following
expiration, whichever comes first". Per-state terms are `unknown` (the existing `us.state.puc_dockets` row). They
are a manual, docket-by-docket lane for later: facts and link only, and a price only where the filing states it
publicly.

**LevelTen.** Terms, https://www.leveltenenergy.com/terms (20:38:49Z). Users may not:

> (i) resell, sublicense, or make any other commercial use of the Platform other than as contemplated by these
> Terms; (ii) distribute, redistribute, transfer, or modify the Platform, or otherwise grant rights to third
> parties with regard to the Platform, without our written consent

**Pexapark.** Terms of Use, https://pexapark.com/terms/ (20:38:53Z). Users may not:

> copy, reproduce, frame, mirror, crawl, republish, modify, create derivatives of, any part of the Platforms, the
> Services and/or the Content

**Trio.** Its own terms were not read. Its indices reach us only as sheets inside LBNL's files.

**Class.** All three are private aggregators: `restricted`, do not ingest at all (status excluded), the same class
as the aggregators named in CLAUDE.md.

### 2.17 At a glance

| Source | Real-time redistribution | Delay | Attribution | Rate limit | Node coordinates |
|---|---|---|---|---|---|
| ERCOT | allowed (raw data in compilations) | none | none required; "Source: ERCOT" as policy | 30 req/min (API); 3 downloads per report per 12 months | none |
| CAISO | not settled; derived-only until written authorisation | none stated | "Source: California ISO" | none published; 1 per 5 s assumed | yes, every node (display positions) |
| NYISO | not addressed; derived-only | none stated | "Source: NYISO" (policy) | none published | generators only (86% in-state) |
| MISO | unknown | — | — | — | none found |
| SPP | not for commercial publication | — | "appropriate citation" | AUP: no polling beyond "basic reconciliation" | hubs only |
| ISO-NE | no ("non-personal use may violate copyright") | — | — | throttling by IP | none |
| PJM | no (Redistribution License) | — | — | 6 connections/min non-member | n/a |
| Elexon | allowed, including commercially | none | exact credit with year, plus licence link | once per half hour per half-hourly dataset | n/a (one zone) |
| ENTSO-E day-ahead | no (Primary Owner's agreement) | — | — | 400/min (unverified) | n/a |

## 3. Coverage

| Layer | Publish today, once built | Needs terms recorded or a licence | Link-out only |
|---|---|---|---|
| Node statistics | ERCOT (raw-ok); CAISO and NYISO (derived: county grade, plus EIA-860 points) | MISO (read the terms); SPP (owner decision, or SPP's written consent); ISO-NE (written permission) | PJM (Redistribution License), and everything in the next two columns until cleared |
| Hub and zone statistics | ERCOT hubs and zones; CAISO hubs and DLAPs; NYISO zones | MISO hubs, SPP hubs, ISO-NE hub and zones | PJM hubs; EIA's ICE hub table |
| Latest price readout | ERCOT only | CAISO and NYISO (written authorisation, or counsel moves them to raw-ok) | everyone else |
| GB | Elexon system prices | Elexon MID (one question to Elexon) | EPEX and N2EX auctions (excluded) |
| EU | none | ENTSO-E imbalance prices (CC BY 4.0, not yet a row) | day-ahead prices (excluded) |
| Delivered and retail rates | EIA-861, EIA-861M and EPM by state and utility; URDB tariffs (later) | — | — |
| PPA prices | LBNL solar (425 named plants), LBNL wind (regional); FERC EQR (later) | state commission dockets (per state) | LevelTen, Pexapark, Trio (excluded) |

## 4. The gridstatus library

| Question | Finding |
|---|---|
| Licence | BSD 3-Clause, "Copyright 2022 James Max Kanter", unchanged on main. Already vendored for the queues under that licence (`pipeline/vendor/gridstatus/LICENSE`) |
| Maintainer | The README: "maintained by Grid Status", the company that also sells the hosted API. The hosted API is a re-host and is excluded (`us.gridstatus_io.hosted_api`; its terms page answered 403) |
| Release vs main | PyPI's latest is still 0.36.0 (2026-04-21). Main (commit `5c48466`, 2026-10-22) is six months ahead and still reports `__version__ = "0.36.0"`, so a vendored file must name the commit, not the version. Recent price fixes on main include "Fix MISO public five-minute LMP interval labels" (2026-09-04), "Handle ERCOT's capped/uncapped SCED price column split" (2026-08-28, 2026-09-02), "Keep retired PJM pnodes in LMP results" (2026-09-05) and "Fix CAISO historical CSV SSL verification" (2026-08-31). Actively maintained, and the ISOs change formats often enough that it matters |

**What it calls, per ISO** (main, `5c48466`):

| ISO | Endpoints | Functions |
|---|---|---|
| ERCOT | MIS public document lists (report types 12301, 12331, 10008); `data.ercot.com` archive pages; `api.ercot.com` (account) | `Ercot.get_spp` (ercot.py:2348), `get_rtm_spp` / `get_dam_spp` (1548 / 1590, the annual hub and zone files), `get_settlement_points_electrical_bus_mapping` (7315); `ErcotAPI.get_spp_real_time_15_min` / `get_spp_day_ahead_hourly` (ercot_api.py:1780 / 1817) |
| CAISO | `oasis.caiso.com/oasisapi/SingleZip` | `CAISO.get_lmp_day_ahead_hourly` / `get_lmp_real_time_15_min` / `get_lmp_real_time_5_min` (caiso.py:1708 / 1691 / 1674) over `_get_lmp` (1482); `CAISO.get_pnodes` (1457). The price-map feed is not in gridstatus |
| NYISO | `mis.nyiso.com/public/csv` (damlbmp, realtime), `generator.csv` | `NYISO.get_lmp` and its market wrappers (nyiso.py:873–993), `NYISO.get_generators` (1225) |
| MISO | `docs.misoenergy.org/marketreports`, `public-api.misoenergy.org`; `apim.misoenergy.org` (Data Exchange keys) | `MISO.get_lmp_*` (miso.py:737–817); `MISOAPI.get_lmp_*` (miso_api.py:92–193) |
| SPP | `portal.spp.org/file-browser-api`, `pricecontourmap.spp.org` | `SPP.get_lmp_day_ahead_hourly` (spp.py:1683), `get_lmp_real_time_5_min_by_location` (1538) |
| ISO-NE | `www.iso-ne.com/transform/csv`, `static-transform/csv/histRpts`; `webservices.iso-ne.com` (account) | `ISONE.get_lmp_*` (isone.py:537–596); `ISONEAPI.get_lmp_*` (isone_api.py:1286–1375) |
| PJM | `api.pjm.com/api/v1` (Data Miner 2 key) | `PJM.get_lmp_*` (pjm.py:655–849) |

**Is using it consistent with each ISO's terms?** The library fetches from the ISOs directly, so each ISO's terms
govern, exactly as if we wrote the client ourselves. The library adds no permission and lifts no limit. Three of its
routes would commit us to terms we must not accept, or have not read:

1. **PJM.** Every PJM call needs a Data Miner 2 key, which means accepting "internal use only". Never use it.
   pjm.py also hard-codes a PJM Planning API subscription key taken from PJM's page (line 1349); we vendor none
   of it.
2. **ISO-NE web services and MISO's Data Exchange.** Both need accounts whose terms are unread.
3. **ERCOT's API.** Fine, if a person registers the account. Its token flow is automatable; account creation is not.

Its defaults are not ours. Each class runs its own `requests` session with sleeps and retries, and PoliteSession
(user-agent, robots, per-host token bucket) must replace them.

**Recommendation: vendor parse-only functions, not the client.** Do as was done for the queues
(`pipeline/vendor/gridstatus/README.md`):
- copy each ISO's URL builders and parsers from a pinned main commit into `pipeline/vendor/gridstatus/prices.py`;
- fetch through PoliteSession;
- record the upstream file, lines and SHA-256 per function;
- pin a fixture frame to gridstatus's own output (digest test), so drift fails CI.

This adapts the fetchers to our connector contract: bytes in through `RawSnapshot`, pure `parse`, the provenance
quartet on every row. It does not drag in the library's dependency pins, which `docs/00-PLAN.md` closed out on
2026-10-07.

## 5. Node location

Rule: never guess a location. A node is drawn as a point only on evidence that meets the bar in §5.3. Otherwise it
is drawn at region grade or listed as unplaced, and the map says so.

### 5.1 What each ISO publishes

| ISO | Nodes | Coordinates published by the ISO | Other keys usable for matching |
|---|---|---|---|
| ERCOT | 1,043 resource nodes at 710 substations; 7 hubs; load zones | none | substation mnemonic, PSS/E bus name and number, voltage, load zone (settlement point list); unit names per resource node |
| CAISO | 14,770 pnodes in the price map (4,177 in the CAISO area) | **yes, every node** (display positions) | node name (joins OASIS), GEN/LOAD type, area |
| NYISO | 813 active generators; 11 zones | **yes, 563 generators** (86% of in-state) | PTID, zone, subzone |
| MISO | 2,628 CPNODEs, 8 hubs | none found | node name (often utility prefix and unit) |
| SPP | not counted | hubs, interfaces and DC ties only | settlement location names |
| ISO-NE | over 1,000 pnodes | none | load zone, reserve zone, RSP area, dispatch zone |
| PJM | link-out | n/a | n/a |

### 5.2 Measured matches (2026-10-22)

**EIA-860 names the node of many generators.** Schedule 3 of EIA-860 (2025 annual, file 23,622,347 bytes, sha256
`2b27929d…6d08ba`, public domain) has a column "RTO/ISO LMP Node Designation". It is filled on 5,817 of 27,918
generators: MISO 1,535, PJM 1,423, SPP 556, CAISO 510, ERCOT 510, NYISO 433, ISO-NE 409. Its plants carry EIA's
coordinates. A generator's owner naming the node it settles at, at a plant whose location EIA publishes, is evidence,
not a guess. `docs/27` noted the column on 2026-10-06 and left it unused.

| ISO | Distinct designations | Match the ISO's node list | Share of the ISO's nodes placed this way |
|---|---|---|---|
| ERCOT | 281 | 152 exact, 156 after space and hyphen become underscores; 6 ambiguous (designating plants more than 5 km apart) | 156 / 1,043 = **15%** |
| MISO | 750 | 536 | 536 / 2,628 = 20% (terms not cleared) |
| CAISO | 300 | 149 | 1% of 14,770 (4% of the CAISO area) |
| NYISO | 166 (PTIDs) | 108 | 108 / 651 in-state generators = 17% |

The two ISO-published coordinate sets were checked against EIA's plant coordinates through the same join:

| Pairs | Median distance | Within 2 km | Within 10 km | Largest |
|---|---|---|---|---|
| CAISO price map vs EIA plant, 153 pairs | 0.87 km | 85 | 120 (78%) | 812 km |
| NYISO generator.csv vs EIA plant, 161 pairs | 1.87 km | 81 | 120 (75%) | 329 km |

*Inference:* about three in four agree within 10 km. A quarter do not, and either side can be wrong: a schematic
position on the map, or a stale designation in EIA-860. Neither source is survey grade, and the label says whose
position it is.

**ERCOT bus numbers join the queue.** Docs/25 quotes 11 ERCOT POI strings that carry a bus number (§1.1 and the
§2.5 hand-check sample), among them "59903 Bearkat 345kV", "7042 Zorn 345kV", "(#170174) LOST PINES 345 kV",
"#80064 Chocolate Bayou 138kV" and "Bus# 1048 - Tonkawa". All 11 resolve in the settlement point list's PSS/E bus
numbers to an ERCOT substation whose bus name and voltage agree with the string. Substation and bus name pairs
include W_BK_345 / BEARKAT, ZORN, LOSTPI / LOSTPINE5, CHOCBAYU / CHOCBAYU4A and TKWSW / TONKAWA_5, all at the
voltage the string states. The join is deterministic, and it settles the one error docs/25's line-endpoint matcher
made (#80064 Chocolate Bayou, put in Harris County): ERCOT's own bus 80064 is at CHOCBAYU. It gives a node a
substation and, through the queue rows, a county. It gives no point.

**OpenStreetMap substations.** Not measured here: Overpass reset the connection, then answered 504, from this
sandbox. The site-finder lane is loading them. A national figure for scale, from taginfo (data until 2026-10-20):
about 890,000 `power=substation` objects worldwide, 26.6% with a `name` and 47.1% with a `voltage`. US
transmission substations are likely better named than that average, which is dominated by unnamed European
distribution substations. Unmeasured.

### 5.3 The matcher and its score (lane P2)

Evidence, strongest first. Each node keeps `placement_grade` (point, region, none), `placement_method`,
`placement_confidence` (0–1) and the evidence ids.

1. **EIA-860 designation** (public domain). An exact or normalised name match, with every designating plant within
   2 km: point at the EIA plant, confidence 0.9. Plants within 5 km: 0.8. Plants further apart: ambiguous, not
   placed by this rule.
2. **ISO-published coordinate** (CAISO, NYISO). Used to derive a county (region grade) under derived-only. Served
   as a point only after written authorisation, labelled "as CAISO's price map draws it".
3. **ERCOT bus number to substation to OpenStreetMap.**
   - Join the queue POI's bus number to the ERCOT substation.
   - Match that substation (mnemonic, PSS/E bus name, and the POI's own name) to an OSM substation on name,
     voltage and the queue rows' county.
   - Confidence 0.85 when all three agree; 0.7 on a fuzzy name with voltage and county.
   - The point is OSM's (ODbL). The crosswalk is kept separate so share-alike covers only it (`docs/00-PLAN.md`
     2026-10-22, OSM substation row).
4. **Name and voltage match to OSM alone**, inside the ISO footprint. Needs two corroborating facts (name and
   voltage, or name and county): 0.6.
5. **Region only:** a county from rule 2 or from the queue rows, else the load zone or the ISO footprint.
6. **Unplaced:** no evidence, or evidence that conflicts. The node is listed and counted in the map panel ("312
   ERCOT nodes not placed"). It is never drawn.

Points are drawn at confidence 0.8 or more, and only after a hand-checked random sample of 100 shows 95% or better
precision, as docs/25 did for the POI matcher (39 of 40).

### 5.4 Honest sizing

| ISO | Point grade | Region grade | Unplaced |
|---|---|---|---|
| ERCOT | 15% on EIA-860 alone (measured). With the bus-number and OSM rules, an **estimated 35–60%** | some of the rest by county | an **estimated 40–65%** placed only as a region or not at all |
| CAISO | about 1% (EIA-860) until CAISO authorises its positions; then 100% "as CAISO draws it" | 100% (county from CAISO's coordinate) | about 0% |
| NYISO (tranche 2) | about 17% (EIA-860) | 86% of in-state generators by county | external proxies are by definition not in New York |
| MISO (after terms) | 20% (EIA-860), more with OSM | — | an estimated majority |

The ERCOT range is an estimate. The OSM part is unmeasured, and lane P2 replaces it with a number in its first
week. Load nodes are the hard part everywhere: they serve no EIA plant, so they place only through substations.

## 6. The noncommercial posture

- **Nothing in tranche 1 is `noncommercial`-class.** ERCOT is permissive. CAISO and NYISO are
  attribution-restricted. EIA and FERC are public domain. Elexon, LBNL and URDB permit commercial use with credit.
  The price layers therefore add nothing to the six sources that must grant commercial reuse before the posture
  flips (`docs/00-PLAN.md` 2026-10-22), and they survive a flip unchanged.
- **SPP is the exception, if the owner takes the noncommercial reading** (§2.4). SPP would become the seventh
  source. Its queue and price rows would publish now, and they would be withdrawn under `docs/26` §5 at a flip
  unless SPP grants commercial use first. Recommendation: do not take it for prices alone. Send SPP one letter
  (`docs/39` pattern) asking for both the queue and prices. Until it answers, SPP stays link-out.
- **No paid tier for prices.** Paid tiers are inactive under the posture (`docs/26` §3 (i)). The ERCOT readout is
  free for every reader. Nothing here relies on a "live paid" tier.
- **The firewall.** Price statistics from attribution and open sources may sit beside a private deal in My
  pipeline, read live with their provenance. No `noncommercial` row ever feeds them (CLAUDE.md), and nothing from a
  private deal feeds the price layers, statistics or analytics.
- **Under a later commercial posture,** ERCOT, Elexon, EIA, FERC, LBNL and URDB stay usable as they are. CAISO's
  "competitive purposes" phrase should be read by counsel before anything is sold (§10). EPEX, Nord Pool,
  ENTSO-E day-ahead and PJM need licences in any posture.

## 7. Data design

### 7.1 Tables and files

Prices are context data, not records. They never create proposals and have no lifecycle. Per ADR 0008 §4, as
amended by the decision in §11, they live in their own tables, linked to assets and interconnection points by id.
Every row carries the provenance quartet (`source_id`, `source_url`, `retrieved_at`, `licence_id`).

| Name | Where | Rows (tranche 1) | Contents |
|---|---|---|---|
| `price_node` | Postgres | about 16,000 | ISO, node name, kind (gen, load, hub, zone, interface), zone, reference hub, voltage where known, first and last seen, placement fields (§5.3), `geom` only at point grade, `region_id` at region grade, the node list's provenance |
| `price_node_asset` | Postgres | about 400 in tranche 1 (ERCOT 156, CAISO 149, NYISO 108 matched nodes) | node to `power_plant` asset via EIA plant id and generator ids (the EIA-860 designation); method and confidence |
| `price_stat` | Postgres | about 16,000 × 2 markets × windows | node, market (da, rt), window end date, window length (365 days; calendar year), mean, basis mean, negative-interval share, mean daily top-4 minus bottom-4 spread, coverage share, computed_at, provenance and publication rule |
| `price_latest` | Postgres | about 15 (ERCOT hubs and zones), about 1,060 if nodes are added | interval end, price, retrieved_at; ERCOT only (raw-ok) |
| daily summaries | Parquet on R2, `prices/<source_id>/daily/<yyyy>/<mm>.parquet` | about 11.6 million rows a year | per node, day and market: mean, min, max, negative intervals, top-4 and bottom-4 means, basis mean, intervals present |
| node locations | Parquet, `data/normalized/prices/node_locations.parquet` | about 16,000 | the matcher's output with evidence |
| `utility_price` and `utility_county` | Postgres | about 3,300 utilities × sectors; about 10,000 utility-county pairs | EIA-861 revenue, sales, customers, average price, service type; service territory |
| `ppa_price` | Postgres | about 425 solar plants, plus regional rows | asset id (EIA plant), execution date, term, capacity, price, price basis ("LBNL levelised, 2024 $/MWh"), buyer type, provenance |

### 7.2 Statistics

All are computed per node and market, over the trailing 365 days that end with the last complete operating day, and
for each calendar year.

- **Average day-ahead price:** the time-weighted mean of hourly day-ahead prices, $/MWh. Not load-weighted.
- **Average real-time price:** the mean of the real-time intervals: ERCOT's 15-minute settlement point prices,
  CAISO's five-minute LMP. *As built for hubs and zones (slice 1, §9.1):* ERCOT 15-minute settlement point prices;
  SPP five-minute, read once an hour (a readout, not a full series); CAISO the 15-minute pre-dispatch market
  (`PRC_RTPD_LMP`, the §7.4 fallback, a third of the five-minute volume); NYISO the hourly integrated real-time
  LBMP (`rtlbmp`); Great Britain the half-hourly system price. The five-minute CAISO market is a node-level
  (tranche 2) question. `/prices` names each market's real-time series in its method section and on each figure.
- **Basis to hub:** the mean of node minus reference hub, interval by interval, in each market. The reference hub
  is ERCOT's hub for the node's load zone, or the CAISO trading hub whose zone contains the node. It is not
  computed where no hub is defined (most WEIM areas), and the page says so.
- **Negative-price share:** the share of hours (day-ahead) or intervals (real-time) priced below zero.
- **Storage spread:** each day, the mean of the four highest hourly prices minus the mean of the four lowest, then
  averaged over the days. Day-ahead first; real-time from hourly averages. It is an upper bound on a 4-hour
  battery's energy arbitrage with perfect foresight, before losses. It is a signal, not revenue, and the label says
  so.
- **Coverage:** a statistic is withheld below 90% interval coverage. Price corrections replace earlier values: both
  ERCOT and CAISO publish corrections. Days run in market-local time across DST changes.
- **Hubs and zones:** the same statistics over the full history the source offers. ERCOT's hub and zone workbooks
  go back to 2011.

### 7.3 Refresh cadence

| What | When |
|---|---|
| Day-ahead prices | daily, after publication (early afternoon market time for the next day at both ISOs; the exact times are not quoted here) |
| Real-time intervals | daily batch for the previous day; ERCOT's MIS list holds 7 days, so a missed day is recoverable for a week |
| Trailing statistics | recomputed daily after the batch |
| ERCOT latest readout | 15-minute bucket, hubs and zones only: one MIS list request and one file per interval, well inside 30 a minute |
| Node lists and placement | weekly (ERCOT published four settlement point lists between 2026-09-23 and 2026-10-21, one per network model load) |
| EIA-861 | annual (release month 10); EIA-861M and EPM monthly |
| LBNL | annual (solar October, wind August) |
| URDB | monthly |

### 7.4 Volume estimates

| Item | Estimate | Basis |
|---|---|---|
| ERCOT raw snapshots, kept (the three-downloads rule) | about 0.37 GB a year | measured file sizes: 96 × 8.7 KB plus 184 KB a day |
| ERCOT backfill, 12 months | about 35,000 real-time files and 365 day-ahead files; 35 batches of 1,000 at 30 requests a minute | API limits |
| CAISO day-ahead, all nodes | **unmeasured**; estimated 5–15 MB a day zipped; 365 requests to backfill a year at 1 per 5 s, about 30 minutes | row count (about 6,000 day-ahead nodes × 24 × 5 components) |
| CAISO real-time, all nodes | **unmeasured**; estimated 50–250 MB a day zipped for five-minute; 8,760 hourly requests to backfill a year, about 12 hours at 1 per 5 s | row count (14,770 nodes × 288 intervals × 5 components) |
| CAISO raw retention | keep 7 days of real-time and 90 days of day-ahead, then delete once the daily summaries are verified; if lane P1's first week measures real-time at the top of the range, use fifteen-minute (PRC_RTPD_LMP, a third of the volume) for the real-time statistic | volume |
| Daily summaries | about 11.6 million rows a year; 60–90 MB a year as Parquet | 16,000 nodes × 365 × 2 markets × about 32 bytes, 4–6× compression |
| Postgres | `price_stat` and `price_node` together under 50 MB | about 16,000 nodes |
| API payload | at national zoom, hubs and zones only; nodes from zoom 6, capped like `LINE_FEATURE_CAP`; vector tiles if a national node layer is wanted later | the 1.8 MB transmission precedent (`docs/25` §2.3) |

### 7.5 Delivered rates and PPA prices

**Delivered rates.**
- County view: the EIA-861 industrial average of each utility serving the county (service-territory join). Where
  several utilities serve a county, show the range and name them; where retail choice splits energy and delivery,
  say so (§2.12).
- URDB tariffs are a later calculation: a representative monthly bill for named load shapes (for example 100 MW at
  85% load factor, transmission voltage), shown with its assumptions.

**PPA prices.**
- LBNL's solar figures attach to the plant asset as a separate `ppa_price` row with the LBNL credit and its basis.
- Wind and solar regional averages go in the panel.
- EQR contracts are a later lane with its own schema.

## 8. On the map and in the site finder

All of this is in **Market** ("Market | My pipeline", `docs/00-PLAN.md` 2026-10-22). A signed-out reader sees it.

**Home map.**

*The layer.* A "Wholesale prices" entry in the floating panel and key, with a statistic selector:
- average day-ahead (the default);
- average real-time;
- basis to hub;
- negative-price hours;
- storage spread.

Scales are fixed per statistic so views compare: diverging around zero for basis, sequential for the rest.

*Where the marks go.*
- **Points** for nodes placed at point grade. The popup names the node, the ISO, the statistic, the window, the
  placement method ("at the EIA-860 plant this generator settles at") and the credit.
- **Counties** shaded for region-grade nodes: the median node value and a node count. This is how CAISO appears
  until CAISO authorises its positions.
- **Unplaced** nodes are counted in the panel, never drawn.

*Hubs and zones.* Values listed in the panel for the ISOs in view. For ERCOT, the latest 15-minute hub prices, with
the interval end time and "as published by ERCOT".

*National zoom.* The panel's summary mode shows hub averages per ISO and the share of nodes with negative-price
hours above a threshold. It lists no nodes (as decided for projects, 2026-10-22).

*More layers.*
- "Average industrial price (EIA-861)": county choropleth, labelled "average revenue per MWh, not a tariff quote".
- "Disclosed PPA prices": LBNL solar plants as points on the existing plant marks, with "LBNL levelised estimate,
  2024 $/MWh". Wind as regional figures in the panel.

*Link-out.* For PJM, MISO, SPP and ISO-NE the panel says "prices: see <ISO>", with the ISO's own price-map link and
the reason (licence, or terms unread). The withheld-source statement on `/v1/coverage` lists them too (§9, "Side
effect of these rows today").

**Record pages.**
- A plant page shows "Pricing node: X (EIA-860)" and its statistics.
- An interconnection point page shows the node at that substation when the ERCOT bus join or the matcher found one,
  with method and confidence.

**Site finder** (its lane owns the page). Each candidate corridor samples:
- the nearest point-grade node: its distance, method and confidence, and its statistics;
- the zone and hub values;
- the county industrial average.

The large-load preset leads with the delivered-rate proxy and basis. The generation and storage preset leads with
negative-price share and spread.

Per the 2026-10-22 decision on the prospect score, prices are shown as facts and not scored until the back-test
says which way they count. The page names what it cannot see: a delivered rate for a new large load is negotiated,
and a node's history is not its future.

## 9. Build plan

Lanes, each with the files it owns. The **home-map lane holds `web/templates/home_map.html` and
`web/static/js/map.js` until it lands.** Lane P4 starts after that and edits neither before then. The site-finder
lane owns its own page and sampling code. P4 hands it a sampling function, not edits to its files.

| Lane | Owns | Does | Effort |
|---|---|---|---|
| **P0** legal follow-ups (no code) | `docs/13-legal-data-rights.md` (§1.7, §6 rows), letters in `docs/39` (new section) | CAISO letter: written authorisation to show price-map node positions and the latest interval price. SPP letter: commercial and noncommercial reuse of queue and prices. Elexon: one email on MID. A person: reads MISO's legal page in a browser and registers the ERCOT API account | 1–2 days drafting; the owner sends |
| **P1** fetch and statistics, tranche 1 | `pipeline/prices/` (new: `base.py`, `ercot.py`, `caiso.py`, `nyiso.py`, `stats.py`, `fixtures/`, `test_*.py`); `pipeline/vendor/gridstatus/prices.py` and its README section; the tranche-1 rows of `data/sources.yaml` (drop `status: pending` when built) | ERCOT real-time and day-ahead settlement point prices, the hub and zone history and the latest readout; CAISO OASIS day-ahead and real-time, all nodes, and the price-map node list; NYISO zones; daily summaries and trailing statistics; snapshot retention | 8–10 days |
| **P2** node registry and location (separate lane, can start now) | `pipeline/prices/nodes.py`, `pipeline/prices/node_match.py`, `pipeline/prices/test_node_match.py`, `data/normalized/prices/node_locations.parquet` (generated). Reads, never writes, the OSM substation output of the site-finder lane and `pipeline/context/poi_match.py` | The §5.3 matcher: EIA-860 designations, the ERCOT bus join, OSM name, voltage and county matching, the score, the unplaced state, a precision report on 100 hand-checked nodes | 6–8 days; the OSM part waits for the site-finder lane's substations |
| **P3** serving | `services/db/migrations/versions/00NN_price_tables.py` (0039 is the next free number on 2026-10-22; renumber at merge as usual), a price section in `services/db/models.py`, `services/ingest/prices.py`, `services/api/prices.py`, the `/v1/prices/*` paths in `api/openapi.yaml`, tests | `price_node`, `price_node_asset`, `price_stat`, `price_latest`; the loader; `GET /v1/prices/nodes/geo` (grade-aware, licence predicate applied, county fallback for derived-only), `GET /v1/prices/nodes/{id}`, `GET /v1/prices/hubs`; credits in every response (`docs/21` §8) | 4–5 days |
| **P4** map and site-finder UI | after the home-map lane lands: `web/static/js/map.js` (price layer), `web/templates/home_map.html` (panel and key entries), a new `web/static/js/prices.js` if the layer is split out; the plant and interconnection-point page partials for the node and statistics rows | §8; axe-core run as for other layers | 4–6 days |
| **P5** delivered rates | `pipeline/context/eia861.py`, `pipeline/context/eia_retail.py`, tests; URDB later in `pipeline/context/urdb.py` | EIA-861 utility and county prices with the retail-choice split; the state monthly series | 3–4 days; URDB bill calculation 3–5 days more |
| **P6** PPA prices | `pipeline/context/lbnl_ppa.py` and test (drops LevelTen and Trio); later `pipeline/context/ferc_eqr.py` | LBNL solar per plant and regional wind; EQR later | LBNL 1–2 days, after the owner's ADR 0008 §4 decision. EQR 2–3 weeks (multi-GB quarters, the PII strip, the Order No. 917 change) |

**Tranche 1:** P1, P2, P3, P4 and P5, plus P6's LBNL part. About **25–35 lane-days**, which runs as about two
calendar weeks in parallel once the home-map lane has landed. P2 and P5 can start now.

**Tranche 2:** node level for each further market as its terms clear. About 3–5 days each: a parser and node list
in P1, matching in P2.

| Market | Gate |
|---|---|
| NYISO generator nodes | first; no terms wait, derived-only |
| MISO | after the browser read |
| SPP | after the owner's decision or SPP's letter |
| ISO-NE | after written permission |
| PJM | only with a Redistribution License |

**Why not subclass `Connector`.**
- `pipeline/connectors/base.py` types `kind` as proposal, opportunity or document, and its `normalize` produces the
  canonical record schema.
- It is one of `_SHARED_PARSER_MODULES`, so editing it re-versions every connector's parser.
- P1 therefore builds `pipeline/prices/base.py` on the same parts: `RawSnapshot`, `PoliteSession`, `Store`,
  `SourceEntry.licence_id`, and fetch, parse and normalise with parse pure on fixtures. That is the context-layer
  pattern of `pipeline/context/lbnl_transmission.py`.
- If the coordinator prefers a new `kind` in `base.py`, the cost is a one-time parser-version bump on every
  connector.

**Side effect of these rows today.** `/v1/coverage` lists every registry source whose terms withhold it
(`services/api/coverage.py::_withheld`). The nine market-price rows with `publication: none` will appear there as
withheld:

| Rows | Reason the coverage page gives |
|---|---|
| MISO | "terms not retrievable" |
| SPP, ISO-NE, PJM, Elexon MID, ENTSO-E day-ahead, EPEX, Nord Pool, EIA's ICE table | "terms require a licence or consent we do not hold" |

That is accurate. The coordinator may prefer to keep the excluded rows out, by category, until the price layer
ships. The four private-aggregator rows are excluded automatically (`category: aggregator`).

### 9.1 Slice 1 as built (lane price-hubs, 2026-10-22; review fixes 2026-10-23)

Built as one family package, `pipeline/connectors/prices/` (not `pipeline/prices/`; same contract as the P1 row),
with `services/prices/` (tables, loader, statistics), migration `0041_prices.py`, `services/api/prices.py` and
`/prices`. What the build settled that this document did not:

- **ERCOT's MIS routes and robots.txt.** `www.ercot.com/robots.txt` disallows `/misapp` and `/misdownload`. The
  document lists and annual workbooks are read there with the robots override, as the ERCOT queue connector reads
  the same documented MIS data-product route; the display pages under `/content/cdr/`, which robots.txt allows, are
  read honouring it. The header above ("robots.txt read first where one exists") describes the research reads;
  this exception is recorded as a decision row for `docs/00-PLAN.md` (2026-10-23).
- **The three-downloads rule, enforced at the fetch (2026-10-23).** Each capped download (`meta.max_downloads`)
  is written to a download ledger (`prices/<source>/downloads/`) before it is sent, and rewritten once stored or
  failed; the runner counts the ledger and the run records and refuses a fourth download in twelve months on every
  path (a planned download, a stored copy that fails its SHA-256 or is gone). A capped request is sent once, without
  the HTTP session's in-call retries. A download whose process died before its outcome is counted. A daily tick
  escalates to a backfill only while the source has automatic tries left (ERCOT one, the others three), counted from
  a marker each backfill writes before it fetches, so a killed backfill is not started again by the next tick or a
  queue retry.
- **The backfill window.** A backfill parses only the workbook sheets and rows inside the trailing 366 days (the
  workbooks stay stored whole). ERCOT's interval store is 21 MB for those days, against 38 MB for the 22 months the
  first build kept.
- **Memory, for the worker's 1 GB container (2026-10-23).** Annual workbooks are parsed one monthly sheet at a time
  (`ercot_annual_spp_sheets`, vendored change 10) in a forked child process, normalised column by column, and spilled
  to a month-partitioned staging area; each backfill month is merged in a forked child, as Arrow tables; the CLI uses
  Arrow's system allocator. The loader reads no `raw` column, shares one string per distinct value, summarises one
  local month at a time over three UTC months, and runs as its own process (`python -m services.prices.load
  <source> --run <ts>`), not in the worker.

| Measured on the stored ERCOT snapshots (replayed through a fake network into an empty store) | First build | Since 2026-10-23 |
|---|---|---|
| ERCOT backfill: requests | 2 lists, 4 workbooks, 10 pages | the same (16) |
| ERCOT backfill: interval rows stored | 1,186,845 (2025-01-01 on) | 659,520 (366 days) |
| ERCOT backfill: time | 387 s | 195-225 s |
| ERCOT backfill: peak memory | 2,073 MB RSS, one process | 359 MB for the whole run, children included, measured as a memory cgroup's peak (page cache included); completes under a 400 MB cgroup limit, fails cleanly (the run recorded `failed`, the parent alive) under 320 MB |
| ERCOT full load (13 months into `price_daily` and `price_latest`) | 2,601 MB RSS, 94 s | 327 MB RSS, 26 s (a process of its own) |
| Statistics, ERCOT (30 rows) | | 1.6 s |

Page and API latency and the other markets' figures are the review's measurements on the first build (2026-10-23:
`GET /v1/prices/hubs` p50 37 ms and 111 KB; an ERCOT series 46 ms and 130 KB; `/prices` 100-340 ms and 112 KB;
statistics 3.0 s for 37 locations).

## 10. Open items and counsel flags

1. **CAISO**, counsel: does "competitive purposes" in the API terms reach a public price map? Does "most of the
   materials" cover OASIS prices for raw display? The written-authorisation route is cheaper than an opinion.
   Lane P0 drafts the letter.
2. **SPP**: the owner's call on the noncommercial reading, for the queue and prices together (§2.4, §6).
3. **MISO**: unchanged since 2026-09-12. A person reads the page in a browser.
4. **Elexon MID**: one email to Elexon.
5. **LBNL**: a CC BY notice over a file that embeds third-party indices. Dropping the LevelTen and Trio sheets is
   my reading of the safe course, not counsel's. The same question applies to LBNL's queue file (`docs/13` §7 item
   3).
6. **EIA-860 designations as placement**: a public-domain fact joined to a public-domain coordinate. The output is a
   located node built from an ISO's node name. *Inference:* a name is a fact, and its location comes from EIA, not
   the ISO. Low risk; noted.
7. **ODbL**: node points placed on OSM substations inherit "© OpenStreetMap contributors" and the derived-database
   offer, the open item already in the 2026-10-22 OSM row. Keep the crosswalk separate.
8. **Critical infrastructure**: a priced node at a substation, near queue and capacity facts, is the pairing the
   2026-10-22 OSM row flags. The same review covers it.
9. **ERCOT API account**: a person registers it, per the terms ("bots ... are not permitted").

**Assumptions.**

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| A-1 | One OASIS request per 5 seconds is acceptable to CAISO (no published limit) | gridstatus default | Slower backfill; ask CAISO |
| A-2 | The CAISO real-time all-node volume is 50–250 MB a day zipped | row counts, unmeasured | Lane P1 measures it on day 1; fifteen-minute fallback |
| A-3 | ERCOT's three-downloads rule applies to API archive downloads, not only the web portal | the terms name the API as including the Data Access Portal | None if we store once anyway |
| A-4 | EIA-861 separates bundled, energy-only and delivery-only service in retail-choice states | the form's structure; moderate confidence | Lane P5 checks the 2025 files |
| A-5 | 35–60% of ERCOT resource nodes reach point grade | the EIA measurement plus an unmeasured OSM share | The map shows more region and unplaced nodes; the panel counts them either way |

## 11. Decision needed (draft row for `docs/00-PLAN.md`)

The coordinator's same-day row ("Energy prices join Market intelligence ...", commit `97d5dad`) records the owner's
request and the terms-first rule. This draft follows it with what the terms allow. It corrects one phrase in that
row: "placed on the map only from published coordinates" cannot hold for CAISO and NYISO, whose coordinates are
withheld under derived-only (§1, point 2).

| Date | Decision | Why |
|---|---|---|
| 2026-10-22 | **Energy prices, tranche 1, as the terms allow (follows the same-day "Energy prices join Market intelligence" row; amends ADR 0008 §4 for market prices, average delivered rates and disclosed PPA prices, which live in their own tables, never in `asset.attributes`).** Tranche 1: twelve-month node statistics for ERCOT (raw-ok; latest hub and zone readout every 15 minutes) and CAISO (derived-only, drawn by county until CAISO authorises its node positions), NYISO zones, EIA-861 average industrial prices by utility and county, and LBNL's levelised PPA estimates for 425 named solar plants. Excluded in every tier: PJM (no Redistribution License), EPEX, N2EX and Nord Pool auction prices and ENTSO-E's day-ahead prices (proprietary, licensed per user), ICE hub prices via EIA, and LevelTen, Pexapark and Trio. MISO, SPP and ISO-NE link out until their terms are read or they consent. Nodes are drawn only on evidence (an EIA-860 node designation at an EIA plant, the ERCOT bus join, an OSM match at 0.8 or more after a 100-node hand check); the rest are drawn by county or listed as unplaced. Prices are facts in the site finder, not scored, until the back-test. Spec: `docs/29`. | Prices are what turn a powered site into a priced one: basis and negative hours for generators, spread for storage, delivered cost for large loads. The public sources that allow it cover Texas and the West well. Saying which markets are missing, and why, keeps the credibility the owner is building. |
