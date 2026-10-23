"""Page fetching and item extraction for the three page formats of tranche 1 (docs/28 §8.2).

Each registry row names one extractor in `connector.extractor`:

- `aep_energy_rfps` (PSO, APCo, SWEPCO, I&M; one AEP page template): fetch the Energy Provider RFPs
  page, then the per-RFP page of each current generation item on the same host; one item per bold
  title line, except year and section headings (`aep_heading_rule`).
- `news_release_index` (CT DEEP): fetch the year's news-release listing, paged back to the run's
  look-back, then each candidate release; one item per release whose title names a generation RFP.
- `notice_table_pdf` (NJ BPU): fetch the public-notice table, then each candidate notice PDF, read
  in memory and never stored; one item per solicitation docket (or notice, without a docket).

**What a run stores** (the snapshot, `bundle`): one JSON document per run holding, per page read,
its URL, HTTP status and either the *region* of the page the extractor reads (the AEP content
column, the CT results list or release body, the NJ notice tables; navigation, scripts and footers
cut away) or, for a PDF, the derived fields only (dates, docket, a target MW, whether a restriction
notice was found, the file's SHA-256). Never a solicitation document: RFP PDFs and the CT DEEP
document store are linked, not fetched (docs/10 US-302 AC1; docs/28 §8.2), and a notice PDF's text
leaves memory only as those fields (docs/28 §8.2, NJ BPU: "keep no bytes").

**Politeness.** Every request goes through `PoliteSession` (identified User-Agent, robots.txt per
host, one token bucket per host, no challenge bypass). The connector sets each host it reads to at
most 0.5 rps and lowers that to a declared `Crawl-delay`. Only the listing host and the row's terms
host are ever requested; a URL anywhere else, or on a `link_only_hosts` host, is refused before any
I/O (a candidate release or notice linked there is noted `off_host` and skipped, not fatal). A 401,
403 or 451, or a challenge page, stops the run (`BlockedError`): no further request to the host in
that run, no retry. It is not a circuit breaker across runs: the run is `blocked`, health shows it,
and the next scheduled tick asks once more (docs/28 §8.2 asks for a circuit break on 403; the
scheduler has none yet). A `tdm-reservation: 1` header or meta tag (the W3C TDM Reservation Protocol,
the machine-readable DSM Art. 4 opt-out) is honoured the same way. Conditional requests
(`If-None-Match` / `If-Modified-Since`) are sent wherever the previous run recorded a validator,
and a 304 reuses the stored entry. The AEP and CT hosts send no validator; www.nj.gov sends both but
answered 200 to both conditionals on 2026-10-21, so there the runner's SHA-256 comparison is what
makes an unchanged day `unchanged`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from pipeline.connectors.base import BlockedError, ConnectorError, ParseError
from pipeline.connectors.http import HttpBlocked, PoliteSession
from pipeline.connectors.us_rfp import classify as clf
from pipeline.connectors.us_rfp import htmltree
from pipeline.connectors.us_rfp.issuers import IssuerRow

#: Region markers per extractor: (start marker, end markers). A row may override them with
#: `connector.region` / `connector.detail_region` ({start, ends}) when an issuer's template differs.
REGIONS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "aep_energy_rfps": {
        "listing": ('id="main"', ('id="GlobalUserControl1"', "RelatedInformationHeader", "<footer")),
        "detail": ('id="main"', ('id="GlobalUserControl1"', "RelatedInformationHeader", "<footer")),
    },
    "news_release_index": {
        "listing": ('id="resultsPanel"', ('class="cg-c-pagination', "<footer")),
        "detail": ('class="content"', ("<h5", 'class="social"', "<footer")),
    },
    "notice_table_pdf": {
        "listing": ('class="sectionTitle"', ('class="right_content', 'id="footer"', "<footer")),
    },
}
BLOCKING_STATUSES = frozenset({401, 403, 451})
MISSING_STATUSES = frozenset({404, 410})
#: A notice PDF larger than this is not read (a notice is a few pages; a large file is a document).
MAX_PDF_BYTES = 15_000_000
MAX_PDF_PAGES = 6
_TDM_META = re.compile(r"<meta[^>]+name=[\"']tdm-reservation[\"'][^>]+content=[\"']1[\"']", re.I)
_CHARSET = re.compile(r"charset=([\w-]+)", re.I)


# =============================================================================== fetching
@dataclass
class Fetcher:
    """One run's requests for one issuer row (module docstring, "Politeness")."""

    http: PoliteSession
    row: IssuerRow
    previous_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    previous_validators: dict[str, dict[str, str]] = field(default_factory=dict)
    validators: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    max_rps: float = 0.5
    requests: int = 0

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {self.row.listing_host}
        if self.row.terms_url:
            hosts.add(urlsplit(self.row.terms_url).netloc.lower())
        return frozenset(hosts)

    def note(self, key: str, value: str) -> None:
        self.notes.setdefault(key, []).append(value)

    def _check_host(self, url: str) -> str:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if parts.scheme != "https":
            raise ConnectorError(f"{self.row.id}: refusing a non-https request to {url}")
        if host in self.row.link_only_hosts:
            raise ConnectorError(f"{self.row.id}: {host} is link-only for this issuer; never fetched")
        if host not in self.allowed_hosts:
            raise ConnectorError(f"{self.row.id}: {host} is not this issuer's listing or terms host")
        return host

    def on_allowed_host(self, url: str) -> bool:
        """Whether `get` would accept the URL's scheme and host; never raises, never requests."""
        try:
            self._check_host(url)
        except ConnectorError:
            return False
        return True

    def _pace(self, url: str, host: str) -> None:
        rate = min(self.http.rate_limits.get(host, self.max_rps), self.max_rps)
        delay = self.http.crawl_delay(url)
        if delay and delay > 0:
            rate = min(rate, 1.0 / delay)
        self.http.rate_limits[host] = rate

    def allowed(self, url: str) -> bool:
        self._check_host(url)
        return self.http.allowed_by_robots(url)

    def get(self, url: str, *, conditional: bool = True) -> tuple[int, bytes, dict[str, str]]:
        """One polite GET. Raises `BlockedError` on a robots disallow, a challenge, a 401/403/451
        or a TDM reservation; `ConnectorError` on any other failure but a 404/410 or a 304, which
        are returned for the caller to decide."""
        host = self._check_host(url)
        self._pace(url, host)
        headers: dict[str, str] = {}
        stored = self.previous_validators.get(url) if conditional and url in self.previous_pages else None
        if stored:
            if stored.get("etag"):
                headers["If-None-Match"] = stored["etag"]
            if stored.get("last_modified"):
                headers["If-Modified-Since"] = stored["last_modified"]
        try:
            resp = self.http.get(url, timeout=60, headers=headers)
        except HttpBlocked as exc:
            raise BlockedError(str(exc)) from exc
        except Exception as exc:  # HttpFailed, connection errors after retries
            raise ConnectorError(f"GET {url} failed: {exc!r}"[:400]) from exc
        self.requests += 1
        status = int(resp.status_code)
        if status in BLOCKING_STATUSES:
            raise BlockedError(f"{host} answered HTTP {status} to an identified client; not retried")
        resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        body = bytes(resp.content or b"")
        if resp_headers.get("tdm-reservation", "").strip() == "1" or _TDM_META.search(
            body[:20000].decode("latin-1")
        ):
            raise BlockedError(f"{url} declares tdm-reservation: 1 (DSM Art. 4 opt-out); not read")
        if status == 304:
            if stored:
                self.validators[url] = dict(stored)
            return status, b"", resp_headers
        if status in MISSING_STATUSES:
            return status, b"", resp_headers
        if status != 200:
            raise ConnectorError(f"GET {url} -> HTTP {status}")
        validator = {
            k: v
            for k, v in (
                ("etag", resp_headers.get("etag")),
                ("last_modified", resp_headers.get("last-modified")),
            )
            if v
        }
        if validator and conditional:
            self.validators[url] = validator
        return status, body, resp_headers

    def page(
        self, url: str, role: str, *, region: tuple[str, tuple[str, ...]] | None, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        """An HTML page as a bundle entry: `{role, url, status, region}`. A 304 returns the previous
        run's entry for the URL; a 404/410 returns None when `missing_ok`, else fails the run."""
        status, body, headers = self.get(url)
        if status == 304:
            if url not in self.previous_pages:
                raise ConnectorError(f"GET {url} -> 304 with no stored entry to reuse")
            reused = dict(self.previous_pages[url])
            reused["role"] = role
            self.note("not_modified", url)
            return reused
        if status in MISSING_STATUSES:
            if missing_ok:
                self.note("missing", f"{status} {url}")
                return None
            raise ConnectorError(f"GET {url} -> HTTP {status}")
        markup = decode(body, headers.get("content-type", ""))
        cut = htmltree.region(markup, region[0], region[1]) if region else markup
        return {"role": role, "url": url, "status": status, "region": cut}


def decode(body: bytes, content_type: str) -> str:
    m = _CHARSET.search(content_type or "")
    encoding = m.group(1) if m else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _region_spec(row: IssuerRow, role: str) -> tuple[str, tuple[str, ...]]:
    override = row.connector.get(f"{role}_region" if role != "listing" else "region")
    if isinstance(override, dict) and override.get("start"):
        return str(override["start"]), tuple(str(e) for e in override.get("ends") or ())
    return REGIONS[row.extractor][role]


def _same_listing_area(url: str, row: IssuerRow) -> bool:
    """A per-RFP page the AEP template links: same host, under the listing's own path."""
    listing = urlsplit(row.listing_url)
    target = urlsplit(url)
    prefix = listing.path if listing.path.endswith("/") else listing.path + "/"
    return target.netloc.lower() == listing.netloc.lower() and target.path.lower().startswith(prefix.lower())


# =============================================================================== AEP template
_DUE_IN_TITLE = re.compile(r"\s*[-\u2013\u2014]\s*proposals?\s+due\b.*$", re.I)
_CLOSED_SUFFIX = re.compile(r"\s*[-\u2013\u2014]+\s*closed\s*$", re.I)
_CLOSED = re.compile(r"\bclosed\b", re.I)


@dataclass
class AepItem:
    title_line: str
    section: str
    description: list[str] = field(default_factory=list)
    link: str | None = None


#: A bold line that only names a year ("2024", "2024 RFPs", "2025-26 Requests for Proposals",
#: "2024 Archive"): a heading over the RFPs of that year, unless it has a page of its own (I&M's
#: "2024 RFPs" links its own per-RFP page, which names its four RFPs, so it is an item).
_YEAR_LABEL = re.compile(
    r"^(?:archived?\s+)?(?:19|20)\d{2}(?:\s*[-\u2013/]\s*(?:19|20)?\d{2})?"
    r"(?:\s+(?:archived?\s+)?(?:rfps?|requests?\s+for\s+proposals?|solicitations?|archive))?\s*:?$",
    re.I,
)
#: A bold "Archive" line ("Archive", "Archived RFPs"): the same section break as an `Archive` heading.
_ARCHIVE_LINE = re.compile(
    r"^archived?s?(?:\s+(?:rfps?|requests?\s+for\s+proposals?|solicitations?))?\s*:?$", re.I
)


def aep_heading_rule(item: AepItem) -> str | None:
    """Why a bold line is a heading rather than an item, as the rule `meta.left_out` counts it
    under, or `None` for an item (lane rfp-view, 2026-10-23). An item, in this template, is a bold
    title with a description, a page of its own, or both. `heading.year`: a year label with no page
    of its own, even with an introductory paragraph under it (read as an item, that paragraph would
    lend the heading the words of the RFPs below and load it as one, status unknown).
    `heading.section`: any other bold line with neither a description nor a page (a section name
    such as "Current RFPs", or a bold "Archive" line). Headings are never items, so they are left out
    before classification."""
    if item.link is not None:
        return None
    title = htmltree.clean_text(aep_clean_title(item.title_line))
    if _YEAR_LABEL.match(title):
        return "heading.year"
    if not item.description:
        return "heading.section"
    return None


def aep_items(region: str | None, base_url: str) -> list[AepItem]:
    """The listing's items in page order: a bold paragraph starts one, the paragraphs after it are
    its description, the first link after it is its page. An `Archive` heading, or a bold `Archive`
    line, starts the archive section. Headings come back too, for `aep_records` to count them
    (`aep_heading_rule`). Raises `ParseError` when the content column is missing (a template
    change)."""
    if not region:
        raise ParseError(f"{base_url}: the AEP content column (id=main) is not on the page")
    root = htmltree.parse(region)
    items: list[AepItem] = []
    section = "current"
    current: AepItem | None = None
    for node in root.iter():
        if node.tag in ("h2", "h3") and node.text().strip().lower().startswith("archive"):
            section = "archive"
            current = None
        elif node.tag == "p" and "text-bold" in node.classes:
            line = node.text()
            if line and _ARCHIVE_LINE.match(htmltree.clean_text(line)):
                # A section break, not an item: nothing after it is its description or its page.
                section = "archive"
                items.append(AepItem(title_line=line, section=section))
                current = None
            elif line:
                current = AepItem(title_line=line, section=section)
                items.append(current)
        elif node.tag == "p" and current is not None and current.link is None:
            text = node.text()
            if text:
                current.description.append(text)
        elif node.tag == "a" and current is not None and current.link is None and node.attrs.get("href"):
            href = node.attrs["href"].strip()
            if not href.lower().startswith(("mailto:", "tel:", "javascript:")):
                current.link = urljoin(base_url, href)
    return items


def aep_clean_title(line: str) -> str:
    return _CLOSED_SUFFIX.sub("", _DUE_IN_TITLE.sub("", line)).strip(" -\u2013\u2014")


#: The bidders' deadline. A label qualified by "self-build" is the utility's own deadline, never the
#: bidders': SWEPCO's 2026 All-Source page lists "Self-Build Proposal Due Date: 7/28/2026" just
#: before "Proposal Due Date: 7/29/2026", and the first labelled date wins (`aep_records`). The text
#: is whitespace-collapsed (`htmltree.clean_text`), so fixed-width lookbehinds are enough.
_AEP_DUE_LABEL = re.compile(
    r"(?<!self-build )(?<!self build )(?<!self\u2013build )"
    r"(?:(?:proposals?|bids?)\s+due(?:\s+date)?|proposal\s+submission\s+deadline)",
    re.I,
)
_AEP_FINAL_ISSUED = re.compile(r"final\s+rfps?\s+issued|issue\s+final\s+rfps?", re.I)
_AEP_ISSUED = re.compile(
    r"(?<!draft )rfps?\s+issued|(?<!draft )rfps?\s+release(?:d)?|issue\s+rfps?|rfp\s+issue\s+date", re.I
)


def fetch_aep(fx: Fetcher, now: dt.datetime) -> tuple[list[dict[str, Any]], bool]:
    row = fx.row
    listing = fx.page(row.listing_url, "listing", region=_region_spec(row, "listing"))
    assert listing is not None  # noqa: S101 - page() raises unless missing_ok
    pages = [listing]
    detail_urls: list[str] = []
    for item in aep_items(listing.get("region"), row.listing_url):
        if item.section != "current" or _CLOSED.search(item.title_line) or not item.link:
            continue
        if not clf.classify(item.title_line, " ".join(item.description)).publishable:
            continue
        url = urldefrag(item.link)[0]
        if _same_listing_area(url, row) and url not in detail_urls:
            detail_urls.append(url)
    cap = int(row.connector.get("max_detail_pages") or 8)
    truncated = len(detail_urls) > cap
    for url in detail_urls[:cap]:
        if not fx.allowed(url):
            fx.note("robots_disallowed", url)
            continue
        page = fx.page(url, "detail", region=_region_spec(row, "detail"), missing_ok=True)
        if page is not None:
            pages.append(page)
    return pages, truncated


def aep_records(bundle: dict[str, Any], row: IssuerRow) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = bundle.get("pages") or []
    listing = next((p for p in pages if p.get("role") == "listing"), None)
    if listing is None:
        raise ParseError(f"{row.id}: the bundle holds no listing page")
    details = {urldefrag(p["url"])[0]: p for p in pages if p.get("role") == "detail"}
    items = aep_items(listing.get("region"), row.listing_url)
    # A per-RFP page linked from several items (APCo's storage PSA and CPA share one) is not read
    # for dates: its schedule cannot be assigned to one item.
    link_counts: dict[str, int] = {}
    for item in items:
        if item.link:
            base = urldefrag(item.link)[0]
            link_counts[base] = link_counts.get(base, 0) + 1
    kept: list[dict[str, Any]] = []
    left_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        description = " ".join(item.description)
        title = aep_clean_title(item.title_line)
        key = _aep_key(item, title)
        record = _blank_record(row, key, item.link or row.listing_url, title)
        heading = aep_heading_rule(item)
        if heading is not None:
            # A year or section heading: never an item, counted under its own rule (lane rfp-view).
            record.update(
                title_line=item.title_line,
                section=item.section,
                description=description,
                classification="heading",
                classification_rule=heading,
            )
            left_out.append(record)
            continue
        verdict = clf.classify(
            item.title_line, description, ignore=tuple(row.connector.get("classify_ignore") or ())
        )
        record.update(
            title_line=item.title_line,
            section=item.section,
            description=description,
            classification=verdict.category,
            classification_rule=verdict.rule,
        )
        if not verdict.publishable:
            left_out.append(record)
            continue
        if key in seen:
            record["classification_rule"] = "duplicate_key"
            left_out.append(record)
            continue
        seen.add(key)
        record["status_raw"] = (
            "closed"
            if _CLOSED.search(item.title_line)
            else "archived"
            if item.section == "archive"
            else "listed"
        )
        due = clf.labelled_dates(item.title_line, _AEP_DUE_LABEL)
        item_page = urldefrag(item.link)[0] if item.link else None
        detail = details.get(item_page) if item_page else None
        usable_detail = (
            detail is not None and link_counts.get(item_page or "", 0) == 1 and detail.get("region")
        )
        detail_text = ""
        if usable_detail and detail is not None:
            detail_text = htmltree.parse(str(detail["region"])).text()
            record["detail_url"] = detail["url"]
        if not due and detail_text:
            due = clf.labelled_dates(detail_text, _AEP_DUE_LABEL)
        if due:
            record["due_text"] = due[0][0]
            when = clf.deadline(due[0][0], due[0][1])
            record["due_at"] = when.isoformat() if when else None
        if detail_text:
            opened = clf.labelled_dates(detail_text, _AEP_FINAL_ISSUED) or clf.labelled_dates(
                detail_text, _AEP_ISSUED
            )
            if opened:
                day = clf.parse_date(opened[0][0])
                record["open_text"] = opened[0][0]
                record["open_at"] = day.isoformat() if day else None
        _facts(record, title, description, detail_text)
        kept.append(record)
    return kept, left_out


def _aep_key(item: AepItem, title: str) -> str:
    if item.link:
        parts = urlsplit(item.link)
        key = parts.path.lower().rstrip("/")
        return f"{key}#{parts.fragment.lower()}" if parts.fragment else key
    return "title:" + hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:12]  # noqa: S324 - identity key


