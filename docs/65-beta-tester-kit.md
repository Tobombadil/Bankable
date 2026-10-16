# Private beta tester kit

Written 2026-10-11 for the private beta (`docs/64`). It was planned to open on 14 October 2026; the date moved because nothing was deployed yet, and the new date is set once the site is live. 8–12 people on deal and
portfolio teams at infrastructure investors, sharing one login, for two weeks. Four parts, each meant to be about a
page:

- **(a)** the tester guide;
- **(b)** a draft invitation email, for the owner to send personally;
- **(c)** a 20-minute debrief script with a notes template;
- **(d)** what the owner looks at each week.

**Why this exists.** `docs/51` §0 and §4 found that learning, not code, is the constraint: the beta is measured by use
and by what testers say, not by revenue (owner, 2026-10-10, `docs/00-PLAN.md`). Testers share one login, so every
usage number is aggregate. What testers say is the main signal. Until now the only channel was "Report a problem" on
a record, which catches a wrong record but not "I couldn't find what I was looking for". The beta therefore adds a
link on every page, **Tell us what you were trying to do** (`/feedback`). It shows only while the shared login is on
(`SITE_ACCESS=basic`), and what it sends lands in the same admin queue as record reports (§(d)).

**Product name.** Inframation, at inframation.io (owner, 2026-10-16; `docs/12` §12). It is the working name until
counsel's trademark checks are answered, and the privacy page says so. The draft below calls it "the beta" so the
email survives a further rename.

---

## (a) Tester guide

**How to use this guide.** Try each task below on a real question from your own work, not on our examples. Each task
gives a starting page, what a good result looks like, and the limits we already know about. When the site does not
do what you need, that is the most useful thing you can tell us.

**Giving feedback, in order of usefulness:**
1. **Tell us what you were trying to do.** This link is at the top of every page. Say what you wanted, and what
   happened instead. We keep the page you came from, without any search terms. An email address is optional.
2. **Report a problem.** This is at the bottom of every proposal and opportunity page. Use it when a record is
   wrong: two projects merged into one, the wrong status, or the wrong sponsor.
3. **The two 20-minute calls.** We will book them with you.

