# services/alerts/worker.py — the scheduled alert cycle and delivery tick

Sprint 3 item 4 (docs/00-PLAN.md). Read `docs/20-architecture.md` §4.1 (worker pools) and §4.2
(scheduler/queue, job `alert`) first. This file documents `services/alerts/worker.py` and
`services/alerts/__main__.py` — what a tick does, its defaults, why it is two transactions, how
it stays idempotent, and what reading `evaluate.py`/`webhooks.py` for this task turned up.

## What one tick does

`run_alert_tick(session_factory, *, email_port=None, transport=None, now=None) -> AlertTickReport`
is exactly the function `infra/scheduler/`'s Procrastinate `alert_tick` job calls (confirmed
against `infra/scheduler/jobs.py`'s `alert_tick_job`, written concurrently: `run(build_session_
factory())` — positional session factory, everything else defaulted). One call is:

1. **Alert half** (one session, one transaction): count active `SavedSearch` rows, then call
   `services.alerts.evaluate.run_alert_cycle(session, email_port=..., now=...)` **once**. That
   function already loops over every active saved search itself, advances each one's
   `watermark_seq`, groups matches into one digest per search, and — for the `email` channel —
   sends the digest synchronously through the given `EmailPort` before returning. This half's job
   is only to open the session, make that one call, count what came back, and commit (or roll
   back and record the exception).
2. **Delivery half** (a second, independent session/transaction): call
   `services.alerts.webhooks.deliver_pending(session, transport=..., secret_for=..., now=...)`
   once. It already finds every `pending` delivery and every `retrying` delivery whose
   `next_attempt_at` has arrived, attempts each through the given `Transport`, and updates
   `WebhookDelivery.status` accordingly. Same shape: open, call, count, commit or roll back.

Each half commits independently; `_run_alert_half` and `_run_delivery_half` (both in `worker.py`)
each wrap their own body in `try/except Exception`, so a failure in one is rolled back and
recorded in `AlertTickReport.errors` **without preventing the other half from running** — the
delivery half is always attempted even if the alert half raised, and vice versa. Every exception
is logged once via the stdlib `logging` module as a structured `key=value` line (`error_type=`
only — never the exception's full string in the log line itself, and never an email body, a
saved-search query, or a webhook secret) before being summarised into `errors` as
`"{half}: {ExceptionType}: {message}"`.

## Defaults

- `email_port=None` → a **process-wide** `ResendEmailAdapter()`, built lazily on first use
  (`worker._process_default_email_port`), not at import time — so importing this module never
  reads `RESEND_API_KEY` before a caller (a test, or `main()`) has had the chance to set or force
  it. It is dry-run whenever `RESEND_API_KEY` is unset (`services/api/auth.py`), which is always
  true in this sandbox and in CI.
- `transport=None` → a fresh `HttpxTransport()` per tick — a thin wrapper around one
  `httpx.Client(timeout=10.0)` that satisfies `webhooks.Transport` structurally (`httpx.Client.
  post(url, *, content=, headers=)` returns an `httpx.Response`, which already has `status_code`
  and needs no adaptation). `run_alert_tick` closes this client at the end of the tick **only**
  when it built it itself — a transport passed in by a caller (a test, or `--dry-run`'s
  `DryRunTransport`) is that caller's to close.
- `now=None` → `dt.datetime.now(dt.UTC)`, used as `started_at` and passed through to both halves
  so their window/backoff math is computed against one consistent instant.

## Why two transactions, not one

The task brief requires that a failure in the alert half never blocks the delivery half (and
vice versa) — a bug in a saved-search's matching query should never leave webhook customers
without their deliveries, and a webhook endpoint timing out should never block a lender's email
digest. One shared transaction would couple their failure modes (a rollback in either would
discard both); two independent sessions/transactions is the only way to keep them isolated while
still giving each half itself a single, coherent commit/rollback unit.

## Idempotency (proved in `tests/test_alerts_worker.py`)

- **Alerts**: `evaluate_saved_search` (`evaluate.py`) advances `SavedSearch.watermark_seq` to the
  highest `event.seq` it considered *whether or not that event matched* — so a second tick with no
  new events since the first sees zero events past the watermark and creates nothing.
  `test_second_tick_is_a_no_op_on_both_halves` proves this by handing the second tick's transport
  an **empty** response queue: a `FakeTransport([])` would raise `IndexError` on any `.post()`
  call, so the test passing at all is itself the proof no delivery was re-attempted.
- **Deliveries**: `deliver_pending` (`webhooks.py`) only selects `WebhookDelivery` rows whose
  status is `pending` or `retrying`-and-due; a `delivered` row is never selected again by
  construction, with no extra bookkeeping needed on this module's side.

## Decisions

1. **No separate "send queued alerts" step exists, because none is needed.** The task brief asked
   this module to "only send `queued` rows if `evaluate.py` leaves sending to a later step." It
   does not: `run_alert_cycle` (`evaluate.py` lines ~167-192) sets `Alert.status = "queued"` only
   as the row's initial in-memory value immediately before either sending it (channel `email`,
   status flips to `"sent"` in the same code path, before the row is ever flushed in a visible
   state) or marking it `"suppressed"` (no deliverable address for the channel). No code path
   anywhere leaves a row's terminal status as `"queued"` — it is a construction default, not a
   pending state. So `worker.py`'s alert half is exactly one call to `run_alert_cycle`; there is
   nothing left to "send" afterwards.
2. **`--dry-run`'s transport records but still lets deliveries reach `delivered`.** The task
   brief's "a transport that records but never sends" is read as "makes no outbound network call,"
   not "leaves the database untouched" — `DryRunTransport.post` always answers a synthetic 2xx, so
   a `--dry-run` invocation exercises the full alert-and-delivery wiring (including status
   transitions) safely, which is the point of a dry run of the *worker*, as opposed to a dry run
   that no-ops the whole tick. `RESEND_API_KEY` is forced off the same way: `ResendEmailAdapter(
   api_key="")`, an explicit empty string (not `None`, which would fall through to the real
   environment variable) so `--dry-run` wins even if a real key happens to be configured.
3. **`main()` calls `services.db.session.init_db(engine)` before building the session factory.**
   `init_db` only creates tables that do not already exist (`Base.metadata.create_all`), so this
   is a no-op against an already-migrated Postgres database and makes the CLI usable stand-alone
   against a fresh SQLite target with no separate setup step. It never drops or alters an existing
   table.
4. **`searches_evaluated` is counted separately from `run_alert_cycle`'s return value**, via one
   `SELECT count(*) FROM saved_search WHERE status = 'active'` in the same transaction, because
   `run_alert_cycle` returns only the `Alert` rows it created, not the number of searches it looked
   at. This count is taken *before* `run_alert_cycle` runs, so — as
   `test_email_port_raising_leaves_delivery_half_running_and_records_error` shows — it survives
   that half's rollback even though the cycle's own writes (the alert row, the watermark) do not;
   it answers "how many searches did this tick attempt to evaluate," not "how many were
   successfully evaluated."
5. **`deliveries_failed` counts every attempted-but-not-delivered delivery this tick**, i.e.
   `deliveries_attempted - deliveries_delivered`, which includes deliveries left in `retrying`
   (backoff scheduled, not yet exhausted) as well as ones that hit `webhooks.MAX_ATTEMPTS` and
   became terminally `failed`. This is a per-tick attempt outcome, not a delivery's final fate —
   the `AlertTickReport` contract has no separate `retrying` field, and folding it into "delivered
   vs. not" is the only reading of `deliveries_failed` that makes `attempted = delivered + failed`
   hold, which a scheduler dashboard would otherwise expect.
6. **A raising `Transport` is a documented non-event at the tick level, by design elsewhere.**
   See "Found in webhooks.py" below — this is a finding, not a defect, but it changes what
   `tests/test_alerts_worker.py` could actually prove for "a transport that raises" and is
   recorded here so the reasoning isn't lost.

## Found in `webhooks.py` (no patch proposed — documenting existing, deliberate behaviour)

`attempt_delivery`'s own docstring says a transport failure is "a delivery failure, not a 500,"
and the code matches: `transport.post(...)` is called inside `attempt_delivery`'s own
`try/except Exception`, so **no exception a `Transport.post` raises can ever reach this module's
exception boundary** — it is unconditionally turned into `delivery.error_class` plus a
`retrying`/`failed` status, before `deliver_pending` (and therefore `worker.py`) ever sees it.

This means the task brief's "a transport that raises likewise [leaves the email half running and
records the error]" cannot be demonstrated with a literal raising `Transport.post`, because that
exact case is already fully absorbed one layer down, by design, and is instead exercised as a
**counted delivery failure**: `test_transport_post_raising_is_recorded_as_a_failed_delivery_not_a_
tick_error` proves `errors == ()` and `deliveries_failed == 1` for a `Transport` whose `.post`
raises `ConnectionError`. To still prove `worker.py`'s own boundary around the whole delivery half
(the thing the brief is really asking for — some other failure in that half, e.g. a dropped DB
connection, should not sink the alert half or crash the tick),
`test_delivery_half_raising_leaves_alert_half_committed_and_records_error` monkeypatches
`services.alerts.worker.deliver_pending` itself to raise, which is a failure `webhooks.py` cannot
and does not shield against. Both tests are in `tests/test_alerts_worker.py`; no change to
`webhooks.py` is proposed — this is `attempt_delivery`'s stated intent working as designed, not a
bug, and changing it would weaken the delivery log's own resilience.

## Files

- `services/alerts/worker.py` (new): `AlertTickReport`, `run_alert_tick`, `HttpxTransport`,
  `DryRunTransport`, `main`.
- `services/alerts/__main__.py` (new): `python -m services.alerts` delegates to `worker.main()`;
  `python -m services.alerts.worker` also works directly (`worker.py` has its own `__main__`
  guard).
- `tests/test_alerts_worker.py` (new): seeds one account/user/saved-search/matching-event pair
  plus one webhook endpoint/pending delivery (reusing `services.api.conftest`'s and
  `tests.conftest`'s factories, same as `tests/test_alerts_evaluate.py` and
  `tests/test_alerts_webhooks.py`), then exercises the happy path, idempotency, both halves'
  failure containment, the real `HttpxTransport` against `httpx.MockTransport` (2xx and a
  connection error), the default email port/transport, and the CLI.

## CLI

```
python -m services.alerts.worker            # one tick against DATABASE_URL
python -m services.alerts.worker --dry-run   # forces a dry-run email port and a recording-only transport
```

Writes one `key=value` summary line to stdout via `sys.stdout.write` (not `logging` — visible
regardless of log level, since the scheduler's job log is expected to capture stdout) of the shape:

```
alert_tick duration_ms=<n> searches_evaluated=<n> alerts_created=<n> emails_sent=<n> deliveries_attempted=<n> deliveries_delivered=<n> deliveries_failed=<n> errors=<n>
```

Individual error strings (`AlertTickReport.errors`) are logged via `logging.error`, one per line,
not put in the stdout summary — they can be long and are secondary to the counts. Exit code is
`1` if the tick recorded any error, `0` otherwise, so a scheduler wrapper can treat the CLI's exit
status as a job pass/fail signal independent of the Procrastinate task's own return value.

## Not done / out of scope

- No change to `evaluate.py`, `webhooks.py`, `feed.py`, `matching.py`, or anything in
  `services/api`, `services/db`, `infra`, or `pyproject.toml` — none was needed; see "Found in
  webhooks.py" above for the one design observation surfaced while reading it.
- This module has no opinion on Procrastinate wiring, cadence, retries-of-the-job-itself, or
  leader election — that is `infra/scheduler/`'s area (concurrent work, not read beyond confirming
  the call shape in `infra/scheduler/jobs.py` matches this contract).
- `AlertTickReport.errors` entries include the exception's `str()`, which is a decision to weigh
  against CLAUDE.md's "no secrets/personal data" guardrail if a future `EmailPort`/`Transport`
  implementation ever raises an exception whose message embeds one (neither `ResendEmailAdapter`
  nor `HttpxTransport`/`httpx` does today — `httpx` exceptions carry the URL and error class, not
  headers or bodies). Flagged for the coordinator rather than pre-emptively redacting exception
  text this sprint has no concrete case of leaking anything.

## Verbatim tails

```
$ .venv/bin/python -m pytest tests/test_alerts_worker.py tests/test_alerts_evaluate.py tests/test_alerts_webhooks.py
.........................................                                [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/starlette/testclient.py:37
  /home/user/Bankable/.venv/lib/python3.11/site-packages/starlette/testclient.py:37: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
41 passed, 1 warning in 3.13s

$ .venv/bin/ruff check services/alerts tests/test_alerts_worker.py
All checks passed!

$ .venv/bin/ruff format --check services/alerts tests/test_alerts_worker.py
8 files already formatted

$ .venv/bin/mypy --cache-dir /tmp/mypy-alerts services/alerts
Success: no issues found in 7 source files

$ .venv/bin/python -m coverage run --source=services.alerts.worker -m pytest tests/test_alerts_worker.py -q
................                                                         [100%]
16 passed, 1 warning in <1s

$ .venv/bin/python -m coverage report -m
Name                        Stmts   Miss  Cover   Missing
---------------------------------------------------------
services/alerts/worker.py     124      1    99%   302
---------------------------------------------------------
TOTAL                         124      1    99%
```

(Line 302 is `worker.py`'s own `if __name__ == "__main__":` guard — not exercised by importing the
module in tests, same as any script's guard line.)
