"""Shared helpers for demand-side (opportunity) connectors — docs/21 §3.3 and §7.2."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
from typing import Any

import pandas as pd

OPPORTUNITY_STATES = (
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
)
KINDS = ("rfp", "foa", "tender", "auction", "loan_program", "procurement_notice", "program")

#: The tag of a solicitation open to every technology: a utility's all-source or capacity RFP that
#: names no technology (docs/28 B-1, B-7). It matches every value of the technology filter
#: (`services/api/records.py`) and passes the matcher's technology rule against any proposal
#: (`data/match_rules.yaml` `all_source_tokens`). It is not one of `OPPORTUNITY_TECHNOLOGIES`: a
#: reader does not choose "all-source", every technology they choose includes these notices. An
#: EMPTY list is different: it means no technology was read from the notice, and since docs/28
#: B-1 an untagged notice matches no technology.
ALL_SOURCE = "all_source"

#: How a solicitation names itself open to every resource (docs/28 B-1, B-7; review of 2026-10-24,
#: finding 1). "all-source", or a capacity solicitation named as one ("2025 SPP Capacity RFP",
#: "Request for Proposals for Capacity Resources"); never the bare word "capacity", which every
#: utility RFP's text carries ("by the 2029/30 capacity planning year"). Read from the title.
ALL_SOURCE_PHRASE = r"\ball[\s-]+source\b"
CAPACITY_SOLICITATION = (
    r"\bcapacity\s+(?:rfps?|rfos?|requests?\s+for\s+(?:proposals?|offers?|bids?)|solicitations?"
    r"|procurements?|auctions?|tenders?)\b"
    r"|\b(?:rfps?|rfos?|requests?\s+for\s+(?:proposals?|offers?|bids?)|solicitations?|procurements?)"
    r"\s+(?:for|of)\s+(?:[\w-]+\s+){0,3}capacity\b"
)
#: A technology class: a solicitation that names one is open to that class only ("Renewable RFP",
#: "Thermal Capacity RFP", "clean firm capacity", "demand response capacity"), so it is not
#: all-source although it names no single technology.
TECHNOLOGY_CLASS = (
    r"\brenewabl\w*|\bthermal\b|\bclean\b|\bzero[\s-]+carbon\b|\bcarbon[\s-]+free\b|\bnon[\s-]+emitting\b"
    r"|\bemissions?[\s-]+free\b|\bdispatchable\b|\bdemand[\s-]+response\b|\bpeaking\b|\bpeakers?\b"
    r"|\bbaseload\b|\bfossil\b|\bintermittent\b|\bvariable\s+(?:energy|resources?|generation)\b"
)
ALL_SOURCE_RE = re.compile(ALL_SOURCE_PHRASE, re.I)
CAPACITY_SOLICITATION_RE = re.compile(CAPACITY_SOLICITATION, re.I)
TECHNOLOGY_CLASS_RE = re.compile(TECHNOLOGY_CLASS, re.I)
#: The same classes as qualifiers of what is sought, in a solicitation's own description
#: ("seeking up to 100 MW of demand response capacity", "renewable resources").
QUALIFIED_RESOURCE_RE = re.compile(
    r"(?:" + TECHNOLOGY_CLASS + r")\s+(?:[\w-]+\s+){0,2}(?:capacity|resources?|energy|generation|power)\b",
    re.I,
)

#: Phrases whose technology word is not that technology, removed before the keyword search (docs/28
#: B-4 found "nuclear" read from CPV labels; these are the same mistake in a buyer's own words).
TECH_FALSE_FRIENDS: tuple[str, ...] = (
    r"nuclear\s+(?:medicine|magnetic|physics|weapons?)",
    r"medicin\w*\s+nuclear\w*",  # medicina nuclear / nucleare
    r"médecine\s+nucléaire",
    r"nuklearmedizin\w*|nukleární\w*\s+medicín\w*|medycyn\w*\s+nuklearn\w*",
    r"r[ée]sonan\w*\s+magn[ée]ti\w*\s+nucl[ée]\w*|risonanza\s+magnetica\s+nucleare",
    r"wind\s+tunnels?|solar\s+wind|directed[\s-]+energy|dark\s+energy",
    # activated carbon and charcoal are not coal
    r"charbon\s+actif|carb[oó]n\s+activ\w*|carbone\s+attiv\w*|aktivkohle|activated\s+carbon",
    r"w[ęe]gl\w*\s+aktywn\w*|holzkohle|grillkohle|charcoal",
    # gas-insulated switchgear is grid equipment, not gas
    r"gas[\s-]*insulated|gasisoliert\w*|gas[\s-]*isolated",
)
_TECH_FALSE_FRIEND_RE = re.compile("|".join(TECH_FALSE_FRIENDS))

#: Electricity network words (docs/28 B-6: "transmission needs voltage, substation, transformer,
#: switchgear or grid words, or a grid CPV"). The bare words the old rule used ("netz", "sieci",
#: "électrique", "grid" inside "off-grid", Czech "HV" for hot water) name any network or anything
#: electrical; these name the electricity network, in the languages the registers publish in.
GRID_STRONG = (
    r"substation|sub-station|umspann\w*|unterwerk|schaltanlage|schaltfeld|leistungsschalter|trennschalter"
    r"|sous-stations?|postes?\s+(?:source|de\s+transformation|électriques?)|subestaci\w*|subestaç\w*"
    r"|centros?\s+de\s+transformaci\w*|sottostazion\w*|cabina\s+primaria|cabine\s+(?:elettriche|secondarie)"
    r"|stacj\w*\s+(?:transformatorow\w*|elektroenergetyczn\w*)|\bgpz\b|rozdzielni\w*|elektroenergetyczn\w*"
    r"|przekładnik\w*|odłącznik\w*|uziemnik\w*|transformovn\w*|rozvodn\w*|trafostanic\w*|transformačn\w*"
    r"|elektrick\w*\s+veden\w*|distribučn\w*\s+soustav\w*|stați\w*\s+de\s+transformare|stati\w*\s+de\s+transformare"
    r"|re[țt]e\w*\s+electric\w*|linii?\s+electric\w*|dalekovod\w*|daljnovod\w*|elektroenergetsk\w*"
    r"|razdel\w*\s+postaj\w*|distribucijsk\w*\s+omrež\w*|elektro\w*\s+omrež\w*"
    r"|подстанци\w*|трансформатор\w*|далекопровод\w*|електропровод\w*"
    r"|електроразпредел\w*|електропренос\w*|υποσταθμ\w*|μετασχηματιστ\w*|alállomás\w*|transzformátor\w*"
    r"|távvezeték\w*|villamos\s+hálózat\w*|pastot\w*|elektros\s+tinkl\w*|apakšstacij\w*|elektrotīkl\w*"
    r"|alajaam\w*|elektrivõrk\w*|ställverk\w*|kraftledning\w*|elnät\w*|nätstation\w*|transformerstation\w*"
    r"|\belnet\w*|sähköasem\w*|muuntaj\w*|voimajoh\w*|sähköverk\w*|nettstasjon\w*|onderstation\w*"
    r"|elektriciteitsnet\w*|stroomnet\w*"
    r"|\btransformers?\b|transformato[ra]\w*|transformateur\w*|trasformator\w*|transformador\w*"
    r"|transformátor\w*|\btrafo\w*|switchgear|disconnectors?|circuit[\s-]breakers?|reclosers?"
    r"|disjoncteur\w*|sectionneur\w*|seccionador\w*|aparamenta"
    r"|power\s+grid|electricity\s+grid|electric(?:al)?\s+grid|\bgrids?\b|power\s+lines?|overhead\s+lines?"
    r"|transmission\s+(?:lines?|system|network|grid|towers?|projects?|infrastructure|operator|and\s+distribution)"
    r"|(?:power|electricity)\s+(?:transmission|distribution)|distribution\s+(?:lines?|transformers?|substations?|grid)"
    r"|interconnectors?|aerial\s+bundled?\s+cables?|\babc\s+cables?"
    r"|stromnetz\w*|verteilnetz\w*|ortsnetz\w*|übertragungsnetz\w*|elektrizitätsnetz\w*|mittelspannungsnetz\w*"
    r"|niederspannungsnetz\w*|hochspannungsnetz\w*|netzanschluss\w*|netzausbau\w*|freileitung\w*|erdkabel\w*"
    r"|réseaux?\s+(?:électriques?|de\s+distribution\s+(?:d'électricité|électrique)|hta|bt\b|ht\b)"
    r"|lignes?\s+(?:électriques?|aériennes?|haute\s+tension|ht\b)|raccordement\s+au\s+réseau"
    r"|red(?:es)?\s+eléctricas?|red(?:es)?\s+de\s+distribución\s+eléctrica|líneas?\s+eléctricas?"
    r"|líneas?\s+de\s+alta|redes?\s+elétricas?|linhas?\s+de\s+transmissão|ligação\s+elétrica"
    r"|rete\s+elettrica|reti\s+elettriche|linea\s+elettrica|linee\s+elettriche|elettrodott\w*"
)
#: Voltage words. They count as grid evidence unless the notice buys electricity: "supply of
#: electricity, low and medium voltage" names the supply voltage, not network work.
GRID_VOLTAGE = (
    r"\b\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)*\s*-?\s*kv\b|(?:extra[\s-])?high[\s-]voltage|medium[\s-]voltage"
    r"|hochspannung\w*|mittelspannung\w*|höchstspannung\w*|haute\s+tension|moyenne\s+tension|\bhta\b|\bhtb\b"
    r"|alta\s+tensi[oó]n\w*|media\s+tensi[oó]n\w*|alta\s+tensão|média\s+tensão"
    r"|wysokiego\s+napięcia|średniego\s+napięcia|najwyższych\s+napięć|\bsn/nn\b|\bsn/n\b"
    r"|vysok\w*\s+napětí|vysok\w*\s+napätia|\bvvn\b|\bvn\b|medie\s+tensiune|[îi]nalt\w*\s+tensiune"
    r"|visok\w*\s+napon\w*|srednj\w*\s+napon\w*|srednjenapon\w*|visokonapon\w*|високо\s+напрежение"
    r"|средно\s+напрежение|υψηλής\s+τάσης|μέσης\s+τάσης|nagyfeszültség\w*|középfeszültség\w*"
    r"|aukštos\s+įtampos|vidutinės\s+įtampos|vidsprieguma|augstsprieguma|kõrgepinge\w*|keskpinge\w*"
    r"|högspänning\w*|mellanspänning\w*|højspænding\w*|mellemspænding\w*|suurjännit\w*|keskijännit\w*"
    r"|høyspenning\w*|hoogspanning\w*|middenspanning\w*"
)
#: Rail and other traction networks: a railway's substations, contact lines and 15 or 25 kV are
#: the railway's, never the electricity grid's, even when named as plainly as grid work (docs/28
#: B-6 sample: railway electrification, traction substations, a cable car). These veto every grid
#: word. Read after `_RAIL_FALSE_FRIEND_RE` removes the rail words that name no railway.
RAIL_TRACTION = (
    r"trakcyj\w*|kolejow\w*|\bkolei\b|railways?|\brail\b|bahnstrom|fahrleitung\w*|oberleitung\w*|stadtbahn\w*"
    r"|straßenbahn\w*|\btraction\b|caténaire\w*|ferroviai\w*|ferrovi\w*|ferrocarril\w*|tranv[ií]a\w*|\btrams?\b"
    r"|tramway\w*|trolley\w*|trolibusz\w*|trolejbus\w*|\bmetro\b|\bmetra\b|vilces|trakčn\w*|železnič\w*"
    r"|lanov\w*\s+dráh\w*|seilbahn\w*|téléphérique\w*|funivi\w*|kolej\w*\s+linow\w*|cable\s+cars?"
)
#: Rail words that name no railway (review of 2026-10-24): a guard rail beside an overhead line, a
#: street named after one ("132kV substation at Railway Road").
_RAIL_FALSE_FRIEND_RE = re.compile(
    r"(?:guard|hand|crash|safety)[\s-]*rails?|railway\s+(?:road|street|st|terrace|avenue|ave|lane|place|close"
    r"|crescent|view|cottages?|arches)\b"
)
#: Contexts whose electrical content is usually their own (street lighting, fibre networks,
#: building wiring trades, vehicle charging). They veto grid evidence read from a voltage, a code
#: or a cue, but not an explicit grid word: "Electricity distribution network reinforcement and
#: street lighting" is grid work (review of 2026-10-24).
NOT_GRID_SOFT = (
    r"oświetleni\w*|beleuchtung\w*|éclairage\w*|alumbrado\w*|iluminaci\w*|illuminazione|osvětlen\w*|iluminat\w*"
    r"|street\s+lighting|public\s+lighting|fttb|ftth|glasfaser\w*|światłowod\w*|breitband\w*|broadband"
    r"|gigabit\w*|starkstrom\w*|schwachstrom\w*|gebäudeautomation|dobíjec\w*|ładowa\w*"
    r"|charging\s+stations?|ladestation\w*|ladesäul\w*|bornes?\s+de\s+recharge|recharge\s+électrique"
    r"|cargador\w*|carregador\w*"
)
#: Every context that vetoes grid evidence from codes and cues.
NOT_GRID = RAIL_TRACTION + "|" + NOT_GRID_SOFT
#: A bare network word ("grid", "Netzanschluss") in a gas notice is the gas network's: "Delgaz
#: Grid", "Biogas-Netzanschluss ... am Netz der ONTRAS Gastransport" (labelled sample, 2026-10-24).
_GENERIC_NETWORK_RE = re.compile(r"grids?|netzanschluss\w*|netzausbau\w*")
_GAS_NETWORK_RE = re.compile(
    r"\bgaz\w*|\bgaze\b|erdgas\w*|biogas\w*|biogaz\w*|biomethan\w*|gastransport\w*|gasnetz\w*|ferngas\w*"
    r"|gazociąg\w*|plynovod\w*|\bgas\s+(?:network|grid|pipelines?|mains|distribution)"
)
_ELECTRIC_RE = re.compile(
    r"electr|elektr|électr|eléctr|elettr|strom\w*|\bpower\b|\bkv\b|villamos|sähkö|ηλεκτρ|електр"
)
#: Contexts where a power-line code or a weak cue is the project's own cabling (roads, cycle paths,
#: buildings): they veto grid evidence read from codes or cues, not explicit grid words ("33kV
#: overhead lines ... and road crossings" is grid work).
NOT_GRID_WEAK = (
    r"\bdrog[aiy]\b|drogow\w*|\bulic\w*|\broads?\b|\bstreets?\b|straße|strasse|carretera\w*|silnic\w*"
    r"|rowerow\w*|radweg\w*|\bcycl(?:e|ing)\s+(?:paths?|lanes?|routes?|ways?)|bicycle\w*|piste\s+cyclable"
    r"|ścieżk\w*|\bschul\w*|grundschul\w*|\bschools?\b|szkoł\w*|école\w*|krankenhaus\w*|hospital\w*|szpital\w*"
    r"|budyn\w*|gebäude\w*|\bbuildings?\b|internát\w*"
)
#: Phrases of a contract to buy electricity, heat or gas (docs/28 §8.2 "Electricity and gas
#: supply"; the owner's open question in §11 item 1). Read by the grid rule above and by the
#: type-of-work rules below.
ENERGY_SUPPLY_TEXT = (
    r"suppl(?:y|ies)\s+of\s+(?:electricity|electrical\s+energy|electric\s+power|natural\s+gas|gas|heat)"
    r"|electricity\s+supply|purchase\s+of\s+(?:electricity|natural\s+gas)"
    r"|dostaw\w*\s+(?:i\s+dystrybucj\w*\s+)?(?:energii\s+elektrycznej|gazu|paliwa\s+gazowego|ciepła|energii\s+cieplnej)"
    r"|zakup\w*\s+(?:energii|gazu|paliwa\s+gazowego)|sprzedaż\w*\s+(?:energii|gazu|paliwa\s+gazowego)"
    r"|kompleksow\w*\s+dostaw\w*\s+(?:energii|gazu|paliwa\s+gazowego)"
    r"|lieferung\s+(?:von\s+)?(?:strom|erdgas|elektrischer\s+energie|wärme|elektrizität)"
    r"|stromlieferung\w*|energielieferung\w*|gaslieferung\w*|strombezug|stromversorgung\s+(?:der|für)"
    r"|fourniture\s+(?:et\s+acheminement\s+)?(?:d'|d’|de\s+l'|de\s+l’)?(?:électricité|energie|énergie|gaz)"
    r"|achat\s+d['’]électricité|suministro\s+(?:de\s+)?(?:energía\s+eléctrica|electricidad|gas)"
    r"|fornitura\s+(?:di\s+)?(?:energia\s+elettrica|gas)|dodávk\w*\s+(?:elektrick\w*\s+energi\w*|elektřin\w*"
    r"|zemního\s+plynu|zemného\s+plynu|plynu|tepla|elektriny)|nákup\w*\s+(?:a\s+dodávk\w*\s+)?(?:elektrick\w*|elektřin\w*"
    r"|elektriny)|furniz\w*\s+(?:de\s+)?(?:energie\s+electric\w*|energiei\s+electrice|gaze\s+naturale)"
    r"|achiziți\w*\s+(?:de\s+)?(?:energie\s+electric\w*|gaze)|доставка\s+на\s+(?:\w+\s+){0,3}електрическа\s+енергия"
    r"|доставка\s+на\s+природен\s+газ|dobava\s+(?:električne\s+energije|zemeljskega\s+plina)"
    r"|opskrb\w*\s+električnom\s+energijom|el-energi|elleverans\w*|leverans\s+av\s+el\b|sähkön\s+toimitu\w*"
    r"|sähkönhankin\w*|levering\s+van\s+(?:elektriciteit|aardgas)|elektros\s+energijos\s+pirkim\w*"
    r"|elektroenerģijas\s+(?:piegād|iegād)\w*|elektrienergia\s+ost\w*|villamos\s*energia\w*\s+beszerz\w*"
    r"|ηλεκτρικής\s+ενέργειας|φυσικού\s+αερίου"
)
_GRID_STRONG_RE = re.compile(GRID_STRONG)
_GRID_VOLTAGE_RE = re.compile(GRID_VOLTAGE)
_RAIL_TRACTION_RE = re.compile(RAIL_TRACTION)
_NOT_GRID_SOFT_RE = re.compile(NOT_GRID_SOFT)
_NOT_GRID_RE = re.compile(NOT_GRID)
_NOT_GRID_WEAK_RE = re.compile(NOT_GRID_WEAK)
_ENERGY_SUPPLY_RE = re.compile(ENERGY_SUPPLY_TEXT)
#: "off-grid", "mini-grid" and "micro-grid" are the microgrid tag, not grid work.
_SMALL_GRID_RE = re.compile(r"(?:off|mini|micro)[\s-]+grids?")
#: The rule-order slot of the grid tag: its pattern below is the evidence, `grid_evidence` decides.
GRID_TECH = "transmission"

#: Solar heat: collectors and solar water heating, not PV (CPV 09331100 "Solar collectors for heat
#: production", which the `0933` prefix had read as `solar_pv`; review of 2026-10-24). Its words
#: are removed before the solar PV rule is read, so "solar thermal" alone is not PV too. A family
#: member of "Solar" (docs/28 §8.1).
SOLAR_THERMAL = (
    r"solar\s*thermal\w*|solarthermi\w*|solar\s+(?:water\s+heat\w*|hot\s+water|collectors?|heat\w*)"
    r"|kolektor\w*\s+słoneczn\w*|capteurs?\s+solaires?(?:\s+thermiques?)?|solaire\s+thermique"
    r"|termosolar\w*|solare\s+termic\w*|sonnenkollektor\w*|solární\w*\s+kolektor\w*|napkollektor\w*"
)
_SOLAR_THERMAL_RE = re.compile(SOLAR_THERMAL)
#: The solar PV token, read with the solar-heat words removed (`classify_technologies`).
SOLAR_PV = "solar_pv"

# Ordered keyword rules over notice titles/descriptions -> normalised technology tokens. A
# solicitation open to every technology carries `ALL_SOURCE`, set by the RFP registers
# (`pipeline/connectors/us_rfp/classify.py`); this list never emits it.
TECH_KEYWORDS: list[tuple[str, str]] = [
    (r"offshore\s*wind", "wind_offshore"),
    (r"\bwind\b|éolien|eolic|wiatrow|windkraft|windenergie", "wind"),
    (SOLAR_THERMAL, "solar_thermal"),
    (
        r"photovolta|fotowolta|fotovolta|\bsolar\b|\bsolares\b|\bpv\b|solaire|solare|\bfve\b"
        r"|solar\w*(?:anlage|park|modul|strom)\w*|solcell\w*|aurinkosähk\w*|aurinkovoim\w*|zonnepane\w*"
        r"|napelem\w*|slunečn\w*\s+elektrár\w*|solárn\w*|sončn\w*\s+elektrarn\w*|фотоволта\w*|φωτοβολτα\w*",
        "solar_pv",
    ),
    (r"battery|\bbess\b|energy storage|stockage|batter[iy]|magazyn", "bess"),
    (r"pumped[\s-]*storage|pumped hydro", "pumped_storage"),
    (r"hydro(?:electric|power)?\b|hydraul|wasserkraft|\bs?hpp\b", "hydro"),
    (r"\btidal\b|wave energy|marine energy|hydrokinetic", "marine"),
    (
        r"nuclear|nucléaire|nucleare|jądrow|kernkraft|\bsmr\b|jadern\w*|ядрен\w*|atomkraft|kärnkraft|ydinvoim",
        "nuclear",
    ),
    (r"hydrogen|hydrogène|wasserstoff|wodor|electrolys", "hydrogen"),
    (r"geotherm|géotherm", "geothermal"),
    (
        r"biomas|biogas|biomethane|biométhane|biogaz|anaerobic|pellet|wood\s*chips?|wood\s+fuels?|firewood"
        r"|hackschnitzel|brennholz|zrębk\w*|štěpk\w*|copeaux\s+de\s+bois|bois[\s-]énergie|cippato|astillas"
        r"|malkas|kokskaid\w*|medžio\s+granul\w*",
        "biomass",
    ),
    (r"carbon capture|\bccs\b|\bccus\b|carbon dioxide removal", "ccs"),
    (r"natural gas|\bgas\b|gaz naturel|erdgas|combined[\s-]cycle|\blng\b|gasoduc|pipeline", "gas"),
    (
        r"\bcoal\b|steinkohle|braunkohle|kohlekraftwerk\w*|\blignit\w*|\bcharbon\b|\bcarbón\b"
        r"|w[ęe]giel\s+kamienn\w*|w[ęe]gla\s+(?:kamienn\w*|energetyczn\w*|brunatn\w*)|miał\w*\s+w[ęe]glow\w*"
        r"|ekogroszek|\buhl(?:í|ie)\b|c[ăa]rbune\w*|въглища",
        "coal",
    ),
    (GRID_STRONG + "|" + GRID_VOLTAGE, GRID_TECH),
    (
        r"district heat|heat network|chaleur|fernwärme|heat pump|ciepł|zásobování\s+teplem|\bc?sczt\b"
        r"|horkovod\w*|parovod\w*"
        # review of 2026-10-24: heat pumps, district cooling and heat supply in the buyer's words
        r"|wärmepump\w*|fernkälte\w*|district\s+cooling|wärmeliefer\w*|wärmenetz\w*"
        r"|топлинн\w*\s+енерги\w*|топлоенерги\w*|chauffage\s+urbain|teleriscaldament\w*|calefacción\s+urbana",
        "heat",
    ),
    (
        r"electric vehicle|\bev\b|charging|recharge|ładowa|ladestation\w*|ladeinfrastruktur\w*|ladesäul\w*"
        r"|ladepunkt\w*|wallbox\w*|laadpa\w*|laadinfra\w*|ricarica|recarga|dobíjec\w*|nabíjec\w*",
        "ev_charging",
    ),
    (r"energy efficien|efficacité énergétique|retrofit|insulation|termomodern", "efficiency"),
    (r"microgrid|mini[\s-]grid|off[\s-]grid|rural electrification", "microgrid"),
    (r"smart meter|metering|\bami\b", "metering"),
]
_TECH_RES: list[tuple[re.Pattern[str], str]] = [(re.compile(p), t) for p, t in TECH_KEYWORDS]

#: Every technology token an opportunity can carry, in rule order: the vocabulary the opportunity
#: technology filter offers (`GET /v1/meta/vocabularies` `opportunity_technology`). It is not the
#: proposal vocabulary (`pipeline.normalize.TECH_RULES`): opportunities say `solar_pv`, `bess`,
#: `gas`, `heat`, ... and the opportunities page used to offer the proposal tokens, which match no
#: tagged notice (audit 2026-09-30, frontend F2). `coal` joined it on 2026-10-24 (docs/28 B-5: 28
#: notices carried it and no reader could choose it), and `solar_thermal` the same day (CPV
#: 09331100 and solar-heat words; the proposal vocabulary, its label and the matcher's solar and
#: heat families already hold it).
OPPORTUNITY_TECHNOLOGIES: tuple[str, ...] = tuple(dict.fromkeys(token for _, token in TECH_KEYWORDS))

# ISO 3166-1 alpha-3 -> alpha-2 for the buyer countries TED and MDB notices use.
ISO3_TO_ISO2: dict[str, str] = {
    "AUT": "AT",
    "BEL": "BE",
    "BGR": "BG",
    "HRV": "HR",
    "CYP": "CY",
    "CZE": "CZ",
    "DNK": "DK",
    "EST": "EE",
    "FIN": "FI",
    "FRA": "FR",
    "DEU": "DE",
    "GRC": "GR",
    "HUN": "HU",
    "IRL": "IE",
    "ITA": "IT",
    "LVA": "LV",
    "LTU": "LT",
    "LUX": "LU",
    "MLT": "MT",
    "NLD": "NL",
    "POL": "PL",
    "PRT": "PT",
    "ROU": "RO",
    "SVK": "SK",
    "SVN": "SI",
    "ESP": "ES",
    "SWE": "SE",
    "NOR": "NO",
    "ISL": "IS",
    "LIE": "LI",
    "CHE": "CH",
    "GBR": "GB",
    "UKR": "UA",
    "MDA": "MD",
    "SRB": "RS",
    "MKD": "MK",
    "MNE": "ME",
    "ALB": "AL",
    "BIH": "BA",
    "TUR": "TR",
    "GEO": "GE",
    "ARM": "AM",
    "USA": "US",
    "CAN": "CA",
    "AUS": "AU",
    "NZL": "NZ",
    "JPN": "JP",
    "KOR": "KR",
    "IND": "IN",
    "CHN": "CN",
    "BRA": "BR",
    "ZAF": "ZA",
    "XKX": "XK",
    "1A0": "XK",
}


def _blob(texts: tuple[Any, ...]) -> str:
    return " ".join(
        str(t) for t in texts if t is not None and not (isinstance(t, float) and pd.isna(t))
    ).lower()


def not_grid_context(*texts: Any, weak: bool = True) -> str | None:
    """The words that make a notice's electrical content something other than grid work, or None:
    a railway or cable car (`RAIL_TRACTION`), street lighting, a fibre network, building wiring
    trades, vehicle charging (`NOT_GRID_SOFT`), and with `weak`, a road, cycle path or building
    (`NOT_GRID_WEAK`). All of them veto grid evidence read from codes or cues."""
    blob = _RAIL_FALSE_FRIEND_RE.sub(" ", _blob(texts))
    hit = _NOT_GRID_RE.search(blob) or (_NOT_GRID_WEAK_RE.search(blob) if weak else None)
    return hit.group(0) if hit else None


def grid_evidence(*texts: Any, buys_energy: bool = False) -> str | None:
    """The words that make a notice electricity-network work (docs/28 B-6), or None: a network,
    substation, transformer or switchgear word (`GRID_STRONG`), or a voltage (`GRID_VOLTAGE`) in a
    notice that does not buy electricity (`buys_energy`, or a supply phrase in the text). A railway
    or other traction context vetoes both (`RAIL_TRACTION`); lighting, fibre, wiring trades and
    charging (`NOT_GRID_SOFT`) veto a voltage alone, not an explicit grid word; and a bare network
    word in a gas notice that names no electricity is the gas network's."""
    text = _RAIL_FALSE_FRIEND_RE.sub(
        " ", _SMALL_GRID_RE.sub(" ", _TECH_FALSE_FRIEND_RE.sub(" ", _blob(texts)))
    )
    if _RAIL_TRACTION_RE.search(text):
        return None
    strong = [m.group(0) for m in _GRID_STRONG_RE.finditer(text)]
    gas_only = bool(_GAS_NETWORK_RE.search(text)) and not _ELECTRIC_RE.search(text)
    if gas_only:
        strong = [s for s in strong if not _GENERIC_NETWORK_RE.fullmatch(s)]
    if strong:
        return strong[0]
    if _NOT_GRID_SOFT_RE.search(text):
        return None
    voltage = _GRID_VOLTAGE_RE.search(text)
    if voltage and not buys_energy and not _ENERGY_SUPPLY_RE.search(text):
        return voltage.group(0)
    return None