**Known limits, stated once, true of every task:**
- **Queue coverage is about a quarter of the US active queue.** That is about 2,100 active queue entries, against
  the ~8,200 that LBNL counts (`docs/51` §2.7;
  https://emp.lbl.gov/publications/queued-2025-edition-characteristics). ERCOT, CAISO, NYISO and EIA's monthly
  planned-generator survey are in. **PJM, MISO, SPP and ISO-NE are not yet.** An empty result in those regions means
  "not covered", not "nothing there".
- **Records are matched across registers, and the accuracy of that matching is still being checked.** One project
  can appear in a queue, an EIA survey and a permit. We fuse them into one record. A 100-record hand check runs as
  the beta opens. Where a merge looks wrong, report it.
- **Email alerts depend on the email provider being set up.** Saving an alert needs your own site account with a
  confirmed email address. The shared beta login is not an account. Until email sending is switched on, the
  confirmation email does not arrive, so alerts cannot be saved yet. We will tell you when they work.
- **Every figure has a source, and you should check it there before relying on it.** Each record names its register,
  the date we retrieved it, and a link to the original.

### Task 1. Check a developer's or owner's pipeline, with a source for every claim

- **Start:** `/organizations`. Search for the company, then open its page.
- **What good looks like:**
  - the **Pipeline** block counts the company's proposals by status, technology and grid operator;
  - every count links to the list of exactly those records;
  - every record you open has a **Sources** section naming the register and the retrieval date, with a link to the
    original row;
  - projects no register lists any more are counted apart, as "No longer listed", not as active.
- **Look for:** a count you cannot reconcile with the company's own disclosures; a subsidiary missing from the group
  view; a project you know of that is not there (check the limits above first).
- **Limits:**
  - sponsor names are as each register prints them. For 24 of the most active developer and utility groups
    (among them NextEra, RWE, SSE, Entergy, Invenergy and Lightsource bp) the parent's page gathers the proposals
    of its subsidiaries, each linked from the parent's own filing, annual report, LEI record or site, and names the
    subsidiary that filed each one (`docs/22` §24);
  - outside those groups one company can still appear under two spellings, and inside them a project company
    whose name does not carry its owner's (a "Flat Hill Solar LLC") stays unlinked unless a filing names it. A
    group's count can therefore be short; it is never padded with a guess;
  - parent companies from the global LEI register are not loaded in the beta, so other group views rest on EIA and
    EPA ownership data (`docs/64` §7).

### Task 2. Find where large load can connect

Covers grid points, queue congestion, retiring plants and gas.
- **Start:**
  - `/interconnection-points`: grid connection points, sorted by the active MW queued at each, filterable by grid
    operator, state and kind;
  - the map (`/`), with its layers for retired and retiring plants and for existing assets (transmission lines,
    gas pipelines, processing, storage and LNG terminals).
- **What good looks like:** you can shortlist a handful of points or areas that combine:
  - a connection point whose queue is not already crowded;
  - transmission lines nearby;
  - a retiring plant, whose connection may be reused;
  - gas nearby.
  Each fact links to its source.
- **Look for:** a point you know is congested that reads as quiet, which is usually a coverage gap. Also look for
  layers you expected and did not find.
- **Limits:**
  - congestion is only as complete as queue coverage (above);
  - connection points are the names the queues themselves give. There is no substation map: the federal
    substation layer is no longer public, and the copies we found have unclear terms (`data/sources.yaml`);
  - a retiring plant is a possibility, never a claim that its connection is available;
  - fiber routes are not shown.

### Task 3. Set an alert on a target's or competitor's projects

- **Start:**
  - a company page, then **Follow this company's proposals**; or
  - any filtered list or map view, then **Save this search as an alert**.
- **What good looks like:** a daily or weekly email listing new filings and status changes for exactly that company
  or view, each linking to the record. Your alerts are listed, and can be paused, at `/alerts`.
- **Limits:**
  - this task depends on email (above);
  - the free plan holds up to 10 alerts, with daily or weekly digests only;
  - a project leaving a queue register is announced as "No longer in <register>'s report (reason not stated)". It is
    never called a withdrawal unless the register says so.

### Task 4. See who owns which assets across asset classes

- **Start:** a company page's **Assets** section, which has a map and one table per role and asset type. Or an asset
  page (`/assets/...`), under **Owners and operators**.
- **What good looks like:**
  - power plants with ownership shares;
  - gas pipelines and processing plants with their operators;
  - LNG, ethanol and renewable gas facilities;
  - all under one company, each row with its register.
- **Look for:** an owner you know is wrong or out of date; asset classes you expected and did not find.
- **Limits:**
  - ownership is as of each register's latest release; EIA's annual plant survey can be more than a year old;
  - where a register names a private person as owner, no share is shown.

---

## (b) Invitation email: DRAFT for the owner to send personally

> **DRAFT. For the owner to send personally, from their own address, to people they know.** No agent or automated
> channel sends it (`CLAUDE.md`: agents draft, a human sends). Replace the bracketed parts. Send the login
> separately: by phone or text, or in a second email.

**Subject:** Would you try an early tool for tracking energy and infrastructure projects?

Hi [first name],

I'm building a tool that tracks energy and infrastructure projects from public registers: grid interconnection
queues, EIA's plant surveys, permits and procurement notices. It joins them into one record per project, with the
source for every figure. I'd value your eye on it, because it's meant for people doing exactly what your team does.

**What it is:** a private beta for 8–12 people at infrastructure investors, from [start date] for two weeks.
I'd like to see whether it helps with four things:
- checking a developer's or owner's pipeline;
- finding where large load can connect;
- following a target's or competitor's projects;
- seeing who owns which assets.

**What it isn't:**
- It isn't complete. It covers about a quarter of the US interconnection queue, with no PJM, MISO, SPP or ISO-NE yet.
- It isn't investment advice, and it isn't for sale. There's no pricing in the beta.
- Its matching of records across registers is still being checked, and email alerts may not be switched on when you
  start.

I'd rather you found its gaps than that I described them.

**What I'm asking:** two 20-minute calls, one early and one at the end, plus whatever use fits your real work over the
two weeks. When it doesn't help, the "Tell us what you were trying to do" link at the top of every page takes a
minute and goes straight to me.

**Logging in:** everyone in the beta shares one login, which I'll send you separately. Because it's shared, I can't
see who did what, only totals.

**Data and credit:**
- Everything comes from public registers, each under its own terms. Some allow only noncommercial reuse, and most
  ask for credit.
- Every record names its source and links to the original. The site's Reuse conditions page summarises each
  source's terms.
- During the beta, please use it to judge the tool. Don't export or republish the data, and check any figure at its
  source before you rely on it.

**Privacy:** the feedback form keeps what you type and the page you sent it from; your email is optional. I'll take
notes on our calls and keep them myself. Tell me any time and I'll delete them.

**Saying no or stopping:** just reply "no thanks", or ignore this. If you join and want to stop, reply "stop" and I'll
take you off the list and delete my notes. No hard feelings either way.

Would you be up for it? If so, reply with two 20-minute slots in the week of [start date] that suit you.

Thanks,
[owner's name]

---

## (c) Debrief interview: 20 minutes

**Purpose.** Learn what testers tried, where they got stuck, and how they do these jobs today. Audience comes first
(owner, 2026-10-10), so money is asked about last and lightly, or not at all if it does not come up naturally.
Ask open questions, let silences run, and do not demo or defend the tool. Run the same script at the first and the
last call; at the last call, ask what changed.

| Minutes | Part | What to say or ask |
|---|---|---|
| 0–2 | Open | "Thanks. I'm testing the tool, not you; there are no right answers. May I take notes? I won't record unless you'd like me to." |
| 2–4 | Their work | "Tell me about the last time you had to size up a developer, a site or a portfolio. What did you do, and with what?" |
| 4–7 | Job 1: pipeline | "Did you look up a developer or owner here? Walk me through it." "What did you expect to see?" "Was there anything you wouldn't put in front of your investment committee? Why?" |
| 7–10 | Job 2: large load | "Did you look for somewhere load could connect? Where did you start?" "What made a place look promising or not?" "What was missing?" |
| 10–13 | Job 3: alerts | "Is there a company or area you'd want to hear about when it changes?" "How do you find out today?" "What would make an alert worth opening?" |
| 13–16 | Job 4: ownership | "Did you check who owns what? Was it right, as far as you know?" "What would you need to trust it?" |
| 16–18 | Weekly use | "If this disappeared tomorrow, what, if anything, would you miss?" "What would make you open it every week?" |
| 18–19 | Value (lightly) | Only if it fits: "Does your team pay for anything that does part of this today?" Do not quote prices. |
| 19–20 | Close | "Anything I should have asked?" "Is there anyone else I should talk to?" Remind them of the feedback link; thank them. |

**Follow-ups that work:** "What happened next?" "Why did that matter?" "Can you show me?" "What did you do instead?"
**Avoid:** "Would you use…?", "Don't you think…?", feature lists, and explaining a gap away.

**Notes template.** Keep one per call. Use a tester code (T01–T12), not a name; the owner keeps the code-to-name key
separately. Write their words in quotation marks and your reading outside them.

```
Tester: T__   Call: first / last   Date: 2026-10-__   Role (generic): deal / portfolio / other
Their current way (tools, people, time):

Job 1 pipeline   Tried? y/n   What they did:            Stuck at:            Trust needs:
Job 2 load       Tried? y/n   What they did:            Stuck at:            Trust needs:
Job 3 alerts     Tried? y/n   What they did:            Stuck at:            Trust needs:
Job 4 ownership  Tried? y/n   What they did:            Stuck at:            Trust needs:

Would miss if gone:
Weekly-use trigger:
Value signal (none / weak / strong), with quote:
Top 3 problems, in their words:
1.
2.
3.
Quotes worth keeping:
Follow-ups promised (owner sends; nothing automated):
Asked to delete notes? y/n
```

---

## (d) What the owner looks at each week

About 30 minutes, the same day each week, during the beta. Admin is on its own host, behind the shared beta login
and then the operator sign-in (`docs/64` §1). `{DOMAIN}` is the deployed domain, `inframation.io`.

1. **Engagement** (`https://admin.{DOMAIN}/admin/engagement?weeks=2`).
   - **Page views by type,** per ISO week: proposal, company, asset, grid point and site pages. Use them as a rough
     guide to which of the four jobs are being tried. Company pages map to jobs 1 and 4; grid points to job 2.
   - **Registrations** and **Alerts created** are the alert sign-ups, job 3.
   - Counts are page loads, not people. Known crawlers and HEAD requests are excluded. With one shared login and a
     dozen testers, a single enthusiastic tester can move every number.
2. **Feedback and reports** (`https://admin.{DOMAIN}/admin/tasks?type=report&status=open`).
   - Rows whose **Issue** reads **Beta feedback** came from "Tell us what you were trying to do". Their subject column
     shows the page the reader came from. The other rows are record problem reports.
   - Open each at `https://admin.{DOMAIN}/admin/tasks/<task id>`. Feedback shows "What they were trying to do",
     "What happened" and "Page they came from", plus an email only if the tester gave one.
   - Put a one-word theme in **Notes** (coverage, search, matching, ownership, alerts, speed, other). Then set the
     status to **done** with a reason.
   - If there is an email and a reply would help, write it yourself from your own address. Nothing is sent from the
     platform.
3. **Alert sign-ups** (`https://admin.{DOMAIN}/admin/users`).
   - New accounts, and on each account's page whether its email is confirmed.
   - While the email provider is not set up, no address can be confirmed, so "Alerts created" stays at zero. If testers
     want alerts, that is the signal to finish the email setup.
4. **If testers mention stale data:** `https://admin.{DOMAIN}/admin/sources` shows each source's last run and health.

**Write five lines** for the weekly note:
- what testers tried most;
- the top three themes from feedback and calls;
- one thing to fix now;
- one thing to stop doing;
- one open question for the next calls.

What testers say outranks the counts. A theme that two testers raise unprompted matters more than a doubling of page
views.

**Two things the numbers cannot show:**
- **Who did what.** The login is shared, so there are no per-tester figures.
- **Feedback blocked by the rate limit.** Feedback is limited to 5 messages an hour per connection, shared with
  record reports and project submissions. Testers at one office often share one connection. A tester who hits the
  limit sees a message saying so, but nothing reaches the queue.

---

## Assumptions

- **[A-65-1]** The coverage figure is `docs/51` §2.7's (2,118 rows, 524 GW, against LBNL's ~8,200 and ~2,060 GW),
  measured 2026-10-10. Restate it if a source is added before the beta opens.
- **[A-65-2]** Testers at one firm may share an outbound address, and so share the feedback rate limit (5 an hour,
  `services/api/admin_posts.py` `INTAKE_RATE_LIMIT`). That is expected to be enough for 8–12 testers. If a tester
  reports hitting it, the setting is the place to look.
- **[A-65-3]** Whether commercial firms may use a free tool built partly on noncommercial-licence data is open with
  counsel (`docs/00-PLAN.md`, 2026-10-10 Q4). The invitation therefore asks testers to judge the tool, not to reuse
  the data.
