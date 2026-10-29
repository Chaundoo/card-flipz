#!/usr/bin/env python3
"""Shared slab record for the graded bots (graded/sources.json), and the price check that decides which
PSA 10 / PSA 9 price to believe.

Every bot that looks up a slab writes one line here: which source checked it, when, and the PSA 10 / PSA 9
prices it saw. Before spending its daily requests, each bot reads this file to

  * skip slabs that are over your price cap (max_slab_price in scripts/graded_config.json),
  * skip slabs another eBay-sales bot (PokemonPriceTracker / pokemon-api.com) checked in the last couple of
    days, except the slabs that are heating up (those get a second source, on purpose, as a cross-check),
  * pick the slabs no sales source has looked at for the longest time first.

Sources:  "cardprice" (CardPrice.com, free and fast: the broad daily sweep of estimates, pop counts, history)
          "ppt"       (PokemonPriceTracker, ~30 slabs a day: eBay sales counts and history)
          "pa"        (pokemon-api.com via RapidAPI, ~90 slabs a day: eBay median and how many sold)
          "site"      (the PSA 10 / Grade 9 values saved on your own site's cards; passed in, not stored here)

Which price to believe (consensus(), used by the Graded Slabs bot and the price-cap test). Each source
gets things wrong in its own way (one odd sale, the wrong card matched, an estimate far off), so no single
source is trusted on its own:
  1. Too old (over price_fresh_days) is ignored.
  2. Sanity vs the raw NM price: a PSA 10 under ~1x raw or a PSA 9 under ~0.6x raw is the wrong card or a
     junk sale; one over ~250x raw (PSA 10) / ~40x raw (PSA 9) is too (your site's real cards never go
     past ~105x / ~12x). Those are thrown out.
  3. The rest are combined as a weighted median (weights: how reliable the source is, how many sales it
     saw, how recent, and how often it has agreed with the others before: "trust", learned over time).
     With 3+ prices, any more than 60% away from the middle is thrown out.
  4. PSA 10 must be worth more than PSA 9. If they come out the other way round, the less certain grade is
     re-done without the prices that cause it; if that can't fix it, both are marked unverified.
  5. Confidence: "high" = 2+ sources agree within 25%; "med" = one real sales source with 2+ sales;
     "est" = only CardPrice's estimate so far (passed the checks, but no sales behind it yet);
     "low" = one sale, or sources that can't be reconciled. Low-confidence slabs are never shown as
     "Breaking out" / "Warming up"; they show "Verify price" instead.

The workflows for all the slab bots share one GitHub Actions concurrency group and run one after another
(CardPrice -> pokemon-api.com -> Graded Slabs), so only one bot ever writes this file at a time.
"""
import json, os, math, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "graded", "sources.json")
CFG_PATH = os.path.join(ROOT, "scripts", "graded_config.json")
SALES_SOURCES = ("ppt", "pa")          # the bots with daily limits that read eBay sales
ALL_SOURCES = ("cardprice", "ppt", "pa")
NAMES = {"cardprice": "CardPrice", "ppt": "PokemonPriceTracker", "pa": "pokemon-api.com", "site": "your site",
         "own": "earlier checks", "ta": "tcgapi.net (retired)"}