def energy_supply_phrase(*texts: Any) -> str | None:
    """The words that make a notice a contract to buy electricity, heat or gas, or None."""
    hit = _ENERGY_SUPPLY_RE.search(_blob(texts))
    return hit.group(0) if hit else None


def classify_technologies(*texts: Any) -> list[str]:
    """Technology tokens the texts name, in `TECH_KEYWORDS` order. False friends
    (`TECH_FALSE_FRIENDS`) are removed first; the grid tag needs `grid_evidence`."""
    blob = _TECH_FALSE_FRIEND_RE.sub(" ", _blob(texts))
    found: list[str] = []
    for pattern, token in _TECH_RES:
        if token in found:
            continue
        if token == GRID_TECH:
            if grid_evidence(blob) is not None:
                found.append(token)
        elif token == SOLAR_PV:
            if pattern.search(_SOLAR_THERMAL_RE.sub(" ", blob)):
                found.append(token)
        elif pattern.search(blob):
            found.append(token)
    return found


def iso2(country: Any) -> str | None:
    if country is None or (isinstance(country, float) and pd.isna(country)):
        return None
    c = str(country).strip().upper()
    if len(c) == 2:
        return c
    return ISO3_TO_ISO2.get(c, c[:2] if c else None)


def to_utc(v: Any, fmt: str | None = None) -> pd.Timestamp | None:
    """Parse a date/datetime; naive values are taken as UTC (docs/04 E-4)."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    try:
        ts = pd.to_datetime(v, format=fmt, errors="coerce", utc=False)
    except (ValueError, TypeError):
        return None
    if ts is None or pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    utc: pd.Timestamp = ts.tz_convert("UTC")
    return utc


def deadline_passed(due: pd.Timestamp | None, now: dt.datetime) -> str:
    """'yes'/'no'/'' — a string so status_map refine rules can match it."""
    if due is None:
        return ""
    return "yes" if due < pd.Timestamp(now).tz_convert("UTC") else "no"


#: `status_rule` of a row `close_past_deadline` moved from `open` to `closed`.
DEADLINE_PASSED_RULE = "opportunity.deadline_passed"


def close_past_deadline(df: pd.DataFrame, now: dt.datetime) -> tuple[pd.DataFrame, int]:
    """Every `open` row whose `due_at` is before `now` becomes `closed` (docs/21 §7.2: "open -->
    closed: deadline passed"; `closed` is "deadline passed, outcome unknown"), with `status_rule`
    `DEADLINE_PASSED_RULE`. Returns the frame and how many rows moved.

    Each connector already applies this at fetch time through its status map's refine rule (TED
    `ted.competition_deadline_passed`, Find a Tender, World Bank, grants.gov). The runner applies it
    again to the whole frame at the run's `retrieved_at`, because an incremental source carries
    earlier rows forward unchanged: a notice fetched while open and never re-fetched stayed `open`
    after its deadline (audit 2026-09-30: 90 of 405 stored `open` notices had a past `due_at`)."""
    if "status" not in df.columns or "due_at" not in df.columns or not len(df):
        return df, 0
    due = pd.to_datetime(df["due_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp(now)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    moved = (df["status"].astype("string") == "open").fillna(False).to_numpy() & (due < cutoff).fillna(
        False
    ).to_numpy()
    if not moved.any():
        return df, 0
    out = df.copy()
    out["status"] = out["status"].astype("object")
    out.loc[moved, "status"] = "closed"
    if "status_rule" in out.columns:
        out["status_rule"] = out["status_rule"].astype("object")
        out.loc[moved, "status_rule"] = DEADLINE_PASSED_RULE
    return out, int(moved.sum())


def technologies_str(tokens: list[str]) -> str:
    return "|".join(tokens)


def known_budget(amount: float | int | None) -> float | None:
    """A stated budget as a float, or `None` when it is unknown.

    Zero and negative amounts are placeholders, not budgets: TED states `estimated-value-glo` as 0
    or -1 on notices that give no value (9 of the 279 TED notices with an amount in the 2026-09-13
    dev snapshot: 8 at 0 EUR, 1 at -1 PLN). Stored as numbers they sorted first on an ascending
    budget sort and printed "-1 PLN". As `None` they take the API's null ordering (last, both ways)
    and the page's dash. The notice's stated currency is left alone (docs/00 2026-09-27, lane E16:
    one currency per budget sort), and `raw` keeps the source's own value. Lane I3, 2026-09-29."""
    if amount is None or isinstance(amount, bool):
        return None
    value = float(amount)
    return value if math.isfinite(value) and value > 0 else None


