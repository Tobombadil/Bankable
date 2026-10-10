"""The statistics behind the owner's hand check (`services/sites/handcheck.py`): pure functions, no store.

* **Wilson score interval** (`wilson`) for a proportion: it stays inside [0, 1] and behaves at 0 or n
  errors, where the normal ("Wald") interval collapses to a point. Wilson (1927), "Probable inference,
  the law of succession, and statistical inference", JASA 22:209-212; Brown, Cai and DasGupta (2001),
  "Interval estimation for a binomial proportion", Statistical Science 16:101-133, who recommend it for
  small n.
* **Allocation** (`allocate`): strata that must be checked in full (the largest sites) take every item;
  the rest of the budget goes to the other strata in proportion to the square root of their size, at
  least `minimum` each (never more than a stratum holds). Square-root allocation sits between
  proportional allocation (best for the overall rate) and equal allocation (best for comparing strata):
  every rule and confidence level gets looked at, and the big strata still dominate.
* **Permanent random numbers** (`prn`): an item's draw number is a hash of the seed and its stable key
  (source id plus source record id), so re-running the generator on another store holding the same
  real-world records draws the same items where it can (coordinated sampling, as statistics offices
  use for repeated business surveys: Ohlsson (1995), "Coordination of samples using permanent random
  numbers", in Business Survey Methods, Wiley).
* **Weighted estimate** (`estimate`): each judged item stands for `N_h / n_h` items of its stratum
  (`n_h` = items judged Yes or No there), so the estimate is the error rate of the whole served
  population rather than of a sample that over-represents the risky strata. Its interval is the Wilson
  interval at Kish's effective sample size `(sum w)^2 / sum w^2` (Kish 1965, Survey Sampling), one of
  the intervals Dean and Pagano (2015, Journal of Survey Statistics and Methodology 3:213-235) found to
  hold its coverage for survey proportions. Unsure and blank items are left out of both numerator and
  denominator (treated as missing at random within their stratum) and counted separately.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: Two-sided 95 % normal quantile.
Z95 = 1.959963984540054
#: The owner's rule (decisions log 2026-10-10): more than about one site in ten wrong switches sites off.
KILL_SWITCH_RATE = 0.10


def wilson(errors: float, n: float, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for `errors` out of `n` (either may be fractional, for an effective
    sample size). `(0.0, 1.0)` when `n` is zero: nothing judged says nothing."""
    if n <= 0:
        return 0.0, 1.0
    p = min(max(errors / n, 0.0), 1.0)
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    # At p = 0 (or 1) the bound is exactly 0 (or 1); the arithmetic leaves a rounding residue.
    lower = 0.0 if p == 0.0 else max(0.0, centre - half)
    upper = 1.0 if p == 1.0 else min(1.0, centre + half)
    return lower, upper


def prn(seed: int, key: str) -> float:
    """A permanent random number in [0, 1) for `key` under `seed` (module docstring)."""
    digest = hashlib.sha256(f"{seed}\x1f{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


def allocate(
    populations: Mapping[str, int],
    total: int,
    *,
    minimum: int = 2,
    certainty: Iterable[str] = (),
) -> dict[str, int]:
    """Sample sizes per stratum (module docstring). Certainty strata take every item and count against
    `total`; the remainder is shared by the square root of each other stratum's size, each at least
    `min(minimum, N_h)` and at most `N_h`, rounded by largest remainder. When the strata hold no more
    than `total` items in all, every item is taken."""
    sizes = {h: max(0, int(n)) for h, n in populations.items()}
    sure = {h for h in certainty if h in sizes}
    out = {h: (sizes[h] if h in sure else 0) for h in sizes}
    rest = [h for h in sorted(sizes) if h not in sure and sizes[h] > 0]
    budget = total - sum(out.values())
    if budget <= 0 or not rest:
        return out
    if sum(sizes[h] for h in rest) <= budget:
        out.update({h: sizes[h] for h in rest})
        return out
    floor = {h: min(minimum, sizes[h]) for h in rest}
    if sum(floor.values()) >= budget:
        # Not even the minimum fits: the largest strata get theirs first.
        for h in sorted(rest, key=lambda h: (-sizes[h], h)):
            take = min(floor[h], budget)
            out[h] = take
            budget -= take
        return out
    # Square-root shares, capped at N_h and raised to the floor; repeat until nothing moves.
    fixed: dict[str, float] = {}
    share: dict[str, float] = {}
    while True:
        free = [h for h in rest if h not in fixed]
        left = budget - sum(fixed.values())
        root = sum(math.sqrt(sizes[h]) for h in free)
        share = {h: left * math.sqrt(sizes[h]) / root for h in free} if root else {}
        moved = False
        for h in free:
            if share[h] >= sizes[h]:
                fixed[h] = float(sizes[h])
                moved = True
            elif share[h] < floor[h]:
                fixed[h] = float(floor[h])
                moved = True
        if not moved:
            break
    exact = {**share, **fixed}
    whole = {h: math.floor(exact[h]) for h in rest}
    spare = budget - sum(whole.values())
    order = sorted(rest, key=lambda h: (-(exact[h] - whole[h]), -sizes[h], h))
    for h in order:
        if spare <= 0:
            break
        if whole[h] < sizes[h]:
            whole[h] += 1
            spare -= 1
    out.update(whole)
    return out


@dataclass(frozen=True)
class Judged:
    """One sampled item as the scorer reads it back: its stratum, the stratum's population and sample
    size as written, and the verdict: True = an error, False = right, None = unsure or blank."""

    stratum: str
    population: int
    sampled: int
    error: bool | None
    unsure: bool = False


@dataclass
class StratumResult:
    stratum: str
    population: int
    sampled: int
    judged: int = 0
    errors: int = 0
    unsure: int = 0
    blank: int = 0

    @property
    def rate(self) -> float | None:
        return self.errors / self.judged if self.judged else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.errors, self.judged)


