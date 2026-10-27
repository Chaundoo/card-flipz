#!/usr/bin/env python3
"""tcgapi.dev bot: a raw-price radar for the slab pool (FREE plan only, never more than 100 requests a day).

What it does and why
  tcgapi.dev's free plan has no eBay or graded data (that's Pro, $49.99/mo), so this bot does NOT price slabs
  and never writes graded/sources.json. It does the one job none of the other slab bots do: it watches the
  RAW (ungraded) TCGplayer market price of the cards in the slab pool, with its 24-hour, 7-day and 30-day
  change. Raw prices often start climbing before the slabs do, so:
    * a card whose raw price is rising gets checked with real eBay sales by the Graded Slabs bot first
      (graded_heat.py moves it to the front of the queue), and
    * it adds a "Raw TCGplayer price up X%" point to the slab's heat score (one bonus shared with CardPrice's
      movers, so the two never double-count).
  It also gives the price check a fresh raw price for cards that aren't on your site.

Which cards (no overlap with the other bots)
  Slabs that are heating up / need verifying first, then mid-era cards (focus_eras in graded_config.json:
  DP/HGSS, Black & White, XY, Sun & Moon), then the rest by your priority list. It skips slabs over your price
  cap and cards it checked recently (ta_recheck_days; heating-up ones every ta_hot_recheck_days).

The 100-a-day limit (free plan; resets at midnight UTC = 4 or 5 pm Pacific)
  Three separate guards, any one of them stops the bot:
    1. Its own count, saved in graded/tcgapi.json per UTC day (manual runs and test runs count too), never past
       ta_max_per_day (90). The setting can't be raised past FREE_PLAN_DAILY - ta_keep_free (90) by mistake.
    2. tcgapi.dev's own counter (X-RateLimit-Remaining header / rate_limit.daily_remaining): it stops when the
       server says ta_keep_free (10) or fewer are left, even if its own count got lost.
    3. A "rate limit" (HTTP 429) answer stops it at once; it never retries one.
  Each request is counted before it's sent, retries included.

Requests: a card it hasn't seen costs 2 (look up the card by its TCGplayer id, once, then its prices);
after that 1 per check. About 45-90 cards a day.

API (https://tcgapi.dev, header X-API-Key: tcg_live_...):
  GET /v1/cards/tcgplayer/{tcgplayerId}  -> {"data": {"id", "name", "prices": [{printing, market_price, price_change_24h}]}}
  GET /v1/cards/{id}/prices               -> {"data": [{printing, market_price, price_change_24h / _7d / _30d}]}
Environment: TCGAPI_KEY (GitHub secret, free key from tcgapi.dev) · DRY_RUN "true" = report only (requests
still count) · TA_CARDS = max cards this run · TA_BASE = API base URL (testing).
"""
import json, os, re, sys, time, math, datetime
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp
import graded_heat as gh
import slab_sources

ROOT = rp.ROOT
OUT = os.path.join(ROOT, "graded", "tcgapi.json")
KEY = (os.environ.get("TCGAPI_KEY") or "").strip()
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
TODAY = rp.TODAY                                         # Pacific date, for "checked" (same as the other bots)
UA = "card-flipz-raw-radar/2.0 (+https://github.com/Chaundoo/card-flipz)"
FREE_PLAN_DAILY = 100                                    # tcgapi.dev free plan: 100 requests per UTC day
DEFAULTS = {"ta_base": "https://api.tcgapi.dev/v1", "ta_max_per_day": 90, "ta_keep_free": 10, "ta_max_per_run": 90,
            "ta_pause_seconds": 1.0, "ta_recheck_days": 10, "ta_hot_recheck_days": 3, "ta_retry_unmatched_days": 60,
            "ta_riser_7d": 10, "ta_riser_30d": 15, "ta_min_market": 3, "min_raw_nm": 4}


def utc_day():
    """The quota day: tcgapi.dev resets at midnight UTC, not Pacific midnight."""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


class Stop(Exception):
    pass