def empty_opportunity(n: int) -> dict[str, list[Any]]:
    return {c: [None] * n for c in ("summary", "capacity_sought_mw", "budget_amount", "budget_currency")}


# ------------------------------------------------------------------------------- stated values
#: What a notice's stated amount is, in the words the page prints beside it (lane rfp-value,
#: 2026-10-22). A bare "budget" misleads both ways: a TED result notice's total is what was
#: awarded, not what is on offer, and a Grants.gov award ceiling is the most one award can be, not
#: the programme's size. Each connector's `stated_value(row)` names one of these per figure; the
#: amount is always the source's own number, never derived from another figure.
VALUE_KINDS: dict[str, str] = {
    "estimated_value": "Estimated value",
    "estimated_value_incl_vat": "Estimated value including VAT",
    "awarded_value": "Total awarded value",
    "contract_value": "Contract value",
    "total_programme_funding": "Estimated total programme funding",
    "award_ceiling": "Award ceiling",
    "award_floor": "Award floor",
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
#: The largest signed and unsigned 32-bit integers: what a form's numeric field holds when someone
#: typed past its maximum. Grants.gov 363912 states `estimatedFunding` 2147483647 (2026-10-22).
_FIELD_MAXIMA = frozenset({2_147_483_647.0, 4_294_967_295.0})
#: Below this a "value" is a unit price or a placeholder, not what a contract or programme is worth:
#: on 2026-10-22 the store's 14 amounts under 100 were electricity and fuel unit prices (EUR 0.01,
#: 0.26, 1.86, 3.28, 9.10), five notices stating 1, and two stating 2.
PLACEHOLDER_FLOOR = 100.0
#: The shortest run of nines read as a placeholder: 999,999,999 (Grants.gov 361080's award ceiling,
#: 2026-10-22, the only run of nines among the store's and the live Grants.gov figures). A shorter
#: run is an ordinary figure set just under a threshold ($999,999, $9,999,999) and is ranked.
NINES_MIN_DIGITS = 9


def placeholder_reason(amount: float | None) -> str | None:
    """Why a stated amount is not ranked as a value, or None when it is (lane rfp-value). The
    amount stays the notice's own and is shown as stated; only the USD equivalent that orders the
    list is withheld (`services/ingest/opportunity_value.py`). Rules: a 32-bit field maximum, a run
    of `NINES_MIN_DIGITS` or more nines, or less than `PLACEHOLDER_FLOOR` in any currency. Nothing
    else is judged: an implausible but ordinary-looking figure is ranked as stated."""
    if amount is None:
        return None
    if amount in _FIELD_MAXIMA:
        return "the largest number the source's field holds, not a figure"
    digits = str(int(amount)) if amount == int(amount) else ""
    if len(digits) >= NINES_MIN_DIGITS and set(digits) == {"9"}:
        return "a run of nines, a placeholder rather than a figure"
    if amount < PLACEHOLDER_FLOOR:
        return f"under {PLACEHOLDER_FLOOR:,.0f}, a unit price or placeholder rather than a total"
    return None


def parse_amount(value: Any) -> float | None:
    """A stated amount from a JSON number, a numeric string (`"35400000"`, `"3,290,325.87"`) or a
    one-element list of either (TED's `estimated-value-glo` is `["3290325.87"]`), through
    `known_budget`: 0 and negatives are the sources' placeholders for "not stated"."""
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    return known_budget(value)


def currency_code(value: Any) -> str | None:
    """An ISO 4217 code as the store keeps it (three upper-case letters), else None. A list takes
    its first element (TED's `total-value-cur` is `["RON"]`)."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _CURRENCY_RE.fullmatch(code) else None


@dataclasses.dataclass(frozen=True)
class StatedFigure:
    """One amount a notice states: what it is (`VALUE_KINDS`), the number, its currency and the
    source field it was read from. `may_lead` is False for a figure that is listed but never the
    notice's value: an award floor (the least one award can be), or one of several awards (a lot's
    value is not the procurement's)."""

    kind: str
    amount: float
    currency: str | None
    source_field: str
    may_lead: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount": self.amount,
            "currency": self.currency,
            "source_field": self.source_field,
        }


