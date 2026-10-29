#!/usr/bin/env python3
"""Graded Heat: watch PSA 10 / PSA 9 slabs of popular Pokémon and flag the ones that look ready to move.

Runs in GitHub Actions every morning (.github/workflows/graded-heat.yml). Nothing on the site's card list
is changed; it only writes graded/heat.json (what the site's "Graded Slabs" tab shows) and
graded/state.json (its own memory: price snapshots, which cards it checked and when).

Where the cards come from (the "pool")
  1. Cards already on the site and in the Flip Finder list, if they are a popular Pokémon (the "popular"
     list in scripts/discovery_config.json), not stamped, not a trainer.  Mostly 1999-2020 cards.
  2. A small daily scan (Mon-Thu only, a few spare TCG Price Lookup requests) of eras the site has few
     cards from: Sword & Shield / Scarlet & Violet / Mega Evolution chase cards, plus more WOTC and EX.

Where the graded prices come from
  PokemonPriceTracker's free API (PPT_API_KEY secret): PSA 10 / 9 / 8 eBay sold prices, how many sold, and
  price history.  About 30 cards a day on the free plan, so the full pool is covered every few weeks, and
  cards that are heating up are re-checked every 3 days.  Every check is saved as a snapshot, so the bot
  builds its own price history even if the API's history is short.

Which price is shown: no single source is trusted. Every source's PSA 10 / 9 (PokemonPriceTracker,
pokemon-api.com, CardPrice and the values saved on your site's cards) goes through the price check in
scripts/slab_sources.py (consensus): junk vs the raw price is thrown out, the rest are combined, PSA 10 must
beat PSA 9, and each slab gets a confidence. A slab whose price can't be confirmed shows "Verify price"
instead of "Breaking out" / "Warming up", and its heat is capped.

How a slab is scored ("heat", 0-100)
  - how popular the Pokémon is (tier 1 like Charizard / Gengar / Umbreon count most)
  - era: mid-era (2007-2019: DP/HGSS, Black & White, XY, Sun & Moon) scores highest and is checked first
    ("focus_eras" in scripts/graded_config.json), then vintage, then modern
  - the raw (ungraded) price rising (CardPrice movers): raw often moves first
  - PSA 10 price change over ~4 and ~12 weeks: an early rise (+5% to +60%) scores best; a card that
    already doubled is marked "Already spiked" (you'd be chasing it)
  - more sales than before (volume often rises before price does)
  - the same Pokémon heating up on its other cards (the rest often follow)
  - a new set / product featuring the Pokémon soon (the "catalysts" list in scripts/graded_config.json)
  - thin sales (under 2 sold) count against it; a low PSA 10 population / gem rate counts for it

Settings: scripts/graded_config.json.  Environment (all optional):
  PPT_API_KEY     PokemonPriceTracker API key (GitHub secret)                     needed for prices
  TCGPL_API_KEY   same key as the price update, only for the small pool scan
  DRY_RUN         "true" = check and report, save nothing
  HEAT_CARDS      max cards to check this run (blank = from the credit budget)
  PPT_BASE        API base URL (testing only)
  NO_POOL_SCAN    "true" = skip the TCG Price Lookup scan
  FORCE_POOL_SCAN "true" = run the scan even Friday-Sunday (manual runs)
"""
import json, os, re, sys, time, math, datetime, statistics
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp     # TCG Price Lookup client + price helpers
import add_card as ac           # era guess + printing labels
import slab_sources             # shared record of which source checked each slab + your price cap

ROOT = rp.ROOT
CFG_PATH = os.path.join(ROOT, "scripts", "graded_config.json")
DISC_CFG = os.path.join(ROOT, "scripts", "discovery_config.json")
DSTATE = os.path.join(ROOT, "scripts", "discovery_state.json")
GDIR = os.path.join(ROOT, "graded")
HEAT = os.path.join(GDIR, "heat.json")
CHARTS = os.path.join(GDIR, "charts.json")
CPFILE = os.path.join(GDIR, "cardprice.json")   # written by scripts/cardprice_bot.py (CardPrice.com)
PAFILE = os.path.join(GDIR, "pokemonapi.json")  # written by scripts/pokemon_api_bot.py (pokemon-api.com)
GSTATE = os.path.join(GDIR, "state.json")
CANDS = os.path.join(ROOT, "candidates.json")
PPT_KEY = (os.environ.get("PPT_API_KEY") or "").strip()
PPT_BASE = (os.environ.get("PPT_BASE") or "https://www.pokemonpricetracker.com/api/v2").rstrip("/")
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
NO_SCAN = (os.environ.get("NO_POOL_SCAN") or "").lower() == "true"
TODAY = rp.TODAY
PT = datetime.timezone(datetime.timedelta(hours=-8))
GRADES = ("psa10", "psa9", "psa8")
UA = "card-flipz-graded-heat/1.0 (+https://github.com/Chaundoo/card-flipz)"


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jdump(path, obj, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, **kw)


def days_between(a, b=None):
    try:
        return (datetime.date.fromisoformat((b or TODAY)[:10]) - datetime.date.fromisoformat(str(a)[:10])).days
    except (TypeError, ValueError):
        return 9999


