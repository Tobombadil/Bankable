"""Links from a parcel to its appraisal district's own record (docs/25-siting-parcels.md §4.7 option (b)):
the owner is read there, under the district's own redactions, never here. Links only: no request is
ever made to an appraisal district's site by this code or any job.

Verified 2026-10-23, county by county, for the counties the parcels lane measured. Each district's
website is the one the Comptroller's county directory names
(https://comptroller.texas.gov/taxes/property-tax/county-directory/<county>.php). Only each site's
robots.txt, its home page and the search platform's own script were read; no property record was
requested (the property-search sites' robots.txt disallows `/Property/` and `/Search/` to robots, which
a person's click is not):

- Jefferson (jcad.org), Newton (newtoncad.org), Jasper (jaspercad.org), Hardin (hardin-cad.org): the
  "esearch" property search (`esearch.<district site>`). Its own script builds a record's address as
  `/property/view/${id}` (`/js/_site.js`, the same file on all four), so the record link is
  `https://esearch.<site>/Property/View/<PROP_ID>`. Not verified with a record: that TxGIO's `PROP_ID`
  is the id this route takes (no TxGIO parcel was downloadable here; docs/25-siting-parcels.md as-built
  section).
- Orange (orangecad.net): the same platform at an older release (`/Content/js/_site.js?2.0...`), whose
  script does not show the record route: search page only.
- Pecos (pecoscad.org), Ward (wardcad.org): a different search site (`/home/PropertyAppraisal`, a
  keyword search form); no record route seen: search page only. Both answer over HTTPS (a HEAD of the
  search page, 200, 2026-10-23), so the links are https.

Every other county links to its page in the Comptroller's appraisal-district directory, which names the
district's website, with the property id to copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

VERIFIED = "2026-10-23"
DIRECTORY_URL = "https://comptroller.texas.gov/taxes/property-tax/county-directory/{slug}.php"


@dataclass(frozen=True)
class District:
    name: str
    search_url: str
    #: The record's address with `{prop_id}`, where verified; else None (search page and the id to copy).
    record_url: str | None = None


DISTRICTS: dict[str, District] = {
    "48245": District(
        "Jefferson Central Appraisal District",
        "https://esearch.jcad.org/",
        "https://esearch.jcad.org/Property/View/{prop_id}",
    ),
    "48351": District(
        "Newton Central Appraisal District",
        "https://esearch.newtoncad.org/",
        "https://esearch.newtoncad.org/Property/View/{prop_id}",
    ),
    "48241": District(
        "Jasper Central Appraisal District",
        "https://esearch.jaspercad.org/",
        "https://esearch.jaspercad.org/Property/View/{prop_id}",
    ),
    "48199": District(
        "Hardin County Appraisal District",
        "https://esearch.hardin-cad.org/",
        "https://esearch.hardin-cad.org/Property/View/{prop_id}",
    ),
    "48361": District("Orange County Appraisal District", "https://esearch.orangecad.net/"),
    "48371": District("Pecos County Appraisal District", "https://www.pecoscad.org/home/PropertyAppraisal"),
    "48475": District("Ward County Appraisal District", "https://www.wardcad.org/home/PropertyAppraisal"),
}


def directory_slug(county_name: str) -> str:
    """The Comptroller directory's page name for a county: lower case, letters only ("Fort Bend" ->
    "fortbend", "De Witt" -> "dewitt")."""
    name = re.sub(r"\s+county$", "", county_name.strip(), flags=re.I)
    return re.sub(r"[^a-z]", "", name.lower())


def cad_link(fips: str | None, prop_id: str | None, county_name: str | None = None) -> dict[str, Any] | None:
    """Where a reader reads a parcel's owner: `kind` "record" (the district's page for the property),
    "search" (the district's search page; copy `property_id`) or "directory" (the Comptroller's page for
    the county, which names the district's site)."""
    if not fips:
        return None
    pid = (prop_id or "").strip() or None
    district = DISTRICTS.get(fips)
    if district is not None:
        if district.record_url and pid:
            return {
                "kind": "record",
                "url": district.record_url.format(prop_id=quote(pid, safe="")),
                "district": district.name,
                "property_id": pid,
                "verified": VERIFIED,
            }
        return {
            "kind": "search",
            "url": district.search_url,
            "district": district.name,
            "property_id": pid,
            "verified": VERIFIED,
        }
    if not county_name:
        return None
    bare = re.sub(r"\s+county$", "", county_name.strip(), flags=re.I)
    return {
        "kind": "directory",
        "url": DIRECTORY_URL.format(slug=directory_slug(county_name)),
        "district": f"{bare} County appraisal district",
        "property_id": pid,
        "verified": None,
    }