class TA:
    def __init__(self, cfg, usage):
        self.base = (os.environ.get("TA_BASE") or cfg["ta_base"]).rstrip("/")
        self.pause = float(cfg["ta_pause_seconds"])
        self.keep_free = max(5, int(cfg["ta_keep_free"]))
        # never above what the free plan allows minus the safety margin, whatever the config says
        self.max_day = min(int(cfg["ta_max_per_day"]), FREE_PLAN_DAILY - self.keep_free)
        self.usage, self.day = usage, utc_day()
        self.used, self.last, self.server_left, self.server_limit = 0, 0.0, None, None

    def count_today(self):
        return int(self.usage["days"].get(self.day, 0))

    def room(self):
        n = self.max_day - self.count_today()
        if self.server_left is not None:
            n = min(n, self.server_left - self.keep_free)
        return max(0, n)

    def get(self, path):
        for attempt in range(2):
            if self.room() <= 0:
                raise Stop("reached today's request cap" if self.server_left is None or self.server_left > self.keep_free
                           else f"tcgapi.dev says only {self.server_left} requests are left today")
            wait = self.pause - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.used += 1                                # counted before sending, so a crash can't lose it
            self.usage["days"][self.day] = self.count_today() + 1
            req = urllib.request.Request(self.base + path, headers={"X-API-Key": KEY, "Accept": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._limits(r.headers)
                    j = json.loads(r.read().decode("utf-8", "replace"))
                    self._limits_body(j)
                    return j
            except urllib.error.HTTPError as e:
                self._limits(e.headers)
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 404:
                    return None
                if e.code == 401:
                    raise Stop(f"tcgapi.dev refused the key (HTTP 401). Check the TCGAPI_KEY secret: it should be a free "
                               f"tcgapi.dev key starting with tcg_live_. {body[:120]}")
                if e.code == 403:
                    raise Stop(f"tcgapi.dev says this needs a paid plan (HTTP 403); the bot only uses free endpoints, "
                               f"so tell Claude what it said. {body[:120]}")
                if e.code == 429:
                    self.server_left = 0
                    raise Stop("tcgapi.dev's daily limit is used up (HTTP 429), stopped without retrying")
                if e.code >= 500 and attempt == 0:
                    time.sleep(8)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body[:140]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 0:
                    time.sleep(8)
                    continue
                raise RuntimeError(f"network error: {e}")
        raise RuntimeError("gave up after retries")

    def _limits(self, h):
        h = h or {}
        for k, attr in (("X-RateLimit-Remaining", "server_left"), ("X-RateLimit-Limit", "server_limit")):
            v = h.get(k)
            if v is not None:
                try:
                    setattr(self, attr, int(float(v)))
                except ValueError:
                    pass

    def _limits_body(self, j):
        rl = j.get("rate_limit") if isinstance(j, dict) else None
        if isinstance(rl, dict) and isinstance(rl.get("daily_remaining"), (int, float)):
            self.server_left = int(rl["daily_remaining"])


# ---------------------------------------------------------------- reading the answers
def unwrap(j):
    return j.get("data") if isinstance(j, dict) and "data" in j else j


def price_rows(j):
    """Any prices answer -> [{'pr', 'm', 'ch24', 'ch7', 'ch30'}] (one per printing)."""
    d = unwrap(j)
    if isinstance(d, dict) and isinstance(d.get("prices"), list):
        d = d["prices"]
    rows = d if isinstance(d, list) else [d] if isinstance(d, dict) else []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = gh.fnum(r.get("market_price"))
        if not m:
            continue
        def pct(k):
            v = r.get(k)
            return round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append({"pr": str(r.get("printing") or "Normal"), "m": round(m, 2), "ch24": pct("price_change_24h"),
                    "ch7": pct("price_change_7d"), "ch30": pct("price_change_30d")})
    return out


def choose(rows, e):
    """The printing that is this card: by the pool's variant (holo / reverse / non-holo), then the one whose
    price is closest to the card's raw NM price if it has one."""
    if not rows:
        return None
    kind = gh.variant_kind(e.get("variant"))
    def fits(pr):
        p = pr.lower()
        if kind == "reverse":
            return "reverse" in p or ("foil" in p and "holo" not in p)
        if kind == "nonholo":
            return p in ("normal", "unlimited", "1st edition") or ("normal" in p and "foil" not in p)
        return "holo" in p or "foil" in p
    pool = [r for r in rows if fits(r["pr"])] or rows
    nm = e.get("nm")
    if nm:
        return min(pool, key=lambda r: abs(math.log(r["m"] / nm)))
    return max(pool, key=lambda r: r["m"])


def own_change(snaps, days):
    """% change vs the bot's own check closest to `days` ago (within a week), or None."""
    if not snaps:
        return None
    now = snaps[-1][1]
    best = None
    for d, m in snaps[:-1]:
        gap = abs(gh.days_between(d) - days)
        if gap <= 7 and (best is None or gap < best[0]):
            best = (gap, m)
    return round((now / best[1] - 1) * 100, 1) if best and best[1] else None


# ---------------------------------------------------------------- what to check
def pick(pool, gstate, src, cards, cfg, names):
    gcards = gstate.get("cards") or {}
    todo = []
    for key, e in pool.items():
        mine = cards.get(key) or {}
        age = gh.days_between(mine.get("checked", ""))
        if mine.get("none"):
            if age < cfg["ta_retry_unmatched_days"]:
                continue
        sc = (gcards.get(key) or {}).get("score") or {}
        hot = sc.get("stage") in ("breaking", "warming", "verify", "conflict") or sc.get("heat", 0) >= 60 \
            or gh.raw_riser(mine, cfg)
        if not hot and (e.get("nm") or 0) < cfg["min_raw_nm"]:
            continue
        own = [(s[0], s[1], s[3] if len(s) > 3 else None) for s in (gcards.get(key) or {}).get("snaps", [])]
        if src.over_cap(key, own, raw=e.get("nm")):
            continue                                     # the slabs are over your cap: the raw trend won't matter
        if age < (cfg["ta_hot_recheck_days"] if hot else cfg["ta_recheck_days"]):
            continue
        todo.append((0 if hot else 1, 0 if gh.focus(e, cfg) else 1, -min(age, 90), -gh.prior(e, cfg, names), key))
    todo.sort()
    return [x[-1] for x in todo]


# ---------------------------------------------------------------- main
def main():
    cfg = {**DEFAULTS, **(gh.jload(gh.CFG_PATH, {}) or {})}
    names = gh.Names(cfg, gh.jload(gh.DISC_CFG, {}))
    manifest = gh.jload(rp.MANIFEST, {"eras": []})
    gstate = gh.jload(gh.GSTATE, {})
    pool = {str(k): v for k, v in gh.pool_from_site(names, manifest).items()}
    for k, v in (gstate.get("pool") or {}).items():
        pool.setdefault(k, v)
    data = gh.jload(OUT, {})
    if "usage" in data and not isinstance((data.get("usage") or {}).get("days"), dict):
        data["usage"] = {}
    if data.get("api") != "tcgapi.dev":                  # old tcgapi.net file (that API never existed): start fresh
        data = {"usage": data.get("usage") or {}}
    cards = data.setdefault("cards", {})
    usage = data.get("usage") or {}
    usage["days"] = {d: n for d, n in (usage.get("days") or {}).items() if gh.days_between(d) <= 31}
    src = slab_sources.Sources(cfg)                      # read only: this bot never writes graded/sources.json
    api = TA(cfg, usage)
    t0, problems, stop = time.time(), [], None
    checked = resolved = unmatched = 0
    errors_row = 0
    risers = []

    if not KEY:
        stop = "the TCGAPI_KEY secret isn't set yet, so nothing was looked up (see the README)"
        todo = []
    else:
        todo = pick(pool, gstate, src, cards, cfg, names)
    limit = int(os.environ.get("TA_CARDS") or cfg["ta_max_per_run"])

    done = []
    for key in todo:
        if checked >= limit or api.room() <= 0:
            break
        e = pool[key]
        done.append(key)
        c = cards.get(key) or {}
        try:
            first = None
            if not c.get("id"):
                j = api.get(f"/cards/tcgplayer/{int(e['tcgId'])}")
                d = unwrap(j) if j else None
                if not isinstance(d, dict) or not d.get("id"):
                    cards[key] = {"none": 1, "checked": TODAY}
                    unmatched += 1
                    errors_row = 0
                    continue
                c = {**c, "id": d["id"]}
                c.pop("none", None)
                resolved += 1
                first = price_rows(j)                    # market + 24h change, in case there's no room for more
            rows = None
            if api.room() > 0:
                j = api.get(f"/cards/{urllib.parse.quote(str(c['id']))}/prices")
                rows = price_rows(j) if j else []
            rows = rows or first or []
            errors_row = 0
        except Stop as x:
            if c.get("id"):
                cards[key] = {**cards.get(key, {}), **c}
            stop = str(x)
            break
        except Exception as x:
            problems.append(f"{e['name']} ({e['set']}): {str(x)[:140]}")
            errors_row += 1
            if errors_row >= 4:
                stop = "4 lookups in a row failed, so tcgapi.dev may be down; stopped to be safe"
                break
            continue
        checked += 1
        c["checked"] = TODAY
        r = choose(rows, e)
        if not r:
            c["noprice"] = 1
            cards[key] = c
            continue
        c.pop("noprice", None)
        c.update({"pr": r["pr"], "m": r["m"], "ch24": r["ch24"], "ch7": r["ch7"], "ch30": r["ch30"]})
        c["snaps"] = [s for s in c.get("snaps", []) if s[0] != TODAY and gh.days_between(s[0]) <= 400] + [[TODAY, r["m"]]]
        if c["ch30"] is None:
            c["ch30"] = own_change(c["snaps"], 30)
        if c["ch7"] is None:
            c["ch7"] = own_change(c["snaps"], 7)
        c = {k: v for k, v in c.items() if v is not None}
        cards[key] = c
        if gh.raw_riser(c, cfg):
            risers.append((max(c.get("ch30") or 0, c.get("ch7") or 0), e, c))

    data["usage"] = usage
    status = "FAILED" if stop and ("refused" in stop or "paid plan" in stop) else \
             "PARTIAL" if (stop and "cap" not in stop and "left today" not in stop and "used up" not in stop) or (problems and not checked) else "OK"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    focus_n = sum(1 for k in done if gh.focus(pool[k], cfg) and (cards.get(k) or {}).get("checked") == TODAY and not (cards.get(k) or {}).get("none"))
    L = [f"## {icon} tcgapi.dev raw radar {status} — {TODAY}" + (" (test run, only the request count saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped: {stop}.", ""]
    L += [f"- Cards checked: **{checked}** ({min(focus_n, checked)} mid-era) · new cards matched {resolved}, not on tcgapi.dev {unmatched}",
          f"- Requests: {api.used} this run · **{api.count_today()} of {api.max_day}** allowed today (UTC day {api.day}; free plan "
          f"{FREE_PLAN_DAILY}/day, {api.keep_free} always kept spare)"
          + (f" · tcgapi.dev says {api.server_left} left" if api.server_left is not None else ""),
          f"- Raw risers (30-day ≥ {cfg['ta_riser_30d']}% or 7-day ≥ {cfg['ta_riser_7d']}%): **{len(risers)}**, "
          "queued for a real eBay sales check by the Graded Slabs bot",
          f"- Due but not reached today: {max(0, len(todo) - len(done))} · took {secs // 60}m {secs % 60}s"]
    if risers:
        risers.sort(key=lambda x: -x[0])
        L += ["", "**Raw price rising:**"] + [
            f"- {e['name']} ({e['set']} {e['number']}): raw ${c['m']:,.2f}"
            + (f" · 7d {c['ch7']:+.0f}%" if c.get("ch7") is not None else "")
            + (f" · 30d {c['ch30']:+.0f}%" if c.get("ch30") is not None else "") for _, e, c in risers[:15]]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {p}" for p in problems[:10]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        if not KEY:
            head = "tcgapi.dev isn't set up yet: add a free tcgapi.dev key as the TCGAPI_KEY secret (see the README)."
        elif status == "FAILED":
            head = f"tcgapi.dev failed: {stop}."
        else:
            head = f"tcgapi.dev checked {checked} raw prices: {len(risers)} rising."
        botlog.summary(("Test run: " if DRY else "") + head,
                       "warn" if not KEY else {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"),
                       [f"{api.count_today()}/{api.max_day} requests today", f"{len(risers)} raw risers"])
    except Exception as x:
        print(f"(admin log summary skipped: {x})")
    saved = api.used > 0
    if saved:
        if DRY:                                          # a test run saves only the request count
            old = gh.jload(OUT, {})
            old["usage"] = usage
            gh.jdump(OUT, old, separators=(",", ":"), sort_keys=True)
        else:
            data.update({"api": "tcgapi.dev", "generated": TODAY,
                         "about": "Written by scripts/tcgapi_bot.py: raw (ungraded) TCGplayer market price and its 24h / 7d / 30d "
                                  "change from tcgapi.dev's free plan, per slab-pool card (key = TCGplayer id). Read by "
                                  "scripts/graded_heat.py. usage.days = requests per UTC day (free plan: 100)."})
            gh.jdump(OUT, data, separators=(",", ":"), sort_keys=True)
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
