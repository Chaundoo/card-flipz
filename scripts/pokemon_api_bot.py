#!/usr/bin/env python3
"""pokemon-api.com bot: a third source of eBay graded prices (PSA 10 / 9 median and how many sold).

Runs every morning right after the CardPrice bot and before the Graded Slabs bot
(.github/workflows/pokemon-api.yml) and writes graded/pokemonapi.json, which scripts/graded_heat.py reads.
It never changes the site's cards.

The free plan is small, so this bot is picky about where each request goes:
  1. Slabs that are heating up (breaking out / warming up): checked here too, on purpose, as a cross-check of
     PokemonPriceTracker. If the two disagree by more than 25% the Graded Slabs tab flags the slab.
  2. Your priority Pokémon (scripts/graded_config.json "priority"): mid-era cards first (focus_eras), then
     the slabs no sales source has looked at for the longest time.
  3. Slabs CardPrice couldn't price.
  It skips anything over your price cap (max_slab_price) and anything PokemonPriceTracker checked in the last
  couple of days (graded/sources.json keeps track).
  Its prices go through the price check in scripts/slab_sources.py before they're shown: on its own it often
  matches the wrong card (e.g. a Base Set Blastoise PSA 10 at $112), and those prices get thrown out.

Request limits (scripts/graded_config.json):
  pa_max_per_day / pa_max_per_month  hard caps the bot never goes past (it counts its own requests);
                                      the free Basic plan is 100 a day, so the default is 90 a day
  pa_keep_free                        stop when RapidAPI says this many or fewer requests are left
RapidAPI may bill for requests over a plan's quota, so keep both caps under the free plan's limit.

API (pokemon-api.com, sold through RapidAPI; GET with X-RapidAPI-Key and X-RapidAPI-Host headers):
  /cards?search=gengar ex 114        -> cards with prices
  /cards/{id}                        -> one card: prices.ebay.graded.psa["10"] = {median_price, sample_size}
  The bot also tries /pokemon/cards if /cards doesn't exist and remembers whichever path works.

Environment: POKEMON_API_KEY (GitHub secret, your RapidAPI key) · DRY_RUN "true" = report only ·
PA_CARDS = max slabs this run · PA_BASE = API base URL (testing).
"""
import json, os, re, sys, time, datetime
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp
import graded_heat as gh
import cardprice_bot as cpb
import slab_sources

ROOT = rp.ROOT
OUT = os.path.join(ROOT, "graded", "pokemonapi.json")
KEY = (os.environ.get("POKEMON_API_KEY") or "").strip()
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
TODAY = rp.TODAY
UA = "card-flipz-graded-heat/1.0 (+https://github.com/Chaundoo/card-flipz)"
DEFAULTS = {"pa_base": "https://pokemon-tcg-api.p.rapidapi.com", "pa_paths": ["/cards", "/pokemon/cards"],
            "pa_max_per_day": 3, "pa_max_per_month": 95, "pa_keep_free": 5, "pa_pause_seconds": 2.5,
            "pa_recheck_days": 14, "pa_hot_recheck_days": 3, "pa_retry_unmatched_days": 45}


class Stop(Exception):
    pass