@dataclass
class Estimate:
    sampled: int = 0
    judged: int = 0
    errors: int = 0
    unsure: int = 0
    blank: int = 0
    #: Unweighted: the share of judged sample items that are errors.
    rate: float | None = None
    interval: tuple[float, float] = (0.0, 1.0)
    #: Weighted back to the population.
    weighted_rate: float | None = None
    weighted_interval: tuple[float, float] = (0.0, 1.0)
    effective_n: float = 0.0
    #: Share of the population in strata with at least one judged item (the weighted estimate's reach).
    covered: float = 0.0
    population: int = 0
    strata: list[StratumResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "sampled": self.sampled,
            "judged": self.judged,
            "errors": self.errors,
            "unsure": self.unsure,
            "blank": self.blank,
            "rate": self.rate,
            "interval": list(self.interval),
            "weighted_rate": self.weighted_rate,
            "weighted_interval": list(self.weighted_interval),
            "effective_n": round(self.effective_n, 2),
            "covered": round(self.covered, 4),
            "population": self.population,
            "strata": [
                {
                    "stratum": s.stratum,
                    "population": s.population,
                    "sampled": s.sampled,
                    "judged": s.judged,
                    "errors": s.errors,
                    "unsure": s.unsure,
                    "blank": s.blank,
                    "rate": s.rate,
                    "interval": list(s.interval),
                }
                for s in self.strata
            ],
        }


def estimate(items: Sequence[Judged]) -> Estimate:
    """Unweighted and stratum-weighted error rates with Wilson intervals (module docstring)."""
    by: dict[str, StratumResult] = {}
    for item in items:
        s = by.setdefault(item.stratum, StratumResult(item.stratum, item.population, item.sampled))
        s.population = max(s.population, item.population)
        s.sampled = max(s.sampled, item.sampled)
        if item.error is None:
            if item.unsure:
                s.unsure += 1
            else:
                s.blank += 1
            continue
        s.judged += 1
        s.errors += int(item.error)
    out = Estimate(strata=sorted(by.values(), key=lambda s: s.stratum))
    out.sampled = len(items)
    out.judged = sum(s.judged for s in out.strata)
    out.errors = sum(s.errors for s in out.strata)
    out.unsure = sum(s.unsure for s in out.strata)
    out.blank = sum(s.blank for s in out.strata)
    out.population = sum(s.population for s in out.strata)
    if out.judged:
        out.rate = out.errors / out.judged
        out.interval = wilson(out.errors, out.judged)
    weights: list[tuple[float, int]] = []
    for s in out.strata:
        if s.judged:
            w = s.population / s.judged
            weights.extend([(w, 1)] * s.errors + [(w, 0)] * (s.judged - s.errors))
    total_w = sum(w for w, _ in weights)
    if total_w > 0:
        out.weighted_rate = sum(w * y for w, y in weights) / total_w
        out.effective_n = total_w**2 / sum(w * w for w, _ in weights)
        out.weighted_interval = wilson(out.weighted_rate * out.effective_n, out.effective_n)
        out.covered = total_w / out.population if out.population else 0.0
    return out


def decisive_counts(n: int, threshold: float = KILL_SWITCH_RATE) -> tuple[int, int, int]:
    """For a sample of `n` judged items: the largest error count whose Wilson upper bound is at or
    under `threshold` (-1 when none is), the smallest count whose point estimate is above it, and the
    smallest count whose Wilson lower bound is above it (n + 1 when none is). The Instructions sheet
    states these so the owner knows what a result will mean before starting."""
    clean = max((k for k in range(n + 1) if wilson(k, n)[1] <= threshold), default=-1)
    over = min((k for k in range(n + 1) if k / n > threshold), default=n + 1) if n else 1
    sure = min((k for k in range(n + 1) if wilson(k, n)[0] > threshold), default=n + 1)
    return clean, over, sure