@dataclasses.dataclass(frozen=True)
class StatedValue:
    """A notice's value: the `primary` figure (it becomes `budget_amount`/`budget_currency`, and its
    USD equivalent is what the list sorts on), the other figures the same notice states, and, for
    figures read from a different response than the record's own row (Grants.gov's opportunity
    detail), when and from where they were read."""

    primary: StatedFigure
    also_stated: tuple[StatedFigure, ...] = ()
    expected_awards: int | None = None
    retrieved_at: str | None = None
    endpoint: str | None = None

    @property
    def amount(self) -> float:
        return self.primary.amount

    @property
    def currency(self) -> str | None:
        return self.primary.currency

    def identifiers(self) -> dict[str, Any]:
        """`identifiers["value"]` as a connector writes it; the loader adds the USD equivalent
        (`services/ingest/opportunity_value.py`)."""
        out: dict[str, Any] = {"kind": self.primary.kind, "source_field": self.primary.source_field}
        if self.also_stated:
            out["also_stated"] = [f.as_json() for f in self.also_stated]
        if self.expected_awards is not None:
            out["expected_awards"] = self.expected_awards
        if self.retrieved_at:
            out["retrieved_at"] = self.retrieved_at
        if self.endpoint:
            out["endpoint"] = self.endpoint
        return out


