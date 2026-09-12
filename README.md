# Bankable

Deal intelligence for energy and infrastructure: a continuously updated graph of active project **proposals**
and funding / procurement **opportunities**, published with delayed-free and live-paid tiers, syndicated to
social channels, and feeding the Bankable analyse → certify → route → fund workflow at bankablehq.com.

Start with `docs/00-PLAN.md` (roadmap and decisions), then `docs/01-feasibility.md` and
`docs/02-data-sources.md`. The source registry lives in `data/sources.yaml`; re-verify it with:

```
python -m venv .venv && .venv/bin/pip install pyyaml requests gridstatus
.venv/bin/python scripts/probe_sources.py --gridstatus
```
