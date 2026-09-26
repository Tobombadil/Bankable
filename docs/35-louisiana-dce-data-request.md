# 35 — Louisiana DCE data-sharing request: draft email for the owner to send

**Status:** draft only. Nothing here is sent, posted or submitted by an agent (`CLAUDE.md`: outbound
communication to real people is drafted by agents and sent by a human). The owner sends this from his own
account, edits it as he sees fit, and decides the two open items in the checklist before sending.
**Reads:** `docs/02-data-sources.md` §11.1 (Class VI table), §11.3 item 5, §11.4 row 13; `data/sources.yaml`
`us.la.dce.class_vi`; `docs/00-PLAN.md` decisions log, 2026-09-18 row (5) (legal entity and address) and
2026-09-25 rows (noncommercial posture).

## Why this address, confirmed today

The department was renamed twice: DNR → Department of Energy and Natural Resources (DENR) → **Department of
Conservation and Energy (DCE)** on **2025-10-01**. Confirmed live, 2026-09-26, via
`pipeline/connectors/http.py`'s `PoliteSession` (robots honoured, no retries): `www.dce.louisiana.gov/robots.txt`
answers 200 (permissive) and
<https://www.dce.louisiana.gov/page/class-vi-permits-and-applications> answers 200; its page `<title>` reads
"Class VI Permits and Applications | Louisiana Department of Conservation and Energy", and a site-wide banner
states verbatim: "As of Oct. 1, 2025, the Department of Energy and Natural Resources (DENR) has become the
Department of Conservation and Energy (DCE)."

**No named individual contact is public.** The page itself gives one functional mailbox for exactly this
purpose — status questions about Class VI applications — reached by the department's own text: "You can also
email <injection-mining@la.gov> to request an estimate of when we expect that particular application to be
posted." This reads as the Injection & Mining programme's public inbox, i.e. the "UIC / Class VI programme"
contact the brief asked for, not a general data-services desk (none is named on this page). **Address the
office**, not a person.

Why we are asking at all: SONRIS (the Oracle APEX system that actually serves Class VI application data) is
**robots-disallowed** — `Disallow: /`, confirmed 2026-09-22 (`docs/02` §11.1, `data/sources.yaml`
`us.la.dce.class_vi`) — so this is the largest primacy-state Class VI dataset with **no automated public
route at all** (`docs/02` §11.3 item 5, §11.4 row 13). Every other state's Class VI list is at least readable
by a script or a browser; Louisiana's is the one closed by robots.txt on principle, which is why it is the
one worth a direct ask rather than a browser workaround.

---

## Draft email

**To:** injection-mining@la.gov
**Subject:** Data-sharing request — Louisiana Class VI applications (public, noncommercial mapping project)

> Hello,
>
> My name is [OWNER NAME], and I run Infraque (infraque.com), a public, noncommercial platform that maps
> announced and permitted energy and infrastructure projects across the US, including Class VI
> carbon-storage wells, from public agency records.
>
> I'd like to include Louisiana's Class VI applications and injection-well data alongside what EPA and the
> other primacy states already publish. Your Class VI page links out to SONRIS, and SONRIS's own
> robots.txt disallows automated access to the whole site, so I have no way to keep this current without
> checking it by hand, application by application.
>
> If DCE has a data extract, a bulk file, or an API for Class VI applications, I would be glad to use that
> instead. If not, I'd like to ask permission for periodic automated retrieval of SONRIS's Class VI
> pages under limits your office sets — a fixed rate, off-peak hours, a declared user-agent, whatever you'd
> want to see.
>
> What I'd do with it: only derived facts (a project's name, location, status and filing date) would be
> published, each one attributed to DCE with a link back to the source page and application on SONRIS. I
> would never republish the underlying application documents or redistribute SONRIS's raw data myself.
>
> In return, I'm happy to credit DCE by name and link on every record, send you a copy of the resulting map
> layer, and route back anything a reader flags as wrong or out of date.
>
> Would a data extract, API access, or a stated retrieval allowance be possible? Happy to talk it through
> however's easiest for your office.
>
> Thank you,
> [SIGNATURE BLOCK]

(≈230 words.)

---

## Before you send

- [ ] **Verify the addressee.** `injection-mining@la.gov` is read live off the current Class VI page
      (2026-09-26); confirm it's still the right inbox and not a stale address, and consider a phone
      follow-up if DCE's Injection & Mining division lists a direct line elsewhere.
- [ ] **Add your signature block**: name, title, phone, and the postal address the platform's other
      outbound mail already carries — Compass International Trading Group LLC, 1029 E 8th Ave, Suite 806,
      Denver, CO 80216 (`docs/00-PLAN.md` decisions log, 2026-09-18 row (5)) — if you decide to name the LLC
      (next item).
- [ ] **Decide whether to name the LLC.** The draft above signs as "Infraque" in the first person; DCE may
      reasonably ask who legally operates it. Naming Compass International Trading Group LLC is the more
      transparent version and matches the address rule above; leaving it as a personal/product introduction
      is lighter-weight for a first ask. Either is consistent with `CLAUDE.md`'s no-impersonation rule as
      long as it's you sending it, signed as yourself.
