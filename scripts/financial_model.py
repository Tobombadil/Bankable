"""Reproduce every table in docs/14-financial-model.md.

    .venv/bin/python scripts/financial_model.py

Pure Python, no dependencies, deterministic. The doc pastes this script's output verbatim; if a
number in the doc disagrees with this script, the script is right and the doc is stale.

What is modelled (docs/14 §1 explains each choice and cites the source of every number):

* 24 months from 2026-10 (month 1). ``PLATFORM_POSTURE=noncommercial`` until ``switch_month``;
  those months carry zero subscription revenue and only costs. From ``switch_month`` the paid
  tiers are offered again at the docs/11 §3 prices.
* Three adoption scenarios (low / base / high) that differ only in gross adds per month and the
  API attach rate. Every other input is shared and comes from a cited row.
* Pro is billed monthly (cash = recognised revenue). Team and the API add-on are billed annually
  in advance, so their cash lands at signing and at each renewal while recognition is spread
  over twelve months. Annual churn is applied at renewal for Team/API and monthly for Pro.
* Costs: the docs/60 §4 infrastructure stack, model calls from the month the gateway ships,
  social/API budgets per docs/32, CRM seat from the switch month, Stripe fees on cash receipts.
  Unpriced items (PJM licence, counsel, an owner draw) are parameters that default to zero and
  appear in the sensitivity table so their absence is visible rather than silent.
* A tornado over every input with a cited range picks the three inputs that move the 24-month
  cash position most; grids for those three follow.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from typing import Any

MONTHS = 24
START_YEAR = 2026
START_MONTH = 10  # month 1 of the model is October 2026

# docs/60 §4 line items, USD per month, midpoints of the quoted ranges where a range is quoted.
INFRA_LINES: tuple[tuple[str, float, str], ...] = (
    ("Compute: 3 x Hetzner cx32 (app, worker, browser worker)", 3 * 7.69, "docs/60 §4, list price"),
    ("Managed Postgres (Neon, ~0.5 CU avg + PITR)", 45.0, "docs/60 §4, midpoint of 35–55"),
    ("Object storage ~200 GB (Cloudflare R2)", 4.5, "docs/60 §4, midpoint of 3–6"),
    ("Edge: DNS, CDN, WAF (Cloudflare)", 10.0, "docs/60 §4, midpoint of 0–20"),
    ("Transactional email (Resend)", 30.0, "docs/60 §4, midpoint of 20–40"),
    ("Error tracking, logs, metrics (Sentry + Grafana free tiers)", 15.0, "docs/60 §4, midpoint of 0–30"),
    ("Domain, certificates, secrets tooling", 5.0, "docs/60 §4"),
)
INFRA_CORE_BASE = round(sum(v for _, v, _ in INFRA_LINES), 2)


@dataclass(frozen=True)
class Params:
    """Every input. Field comments name the source; docs/14 §1 carries the full citation."""

    name: str = "base"
    switch_month: int = 7  # A-1: PLATFORM_POSTURE flips back to commercial in 2027-04
    modelgw_start_month: int = 4  # A-2: services/modelgw ships and starts billing model calls
    x_start_month: int = 1  # docs/32 §7 launch calendar: X enabled from day 0, capped
    # Monthly costs, USD.
    infra_core: float = INFRA_CORE_BASE  # docs/60 §4 core stack today (80–170)
    model_calls: float = 105.0  # docs/60 §4 / docs/20 §14: 60–150 once the gateway ships
    x_posting: float = 189.0  # docs/32 §4.7: measured ≈$189 at 30 link posts/day; cap $250
    linkedin_bridge: float = 30.0  # docs/32 §4.6: scheduler bridge, assume ≤$30/month
    crm_seat: float = 35.0  # Attio Plus, annual billing, 1 seat (API access needs a paid plan)
    owner_draw: float = 0.0  # unpriced: owner input (docs/00-PLAN open question 2)
    pjm_licence: float = 0.0  # unpriced: docs/20 §14 "price unknown; owner enquiry"
    counsel_one_off: float = 0.0  # unpriced: docs/13 §7 counsel items, charged at switch_month
    # Prices, USD (docs/11 §3; docs/41).
    pro_monthly: float = 149.0
    team_annual: float = 9000.0
    api_addon_annual: float = 5000.0
    # Payment fees (stripe.com/pricing, stripe.com/billing/pricing, read 2026-09-26).
    card_pct: float = 0.029
    card_fixed: float = 0.30
    billing_pct: float = 0.007
    # Adoption (the scenario axis) and retention (docs/11 §2.3).
    pro_adds_per_month: float = 5.0
    team_adds_per_month: float = 1.0 / 3.0
    api_attach: float = 0.5  # share of Team accounts that take the +$5k API add-on
    pro_annual_churn: float = 0.20
    team_annual_churn: float = 0.10


SCENARIOS: tuple[Params, ...] = (
    Params(name="low", pro_adds_per_month=2.0, team_adds_per_month=1.0 / 6.0, api_attach=0.0),
    Params(name="base"),
    Params(name="high", pro_adds_per_month=7.0, team_adds_per_month=0.5, api_attach=0.5),
)


@dataclass
class MonthRow:
    month: int
    posture: str
    pro_seats: float
    team_accounts: float
    api_accounts: float
    rev_pro: float
    rev_team: float
    rev_api: float
    cash_pro: float
    cash_team: float
    cash_api: float
    fees: float
    costs: float
    cost_lines: dict[str, float] = field(default_factory=dict)

    @property
    def revenue(self) -> float:
        return self.rev_pro + self.rev_team + self.rev_api

    @property
    def cash_in(self) -> float:
        return self.cash_pro + self.cash_team + self.cash_api

    @property
    def net(self) -> float:
        return self.cash_in - self.fees - self.costs


@dataclass
class Result:
    params: Params
    rows: list[MonthRow]
    cumulative: list[float]

    @property
    def operating_breakeven(self) -> int | None:
        """First month where recognised revenue net of fees covers that month's costs."""
        for r in self.rows:
            if r.revenue > 0 and r.revenue - r.fees >= r.costs:
                return r.month
        return None

    @property
    def cash_positive_month(self) -> int | None:
        """First month where cumulative cash is back to zero or above and stays there."""
        for i, c in enumerate(self.cumulative):
            if c >= 0 and all(x >= 0 for x in self.cumulative[i:]):
                return i + 1
        return None

    @property
    def trough(self) -> tuple[float, int]:
        low = min(self.cumulative)
        return low, self.cumulative.index(low) + 1

    @property
    def end_cash(self) -> float:
        return self.cumulative[-1]


