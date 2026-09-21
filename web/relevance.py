"""Default technology relevance for the company page's "Proposals near these assets" list.

The mapping itself is *not* here: it is `data/vendored/relevance/asset_technology_relevance.yaml`,
an owner-editable commercial judgement with its reasoning written next to each list (CLAUDE.md:
"record any assumption in the doc that depends on it"). This module loads that file, resolves a
company's asset types to a default technology set, and resolves one request's
`?nearby_technology=` parameter against it.

Three states a reader can be in, and the URL grammar that names them (a GET form, no JavaScript --
`web/test_e2e.py` runs with scripts off, docs/04 D-30):

  no parameter            the default filter, when the company has one; otherwise everything
  `nearby_technology=all` everything, explicitly -- the one-click escape from the default
  `nearby_technology=<t>` the technologies the reader picked, comma-separated

An unrecognised token degrades to "everything" rather than to a 400 or an empty list: a hand-typed
or stale URL must never make the register look emptier than it is, which is the whole risk this
feature carries.
"""

from __future__ import annotations

import functools
import pathlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEVANCE_YAML = REPO_ROOT / "data" / "vendored" / "relevance" / "asset_technology_relevance.yaml"

#: The query parameter the company page filters on. Named for the section it controls, not
#: `technology`, because the page also lists proposals *sponsored by* the company and a bare
#: `technology` would read as though it filtered both.
PARAM = "nearby_technology"
#: `?nearby_technology=all` -- an explicit "show me everything", distinct from "no parameter",
#: which means "whatever the default is". Not a technology token, and never one: the vocabulary
#: is pinned in `web/test_relevance.py`.
ALL = "all"


@dataclass(frozen=True)
class Group:
    """One rule in the YAML: asset types -> technologies, with the words the page says about it."""

    id: str
    asset_types: frozenset[str]
    technologies: tuple[str, ...]
    filter_label: str
    reason: str


@dataclass(frozen=True)
class Relevance:
    """The loaded mapping. `unfiltered` maps an asset type to why it has no rule."""

    groups: tuple[Group, ...]
    unfiltered: Mapping[str, str]

    @property
    def mapped_asset_types(self) -> frozenset[str]:
        return frozenset().union(*(g.asset_types for g in self.groups)) if self.groups else frozenset()

    @property
    def known_asset_types(self) -> frozenset[str]:
        return self.mapped_asset_types | frozenset(self.unfiltered)

    def default_for(self, asset_types: Iterable[str]) -> Default | None:
        """The default filter for a company holding `asset_types`, or `None` for "show everything".

        `None` whenever the company holds an asset type with no rule (including one nobody has
        classified yet) or holds nothing at all -- rule 2 in the YAML header. The failure mode is
        always a longer list, never a shorter one.
        """
        held = {t for t in asset_types if t}
        if not held:
            return None
        matched = [g for g in self.groups if held & g.asset_types]
        if not matched:
            return None
        if held - frozenset().union(*(g.asset_types for g in matched)):
            return None
        technologies: list[str] = []
        for group in matched:
            for tech in group.technologies:
                if tech not in technologies:
                    technologies.append(tech)
        if not technologies:
            return None
        # One group: say what it is ("gas-related technologies"). More than one: naming both reads
        # as a list of jargon, so the page says what the rule is instead and the reasons carry the
        # detail.
        label = (
            matched[0].filter_label
            if len(matched) == 1
            else "the technologies relevant to what this company holds"
        )
        return Default(
            technologies=tuple(technologies),
            label=label,
            reason=" and ".join(g.reason for g in matched),
            group_ids=tuple(g.id for g in matched),
        )


@dataclass(frozen=True)
class Default:
    technologies: tuple[str, ...]
    label: str
    reason: str
    group_ids: tuple[str, ...]


