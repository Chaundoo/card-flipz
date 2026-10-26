#!/usr/bin/env python3
"""Shared slab record for the three graded bots (graded/sources.json).

Every bot that looks up a slab writes one line here: which source checked it, when, and the PSA 10 / PSA 9
prices it saw. Before spending its daily requests, each bot reads this file to

  * skip slabs that are over your price cap (max_slab_price in scripts/graded_config.json),
  * skip slabs another eBay-sales bot (tcgapi.net / PokemonPriceTracker / pokemon-api.com) checked in the last
    couple of days, except the slabs that are heating up (those get a second source, on purpose, as a cross-check),
  * pick the slabs no sales source has looked at for the longest time first.

Sources:  "cardprice" (CardPrice.com, free and fast: the broad daily sweep of estimates, pop counts, history)
          "ta"        (tcgapi.net, up to ~400 slabs a day: individual eBay sold comps, cleaned of outliers)
          "ppt"       (PokemonPriceTracker, ~30 slabs a day: eBay sales counts and history)
          "pa"        (pokemon-api.com via RapidAPI, a few slabs a day: eBay median and how many sold)

The three eBay-sales sources (ta, ppt, pa) skip any slab another one of them checked in the last
skip_if_other_checked_days days, except slabs heating up, which get a second opinion on purpose.

The workflows for all four bots share one GitHub Actions concurrency group and run one after another
(CardPrice -> tcgapi.net -> pokemon-api.com -> Graded Slabs), so only one bot ever writes this file at a time.
"""
import json, os, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "graded", "sources.json")
CFG_PATH = os.path.join(ROOT, "scripts", "graded_config.json")
SALES_SOURCES = ("ta", "ppt", "pa")    # the bots with daily limits that read eBay sales
ALL_SOURCES = ("cardprice", "ta", "ppt", "pa")
NAMES = {"cardprice": "CardPrice", "ta": "tcgapi.net", "ppt": "PokemonPriceTracker", "pa": "pokemon-api.com"}


def today():
    """Same 'today' as the other bots (Pacific standard time), so dates line up across files."""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8))).date().isoformat()


def days_since(d, ref=None):
    try:
        return (datetime.date.fromisoformat((ref or today())[:10]) - datetime.date.fromisoformat(str(d)[:10])).days
    except (TypeError, ValueError):
        return 9999


def load_cfg():
    try:
        return json.load(open(CFG_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cap_of(cfg):
    """The most you'd pay for one slab. Change max_slab_price in scripts/graded_config.json."""
    for k in ("max_slab_price", "budget_default"):
        try:
            v = float(cfg.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 1500.0


class Sources:
    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else load_cfg()
        s = self.cfg.get("sources") or {}
        self.cap = cap_of(self.cfg)
        self.skip_days = int(s.get("skip_if_other_checked_days", 2))
        self.disagree = float(s.get("disagree_pct", 25))
        self.fresh_days = int(s.get("price_fresh_days", 45))
        self.p9_guess = float(s.get("psa9_share_of_psa10_guess", 0.35))
        try:
            self.data = json.load(open(PATH, encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.slabs = self.data.setdefault("slabs", {})

    # ---------------------------------------------------------- writing
    def record(self, key, source, psa10=None, psa9=None, n10=None, n9=None, found=True):
        """Remember that `source` checked slab `key` today and what it saw (prices may be None)."""
        e = self.slabs.setdefault(str(key), {})
        r = {"d": today()}
        if not found:
            r["none"] = 1
        for k, v in (("p10", psa10), ("p9", psa9), ("n10", n10), ("n9", n9)):
            if isinstance(v, (int, float)) and v > 0:
                r[k] = round(float(v), 2) if k[0] == "p" else int(v)
        e[source] = r

    def save(self):
        self.data["about"] = ("Shared by scripts/cardprice_bot.py, scripts/pokemon_api_bot.py and scripts/graded_heat.py: "
                              "which source last checked each slab, when, and the PSA 10 / 9 prices it saw. Not loaded by the site.")
        self.data["cap"] = self.cap
        self.data["updated"] = today()
        # forget slabs nobody has touched in over a year
        for k in [k for k, e in self.slabs.items() if min((days_since(r.get("d")) for r in e.values()), default=9999) > 400]:
            del self.slabs[k]
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    # ---------------------------------------------------------- reading
    def get(self, key, source):
        return (self.slabs.get(str(key)) or {}).get(source) or {}

    def last_checked(self, key, source):
        return self.get(key, source).get("d")

    def sales_age(self, key):
        """Days since any eBay-sales source (tcgapi.net, PokemonPriceTracker, pokemon-api.com) looked at this slab (9999 = never)."""
        return min((days_since(self.last_checked(key, s)) for s in SALES_SOURCES if self.last_checked(key, s)), default=9999)

    def other_sales_checked_recently(self, key, me):
        """True if another eBay-sales bot looked at this slab in the last `skip_if_other_checked_days` days."""
        for s in SALES_SOURCES:
            if s != me and days_since(self.last_checked(key, s)) < self.skip_days:
                return True
        return False

    def best_prices(self, key, extra=None):
        """Newest PSA 10 / PSA 9 price any source saw in the last `price_fresh_days` days -> (p10, p9).
        `extra` = [(date, p10, p9)] from a bot's own memory, used the same way."""
        rows = [(r.get("d") or "", r.get("p10"), r.get("p9")) for r in (self.slabs.get(str(key)) or {}).values()]
        rows += list(extra or [])
        rows = [r for r in rows if days_since(r[0]) <= self.fresh_days]
        rows.sort(key=lambda r: r[0], reverse=True)
        p10 = next((r[1] for r in rows if r[1]), None)
        p9 = next((r[2] for r in rows if r[2]), None)
        return p10, p9

    def over_cap(self, key, extra=None):
        """True when this slab is over your cap in both PSA 10 and PSA 9 (so no limited requests go to it).
        If only the PSA 10 price is known, the PSA 9 is guessed at about a third of it."""
        p10, p9 = self.best_prices(key, extra)
        if p9:
            return p9 > self.cap and (not p10 or p10 > self.cap)
        if p10:
            return p10 * self.p9_guess > self.cap
        return False

    def known_affordable(self, key, extra=None):
        p10, p9 = self.best_prices(key, extra)
        return bool((p10 and p10 <= self.cap) or (p9 and p9 <= self.cap))

    def conflict(self, key, grade="p10", days=7):
        """If two or more eBay-sales sources (tcgapi.net, PokemonPriceTracker, pokemon-api.com) priced this grade in
        the last `days` days and disagree by more than disagree_pct, return [(source name, price), ...]; else None.
        (CardPrice's graded price is an estimate, not a recent-sales median, so it isn't used to flag.)"""
        seen = [(NAMES[s], r[grade]) for s, r in (self.slabs.get(str(key)) or {}).items()
                if s in SALES_SOURCES and r.get(grade) and days_since(r.get("d")) <= days]
        if len(seen) < 2:
            return None
        lo, hi = min(p for _, p in seen), max(p for _, p in seen)
        return sorted(seen, key=lambda x: x[1]) if lo > 0 and (hi / lo - 1) * 100 > self.disagree else None