# =============================================================================== CT DEEP news index
_PAGE_OF = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.I)
_RELEASE_VERB = re.compile(r"\b(?:announces?|releases?|issues?|launch(?:es)?|opens?|seeks?|seeking)\b", re.I)
#: A release about a step before the solicitation opens: a draft RFP, a request for comment that
#: "will inform a forthcoming request for proposals", a notice of proceeding. Announced, not open.
_PRE_SOLICITATION = re.compile(
    r"\bdraft\b|\brequest for (?:written )?comments?\b|\bnotice of proceeding\b"
    r"|\bforthcoming (?:request for proposals|rfp|procurement|solicitation)\b",
    re.I,
)
#: A lead that says the release is the final solicitation ("released a final Request for Proposals")
#: is not a pre-solicitation step, even when it mentions the earlier draft. The title still decides
#: for a title that says "draft".
_FINAL_SOLICITATION = re.compile(r"\bfinal (?:request for proposals|rfps?|solicitation)\b", re.I)
_CT_DUE_SENTENCE = re.compile(
    r"\baccept(?:s|ing)?\s+(?:bids|proposals|applications|submissions)\b|"
    r"\b(?:bids|proposals|applications|submissions)\s+(?:are|will be|must be)\s+(?:due|submitted|received)\b|"
    r"\b(?:submission|bid|proposal|application)\s+deadline\b|\bdue\s+date\s+for\s+(?:bids|proposals)\b|"
    r"\bproposals?\s+due\b",
    re.I,
)
_LEAD_MIN_CHARS = 60
_CT_DUE_DATE = re.compile(r"\b(?:through|until|by|on|is|:)\s+(" + clf.DATE + r")", re.I)


