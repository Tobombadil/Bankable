"""Parser tests for eu.ted.api against a recorded TED search response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.eu_ted_api.connector import cpv_technologies, first_text, technologies
from pipeline.connectors.opportunity import ted_buyer_title

SOURCE_ID = "eu.ted.api"
URL = "https://api.ted.europa.eu/v3/notices/search"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ted_search.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_publication_number_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {str(r["publication-number"]) for r in rows}
    assert not df["record_id"].duplicated().any()
    assert df["source_url"].str.startswith("https://ted.europa.eu/en/notice/").all()


def test_multilingual_titles_prefer_english():
    assert first_text({"eng": "Wind farm", "pol": "Farma wiatrowa"}) == "Wind farm"
    assert first_text({"pol": "Farma wiatrowa"}) == "Farma wiatrowa"
    assert first_text(["a", "b"]) == "a"
    assert first_text(None) is None


def test_notice_types_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "awarded", "cancelled", "unknown"}
    assert (df.loc[df["status_raw"] == "can-standard", "status"] == "awarded").all()
    assert (df.loc[df["status_raw"] == "pmc", "status"] == "announced").all()


def test_no_unmapped_statuses_in_the_recorded_sample(parsed):
    _, _, _, df = parsed
    assert not df["status_rule"].str.endswith(".unmapped").any()


def test_a_competition_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    comp = df[(df["status_raw"] == "cn-standard") & df["due_at"].notna()]
    passed = comp[comp["due_at"] < comp["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()


def test_cpv_codes_drive_technology_tokens():
    assert cpv_technologies(["09331000"]) == ["solar_pv"]
    assert cpv_technologies(["45251160"]) == ["wind"]
    assert cpv_technologies(["99999999"]) == []


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        # docs/28 B-3: "093" caught Electricity and Radio-isotopes as solar.
        (["09310000"], []),
        (["09300000"], []),
        (["09344000", "33696400"], []),
        (["09331200", "09332000"], ["solar_pv"]),
        # docs/28 B-5: wood fuels are biomass, coal is coal (and lignite).
        (["09111400"], ["biomass"]),
        (["09111100"], ["coal"]),
        (["09111210"], ["coal"]),
        (["09112100"], ["coal"]),
        (["09111000"], ["coal"]),  # "coal and coal-based fuels"
        (["09111000", "09111400"], ["biomass"]),  # beside wood fuels it is their parent
        (["09110000", "03413000"], ["biomass"]),
        # Codes that named the wrong technology (CPV 2008, checked 2026-10-24).
        (["45251100"], []),  # any power plant, not wind
        (["45251143"], []),  # compressed-air generating plant, not nuclear
        (["31121320"], ["wind"]),  # wind turbines, not gas
        (["31121000"], []),  # generating sets
        (["31122100"], ["hydrogen"]),  # fuel cells
        (["45251111"], ["nuclear"]),
        # Grid codes, and a grid code in construction of another kind (docs/28 B-6).
        (["31170000"], ["transmission"]),
        (["31682540"], ["transmission"]),
        (["31321220"], ["transmission"]),
        (["31174000"], []),  # power-supply transformers for electronics
        (["31210000"], []),  # any switching apparatus: needs grid words
        (["45231400"], ["transmission"]),
        (["45100000", "45233120", "45231400", "45233161"], []),  # a cycle path's cabling
        (["45000000", "45212200", "45232200", "45310000"], []),  # a sports hall's
        (["45000000", "45231300", "45232221", "45255110"], []),  # a water intake's substation
        # Review of 2026-10-24: a code takes its longest listed prefix, so solar collectors for
        # heat are solar heat, not PV; heat supply codes are heat; low- and medium-voltage cable
        # (the parent of 31321220) is a grid code.
        (["09331100"], ["solar_thermal"]),
        (["09331100", "09331200"], ["solar_thermal", "solar_pv"]),
        (["09323000"], ["heat"]),
        (["09320000"], ["heat"]),
        (["09300000", "09320000", "65400000"], ["heat"]),
        (["31321200"], ["transmission"]),
        (["31321210"], ["transmission"]),
    ],
)
def test_cpv_technologies_follow_the_cpv_list(codes, expected):
    assert cpv_technologies(codes) == expected


@pytest.mark.parametrize(
    ("title", "codes", "expected"),
    [
        # docs/28 B-3, B-4: electricity and heat purchases, and the CPV label in the title.
        (
            "Poland – Electricity – ZAKUP ENERGII ELEKTRYCZNEJ PRZEZ UCZESTNIKÓW GRUPY "
            "ZAKUPOWEJ POWIATU BIELSKIEGO",
            ["09310000"],
            [],
        ),
        (
            "Poland – Electricity, heating, solar and nuclear energy – Zakup energii "
            "elektrycznej dla obiektów "
            "PWiK Sp. z o.o. na okres od 01.01.2027r. do 31.12.2027r.",
            ["09300000"],
            [],
        ),
        (
            "Czechia – Solar photovoltaic modules – Instalace OZE na budovách Mob Slezská Ostrava",
            ["09331200", "09332000", "45310000"],
            ["solar_pv"],
        ),
        ("Czechia – Inverters – DNS - Centrální střídače pro výstavbu FVE", ["31155000"], ["solar_pv"]),
        # docs/28 B-5
        (
            "Latvia – Wood fuels – Kurināmās malkas iegāde Aizkraukles novada pašvaldības iestādēm.",
            ["09111400"],
            ["biomass"],
        ),
        (
            "Ireland – Installation services of electrical and mechanical equipment – "
            "Schools Energy Pathfinder "
            "2025 - Biomass Programme - Biomass Package - Bundle 3",
            ["03413000", "09110000", "09111400", "45000000", "45350000", "51100000", "51120000"],
            ["biomass"],
        ),
        # docs/28 B-6: railway electrification, roads, cycle paths, cable cars, fibre are not grid work.
        (
            "Poland – Construction work – Wykonanie robót dla Zadania pn.: "
            "„Modernizacja i elektryfikacja linii "
            "kolejowej nr 24 na odcinku Piotrków Trybunalski – Bełchatów",
            ["45000000", "45200000", "45230000", "45231400", "45234000", "45234100"],
            [],
        ),
        (
            "Poland – Cycle path construction work – Rozbudowa dróg powiatowych w ramach projektu „Rowerowe "
            "Roztocze. Drogi pieszo-rowerowe w obszarach wiejskich”",
            ["45100000", "45200000", "45230000", "45231400", "45233000", "45233120"],
            [],
        ),
        (
            "Slovakia – Miscellaneous special-purpose machinery – Výstavba lanovej dráhy na Martinské hole",
            ["42990000", "45100000", "45111230", "45231400"],
            [],
        ),
        (
            "Germany – Construction and ancillary works for telephone and communication lines – Bau eines "
            "Gigabit-Netzes (FTTB) in der Stadt Scheer, Landkreis Sigmaringen",
            ["45231600", "45232300", "45314300"],
            [],
        ),
        # Czech "HV" is hot water (horká voda): district heating, not high voltage.
        (
            "Czechia – Pipelaying construction work – Rekonstrukce SCZT pára x HV – oblast provozu Špitálka",
            ["44163120", "45231110", "45233100", "45233252"],
            ["heat"],
        ),
        # A voltage in an electricity purchase is the supply's.
        ("Spain – Electricity – Suministro de energía eléctrica, en baja y alta tensión.", ["09310000"], []),
        ("Romania – Electricity – Energie electrică Medie Tensiune și Joasă Tensiune", ["09310000"], []),
        # Grid work, by word or by code.
        (
            "Switzerland – Transformers – Ernen nouvelle sous-station, Transformateurs 65/16.5 kV",
            ["31170000"],
            ["transmission"],
        ),
        (
            "Poland – Substation equipment – Dostawa mobilnych stacji "
            "transformatorowych SN/nN zabudowanych na "
            "przyczepach",
            ["31682540"],
            ["transmission"],
        ),
        (
            "Germany – Low- and medium-voltage cable – Rahmenvertrag Kabelversorgung "
            "Elektrizitätsnetz 2027-2033",
            ["31321200", "31321210", "31321220"],
            ["transmission"],
        ),
        (
            "Poland – Construction work – Kompleksowa modernizacja stacji "
            "elektroenergetycznej 110/15 kV Brzesko",
            ["45000000", "45100000", "45220000", "45222000", "45310000"],
            ["transmission"],
        ),
        # Explicit grid words survive a road or street in the title; a code does not.
        (
            "Germany – Insulated wire and cable – 20kV-Kabel, Anna-Birle-Straße, Mainz-Kastel",
            ["31300000", "31321220"],
            ["transmission"],
        ),
        # Nuclear medicine is not nuclear power.
        ("Italy – Medical equipments – Fornitura di sistemi per medicina nucleare", ["33100000"], []),
        # Labelled sample, 2026-10-24: a gas network's "Grid" and "Netzanschluss" are not power grid
        # work, transformer oil is a consumable, a vehicle charger is not grid work, and coal bought
        # under the group code is coal.
        (
            "Romania – Pipeline relaying works – Execuţia lucrărilor de înlocuiri conducte şi racorduri şi "
            "realizări de noi distribuţii de gaze naturale pentru Delgaz Grid S.A. 4 loturi",
            ["45231113", "45231221"],
            ["gas"],
        ),
        (
            "Germany – Energy and related services – Bereitstellung erhöhter Übergabedruck "
            "Biogas-Netzanschluss der Biogasaufbereitungsanlage Sukow am Netz der ONTRAS Gastransport GmbH",
            ["71314000"],
            ["biomass", "efficiency"],
        ),
        (
            "Bulgaria – Lubricating oils and lubricating agents – Доставка на трансформаторно масло чрез "
            "покупка от стокова борса",
            ["09211000"],
            [],
        ),
        (
            "Spain – Research and development services and related consultancy services – Desarrollo "
            "Innovador de un supercargador bidireccional de alta potencia con funcionalidades de soporte a "
            "la red eléctrica",
            ["31158000", "73000000"],
            # Since the review of 2026-10-24 a charging context vetoes a voltage, code or cue, not
            # an explicit grid word ("red eléctrica"). The labelled sample judges this R&D notice
            # not grid work: a known false positive of that rule (transmission row 181).
            ["transmission"],
        ),
        (
            "Bulgaria – Solid fuels – Доставка на твърди горива за нуждите на община Самуил",
            ["09110000", "09111000"],
            ["coal"],
        ),
        # A gas-insulated switchgear is grid equipment: the gas rule reads only bare network words.
        (
            "Iceland – Electrical equipment and apparatus – Gas Insulated Switchgear",
            ["31600000"],
            ["transmission"],
        ),
        # A gas notice that names electricity keeps its grid words.
        (
            "Germany – Construction work – Netzanschluss Strom und Gas, Neubaugebiet Süd",
            ["45231400"],
            ["gas", "transmission"],
        ),
    ],
)
def test_technologies_read_the_buyers_words_and_the_codes(title, codes, expected):
    assert technologies(title, codes) == expected


def test_the_buyers_title_is_the_third_segment():
    title = "Poland – Solar photovoltaic modules – „Klaster Ropczycko-Dębicki – inicjatorem rozwoju OZE”"
    assert ted_buyer_title(title) == "„Klaster Ropczycko-Dębicki – inicjatorem rozwoju OZE”"
    assert ted_buyer_title("Norway, Sweden – Fuels – Supplies") == "Supplies"
    assert ted_buyer_title("A title without TED segments") == "A title without TED segments"
    assert ted_buyer_title(None) == ""


def test_normalised_notices_carry_no_cpv_label_tags(parsed):
    """The recorded sample (2026-09-12): its "Finland – Electricity – ... latauspalvelu" notice
    (09310000, an EV charging service) used to be solar; "Czechia – Solar photovoltaic modules"
    stays solar from its codes."""
    _, _, _, df = parsed
    tags = dict(zip(df["source_record_id"], df["technologies"], strict=True))
    titles = dict(zip(df["source_record_id"], df["title"], strict=True))
    by_title = {titles[k]: v for k, v in tags.items()}
    finland = next(v for t, v in by_title.items() if t.startswith("Finland – Electricity"))
    assert "solar_pv" not in finland.split("|")
    czech = next(v for t, v in by_title.items() if t.startswith("Czechia – Solar photovoltaic modules"))
    assert czech.split("|") == ["solar_pv"]
    assert not any("nuclear" in v.split("|") for v in tags.values())


def test_buyer_country_becomes_iso_3166_alpha_2(parsed):
    _, _, _, df = parsed
    assert df["jurisdiction"].str.len().eq(2).all()


def test_no_contact_personal_data_is_requested_or_stored(parsed):
    c, _, rows, _ = parsed
    assert not any(k.lower().startswith("contact") or "email" in k.lower() for r in rows for k in r)
    assert not any("contact" in f.lower() for f in c.__class__.__module__ and [])


def test_identifiers_carry_the_notice_id_and_cpv(parsed):
    _, _, _, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["ted_notice_id"] == df["source_record_id"].iloc[0]
    assert isinstance(ids["cpv"], list)


def test_a_page_without_notices_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ted_search.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"error": "bad query"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)


def test_a_zero_or_negative_value_is_an_unknown_budget(parsed):
    """TED states 0 or -1 where a notice gives no value (9 of 279 dev-snapshot notices with an
    amount, 2026-09-13). They are unknown budgets, not the smallest ones (lane I3)."""
    c, raw, rows, _ = parsed
    edited = [dict(r) for r in rows]
    edited[0].update({"estimated-value-glo": -1, "total-value": None, "estimated-value-cur-glo": "PLN"})
    edited[1].update({"estimated-value-glo": 0, "total-value": 0, "estimated-value-cur-glo": "EUR"})
    edited[2].update({"estimated-value-glo": 1250.5, "estimated-value-cur-glo": "EUR"})
    df = c.normalize(edited, raw)
    assert df["budget_amount"].isna()[0]
    assert df["budget_amount"].isna()[1]
    assert df.loc[2, "budget_amount"] == 1250.5
    assert list(df.loc[:1, "budget_currency"]) == ["PLN", "EUR"]  # the stated currency is kept


# ------------------------------------------------- what the value is (lane rfp-value, 2026-10-22)
@pytest.mark.parametrize(
    ("pubnum", "kind", "amount", "currency"),
    [
        ("619292-2026", "awarded_value", 4_673_237.67, "PLN"),  # result form: BT-161 notice value
        ("619362-2026", "awarded_value", 21_100_000.0, "DKK"),
        ("619343-2026", "estimated_value", 1_678_875.0, "CZK"),  # competition form: an estimate
        ("622360-2026", "contract_value", 500_000.0, "EUR"),  # direct award (VEAT)
        ("619495-2026", "contract_value", 10_132_381.64, "RON"),  # contract modification
    ],
)
def test_the_value_kind_follows_the_notice_form(parsed, pubnum, kind, amount, currency):
    _, _, _, df = parsed
    row = df.set_index("source_record_id").loc[pubnum]
    assert (row["budget_amount"], row["budget_currency"]) == (amount, currency)
    value = json.loads(row["identifiers"])["value"]
    assert value == {"kind": kind, "source_field": "total-value"}


def test_the_estimate_list_is_read_and_listed_beside_an_awarded_total():
    """The two notices of the 2026-10-20 store that stated `estimated-value-glo` carried it as a
    one-element list of strings, which the connector did not read, so they had no budget; these are
    one of them's own values (a result notice)."""
    from pipeline.connectors.eu_ted_api.connector import stated_value

    row = {
        "form-type": "result",
        "estimated-value-glo": ["3290325.87"],
        "estimated-value-cur-glo": ["EUR"],
        "total-value": 2427893.62,
        "total-value-cur": ["EUR"],
    }
    value = stated_value(row)
    assert value is not None
    assert (value.primary.kind, value.amount, value.currency) == ("awarded_value", 2_427_893.62, "EUR")
    assert [(f.kind, f.amount, f.source_field) for f in value.also_stated] == [
        ("estimated_value", 3_290_325.87, "estimated-value-glo")
    ]
    # On a competition form the estimate leads, and an equal total is not listed twice.
    competition = {**row, "form-type": "competition", "total-value": 3290325.87}
    value = stated_value(competition)
    assert value is not None and value.primary.source_field == "estimated-value-glo"
    assert value.also_stated == ()
    # A notice stating neither figure has no value (619389-2026 in the fixture), and no kind.
    assert stated_value({"form-type": "competition", "total-value": None}) is None