def _require_str(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{RELEVANCE_YAML.name}: {where} needs a non-empty {field}")
    return value.strip()


def parse_relevance(document: Any) -> Relevance:
    """Parse the YAML document. Raises on a malformed file rather than degrading quietly: a
    mis-edited mapping is an owner's typo to be shown, not a filter to be silently dropped."""
    if not isinstance(document, Mapping):
        raise ValueError(f"{RELEVANCE_YAML.name}: top level must be a mapping")
    groups: list[Group] = []
    for raw in document.get("groups") or []:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{RELEVANCE_YAML.name}: each group must be a mapping")
        group_id = _require_str(raw.get("id"), "id", "group")
        asset_types = [str(t) for t in (raw.get("asset_types") or [])]
        technologies = [str(t) for t in (raw.get("technologies") or [])]
        if not asset_types or not technologies:
            raise ValueError(f"{RELEVANCE_YAML.name}: group {group_id} needs asset_types and technologies")
        groups.append(
            Group(
                id=group_id,
                asset_types=frozenset(asset_types),
                technologies=tuple(technologies),
                filter_label=_require_str(raw.get("filter_label"), "filter_label", f"group {group_id}"),
                reason=_require_str(raw.get("reason"), "reason", f"group {group_id}"),
            )
        )
    unfiltered_raw = document.get("unfiltered") or {}
    if not isinstance(unfiltered_raw, Mapping):
        raise ValueError(f"{RELEVANCE_YAML.name}: `unfiltered` must be a mapping of asset type to reason")
    unfiltered = {str(k): _require_str(v, "reason", f"unfiltered.{k}") for k, v in unfiltered_raw.items()}
    grouped: frozenset[str] = frozenset().union(*(g.asset_types for g in groups)) if groups else frozenset()
    overlap = sorted(frozenset(unfiltered) & grouped)
    if overlap:
        raise ValueError(f"{RELEVANCE_YAML.name}: {', '.join(overlap)} is both grouped and unfiltered")
    return Relevance(groups=tuple(groups), unfiltered=unfiltered)


@functools.lru_cache(maxsize=1)
def load_relevance(path: pathlib.Path | None = None) -> Relevance:
    """The parsed mapping, cached for the process. Editing the YAML needs a restart, the same as
    every other vendored file this site reads."""
    return parse_relevance(yaml.safe_load((path or RELEVANCE_YAML).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class NearbyFilter:
    """What one request asked for, resolved against the company's default.

    `technologies` is `None` for "no filter". `mode` is what the page has to explain:
    `default` (the relevance rule is on), `manual` (the reader picked), `off` (the reader turned
    the default off), `none` (this company has no default, so there is nothing to explain).
    """

    mode: str
    technologies: tuple[str, ...] | None
    default: Default | None
    selected: str

    @property
    def default_available(self) -> bool:
        return self.default is not None


def resolve_nearby_filter(
    value: str | None, *, default: Default | None, vocabulary: Sequence[str] | None = None
) -> NearbyFilter:
    """Resolve `?nearby_technology=` for a company whose default is `default`.

    `vocabulary` is the API's technology vocabulary when the page has it; tokens outside it are
    dropped, because the API rejects an unknown token with a 400 and a 400 here would render an
    empty list under a banner claiming a count. With every token dropped the result is "show
    everything", never "show nothing".
    """
    raw = (value or "").strip()
    if not raw:
        if default is None:
            return NearbyFilter(mode="none", technologies=None, default=None, selected=ALL)
        return NearbyFilter(mode="default", technologies=default.technologies, default=default, selected="")
    if raw.lower() == ALL:
        return NearbyFilter(
            mode="off" if default is not None else "none", technologies=None, default=default, selected=ALL
        )
    allowed = frozenset(vocabulary) if vocabulary else None
    picked = [t.strip() for t in raw.split(",") if t.strip()]
    if allowed is not None:
        picked = [t for t in picked if t in allowed]
    if not picked:
        return NearbyFilter(
            mode="off" if default is not None else "none", technologies=None, default=default, selected=ALL
        )
    return NearbyFilter(mode="manual", technologies=tuple(picked), default=default, selected=",".join(picked))


def _tail(shown: int, listed_cap: int | None, listed_projects: int | None) -> str:
    """The second sentence: what the reader is actually looking at, when it is not simply the
    `shown` rows. Two things can shrink it, and both have to be said or the counts look wrong --
    `limit` caps the page, and `group_nearby_proposals` collapses a project a register lists once
    per generator unit into one row."""
    listed = min(shown, listed_cap) if listed_cap is not None else shown
    capped = listed_cap is not None and shown > listed_cap
    grouped = listed_projects is not None and listed_projects < listed
    if capped and grouped:
        return (
            f" The {listed} nearest are listed, as {listed_projects} projects: "
            "a register lists a project once per generator unit."
        )
    if capped:
        return f" The {listed} nearest are listed."
    if grouped:
        return f" Listed as {listed_projects} projects: a register lists a project once per generator unit."
    return ""


def _href(path: str, keep: Mapping[str, str] | None, **extra: str) -> str:
    """`path` with `keep` (the page's other state, e.g. `scope=children`) and `extra` composed as
    one query string. Added 2026-09-20 with the recursive ownership scope: the company page is now
    a different page at each level of the tree, and a filter link that dropped `scope` would walk
    the reader silently back up to the default level. With no `keep` the output is byte-identical
    to the `f"{path}?..."` this replaced."""
    params = {**(keep or {}), **extra}
    return f"{path}?{urlencode(params)}" if params else path


def nearby_notice(
    nearby_filter: NearbyFilter,
    *,
    shown: int | None,
    total: int | None,
    path: str,
    listed_cap: int | None,
    listed_projects: int | None = None,
    keep: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The sentence above the list, and the link that undoes or applies the filter.

    Stated in whole words with both numbers in it, because the failure this guards against is a
    reader seeing a short list and concluding the coverage is thin (owner brief, 2026-09-20). The
    text is plain (Jinja escapes it); only the link is markup, so the sentence stays testable.

    `keep` is the page state the links must carry through (the ownership `scope`); `path` stays a
    bare path.
    """
    if shown is None or total is None:
        return None
    plural = "" if total == 1 else "s"
    capped = _tail(shown, listed_cap, listed_projects)
    if nearby_filter.mode == "default" and nearby_filter.default is not None:
        return {
            "text": (
                f"Showing {shown} of {total} nearby proposal{plural}, narrowed to "
                f"{nearby_filter.default.label} because this company "
                f"{nearby_filter.default.reason}.{capped}"
            ),
            "link_text": f"Show all {total}",
            "link_href": _href(path, keep, **{PARAM: ALL}),
            "tone": "narrowed",
        }
    if nearby_filter.mode == "manual":
        picked = ", ".join(nearby_filter.technologies or ())
        return {
            "text": (
                f"Showing {shown} of {total} nearby proposal{plural}, narrowed to the "
                f"technolog{'y' if len(nearby_filter.technologies or ()) == 1 else 'ies'} "
                f"you chose: {picked}.{capped}"
            ),
            "link_text": f"Show all {total}",
            "link_href": _href(path, keep, **{PARAM: ALL}),
            "tone": "narrowed",
        }
    if nearby_filter.mode == "off" and nearby_filter.default is not None:
        return {
            "text": f"Showing all {total} nearby proposal{plural}, unfiltered.{capped}",
            "link_text": f"Narrow to {nearby_filter.default.label}",
            "link_href": _href(path, keep),
            "tone": "all",
        }
    return {
        "text": f"Showing all {total} nearby proposal{plural}.{capped}",
        "link_text": None,
        "link_href": None,
        "tone": "all",
    }