@dataclass
class NewsItem:
    date: dt.date
    url: str
    title: str
    snippet: str


def news_items(region: str | None, base_url: str) -> tuple[list[NewsItem], int | None]:
    """The listing page's releases and its page count ("Page 1 of 10"), or ParseError when the
    results panel is missing."""
    if region is None:
        raise ParseError(f"{base_url}: the news-release results panel is not on the page")
    root = htmltree.parse(region)
    total: int | None = None
    m = _PAGE_OF.search(root.text())
    if m:
        total = int(m.group(2))
    items: list[NewsItem] = []
    for li in root.find_all("li"):
        date_node = li.find("span", "date")
        link = li.find("a")
        if date_node is None or link is None or not link.attrs.get("href"):
            continue
        day = clf.parse_date(date_node.text())
        if day is None:
            continue
        para = li.find("p")
        items.append(
            NewsItem(
                date=day,
                url=urljoin(base_url, link.attrs["href"].strip()),
                title=link.text(),
                snippet=para.text() if para is not None else "",
            )
        )
    return items, total


def news_window_start(
    now: dt.datetime, previous_retrieved_at: dt.datetime | None, row: IssuerRow
) -> dt.datetime:
    """The oldest release date a run reads back to: the previous fetch minus `overlap_days`, or
    `first_run_lookback_days` before now on a first run."""
    overlap = dt.timedelta(days=int(row.connector.get("overlap_days") or 7))
    if previous_retrieved_at is not None and previous_retrieved_at <= now:
        return previous_retrieved_at - overlap
    return now - dt.timedelta(days=int(row.connector.get("first_run_lookback_days") or 365))


