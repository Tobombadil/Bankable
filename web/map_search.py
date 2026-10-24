"""The home map's search field (owner decision 2026-10-22, `docs/00-PLAN.md`; `docs/31` §5.18).

One box on the map finds a project, a company or a place, and `web/static/js/map.js` moves the map
to what the reader picks. Nothing here, and nothing in map.js, calls a third-party geocoder.

- **Places** are the US states and counties the map already shades for region-placed proposals:
  the vendored Census cartographic boundary files `GET /v1/geo/regions` serves
  (`pipeline/context/regions.py`, `services/api/regions.py`). Their names and bounding boxes are
  read once per process through that same call and kept here. A place matches when the first word
  typed starts a word of its name and every other word starts a word of its name or its state's
  ("travis tx" and "harris texas" both work). The region quick views the page already offers
  (United States, United Kingdom, Europe) are matched in the browser from the page's own buttons.
- **Projects** are `GET /v1/proposals?q=`, the search the header box and `/search` use (name,
  sponsor or source record id), in every lifecycle state, so a withdrawn project is found too and
  its suggestion says so. Each carries where map.js should go (`lnglat` for a point placed at a
  site, else its county's or state's `bbox`) and the fields the map's drawer prints, provenance
  included (D-2), so the drawer opens on it whether or not the current filters draw it.
- **Companies** are `GET /v1/organizations?q=`. map.js flies to the bounds of a company's projects
  that the current filters draw (`/api/proposals/geo?sponsor_id=`), or opens its page when none
  is. An organisation that is a natural person (`personal_data`, `docs/13` §5.5) is not offered:
  the map's search is for companies, and the minimum of personal data is shown.

Each group asks the API for a few more rows than it shows. Every suggestion carries a `rank`
(`word_rank`, `place_rank`): 0 a whole name or a state's code ("ny"), 1 a name that begins with the
words typed ("duke" for "Duke Energy Carolinas"), 2 one that begins with the letters typed ("Dukes
County"), 3 one with a word that begins with them, 4 the rest (a sponsor or a queue id matched).
map.js shows one list in that order whatever the kind, places before companies before projects
within a rank, so Enter takes the best match (lane stacked-lists, fix round, 2026-10-24).

The site's own calls are not metered by the API's search window (`services/api/ratelimit.py`:
`X-Internal-Token` is exempt); map.js waits for two characters and a pause in typing before it asks.

**Busiest areas** (`busiest_areas`, `GET /api/map/areas` in `web/app.py`; `docs/31` §5.19). The
panel's overview at national and multi-state views names the five states (or countries) holding the
most projects in view. A cluster in the map's own geo answer carries no state, and at national zoom
its cell is 9 degrees wide, so the route asks the API once more for the same filters and bounds at
its finest grid (zoom 12: cells of about 10 km, `services/api/geo.py` `MIN_CELL_DEG`) and puts each
cell in the state, else the country, whose outline holds it, read from the same vendored files.
Region-placed projects carry their county or state, so they need no lookup. A cell across a border
counts wholly to one side; nothing else is estimated. Answers are kept for two minutes per query.
"""

from __future__ import annotations

import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.api.geo import precision_note
from web.api_client import ApiClient, ApiError
from web.formatting import mw
from web.page import get_api
from web.viewmodels import lifecycle_family, lifecycle_label, technology_label, web_relative_url

router = APIRouter()

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 80
PLACE_LIMIT = 5
COMPANY_LIMIT = 4
PROJECT_LIMIT = 6
#: Rows asked of the API per group before the names that begin with the query are put first.
FETCH_LIMIT = 20

#: Independent cities sit in the county file with a county code from 500 (Baltimore city 24510,
#: St. Louis city 29510, Carson City 32510, Virginia's 38): "Baltimore (city), MD", not a second
#: "Baltimore County, MD".
_INDEPENDENT_CITY_STATES = frozenset({"VA", "MD", "MO", "NV"})
_HAS_OWN_NOUN = re.compile(r"\b(County|Parish|Borough|Census Area|Municipality)$")


@dataclass(frozen=True)
class Place:
    kind: str  # "state" | "county"
    region_id: str
    label: str
    detail: str
    bbox: tuple[float, float, float, float]
    name_words: tuple[str, ...]
    words: tuple[str, ...]
    name_key: str
    abbr: str
    #: What the place is, as the map's list prints it beside the name (`county_noun`).
    noun: str = "County"

    def payload(self, rank: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "id": self.region_id,
            "label": self.label,
            "detail": self.detail,
            "noun": self.noun,
            "bbox": list(self.bbox),
        }
        if rank is not None:
            out["rank"] = rank
        return out


