"""The pre-collector baseline, from propane invoices and the station's weather history.

Fits gallons used between deliveries = base x days + slope x HDD, with no constant term.
The base is the non-heating load (hot water, range, fireplace) and the slope is gallons
per heating degree day.
"""

from dataclasses import dataclass
from datetime import date, timedelta

# A delivery interval needs HDD for at least this share of its days.
MIN_HDD_COVERAGE = 0.9


@dataclass
class Delivery:
    date: date
    gallons: float
    price_per_gal: float | None
    tank_pct_before: float | None
    tank_pct_after: float | None


@dataclass
class DeliveryInterval:
    start: date  # a delivery
    end: date  # the next delivery
    days: int
    gallons: float  # used during the interval
    hdd: float | None  # None when too many days have no HDD
    price_per_gal: float | None  # paid at the end delivery
    method: str


@dataclass
class Baseline:
    base_gal_per_day: float
    gal_per_hdd: float
    r2: float
    intervals: int
    start: date
    end: date

    def predict(self, days: int, hdd: float) -> float:
        return self.base_gal_per_day * days + self.gal_per_hdd * hdd


def delivery_intervals(deliveries: list[Delivery], hdd_by_day: dict[date, float],
                       tank_size: float) -> list[DeliveryInterval]:
    """Propane used between consecutive deliveries.

    With the tank level after both fills, use = the later delivery's gallons, corrected for
    the two fills ending at different levels. With the level after the first fill and the
    level before the second, use is the drop between them. Otherwise assume both fills
    topped the tank to the same level, so use = the later delivery's gallons.
    """
    ds = sorted(deliveries, key=lambda d: d.date)
    out = []
    for a, b in zip(ds, ds[1:]):
        if a.tank_pct_after is not None and b.tank_pct_after is not None:
            used = b.gallons + tank_size * (a.tank_pct_after - b.tank_pct_after) / 100
            method = "fill levels"
        elif a.tank_pct_after is not None and b.tank_pct_before is not None:
            used = tank_size * (a.tank_pct_after - b.tank_pct_before) / 100
            method = "tank levels"
        else:
            used, method = b.gallons, "delivery size"
        days = (b.date - a.date).days
        span = [a.date + timedelta(days=i) for i in range(days)]
        known = [hdd_by_day[d] for d in span if d in hdd_by_day]
        hdd = sum(known) * days / len(known) if span and len(known) >= MIN_HDD_COVERAGE * days else None
        out.append(DeliveryInterval(a.date, b.date, days, round(used, 1),
                                    None if hdd is None else round(hdd, 1), b.price_per_gal, method))
    return out


def fit_baseline(intervals: list[DeliveryInterval]) -> Baseline | None:
    """Least squares for gallons = base x days + slope x HDD over intervals with HDD."""
    use = [iv for iv in intervals if iv.hdd is not None and iv.days > 0]
    if len(use) < 3:
        return None
    sdd = sum(iv.days ** 2 for iv in use)
    sdh = sum(iv.days * iv.hdd for iv in use)
    shh = sum(iv.hdd ** 2 for iv in use)
    sdg = sum(iv.days * iv.gallons for iv in use)
    shg = sum(iv.hdd * iv.gallons for iv in use)
    det = sdd * shh - sdh ** 2
    if det == 0:
        return None
    base = (sdg * shh - shg * sdh) / det
    slope = (shg * sdd - sdg * sdh) / det
    mean_g = sum(iv.gallons for iv in use) / len(use)
    ss_tot = sum((iv.gallons - mean_g) ** 2 for iv in use)
    ss_res = sum((iv.gallons - base * iv.days - slope * iv.hdd) ** 2 for iv in use)
    return Baseline(base, slope, 1 - ss_res / ss_tot if ss_tot else 0.0, len(use),
                    min(iv.start for iv in use), max(iv.end for iv in use))


def season_of(d: date) -> str:
    """Heating seasons run October to April, labeled like '2025–26'. The rest is summer."""
    if d.month >= 10:
        return f"{d.year}–{str(d.year + 1)[2:]}"
    if d.month <= 4:
        return f"{d.year - 1}–{str(d.year)[2:]}"
    return f"Summer {d.year}"


def by_season(intervals: list[DeliveryInterval], hdd_by_day: dict[date, float], fit: Baseline,
              default_price: float | None) -> list[dict]:
    """Spread each interval's gallons over its days by the fitted line, then total by season.

    Seasons at either end are only partly covered by deliveries; 'days' shows how much.
    Delivery periods without weather are left out, so gallons and HDD stay matched.
    """
    seasons: dict[str, dict] = {}
    for iv in intervals:
        if iv.hdd is None:
            continue
        span = [iv.start + timedelta(days=i) for i in range(iv.days)]
        weights = [fit.base_gal_per_day + fit.gal_per_hdd * hdd_by_day.get(d, 0.0) for d in span]
        total = sum(weights) or 1.0
        price = iv.price_per_gal or default_price
        for d, w in zip(span, weights):
            s = seasons.setdefault(season_of(d), {"first": d, "days": 0, "gallons": 0.0, "hdd": 0.0,
                                                  "cost": 0.0, "priced": True})
            gal = iv.gallons * w / total
            s["days"] += 1
            s["gallons"] += gal
            s["hdd"] += hdd_by_day.get(d, 0.0)
            if price is None:
                s["priced"] = False
            else:
                s["cost"] += gal * price
    return [{
        "season": name, "days": s["days"], "gallons": round(s["gallons"]), "hdd": round(s["hdd"]),
        "gal_per_hdd": round(s["gallons"] / s["hdd"], 3) if s["hdd"] >= 50 else None,
        "cost": round(s["cost"]) if s["priced"] else None,
        "cost_per_hdd": round(s["cost"] / s["hdd"], 2) if s["priced"] and s["hdd"] >= 50 else None,
    } for name, s in sorted(seasons.items(), key=lambda kv: kv[1]["first"])]