def _repeat_key(figure: StatedFigure) -> tuple[Any, ...]:
    """Two fields stating the same figure are one figure (a TED estimate repeated as its total);
    two items of a list (Find a Tender's `awards[0]` and `awards[1]`) are two figures, even at the
    same amount."""
    item = figure.source_field if "[" in figure.source_field else None
    return (figure.kind, figure.amount, figure.currency, item)


def stated_value_of(figures: list[StatedFigure], **extra: Any) -> StatedValue | None:
    """The notice's value from its figures, in the connector's order of precedence, without exact
    repeats; None for no figures. The primary is the first figure that may lead and is not a
    placeholder (`placeholder_reason`), so a notice stating a real ceiling beside a placeholder
    total (Grants.gov 363912: 2,147,483,647 and 250,000,000) is ranked on the ceiling, labelled as
    one. When every figure that may lead is a placeholder, the first of them stays primary, shown
    as stated with its note. The others, in order, are `also_stated`."""
    unique: list[StatedFigure] = []
    seen: set[tuple[Any, ...]] = set()
    for f in figures:
        key = _repeat_key(f)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    if not unique:
        return None
    leads = [f for f in unique if f.may_lead] or unique
    primary = next((f for f in leads if placeholder_reason(f.amount) is None), leads[0])
    return StatedValue(primary=primary, also_stated=tuple(f for f in unique if f is not primary), **extra)


# ------------------------------------------------------------------------- TED title segments
#: TED's eForms titles read "<buyer country> – <label of the main CPV code> – <the buyer's own
#: title>" (all 1,516 TED notices of the 2026-10-23 register had at least three segments). The
#: label is TED's, not the buyer's, so it is never classified (docs/28 B-4).
TED_TITLE_SEPARATOR = " – "


def ted_buyer_title(title: Any) -> str:
    """The buyer's own words of a TED title: everything after the second separator; the whole
    title when it has fewer than three segments."""
    text = "" if title is None or (isinstance(title, float) and pd.isna(title)) else str(title)
    parts = text.split(TED_TITLE_SEPARATOR)
    return TED_TITLE_SEPARATOR.join(parts[2:]) if len(parts) >= 3 else text


# ------------------------------------------------------------- type of work and who it is for
# Owner, 2026-10-24: "filter RFPs and the like by what type of opportunity it is for to help the
# user know if it's an opportunity for them". Two layers, multi-label, pure functions over the
# normalised notice and the source fields it came from (docs/28 lane F3): (a) what is being
# bought (`WORK_TYPES`), read from the structured fields first and the buyer's own title second;
# (b) who it is for (`AUDIENCES`), derived from (a) and, for funding, from the applicant types
# the source says are eligible, by the two tables `WORK_TYPE_AUDIENCES` and `APPLICANT_AUDIENCES`
# (the one place the mapping lives; `audiences_for` is its only reader). Every tag carries its
# evidence (the field and value), so a page can say why. A notice no rule can read has no type
# of work: it is counted as unknown, never guessed.

#: What is being bought, in display order.
WORK_TYPES: tuple[str, ...] = (
    "project",  # develop and sell power, capacity or a built asset: PPA, build-transfer, capacity, all-source
    "site",  # land, lease or site offers
    "works",  # construction and installation, EPC
    "operations",  # operation and maintenance
    "equipment",  # supply of plant and equipment (CPV 31, 32, 42; energy plant named in the title)
    "services",  # engineering, studies, owner's engineer, advisory
    "energy_supply",  # contracts to supply electricity, heat or gas
    "funding",  # grants, loans, programmes
    "other",  # read, and none of the above (a fuel purchase, furniture, vehicles, IT, medicines)
)
#: Applicant types a funding notice may name as eligible (Grants.gov `applicantTypes`).
APPLICANT_TYPES: tuple[str, ...] = ("for_profit", "utility", "public_body", "non_profit", "tribal")
#: Who a notice is for: the five roles, then the applicant types.
AUDIENCES: tuple[str, ...] = (
    "developers",  # developers and IPPs
    "contractors",
    "equipment_suppliers",
    "consultants",  # engineers and consultants
    "energy_suppliers",
    *APPLICANT_TYPES,
)
#: Layer (b), part one: who each type of work is for. Funding names no audience of its own: who
#: a grant or loan is for is who the source says may apply (`APPLICANT_AUDIENCES`).
WORK_TYPE_AUDIENCES: dict[str, tuple[str, ...]] = {
    "project": ("developers",),
    "site": ("developers",),
    "works": ("contractors",),
    "operations": ("contractors",),
    "equipment": ("equipment_suppliers",),
    "services": ("consultants",),
    "energy_supply": ("energy_suppliers",),
    "funding": (),
    "other": (),
}
#: Layer (b), part two: who a funding notice is for, from each applicant type the source states
#: as eligible (`eligible_applicants`). Developers and IPPs when for-profit applicants may apply.
#: Grants.gov states them in its opportunity detail, which `us.grants_gov.search2` does not keep
#: today (`DETAIL_BLOCK_KEYS`), so no stored funding notice has an audience until it does.
APPLICANT_AUDIENCES: dict[str, tuple[str, ...]] = {
    "for_profit": ("developers", "for_profit"),
    "utility": ("utility",),
    "public_body": ("public_body",),
    "non_profit": ("non_profit",),
    "tribal": ("tribal",),
}
FUNDING_KINDS = frozenset({"foa", "loan_program", "program"})

#: CPV code prefixes -> type of work, longest prefix first (codes and labels: CPV 2008, the
#: Publications Office list in the eForms SDK, `codelists/cpv.gc`, read 2026-10-24).
CPV_WORK_TYPES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("65320000", "operations"),  # operation of electrical installations
            ("65410000", "operations"),  # operation of a power plant
            ("09300000", "energy_supply"),  # electricity, heating, solar and nuclear energy (the group)
            ("0931", "energy_supply"),  # electricity
            ("0932", "energy_supply"),  # steam, hot water, district heating
            ("0912", "energy_supply"),  # gaseous fuels, mains and natural gas
            ("653", "energy_supply"),  # electricity distribution and related services
            ("654", "energy_supply"),  # other sources of energy supplies and distribution
            ("0933", "equipment"),  # solar energy: panels, modules, installations
            ("3855", "equipment"),  # meters
            ("701", "site"),  # real estate services with own property (letting, development)
            ("702", "site"),  # renting or leasing of own property
            ("45", "works"),  # construction work
            ("50", "operations"),  # repair and maintenance services
            ("71", "services"),  # architectural, construction, engineering and inspection services
            ("73", "services"),  # research and development services and related consultancy
            ("794", "services"),  # business and management consultancy
            ("31", "equipment"),  # electrical machinery, apparatus, equipment
            ("32", "equipment"),  # radio, television, communication equipment
            ("42", "equipment"),  # industrial machinery (turbines, boilers, pumps)
            ("4432", "equipment"),  # cable and related products
            ("09", "fuel"),  # fuels and petroleum products (not a type of work: "other")
            # Every other goods division (03-44 but 09: agricultural, food, chemicals, office and
            # computing machines, medical, vehicles, furniture, construction materials ...): goods
            # that are not plant and equipment, so a contract that only buys them is `other`
            # (review of 2026-10-24, finding 4: vehicles, IT, furniture and medicines had read as
            # equipment). Listed last, so the stable sort keeps 31, 32 and 42 as equipment.
            *((f"{d:02d}", "goods") for d in range(3, 45) if d != 9),
        ),
        key=lambda pair: -len(pair[0]),
    )
)
#: TED `contract-nature`, Find a Tender `mainProcurementCategory` and World Bank
#: `procurement_group` as one vocabulary. A TED or Find a Tender "services" contract is any
#: service (waste collection, IT, cleaning), so it is not by itself the `services` type of work
#: (engineering, studies, advisory); the World Bank's "consulting services" group is.
_NATURE_ALIASES = {
    "works": "works",
    "supplies": "supplies",
    "goods": "supplies",
    "services": "services",
    "cw": "works",
    "go": "supplies",
    "cs": "consulting",
    "nc": "non_consulting",
}
WORLD_BANK_PROCUREMENT_GROUPS = {
    "CW": "works",
    "GO": "goods",
    "CS": "consulting services",
    "NC": "non-consulting services",
}
_WB_GROUP_IN_REFERENCE = re.compile(r"(?:^|[-/_\s])(CW|GO|CS|NC)(?:$|[-/_\s])")
#: Grants.gov applicant types, read by their description (the codes are in the evidence).
_APPLICANT_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"unrestricted", re.I), ("for_profit", "public_body", "non_profit", "tribal")),
    (re.compile(r"for[\s-]profit|small\s+business", re.I), ("for_profit",)),
    (re.compile(r"tribal|native\s+american", re.I), ("tribal",)),
    (re.compile(r"non[\s-]?profit", re.I), ("non_profit",)),
    (re.compile(r"private\s+institutions\s+of\s+higher\s+education", re.I), ("non_profit",)),
    (
        re.compile(r"government|housing\s+authorit|school\s+district|public\s+and\s+state", re.I),
        ("public_body",),
    ),
)
_UTILITY_ELIGIBLE = re.compile(r"\butilit(?:y|ies)\b", re.I)