class PA:
    def __init__(self, cfg, usage):
        self.base = (os.environ.get("PA_BASE") or cfg["pa_base"]).rstrip("/")
        self.host = urllib.parse.urlparse(self.base).netloc
        self.pause = float(cfg["pa_pause_seconds"])
        self.usage = usage
        self.max_day, self.max_month, self.keep = int(cfg["pa_max_per_day"]), int(cfg["pa_max_per_month"]), int(cfg["pa_keep_free"])
        self.used, self.last, self.left, self.limit, self.keys = 0, 0.0, None, None, set()

    def room(self):
        """How many more requests are allowed right now (our own caps, then what RapidAPI says is left)."""
        n = min(self.max_day - self.usage["days"].get(TODAY, 0), self.max_month - self.usage["used"])
        if self.left is not None:
            n = min(n, self.left - self.keep)
        return max(0, n)

    def get(self, path, params=None):
        if self.room() <= 0:
            raise Stop("reached today's request cap")
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        wait = self.pause - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        req = urllib.request.Request(url, headers={"X-RapidAPI-Key": KEY, "X-RapidAPI-Host": self.host,
                                                   "Accept": "application/json", "User-Agent": UA})
        # every attempt counts against the quota, so no automatic retries here
        self.used += 1
        self.usage["used"] += 1
        self.usage["days"][TODAY] = self.usage["days"].get(TODAY, 0) + 1
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._limits(r.headers)
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            self._limits(e.headers)
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 404:
                return None
            if e.code in (401, 403):
                raise Stop(f"RapidAPI refused the key (HTTP {e.code}). Check the POKEMON_API_KEY secret and that you're "
                           f"subscribed to the free plan of pokemon-api.com's API. {body}")
            if e.code == 429:
                raise Stop(f"RapidAPI says the plan's limit is reached (HTTP 429). {body}")
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"network error: {e}")

    def _limits(self, h):
        if not h:
            return
        for k, attr in (("x-ratelimit-requests-remaining", "left"), ("x-ratelimit-requests-limit", "limit")):
            v = h.get(k)
            if v is not None:
                try:
                    setattr(self, attr, int(float(v)))
                except ValueError:
                    pass


# ---------------------------------------------------------------- reading responses
def card_list(j):
    """Any reasonable search response -> [card dicts]."""
    if isinstance(j, list):
        return [c for c in j if isinstance(c, dict)]
    if isinstance(j, dict):
        if j.get("id") and j.get("name"):
            return [j]
        for k in ("data", "cards", "results", "items"):
            v = j.get(k)
            if isinstance(v, list):
                return [c for c in v if isinstance(c, dict)]
            if isinstance(v, dict):
                return card_list(v)
    return []


def as_cp(c):
    """pokemon-api.com card -> the field names the CardPrice matcher understands."""
    ep = c.get("episode") if isinstance(c.get("episode"), dict) else {}
    return {"name": c.get("name") or "", "number": c.get("card_number") or c.get("number") or "",
            "set_name": ep.get("name") or c.get("set_name") or "", "_card": c}


def ebay_graded(c):
    """-> {'psa10': (median, sold), 'psa9': (median, sold)} from prices.ebay.graded.psa."""
    prices = c.get("prices") if isinstance(c.get("prices"), dict) else {}
    ebay = prices.get("ebay") if isinstance(prices.get("ebay"), dict) else {}
    psa = ((ebay.get("graded") or {}).get("psa")) if isinstance(ebay.get("graded"), dict) else None
    out = {}
    if not isinstance(psa, dict):
        return out
    for g in ("10", "9"):
        o = psa.get(g) or psa.get("psa" + g)
        if isinstance(o, dict):
            p = gh.fnum(o.get("median_price") or o.get("median") or o.get("price"))
            n = o.get("sample_size") if isinstance(o.get("sample_size"), int) else o.get("count")
            if p:
                out["psa" + g] = (round(p, 2), n if isinstance(n, int) else None)
        elif gh.fnum(o):
            out["psa" + g] = (round(gh.fnum(o), 2), None)
    return out


def note(keys, obj, prefix="", depth=0):
    if depth > 4 or not isinstance(obj, dict):
        return
    for k, v in obj.items():
        keys.add(prefix + k)
        if isinstance(v, dict):
            note(keys, v, prefix + k + ".", depth + 1)