def fetch_news(fx: Fetcher, now: dt.datetime, since: dt.datetime) -> tuple[list[dict[str, Any]], bool]:
    row = fx.row
    max_pages = int(row.connector.get("max_pages") or 25)
    pages: list[dict[str, Any]] = []
    read = 0
    truncated = False
    candidates: list[NewsItem] = []
    ignore = tuple(row.connector.get("classify_ignore") or ())
    # The year's listing (`news-releases---<year>`) may not exist in the first days of January.
    # Within `new_year_grace_days` of 1 January a missing current-year listing is noted and the run
    # also reads the previous year's; after that, a missing current-year listing fails the run.
    grace = dt.timedelta(days=int(row.connector.get("new_year_grace_days") or 14))
    in_grace = now - dt.datetime(now.year, 1, 1, tzinfo=now.tzinfo) < grace
    first_year = min(since.year, now.year - 1) if in_grace else since.year
    for year in range(now.year, first_year - 1, -1):
        page_no = 1
        while True:
            if read >= max_pages:
                truncated = True
                break
            url = row.listing_url.format(year=year) + (f"?page={page_no}" if page_no > 1 else "")
            page = fx.page(
                url, "listing", region=_region_spec(row, "listing"), missing_ok=year != now.year or in_grace
            )
            read += 1
            if page is None:
                break
            pages.append(page)
            items, total = news_items(page.get("region"), url)
            for item in items:
                if (
                    item.date >= since.date()
                    and clf.classify(item.title, item.snippet, ignore=ignore).publishable
                ):
                    candidates.append(item)
            if not items or min(i.date for i in items) < since.date() or total is None or page_no >= total:
                break
            page_no += 1
        if truncated:
            break
    cap = int(row.connector.get("max_detail_pages") or 10)
    candidates.sort(key=lambda i: i.date, reverse=True)
    if len(candidates) > cap:
        truncated = True
    for item in candidates[:cap]:
        if not fx.on_allowed_host(item.url):
            fx.note("off_host", item.url)
            continue
        if not fx.allowed(item.url):
            fx.note("robots_disallowed", item.url)
            continue
        page = fx.page(item.url, "detail", region=_region_spec(row, "detail"), missing_ok=True)
        if page is not None:
            pages.append(page)
    return pages, truncated


