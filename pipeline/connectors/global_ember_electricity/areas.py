"""Area keys for Ember's geographies: `world`, a region slug, or a country's ISO 3166-1 alpha-2 code.

Ember names a country by its ISO 3166-1 alpha-3 code (`entity_code` in the API, the ISO 3 column in the
CSV) and an aggregate (World, a continent, the EU, the OECD, the G20, ...) by name only. The rest of the
platform keys countries by alpha-2 (`country=US`), so a country becomes its alpha-2 code here; an
alpha-3 code this table does not know keeps its alpha-3 form (three letters, so it can never collide
with an alpha-2 key) and the run's checks list it. A region becomes a lowercase slug of Ember's name
(`latin-america-and-caribbean`, `eu`, `g20`); `EU` is not an assigned alpha-2 code, so `area=EU` still
reads as the region.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ISO 3166-1 alpha-3 -> alpha-2 (the 249 assigned codes), plus Kosovo's user-assigned XKX -> XK, which
#: Ember and the World Bank use.
_ISO_TABLE = """
ABW AW AFG AF AGO AO AIA AI ALA AX ALB AL AND AD ARE AE ARG AR ARM AM ASM AS ATA AQ ATF TF ATG AG
AUS AU AUT AT AZE AZ BDI BI BEL BE BEN BJ BES BQ BFA BF BGD BD BGR BG BHR BH BHS BS BIH BA BLM BL
BLR BY BLZ BZ BMU BM BOL BO BRA BR BRB BB BRN BN BTN BT BVT BV BWA BW CAF CF CAN CA CCK CC CHE CH
CHL CL CHN CN CIV CI CMR CM COD CD COG CG COK CK COL CO COM KM CPV CV CRI CR CUB CU CUW CW CXR CX
CYM KY CYP CY CZE CZ DEU DE DJI DJ DMA DM DNK DK DOM DO DZA DZ ECU EC EGY EG ERI ER ESH EH ESP ES
EST EE ETH ET FIN FI FJI FJ FLK FK FRA FR FRO FO FSM FM GAB GA GBR GB GEO GE GGY GG GHA GH GIB GI
GIN GN GLP GP GMB GM GNB GW GNQ GQ GRC GR GRD GD GRL GL GTM GT GUF GF GUM GU GUY GY HKG HK HMD HM
HND HN HRV HR HTI HT HUN HU IDN ID IMN IM IND IN IOT IO IRL IE IRN IR IRQ IQ ISL IS ISR IL ITA IT
JAM JM JEY JE JOR JO JPN JP KAZ KZ KEN KE KGZ KG KHM KH KIR KI KNA KN KOR KR KWT KW LAO LA LBN LB
LBR LR LBY LY LCA LC LIE LI LKA LK LSO LS LTU LT LUX LU LVA LV MAC MO MAF MF MAR MA MCO MC MDA MD
MDG MG MDV MV MEX MX MHL MH MKD MK MLI ML MLT MT MMR MM MNE ME MNG MN MNP MP MOZ MZ MRT MR MSR MS
MTQ MQ MUS MU MWI MW MYS MY MYT YT NAM NA NCL NC NER NE NFK NF NGA NG NIC NI NIU NU NLD NL NOR NO
NPL NP NRU NR NZL NZ OMN OM PAK PK PAN PA PCN PN PER PE PHL PH PLW PW PNG PG POL PL PRI PR PRK KP
PRT PT PRY PY PSE PS PYF PF QAT QA REU RE ROU RO RUS RU RWA RW SAU SA SDN SD SEN SN SGP SG SGS GS
SHN SH SJM SJ SLB SB SLE SL SLV SV SMR SM SOM SO SPM PM SRB RS SSD SS STP ST SUR SR SVK SK SVN SI
SWE SE SWZ SZ SXM SX SYC SC SYR SY TCA TC TCD TD TGO TG THA TH TJK TJ TKL TK TKM TM TLS TL TON TO
TTO TT TUN TN TUR TR TUV TV TWN TW TZA TZ UGA UG UKR UA UMI UM URY UY USA US UZB UZ VAT VA VCT VC
VEN VE VGB VG VIR VI VNM VN VUT VU WLF WF WSM WS YEM YE ZAF ZA ZMB ZM ZWE ZW XKX XK
"""
_PAIRS = _ISO_TABLE.split()
ISO3_TO_ISO2: dict[str, str] = dict(zip(_PAIRS[0::2], _PAIRS[1::2], strict=True))
ISO2_TO_ISO3: dict[str, str] = {two: three for three, two in ISO3_TO_ISO2.items()}

WORLD_ID = "world"
AREA_KINDS = ("world", "region", "country")
_SLUG = re.compile(r"[^a-z0-9]+")
_ALPHA3 = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    kind: str  # world | region | country
    iso3: str | None


def region_slug(name: str) -> str:
    """`Latin America and Caribbean` -> `latin-america-and-caribbean`; `EU` -> `eu`."""
    text = name.strip().lower().replace("&", " and ")
    return _SLUG.sub("-", text).strip("-")


def country_code(code: object) -> str | None:
    """`code` as an ISO 3166-1 alpha-3 code: any three letters as given (`USA`, or an unassigned code
    kept as its own key), or an assigned alpha-2 code turned to alpha-3 (`US` -> `USA`); else None."""
    text = str(code or "").strip().upper()
    if _ALPHA3.match(text):
        return text
    return ISO2_TO_ISO3.get(text) if len(text) == 2 else None


def area_for(name: str, code: str | None, *, is_aggregate: bool | None) -> Area:
    """The area one Ember row is about. `is_aggregate` is Ember's own flag when the row carries one
    (`is_aggregate_entity`); without it, a row with a country code (three letters, or an assigned
    alpha-2) is a country and any other row is World (by name) or a region. A row Ember flags as a
    country but that carries no usable code stays a region here; the parsers count it
    (`ParseReport.uncoded_countries`) and the run's checks warn, since it drops out of coverage and
    the rankings."""
    label = " ".join(str(name or "").split())
    iso3 = country_code(code)
    if label.lower() == WORLD_ID:
        return Area(WORLD_ID, "World", "world", None)
    country = iso3 is not None if is_aggregate is None else not is_aggregate
    if country and iso3:
        return Area(ISO3_TO_ISO2.get(iso3, iso3), label or iso3, "country", iso3)
    return Area(region_slug(label), label, "region", None)


def unknown_iso3(codes: set[str]) -> list[str]:
    """The alpha-3 codes in `codes` this table does not map (kept as their own key; the run warns)."""
    return sorted(c for c in codes if c and c not in ISO3_TO_ISO2)