# how much each source counts when they're combined (scaled by sales count, age and learned trust)
BASE_WEIGHT = {"ppt": 1.0, "pa": 0.6, "cardprice": 0.5, "site": 0.7, "own": 0.6}
# raw NM -> plausible slab price: (lowest multiple, highest multiple, plus this many dollars of slack on top)
RAW_BOUNDS = {"p10": (1.0, 250.0, 500.0), "p9": (0.6, 40.0, 300.0)}
CONF_RANK = {"low": 0, "est": 1, "med": 2, "high": 3}
ESTIMATES = ("cardprice",)             # sources that give an estimate, not recent sales


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
    """The most the bots will consider for one slab (max_slab_price in scripts/graded_config.json).
    0 or null = no cap (the default now: your budget is set with the slider on the Graded Slabs tab)."""
    if "max_slab_price" in cfg and not cfg.get("max_slab_price"):
        return float("inf")
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
        self.off = set(self.cfg.get("sources_off") or [])   # sources switched off: their old prices are ignored
        self.skip_days = int(s.get("skip_if_other_checked_days", 2))
        self.disagree = float(s.get("disagree_pct", 25))
        self.fresh_days = int(s.get("price_fresh_days", 45))
        self.p9_guess = float(s.get("psa9_share_of_psa10_guess", 0.35))
        self.outlier_pct = float(s.get("outlier_pct", 60))
        self.split_pct = float(s.get("split_pct", 45))
        self.max_ratio = float(s.get("max_psa10_to_psa9_ratio", 30))
        self.weights = {**BASE_WEIGHT, **(s.get("weights") or {})}
        self.bounds = {g: tuple(v) for g, v in {**RAW_BOUNDS, **(s.get("raw_bounds") or {})}.items()}
        try:
            self.data = json.load(open(PATH, encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.slabs = self.data.setdefault("slabs", {})
        self.trust = self.data.setdefault("trust", {})

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
                              "which source last checked each slab, when, and the PSA 10 / 9 prices it saw; 'trust' = how often "
                              "each source agreed with the others [agreed, checked]. Not loaded by the site.")
        self.data["cap"] = None if self.cap == float("inf") else self.cap
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
        """Days since any eBay-sales source (PokemonPriceTracker, pokemon-api.com) looked at this slab (9999 = never)."""
        return min((days_since(self.last_checked(key, s)) for s in SALES_SOURCES if self.last_checked(key, s)), default=9999)

    def other_sales_checked_recently(self, key, me):
        """True if another eBay-sales bot looked at this slab in the last `skip_if_other_checked_days` days."""
        for s in SALES_SOURCES:
            if s != me and days_since(self.last_checked(key, s)) < self.skip_days:
                return True
        return False

    def best_prices(self, key, extra=None, raw=None):
        """The believable PSA 10 / PSA 9 price -> (p10, p9), from consensus() over every source's last check
        (in the last `price_fresh_days` days). `extra` = [(date, p10, p9)] from a bot's own memory.
        Before, this took whichever price was newest, so one junk price (e.g. a $96,000 estimate for a
        $1,600 slab) could push a slab over the cap and it would never be looked at again."""
        rows = []
        for r in list(extra or [])[-3:]:
            if r and len(r) >= 3 and days_since(r[0]) <= self.fresh_days:
                rows += [{"src": "own", "g": "p10", "p": r[1], "d": r[0]}, {"src": "own", "g": "p9", "p": r[2], "d": r[0]}]
        c = self.consensus(key, raw=raw, extra=rows)
        return (c["p10"] or {}).get("p"), (c["p9"] or {}).get("p")

    # ---------------------------------------------------------- which price to believe
    def _trust(self, src, g):
        a, t = (self.trust.get(src) or {}).get(g) or [0, 0]
        return (a + 1.0) / (t + 2.0)                     # 0.5 until it has a record

    def _learn(self, src, g, agreed):
        e = self.trust.setdefault(src, {}).setdefault(g, [0, 0])
        e[0] += 1 if agreed else 0
        e[1] += 1
        if e[1] > 400:                                   # keep it recent: old results count half
            e[0], e[1] = round(e[0] / 2, 1), round(e[1] / 2, 1)

    def _weight(self, c):
        w = self.weights.get(c["src"], 0.5)
        n = c.get("n")
        w *= 0.7 if n is None else 0.55 if n <= 1 else min(1.0, 0.8 + 0.03 * n)
        age = days_since(c["d"]) if c.get("d") else 30
        w *= 1.0 if age <= 7 else 0.8 if age <= 21 else 0.6
        if c["src"] in ALL_SOURCES:
            w *= 0.5 + self._trust(c["src"], c["g"])
        return w

    @staticmethod
    def _wmedian(cands):
        """Weighted median, in log space (prices are compared as ratios)."""
        rows = sorted((math.log(c["p"]), c["w"]) for c in cands)
        half, acc = sum(w for _, w in rows) / 2, 0.0
        for lp, w in rows:
            acc += w
            if acc >= half:
                return math.exp(lp)
        return math.exp(rows[-1][0])

    def _one_grade(self, cands, raw, g):
        """-> (result dict or None, dropped [[source, price, reason]])"""
        dropped, ok = [], []
        lo_x, hi_x, slack = self.bounds[g]
        for c in cands:
            if raw and raw > 0:
                if c["p"] < lo_x * raw:
                    dropped.append([c["src"], c["p"], f"under {lo_x:g}x the raw NM ${raw:,.0f}: wrong card or junk sale"])
                    continue
                if c["p"] > hi_x * raw + slack:
                    dropped.append([c["src"], c["p"], f"over {hi_x:g}x the raw NM ${raw:,.0f}: wrong card or junk sale"])
                    continue
            c["w"] = self._weight(c)
            ok.append(c)
        if not ok:
            return None, dropped
        mid = self._wmedian(ok)
        if len(ok) >= 3:
            lim = math.log(1 + self.outlier_pct / 100)
            keep = [c for c in ok if abs(math.log(c["p"] / mid)) <= lim]
            for c in ok:
                if c not in keep:
                    dropped.append([c["src"], c["p"], f"over {self.outlier_pct:.0f}% away from the other sources"])
            ok = keep or ok
            mid = self._wmedian(ok)
        agree_lim = math.log(1 + self.disagree / 100)
        agree = [c for c in ok if abs(math.log(c["p"] / mid)) <= agree_lim]
        srcs = sorted({c["src"] for c in agree})
        sales = [c for c in agree if c["src"] in SALES_SOURCES]
        if len(srcs) >= 2:
            conf = "high"
            if len(ok) == 2 and len(agree) < 2:
                conf = "low"
        elif sales and (sales[0].get("n") is None or sales[0]["n"] >= 2):
            conf = "med"
        elif agree and all(c["src"] in ESTIMATES for c in agree):
            conf = "est"                                 # an estimate that passed the checks, no sales behind it yet
        else:
            conf = "low"
        if len(ok) == 2 and len(agree) < 2:
            gap = abs(math.log(ok[0]["p"] / ok[1]["p"]))
            if gap > math.log(1 + self.split_pct / 100):   # two sources, far apart: can't tell which is right
                best = max(ok, key=lambda c: c["w"])
                mid, srcs, conf = best["p"], [best["src"]], "low"
            else:                                        # a bit apart (normal for slabs): meet in the middle
                srcs, conf = sorted({c["src"] for c in ok}), "med"
        return {"p": round(mid, 2), "conf": conf, "srcs": srcs, "cands": ok}, dropped

    def consensus(self, key, raw=None, extra=None, learn=False):
        """The PSA 10 / PSA 9 price to believe for one slab (see the top of this file).
        raw = the card's raw NM price (for the sanity bounds) or None.
        extra = more prices [{'src', 'g': 'p10'|'p9', 'p', 'n', 'd'}] (your site's values, a bot's own memory).
        -> {'p10': {'p', 'conf', 'srcs'} | None, 'p9': ... | None, 'dropped': [[source, grade, price, why]],
            'flags': ['inverted' | 'gap' | 'split', ...]}"""
        cands = {"p10": [], "p9": []}
        for s, r in (self.slabs.get(str(key)) or {}).items():
            if s not in ALL_SOURCES or s in self.off or days_since(r.get("d")) > self.fresh_days:
                continue
            for g, n in (("p10", "n10"), ("p9", "n9")):
                if isinstance(r.get(g), (int, float)) and r[g] > 0:
                    cands[g].append({"src": s, "g": g, "p": float(r[g]), "n": r.get(n), "d": r.get("d")})
        for x in extra or []:
            if x.get("g") in cands and isinstance(x.get("p"), (int, float)) and x["p"] > 0:
                cands[x["g"]].append({"src": x.get("src") or "own", "g": x["g"], "p": float(x["p"]), "n": x.get("n"), "d": x.get("d")})
        out, flags, dropped = {}, [], []
        for g in ("p10", "p9"):
            res, dr = self._one_grade(cands[g], raw, g)
            out[g] = res
            dropped += [[s, g, p, why] for s, p, why in dr]
            if res and res["conf"] == "low" and len({c["src"] for c in res["cands"]}) >= 2:
                flags.append("split")
        a, b = out["p10"], out["p9"]
        if a and b and a["p"] < b["p"]:
            # PSA 10 under PSA 9: redo the less certain grade without the prices that cause it
            fix_g, keep_g = ("p9", "p10") if CONF_RANK[b["conf"]] < CONF_RANK[a["conf"]] else ("p10", "p9")
            if CONF_RANK[a["conf"]] == CONF_RANK[b["conf"]]:
                fix_g = None
            if fix_g:
                ref = out[keep_g]["p"]
                ok = [c for c in cands[fix_g] if (c["p"] <= ref if fix_g == "p9" else c["p"] >= ref)]
                res, _ = self._one_grade([dict(c) for c in ok], raw, fix_g)
                for c in cands[fix_g]:
                    if c not in ok:
                        dropped.append([c["src"], fix_g, c["p"], "PSA 10 can't be worth less than PSA 9"])
                out[fix_g] = res
                if res:
                    res["conf"] = "low" if res["conf"] == "low" else "med"
            else:
                flags.append("inverted")
                a["conf"] = b["conf"] = "low"
        a, b = out["p10"], out["p9"]
        if a and b and a["p"] > self.max_ratio * b["p"]:
            flags.append("gap")
            (a if CONF_RANK[a["conf"]] <= CONF_RANK[b["conf"]] else b)["conf"] = "low"
        if learn:
            for g in ("p10", "p9"):
                r = out[g]
                if r and r["conf"] == "high":
                    lim = math.log(1.3)
                    for c in cands[g]:
                        if c["src"] in ALL_SOURCES:
                            self._learn(c["src"], g, abs(math.log(c["p"] / r["p"])) <= lim)
        for g in ("p10", "p9"):
            if out[g]:
                out[g].pop("cands", None)
        return {**out, "dropped": dropped, "flags": flags}

    def over_cap(self, key, extra=None, raw=None):
        """True when this slab is over your cap in both PSA 10 and PSA 9 (so no limited requests go to it).
        If only the PSA 10 price is known, the PSA 9 is guessed at about a third of it."""
        p10, p9 = self.best_prices(key, extra, raw)
        if p9:
            return p9 > self.cap and (not p10 or p10 > self.cap)
        if p10:
            return p10 * self.p9_guess > self.cap
        return False

    def known_affordable(self, key, extra=None, raw=None):
        p10, p9 = self.best_prices(key, extra, raw)
        return bool((p10 and p10 <= self.cap) or (p9 and p9 <= self.cap))

    def conflict(self, key, grade="p10", days=7):
        """If two or more eBay-sales sources (PokemonPriceTracker, pokemon-api.com) priced this grade in
        the last `days` days and disagree by more than disagree_pct, return [(source name, price), ...]; else None.
        (CardPrice's graded price is an estimate, not a recent-sales median, so it isn't used to flag.)"""
        seen = [(NAMES[s], r[grade]) for s, r in (self.slabs.get(str(key)) or {}).items()
                if s in SALES_SOURCES and r.get(grade) and days_since(r.get("d")) <= days]
        if len(seen) < 2:
            return None
        lo, hi = min(p for _, p in seen), max(p for _, p in seen)
        return sorted(seen, key=lambda x: x[1]) if lo > 0 and (hi / lo - 1) * 100 > self.disagree else None
