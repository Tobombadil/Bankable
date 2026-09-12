# Naming shortlist — five cleared candidates for the platform

**Status:** Phase 1 deliverable, v0.1 · 2026-09-12 · owners: market-researcher + legal-compliance · reviewed by: owner
**Depends on:** `docs/00-PLAN.md` (naming decision row; open question 1 and its clearance checks), `docs/10-prd-mvp.md` §1–§2
(product and audience), `docs/11-market-and-competition.md` §1 (names to avoid), `docs/04-standards.md` D-6/D-7 (distinctive,
not generic). **Settles:** nothing. It ranks; the owner chooses. **Not legal advice:** every trademark line below is
preliminary and must be cleared by counsel before adoption (`docs/13-legal-data-rights.md` §7 counsel list applies).

All checks were run on **2026-09-12 between 17:33 and 17:39 UTC** from this session. Each table cell names the command or
URL. "Unverified" means the tool was blocked, not that the name is clear.

---

## 1. Brief

The name labels a data product: a continuously updated graph of energy and infrastructure **proposals** (interconnection
queues, permits, dockets) and **opportunities** (RFPs, funding, tenders, large-load requests), with a **map as a primary
navigation surface** and a change feed as the product. The audience is industry-native: developers, lenders, utilities,
EPCs, advisors, data-centre operators (`docs/10` §2). The name has to work in four places at once: as a wordmark that can
carry a distinctive typographic identity (D-6: the wordmark comes before the design-reference study), as a domain, as a
social handle on Bluesky, LinkedIn and X (`docs/32`), and in the sentence a lender says to a developer: "saw it on ___."
It also has to survive being said aloud on a call and typed into a browser afterwards without a spelling guess.

**Naming principles applied to the longlist**

1. Short: one to three syllables, ideally one word; ten letters or fewer.
2. Pronounceable and spellable on hearing; no silent letters, no ambiguous vowels.
3. No startup suffixes (-ly, -ify, -io, -ai) and no AI-era abstractions (Nexus, Vertex, Synth, Lumen, Quant).
4. Industry-native or map-native meaning that the audience recognises without explanation; it should sound like a term
   they already use, not a brand aimed at them.
5. No collision with the competitor and adjacent set: Grid Status, GridTracker / Interconnection.fyi, Cleanview,
   Halcyon, Paces, Enverus, Energy Acuity, Energy Adepto, LevelTen, Nira, Anderson Optimization / PVcase, Ember, Modo,
   Kevala, Pearl Street, Voltus, Arcadia, Aurora, Zeitview, Bankable, plus the entrants in `docs/11` §1.3 (Piq, Vela,
   GridUnity, Orennia, LandGate, Transect, Kadoa, datacenterHawk, New Project Media, ZEG, interconnectionqueue.org).
   Corollary: no "Grid-", "Volt-", "Watt-" or "Energy-" prefix; that shelf is full.
6. No live company, product or fund with the same name in energy, infrastructure, finance or data.
7. No unfortunate meaning in Spanish, French, German or Portuguese (the first non-US markets in `docs/00` open question 6).
8. Works as a wordmark: no letter-pairs that fight in a sans (I/l), no more than one descender cluster, sets in caps.

---

## 2. Longlist and cut

Forty-three candidates were generated across three families (Appendix A gives the one-line reason for every rejection).
Ten survived the principles and had a first-pass domain check; five went to full clearance. Two near-finalists were cut
on evidence found during clearance: **Offtake** (Offtake LLC, Vancouver, a supply-contract marketplace that names energy
and carbon buyers as customers — a direct energy-and-finance collision) and **Gazetteer** (a live UK software category,
"gazetteer management system", sold to utilities and local authorities by Idox; Bluesky handle in use). **Kilovolt** and
**Phasor** were cut on multiple live energy companies each.

**Finalists:** Switchyard, Busbar, Isoline, Catchment, Platbook.

---

## 3. Method and tool status