# The buyer's own words, second (each a list of languages the registers publish in).
_OPERATIONS_TEXT = re.compile(
    r"operations?\s+(?:and|&)\s+maintenance|\bo\s*&\s*m\b|\bmaintenance\b|eksploatacj\w*|konserwacj\w*"
    r"|utrzymani\w*|wartung\w*|instandhaltung\w*|betriebsführung\w*|(?<!mise en )exploitation\b|mantenimiento"
    r"|manutenzione|údržb\w*|mentenanț\w*|întreținere|обслужване|drift\s+og\s+vedligehold\w*|onderhoud\w*"
    r"|operation\s+of\s+(?:the\s+)?(?:plant|power|facility|station)"
    # design-build-operate (review of 2026-10-24, finding 3): "Design, build and operate", "DBO",
    # "DBOM", "Planung, Errichtung und Betrieb" (not "im laufenden Betrieb", during operation)
    r"|build[\s,]+(?:and\s+|&\s*)?operat\w*|\bdbo[mt]?\b|\bdbfom?\b|operat\w*\s+(?:and|&)\s+maintain\w*"
    r"|(?<!laufenden\s)(?<!laufendem\s)\bbetrieb\b|\bbetreib(?:en|ung)\b|explotaci[oó]n\b|\bexploração\b",
    re.I,
)
_SERVICES_TEXT = re.compile(
    r"\bconsult\w*|\bstud(?:y|ies)\b|feasibility|supervision|owner'?s\s+engineer|advisory|advisor\b"
    r"|technical\s+assistance|\baudit\w*|assessment|\bsurvey\w*|doradztw\w*|nadz[oó]r\w*|studi\w*\s+wykonaln\w*"
    r"|ekspertyz\w*|gutachte\w*|beratung\w*|machbarkeit\w*|bauüberwachung|étude\w*|assistance\s+technique"
    r"|maîtrise\s+d['’]œuvre|recrutement\s+d['’]un\s+consultant|estudio\w*|consultor\w*|asistencia\s+técnica"
    r"|consulenz\w*|progettazione\b(?!\s+ed?\s+(?:esecuzione|realizzazione))|posud\w*|poradensk\w*"
    r"|\bspecialist\b|\bexpert\b|\bspécialiste\b",
    re.I,
)
_DESIGN_BUILD_TEXT = re.compile(
    r"design[\s,-]+(?:and\s+|&\s*)?(?:build|supply)|zaprojektowanie\s+i\s+(?:wykonanie|wybudowanie)|zaprojektuj\s+i\s+wybuduj"
    r"|planung\s+und\s+(?:errichtung|bau)|conception[\s-]+réalisation|proyecto\s+y\s+(?:obra|ejecución)"
    r"|progettazione\s+ed?\s+(?:esecuzione|realizzazione)|projekt\w*\s+a\s+realizac\w*|proiectare\s+(?:și|si)\s+execuți\w*"
    r"|planung,\s+(?:lieferung,\s+)?(?:errichtung|bau)|\bdbo[mt]?\b|\bdbfom?\b",
    re.I,
)
_SUPPLY_INSTALL_TEXT = re.compile(
    r"suppl(?:y|ies)[\s,]+(?:and\s+)?install\w*|supply,\s+installation"
    r"|dostaw\w*\s+(?:wraz\s+z\s+|i\s+)(?:montaż|instalacj|zabudow)\w*"
    r"|lieferung\s+und\s+(?:montage|einbau|installation)|fourniture\s+et\s+(?:installation|pose|mise\s+en)"
    r"|suministro\s+[ey]\s+(?:instalaci\w*|montaje)|fornitura\s+e\s+(?:posa|installazione)"
    r"|dodávk\w*\s+a\s+(?:montáž|instalac)\w*|furnizare\s+(?:și|si)\s+(?:montaj|instalare)"
    r"|dobava\s+in\s+montaž\w*|dobava\s+i\s+(?:montaž|ugradnj)\w*|dostava\s+i\s+montaž\w*|доставка\s+и\s+монтаж\w*"
    r"|levering\s+en\s+(?:montage|installatie)|leverans\s+och\s+(?:montage|installation)|toimitus\s+ja\s+asennu\w*"
    r"|fornecimento\s+e\s+(?:instalação|montagem)|προμήθεια\s+(?:και\s+)?εγκατάστασ\w*|piegāde\s+un\s+uzstādīšan\w*"
    r"|tarne\s+ja\s+paigaldu\w*|pristatym\w*\s+ir\s+montav\w*",
    re.I,
)
_WORKS_TEXT = re.compile(
    r"\bconstruct\w*|\bbuild\w*|installation|\binstall\w*|\bepc\b|\bworks\b|rehabilitat\w*|electrification"
    r"|roboty\s+budowlane|wykonanie\s+robót|\bbudow\w*|przebudow\w*|rozbudow\w*|montaż\w*|\bbau\b|errichtung"
    r"|neubau|sanierung|travaux|réalisation|obras?\b|lavori|výstavb\w*|stavebn\w*|rekonstrukc\w*|izgradnj\w*"
    r"|lucrări|execuți\w*|строител\w*|électrification",
    re.I,
)
_EQUIPMENT_TEXT = re.compile(
    r"\bsuppl(?:y|ies)\s+of\b|\bdelivery\s+of\b|\bpurchase\s+of\b|\bacquisition\b|\bprocurement\s+of\b"
    r"|dostaw\w*|lieferung\w*|fourniture\w*|suministro\w*|fornitura\w*|dodávk\w*|achiziți\w*|furnizare"
    r"|доставка|adquisici\w*|aquisição|beschaffung\w*|\bzakup\w*",
    re.I,
)
#: Plant and equipment named in a goods notice's own words (review of 2026-10-24, finding 4): the
#: World Bank's "goods" group and a title-only notice buy vehicles, computers and furniture as
#: often as transformers, so goods are `equipment` only when the title names plant or equipment of
#: this kind (or a technology, `classify_technologies`); otherwise they are `other`.
_PLANT_TEXT = re.compile(
    r"\btransformers?\b|transformateurs?|transformadores?|\bgenerators?\b|générateurs?|groupes?\s+électrogènes?"
    r"|\bgensets?\b|generadores?|grupos?\s+electrógenos?|geradores?|\bturbines?\b|turbinas?|\binverters?\b"
    r"|onduleurs?|inversores?|\bbatter(?:y|ies)\b|batteries|baterías?|baterias?"
    r"|(?:energy|electricity|electric|water|smart|prepaid|pre-paid|gas)\s+meters?|\bcompteurs?|medidores?"
    r"|(?:electric(?:al)?|power|aerial|bundled|abc|mv|lv|hv|underground|distribution)\s+cables?|\bconductors?\b"
    r"|câbles?\s+(?:électriques?|de\s+(?:distribution|puissance)|basse|moyenne|haute)|conducteurs?"
    r"|cables?\s+eléctricos?|cabos?\s+elétricos?|conductores?"
    r"|(?:electric(?:ity|al)?|wooden|wood|concrete|steel|utility|distribution|transmission)\s+poles?\b|poteaux"
    r"|switchgear|disjoncteurs?|circuit[\s-]breakers?|reclosers?|\binsulators?\b|isolateurs?"
    r"|electrical\s+(?:equipment|materials?|goods|accessories)|matériels?\s+électriques?|équipements?\s+électriques?"
    r"|materiais?\s+elétricos?|materiales?\s+eléctricos?|equipos?\s+eléctricos?|electro-?mechanical|électromécaniques?"
    r"|\bpumps?\b|\bpompes?\b|\bbombas?\b|\bboilers?\b|chaudières?|calderas?|\bcompressors?\b|compresseurs?"
    r"|machinery|\bluminaires?\b|lampadaires?|street\s*lights?|solar\s+(?:lamps?|lanterns?|kits?)",
    re.I,
)
_SITE_TEXT = re.compile(
    r"lease\s+of\s+(?:land|sites?|roofs?|rooftops?)|land\s+lease|site\s+lease|roof\s*top\s+lease"
    r"|dzierżaw\w*\s+(?:grunt\w*|nieruchomo\w*|dział\w*|teren\w*)|verpachtung\w*|\bpacht\w*"
    r"|bail\s+emphytéotique|mise\s+à\s+disposition\s+(?:de\s+|d['’])(?:terrain|toiture|foncier)"
    r"|arrendamiento\s+de\s+(?:terrenos?|cubiertas?)|pronájem\s+pozemk\w*|concessione\s+di\s+(?:aree|superfici)",
    re.I,
)
_PROJECT_TEXT = re.compile(
    r"\bppas?\b|power\s+purchase\s+agreements?|contracts?\s+for\s+difference|build[\s-]own[\s-]operate"
    r"|build[\s-]transfer|umow\w*\s+ppa|stromabnahmevertr\w*|contrat\s+d['’]achat\s+d['’]électricité",
    re.I,
)
#: A short-term power purchase from existing plants is a supply contract, not a project ("Kurzfrist-
#: PPA", 09310000; review of 2026-10-24).
_SHORT_TERM_TEXT = re.compile(
    r"kurzfrist\w*|short[\s-]+term|krótkoterminow\w*|court\s+terme|corto\s+plazo|breve\s+termine", re.I
)
#: Generation and storage technologies: a power purchase that names one buys from such a plant.
GENERATION_TECHNOLOGIES = frozenset(
    {"wind_offshore", "wind", "solar_pv", "bess", "pumped_storage", "hydro", "marine", "nuclear"}
    | {"hydrogen", "geothermal", "biomass"}
)
_FUNDING_TEXT = re.compile(r"\bgrants?\b|funding\s+opportunit\w*|\bloans?\b|cost[\s-]share|subsid\w*", re.I)
#: A funding register also lists requests for information and sources-sought notices: no award.
_RFI_TEXT = re.compile(r"request\s+for\s+information|\brfi\b|sources\s+sought", re.I)
_DEMAND_RESPONSE_TEXT = re.compile(r"demand[\s-]response", re.I)


