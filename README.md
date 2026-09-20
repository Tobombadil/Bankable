# Bankable

Deal intelligence for energy and infrastructure: a continuously updated graph of active project **proposals**
and funding / procurement **opportunities**, published with delayed-free and live-paid tiers, syndicated to
social channels. The existing Bankable deal workflow at bankablehq.com is a later consideration, not a foundation.

## Run the prototype locally (about fifteen minutes, no accounts needed)

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pipeline.connectors run --all        # proposals and opportunities, a few minutes
.venv/bin/python -m pipeline.context.eia_plants --fetch  # existing power plants
.venv/bin/python -m pipeline.context.eia_atlas --layer all   # gas pipelines, processing, storage, LNG
.venv/bin/python -m web.dev_up --preview                 # site on http://127.0.0.1:8000, API on :8001
.venv/bin/python -m services.api.bootstrap owner --email you@example.com --db web/.data/dev.db
```

The context layers are separate connectors because they are registries, not queues: each writes a
parquet under the git-ignored `data/normalized/context/`, and `web.dev_up` loads whatever it finds
and names whatever is missing. Ethanol and RNG (`ethanol_plants`, `ethanol_capacity`, `lmop`,
`agstar`), ownership (`eia_owners`), asset features (`phmsa`, `eia923`, `rfs`) and company parents
(`gleif`) follow the same pattern. **`docs/61-run-the-latest-locally.md` is the step-by-step
version**, including how to tell from the page itself which commit and which data vintage you are
looking at.

Then sign in at `/login` with that email and open `/admin`. `--preview` bypasses the public delay so today's rows
show; without it the public pages show what the delayed tier would show. Every vendor adapter (Attio, Stripe,
Resend) runs in a dry-run mode until its key is set, so nothing here needs an account. `docs/40-launch-runbook.md`
is the path from this to a deployment.

Start with `docs/00-PLAN.md` (roadmap and decisions), then `docs/01-feasibility.md` and
`docs/02-data-sources.md`. The source registry lives in `data/sources.yaml`; re-verify it with:

```
python -m venv .venv && .venv/bin/pip install pyyaml requests gridstatus
.venv/bin/python scripts/probe_sources.py --gridstatus
```
