"""Ember's electricity data: operating capacity, generation and shares for World, regions and countries
(`global.ember.electricity`; docs/26-global-figures.md; owner, 2026-10-23: "If we have numbers that are
credible for Global numbers then let's lead with those").

These are **operating** figures (installed capacity, electricity generated), never a pipeline of
proposals: nothing here becomes a `proposal`, an `event` or a change on the public feed. Ember revises
its series often; a changed value is a revision, recorded in the run's diff counts, not news.

Two routes, both Ember's own (docs/26 §2):

* **The API** (`api.ember-energy.org/v1`, documented at https://api.ember-energy.org/v1/docs): yearly
  and monthly generation for every area, and monthly wind and solar capacity. It needs a free key
  (`EMBER_API_KEY`), which the owner requests at https://ember-energy.org/data/api/; without one a run
  ends `blocked` and the figures already loaded stay as they are.
* **A person's download** of Ember's yearly CSV, placed in `<data root>/inbox/global.ember.electricity/`.
  The CSV is the only file with yearly capacity by source for every area, and the host it is served
  from, files.ember-energy.org, answers robots.txt with `Disallow: /` (only PDFs allowed; read
  2026-10-23), so no connector or probe ever fetches it.

`ember` holds the parsers, `areas` the area keys, `runner` one run end to end, `dq` the checks.
`python -m pipeline.connectors.global_ember_electricity run` is what the scheduler calls.
"""

from __future__ import annotations

SOURCE_ID = "global.ember.electricity"
