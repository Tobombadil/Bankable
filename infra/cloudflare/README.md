# Cloudflare worker: bankablehq.com → Lovable proxy

`lovable-proxy-worker.js` is the pre-existing Cloudflare Worker that fronts bankablehq.com and rewrites
requests to the published Lovable app. It is unrelated to this platform (CLAUDE.md: the bankablehq deal
workflow is a later integration, not a foundation) and is kept here only so the account's one deployed
worker is versioned.

It used to live at `.github/workflows/blank.yml`. GitHub parsed that JavaScript file as an Actions workflow on
every push and recorded a failed run with zero jobs — 43 red runs on the Sprint 3 branch by 2026-09-15, none
of them from the real `ci.yml`, which triggers only on pushes to `main` and pull requests against `main`.
Moved 2026-09-15 so the red mark stops and the real CI result is the only one a reviewer sees.