# ---------------------------------------------------------------- what to check
def pick(pool, gstate, src, cards, cpcards, cfg, names, limit):
    hot, prio, unpriced = [], [], []
    gcards = gstate.get("cards") or {}
    for key, e in pool.items():
        mine = cards.get(key) or {}
        if mine.get("none") and gh.days_between(mine.get("checked", "")) < cfg["pa_retry_unmatched_days"]:
            continue
        own = [(s[0], s[1], s[3]) for s in (gcards.get(key) or {}).get("snaps", [])]
        if src.over_cap(key, own, raw=e.get("nm")):
            continue
        sc = (gcards.get(key) or {}).get("score") or {}
        age = gh.days_between(mine.get("checked", ""))
        is_hot = sc.get("stage") in ("breaking", "warming") or sc.get("heat", 0) >= 60
        if is_hot and src.known_affordable(key, own, raw=e.get("nm")):
            if age >= cfg["pa_hot_recheck_days"]:
                hot.append((-sc.get("heat", 0), key))           # cross-check on purpose, even if PPT just looked
            continue
        if age < cfg["pa_recheck_days"] or src.other_sales_checked_recently(key, "pa"):
            continue
        cpc = cpcards.get(key) or {}
        mid = 0 if gh.focus(e, cfg) else 1                      # mid-era (focus_eras) first
        if names.tier(names.species(e["name"])) == 1:
            prio.append((mid, -src.sales_age(key), -gh.prior(e, cfg, names), key))
        elif cpc.get("none") or not cpc.get("graded"):
            if (e.get("nm") or 0) <= src.cap * 0.3:              # raw already pricey: the slabs are likely over the cap
                unpriced.append((mid, -src.sales_age(key), -gh.prior(e, cfg, names), key))
    hot.sort(); prio.sort(); unpriced.sort()
    out = []
    for lst in ([k for _, k in hot], [x[-1] for x in prio], [x[-1] for x in unpriced]):
        for k in lst:
            if len(out) >= limit:
                return out
            if k not in out:
                out.append(k)
    return out