def news_records(bundle: dict[str, Any], row: IssuerRow) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ignore = tuple(row.connector.get("classify_ignore") or ())
    pages = bundle.get("pages") or []
    details = {p["url"]: p for p in pages if p.get("role") == "detail"}
    try:
        since = dt.date.fromisoformat(str(bundle.get("window_start") or ""))
    except ValueError:
        since = dt.date.min
    kept: list[dict[str, Any]] = []
    left_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        if page.get("role") != "listing":
            continue
        items, _ = news_items(page.get("region"), page["url"])
        for item in items:
            key = urlsplit(item.url).path.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            verdict = clf.classify(item.title, item.snippet, ignore=ignore)
            record = _blank_record(row, key, item.url, item.title)
            record.update(
                description=item.snippet,
                published=item.date.isoformat(),
                classification=verdict.category,
                classification_rule=verdict.rule,
            )
            if not verdict.publishable:
                left_out.append(record)
                continue
            detail = details.get(item.url)
            if item.date < since or detail is None or not detail.get("region"):
                # Not read this run (outside the window, or its page missing): an incremental source
                # carries the earlier row forward, so emitting a thinner copy would erase its facts.
                record["classification_rule"] = "not_read_this_run"
                left_out.append(record)
                continue
            body = htmltree.parse(str(detail["region"]))
            heading = body.find("h1")
            if heading is not None and heading.text():
                record["title"] = heading.text()
            # The lead is the first paragraph of prose; the release's date line is a paragraph too.
            paragraphs = [p.text() for p in body.find_all("p") if len(p.text()) >= _LEAD_MIN_CHARS]
            lead = paragraphs[0] if paragraphs else item.snippet
            text = body.text()
            record["description"] = lead
            record["detail_url"] = detail["url"]
            record["document_links"] = sorted(
                {
                    urljoin(detail["url"], a.attrs["href"])
                    for a in body.find_all("a")
                    if a.attrs.get("href")
                    and urlsplit(urljoin(detail["url"], a.attrs["href"])).netloc.lower()
                    in row.link_only_hosts
                }
            )
            draft = bool(
                _PRE_SOLICITATION.search(item.title)
                or (_PRE_SOLICITATION.search(lead) and not _FINAL_SOLICITATION.search(lead))
            )
            record["status_raw"] = "pre-solicitation" if draft else "released"
            if not draft and _RELEASE_VERB.search(item.title):
                record["open_at"] = item.date.isoformat()
                record["open_text"] = f"release dated {item.date.isoformat()}"
            due = _latest_due(text)
            if due is not None:
                record["due_text"], when = due
                record["due_at"] = when.isoformat()
            _facts(record, str(record["title"]), lead, "")
            kept.append(record)
    return kept, left_out