@dataclasses.dataclass(frozen=True)
class Tag:
    """One type-of-work or audience tag and the evidence for it ("CPV 45251100", "contract
    nature: works", "work type: works")."""

    key: str
    evidence: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {"key": self.key, "evidence": list(self.evidence)}


@dataclasses.dataclass(frozen=True)
class WorkProfile:
    """Both layers for one notice. `work_types` empty means unknown (counted, never guessed)."""

    work_types: tuple[Tag, ...]
    audiences: tuple[Tag, ...]

    @property
    def work_type_keys(self) -> list[str]:
        return [t.key for t in self.work_types]

    @property
    def audience_keys(self) -> list[str]:
        return [t.key for t in self.audiences]

    @property
    def unknown(self) -> bool:
        return not self.work_types

    def as_json(self) -> dict[str, Any]:
        return {
            "work_types": [t.as_json() for t in self.work_types],
            "audiences": [t.as_json() for t in self.audiences],
        }


class _Tags:
    """Ordered tags with their evidence, built up rule by rule."""

    def __init__(self) -> None:
        self._evidence: dict[str, list[str]] = {}

    def add(self, key: str, evidence: str) -> None:
        bucket = self._evidence.setdefault(key, [])
        if evidence not in bucket:
            bucket.append(evidence)

    def drop(self, key: str) -> None:
        self._evidence.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._evidence

    def evidence(self, key: str) -> list[str]:
        return list(self._evidence.get(key, []))

    def __bool__(self) -> bool:
        return bool(self._evidence)

    def tags(self, order: tuple[str, ...]) -> tuple[Tag, ...]:
        return tuple(Tag(k, tuple(self._evidence[k])) for k in order if k in self._evidence)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _strings(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(v) for v in value if v is not None and str(v).strip()]
    try:  # numpy arrays from a parquet round trip
        return [str(v) for v in list(value) if v is not None and str(v).strip()]
    except TypeError:
        return [str(value)]


def _cpv_codes(identifiers: dict[str, Any], raw: dict[str, Any]) -> list[str]:
    codes = (
        _strings(identifiers.get("cpv"))
        or _strings(raw.get("classification-cpv"))
        or _strings(raw.get("cpv"))
    )
    out: list[str] = []
    for code in codes:
        digits = code.strip().split("-")[0]
        if digits and digits not in out:
            out.append(digits)
    return out


def _natures(source_id: str, identifiers: dict[str, Any], raw: dict[str, Any]) -> list[tuple[str, str]]:
    """(nature, evidence) pairs from the source's own contract-type field."""
    out: list[tuple[str, str]] = []
    values = _strings(identifiers.get("contract_nature")) or _strings(raw.get("contract-nature"))
    for value in values:
        nature = _NATURE_ALIASES.get(value.strip().lower())
        if nature:
            out.append((nature, f"contract nature: {value.strip().lower()}"))
    category = (_mapping(raw.get("tender")) or {}).get("mainProcurementCategory")
    if isinstance(category, str) and _NATURE_ALIASES.get(category.lower()):
        out.append((_NATURE_ALIASES[category.lower()], f"procurement category: {category}"))
    if source_id.startswith("mdb.worldbank"):
        group = str(raw.get("procurement_group") or "").strip().upper()
        if group in WORLD_BANK_PROCUREMENT_GROUPS:
            out.append(
                (
                    _NATURE_ALIASES[group.lower()],
                    f"procurement category: {group} ({WORLD_BANK_PROCUREMENT_GROUPS[group]})",
                )
            )
        else:
            ref = str(identifiers.get("bid_reference_no") or raw.get("bid_reference_no") or "")
            hit = _WB_GROUP_IN_REFERENCE.search(ref.upper())
            if hit:
                code = hit.group(1)
                out.append(
                    (
                        _NATURE_ALIASES[code.lower()],
                        f"bid reference {ref.strip()}: {code} ({WORLD_BANK_PROCUREMENT_GROUPS[code]})",
                    )
                )
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for nature, evidence in out:
        if nature not in seen:
            seen.add(nature)
            unique.append((nature, evidence))
    return unique


def _cpv_work_type(code: str) -> str | None:
    for prefix, work in CPV_WORK_TYPES:
        if code.startswith(prefix):
            return work
    return None


def _funding_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    detail = _mapping(raw.get("detail")) or raw
    return [b for b in (_mapping(detail.get(name)) for name in ("synopsis", "forecast")) if b]


def _instruments(raw: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in _funding_blocks(raw):
        for item in block.get("fundingInstruments") or []:
            if isinstance(item, dict) and item.get("id"):
                pair = (str(item["id"]).upper(), str(item.get("description") or item["id"]))
                if pair not in out:
                    out.append(pair)
    return out


def eligible_applicants(source_fields: Any) -> list[Tag]:
    """The applicant types a funding notice says are eligible (Grants.gov `applicantTypes`, and
    "utility" when the eligibility text names utilities), each with its evidence. Empty when the
    source states none, or when the connector did not keep them."""
    raw = _mapping(source_fields)
    found = _Tags()
    for block in _funding_blocks(raw):
        for item in block.get("applicantTypes") or []:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "")
            evidence = f"applicant type {item.get('id')}: {description}".strip()
            for pattern, keys in _APPLICANT_RULES:
                if pattern.search(description):
                    for key in keys:
                        found.add(key, evidence)
                    break
        text = str(block.get("applicantEligibilityDesc") or "")
        hit = _UTILITY_ELIGIBLE.search(text)
        if hit:
            found.add("utility", f"eligibility: {hit.group(0)}")
    return list(found.tags(APPLICANT_TYPES))