# ---------------------------------------------------------------- main
def main():
    cfg = {**DEFAULTS, **(gh.jload(gh.CFG_PATH, {}) or {})}
    names = gh.Names(cfg, gh.jload(gh.DISC_CFG, {}))
    manifest = gh.jload(rp.MANIFEST, {"eras": []})
    gstate = gh.jload(gh.GSTATE, {})
    pool = {str(k): v for k, v in gh.pool_from_site(names, manifest).items()}
    for k, v in (gstate.get("pool") or {}).items():
        pool.setdefault(k, v)
    pool = gh.watch_filter(pool, cfg)          # only the eras you watch (Graded Slabs tab settings)
    data = gh.jload(OUT, {})
    cards = data.setdefault("cards", {})
    usage = data.get("usage") or {}
    month = TODAY[:7]
    if usage.get("month") != month:
        usage = {"month": month, "used": 0, "days": {}}
    usage["days"] = {d: n for d, n in (usage.get("days") or {}).items() if d[:7] == month}
    src = slab_sources.Sources(cfg)
    cpcards = (gh.jload(gh.CPFILE, {}) or {}).get("cards") or {}
    api = PA(cfg, usage)
    t0, problems, stop = time.time(), [], None
    checked = found = priced = 0
    lines = []

    if not KEY:
        stop = "the POKEMON_API_KEY secret isn't set yet, so nothing was looked up (see the README)"
        todo = []
    else:
        # each slab costs 1 request (2 the first time if the search has no prices)
        limit = int(os.environ.get("PA_CARDS") or api.room())
        todo = pick(pool, gstate, src, cards, cpcards, cfg, names, limit)

    paths = [data["api_path"]] if data.get("api_path") else list(cfg["pa_paths"])
    for key in todo:
        if api.room() <= 0:
            break
        e = pool[key]
        c = cards.setdefault(key, {})
        try:
            card = None
            if c.get("paid") and data.get("api_path"):
                card = api.get(f"{data['api_path']}/{urllib.parse.quote(str(c['paid']))}")
                if card is None:                             # id went stale: search again next time
                    c.pop("paid", None)
                    c["checked"] = TODAY
                    checked += 1
                    continue
                card = (card_list(card) or [None])[0]
            else:
                q = re.sub(r"\s+", " ", f"{e['name']} {cpb.gh_num(e['number'])}").strip()
                res = None
                for p in paths:
                    res = api.get(p, {"search": q})
                    if res is not None:
                        if data.get("api_path") != p:
                            data["api_path"] = p
                            paths = [p]
                        break
                if res is None:
                    stop = (f"none of the API paths answered ({', '.join(cfg['pa_paths'])}). pokemon-api.com may have "
                            f"changed its URLs; check pa_base / pa_paths in scripts/graded_config.json")
                    break
                hit = cpb.match(e, [as_cp(x) for x in card_list(res)])
                if not hit:
                    c.update({"none": True, "checked": TODAY})
                    src.record(key, "pa", found=False)
                    checked += 1
                    continue
                card = hit["_card"]
                c.pop("none", None)
                c["paid"] = card.get("id")
                if not ebay_graded(card) and card.get("id") and api.room() > 0:
                    full = api.get(f"{data['api_path']}/{urllib.parse.quote(str(card['id']))}")
                    card = (card_list(full) or [card])[0]
            checked += 1
            if not isinstance(card, dict):
                continue
            found += 1
            note(api.keys, card)
            c["checked"] = TODAY
            g = ebay_graded(card)
            p10, n10 = g.get("psa10", (None, None))
            p9, n9 = g.get("psa9", (None, None))
            src.record(key, "pa", p10, p9, n10, n9)
            if p10 or p9:
                priced += 1
                c["psa10"] = {"p": p10, "n": n10} if p10 else None
                c["psa9"] = {"p": p9, "n": n9} if p9 else None
                c = {k: v for k, v in c.items() if v is not None}
                c["snaps"] = [s for s in c.get("snaps", []) if s[0] != TODAY and gh.days_between(s[0]) <= 400] + [[TODAY, p10, n10, p9, n9]]
                cards[key] = c
                lines.append(f"- {e['name']} ({e['set']} {e['number']}): PSA 10 {('$' + format(p10, ',.0f')) if p10 else '—'}"
                             f"{f' ({n10} sold)' if n10 else ''} · PSA 9 {('$' + format(p9, ',.0f')) if p9 else '—'}{f' ({n9} sold)' if n9 else ''}")
        except Stop as x:
            stop = str(x)
            break
        except Exception as x:
            problems.append(f"{e['name']} ({e['set']}): {str(x)[:140]}")
            if len(problems) >= 3 and not found:
                stop = "3 lookups in a row failed, so the API may be down; stopped to be safe"
                break

    data["usage"] = usage
    status = "FAILED" if stop and "refused" in stop else "PARTIAL" if (stop and "cap" not in stop) or (problems and not found) else "OK"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    L = [f"## {icon} pokemon-api.com {status} — {TODAY}" + (" (test run, nothing saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped: {stop}.", ""]
    L += [f"- Slabs looked up: **{checked}** · found {found} · with eBay graded prices {priced}",
          f"- Requests: {api.used} today ({usage['days'].get(TODAY, 0)} of {cfg['pa_max_per_day']} allowed per day, "
          f"{usage['used']} of {cfg['pa_max_per_month']} this month)" +
          (f" · RapidAPI says {api.left} of {api.limit} left" if api.left is not None else ""),
          f"- Price cap: ${src.cap:,.0f} per slab (max_slab_price) · took {secs}s"]
    if lines:
        L += [""] + lines[:20]
    if api.keys:
        L += ["", "<details><summary>Fields the API returned (for tuning)</summary>", "", "`" + ", ".join(sorted(api.keys)[:120]) + "`", "</details>"]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {p}" for p in problems[:10]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        if not KEY:
            head = "pokemon-api.com isn't set up yet: add the POKEMON_API_KEY secret (see the README)."
        elif status == "FAILED":
            head = f"pokemon-api.com failed: {stop}."
        else:
            head = f"pokemon-api.com looked up {checked} slab{'s' if checked != 1 else ''}: {priced} with eBay graded prices."
        botlog.summary(("Test run (nothing saved): " if DRY else "") + head,
                       "warn" if not KEY else {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"),
                       [f"{api.used} requests today · {usage['used']} this month"])
    except Exception as x:
        print(f"(admin log summary skipped: {x})")
    saved = not DRY and api.used > 0
    if saved:
        data.update({"about": "Written by scripts/pokemon_api_bot.py from pokemon-api.com (RapidAPI). Read by scripts/graded_heat.py.",
                     "generated": TODAY})
        gh.jdump(OUT, data, separators=(",", ":"), sort_keys=True)
        src.save()
    open(rp.REPORT, "w", encoding="utf-8").write(report)
    print(report)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"status={status}\nsaved={'true' if saved else 'false'}\nchecked={checked}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(report)
    return 1 if status == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