def _latest_due(text: str) -> tuple[str, dt.datetime] | None:
    """The latest deadline stated in a sentence about accepting bids ("DEEP will accept bids ...
    through February 27, 2026, at 11:59 PM EPT for projects also bidding into the Maine RFP, and
    through March 17, 2026, at 12:00 PM EPT for all other bids" -> 17 March, 16:00 UTC)."""
    best: tuple[str, dt.datetime] | None = None
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
        if not _CT_DUE_SENTENCE.search(sentence):
            continue
        for m in _CT_DUE_DATE.finditer(sentence):
            when = clf.deadline(m.group(1), sentence[m.end() : m.end() + 40])
            if when is not None and (best is None or when > best[1]):
                best = (m.group(1), when)
    return best


# =============================================================================== NJ BPU notice table
_SECTION_YEAR = re.compile(r"\b(20\d{2})\b")
_RESTRICTION = re.compile(
    r"\u00a9|\bcopyright\b|all rights reserved|may not be (?:reproduced|copied|distributed|republished)|"
    r"not for (?:public )?(?:distribution|redistribution)",
    re.I,
)
_DOCKET = re.compile(r"docket\s+no\.?\s*([A-Z]{2}\d{8,})", re.I)
#: The notice's own date line ("Dated: July 30, 2026"); "Order dated March 4, 2026" is a citation.
_DATED = re.compile(r"\bdated:\s+(" + clf.DATE + r")", re.I)
#: Deadline labels. The "extended to" form comes first so that, in "the Final Application deadline
#: for Tranche 2 of Phase 1 of the ... Program has been extended to 11:59 p.m. ET on October 5,
#: 2026", the label runs to "extended to" rather than stopping at the first "of".
_PDF_DUE = re.compile(
    r"(?:final\s+application|bid|proposal|submission)s?\s+deadline\b[^.]{0,160}?\bextended\s+to\b"
    r"|(?:revised\s+)?deadline\s+for\s+(?:the\s+)?(?:final\s+)?(?:bid|proposal|application)s?\b"
    r"(?:\s+submission)?\*?"
    r"|(?:final\s+application|bid|proposal|submission)s?\s+deadline\s+(?:for\s+[^.]{0,80}?)?"
    r"(?:has\s+been\s+|is\s+hereby\s+|will\s+be\s+)?\b(?:extended\s+to|is|of)\b",
    re.I,
)
_PDF_OPEN = re.compile(
    r"(?:solicitation|application|prequalification|pre-qualification)\s+window\s+(?:opening|opens?|opened)\s+on",
    re.I,
)


@dataclass
class Notice:
    year: int
    title: str
    url: str
    issued: dt.date | None
    issued_text: str


def notice_rows(region: str | None, base_url: str) -> list[Notice]:
    """Every notice row of the year tables, with its year from the table's section title. Raises
    `ParseError` when the page holds no notice table (a template change)."""
    if region is None:
        raise ParseError(f"{base_url}: the public-notice tables are not on the page")
    root = htmltree.parse(region)
    out: list[Notice] = []
    year: int | None = None
    tables = 0
    for node in root.iter():
        if node.tag == "div" and "sectionTitle" in node.classes:
            m = _SECTION_YEAR.search(node.text())
            year = int(m.group(1)) if m else None
        elif node.tag == "table" and year is not None:
            tables += 1
            for tr in node.find_all("tr"):
                cells = [c for c in tr.children if isinstance(c, htmltree.Node) and c.tag in ("td", "th")]
                if len(cells) < 2 or cells[0].tag == "th":
                    continue
                link = cells[0].find("a")
                title = cells[0].text()
                if link is None or not link.attrs.get("href") or not title:
                    continue
                issued_text = cells[1].text()
                out.append(
                    Notice(
                        year=year,
                        title=title,
                        url=urljoin(base_url, link.attrs["href"].strip()).replace(" ", "%20"),
                        issued=_month_day(issued_text, year),
                        issued_text=issued_text,
                    )
                )
    if not tables:
        raise ParseError(f"{base_url}: no public-notice table under a year heading")
    return out


def _month_day(text: str, year: int) -> dt.date | None:
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", text or "")
    if not m:
        return None
    return clf.parse_date(f"{m.group(1)} {m.group(2)}, {year}")


def pdf_fields(body: bytes, extract_text: Callable[[bytes], str] | None = None) -> dict[str, Any]:
    """The derived fields of one notice PDF, read in memory (module docstring). The text itself is
    never returned."""
    text = (extract_text or pdf_text)(body)
    flat = " ".join(text.split())
    restricted = _RESTRICTION.search(flat)
    fields: dict[str, Any] = {
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "restricted": bool(restricted),
        "restriction_phrase": restricted.group(0) if restricted else None,
        "docket": None,
        "dated": None,
        "due_text": None,
        "due_at": None,
        "open_text": None,
        "open_at": None,
        "target_mw": None,
    }
    if restricted:
        return fields
    m = _DOCKET.search(flat)
    fields["docket"] = m.group(1).upper() if m else None
    m = _DATED.search(flat)
    day = clf.parse_date(m.group(1)) if m else None
    fields["dated"] = day.isoformat() if day else None
    dues = [
        (when, date_text)
        for date_text, before, after in _dates_after(flat, _PDF_DUE)
        if (when := clf.deadline(date_text, before if _TIME_BEFORE_DATE.match(before) else after)) is not None
    ]
    if dues:
        latest, latest_text = max(dues)
        fields["due_at"] = latest.isoformat()
        fields["due_text"] = latest_text
    opens = [d for t, _b, _a in _dates_after(flat, _PDF_OPEN) if (d := clf.parse_date(t)) is not None]
    if opens:
        fields["open_at"] = min(opens).isoformat()
        fields["open_text"] = fields["open_at"]
    fields["target_mw"] = clf.target_mw(flat)
    return fields