def work_profile(notice: Any, source_fields: Any = None) -> WorkProfile:
    """Both layers for one notice. `notice` is the normalised record (a mapping or an object with
    `source_id`, `kind`, `title`, `technologies` and `identifiers`); `source_fields` is the row
    the source published (the record's `raw`), or None when only the stored fields are at hand.
    Pure: the same inputs give the same tags in the same order."""

    def field(name: str) -> Any:
        if isinstance(notice, dict):
            return notice.get(name)
        return getattr(notice, name, None)

    source_id = str(field("source_id") or "")
    kind = str(field("kind") or "")
    title = str(field("title") or "")
    identifiers = _mapping(field("identifiers"))
    raw = _mapping(source_fields)
    text = ted_buyer_title(title) if source_id.startswith("eu.ted") else title
    if source_id.startswith("us.rfp."):
        text = " ".join(t for t in (title, str(raw.get("description") or "")) if t)
    work = _Tags()
    structured = False

    # 1. What the kind and the funding instrument say.
    instruments = _instruments(raw)
    rfi = _RFI_TEXT.search(title) if (instruments or kind in FUNDING_KINDS) else None
    if rfi:
        structured = True
        work.add("other", f'title: "{rfi.group(0)}"')
    elif instruments:
        structured = True
        for code, description in instruments:
            if code == "PC":
                work.add("services", f"funding instrument: {code} ({description})")
            else:
                work.add("funding", f"funding instrument: {code} ({description})")
    elif kind in FUNDING_KINDS:
        structured = True
        work.add("funding", f"kind: {kind}")

    technologies = {t for value in _strings(field("technologies")) for t in value.split("|") if t}

    # 2. A curated utility RFP register's solicitation: a project to develop and sell power or
    #    capacity (the register publishes only generation solicitations, `us_rfp/classify.py`).
    #    Only those registers: another source's `rfp` kind says nothing of what is bought.
    if source_id.startswith("us.rfp."):
        structured = True
        for contract in _strings(raw.get("contract_types")):
            work.add("project", f"contract type: {contract}")
        if ALL_SOURCE in technologies:
            work.add("project", f"technology: {ALL_SOURCE}")
        capacity = CAPACITY_SOLICITATION_RE.search(title)
        if capacity:
            work.add("project", f'title: "{capacity.group(0)}"')
        if _DEMAND_RESPONSE_TEXT.search(text):
            work.add("project", "solicitation: demand response")
        if "project" not in work:
            work.add("project", "source: utility RFP register")

    # 3. CPV codes and the contract nature (TED contract-nature, Find a Tender category, World Bank
    #    procurement group).
    codes = _cpv_codes(identifiers, raw)
    natures = _natures(source_id, identifiers, raw)
    nature_keys = {n for n, _ in natures}
    by_cpv: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for code in codes:
        wt = _cpv_work_type(code)
        if wt is None:
            unmapped.append(code)
        else:
            by_cpv.setdefault(wt, []).append(code)
    if codes or natures:
        structured = True
    # Design-build and supply-and-install notices are works, whatever nature the buyer ticked.
    design_build = _DESIGN_BUILD_TEXT.search(text)
    install = _SUPPLY_INSTALL_TEXT.search(text)
    works_contract = "works" in by_cpv or "works" in nature_keys or bool(design_build)
    # Goods count as a supply only in a supply contract (or one whose nature is not stated and has
    # no construction): in a works or services contract they are its installation scope, and a
    # design-build is works whatever nature the buyer ticked.
    supplies_goods = ("supplies" in nature_keys and not design_build) or (
        not nature_keys and not works_contract
    )
    op_title = _OPERATIONS_TEXT.search(text)
    plant = _plant_evidence(text)
    for code in by_cpv.get("works", []):
        work.add("works", f"CPV {code}")
    for nature, evidence in natures:
        if nature == "works":
            work.add("works", evidence)
    if design_build:
        work.add("works", f'title: "{design_build.group(0)}"')
    if supplies_goods:
        energy = by_cpv.get("energy_supply", [])
        # 09300000 is the parent of the solar codes too: beside equipment it names no supply.
        if energy == ["09300000"] and by_cpv.get("equipment"):
            energy = []
        for code in energy:
            work.add("energy_supply", f"CPV {code}")
        # Plant and equipment codes only. Other goods (vehicles, computers, furniture, medicines,
        # a fuel contract's sundries) are not equipment unless the title names plant
        # (`_plant_evidence`: poles or solar lamps under a construction-materials code).
        for code in by_cpv.get("equipment", []):
            work.add("equipment", f"CPV {code}")
        if plant and by_cpv.get("goods") and not by_cpv.get("equipment") and not energy:
            work.add("equipment", plant)
            for code in by_cpv["goods"][:3]:
                work.add("equipment", f"CPV {code}")
    for code in by_cpv.get("operations", []):
        # A repair-and-maintenance code in a works contract is the build's own scope unless the
        # title names operation or maintenance too; the explicit operation codes always count.
        if works_contract and code.startswith("50") and not op_title:
            continue
        work.add("operations", f"CPV {code}")
    for code in by_cpv.get("site", []):
        work.add("site", f"CPV {code}")
    # Engineering codes in a works, design-build or supply-and-install contract are the
    # contractor's own design and engineering, not a services contract.
    if not works_contract and not install:
        for code in by_cpv.get("services", []):
            work.add("services", f"CPV {code}")
    for nature, evidence in natures:
        if (
            nature == "supplies"
            and source_id.startswith("mdb.worldbank")
            and not {"equipment", "energy_supply"} & set(_keys(work))
        ):
            # The World Bank's goods group buys any goods: equipment only for plant named in
            # the title, otherwise `other` (step 5).
            if plant:
                work.add("equipment", evidence)
                work.add("equipment", plant)
        elif nature == "consulting":
            work.add("services", evidence)
        elif nature == "non_consulting":
            op = _OPERATIONS_TEXT.search(text)
            if op:
                work.add("operations", evidence)
                work.add("operations", f'title: "{op.group(0)}"')

    # 4. The buyer's own title, second. A supply phrase in a works contract is the build's purpose
    #    ("continuity of electricity supply"), not a contract to buy energy.
    supply = energy_supply_phrase(text) if not works_contract else None
    if supply:
        work.add("energy_supply", f'title: "{supply}"')
        if not by_cpv.get("equipment"):
            work.drop("equipment")
    if op_title and not {"funding", "operations"} & set(_keys(work)):
        work.add("operations", f'title: "{op_title.group(0)}"')
    site = _SITE_TEXT.search(text)
    if site:
        work.add("site", f'title: "{site.group(0)}"')
    ppa = _PROJECT_TEXT.search(text)
    if ppa:
        # A power purchase in an energy-supply contract is that supply ("Kurzfrist-PPA",
        # 09310000) unless it buys from a named generation or storage plant and is not
        # short-term; then it is a project for developers.
        from_plant = bool(GENERATION_TECHNOLOGIES & technologies) and not _SHORT_TERM_TEXT.search(text)
        if "energy_supply" in work and not from_plant:
            work.add("energy_supply", f'title: "{ppa.group(0)}"')
        else:
            work.add("project", f'title: "{ppa.group(0)}"')
    # Supply-and-install of plant is both: the plant is bought and built ("Design, Supply,
    # Installation ... of transmission lines", World Bank works contract). Of anything else
    # (software, furniture) it is neither construction nor plant.
    if install and "funding" not in work and (plant or by_cpv.get("equipment")):
        work.add("works", f'title: "{install.group(0)}"')
        work.add("equipment", f'title: "{install.group(0)}"')
    consult = _SERVICES_TEXT.search(text)
    if consult and not design_build and not {"funding", "project"} & set(_keys(work)):
        work.add("services", f'title: "{consult.group(0)}"')
    if not structured:
        built = _WORKS_TEXT.search(text)
        if built and "services" not in work:
            work.add("works", f'title: "{built.group(0)}"')
        bought = _EQUIPMENT_TEXT.search(text)
        if bought and not work:
            # A purchase of plant is equipment; of anything else ("Adquisición de Refrigerio",
            # refreshments) it is read and is `other`.
            if plant:
                work.add("equipment", f'title: "{bought.group(0)}"')
                work.add("equipment", plant)
            else:
                work.add("other", f'title: "{bought.group(0)}"')
        fund = _FUNDING_TEXT.search(text)
        if fund and not work:
            work.add("funding", f'title: "{fund.group(0)}"')

    # 5. Read, and none of the types above (a fuel purchase, waste collection, IT licences).
    if not work and structured:
        for code in (by_cpv.get("fuel", []) + by_cpv.get("goods", []) + unmapped)[:3]:
            work.add("other", f"CPV {code}")
        for _, evidence in natures:
            work.add("other", evidence)
        if not work.evidence("other"):
            work.add("other", f"kind: {kind}" if kind else "source fields read")
    work_tags = work.tags(WORK_TYPES)
    applicants = eligible_applicants(raw) if "funding" in work else []
    return WorkProfile(work_types=work_tags, audiences=audiences_for(work_tags, applicants))


def work_profile_columns(notice: Any, source_fields: Any = None) -> dict[str, Any]:
    """The stored form proposed for lane F1, which adds the columns (no migration in lane F3):
    `work_types` and `audiences` as text arrays in display order (empty: unknown), and
    `work_evidence` as `{"work_types": {key: [evidence]}, "audiences": {key: [evidence]}}`. The
    loader would compute it at load from the record's fields and its link's `raw`, so every reload
    and parser restatement refreshes it, and a one-off pass over the stored links fills the rows
    already held (lane F1)."""
    profile = work_profile(notice, source_fields)
    return {
        "work_types": profile.work_type_keys,
        "audiences": profile.audience_keys,
        "work_evidence": {
            "work_types": {t.key: list(t.evidence) for t in profile.work_types},
            "audiences": {t.key: list(t.evidence) for t in profile.audiences},
        },
    }


def audiences_for(
    work_tags: tuple[Tag, ...], applicants: list[Tag] | tuple[Tag, ...] = ()
) -> tuple[Tag, ...]:
    """Layer (b): who a notice is for, from its types of work (`WORK_TYPE_AUDIENCES`) and, for
    funding, the applicant types the source states as eligible (`APPLICANT_AUDIENCES`). The only
    reader of the two tables, so the mapping lives in one place."""
    audience = _Tags()
    for tag in work_tags:
        for key in WORK_TYPE_AUDIENCES.get(tag.key, ()):
            audience.add(key, f"work type: {tag.key}")
    for applicant in applicants:
        for key in APPLICANT_AUDIENCES.get(applicant.key, ()):
            for evidence in applicant.evidence:
                audience.add(key, evidence)
    return audience.tags(AUDIENCES)


def _plant_evidence(text: str) -> str | None:
    """Plant or equipment the text names (`_PLANT_TEXT`, or a technology other than efficiency
    work, `classify_technologies`), as evidence; None when it names none."""
    hit = _PLANT_TEXT.search(text)
    if hit:
        return f'title: "{hit.group(0)}"'
    named = [t for t in classify_technologies(text) if t != "efficiency"]
    return f"technology named: {named[0]}" if named else None


def _keys(tags: _Tags) -> list[str]:
    return [k for k in WORK_TYPES if k in tags]