def words_of(text: str) -> list[str]:
    """Lower-case ASCII words: "St. Louis" -> ["st", "louis"], "Doña Ana" -> ["dona", "ana"]."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", folded)


def bbox_of(geometry: Mapping[str, Any] | None) -> tuple[float, float, float, float] | None:
    """`(west, south, east, north)` of a GeoJSON (Multi)Polygon. A shape that crosses the
    antimeridian (Alaska's Aleutians reach +179°) keeps its western-hemisphere part, so a fit to it
    is Alaska and not the whole world."""
    if not geometry:
        return None
    xs: list[float] = []
    ys: list[float] = []

    def walk(coords: Any) -> None:
        if coords and isinstance(coords[0], (int, float)):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
            return
        for item in coords or []:
            walk(item)

    walk(geometry.get("coordinates"))
    if not xs:
        return None
    if min(xs) < -170 and max(xs) > 170:
        west_only = [x for x in xs if x < 0]
        return (min(west_only), min(ys), max(west_only), max(ys))
    return (min(xs), min(ys), max(xs), max(ys))


def is_independent_city(geoid: str, state_abbr: str) -> bool:
    return state_abbr in _INDEPENDENT_CITY_STATES and geoid[2:] >= "500"


#: Alaska's census areas (its county equivalents in the Unorganized Borough), current and former;
#: its other county equivalents are boroughs, Anchorage a municipality.
_ALASKA_CENSUS_AREAS = frozenset(
    {
        "aleutians west",
        "bethel",
        "chugach",
        "copper river",
        "dillingham",
        "hoonah angoon",
        "kusilvak",
        "nome",
        "prince of wales hyder",
        "southeast fairbanks",
        "valdez cordova",
        "yukon koyukuk",
    }
)
_OWN_NOUNS = {"census area": "Census area"}


def county_noun(geoid: str, name: str, state_abbr: str) -> str:
    """What a county-level place is called where it is, as the map's list prints it beside the
    name (lane stacked-lists, fix round): "Orleans" in Louisiana is a parish, "Kenai Peninsula" an
    Alaska borough, "Bethel" a census area, "Adjuntas" a municipio."""
    own = _HAS_OWN_NOUN.search(name)
    if own:
        return _OWN_NOUNS.get(own.group(1).lower(), own.group(1))
    if is_independent_city(geoid, state_abbr):
        return "City"
    if state_abbr == "LA":
        return "Parish"
    if state_abbr == "AK":
        key = " ".join(words_of(name))
        if key == "anchorage":
            return "Municipality"
        return "Census area" if key in _ALASKA_CENSUS_AREAS else "Borough"
    if state_abbr == "PR":
        return "Municipio"
    return "County"


def county_label(geoid: str, name: str, state_abbr: str, state_name: str | None) -> str:
    """A county as the map's `regionName` reads it: "Travis County, TX", "Orleans Parish, LA",
    "Baltimore (city), MD", "Kenai Peninsula, AK"."""
    if is_independent_city(geoid, state_abbr):
        noun = "" if name.lower().endswith("city") else " (city)"
    elif state_abbr == "LA":
        noun = " Parish"
    elif state_abbr in ("AK", "PR") or name == state_name or _HAS_OWN_NOUN.search(name):
        noun = ""
    else:
        noun = " County"
    return f"{name}{noun}, {state_abbr}" if state_abbr else f"{name}{noun}"


def _features(api: ApiClient, level: str) -> list[dict[str, Any]]:
    try:
        envelope = api.get("/v1/geo/regions", params={"level": level})
    except ApiError:
        return []
    data = envelope.get("data") or {}
    return [f for f in data.get("features") or [] if isinstance(f, dict)]


def load_places(api: ApiClient) -> list[Place]:
    """Every state and county `GET /v1/geo/regions` serves, with its label and bounding box."""
    places: list[Place] = []
    state_names: dict[str, str] = {}
    for feature in _features(api, "state"):
        props = feature.get("properties") or {}
        region_id, name = str(props.get("region_id") or ""), str(props.get("name") or "")
        box = bbox_of(feature.get("geometry"))
        if not region_id or not name or box is None:
            continue
        abbr = region_id.rsplit("-", 1)[-1]
        state_names[region_id] = name
        name_words = tuple(words_of(name))
        code, key = abbr.lower(), " ".join(name_words)
        words = (*name_words, code)
        places.append(Place("state", region_id, name, "State", box, name_words, words, key, code, "State"))
    for feature in _features(api, "county"):
        props = feature.get("properties") or {}
        geoid, name = str(props.get("region_id") or ""), str(props.get("name") or "")
        state_code = str(props.get("state_code") or "")
        box = bbox_of(feature.get("geometry"))
        if not geoid or not name or box is None:
            continue
        abbr = state_code.rsplit("-", 1)[-1] if state_code else ""
        state_name = state_names.get(state_code)
        label = county_label(geoid, name, abbr, state_name)
        name_words = tuple(words_of(name))
        extra = words_of(label[len(name) :]) + words_of(state_name or "")
        noun = county_noun(geoid, name, abbr)
        places.append(
            Place(
                "county",
                geoid,
                label,
                f"{noun} in {state_name}" if state_name else noun,
                box,
                name_words,
                (*name_words, *extra),
                " ".join(name_words),
                abbr.lower(),
                noun,
            )
        )
    return places


class PlaceIndex:
    """The places, loaded on the first search and kept for the life of the process (the files
    change on redeploy, as `services/api/regions.py`'s own cache assumes). A failed read is not
    kept, so the next search tries again."""

    def __init__(self, loader: Callable[[ApiClient], list[Place]] = load_places) -> None:
        self._lock = threading.Lock()
        self._loader = loader
        self._places: list[Place] | None = None
        self._by_id: dict[str, Place] = {}

    def places(self, api: ApiClient) -> list[Place]:
        with self._lock:
            if self._places is None:
                loaded = self._loader(api)
                if not loaded:
                    return []
                self._places = loaded
                self._by_id = {p.region_id: p for p in loaded}
            return self._places

    def find(self, api: ApiClient, region_id: str | None) -> Place | None:
        if not region_id:
            return None
        self.places(api)
        return self._by_id.get(region_id)

    def reset(self) -> None:
        """Test hook, as every other process-local cache here has."""
        with self._lock:
            self._places = None
            self._by_id = {}


PLACES = PlaceIndex()


def _starts_a_word(token: str, words: tuple[str, ...]) -> bool:
    return any(w.startswith(token) for w in words)


#: `word_rank`'s answers, best first (module docstring): one scale for every kind, so map.js can put a
#: state matched by its code above a company whose name only begins with the same letters.
RANK_WHOLE, RANK_WORDS, RANK_LETTERS, RANK_A_WORD, RANK_OTHER = range(5)


def word_rank(name_words: Sequence[str], tokens: Sequence[str]) -> int:
    """How well a name answers the words typed: the whole name; its first words, whole ("duke" for
    "Duke Energy Carolinas", "ny" for "NY CDG 1 LLC"); its first letters ("duke" for "Dukes
    County"); a word of it begun by each word typed; or none of these (a sponsor or a queue id
    matched). Words, not letters, so "duke" puts Duke Energy above Dukes County."""
    name, typed = list(name_words), list(tokens)
    if not typed:
        return RANK_OTHER
    if name == typed:
        return RANK_WHOLE
    if name[: len(typed)] == typed:
        return RANK_WORDS
    if " ".join(name).startswith(" ".join(typed)):
        return RANK_LETTERS
    if all(_starts_a_word(t, tuple(name)) for t in typed):
        return RANK_A_WORD
    return RANK_OTHER


def place_rank(place: Place, tokens: Sequence[str]) -> int:
    """`word_rank` for a place, where a state's code ("tx") is its whole name, and so is its name
    followed by its own noun or its state ("travis county", "harris tx", "orleans louisiana")."""
    typed = list(tokens)
    if place.kind == "state" and typed == [place.abbr]:
        return RANK_WHOLE
    size = len(place.name_words)
    if typed[:size] == list(place.name_words) and all(t in place.words for t in typed[size:]):
        return RANK_WHOLE
    rank = word_rank(place.name_words, typed)
    if rank == RANK_OTHER and all(_starts_a_word(t, place.words) for t in typed):
        return RANK_A_WORD  # "trav alp": its name, then its state, each begun
    return rank


def ranked_places(places: list[Place], query: str, limit: int = PLACE_LIMIT) -> list[tuple[Place, int]]:
    """The places a query names, best first, each with its `place_rank`: by rank, a state before a
    county, then by label. The first word typed must begin a word of the name, or be a state's code."""
    tokens = words_of(query)
    if not tokens:
        return []
    scored: list[tuple[int, int, str, Place]] = []
    for place in places:
        if not _starts_a_word(tokens[0], place.name_words):
            if not (place.kind == "state" and tokens == [place.abbr]):
                continue
        if not all(_starts_a_word(t, place.words) for t in tokens[1:]):
            continue
        scored.append((place_rank(place, tokens), 0 if place.kind == "state" else 1, place.label, place))
    scored.sort(key=lambda row: row[:3])
    return [(row[3], row[0]) for row in scored[:limit]]


def match_places(places: list[Place], query: str, limit: int = PLACE_LIMIT) -> list[Place]:
    """The places a query names, best first (`ranked_places`)."""
    return [place for place, _rank in ranked_places(places, query, limit)]


def _ranked(
    rows: list[dict[str, Any]], name_key: str, query: str, limit: int
) -> list[tuple[dict[str, Any], int]]:
    """Rows by the `word_rank` of their name, then in the API's order; each with its rank."""
    tokens = words_of(query)
    ranks = [word_rank(words_of(str(row.get(name_key) or "")), tokens) for row in rows]
    order = sorted(range(len(rows)), key=lambda i: (ranks[i], i))
    return [(rows[i], ranks[i]) for i in order[:limit]]


def _list(api: ApiClient, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        envelope = api.get(path, params=dict(params))
    except ApiError:
        return []
    return [row for row in envelope.get("data") or [] if isinstance(row, dict)]


def company_suggestions(api: ApiClient, query: str) -> list[dict[str, Any]]:
    found = _list(api, "/v1/organizations", {"q": query, "limit": FETCH_LIMIT})
    rows = [r for r in found if not r.get("personal_data")]
    out = []
    for row, rank in _ranked(rows, "name_canonical", query, COMPANY_LIMIT):
        slug = row.get("slug")
        out.append(
            {
                "public_id": row.get("public_id"),
                "label": row.get("name_canonical") or row.get("public_id"),
                "detail": "Company",
                "url": f"/organizations/{slug}" if slug else None,
                "rank": rank,
            }
        )
    return out


def _place_words(loc: Mapping[str, Any], county: Place | None, state: Place | None) -> str | None:
    if county is not None:
        return county.label
    if state is not None:
        return state.label
    parts = [loc.get("county_name"), (loc.get("state_code") or "").rsplit("-", 1)[-1] or None]
    return ", ".join(str(p) for p in parts if p) or None


def project_suggestion(api: ApiClient, entity: Mapping[str, Any]) -> dict[str, Any]:
    """One project: its words for the list, where the map goes, and the drawer's fields."""
    loc = entity.get("location") or {}
    geom = loc.get("geom") or {}
    coords = geom.get("coordinates") if geom.get("type") == "Point" else None
    lnglat = [float(coords[0]), float(coords[1])] if coords and len(coords) == 2 else None
    precision = loc.get("precision")
    reason = loc.get("precision_reason")
    county = PLACES.find(api, loc.get("county_fips"))
    state = PLACES.find(api, loc.get("state_code"))
    # A point placed at a site is flown to; a project the source places by county or state (or one
    # its licence shows only at county level) is fitted to that area, as the map shades it.
    target: dict[str, Any] = {}
    if precision == "exact" and lnglat:
        target = {"lnglat": lnglat}
    elif county is not None:
        target = {"bbox": list(county.bbox)}
    elif state is not None:
        target = {"bbox": list(state.bbox)}
    elif lnglat:
        target = {"lnglat": lnglat}
    state_token = entity.get("lifecycle_state")
    capacity = entity.get("capacity_mw")
    detail = " · ".join(
        part
        for part in (
            technology_label(entity.get("technology")),
            f"{mw(capacity)} MW" if capacity not in (None, "") else None,
            _place_words(loc, county, state),
        )
        if part
    )
    slug = entity.get("slug")
    url = web_relative_url(entity.get("url")) or (f"/proposals/{slug}" if slug else None)
    return {
        "public_id": entity.get("public_id"),
        "label": entity.get("name_canonical") or entity.get("public_id"),
        "detail": detail,
        "family": lifecycle_family(state_token),
        "status": lifecycle_label(state_token or "unknown"),
        "url": url,
        **target,
        # The fields map.js's drawer reads from a map feature (`openDrawer`), so a picked project
        # opens the same drawer, source line and licence included.
        "feature": {
            "feature_kind": "proposal",
            "public_id": entity.get("public_id"),
            "name": entity.get("name_canonical"),
            "url": url,
            "kind": entity.get("kind"),
            "technology": entity.get("technology"),
            "lifecycle_state": state_token,
            "capacity_mw": capacity,
            "county_name": loc.get("county_name"),
            "state_code": loc.get("state_code"),
            "precision": precision,
            "precision_reason": reason,
            "precision_note": precision_note(str(precision or ""), reason) if precision else None,
            "provenance": entity.get("provenance") or [],
        },
    }


def project_suggestions(api: ApiClient, query: str) -> list[dict[str, Any]]:
    """A register that lists a hybrid once per technology ("Hop Hill Solar Generation and BESS":
    solar, storage, wind) gives one record each; the list shows the name and place once."""
    rows = _list(api, "/v1/proposals", {"q": query, "limit": FETCH_LIMIT})
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for row, rank in _ranked(rows, "name_canonical", query, len(rows)):
        loc = row.get("location") or {}
        key = (row.get("name_canonical"), loc.get("county_fips") or loc.get("state_code"))
        if key in seen:
            continue
        seen.add(key)
        out.append({**project_suggestion(api, row), "rank": rank})
        if len(out) == PROJECT_LIMIT:
            break
    return out


def clean_query(raw: str | None) -> str:
    return " ".join((raw or "").split())[:MAX_QUERY_LENGTH]


@router.get("/api/map/search")
def map_search(request: Request) -> JSONResponse:
    """Suggestions for the map's search field: `places`, `companies`, `projects` (module docstring).
    Under `/api/`, which robots.txt disallows: suggestions are not a page."""
    query = clean_query(request.query_params.get("q"))
    body: dict[str, Any] = {"q": query, "places": [], "companies": [], "projects": []}
    if len(query) < MIN_QUERY_LENGTH:
        return JSONResponse(body)
    api = get_api(request)
    body["places"] = [p.payload(rank) for p, rank in ranked_places(PLACES.places(api), query)]
    body["companies"] = company_suggestions(api, query)
    body["projects"] = project_suggestions(api, query)
    return JSONResponse(body)


# ---- busiest areas (the panel's overview; module docstring) ----

#: The API's finest cluster grid (`services/api/geo.py`: `MIN_CELL_DEG`, reached from zoom 12).
AREA_ZOOM = "12"
AREA_LIMIT = 5
#: Answers kept per query (filters and bounds), and for how long.
AREA_CACHE_SIZE = 64
AREA_CACHE_SECONDS = 120.0
#: Lookups are kept on a grid of this many cells per degree (about 2 km); the API's cells are 10 km.
_LOOKUP_GRID = 50.0

Ring = list[list[float]]


def _point_in_ring(x: float, y: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@dataclass(frozen=True)
class Area:
    region_id: str
    label: str
    bbox: tuple[float, float, float, float]
    polygons: tuple[tuple[Ring, ...], ...]

    def holds(self, lon: float, lat: float) -> bool:
        west, south, east, north = self.bbox
        if not (west <= lon <= east and south <= lat <= north):
            return False
        return any(
            bool(rings)
            and _point_in_ring(lon, lat, rings[0])
            and not any(_point_in_ring(lon, lat, hole) for hole in rings[1:])
            for rings in self.polygons
        )


def _areas(api: ApiClient, level: str) -> list[Area]:
    out: list[Area] = []
    for feature in _features(api, level):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        region_id, name = str(props.get("region_id") or ""), str(props.get("name") or "")
        box = bbox_of(geometry)
        if not region_id or box is None:
            continue
        coords = geometry.get("coordinates") or []
        polygons = [coords] if geometry.get("type") == "Polygon" else list(coords)
        out.append(Area(region_id, name or region_id, box, tuple(tuple(p) for p in polygons if p)))
    return out


def _load_areas(api: ApiClient) -> tuple[list[Area], list[Area]]:
    return _areas(api, "state"), _areas(api, "country")


class AreaIndex:
    """Point -> the US state, else the country, whose outline holds it; loaded on first use and kept
    for the life of the process, as `PlaceIndex` is. A failed read is not kept."""

    def __init__(self, loader: Callable[[ApiClient], tuple[list[Area], list[Area]]] = _load_areas) -> None:
        self._lock = threading.Lock()
        self._loader = loader
        self._states: list[Area] | None = None
        self._countries: list[Area] = []
        self._by_id: dict[str, Area] = {}
        self._cache: dict[tuple[int, int], str | None] = {}

    def _ready(self, api: ApiClient) -> bool:
        with self._lock:
            if self._states is None:
                states, countries = self._loader(api)
                if not states and not countries:
                    return False
                self._states, self._countries = states, countries
                self._by_id = {a.region_id: a for a in (*countries, *states)}
            return True

    def area(self, api: ApiClient, region_id: str) -> Area | None:
        return self._by_id.get(region_id) if self._ready(api) else None

    def lookup(self, api: ApiClient, lon: float, lat: float) -> str | None:
        if not self._ready(api):
            return None
        key = (math.floor(lon * _LOOKUP_GRID), math.floor(lat * _LOOKUP_GRID))
        if key in self._cache:
            return self._cache[key]
        found = next((a.region_id for a in self._states or [] if a.holds(lon, lat)), None)
        if found is None:
            found = next((a.region_id for a in self._countries if a.holds(lon, lat)), None)
        self._cache[key] = found
        return found

    def reset(self) -> None:
        """Test hook."""
        with self._lock:
            self._states = None
            self._countries = []
            self._by_id = {}
            self._cache = {}


AREAS = AreaIndex()
_area_cache: dict[tuple[tuple[str, str], ...], tuple[float, dict[str, Any]]] = {}
_area_cache_lock = threading.Lock()


def _region_area(api: ApiClient, props: Mapping[str, Any]) -> str | None:
    """The state (or country) a region-placed feature belongs to, from its own id."""
    level, region_id = props.get("region_level"), str(props.get("region_id") or "")
    if level == "county":
        county = PLACES.find(api, region_id)
        return f"US-{county.abbr.upper()}" if county is not None and county.abbr else None
    return region_id or None


def busiest_areas(
    api: ApiClient, params: Mapping[str, str | None], *, now: float | None = None
) -> dict[str, Any]:
    """`{"areas": [...], "total": n}`: the states or countries holding the most projects in the view
    `params` names (the map's own geo query: filters, lifecycle, `listed`, `bbox`), busiest first, at
    most `AREA_LIMIT`. Each area: `id`, `label`, `count`, `capacity_mw` (the stated capacity of the
    projects counted there) and `bbox` (its outline's bounds, for the map to fly to). `total` counts
    every project in view, as the panel's header does."""
    query = {k: str(v) for k, v in params.items() if v not in (None, "") and k != "zoom"}
    key = tuple(sorted(query.items()))
    clock = time.monotonic() if now is None else now
    with _area_cache_lock:
        hit = _area_cache.get(key)
        if hit is not None and clock - hit[0] < AREA_CACHE_SECONDS:
            return hit[1]
    try:
        envelope = api.get("/v1/proposals/geo", params={**query, "zoom": AREA_ZOOM})
    except ApiError:
        return {"areas": [], "total": 0}
    counts: dict[str, int] = {}
    capacity: dict[str, float] = {}
    total = 0
    for feature in (envelope.get("data") or {}).get("features") or []:
        props = feature.get("properties") or {}
        kind = props.get("feature_kind")
        if kind == "region":
            area = _region_area(api, props)
            n = int(props.get("count") or 0)
            mw_sum = float(props.get("capacity_mw_sum") or 0)
        elif kind in ("cluster", "proposal"):
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) != 2:
                continue
            area = AREAS.lookup(api, float(coords[0]), float(coords[1]))
            cluster = kind == "cluster"
            n = int(props.get("count") or 0) if cluster else 1
            mw_sum = float((props.get("capacity_mw_sum") if cluster else props.get("capacity_mw")) or 0)
        else:
            continue
        total += n
        if area is None or n <= 0:
            continue
        counts[area] = counts.get(area, 0) + n
        capacity[area] = capacity.get(area, 0.0) + mw_sum
    areas = []
    for region_id in sorted(counts, key=lambda a: (-counts[a], a))[:AREA_LIMIT]:
        known = AREAS.area(api, region_id)
        areas.append(
            {
                "id": region_id,
                "label": known.label if known is not None else region_id,
                "count": counts[region_id],
                "capacity_mw": round(capacity[region_id], 1),
                "bbox": list(known.bbox) if known is not None else None,
            }
        )
    body: dict[str, Any] = {"areas": areas, "total": total}
    with _area_cache_lock:
        if len(_area_cache) >= AREA_CACHE_SIZE:
            _area_cache.pop(min(_area_cache, key=lambda k: _area_cache[k][0]))
        _area_cache[key] = (clock, body)
    return body


def reset_area_cache() -> None:
    """Test hook."""
    with _area_cache_lock:
        _area_cache.clear()