#: Nothing but a time and zone between a deadline label and its date ("extended to 11:59 p.m. ET on
#: October 5, 2026"), which `classify.deadline` then reads as the time of day.
_TIME_BEFORE_DATE = re.compile(
    r"^\s*(?:at|by)?\s*\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?\s*\(?\s*[ecmp][sdp]?t\b\)?\s*(?:on\s*|,\s*)?$",
    re.I,
)


def _dates_after(text: str, label: re.Pattern[str]) -> list[tuple[str, str, str]]:
    """(date, the words between the label and the date, the 40 characters after the date) for the
    first date within 60 characters after each label match ("deadline ... has been extended to
    11:59 p.m. ET on October 5, 2026")."""
    out: list[tuple[str, str, str]] = []
    date_re = re.compile(clf.DATE, re.I)
    for m in label.finditer(text):
        window = text[m.end() : m.end() + 60]
        d = date_re.search(window)
        if d:
            end = m.end() + d.end()
            out.append((d.group(0), window[: d.start()], text[end : end + 40]))
    return out


def pdf_text(body: bytes) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages[:MAX_PDF_PAGES])
    except Exception as exc:  # pdfminer raises a zoo of types on a damaged file
        raise ParseError(f"notice PDF could not be read: {exc!r}"[:300]) from exc


def fetch_notices(fx: Fetcher, now: dt.datetime) -> tuple[list[dict[str, Any]], bool]:
    row = fx.row
    listing = fx.page(row.listing_url, "listing", region=_region_spec(row, "listing"))
    assert listing is not None  # noqa: S101 - page() raises unless missing_ok
    pages = [listing]
    if row.listing_url in fx.notes.get("not_modified", []):
        # The index is unchanged: so is the set of notices it points at. Re-use every stored entry.
        pages.extend(p for u, p in fx.previous_pages.items() if p.get("role") == "notice")
        for url in (u for u, p in fx.previous_pages.items() if p.get("role") == "notice"):
            if url in fx.previous_validators:
                fx.validators[url] = dict(fx.previous_validators[url])
        return pages, False
    lookback = dt.timedelta(days=int(row.connector.get("lookback_days") or 400))
    ignore = tuple(row.connector.get("classify_ignore") or ())
    wanted: list[str] = []
    for notice in notice_rows(listing.get("region"), row.listing_url):
        if notice.issued is None or notice.issued < (now - lookback).date():
            continue
        if not clf.classify(notice.title, "", ignore=ignore).publishable:
            continue
        if notice.url not in wanted:
            wanted.append(notice.url)
    cap = int(row.connector.get("max_documents") or 10)
    truncated = len(wanted) > cap
    for url in wanted[:cap]:
        if not fx.on_allowed_host(url):
            fx.note("off_host", url)
            continue
        if not fx.allowed(url):
            fx.note("robots_disallowed", url)
            continue
        status, body, _headers = fx.get(url)
        if status == 304:
            pages.append(dict(fx.previous_pages[url]))
            fx.note("not_modified", url)
            continue
        if status in MISSING_STATUSES:
            fx.note("missing", f"{status} {url}")
            continue
        if len(body) > MAX_PDF_BYTES or not body.startswith(b"%PDF"):
            fx.note("not_a_notice_pdf", url)
            continue
        pages.append({"role": "notice", "url": url, "status": status, "fields": pdf_fields(body)})
    return pages, truncated