def month_label(m: int) -> str:
    total = START_YEAR * 12 + (START_MONTH - 1) + (m - 1)
    return f"{total // 12}-{total % 12 + 1:02d}"


def monthly_churn(annual: float) -> float:
    return 1.0 - float((1.0 - annual) ** (1.0 / 12.0))


def cost_lines(p: Params, m: int, selling: bool) -> dict[str, float]:
    gateway_live = m >= p.modelgw_start_month
    lines = {
        "Infrastructure core (docs/60 §4)": p.infra_core,
        "Model calls (docs/60 §4, from gateway ship month)": p.model_calls if gateway_live else 0.0,
        "X posting (docs/32 §4.7)": p.x_posting if m >= p.x_start_month else 0.0,
        "LinkedIn scheduler bridge (docs/32 §4.6)": p.linkedin_bridge,
        "CRM seat, Attio Plus (from switch month)": p.crm_seat if selling else 0.0,
        "PJM licence (unpriced, parameter)": p.pjm_licence if selling else 0.0,
        "Owner draw (unpriced, parameter)": p.owner_draw,
        "Counsel one-off (unpriced, parameter)": p.counsel_one_off if m == p.switch_month else 0.0,
    }
    return lines


def simulate(p: Params) -> Result:
    pro_churn_m = monthly_churn(p.pro_annual_churn)
    team_keep = 1.0 - p.team_annual_churn
    pro_seats = 0.0
    team_cohorts: dict[int, float] = {}  # signing month -> accounts signed that month
    rows: list[MonthRow] = []
    cumulative: list[float] = []
    running = 0.0
    for m in range(1, MONTHS + 1):
        selling = m >= p.switch_month
        posture = "commercial" if selling else "noncommercial"
        # Pro: monthly churn on the opening balance, then this month's gross adds.
        pro_seats = pro_seats * (1.0 - pro_churn_m)
        if selling:
            pro_seats += p.pro_adds_per_month
            team_cohorts[m] = p.team_adds_per_month
        # Team / API: cohorts survive at team_keep per completed contract year.
        team_active = 0.0
        team_cash_units = 0.0  # accounts paying an annual invoice this month
        for signed, n in team_cohorts.items():
            age = m - signed
            alive = n * team_keep ** (age // 12)
            team_active += alive
            if age % 12 == 0:
                team_cash_units += alive
        api_active = team_active * p.api_attach
        api_cash_units = team_cash_units * p.api_attach

        rev_pro = pro_seats * p.pro_monthly
        rev_team = team_active * p.team_annual / 12.0
        rev_api = api_active * p.api_addon_annual / 12.0
        cash_pro = rev_pro
        cash_team = team_cash_units * p.team_annual
        cash_api = api_cash_units * p.api_addon_annual
        cash_in = cash_pro + cash_team + cash_api
        charges = pro_seats + team_cash_units + api_cash_units
        fees = cash_in * (p.card_pct + p.billing_pct) + charges * p.card_fixed if selling else 0.0

        lines = cost_lines(p, m, selling)
        costs = sum(lines.values())
        row = MonthRow(
            m,
            posture,
            pro_seats,
            team_active,
            api_active,
            rev_pro,
            rev_team,
            rev_api,
            cash_pro,
            cash_team,
            cash_api,
            fees,
            costs,
            lines,
        )
        rows.append(row)
        running += row.net
        cumulative.append(running)
    return Result(p, rows, cumulative)


# --- output -----------------------------------------------------------------------------------


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def table(headers: list[str], rows: list[list[str]]) -> None:
    emit("| " + " | ".join(headers) + " |")
    emit("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        emit("| " + " | ".join(r) + " |")
    emit()


def fmt_month(m: int | None) -> str:
    return f"M{m} ({month_label(m)})" if m is not None else "not within 24 months"


def print_cost_stack(base: Params) -> None:
    emit("### Table 1. Monthly cost stack (USD), base parameters")
    emit()
    rows = [[name, f"{v:,.2f}", src] for name, v, src in INFRA_LINES]
    subtotal = f"**{INFRA_CORE_BASE:,.2f}**"
    rows.append(["**Infrastructure core subtotal**", subtotal, "docs/60 §4 range 80–170"])
    table(["Line", "USD/mo", "Source"], rows)
    phases = [
        ("noncommercial, before model gateway (M1–M3)", 1),
        ("noncommercial, model gateway live (M4–M6)", base.modelgw_start_month),
        ("commercial (from switch month M7)", base.switch_month),
    ]
    headers = ["Line"] + [ph for ph, _ in phases]
    lines_by_phase = [cost_lines(base, m, m >= base.switch_month) for _, m in phases]
    names = list(lines_by_phase[0].keys())
    rows = [[n] + [f"{lp[n]:,.2f}" for lp in lines_by_phase] for n in names]
    rows.append(["**Total per month**"] + [f"**{sum(lp.values()):,.2f}**" for lp in lines_by_phase])
    table(headers, rows)


def print_scenarios(results: list[Result]) -> None:
    emit("### Table 2. Scenario summary")
    emit()
    rows = []
    for r in results:
        p = r.params
        last = r.rows[-1]
        trough, trough_m = r.trough
        rows.append(
            [
                p.name,
                f"{p.pro_adds_per_month:g} / {p.team_adds_per_month * 12:g} / {p.api_attach:.0%}",
                fmt_month(r.operating_breakeven),
                fmt_month(r.cash_positive_month),
                f"{usd(trough)} at M{trough_m}",
                usd(r.end_cash),
                f"{last.pro_seats:.0f} / {last.team_accounts:.1f} / {last.api_accounts:.1f}",
                usd(last.revenue),
                usd(last.revenue * 12),
            ]
        )
    table(
        [
            "Scenario",
            "Pro adds/mo / Team adds/yr / API attach",
            "Operating break-even",
            "Cash back to zero",
            "Cash trough",
            "Cumulative cash M24",
            "Seats / Team / API at M24",
            "Recognised revenue M24",
            "ARR run-rate M24",
        ],
        rows,
    )


def print_monthly(r: Result) -> None:
    letter = "abc"[("low", "base", "high").index(r.params.name)]
    emit(f"### Table 3{letter}. Monthly model, {r.params.name} scenario")
    emit()
    rows = []
    for row, cum in zip(r.rows, r.cumulative, strict=True):
        rows.append(
            [
                f"M{row.month}",
                month_label(row.month),
                row.posture,
                f"{row.pro_seats:.1f}",
                f"{row.team_accounts:.2f}",
                f"{row.api_accounts:.2f}",
                usd(row.revenue),
                usd(row.cash_in),
                usd(row.fees),
                usd(row.costs),
                usd(row.net),
                usd(cum),
            ]
        )
    table(
        [
            "M",
            "Month",
            "Posture",
            "Pro seats",
            "Team",
            "API",
            "Recognised rev",
            "Cash in",
            "Stripe fees",
            "Costs",
            "Net cash",
            "Cumulative cash",
        ],
        rows,
    )


def print_revenue_by_tier(r: Result) -> None:
    emit(f"### Table 4. Revenue by tier after the switch, {r.params.name} scenario (recognised / cash)")
    emit()
    rows = []
    for row in r.rows:
        if row.posture != "commercial":
            continue
        rows.append(
            [
                f"M{row.month}",
                month_label(row.month),
                f"{usd(row.rev_pro)} / {usd(row.cash_pro)}",
                f"{usd(row.rev_team)} / {usd(row.cash_team)}",
                f"{usd(row.rev_api)} / {usd(row.cash_api)}",
                f"{usd(row.revenue)} / {usd(row.cash_in)}",
            ]
        )
    table(["M", "Month", "Pro", "Team", "API add-on", "Total"], rows)


@dataclass(frozen=True)
class Sweep:
    label: str
    attr: str
    low: float
    high: float
    source: str


SWEEPS: tuple[Sweep, ...] = (
    Sweep("Switch-back month", "switch_month", 4, 13, "A-1; owner has not set a date"),
    Sweep("Pro gross adds per month", "pro_adds_per_month", 2.5, 7.5, "docs/11 §2.3 year-1 band 40–80 seats"),
    Sweep("Team gross adds per month", "team_adds_per_month", 1 / 6, 1 / 2, "docs/11 §2.3 band 3–6 per year"),
    Sweep("Pro annual churn", "pro_annual_churn", 0.10, 0.35, "docs/11 §2.3 assumes 20%; no measurement yet"),
    Sweep("Pro price per seat-month", "pro_monthly", 149, 199, "docs/33 §6.1: pre-sale $150, list $200–250"),
    Sweep("API attach rate on Team", "api_attach", 0.0, 1.0, "docs/11 §3; no measurement"),
    Sweep("Infrastructure core", "infra_core", 80, 170, "docs/60 §4 range"),
    Sweep("Model calls per month", "model_calls", 60, 150, "docs/60 §4 range once gateway ships"),
    Sweep("X posting per month", "x_posting", 0, 250, "docs/32 §4.7: off, or the $250 cap"),
    Sweep("Owner draw per month (illustrative)", "owner_draw", 0, 6000, "unpriced; owner input"),
    Sweep("PJM licence per month (illustrative)", "pjm_licence", 0, 1000, "unpriced; docs/20 §14"),
)


def apply(p: Params, attr: str, value: float) -> Params:
    kwargs: dict[str, Any] = {attr: int(value) if attr == "switch_month" else float(value)}
    return replace(p, **kwargs)


def print_tornado(base: Params) -> list[Sweep]:
    ref = simulate(base)
    emit("### Table 5. Sensitivity ranking (tornado), base scenario, effect on cumulative cash at M24")
    emit()
    scored: list[tuple[float, Sweep, Result, Result]] = []
    for s in SWEEPS:
        lo = simulate(apply(base, s.attr, s.low))
        hi = simulate(apply(base, s.attr, s.high))
        span = abs(hi.end_cash - lo.end_cash)
        scored.append((span, s, lo, hi))
    scored.sort(key=lambda t: t[0], reverse=True)
    rows = []
    for span, s, lo, hi in scored:
        rows.append(
            [
                s.label,
                f"{s.low:g} → {s.high:g}",
                f"{usd(lo.end_cash - ref.end_cash)} / {usd(hi.end_cash - ref.end_cash)}",
                usd(span),
                f"{fmt_month(lo.operating_breakeven)} / {fmt_month(hi.operating_breakeven)}",
                s.source,
            ]
        )
    table(
        [
            "Input",
            "Low → high",
            "Δ cash M24 at low / at high",
            "Swing",
            "Operating break-even at low / high",
            "Range source",
        ],
        rows,
    )
    emit(
        f"Reference (base): cumulative cash at M24 {usd(ref.end_cash)}, "
        f"operating break-even {fmt_month(ref.operating_breakeven)}."
    )
    emit()
    return [s for _, s, _, _ in scored[:3]]


def print_grids(base: Params, top: list[Sweep]) -> None:
    for i, s in enumerate(top):
        emit(f"### Table 6{'abc'[i]}. Grid: {s.label}")
        emit()
        if s.attr == "switch_month":
            values = [float(v) for v in (4, 7, 10, 13, MONTHS + 1)]  # MONTHS + 1 = never switches
        else:
            values = [s.low + (s.high - s.low) * k / 4 for k in range(5)]
        rows = []
        for v in values:
            r = simulate(apply(base, s.attr, v))
            trough, trough_m = r.trough
            if s.attr == "switch_month":
                shown = "never (cost only)" if v > MONTHS else f"{int(v)} ({month_label(int(v))})"
            else:
                shown = f"{v:g}"
            rows.append(
                [
                    shown,
                    fmt_month(r.operating_breakeven),
                    fmt_month(r.cash_positive_month),
                    f"{usd(trough)} at M{trough_m}",
                    usd(r.end_cash),
                    usd(r.rows[-1].revenue * 12),
                ]
            )
        headers = [
            s.label,
            "Operating break-even",
            "Cash back to zero",
            "Cash trough",
            "Cumulative cash M24",
            "ARR M24",
        ]
        table(headers, rows)


def main() -> int:
    base = SCENARIOS[1]
    results = [simulate(p) for p in SCENARIOS]
    emit("<!-- generated by scripts/financial_model.py; do not edit the tables by hand -->")
    emit()
    print_cost_stack(base)
    print_scenarios(results)
    for r in results:
        print_monthly(r)
    print_revenue_by_tier(results[1])
    top = print_tornado(base)
    print_grids(base, top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