| Check | Tool | Usable today? | Notes |
|---|---|---|---|
| .com registration | `curl -s -o /dev/null -w '%{http_code}' https://rdap.verisign.com/com/v1/domain/NAME.com` | **Yes** | Calibration: `qzxvbankablecontrol7731.com` → **404** (unregistered); every dictionary-word .com below → 200. Full RDAP JSON was pulled for registration date, expiry, status and registrar handle. |
| .energy | `https://rdap.identitydigital.services/rdap/domain/NAME.energy` (server from https://data.iana.org/rdap/dns.json) | **Yes** | Calibration: `paces.energy` → 200 via rdap.org. |
| .io | `https://rdap.identitydigital.services/rdap/domain/NAME.io` | **Yes** | Calibration: `gridstatus.io` → 200. `https://rdap.org/domain/NAME.io` returns 404 even for `gridstatus.io` and `halcyon.io`, so rdap.org results for .io are **not evidence** and were discarded. |
| .co | `https://rdap.nic.co/domain/NAME.co` | **No** (curl exit 000 through the proxy; rdap.org also 404 for the known-registered `cleanview.co`) | **.co unverified for every finalist.** |
| Live site on .com | `curl -s -L -A Mozilla https://NAME.com`, HTTP status + `<title>` | Yes | Distinguishes parked from in-use. |
| Bluesky handle | `curl https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=NAME.bsky.social` | **Yes** | `{"error":"InvalidRequest","message":"Profile not found"}` = free. For taken handles the profile JSON gives `createdAt`, `postsCount`, `followersCount`. Note: Bluesky allows a **domain as handle** (`@NAME.energy`), so a taken `.bsky.social` is a nuisance, not a blocker (`docs/32` account checklist should say this). |
| X handle | WebFetch `https://x.com/NAME` | **No** (HTTP 402 from the fetch proxy for every name) | Fallback `https://syndication.twitter.com/srv/timeline-profile/screen-name/NAME` → HTTP 429 "Rate limit exceeded" on both attempts. **X unverified for every finalist.** |
| LinkedIn company slug | WebFetch `https://www.linkedin.com/company/NAME` | **Yes** (page rendered without login) | 404 = slug free; page = slug taken by the named company. |
| USPTO | WebFetch `https://tmsearch.uspto.gov/` | **No** — JS app shell (only the heading "Trademark search" renders); `POST https://tmsearch.uspto.gov/api-v1-0-0/tmsearch` → HTTP 405 | |
| EUIPO | WebFetch `https://euipo.europa.eu/eSearch/` | **No** — JS app shell ("eSearch plus" heading, no form) | |
| TMview | `https://www.tmdn.org/tmview/api/search/results?...` | **No** — curl exit 000; page is a JS shell | |
| Justia / uspto.report / TrademarkElite | WebFetch | **No** — Justia: Cloudflare 403; uspto.report: 403; TrademarkElite: returns a $349 marketing page, no records | |
| Trademark fallback actually used | WebSearch `site:trademarks.justia.com NAME` and `site:uspto.report NAME trademark` | **Partial** — search-engine index of record pages; finds well-indexed marks, misses recent and dead ones | **Every trademark finding is preliminary; counsel must run USPTO, EUIPO and UKIPO searches in classes 9, 35, 42 before adoption.** |
| Collisions | WebSearch `"NAME" energy company grid solar`, `"NAME" software startup data platform`, `"NAME" fund OR capital OR ventures` | Yes | Plus targeted fetches of any hit. |
| Meaning | Wiktionary (`busbar`, `plat`), Wikipedia (`Isoline (disambiguation)`), general-knowledge cognates flagged (K) | Partial | WordReference returned HTTP 418. |

---

## 4. Finalists

### 4.1 Switchyard  (family A: industry vocabulary)

*The fenced yard of breakers and buses where a generator meets the transmission system: the physical point at which a
proposal becomes part of the grid.* Two syllables, ten letters, spelled as heard. "Saw it on Switchyard" reads naturally.

| Check | Result | Evidence (2026-09-12) |
|---|---|---|
| switchyard.com | **Registered** since 1997-10-03, expires 2028-10-02, registrar IANA id 1068, status `client transfer prohibited`; **no A record** (`getent hosts switchyard.com` empty; `curl https://switchyard.com` → CONNECT 502 / could not resolve). Held, not used. Acquisition enquiry possible. | `curl https://rdap.verisign.com/com/v1/domain/switchyard.com` → 200 |
| switchyard.energy | **Free** | `https://rdap.identitydigital.services/rdap/domain/switchyard.energy` → 404 |
| switchyard.io | Registered | same server → 200 |
| switchyard.co | Unverified | nic.co RDAP unreachable |
| Bluesky `switchyard.bsky.social` | Taken by an empty account: created 2025-10-10, 0 posts, 0 followers, no display name. Domain handle `@switchyard.energy` is the workaround. | `getProfile?actor=switchyard.bsky.social` → profile JSON |
| LinkedIn `/company/switchyard` | **Free** (HTTP 404) | WebFetch https://www.linkedin.com/company/switchyard |
| X `@switchyard` | Unverified (402 / 429) | see §3 |
| Trademark (preliminary, counsel must clear) | Justia index shows: **SWITCHYARD** (Switchyard Holdings, Inc., filed 2015-09-11, beer, class 32); **THE SWITCHYARD** (University of Tulsa, serial 97743069, filed 2023-01-05, downloadable and printed magazines, class 9/16); **TULSA SWITCHYARD** (University of Tulsa, serial 97742897, festivals, class 41). The Tulsa class-9 "downloadable magazines" mark is the one counsel must look at; no class 35/42 software or data mark found. uspto.report index: none for "switchyard". | WebSearch `site:trademarks.justia.com switchyard`: https://trademarks.justia.com/977/43/the-97743069.html, https://trademarks.justia.com/977/42/tulsa-97742897.html |
| Collisions | (1) **Switchyard, healthcare software** ("Modern healthcare software for secure, efficient clinical operations", HIPAA workflow; no energy) — https://www.switchyardai.com/. Same-name software in another field: a class-42 likelihood-of-confusion question for counsel. (2) **Switchyard-1**, a modular AI data centre by Platform DC, Warrington UK, launching Q3 2026, 4–8 MW — a facility name inside the Platform DC brand, not a brand — https://www.platformdc.io/switchyard-1. Energy-adjacent; note it. (3) Two open-source frameworks, both dormant: JBoss SwitchYard (SOA runtime, https://github.com/jboss-switchyard/switchyard) and Colgate's Switchyard network-teaching framework (https://github.com/jsommers/switchyard). (4) No fund, utility, developer or energy data product found (`"Switchyard" energy company grid solar` returns only technical pages about switchyards; `"Switchyard" fund OR capital OR ventures` returns nothing by that name). | WebSearch, three queries |
| Meaning | No meaning in ES/FR/DE/PT; "switch" is an English loanword in all four; DE technical term is *Schaltanlage*. No unfortunate reading. (K) | |
| Pronounceability | High. Risk: written as two words ("switch yard"). | |
| Wordmark | Long but balanced; one descender (y) mid-word; "tch" cluster needs a face with a tight t–c–h. Sets well in caps and in a slab. Monogram "SY". The object itself (buses, breakers, a fenced grid) is a native mark motif that is not a lightning bolt. | |

### 4.2 Busbar  (family A: industry vocabulary)

*The conductor bar that everything in a substation connects to; the thing a generator's output and a load's draw both
touch.* Two syllables, six letters. "Saw it on Busbar."

| Check | Result | Evidence (2026-09-12) |
|---|---|---|
| busbar.com | **Registered and in use**: since 1997-12-15, expires 2026-12-14, registrar id 1052; `http://busbar.com` → 301 to https://www.mersen.com/en/products/bus-bar (Mersen, a busbar manufacturer). Not acquirable in practice. | RDAP → 200; `curl -I http://busbar.com` |
| busbar.energy | **Free** | identitydigital RDAP → 404 |
| busbar.io | Registered | → 200 |
| busbar.co | Unverified | |
| Bluesky `busbar.bsky.social` | Taken by an **active** account: created 2024-11-15, 27 posts, 25 followers. | getProfile JSON |
| LinkedIn `/company/busbar` | **Taken**: Guangzhou Cinderellon Enterprise LTD, "bus bar, cooling plate, heat sink", 501–1,000 employees, Guangzhou. | WebFetch |
| X `@busbar` | Unverified | |
| Trademark (preliminary, counsel must clear) | The word is a generic product name in classes 9/17, so the register is crowded with goods marks that *contain* it: EBUSBAR (Shenzhen Busbar Technology, serial 79105219, class 9); an nVent Services GmbH filing of March 2026 for busbar conductors; CROSSBOX (Wöhner) and SIFANG (Beijing Sifang) recite busbars in their goods. For a data service the word is arbitrary, not descriptive, which helps registrability in class 42; the crowding is in goods, not services. No class 35/42 "Busbar" mark found. | WebSearch `site:trademarks.justia.com busbar`: https://trademarks.justia.com/791/05/ebusbar-79105219.html |
| Collisions | (1) **Busbar Technologies**, Hyderabad, PeopleSoft/MuleSoft services (https://www.zoominfo.com/c/busbar-technologies/556994401). (2) **Easy Busbar**, EAE Electric's cloud CAD for busbar products (https://www.easybusbar.com/en.html). (3) Mersen at busbar.com. (4) No energy developer, utility, data product or fund named Busbar (`"Busbar" fund OR capital OR ventures OR "Busbar Energy" OR "Busbar Power"` → nothing). | WebSearch, three queries |
| Meaning | ES *embarrado / barra colectora*, FR *jeu de barres / barre omnibus*, DE *Sammelschiene* (Wiktionary, https://en.wiktionary.org/wiki/busbar); PT *barramento* (K). Read as English it is "bus" + "bar" — a bar on a bus — in all four languages: comic, not offensive. German engineers use "Busbar" as a loanword (K). | |
| Pronounceability | High. Variants "bus bar", "buss bar" exist in US trade writing. | |
| Wordmark | The strongest of the five: six letters, b–b symmetry, no descenders, and the literal object is a horizontal bar, which is the whole mark. | |

### 4.3 Isoline  (family C: map-native, with an industry pun)

*A line on a map joining points of equal value (isobar, isotherm, contour); read by this audience as "ISO line" — the
grid operators whose queues are the supply side of the product.* Three syllables, seven letters.

| Check | Result | Evidence (2026-09-12) |
|---|---|---|
| isoline.com | **Registered and in use**: since 2001-06-01, expires 2027-06-01, registrar id 69; redirects to https://www.isoline.eu/ ("ISOline EU, s.r.o", Czech; title "Dáváme vám víc než napít" — a beverage/vending business). | RDAP → 200; curl follow → 200 |
| isoline.energy | **Free** | → 404 |
| isoline.io | Registered | → 200 |
| isoline.co | Unverified | |
| Bluesky `isoline.bsky.social` | Taken by an active personal account: display name "Isoline", created 2024-12-06, 31 followers, bio "Co-Founder @try-OLI.com | Busy mum of 3". | getProfile JSON |
| LinkedIn `/company/isoline` | **Taken**: ISOLINE, Timișoara, Romania, thermal-insulation products, founded 2015, 2–10 employees. `/company/isoline-ltd` is a second company (below). | WebFetch |
| X `@isoline` | Unverified | |
| Trademark (preliminary, counsel must clear) | **ISOLINE — registered**, Frenzelit GmbH, reg. no. 5815770, serial 79234064, filed 2018-03-19, packing and insulating materials (class 17). Different class from 9/35/42, but a live identical word mark exists on the US register. uspto.report index: no "ISOLINE"; nearest IOLINE (Ioline Corporation). | WebSearch `site:trademarks.justia.com isoline`: https://trademarks.justia.com/792/34/isoline-79234064.html |
| Collisions | (1) **Isoline Ltd**, London, incorporated 2012-10-11 (Companies House 08248520), cloud planning, software architecture and security assurance — **class-42 use of the identical name in software services** (https://find-and-update.company-information.service.gov.uk/company/08248520, http://www.isolineltd.com/, https://www.linkedin.com/company/isoline-ltd). (2) Isoline Communications, a SaaS marketing agency (https://www.isolinecomms.com/). (3) Isoline (Romania, insulation) and ISOLINE (food supplements, CB Insights). (4) "Isoline map" is a CARTO/Atlas feature name, i.e. a generic GIS term (https://carto.com/isoline-map/). (5) No energy company, fund or data product named Isoline. | WebSearch, four queries |
| Meaning | FR *isoligne*, ES *isolínea*, DE *Isolinie*, PT *isolinha* — cognate, technical, neutral (K). Also an 1888 opera by André Messager and a French given name (https://en.wikipedia.org/wiki/Isoline_(disambiguation)). Nothing unfortunate. | |
| Pronounceability | Medium-high: EYE-so-line. On hearing, may be typed "Isolene" or "Isolean". | |
| Wordmark | Thin letters: I, s, o, l, i, n, e — no descenders, the mark is literally a line. **Typographic trap:** capital I and lowercase l are indistinguishable in many grotesques ("Iso**l**ine" vs "**I**soline"); the design-reference study (D-6) must pick a face with a differentiated I/l or set it in lowercase with a distinctive l. | |

### 4.4 Catchment  (family B: landscape)

*The area from which everything drains to one point.* Two syllables, nine letters. Metaphor for the product: every
public signal about a project flows into one record. "Saw it on Catchment."

| Check | Result | Evidence (2026-09-12) |
|---|---|---|
| catchment.com | **Registered, for sale**: since 1999-08-05, expires 2027-08-05, registrar id 146 (GoDaddy), all four client locks; site redirects to https://forsale.godaddy.com/forsale/catchment.com (GoDaddy aftermarket listing; page returned 403 to curl but the redirect target is explicit). Buyable at aftermarket price. | RDAP → 200; curl follow → 403 at forsale.godaddy.com |
| catchment.energy | **Free** | → 404 |
| catchment.io | Registered | → 200 |
| catchment.co | Unverified | |
| Bluesky `catchment.bsky.social` | Taken: created 2024-08-15, 0 posts, 58 followers, no display name (dormant). | getProfile JSON |
| LinkedIn `/company/catchment` | **Taken**: Catchment, Irish local-business marketing agency, catchment.ie, 2–10 employees. | WebFetch |
| X `@catchment` | Unverified | |
| Trademark (preliminary, counsel must clear) | Only descriptive uses found: "non-metal catchment basins" (National Diversified Sales, serial 76703114; ECORAIN AMERICA, serial 86669017), "rainwater catchment system" (BlueBarrel, serial 98859475), Condamine Catchment NRM Corporation (education, class 41, serial 79031234). No "CATCHMENT" word mark in class 9/35/42 found. uspto.report index: none. | WebSearch `site:trademarks.justia.com catchment` |
| Collisions | (1) **Catchment Capital, L.P.**, New York, private-equity firm founded 2024, SEC-registered investment adviser 2025 (CRD 335631), $148M AUM, middle-market industrial technology, services and products; not energy-focused but a **finance-sector name collision** in the lender segment we sell to (https://www.catchmentcapital.com/, https://adviserinfo.sec.gov/firm/summary/335631, https://www.linkedin.com/company/catchmentcapital). (2) Catchment (Ireland) marketing agency. (3) "Catchment analysis" is retail-geography jargon (Atlas Mapping's "Vision" tool: https://insight.atlas-mapping.com/blog/catchment-area-calculation). (4) No energy company or data product; `"Catchment Energy" OR "Catchment Data"` → nothing. | WebSearch, three queries |
| Meaning | No cognate in ES (*cuenca*), FR (*bassin versant*), DE (*Einzugsgebiet*), PT (*bacia*) (K); reads as an English word with no meaning to non-English speakers, and no unfortunate one. | |
| Pronounceability | High. | |
| Wordmark | Nine letters, "tchm" cluster is the only awkward spot; no descenders; reads as one unit. Less distinctive shape than Busbar or Switchyard; the metaphor needs the map to explain it. | |

### 4.5 Platbook  (family C: map register, coined compound)

*A plat book is the county map book of land ownership and parcel boundaries that US developers, lenders and title
companies still buy; "plat" is the survey map itself.* Two syllables, eight letters. The most map-first of the five.
"Saw it on Platbook."

| Check | Result | Evidence (2026-09-12) |
|---|---|---|
| platbook.com | **Registered, parked**: since 2002-06-02, expires 2028-06-02, registrar id 146 (GoDaddy), all four client locks, last changed 2026-09-02; the page is a 114-byte redirect to `/lander` (GoDaddy parking). Likely acquirable via broker. `platbooks.com` also registered. | RDAP → 200; `curl -L https://platbook.com` → 200, body `window.location.href="/lander"` |
| platbook.energy | **Free** | → 404 |
| platbook.io | **Free** | identitydigital RDAP → 404 (calibrated against gridstatus.io → 200) |
| platbook.co | Unverified | |
| Bluesky `platbook.bsky.social` | **Free** | `{"error":"InvalidRequest","message":"Profile not found"}` |
| LinkedIn `/company/platbook` | **Taken**: PlatBook, Paris, founded 2024, 2–10 employees, "the intelligent menu scanner for travelers" (the name is the French pun *plat* = dish). Unrelated field; a second slug (`platbook-data`, `platbookhq`) would be needed. | WebFetch |
| X `@platbook` | Unverified (syndication returned rate-limit text) | |
| Trademark (preliminary, counsel must clear) | **Nothing found** in the Justia index for "platbook" or "plat book"; nearest are PLAT (serial 88524908) and PLAT.ONE (serial 86302166), both unrelated, and abandoned PLATYBOOKS (children's books). Counsel must check the French PlatBook company for an EU or French filing (INPI). | WebSearch `site:trademarks.justia.com platbook OR "plat book"`, `"Platbook" trademark` |
| Collisions | (1) PlatBook (Paris menu app) as above. (2) "Plat book" is a generic product category published by Rockford Map Publishers, Mapping Solutions, Farm & Home Publishers and county GIS offices (https://rockfordmap.com/product/plat-book/, https://www.mappingsolutionsgis.com/plat-books/); Rockford sells "MobilePlat". Descriptive-use risk for map products specifically: a mark on a *map data product* called Platbook is closer to the generic meaning than the other four are to theirs; counsel to assess descriptiveness. (3) No energy company, fund or software product named Platbook. | WebSearch, three queries |
| Meaning | FR *plat* = flat, dull; also a dish or course (Wiktionary https://en.wiktionary.org/wiki/plat). A French speaker hears "flat book" or "dish book" — mildly comic, and already used as a pun by the Paris app. DE *platt* = flat; ES *plato* / PT *prato* = dish (K). Nothing offensive. Outside US land work the term "plat" is unknown; the UK/EU audience will hear a coined word. | |
| Pronounceability | High. May be heard as "Plot book" (harmless) or "Flat book". | |
| Wordmark | Eight letters, ascenders (l, t, b, k) and no descenders; sits on a baseline like a book spine. The gridded township page is a native motif. "Book" suggests a static publication; the feed has to be shown, not implied. | |

---

## 5. Comparison

| Name | Family | .com | .energy / .io | Bluesky `.bsky.social` | LinkedIn slug | Trademark (preliminary, counsel must clear) | Collisions | Pronounce / spell on hearing | Wordmark |
|---|---|---|---|---|---|---|---|---|---|
| **Switchyard** | industry | Registered 1997, no DNS, held not used; acquisition enquiry | free / registered | taken, empty (0 posts) | **free** | Beer mark (cl. 32); Tulsa magazine (cl. 9/16) and festival (cl. 41) marks; no cl. 35/42 found | Healthcare software "Switchyard" (switchyardai.com); Platform DC facility "Switchyard-1"; two dormant OSS frameworks; no energy/finance | High / high | Long, balanced, one descender; strong object motif |
| **Busbar** | industry | Registered 1997, **in use** (Mersen) | free / registered | taken, active (27 posts) | taken (Chinese busbar maker) | Generic goods term: many cl. 9 marks contain it (EBUSBAR, nVent 2026); arbitrary for services; no cl. 35/42 found | Busbar Technologies (IT services, India); Easy Busbar (CAD); no energy data/finance | High / high | Best of five: 6 letters, symmetric, the object is a bar |
| **Isoline** | map + pun | Registered 2001, **in use** (Czech beverage) | free / registered | taken, active personal | taken (Romanian insulation) | **ISOLINE registered** (Frenzelit, cl. 17, reg. 5815770) | **Isoline Ltd, London, software/cloud consulting (cl. 42 use)**; Isoline Communications; generic GIS term | Med-high / medium | Thin, elegant; **I/l ambiguity** |
| **Catchment** | landscape | Registered 1999, **for sale** (GoDaddy) | free / registered | taken, dormant (0 posts) | taken (Irish agency) | Only descriptive goods uses (basins, rainwater); no word mark in 9/35/42 found | **Catchment Capital** (NY PE, SEC-registered 2025); Irish agency; retail-geo jargon | High / high | Plain; metaphor needs the map |
| **Platbook** | map register | Registered 2002, **parked** (GoDaddy lander) | **free / free** | **free** | taken (Paris menu app) | Nothing found; counsel to check FR/EU for PlatBook | PlatBook (Paris); "plat book" is a generic map-product category (descriptiveness risk for a map product) | High / high | Book-spine shape; "book" reads static |

X is unverified for all five. .co is unverified for all five.

---

## 6. Recommendation (ranked, not chosen)

1. **Switchyard.** The only finalist whose LinkedIn slug is free, whose .com is held but dead (an acquisition enquiry is
   realistic), whose .energy is free, and whose name carries a precise industry meaning — the point where a proposal
   physically joins the grid — that also says "map". Its collisions are outside energy and finance (a healthcare SaaS, a UK
   data-centre hall, two dead frameworks); the Tulsa class-9 magazine mark and the healthcare product are the two things
   counsel must look at. The empty Bluesky account is bypassed with a domain handle.
2. **Platbook.** Cleanest availability of the five (Bluesky, .io and .energy all free; no marks found; .com parked and
   probably buyable) and the most literally map-first. It ranks second, not first, because "plat book" is an existing map
   product category — counsel must assess descriptiveness for a map data product — because a Paris app already uses the
   name with the French pun, and because "book" implies a static publication that the change-feed product must overcome
   in the design.
3. **Busbar.** The best wordmark and the most industry-native word, but its .com is an operating industrial site (Mersen),
   the LinkedIn slug and an active Bluesky account are taken, and the register is crowded with goods marks containing the
   word. It works on .energy with a domain handle; the owner should weigh a great mark against never owning busbar.com.
4. **Isoline.** The cleverest name (contour line and "ISO line") and the most elegant to set, but it carries a live
   identical US registration (Frenzelit, class 17), an identically named London software consultancy in the class-42 space
   we would file in, taken handles on both networks, and an I/l typographic trap. Counsel's view on Isoline Ltd decides
   whether it stays on the list.
5. **Catchment.** Sound metaphor, clean register, buyable .com — but Catchment Capital is a SEC-registered New York PE
   firm founded 2024 and our second segment is lenders and investors; a name shared with a fund in that room is a
   recurring confusion, not a one-off clearance problem. It stays on the list as the fallback if the industry words fail
   clearance.

Before adoption, whichever name the owner picks: (a) counsel runs USPTO, EUIPO and UKIPO knock-out searches in classes
9, 35 and 42 and reviews the named collisions; (b) register the .energy immediately and open the .com enquiry; (c) create
the Bluesky account on the domain handle and reserve a LinkedIn slug; (d) verify X by hand (blocked to tooling today);
(e) then run the `{{PRODUCT}}` / `{{DOMAIN}}` replacement described in `docs/00-PLAN.md`.

---

## Appendix A — Longlist (43) with rejection reasons

(E) = evidence gathered today with URL or command; (K) = general knowledge, not verified today.

**Family A — industry vocabulary**

| Candidate | Outcome |
|---|---|
| Switchyard | **Finalist** |
| Busbar | **Finalist** |
| Offtake | Cut at clearance: Offtake LLC, Vancouver, supply-contract marketplace naming energy and carbon-credit buyers (E: https://www.zoominfo.com/c/offtake-llc/1326480859 via search summary); offtake.com registered 2005 (E). Bluesky and LinkedIn were free — it hurts to lose. |
| Docket | Halcyon already owns "what the docket says" (`docs/11` §1.2); legal-tech products named Docket (K); docket.com registered (E). |
| Queue | Generic; interconnectionqueue.org and ZEG Queue Tracker already occupy it (`docs/11` §1.1). |
| Tariff | Generic; 2025–26 trade-tariff news noise (K). |
| Substation | Three syllables; reads as Substack; Halcyon sells a Substation Tracker (`docs/11` §1.1); substation.com registered (E). |
| Corridor | Generic; corridor.com registered (E). |
| Feeder | Ambiguous on hearing (RSS feeder, livestock). |
| Nameplate | Generic noun in hardware and signage; nameplate.com registered (E). |
| Kilovolt | Kilovolt Engineering (GA), KiloVolt (Poland, HV test equipment), Kilovolt Energy Solutions LLP (India) (E: https://kilovoltengineering.com/, https://hvkilovolt.com/aboutus-en/); Bluesky taken (E). |
| Phasor | Phasor Energy Company (PV, 1994), Phasor Informatics (grid-oscillation software), Phasor Corp (E: https://phasorinformatics.com/, https://phasorcorp.com/); Bluesky taken (E). |
| Setpoint | Setpoint, capital-markets fintech (K); setpoint.com registered (E). |
| Tieline | Tieline Technology, broadcast audio codecs (K); tieline.com registered (E). |
| Cutover | Cutover, work-orchestration SaaS (K); cutover.com registered (E). |
| Loadshape | "Load" heard as a verb; compound; loadshape.com registered (E). |
| Wayleave | UK-only term; the US audience will not know it. (Bluesky free, E.) |
| Trunkline | Trunkline Gas pipeline (Energy Transfer) (K). |
| Span | Span.io smart panels (K); prefix collision with the electrical-hardware shelf. |
| Tiepoint | Coined survey/tie-line compound; sounds like "tie point" two words; tiepoint.com registered (E). |

**Family B — place and landscape**

| Candidate | Outcome |
|---|---|
| Catchment | **Finalist** |
| Meridian | Meridian Energy (NZ) and dozens of others (K). |
| Cairn | Cairn Energy (UK oil, now Capricorn) (K). |
| Ridgeline | Ridgeline, asset-management software (K); ridgeline.com registered (E). |
| Watershed | Watershed, climate software (K). |
| Confluence | Atlassian (K). |
| Mesa | Mesa Natural Gas Solutions, Mesa Labs (K). |
| Headland | No industry hook; soft. |
| Tarn | Obscure; a French département. |
| Shoal | Connotes running aground. |
| Moraine | Obscure; spelling on hearing. |
| Easement | Land-rights term relevant to transmission but legalistic; easement.com registered (E). |

**Family C — map register, abstract, coined**

| Candidate | Outcome |
|---|---|
| Platbook | **Finalist** |
| Isoline | **Finalist** |
| Gazetteer | Cut at clearance: "gazetteer management system" is a live UK software category sold to utilities and councils by Idox (E: https://www.idoxgroup.com/solutions/address-data-solutions/llpg-lsg/); Bluesky taken by an active account (E); double-t double-e spelling. |
| Cadastre | Generic land-register term in all four languages; PT-BR *cadastro* = sign-up form (K); cadastral software category (E: https://geo-plus.com/cadastre-software/); descriptive for a map product. |
| Graticule | Graticule Asset Management, hedge fund (K); three syllables, hard spelling. |
| Datum | Datum Datacentres (UK) (K). |
| Fieldbook | Fieldbook, startup acquired by Flexport (K); fieldbook.com registered (E). |
| Gridfold | "Grid-" prefix collides with Grid Status, GridTracker, GridUnity; gridfold.com registered (E). |
| Voltmark | "Volt-" prefix crowded (Voltus, Volta); voltmark.com registered (E). |
| Wattline | "Watt-" prefix crowded; wattline.com registered (E). |
| Loadbook / Queuebook / Docketwatch | Descriptive compounds; all three .coms registered (E). |
| Almanac | almanac.com is the Old Farmer's Almanac (K). |
| Ledger | Ledger, crypto hardware wallets (K). |
| Quorum | Quorum Software, energy software (K). |
| Pylon | Pylon, customer-support software (K). |
| Lattice | Lattice, HR software (K). |

---

## Appendix B — Raw command log (2026-09-12, UTC)

```
17:33  for n in <24 longlist>; curl -s -o /dev/null -w '%{http_code}' https://rdap.verisign.com/com/v1/domain/$n.com   → all 200
17:34  control: qzxvbankablecontrol7731.com → 404
17:34  RDAP JSON pulled for switchyard, busbar, isoline, catchment, platbook, offtake, gazetteer (.com): status, events, registrar handle
17:34  curl -s -L https://<name>.com → switchyard 000 (no DNS), busbar 000 (TLS reset; http → 301 mersen.com), isoline → isoline.eu 200,
       catchment → forsale.godaddy.com 403, platbook 200 (114-byte /lander redirect), offtake 200 (empty), gazetteer 200 (empty)
17:34  public.api.bsky.app getProfile for seven names; retried isoline 17:35 (profile returned)
17:35  https://data.iana.org/rdap/dns.json → .energy server rdap.identitydigital.services; .io/.co absent from bootstrap
17:38  rdap.org calibration: gridstatus.io 404, halcyon.io 404, cleanview.co 404 (=> rdap.org unusable for .io/.co); paces.energy 200
17:38  identitydigital: gridstatus.io 200 (calibration); switchyard/busbar/isoline/catchment .io 200; platbook.io 404; all five .energy 404
17:38  https://rdap.nic.co/domain/<name>.co → curl 000 (unreachable)         => .co unverified
17:35–17:39  WebFetch https://x.com/<name> → HTTP 402 ×5; syndication.twitter.com timeline-profile → 429 ×2 rounds   => X unverified
17:35  WebFetch https://www.linkedin.com/company/<name> → switchyard 404, offtake 404; busbar, isoline, catchment, platbook = company pages
17:35  WebFetch https://tmsearch.uspto.gov/ → JS shell; POST api-v1-0-0/tmsearch → 405; https://euipo.europa.eu/eSearch/ → JS shell
17:36–17:37  Justia 403 (Cloudflare), uspto.report 403, trademarkelite marketing page, tmdn.org 000
17:38  WebSearch site:trademarks.justia.com <name> ×5; site:uspto.report <name> trademark ×3
17:34–17:37  WebSearch collision queries ×3–4 per finalist; targeted WebFetch switchyardai.com, platformdc.io/switchyard-1
17:39  Wiktionary busbar, plat; Wikipedia Isoline (disambiguation); WordReference → 418
```

---

# Round two — "an infrastructure map and project/proposals tracker"

**Run 2026-09-12, inline (no subagent). Brief from the owner:** Switchyard was the right register — concrete,
industry-native — but too oblique. The name should let a lender, a utility procurement lead or a data-centre
siting manager guess within two seconds that this is a map of infrastructure projects you can track over time.

## 7. What round two learned, before the candidates

The two-second test and single dictionary words are close to incompatible in 2026. Every word that fuses *map*
and *register* cleanly is already an operating software company, and several sit in adjacent sectors where a
buyer would confuse them with us. Checked and knocked out, with the collision:

| Candidate | Why it was knocked out | Evidence |
|---|---|---|
| Buildout | Buildout Inc., commercial-real-estate software, ~50,000 brokers, Riverside portfolio company | buildout.com |
| Cadastre / Cadastral | Cadastral Inc., AI software for CRE, raised $9.5M late 2025 (JLL Spark, AvalonBay) | press, 2026 |
| Plotline | Plotline, customer-engagement SaaS, founded 2021, $2.6M raised | pitchbook, capterra |
| Gridline | Gridline Energy Group, Gridline Energy Solutions Ltd (UK), Gridline Power — **same sector** | companies house, linkedin |
| Wayfinder | Wayfinder Energy (advisory), Wayfinder Resources (upstream O&G), Sofar Ocean Wayfinder — **same sector** | wayfinder-energy.com |
| Groundwork | GroundWork Open Source (IT monitoring), Groundwork contractor sales software | gwos.com, hellogroundwork.com |
| Siteline | Siteline, construction billing software, founded 2019 | siteline.com |
| Grid Atlas | gridatlas.com live (Canadian land mapping), gridatlas.org, a UK "GridAtlas" tracking renewable projects from planning registers — **near-identical product** | round-two first pass |
| Waymark | a16z-backed healthcare software marks | round-two first pass |
| Prospect | PVcase Prospect / Anderson Optimization — **direct competitor** | `docs/11` §1 |

The conclusion is not that no name exists. It is that a *compound* is now the honest path: two short words whose
combination is self-explaining and whose collision surface is far smaller than either word alone.

## 8. Round-two candidates, checked

`.com` is registered for every pronounceable English compound; that is normal and not informative on its own.
What matters is whether the `.com` **resolves to a live product**, and whether the handle and the register are clear.
Method as round one: Verisign RDAP for `.com` registration, an HTTPS fetch to test whether it is actually in use,
the Bluesky public API for the handle, and search-engine indexing for collisions and preliminary marks.
Checks run 2026-09-12.

| Name | Reads as | `.com` live? | Bluesky | Collisions found | Verdict |
|---|---|---|---|---|---|
| **Gridslate** | the slate of projects on the grid | **no response — parked/dead** | **free** | none in software; "grid slate" returns wallpaper and craft products only | **Cleanest of the set** |
| **Siteplot** | plotting project sites | **no response — parked/dead** | **free** | none found in energy, construction or data | Clean, less evocative |
| **Gridmap** | a map of the grid | no response — parked/dead | free | no single owner, but the phrase is a generic technical term | Descriptiveness risk; hard to protect |
| Platmap | a parcel map | **live** | free | platmap.app (free plat maps and property records), platmap.com "coming soon", PlatWidget, Plat Mapper AI — crowded in real-estate plat mapping | Too crowded |
| Siteatlas | an atlas of sites | **live** | **taken** | siteatlas.co.uk live, @SiteAtlas on X | Dead |
| Projectmap | literal | no response | **taken** | — | Handle gone |

Alternate TLD results are **unusable for this round**: the Identity Digital RDAP endpoint rate-limited
(HTTP 429 on every query including the controls), so `.energy` and `.io` status for these six is **unverified**
and must be re-checked before adoption.

## 9. Recommendation, round two

**Gridslate** is the only candidate that is simultaneously clear, uncrowded and available. "Slate" is ordinary
English for a set of things scheduled or proposed — *a slate of projects* — which is precisely what the platform
tracks, and it is a word a lender uses without translation. "Grid" supplies the sector. The `.com` is registered
but parked with no service behind it, so it is an acquisition enquiry rather than a blocker, and the Bluesky
handle is free. Its weakness is that it carries the *tracker* half of the brief better than the *map* half; the
map has to be shown, not named.

**Siteplot** is the fallback: equally clean, carries the map half better ("plot" is both a parcel and the act of
placing something on a map), but "site" is weaker than "grid" at naming the sector and the word is flatter.

Neither is recommended for adoption before: `.energy` and `.io` re-checked once the registry stops rate-limiting;
a counsel knock-out search in classes 9, 35 and 42; and an enquiry on the parked `.com`. Both are safe to use
immediately as the **placeholder** that unblocks the design references study, which is all that is needed today.

## 10. Longlist tested in round two

Checked and free on Bluesky, not shortlisted: buildwatch, gridledger, buildledger, projectledger, gridroster,
plotworks, gridcanvas, buildatlas, powerplat, siteplot, gridslate, gridmap, platmap, projectmap.
Checked and taken or ambiguous: gridplot, plotgrid, buildmap, mapworks, siteatlas (all returned taken, though
three of those checks hit transport timeouts and should be re-run before relying on them).
Rejected on meaning before checking: Sitemap (web jargon), Pipeline (collides with gas pipelines in our own
subject matter), Horizon and Compass (generic finance), Trailhead (Salesforce), Landgrid (live parcel-data firm).
