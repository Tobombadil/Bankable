# Run the latest locally, and know that you are

Written 2026-09-20 for the owner, after "how do I know I am seeing the latest version?".

The honest answer before today was that you could not tell from the page. Now every page footer
prints the commit that served it and the date the sources were last fetched, so the last step here
is a check you can trust rather than a hope.

Two things have to be current, and they move independently:

| | What makes it current | How you check it |
|---|---|---|
| **Code** | `git` | the commit in the footer matches `git log` |
| **Data** | running the connectors, then reloading | the fetch date in the footer moves |

A perfectly current build can be serving week-old rows, because the local runner loads a snapshot
once and then serves it until you reload.

## 1. Stop whatever is running

In the terminal window running the site, press `Ctrl-C`. If you are not sure one is running:

```bash
lsof -ti:8000,8001 | xargs kill 2>/dev/null; true
```

## 2. Get the latest code

**The branch history was rewritten on 2026-09-19** (owner decision: no model identifiers in commit
trailers). A plain `git pull` on the old branch will not work cleanly. Reset to what is on the
server instead. Nothing of yours is lost — everything was pushed.

```bash
cd ~/path/to/Bankable          # wherever your clone lives
git fetch origin
git status --short             # expect empty; if not, see the note below
git checkout main
git reset --hard origin/main
```

If `git status` listed files you changed yourself, keep them first with
`git stash push -m "my local edits"`, then `git stash pop` after the reset.

## 3. Install dependencies

New ones have been added since you last ran this (error tracking, among others). This is quick and
safe to repeat.

```bash
.venv/bin/pip install -r requirements.txt
```

If the virtual environment does not exist yet: `python3 -m venv .venv` first.

## 4. Fetch the data

`data/normalized/` is deliberately not in git, so a fresh clone has no data at all. If you have run
this before, your existing files are still there and you only need the layers you want to refresh.

**Proposals and opportunities** — the core records, a few minutes:

```bash
.venv/bin/python -m pipeline.connectors run --all
```

**Existing plants** — the original context layer:

```bash
.venv/bin/python -m pipeline.context.eia_plants --fetch
```

**Gas infrastructure** — pipelines, processing plants, storage, LNG terminals:

```bash
.venv/bin/python -m pipeline.context.eia_atlas --layer all
```

**Ethanol and renewable natural gas** — four small files:

```bash
.venv/bin/python -m pipeline.context.ethanol_plants --fetch
.venv/bin/python -m pipeline.context.ethanol_capacity --fetch
.venv/bin/python -m pipeline.context.lmop --fetch
.venv/bin/python -m pipeline.context.agstar --fetch
```

**Who owns which plant** — a 23 MB download:

```bash
.venv/bin/python -m pipeline.context.eia_owners
```

**Asset features** — pipeline mileage and incidents, plant capacity factors, fuel pathways. The
first of these pulls about 110 MB and takes a few minutes:

```bash
.venv/bin/python -m pipeline.context.phmsa
.venv/bin/python -m pipeline.context.eia923
.venv/bin/python -m pipeline.context.rfs
```

**Company parents** — optional, and the one heavy download: the global identifier registry publishes
a 24 MB relationship file plus a 500 MB entity file, and the connector makes one streaming pass over
the second. Skip it unless you want parent companies on company pages; everything else works without
it.

```bash
.venv/bin/python -m pipeline.context.gleif
```

## 5. Start the site

```bash
.venv/bin/python -m web.dev_up --preview
```

This rebuilds the local database from scratch every time (it reloads every row anyway, and
keeping the old file only preserved an out-of-date schema), loads everything it finds, and starts
both processes. The site is
at <http://127.0.0.1:8000> and the API at <http://127.0.0.1:8001>. Watch the log: it names every
layer it loads and every one it skips because the file is absent, so you can see exactly what made
it in. `--preview` bypasses the publish delay so today's rows are visible.

Leave this window running. `Ctrl-C` stops both.

## 6. Check that you are seeing it

Scroll to the bottom of any page. The footer now reads something like:

> Build `38930b4cef3a` · sources last fetched 2026-09-20.

Compare it with your checkout:

```bash
git log --oneline -1
```

The first twelve characters should match. If the footer adds **(modified working tree)**, that is
expected when you are running from a checkout you have edited, and it is telling the truth.

The same information is on the API, if you would rather check it that way:

```bash
curl -s http://127.0.0.1:8001/v1/health | python3 -m json.tool | head -30
```

## If something looks wrong

- **The footer commit is older than `git log`.** The site is still running from before your reset.
  Stop it and start it again.
- **The fetch date is old.** The code is current but the data is not. Re-run the connectors in
  step 4 for the layers you care about, then restart the site so it reloads them.
- **A layer is missing from the map.** Check the startup log for a line naming that file as absent,
  then run its connector from step 4.
- **`no such column: ...` from a page that used to work.** Your local database predates a schema
  change. Step 5 now rebuilds it for you and logs `rebuilding ... from scratch`, so this should not
  happen any more; it was real until 2026-09-20 and is what this section was missing. If you used
  `--skip-load`, the runner stops and names the missing columns instead — drop the flag and let it
  reload.
- **Anything else.** Copy the terminal output and send it; the error text is usually specific.