def notice_records(
    bundle: dict[str, Any], row: IssuerRow, retrieved: dt.date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = bundle.get("pages") or []
    listing = next((p for p in pages if p.get("role") == "listing"), None)
    if listing is None:
        raise ParseError(f"{row.id}: the bundle holds no listing page")
    pdfs = {p["url"]: p.get("fields") or {} for p in pages if p.get("role") == "notice"}
    lookback = dt.timedelta(days=int(row.connector.get("lookback_days") or 400))
    ignore = tuple(row.connector.get("classify_ignore") or ())
    kept: dict[str, dict[str, Any]] = {}
    left_out: list[dict[str, Any]] = []
    for notice in notice_rows(listing.get("region"), row.listing_url):
        verdict = clf.classify(notice.title, "", ignore=ignore)
        record = _blank_record(row, urlsplit(notice.url).path.lower(), notice.url, notice.title)
        record.update(
            published=notice.issued.isoformat() if notice.issued else None,
            classification=verdict.category,
            classification_rule=verdict.rule,
        )
        if not verdict.publishable:
            left_out.append(record)
            continue
        if notice.issued is None or notice.issued < retrieved - lookback:
            record["classification_rule"] = "outside_lookback"
            left_out.append(record)
            continue
        fields = pdfs.get(notice.url)
        if fields is None:
            record["classification_rule"] = "notice_not_read"
            left_out.append(record)
            continue
        if fields.get("restricted"):
            # NJ's grant excepts "particular material or information to which a restriction on free
            # use may apply" (docs/28 §8.2): a notice carrying a restriction is skipped entirely.
            record["classification_rule"] = "restricted_material"
            left_out.append(record)
            continue
        record.update(
            status_raw="notice",
            docket=fields.get("docket"),
            due_text=fields.get("due_text"),
            due_at=fields.get("due_at"),
            open_text=fields.get("open_text"),
            open_at=fields.get("open_at"),
            notices=[notice.url],
        )
        _facts(record, notice.title, "", "")
        if fields.get("target_mw") is not None:
            record["capacity_sought_mw"] = fields["target_mw"]
            record["mw_stated"] = [fields["target_mw"]]
        if record["docket"]:
            record["key"] = f"docket:{str(record['docket']).lower()}"
        _merge_notice(kept, record)
    return list(kept.values()), left_out


def _merge_notice(kept: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    """Notices on one docket are one solicitation: the latest notice names it and links it; the
    latest stated deadline and the earliest stated opening win; every notice URL is kept."""
    key = str(record["key"])
    prior = kept.get(key)
    if prior is None:
        kept[key] = record
        return
    newer, older = (
        (record, prior)
        if str(record.get("published") or "") >= str(prior.get("published") or "")
        else (prior, record)
    )
    merged = dict(newer)
    merged["notices"] = sorted(set(prior.get("notices") or []) | set(record.get("notices") or []))
    dues = [r for r in (prior, record) if r.get("due_at")]
    if dues:
        best = max(dues, key=lambda r: str(r["due_at"]))
        merged["due_at"], merged["due_text"] = best["due_at"], best["due_text"]
    opens = [r for r in (prior, record) if r.get("open_at")]
    if opens:
        first = min(opens, key=lambda r: str(r["open_at"]))
        merged["open_at"], merged["open_text"] = first["open_at"], first["open_text"]
    if merged.get("capacity_sought_mw") is None and older.get("capacity_sought_mw") is not None:
        merged["capacity_sought_mw"] = older["capacity_sought_mw"]
        merged["mw_stated"] = older.get("mw_stated")
    kept[key] = merged


# =============================================================================== shared
#: Every row's keys, in order: the parser's source columns (`DQ schema drift`) are the same set for
#: every record of every issuer.
RECORD_KEYS = (
    "issuer_id",
    "extractor",
    "key",
    "url",
    "listing_url",
    "title",
    "title_line",
    "section",
    "status_raw",
    "description",
    "published",
    "due_text",
    "due_at",
    "open_text",
    "open_at",
    "mw_stated",
    "capacity_sought_mw",
    "technologies",
    "contract_types",
    "classification",
    "classification_rule",
    "detail_url",
    "document_links",
    "docket",
    "notices",
)


def _blank_record(row: IssuerRow, key: str, url: str, title: str) -> dict[str, Any]:
    record: dict[str, Any] = dict.fromkeys(RECORD_KEYS)
    record.update(
        issuer_id=row.id,
        extractor=row.extractor,
        key=key,
        url=url,
        listing_url=row.listing_url,
        title=title,
        mw_stated=[],
        technologies=[],
        contract_types=[],
        document_links=[],
        notices=[],
    )
    return record


def _facts(record: dict[str, Any], title: str, description: str, detail_text: str) -> None:
    """MW, technologies and contract types the item states (classify.py: never guessed). The
    listing's own description is read first; the per-RFP page only when it states no MW."""
    figures = clf.stated_mw(description)
    source = description
    if not figures and detail_text:
        figures = clf.stated_mw(detail_text)
        source = detail_text
    record["mw_stated"] = figures
    record["capacity_sought_mw"] = clf.capacity_sought_mw(source) if figures else None
    record["technologies"] = clf.technologies(title, description)
    record["contract_types"] = clf.contract_types(f"{title} {description}")


def records(
    bundle: dict[str, Any], row: IssuerRow, retrieved: dt.date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(published items, left-out items) from one stored bundle, as of its retrieval date; pure."""
    if row.extractor == "aep_energy_rfps":
        return aep_records(bundle, row)
    if row.extractor == "news_release_index":
        return news_records(bundle, row)
    if row.extractor == "notice_table_pdf":
        return notice_records(bundle, row, retrieved)
    raise ParseError(f"{row.id}: unknown extractor {row.extractor!r}")


# =============================================================================== terms drift
_QUOTE_FOLD = str.maketrans(
    {
        "\u2019": "'",
        "\u2018": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
        "\u00ad": "",
    }
)


def fold(text: str) -> str:
    """A comparison form that survives markup and typography: quotes and dashes unified, case
    folded, every whitespace removed."""
    return "".join(htmltree.fix_mojibake(text).translate(_QUOTE_FOLD).casefold().split())


def terms_check(fx: Fetcher, now: dt.datetime) -> dict[str, Any]:
    """Re-read the row's terms page and confirm every quoted operative clause is still there word
    for word (docs/28 §8.2 "terms-drift check"; the registry's build script ran the same test).
    Raises `BlockedError` when a clause is gone or the page cannot be read: the issuer pauses until
    a reviewer re-reads the terms and updates the registry."""
    row = fx.row
    if not row.terms_url or not row.terms_quotes:
        raise BlockedError(f"{row.id}: no terms_url/terms_quote in the registry; cannot verify the terms")
    if not fx.allowed(row.terms_url):
        raise BlockedError(f"{row.id}: robots.txt disallows the terms page {row.terms_url}")
    status, body, headers = fx.get(row.terms_url, conditional=False)
    if status != 200:
        raise BlockedError(f"{row.id}: terms page {row.terms_url} answered HTTP {status}")
    text = htmltree.parse(decode(body, headers.get("content-type", ""))).text()
    page = fold(text)
    missing = [q for q in row.terms_quotes if fold(q) not in page]
    result = {
        "terms_url": row.terms_url,
        "checked_at": now.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "quotes": len(row.terms_quotes),
        "missing": missing,
        "text_sha256": hashlib.sha256(page.encode("utf-8")).hexdigest(),
    }
    if missing:
        raise BlockedError(
            f"{row.id}: {len(missing)} of {len(row.terms_quotes)} quoted terms clauses are no longer on "
            f"{row.terms_url}; paused until a reviewer re-reads the terms (docs/28 §8.2): "
            f"{missing[0][:120]!r}"
        )
    return result