def fnum(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        try:
            x = float(v.replace("$", "").replace(",", ""))
            return x if x > 0 else None
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- which Pokémon, which tier
class Names:
    """Your priority list (in order) comes first; the Flip Finder's 'popular' list fills in the rest."""
    def __init__(self, cfg, dcfg):
        prio = cfg["priority"]
        allp = list(dict.fromkeys(prio + dcfg.get("popular", [])))
        allp.sort(key=len, reverse=True)          # "Mewtwo" before "Mew", "Alolan Ninetales" before "Ninetales"
        self.res = [(p, re.compile(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])", re.I)) for p in allp]
        self.rank = {p: i for i, p in enumerate(prio)}
        self.n = max(1, len(prio))
        self.stamp = [re.compile(p, re.I) for p in dcfg.get("stamp_patterns", [])]
        self.skip = [re.compile(p, re.I) for p in dcfg.get("skip_name_patterns", [])]

    def species(self, name):
        for p, r in self.res:
            if r.search(name or ""):
                return p
        return None

    def tier(self, sp):
        return 1 if sp in self.rank else 2 if sp else 0

    def prio_points(self, sp):
        """24 for the first name on your list, sliding to 14 for the last; 6 for other popular Pokémon."""
        if sp in self.rank:
            return 24 - 10 * self.rank[sp] / self.n
        return 6 if sp else 0

    def bad(self, name, variant):
        text = f"{name or ''} {variant or ''}"
        return any(r.search(text) for r in self.stamp) or any(r.search(name or "") for r in self.skip)


def variant_kind(v):
    v = (v or "").lower()
    if "reverse" in v:
        return "reverse"
    if re.search(r"non-holo|^normal$|^unlimited$|^1st edition$", v):
        return "nonholo"
    return "holo"


# ---------------------------------------------------------------- the pool
def pool_from_site(names, manifest):
    out = {}
    def add(c, era, src):
        try:
            tid = int(c.get("tcgId"))
        except (TypeError, ValueError):
            return
        sp = names.species(c.get("name"))
        if not sp or names.bad(c.get("name"), c.get("variant")):
            return
        nm = fnum(((c.get("raw") or {}).get("nm") or {}).get("price"))
        e = {"tcgId": tid, "name": c.get("name") or "", "variant": c.get("variant") or "", "set": c.get("set") or "",
             "number": c.get("number") or "", "era": era or "other", "image": c.get("image") or "", "nm": nm,
             "src": src, "siteId": c.get("id") if src == "site" else None}
        # the PSA 10 / Grade 9 values saved on your site's card: one more opinion for the price check
        # (estimates marked "est." are left out)
        g = c.get("graded") if isinstance(c.get("graded"), dict) and src == "site" else {}
        sg = {}
        for k, gk in (("psa10", "p10"), ("g9", "p9")):
            o = g.get(k) if isinstance(g.get(k), dict) else {}
            if fnum(o.get("price")) and not o.get("est"):
                sg[gk] = fnum(o["price"])
        if sg:
            e["sg"] = sg
        pop = c.get("pop") if isinstance(c.get("pop"), dict) else {}   # (Flip Finder uses "pop" for popular: true)
        if pop.get("psa10"):
            e["pop"] = {k: pop.get(k) for k in ("psa10", "psa9", "psaTotal") if pop.get(k)}
        old = out.get(tid)
        # one TCGplayer product can be both the holo and the reverse holo: keep the holo
        if old and variant_kind(old["variant"]) != "reverse" and variant_kind(e["variant"]) == "reverse":
            return
        out[tid] = e
    for era in manifest.get("eras", []):
        d = jload(os.path.join(rp.ERAS_DIR, era["id"], era["id"] + ".json"), {})
        for c in d.get("cards", []):
            add(c, era["id"], "site")
    for c in jload(CANDS, {}).get("cards", []):
        if c.get("tcgId") and int(c["tcgId"]) not in out:
            add(c, c.get("era"), "finder")
    return out


def scan_pool(gstate, cfg, names, manifest, problems):
    """Spend a few spare TCG Price Lookup requests (Mon-Thu) listing sets the site barely covers."""
    if NO_SCAN or not rp.API_KEY:
        return 0, []
    if gstate.get("poolscan_day") == TODAY:
        return 0, []                             # the scan runs once a day (this bot now runs after every CardPrice run)
    now = datetime.datetime.now(PT)
    if now.weekday() > 3 and (os.environ.get("FORCE_POOL_SCAN") or "").lower() != "true":
        return 0, []                             # Fri-Sun: show-day requests stay free
    sets = (jload(DSTATE, {}).get("setlist") or {}).get("sets") or []
    if not sets:
        return 0, []
    budget = rp.load_budget()
    api = rp.Api(int(budget.get("graded_pool_max_requests", cfg["pool_scan_max_requests"])), int(budget["keep_free_weekdays"]))
    cards_by_era = {}
    for e in manifest["eras"]:
        cards_by_era[e["id"]] = jload(os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json"), {}).get("cards", [])
    want = [x for x in cfg["pool_scan_eras"] if watched_era(x, cfg)]   # only eras you watch
    skip_sets = [s.lower() for s in jload(DISC_CFG, {}).get("skip_sets", [])]
    done = gstate.setdefault("poolsets", {})
    todo = []
    for s in sets:
        name, slug = s.get("name") or "", s.get("slug") or ""
        if not name or not slug or any(x in name.lower() for x in skip_sets):
            continue
        era = ac.era_for(name, manifest, cards_by_era)
        if era not in want:
            continue
        if days_between((done.get(slug) or {}).get("scanned", "")) < cfg["rescan_pool_days"]:
            continue
        todo.append((want.index(era), (done.get(slug) or {}).get("scanned") or "", name, slug, era))
    todo.sort()
    pool = gstate.setdefault("pool", {})
    scanned, added = [], 0
    for _, _, name, slug, era in todo:
        if not api.can():
            break
        recs, offset, ok = [], 0, True
        try:
            while True:
                j = api.get({"game": "pokemon", "set": slug, "limit": rp.PAGE, "offset": offset})
                page = rp.records(j)
                recs += page
                offset += rp.PAGE
                if offset >= int((j or {}).get("total") or 0) or not page:
                    break
        except rp.Stop:
            ok = False
        except Exception as e:
            problems.append(f"pool scan {name}: {str(e)[:120]}")
            ok = False
        n = 0
        for r in recs:
            try:
                tid = int(r.get("tcgplayer_id"))
            except (TypeError, ValueError):
                continue
            nm_, var = r.get("name") or "", r.get("variant") or "Normal"
            if not names.species(nm_) or names.bad(nm_, var):
                continue
            prices, _ = rp.checked_prices(r)
            nm = prices.get("nm", (None,))[0]
            kind = variant_kind(ac.LABELS.get(var.lower(), var))
            floor = cfg["pool_min_nm"] if kind == "holo" else cfg["pool_min_nm_reverse_or_nonholo"]
            if not nm or nm < floor:
                continue
            old = pool.get(str(tid))
            if old and variant_kind(old.get("variant")) != "reverse" and kind == "reverse":
                continue
            pool[str(tid)] = {"tcgId": tid, "name": nm_, "variant": ac.LABELS.get(var.lower(), var), "set": name,
                              "number": r.get("number") or "", "era": era, "image": r.get("image_url") or "",
                              "nm": round(nm, 2), "src": "scan", "added": TODAY}
            n += 1
        if ok:
            done[slug] = {"name": name, "scanned": TODAY, "kept": n}
            scanned.append(f"{name} ({n})")
            added += n
        else:
            break
    if api.used:
        gstate["poolscan_day"] = TODAY
    return api.used, scanned


# ---------------------------------------------------------------- PokemonPriceTracker
class PPTStop(Exception):
    pass


class PPT:
    def __init__(self, cfg):
        self.pause = float(cfg.get("ppt_pause_seconds", 1.3))
        self.used, self.last, self.keys = 0, 0.0, set()
        self.credits_left = None

    def card(self, tcg_id):
        q = urllib.parse.urlencode({"tcgPlayerId": tcg_id, "includeEbay": "true", "includeHistory": "true"})
        url = f"{PPT_BASE}/cards?{q}"
        for attempt in range(3):
            wait = self.pause - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {PPT_KEY}", "Accept": "application/json", "User-Agent": UA})
            self.used += 1
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    for h in ("X-RateLimit-Total-Remaining", "X-RateLimit-Daily-Remaining", "X-RateLimit-Remaining"):
                        if r.headers.get(h):
                            try:
                                self.credits_left = int(float(r.headers.get(h)))
                            except ValueError:
                                pass
                    j = json.loads(r.read().decode("utf-8", "replace"))
                    data = j.get("data") if isinstance(j, dict) else j
                    if isinstance(data, dict):
                        data = [data]
                    return [d for d in (data or []) if isinstance(d, dict)]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200]
                if e.code in (401, 403):
                    raise PPTStop(f"PokemonPriceTracker rejected the key (HTTP {e.code}). Check the PPT_API_KEY secret. {body}")
                if e.code == 402 or (e.code == 429 and re.search(r"credit|daily|quota|limit", body, re.I) and attempt == 2):
                    raise PPTStop(f"PokemonPriceTracker's daily credits are used up (HTTP {e.code})")
                if e.code == 429:
                    try:
                        wait = float(e.headers.get("Retry-After") or 20)
                    except ValueError:
                        wait = 20
                    if wait > 90 or re.search(r"daily|credit|quota", body, re.I):
                        raise PPTStop(f"PokemonPriceTracker's daily credits are used up (it asked to wait {int(wait)} s)")
                    time.sleep(wait + 1)
                    continue
                if e.code == 404:
                    return []
                if e.code >= 500 and attempt < 2:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise RuntimeError(f"network error: {e}")
        raise RuntimeError("gave up after retries")


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _date_of(v):
    if isinstance(v, str) and DATE_RE.match(v):
        return v[:10]
    if isinstance(v, (int, float)) and v > 1e9:          # unix seconds or ms
        t = v / 1000 if v > 1e12 else v
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat()
    return None


PRICE_KEYS = ("medianPrice", "median", "averagePrice", "average", "avg", "marketPrice", "market", "price", "value")
COUNT_KEYS = ("count", "salesCount", "sales", "volume", "totalSales", "n")
DATE_KEYS = ("date", "day", "d", "timestamp", "saleDate", "time")


def _price_of(o):
    if not isinstance(o, dict):
        return fnum(o)
    for k in PRICE_KEYS:
        v = o.get(k)
        if isinstance(v, dict):
            v = v.get("price") or v.get("value")
        p = fnum(v)
        if p:
            return p
    return None


def _count_of(o):
    if not isinstance(o, dict):
        return None
    for k in COUNT_KEYS:
        v = o.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def parse_history(h):
    """Any reasonable history shape -> sorted [(date, price, count|None)]."""
    pts = {}
    if isinstance(h, dict):
        for k, v in h.items():
            d = _date_of(k)
            if d:
                p = _price_of(v)
                if p:
                    pts[d] = (p, _count_of(v))
        for key in ("history", "data", "points", "prices"):
            if not pts and isinstance(h.get(key), (list, dict)):
                return parse_history(h[key])
    elif isinstance(h, list):
        for o in h:
            if isinstance(o, dict):
                d = next((_date_of(o.get(k)) for k in DATE_KEYS if _date_of(o.get(k))), None)
                p = _price_of(o)
                if d and p:
                    pts[d] = (p, _count_of(o))
            elif isinstance(o, (list, tuple)) and len(o) >= 2:
                d, p = _date_of(o[0]), fnum(o[1])
                if d and p:
                    pts[d] = (p, int(o[2]) if len(o) > 2 and isinstance(o[2], (int, float)) else None)
    return sorted((d, p, n) for d, (p, n) in pts.items())


def pick_record(recs, entry):
    if len(recs) <= 1:
        return recs[0] if recs else None
    num = rp.num_key(entry.get("number")) if hasattr(rp, "num_key") else None
    for r in recs:
        if num and rp.num_key(r.get("cardNumber") or r.get("number") or "") == num:
            return r
    return recs[0]


def extract(rec, ppt):
    """PokemonPriceTracker card -> per grade {'p': price now, 'med': ~4-month median, 'n7': sold last 7 days,
    'n': sold in the window, 'days': window length, 'last': last sale}, 'hist': {grade: [(date, avg, count)]}.

    Their API: ebay.salesByGrade.psa10 has medianPrice / count over the whole window (dateRangeStart..End,
    about 4 months), marketPriceMedian7Day and dailyVolume7Day for the last week. smartMarketPrice is
    skipped on purpose: it can be one odd sale (e.g. $122 for a $2,000+ slab). ebay.priceHistory.psa10 is
    {date: {average, count}} but the free plan only gives the last ~3 days, so the bot keeps its own."""
    ppt.keys |= set(rec.keys())
    ebay = rec.get("ebay") if isinstance(rec.get("ebay"), dict) else {}
    ppt.keys |= {"ebay." + k for k in ebay.keys()}
    sbg = ebay.get("salesByGrade") or {}
    d0, d1 = _date_of(ebay.get("dateRangeStart") or ""), _date_of(ebay.get("dateRangeEnd") or "") or TODAY
    days = max(7, days_between(d0, d1)) if d0 else None
    out = {"hist": {}, "from": d0}
    ph = ebay.get("priceHistory") or {}
    for g in GRADES:
        o = sbg.get(g) or {}
        if not isinstance(o, dict):
            continue
        med = fnum(o.get("medianPrice")) or fnum(o.get("averagePrice"))
        med7 = fnum(o.get("marketPriceMedian7Day"))
        v7 = o.get("dailyVolume7Day") if isinstance(o.get("dailyVolume7Day"), (int, float)) else 0
        n7 = int(round(v7 * 7))
        h = parse_history(ph.get(g)) if isinstance(ph, dict) and ph.get(g) else []
        recent = [(p, c or 1) for d, p, c in h if days_between(d) <= 14]
        fresh = True
        if med7 and n7 >= 2:
            p = med7
        elif sum(c for _, c in recent) >= 2:
            p = statistics.median([p for p, _ in recent])
        else:
            p, fresh = med, False          # under 2 sales lately: show the usual price, but no trend from it
        if not p:
            continue
        out[g] = {"p": round(p, 2), "med": round(med, 2) if med else None, "n7": n7, "n": _count_of(o),
                  "days": days, "last": _date_of(o.get("lastSaleDate") or ""), "trend": o.get("marketTrend"), "fresh": fresh}
        if h:
            out["hist"][g] = h
    img = rec.get("imageCdnUrl400") or rec.get("imageCdnUrl") or rec.get("imageUrl")
    if isinstance(img, str) and img.startswith("https://"):
        out["img"] = img
    return out


# ---------------------------------------------------------------- series + score
def series_of(st, grade="psa10"):
    """The bot's own price checks for one grade: sorted [(date, price, sold-last-7-days)]."""
    idx = {"psa10": 1, "psa9": 3}[grade]
    return sorted((s[0], s[idx], s[idx + 1] if len(s) > idx + 1 else None) for s in st.get("snaps", [])
                  if len(s) > idx and s[idx])


def price_near(series, days_back, tol):
    target = datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days_back)
    best = None
    for d, p, _ in series:
        gap = abs((datetime.date.fromisoformat(d) - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, p)
    return best[1] if best else None


def change(series, now, days_back, tol):
    then = price_near(series, days_back, tol)
    if now and then:
        return (now / then - 1) * 100
    return None


def catalysts_for(sp, cfg):
    lo, hi = cfg["catalyst_window_days"]
    out = []
    for c in cfg.get("catalysts", []):
        if sp in c.get("pokemon", []):
            until = -days_between(c["date"])          # days until the event (negative = already out)
            if lo <= until <= hi:
                out.append((c, until))
    return out


def momentum(st, grade="psa10"):
    """(change %, how it was measured, price now). 4-week change from the bot's own checks once it has them;
    until then, the last week's median vs the slab's median over PokemonPriceTracker's ~4-month window."""
    g = (st.get("last") or {}).get(grade) or {}
    now = g.get("p")
    ser = series_of(st, grade)
    c4 = change(ser, now, 28, 9)
    if c4 is not None:
        return c4, "4 weeks", now
    if now and g.get("med") and g.get("fresh", True):
        months = max(1, round((g.get("days") or 120) / 30))
        return (now / g["med"] - 1) * 100, f"its {months}-month median", now
    cs = cp_series(st.get("_cp"), grade)
    if cs:
        c = robust_change(cs, 28)
        if c is not None:
            return c, "4 weeks (CardPrice)", now or cs[-1][1]
    return None, None, now


def robust_change(series, days_back):
    """% change using medians, not single points: the last 2 weeks vs a window around `days_back` days ago.
    CardPrice's estimate history sometimes jumps on one odd point (e.g. $420 for months, then $4,680), and
    comparing one point to one point turned those into fake "Already spiked" / "Breaking out" slabs."""
    now = [p for d, p, _ in series if days_between(d) <= 14]
    then = [p for d, p, _ in series if days_back - 10 <= days_between(d) <= days_back + 10]
    if not now or not then:
        return None
    return (statistics.median(now) / statistics.median(then) - 1) * 100


def cp_series(cpc, grade="psa10"):
    """CardPrice's price history for one grade, or the CardPrice bot's own daily checks: [(date, price, None)]."""
    if not cpc:
        return []
    h = (cpc.get("hist") or {}).get(grade)
    if h and len(h) >= 2:
        return [(d, p, None) for d, p in h]
    i = 1 if grade == "psa10" else 2
    return [(x[0], x[i], None) for x in cpc.get("gsnaps", []) if len(x) > i and x[i]]


def pop_change(cpc):
    """% change in PSA 10 pop over ~4 weeks (from the CardPrice bot's checks): more 10s = more supply."""
    snaps = [x for x in (cpc or {}).get("popsnaps", []) if x[1]]
    if len(snaps) < 2:
        return None
    now = snaps[-1]
    old = next((x for x in reversed(snaps[:-1]) if 21 <= days_between(x[0], now[0]) <= 45), None)
    return (now[1] / old[1] - 1) * 100 if old and old[1] else None


def sales_pace(g):
    """(sold last 7 days, usual sold per week over the window, % change)"""
    n7, n, days = g.get("n7"), g.get("n"), g.get("days")
    if n is None or not days:
        return n7, None, None
    usual = n / days * 7
    if usual < 0.5 or n < 4:
        return n7, usual, None
    return n7, usual, ((n7 or 0) / usual - 1) * 100


def score(e, st, cfg, names, sp_heat):
    sp = names.species(e["name"])
    tier = names.tier(sp)
    last = st.get("last") or {}
    g10, g9 = last.get("psa10") or {}, last.get("psa9") or {}
    m, m_how, p10 = momentum(st, "psa10")
    m9, m9_how, p9 = momentum(st, "psa9")
    ch12 = change(series_of(st), p10, 84, 21)
    n7, usual, vol = sales_pace(g10)
    cpc = st.get("_cp") or {}
    # pop counts: your own (site) counts first, then CardPrice, then PokemonPriceTracker
    if (e.get("pop") or {}).get("psa10"):
        pop, pop_src = e["pop"], "your site"
    elif (cpc.get("pop") or {}).get("psa10"):
        pop, pop_src = cpc["pop"], "CardPrice"
    else:
        pop, pop_src = st.get("pop") or {}, "PokemonPriceTracker" if st.get("pop") else None
    popch = pop_change(cpc)
    why, pts = [], 0.0

    pts += names.prio_points(sp)
    pts += cfg["era_points"].get(e["era"], 5)
    tail = []                                    # (shown last: true of most cards on the list)
    if tier == 1:
        tail.append(f"{sp} is on your priority list (#{names.rank[sp] + 1})")
    if e["era"] in ("wotc", "ex"):
        tail.append("Vintage (1999–2007)")
    elif e["era"] not in cfg["modern_eras"] and e["era"] != "other":
        tail.append("Mid-era (2007–2019)")

    stage = "watching" if g10.get("fresh", True) else "quiet"
    if stage == "quiet":
        why.append("Under 2 PSA 10 sales in the last week, so no trend yet")
    if m is not None:
        if m < 0:
            pts += max(-15, m * 0.5)
        elif m <= 60:
            pts += m * 0.45
        elif m < 100:
            pts += 27 - (m - 60) * 0.3
        else:
            pts += max(0, 12 - (m - 100) * 0.05)
        if m >= 100:
            stage = "spiked"
            why.append(f"PSA 10 already up {m:.0f}% vs {m_how} (you'd be chasing)")
        elif m >= 12:
            stage = "breaking"
            why.append(f"PSA 10 up {m:.0f}% vs {m_how}")
        elif m <= -12:
            stage = "cooling"
            why.append(f"PSA 10 down {abs(m):.0f}% vs {m_how}")
        else:
            stage = "steady"
        if ch12 is not None and m_how == "4 weeks":
            a = m - ch12 / 3
            pts += max(-5, min(8, a * 0.15))
            if a > 10 and 0 <= m < 100:
                why.append("Rise is speeding up")
    if vol is not None:
        pts += max(-6, min(12, vol * 0.12))
        if vol >= 30:
            why.append(f"{n7} PSA 10s sold this week vs about {usual:.1f} a week before (volume often moves before price)")
            if stage in ("steady", "watching"):
                stage = "warming"
        elif vol <= -50:
            why.append("Sales slowing down")
    sib = sp_heat.get(sp)
    if sib and sib[1] >= 2 and sib[0] > 8 and (m is None or m < sib[0]):
        pts += max(0, min(12, (sib[0] - max(m or 0, 0)) * 0.3))
        why.append(f"{sp} heating up on {sib[1]} other slabs (typical {sib[0]:+.0f}%)")
        if stage in ("steady", "watching"):
            stage = "warming"
    cats = []
    for c, until in catalysts_for(sp, cfg):
        if c.get("kind") == "reprint":
            pts -= c.get("boost", 5)
            why.append(f"Reprint risk: {c['event']}")
        else:
            pts += c.get("boost", 6)
            when = f"in {until} days" if until > 0 else "today" if until == 0 else f"{-until} days ago"
            why.append(f"{c['event']} ({when})")
        cats.append(c["event"])
    if usual is not None or n7 is not None:
        if (usual or 0) < 0.5 and (n7 or 0) < 2 and stage != "quiet":
            pts -= 8
            why.append("Thin sales: hard to trust the price")
        elif (usual or 0) >= 3:
            pts += 4
    if pop.get("psa10"):
        gr = pop["psa10"] / pop["psaTotal"] * 100 if pop.get("psaTotal") else None
        if gr is not None and gr < 10:
            pts += 5
            why.append(f"Low gem rate ({gr:.0f}% get a 10)")
        if pop["psa10"] < 300:
            pts += 4
            why.append(f"Only {pop['psa10']:,} PSA 10s")
        elif pop["psa10"] > 20000:
            pts -= 5
    if popch is not None and popch >= 5:
        pts -= min(8, popch * 0.5)
        why.append(f"PSA 10 pop up {popch:.0f}% in 4 weeks (more supply)")
    mv = st.get("_mover")
    if mv is not None and mv > 0:
        pts += min(6, mv * 0.2)
        why.append(f"CardPrice: raw price up {mv:.0f}% this week")
    if stage == "spiked":                        # the move already happened: keep it visible, but not on top
        pts = min(pts * 0.7, 60)
    heat = int(round(max(0, min(100, pts))))
    why += tail
    return {"p10": round(p10, 2) if p10 else None, "p9": round(p9, 2) if p9 else None,
            "med10": g10.get("med"), "med9": g9.get("med"), "medMo": max(1, round((g10.get("days") or 120) / 30)), "p8": (last.get("psa8") or {}).get("p"),
            "n10": n7, "wk10": r1(usual), "n9": g9.get("n7"), "last10": g10.get("last"),
            "ch": r1(m), "chHow": m_how, "ch9": r1(m9), "ch12": r1(ch12),
            "vol": r1(vol), "heat": heat, "stage": stage, "why": why[:7], "cat": cats,
            "pop": {k: v for k, v in pop.items() if v and k in ("psa10", "psa9", "psaTotal", "cgc10", "cgc9", "cgcTotal", "bgs10", "bgsTotal", "asOf")} or None,
            "popSrc": pop_src, "popCh": r1(popch), "cp": bool(cpc.get("graded")), "tier": tier, "sp": sp,
            "prio": names.rank.get(sp)}


def verify(sc, e, st, src, key, cfg, quality):
    """Run the slab's prices through the price check (slab_sources.consensus) and fix up the score:
    the shown PSA 10 / 9 become the checked prices, a trend measured on a price the other sources don't back
    up is dropped, and a slab whose price can't be confirmed can't show as "Breaking out" / "Warming up"."""
    raw = e.get("nm")
    extra = [{"src": "site", "g": g, "p": p} for g, p in (e.get("sg") or {}).items()]
    fresh = [s for s, r in (src.slabs.get(str(key)) or {}).items()
             if s in slab_sources.ALL_SOURCES and slab_sources.days_since(r.get("d")) <= src.fresh_days and (r.get("p10") or r.get("p9"))]
    if not fresh:                                        # only an older check: still one opinion, it just counts less
        last = st.get("last") or {}
        for g, gk in (("psa10", "p10"), ("psa9", "p9")):
            if (last.get(g) or {}).get("p"):
                extra.append({"src": st.get("_srckey") or "own", "g": gk, "p": last[g]["p"], "n": last[g].get("n7"), "d": st.get("checked")})
    c = src.consensus(key, raw=raw, extra=extra, learn=not DRY)
    a, b = c["p10"], c["p9"]
    lim = math.log(1.6)
    old10, old9 = sc.get("p10"), sc.get("p9")
    bad10 = bool(old10 and (a and abs(math.log(old10 / a["p"])) > lim or not a and any(d[1] == "p10" for d in c["dropped"])))
    bad9 = bool(old9 and (b and abs(math.log(old9 / b["p"])) > lim or not b and any(d[1] == "p9" for d in c["dropped"])))
    sc["p10"], sc["p9"] = (a or {}).get("p"), (b or {}).get("p")
    if bad10:
        for k in ("ch", "med10", "ch12", "vol", "chHow"):
            sc[k] = None
        sc["why"] = [w for w in sc["why"] if not re.match(r"PSA 10 (up|down|already up)|Rise is speeding", w)]
    if bad9:
        for k in ("ch9", "med9", "n9"):
            sc[k] = None
    main = a or b
    conf = main["conf"] if main else "none"
    sc["conf"] = conf
    sc["srcs"] = [slab_sources.NAMES.get(s, s) for s in (main or {}).get("srcs", [])]
    quality[conf] = quality.get(conf, 0) + 1
    quality["dropped"] += len(c["dropped"])
    first = []
    fixed = any("PSA 10 can't" in d[3] for d in c["dropped"])
    if "inverted" in c["flags"] or fixed:
        quality["inverted"] += 1
    if "inverted" in c["flags"]:
        sc["stage"] = "conflict"
        first.append(f"PSA 10 came out cheaper than PSA 9 (${a['p']:,.0f} vs ${b['p']:,.0f}): one of them is wrong. Check eBay sold")
    elif bad10:
        sc["stage"] = "conflict"
        who = st.get("_src") or "PokemonPriceTracker"
        first.append(f"PSA 10 from {who} (${old10:,.0f}) doesn't match the other sources"
                     + (f" (${a['p']:,.0f})" if a else "") + ", so its trend isn't trusted")
    elif "split" in c["flags"] and conf == "low" and sc.get("stage") in ("breaking", "warming", "spiked"):
        sc["stage"] = "conflict"
        first.append("Price sources disagree and can't be reconciled. Check eBay sold")
    elif conf == "low" and sc.get("stage") in ("breaking", "warming"):
        sc["stage"] = "verify"
        first.append("Price not confirmed (one source or thin sales), so this may not be a real move. Check eBay sold before buying")
    elif "CardPrice" in str(sc.get("chHow") or "") and (sc.get("ch") or 0) >= cfg.get("est_glitch_pct", 100) and conf in ("est", "low"):
        sc["stage"] = "verify"
        first.append(f"CardPrice's estimate jumped {sc['ch']:.0f}% in 4 weeks with no sales to back it up: usually a data glitch. Check eBay sold")
    elif "CardPrice" in str(sc.get("chHow") or "") and conf == "est" and sc.get("stage") == "breaking" \
            and (sc.get("ch") or 0) < cfg.get("est_breaking_pct", 25):
        sc["stage"] = "steady"                           # estimates wobble 10-20% on their own: not a breakout yet
        sc["heat"] = max(0, sc["heat"] - 8)
    elif "CardPrice" in str(sc.get("chHow") or "") and conf == "est" and sc.get("stage") in ("breaking", "warming"):
        first.append("Trend is from CardPrice's estimates only; real sales haven't confirmed it yet (queued for a sales check)")
    for s, g, p, why in c["dropped"][:2]:
        first.append(f"Ignored {slab_sources.NAMES.get(s, s)}'s {'PSA 10' if g == 'p10' else 'PSA 9'} ${p:,.0f}: {why}")
    if conf == "high" and len(sc["srcs"]) >= 2:
        sc["why"] = sc["why"] + [f"Price confirmed by {' + '.join(sc['srcs'])}"]
    if conf == "est":
        sc["heat"] = max(0, sc["heat"] - 5)               # estimate only: a little lower than slabs with real sales
    if sc["stage"] in ("conflict", "verify"):
        quality["verify"] += 1
        sc["heat"] = min(sc["heat"], 45 if sc["stage"] == "conflict" else 55)
    sc["why"] = (first + sc["why"])[:8]
    if c["dropped"] or c["flags"]:
        sc["chk"] = {"drop": [[slab_sources.NAMES.get(s, s), g, round(p, 2)] for s, g, p, _ in c["dropped"][:4]],
                     "flags": c["flags"]}
    return c


def chart_data(st, base):
    """Per grade: 'pts' = days with sales [day, avg sale $, how many], 'line' = the bot's checks
    [day, median $], 'ref' = [from-day, median $ over the API's window]. Days count from base."""
    out = {}
    b = datetime.date.fromisoformat(base)
    def day(d):
        return (datetime.date.fromisoformat(d) - b).days
    last = st.get("last") or {}
    for g, idx in (("psa10", 1), ("psa9", 3)):
        pts = [[day(d), round(p), c or 0] for d, p, c in (st.get("hist") or {}).get(g, []) if p and days_between(d) <= 200]
        line = [[day(s[0]), round(s[idx])] for s in st.get("snaps", []) if len(s) > idx and s[idx] and days_between(s[0]) <= 200]
        lg = last.get(g) or {}
        ref = [day(st["from"]), round(lg["med"])] if lg.get("med") and st.get("from") else None
        cps = [[day(d), round(p)] for d, p, _ in cp_series(st.get("_cp"), g) if day(d) >= 0]
        if pts or line or cps:
            out["g10" if g == "psa10" else "g9"] = {"pts": pts, "line": line, **({"ref": ref} if ref else {}), **({"cp": cps} if cps else {})}
    return out


def r1(x):
    return None if x is None else round(x, 1)


def weekly(series, weeks=26):
    """One point per week (the week's median), newest last."""
    if not series:
        return []
    buckets = {}
    for d, p, _ in series:
        age = days_between(d)
        if 0 <= age < weeks * 7:
            buckets.setdefault(age // 7, []).append(p)
    return [[w, round(statistics.median(v))] for w, v in sorted(buckets.items(), reverse=True)]


# ---------------------------------------------------------------- what to check today
def watched_era(era, cfg):
    """True for the eras the slab bots look at at all ("eras" in graded_config.json; empty = every era).
    Set from the Graded Slabs tab's "What to look for" panel."""
    eras = cfg.get("eras") or []
    return not eras or era in eras


GSET_KEYS = ("eras", "focus_eras", "grades", "max_slab_price", "min_psa10_price", "modern_max_share")


def settings_of(cfg):
    """What the Graded Slabs tab's "What to look for" panel shows (saved into graded/heat.json)."""
    return {k: cfg.get(k) for k in GSET_KEYS}


def watch_filter(pool, cfg):
    """Only the slabs from eras you watch (so no requests are spent on the others)."""
    return {k: v for k, v in pool.items() if watched_era((v or {}).get("era"), cfg)}


def focus(e, cfg):
    """True for the eras you want looked at first (focus_eras in graded_config.json: mid-era by default)."""
    return (e or {}).get("era") in cfg.get("focus_eras", ["dp-hgss", "bw", "xy", "sm"])


def prior(e, cfg, names):
    sp = names.species(e["name"])
    p = names.prio_points(sp) * 1.5 + cfg["era_points"].get(e["era"], 5) * 1.5     # your priority list goes first
    nm = e.get("nm") or 0
    budget = slab_sources.cap_of(cfg)
    p += math.log2(min(nm, budget * 0.3) + 1) * 4
    if nm > budget * 0.5:          # raw already this pricey: the PSA 10 is almost surely far over budget
        p -= 25
    elif nm > budget * 0.3:
        p -= 8
    k = variant_kind(e.get("variant"))
    p += {"holo": 5, "reverse": -8, "nonholo": -3}[k]
    if re.search(r"1st edition|full art|secret|gold|shining|gold star|☆|crystal|rainbow|alt", f"{e.get('variant')} {e.get('name')}", re.I):
        p += 6
    return p


def own_prices(st):
    """The bot's own PSA 10 / 9 checks as [(date, p10, p9)] (for the price-cap test)."""
    return [(x[0], x[1], x[3] if len(x) > 3 else None) for x in (st or {}).get("snaps", [])]


def due_list(pool, gstate, cfg, names, budget, cpcards=None, src=None, skipped=None):
    """Which slabs to spend today's PokemonPriceTracker credits on. Slabs over your cap (both PSA 10 and 9)
    are skipped, and so are slabs pokemon-api.com checked in the last couple of days, unless they're heating
    up (those get both sources on purpose). Slabs whose raw price is rising (CardPrice)
    jump the queue. Among the rest, mid-era first (focus_eras), then the ones no sales source has seen longest.
    -> (urgent, fresh focus-era, fresh other vintage, fresh modern, rest)"""
    cards = gstate.setdefault("cards", {})
    rc = cfg["recheck_days"]
    skipped = skipped if skipped is not None else {}
    fb = cfg.get("focus_priority_bonus", 15)
    urgent, fresh_focus, fresh_vintage, fresh_modern, rest = [], [], [], [], []
    for key, e in pool.items():
        if not e.get("nm") or e["nm"] < cfg["min_raw_nm"]:
            if not cards.get(key):
                continue
        st = cards.get(key)
        cpc = (cpcards or {}).get(key) or {}
        if src is not None:
            extra = own_prices(st) + [(x[0], x[1], x[2] if len(x) > 2 else None) for x in cpc.get("gsnaps", [])]
            if src.over_cap(key, extra, raw=e.get("nm")):
                skipped["over_cap"] = skipped.get("over_cap", 0) + 1
                continue
            sc0 = (st or {}).get("score") or {}
            hot0 = sc0.get("heat", 0) >= 60 or sc0.get("stage") in ("breaking", "warming")
            if not hot0 and src.other_sales_checked_recently(key, "ppt"):
                skipped["other"] = skipped.get("other", 0) + 1
                continue
        cs = cp_series(cpc)
        cp_rise = change(cs, cs[-1][1], 28, 9) if cs else None
        if not st or not st.get("checked"):
            if cp_rise is not None and cp_rise >= 12:            # CardPrice sees it rising: check it with real sales first
                urgent.append((500 + cp_rise + (fb if focus(e, cfg) else 0), key))
                continue
            gp = cpc.get("graded") or {}
            skip = gp and min(x for x in (gp.get("psa10"), gp.get("psa9")) if x) > budget * cfg["out_of_budget_multiple"] \
                if (gp.get("psa10") or gp.get("psa9")) else False
            pr = prior(e, cfg, names) - (40 if skip else 0)    # both grades far over budget: check much later
            if cpc.get("none") or not gp:
                pr += 10                                        # CardPrice couldn't price it: this bot is the only way
            if focus(e, cfg):
                fresh_focus.append((pr + fb, key))
            else:
                (fresh_modern if e["era"] in cfg["modern_eras"] else fresh_vintage).append((pr, key))
            continue
        if st.get("none"):                       # the API had no graded sales for it
            if days_between(st["checked"]) >= 45:
                rest.append((days_between(st["checked"]), key))
            continue
        age = days_between(st["checked"])
        if src is not None:
            age = min(age, src.sales_age(key))   # a recent pokemon-api.com check counts too
        sc = st.get("score") or {}
        p9, p10 = sc.get("p9") or 0, sc.get("p10") or 0
        if p9 > budget * cfg["out_of_budget_multiple"] or (p10 and p10 < cfg["min_psa10_price"]):
            need = rc["out_of_budget"]
        elif sc.get("heat", 0) >= 60 or sc.get("stage") in ("breaking", "warming", "spiked", "verify"):
            need = rc["hot"]
        elif sc.get("heat", 0) < 30:
            need = rc["cold"]
        else:
            need = rc["normal"]
        if age >= need:
            (urgent if need == rc["hot"] else rest).append((sc.get("heat", 0) + age + (fb if focus(e, cfg) else 0), key))
    for l in (urgent, fresh_focus, fresh_vintage, fresh_modern, rest):
        l.sort(reverse=True)
    return urgent, fresh_focus, fresh_vintage, fresh_modern, rest


def plan(lists, n, modern_share):
    """~40% re-checks of hot cards, then new cards: mid-era (focus_eras) first, about a quarter modern,
    then other vintage, then anything else due."""
    urgent, ff, fv, fm, rest = [[k for _, k in l] for l in lists]
    out, seen = [], set()

    def take(l, upto):
        for k in l:
            if len(out) >= min(upto, n):
                break
            if k not in seen:
                seen.add(k)
                out.append(k)
    take(urgent, max(1, int(n * 0.4)))
    room = n - len(out)
    take(ff, len(out) + int(room * 0.5))                 # half of the new-card room goes to mid-era first
    room = n - len(out)
    take(fm, len(out) + min(len(fm), max(1, int(room * modern_share))))
    for l in (ff, fv, fm, urgent, rest):
        take(l, n)
    return out


# ---------------------------------------------------------------- main
def main():
    cfg = jload(CFG_PATH, None)
    if not cfg:
        return finish("FAILED", "❌ scripts/graded_config.json is missing or broken.\n", saved=False)
    dcfg = jload(DISC_CFG, {})
    names = Names(cfg, dcfg)
    manifest = jload(rp.MANIFEST, {"eras": []})
    gstate = jload(GSTATE, {})
    gstate.setdefault("cards", {})
    problems = []
    t0 = time.time()

    # 1. pool
    scan_used, scanned = 0, []
    try:
        scan_used, scanned = scan_pool(gstate, cfg, names, manifest, problems)
    except Exception as e:
        problems.append(f"pool scan stopped: {str(e)[:160]}")
    pool = {str(k): v for k, v in pool_from_site(names, manifest).items()}
    for k, v in (gstate.get("pool") or {}).items():
        pool.setdefault(k, v)
    pool = watch_filter(pool, cfg)

    # 2. graded prices
    budget = slab_sources.cap_of(cfg)          # max_slab_price: nothing over this gets looked up or listed
    src = slab_sources.Sources(cfg)
    off = set(cfg.get("sources_off") or [])     # sources switched off (graded_config.json "sources_off")
    use_ppt = "ppt" not in off
    n = int(os.environ.get("HEAT_CARDS") or (cfg["ppt_max_credits"] // max(1, cfg["ppt_credits_per_card"])))
    cpd = jload(CPFILE, {})
    cpcards = cpd.get("cards") or {}
    pacards = {} if "pa" in off else (jload(PAFILE, {}) or {}).get("cards") or {}
    skipped = {}
    todo = plan(due_list(pool, gstate, cfg, names, budget, cpcards, src, skipped), n, cfg["modern_max_share"]) if use_ppt else []
    ppt = PPT(cfg)
    checked, got, empty, stop = 0, 0, 0, None
    if not use_ppt:
        pass                                     # PokemonPriceTracker is switched off: CardPrice prices only
    elif not PPT_KEY:
        stop = "the PPT_API_KEY secret isn't set yet, so no graded prices were checked (see the README)"
    else:
        errors_row = 0
        per = max(1, int(cfg["ppt_credits_per_card"]))
        for key in todo:
            e = pool[key]
            if ppt.credits_left is not None and ppt.credits_left < per + 2:
                stop = f"today's free PokemonPriceTracker credits are nearly used ({ppt.credits_left} left)"
                break
            try:
                recs = ppt.card(e["tcgId"])
                errors_row = 0
            except PPTStop as x:
                stop = str(x)
                break
            except Exception as x:
                problems.append(f"{e['name']} ({e['set']}): {str(x)[:140]}")
                errors_row += 1
                if errors_row >= 4:
                    stop = "4 checks in a row failed, so the API may be down; stopped to be safe"
                    break
                continue
            checked += 1
            st = gstate["cards"].setdefault(key, {})
            st["checked"] = TODAY
            rec = pick_record(recs, e)
            x = extract(rec, ppt) if rec else {"hist": {}}
            if not any(g in x for g in GRADES):
                st["none"] = True
                empty += 1
                src.record(key, "ppt", found=False)
                continue
            got += 1
            st.pop("none", None)
            st["last"] = {g: x[g] for g in GRADES if g in x}
            for g, h in x["hist"].items():
                if h:
                    merged = {d: (p, c) for d, p, c in st.get("hist", {}).get(g, [])}
                    merged.update({d: (p, c) for d, p, c in h})
                    st.setdefault("hist", {})[g] = [[d, round(p, 2), c] for d, (p, c) in sorted(merged.items()) if days_between(d) <= 200]
            snap = [TODAY, (x.get("psa10") or {}).get("p"), (x.get("psa10") or {}).get("n7"),
                    (x.get("psa9") or {}).get("p"), (x.get("psa9") or {}).get("n7")]
            if x.get("from"):
                st["from"] = x["from"]
            st["snaps"] = [s for s in st.get("snaps", []) if s[0] != TODAY and days_between(s[0]) <= 400] + [snap]
            src.record(key, "ppt", snap[1], snap[3], snap[2], snap[4])
            if x.get("pop"):
                st["pop"] = x["pop"]
            if x.get("img") and not e.get("image"):
                st["img"] = x["img"]

    # 3. score everything we have prices for (species heat first, so siblings can use it)
    # PokemonPriceTracker checks first; slabs only CardPrice has priced yet are listed from CardPrice
    movers = {}
    for g in (cpd.get("movers") or {}).get("gainers", []):
        if g.get("id") and isinstance(g.get("pct"), (int, float)):
            movers[str(g["id"])] = g["pct"]
    rows = []
    for key in pool:
        st, cpc, pac = gstate["cards"].get(key) or {}, cpcards.get(key) or {}, pacards.get(key) or {}
        if not use_ppt:
            st = {}                              # ignore PokemonPriceTracker's old checks while it's off
        if not st.get("last") and not (cpc.get("graded") or {}) and not (pac.get("psa10") or pac.get("psa9")):
            continue
        view = dict(st, _cp=cpc, _pa=pac, _mover=movers.get(str(cpc.get("cpid") or "")), _srckey="ppt")
        if not st.get("last") and (pac.get("psa10") or pac.get("psa9")):
            # pokemon-api.com's eBay median (and how many sold) when PokemonPriceTracker hasn't checked it yet
            view["last"] = {g: {"p": pac[g]["p"], "fresh": (pac[g].get("n") or 0) >= 2}
                            for g in ("psa10", "psa9") if (pac.get(g) or {}).get("p")}
            view["checked"] = pac.get("checked")
            view["_src"], view["_srckey"] = "pokemon-api.com", "pa"
        elif not st.get("last"):
            view["_srckey"] = "cardprice"
            gp = cpc["graded"]
            view["last"] = {g: {"p": gp[g]} for g in ("psa10", "psa9") if gp.get(g)}   # CardPrice price; trend from its history
            view["checked"] = cpc.get("checked")
        rows.append((key, pool[key], view))
    first = {}
    for key, e, st in rows:
        c = momentum(st)[0]
        if c is not None:
            first.setdefault(names.species(e["name"]), []).append((key, c))
    out, views = [], {}
    quality = {"high": 0, "med": 0, "est": 0, "low": 0, "dropped": 0, "inverted": 0, "verify": 0}
    for key, e, st in rows:
        sp = names.species(e["name"])
        others = [c for k, c in first.get(sp, []) if k != key]
        sp_heat = {sp: (statistics.median(others), len(others))} if others else {}
        sc = score(e, st, cfg, names, sp_heat)
        verify(sc, e, st, src, key, cfg, quality)
        if st.get("_src"):
            sc["priceSrc"] = st["_src"]
        if key in gstate["cards"]:
            gstate["cards"][key]["score"] = {k: sc[k] for k in ("heat", "stage", "p10", "p9")}
        views[key] = st
        if not sc["p10"] and not sc["p9"]:
            continue
        if sc["p10"] and sc["p10"] < cfg["min_psa10_price"] and not sc["p9"]:
            continue
        grades = cfg.get("grades") or ["psa10", "psa9"]    # which grades you'd buy (Graded Slabs tab settings)
        if not ((sc["p10"] and sc["p10"] <= budget and "psa10" in grades) or (sc["p9"] and sc["p9"] <= budget and "psa9" in grades)):
            skipped["listed_over_cap"] = skipped.get("listed_over_cap", 0) + 1
            continue                             # over your cap in both grades: not shown
        cpc = cpcards.get(key) or {}
        cg = cpc.get("graded") or {}
        for gk, ck in (("p95", "g95"), ("p8", "psa8"), ("p7", "g7"), ("raw", "raw")):
            if cg.get(ck):
                sc[gk] = round(cg[ck], 2)        # CardPrice's other grades (Grade 9.5 / 8 / 7, ungraded)
        hs = (cpc.get("hist") or {}).get("psa10") or (cpc.get("hist") or {}).get("psa9") or []
        if len(hs) >= 4:
            sc["spark"] = [round(p) for _, p in hs[-26:]]   # ~6 months of weekly prices for the little chart
            sc["sparkG"] = "PSA 10" if (cpc.get("hist") or {}).get("psa10") else "Grade 9"
        out.append(dict(id=key, name=e["name"], variant=e["variant"], set=e["set"], number=e["number"], era=e["era"],
                        image=e.get("image") or st.get("img") or "", tcgId=e["tcgId"], siteId=e.get("siteId"),
                        checked=st.get("checked"), cpid=(st.get("_cp") or {}).get("cpid") or None,
                        cpslug=(st.get("_cp") or {}).get("slug") or None, **sc))
    # your priority Pokémon on the verge first, then other Pokémon on the verge, then everything else
    def group(c):
        verge = c["stage"] in ("breaking", "warming")
        return 0 if verge and c.get("prio") is not None else 1 if verge else 2
    fset = set(cfg.get("focus_eras", ["dp-hgss", "bw", "xy", "sm"]))
    fsort = cfg.get("focus_sort_bonus", 8)          # mid-era slabs rank a little higher within each group
    out.sort(key=lambda c: (group(c), -(c["heat"] + (fsort if c["era"] in fset else 0))))
    modern = set(cfg["modern_eras"])
    cap = int(cfg["list_size"] * cfg["modern_max_share"])
    final, mcount = [], 0
    for c in out:
        if c["era"] in modern:
            if mcount >= cap:
                continue
            mcount += 1
        final.append(c)
        if len(final) >= cfg["list_size"]:
            break
    for c in final:
        if c.get("siteId") is None:
            c.pop("siteId", None)
        for k in [k for k, v in c.items() if v in (None, [], "")]:
            c.pop(k)

    # 4. report
    watched = sum(1 for k in pool if (gstate["cards"].get(k) or {}).get("checked"))
    status = "OK"
    if stop and ("rejected" in stop or "isn't set" in stop):
        status = "FAILED" if "rejected" in stop else "PARTIAL"
    elif problems and not got:
        status = "PARTIAL"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    hot = [c for c in final if c["stage"] in ("breaking", "warming")][:8]
    L = [f"## {icon} Graded Slabs {status} — {TODAY}" + (" (test run, nothing saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped early: {stop}.", ""]
    L += [f"- Slabs checked today: **{checked}** ({got} with PSA sales, {empty} with none) · API calls {ppt.used}" +
          (f" · credits left {ppt.credits_left}" if ppt.credits_left is not None else ""),
          f"- Pool: {len(pool)} popular-Pokémon cards ({watched} checked at least once)",
          (f"- Price cap ${budget:,.0f} (max_slab_price): {skipped.get('over_cap', 0)} slabs over it skipped, "
           f"{skipped.get('listed_over_cap', 0)} left off the list" if budget != float("inf") else
           "- No price cap (set your budget with the slider on the Graded Slabs tab)") +
          (f" · switched off: {', '.join(slab_sources.NAMES.get(s, s) for s in sorted(off))}" if off else ""),
          f"- Price check: {quality['high']} slabs confirmed by 2+ sources, {quality['med']} by one source with real sales, "
          f"{quality['est']} CardPrice estimate only, {quality['low']} unconfirmed · {quality['dropped']} bad prices thrown out (wrong card / junk sale / far from the others) · "
          f"{quality['inverted']} PSA 10-under-PSA 9 fixed or flagged",
          f"- On the list but marked \"Verify price\" or \"Check prices\": {sum(1 for c in final if c['stage'] in ('conflict', 'verify'))}",
          f"- Mid-era (focus) slabs on the list: {sum(1 for c in final if c['era'] in fset)} · heating up: "
          f"{sum(1 for c in final if c['era'] in fset and c['stage'] in ('breaking', 'warming'))}",
          f"- New cards from the set scan: {', '.join(scanned) if scanned else 'none today'} ({scan_used} TCG Price Lookup requests)",
          f"- On the Graded Slabs list: **{len(final)}** ({sum(1 for c in final if c['era'] in modern)} modern) · took {secs // 60}m {secs % 60}s"]
    if hot:
        def fit(c):                              # the grade you can afford: PSA 10 if under the cap, else PSA 9
            return (f"PSA 10 ${c['p10']:,.0f}" if c.get("p10") and c["p10"] <= budget else
                    f"PSA 9 ${c['p9']:,.0f} (PSA 10 ${c.get('p10') or 0:,.0f})")
        L += ["", "**Heating up:**"] + [f"- {c['name']} ({c['set']} {c['number']}): {fit(c)}"
                                         f"{' · PSA 10 ' + format(c['ch'], '+.0f') + '% vs ' + c['chHow'] if c.get('ch') is not None else ''} · heat {c['heat']} · {c['stage']}" for c in hot]
    if ppt.keys:
        L += ["", "<details><summary>Fields the API returned (for tuning)</summary>", "", "`" + ", ".join(sorted(ppt.keys)) + "`", "</details>"]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {p}" for p in problems[:20]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        test = "Test run (nothing saved): " if DRY else ""
        if status == "FAILED":
            head = f"{test}Graded Slabs failed: {stop or 'see details'}."
        elif stop and not checked:
            head = f"{test}Graded Slabs: {stop}."
        else:
            head = (f"{test}Graded Slabs checked {checked} slabs ({got} with PSA sales). "
                    f"{len(final)} on the list, {len(hot)} heating up.")
        botlog.summary(head, {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"),
                       [f"{ppt.used} PokemonPriceTracker calls", f"{scan_used} TCG Price Lookup requests"])
    except Exception as e:
        print(f"(admin log summary skipped: {e})")

    saved = not DRY and status != "FAILED" and bool(checked or scanned or cpcards or not os.path.exists(HEAT))
    if saved:
        jdump(HEAT, {"version": str(int(time.time())), "generated": TODAY, "budget": None if budget == float("inf") else budget,
                     "pool": len(pool), "watched": watched, "needs_key": use_ppt and not PPT_KEY, "settings": settings_of(cfg),
                     "cards": final}, separators=(",", ":"))
        base = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=365)).isoformat()
        charts = {c["id"]: chart_data(views[c["id"]], base) for c in final}
        jdump(CHARTS, {"base": base, "generated": TODAY, "cards": {k: v for k, v in charts.items() if v}}, separators=(",", ":"))
        gstate["about"] = "Written by scripts/graded_heat.py: graded price snapshots, the extra card pool and when each card was checked. Not loaded by the site."
        jdump(GSTATE, gstate, separators=(",", ":"), sort_keys=True)
        src.save()
    return finish(status, report, saved=saved, total=len(final), hot=len(hot))


def finish(status, report, saved, total=0, hot=0):
    open(rp.REPORT, "w", encoding="utf-8").write(report)
    print(report)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\nsaved={'true' if saved else 'false'}\ntotal={total}\nhot={hot}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(report)
    return 1 if status == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
