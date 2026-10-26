#!/usr/bin/env python3
"""tcgapi.net bot: real eBay sold comps for PSA 10 / PSA 9 slabs (the fourth slab source).

Runs every morning right after the CardPrice bot and before the pokemon-api.com and Graded Slabs bots
(.github/workflows/tcgapi.yml). Writes graded/tcgapi.json, which scripts/graded_heat.py reads, and records
every check in graded/sources.json. It never changes the site's cards.

Why it's useful: it returns individual sold listings (title, price, date), not just an average, so this bot
can throw out the junk itself before taking a median:
  * title filter: the grade must be "PSA 10" / "PSA 9"; drops other graders (CGC, BGS, SGC, TAG, ACE), lots,
    bundles, proxies, "you pick" listings, Japanese/Chinese/Korean prints, damaged or cracked slabs, and any
    title that doesn't carry the card's number;
  * accepted Best Offers are dropped when the API marks them (eBay shows the asking price, not what sold);
  * price outliers: with 4+ sales, any sale whose modified z-score is over 3.5 is dropped
        z = 0.6745 (price - median) / MAD     (MAD = median of each sale's distance from the median)
    with fewer, anything above 2x or below 0.5x the median is dropped.

Sharing the work (graded/sources.json): it has the biggest free allowance of the sales sources, so it does the
broad rotation of eBay comps. It skips slabs over your cap (max_slab_price), slabs PokemonPriceTracker or
pokemon-api.com checked in the last couple of days (except slabs heating up, which get a second opinion), and
otherwise takes the slabs no sales source has seen for the longest time.

Request limits (scripts/graded_config.json): ta_max_per_day (hard cap, counted by the bot itself),
ta_max_slabs (per run), ta_pause_seconds. One request per slab.

API (https://tcgapi.net, Authorization: Bearer <key>):
  GET https://api.tcgapi.net/v1/comps?q=base+set+charizard   -> recent eBay sold listings
Environment: TCGAPI_KEY (GitHub secret) · DRY_RUN "true" = report only · TA_CARDS = max slabs this run ·
TA_BASE = API base URL (testing).
"""
import json, os, re, sys, time, statistics
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp
import graded_heat as gh
import cardprice_bot as cpb
import slab_sources

ROOT = rp.ROOT
OUT = os.path.join(ROOT, "graded", "tcgapi.json")
KEY = (os.environ.get("TCGAPI_KEY") or "").strip()
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
TODAY = rp.TODAY
UA = "card-flipz-graded-heat/1.0 (+https://github.com/Chaundoo/card-flipz)"
DEFAULTS = {"ta_base": "https://api.tcgapi.net/v1", "ta_max_per_day": 900, "ta_max_slabs": 400, "ta_pause_seconds": 1.0,
            "ta_recheck_days": 5, "ta_hot_recheck_days": 2, "ta_retry_empty_days": 21, "ta_window_days": 90,
            "ta_keep_sales": 12}


class Stop(Exception):
    pass


class TA:
    def __init__(self, cfg, usage):
        self.base = (os.environ.get("TA_BASE") or cfg["ta_base"]).rstrip("/")
        self.pause, self.max_day, self.usage = float(cfg["ta_pause_seconds"]), int(cfg["ta_max_per_day"]), usage
        self.used, self.last, self.left, self.keys = 0, 0.0, None, set()

    def room(self):
        n = self.max_day - self.usage["days"].get(rp.TODAY, 0)
        if self.left is not None:
            n = min(n, self.left - 5)
        return max(0, n)

    def get(self, path, params):
        if self.room() <= 0:
            raise Stop("reached today's request cap")
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            wait = self.pause - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.used += 1
            self.usage["days"][TODAY] = self.usage["days"].get(TODAY, 0) + 1
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._limits(r.headers)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                self._limits(e.headers)
                body = e.read().decode("utf-8", "replace")[:200]
                if e.code == 404:
                    return None
                if e.code == 401:
                    raise Stop(f"tcgapi.net refused the key (HTTP 401). Check the TCGAPI_KEY secret. {body}")
                if e.code in (402, 403):
                    raise Stop(f"tcgapi.net says eBay comps aren't available on this key's plan (HTTP {e.code}). {body}")
                if e.code == 429:
                    if re.search(r"day|daily|quota|limit exceeded", body, re.I):
                        raise Stop(f"tcgapi.net's daily limit is used up (HTTP 429). {body}")
                    time.sleep(30)
                    continue
                if e.code >= 500 and attempt == 0:
                    time.sleep(8)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 0:
                    time.sleep(8)
                    continue
                raise RuntimeError(f"network error: {e}")
        raise RuntimeError("gave up after retries")

    def _limits(self, h):
        for k in ("X-RateLimit-Remaining-Day", "X-RateLimit-Daily-Remaining", "X-Daily-Remaining"):
            v = (h or {}).get(k)
            if v is not None:
                try:
                    self.left = int(float(v))
                except ValueError:
                    pass


# ---------------------------------------------------------------- reading comps
def sales_of(j, keys=None):
    """Any reasonable comps response -> [{'title','price','date','offer','grader','grade'}]."""
    rows = j
    if isinstance(j, dict):
        for k in ("comps", "sales", "sold", "results", "items", "data", "listings"):
            if isinstance(j.get(k), list):
                rows = j[k]
                break
            if isinstance(j.get(k), dict):
                return sales_of(j[k], keys)
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        if keys is not None:
            keys |= {"sale." + k for k in r.keys()}
        title = r.get("title") or r.get("name") or r.get("listing_title") or ""
        price = None
        for k in ("sold_price", "price", "sale_price", "total_price", "amount", "value"):
            v = r.get(k)
            if isinstance(v, dict):
                v = v.get("value") or v.get("amount")
            price = gh.fnum(v)
            if price:
                break
        date = next((gh._date_of(r.get(k)) for k in ("sold_date", "date", "end_date", "ended_at", "sold_at", "dateSold", "timestamp")
                     if gh._date_of(r.get(k))), None)
        offer = any(r.get(k) is True for k in ("best_offer", "bestOffer", "best_offer_accepted", "offer_accepted"))
        grader = str(r.get("grader") or r.get("grading_company") or "").lower()
        grade = str(r.get("grade") or "").strip()
        if title and price:
            out.append({"title": title, "price": price, "date": date, "offer": offer, "grader": grader, "grade": grade})
    return out


JUNK = re.compile(r"\b(lot|lots|bundle|set of|x\s?\d|\d\s?x\b|proxy|custom|fan ?art|reprint|orica|you pick|pick your|choose|"
                  r"read|crack|cracked|damaged|scratched|chipped|japanese|japan|jpn|chinese|korean|sealed|pack|box|binder|"
                  r"empty|case only|slab only|no card|cgc|bgs|beckett|sgc|tag grad\w*|tag \d+|ace grad\w*|ace \d+)\b", re.I)


def num_regex(num):
    """'114' -> matches 114 / 114/119 / #114; 'tg6' -> matches TG06 / TG6 (not 1140 or 2114)."""
    m = re.match(r"([a-z]*)(\d+)$", num or "")
    if not m:
        return re.compile(r"(?<![0-9a-z])" + re.escape(num or "") + r"(?![0-9a-z])", re.I)
    return re.compile(r"(?<![0-9a-z])" + m.group(1) + r"\s*0*" + m.group(2) + r"(?![0-9])", re.I)


def grade_of(s, num):
    """'psa10' / 'psa9' / None for one sale, using the API's grade fields if it has them, else the title."""
    t = s["title"]
    if JUNK.search(t):
        return None
    if num and not num_regex(num).search(t):
        return None                                      # title doesn't carry this card's number
    if s.get("grader") and s["grader"] != "psa":
        return None
    if s.get("grade"):
        g = re.sub(r"\.0$", "", s["grade"])
        return {"10": "psa10", "9": "psa9"}.get(g)
    m = re.findall(r"\bpsa\s*[-:#]?\s*(?:gem\s*(?:mt|mint)|mint|nm-mt|nm)?\s*(10|9(?:\.5)?|[1-8](?:\.5)?)\b", t, re.I)
    if len(set(m)) != 1:                                 # no grade, or several grades in one title
        return None
    return {"10": "psa10", "9": "psa9"}.get(m[0])


def clean(prices):
    """Drop price outliers. Returns (kept, dropped)."""
    if len(prices) < 2:
        return prices, []
    med = statistics.median(prices)
    if len(prices) >= 4:
        mad = statistics.median([abs(p - med) for p in prices])
        if mad > 0:
            keep = [p for p in prices if abs(0.6745 * (p - med) / mad) <= 3.5]
            return keep, [p for p in prices if p not in keep]
    keep = [p for p in prices if 0.5 * med <= p <= 2 * med]
    return keep, [p for p in prices if p not in keep]


def summarize(sales, num, window):
    """-> {'psa10': {...}, 'psa9': {...}} with p (median of the last 14 days if 2+ sales, else of the window),
    med (window median), n (sales in window), n7 (last 7 days), days, last, sales [[date, price]]; and drop counts."""
    out, dropped = {}, {"title": 0, "offer": 0, "price": 0}
    by = {"psa10": [], "psa9": []}
    for s in sales:
        if s["offer"]:
            dropped["offer"] += 1
            continue
        g = grade_of(s, num)
        if not g or g not in by:
            dropped["title"] += 1
            continue
        if s["date"] and gh.days_between(s["date"]) > window:
            continue
        by[g].append((s["date"] or TODAY, s["price"]))
    for g, rows in by.items():
        keep, bad = clean([p for _, p in rows])
        dropped["price"] += len(bad)
        rows = [(d, p) for d, p in rows if p in keep]
        if not rows:
            continue
        rows.sort()
        recent = [p for d, p in rows if gh.days_between(d) <= 14]
        med = statistics.median(p for _, p in rows)
        out[g] = {"p": round(statistics.median(recent) if len(recent) >= 2 else med, 2), "med": round(med, 2),
                  "n": len(rows), "n7": sum(1 for d, _ in rows if gh.days_between(d) <= 7), "days": window,
                  "last": rows[-1][0], "fresh": len(recent) >= 2, "sales": [[d, round(p, 2)] for d, p in rows]}
    return out, dropped


def query_for(e):
    name = re.sub(r"\s+", " ", e["name"]).strip()
    s = re.sub(r"\b(SWSH\d+|SV\d+|SM\d+|XY\d+|BW\d+)\s*:\s*", "", e.get("set") or "")
    return f"{name} {cpb.gh_num(e['number'])} {s} PSA".strip()


# ---------------------------------------------------------------- what to check
def pick(pool, gstate, src, cards, cfg, names, limit):
    hot, rest = [], []
    gcards = gstate.get("cards") or {}
    for key, e in pool.items():
        mine = cards.get(key) or {}
        age = gh.days_between(mine.get("checked", ""))
        if mine.get("empty") and age < cfg["ta_retry_empty_days"]:
            continue
        own = [(s[0], s[1], s[3]) for s in (gcards.get(key) or {}).get("snaps", [])]
        if src.over_cap(key, own):
            continue
        if not src.slabs.get(str(key)) and (e.get("nm") or 0) > src.cap * 0.3:
            continue                                     # never priced, raw already pricey: slabs likely over the cap
        sc = (gcards.get(key) or {}).get("score") or {}
        if sc.get("stage") in ("breaking", "warming", "conflict") or sc.get("heat", 0) >= 60:
            if age >= cfg["ta_hot_recheck_days"]:
                hot.append((-sc.get("heat", 0), key))    # a second opinion on purpose
            continue
        if age < cfg["ta_recheck_days"] or src.other_sales_checked_recently(key, "ta"):
            continue
        rest.append((-src.sales_age(key), -gh.prior(e, cfg, names), key))
    hot.sort()
    rest.sort()
    return ([k for _, k in hot] + [x[-1] for x in rest])[:limit]


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
    cards = data.setdefault("cards", {})
    usage = data.get("usage") or {}
    usage["days"] = {d: n for d, n in (usage.get("days") or {}).items() if gh.days_between(d) <= 31}
    src = slab_sources.Sources(cfg)
    api = TA(cfg, usage)
    t0, problems, stop = time.time(), [], None
    checked = priced = 0
    dropped = {"title": 0, "offer": 0, "price": 0}
    lines = []

    if not KEY:
        stop = "the TCGAPI_KEY secret isn't set yet, so nothing was looked up (see the README)"
        todo = []
    else:
        todo = pick(pool, gstate, src, cards, cfg, names, int(os.environ.get("TA_CARDS") or cfg["ta_max_slabs"]))

    for key in todo:
        if api.room() <= 0:
            break
        e = pool[key]
        try:
            j = api.get("/comps", {"q": query_for(e)})
        except Stop as x:
            stop = str(x)
            break
        except Exception as x:
            problems.append(f"{e['name']} ({e['set']}): {str(x)[:140]}")
            if len(problems) >= 4 and not checked:
                stop = "4 lookups in a row failed, so tcgapi.net may be down; stopped to be safe"
                break
            continue
        checked += 1
        if isinstance(j, dict):
            api.keys |= set(j.keys())
        sales = sales_of(j, api.keys)
        s, d = summarize(sales, cpb.num_core(e["number"]), int(cfg["ta_window_days"]))
        for k in dropped:
            dropped[k] += d[k]
        c = cards.setdefault(key, {})
        c["checked"] = TODAY
        p10, p9 = (s.get("psa10") or {}), (s.get("psa9") or {})
        src.record(key, "ta", p10.get("p"), p9.get("p"), p10.get("n"), p9.get("n"), found=bool(s))
        if not s:
            c["empty"] = True
            continue
        c.pop("empty", None)
        priced += 1
        keep = int(cfg["ta_keep_sales"])
        for g in ("psa10", "psa9"):
            if g in s:
                c[g] = {k: v for k, v in s[g].items() if k != "sales"}
                old = {(a, b) for a, b in (c.get(g + "_sales") or [])}
                c[g + "_sales"] = sorted(old | {tuple(x) for x in s[g]["sales"]})[-keep:]
                c[g + "_sales"] = [list(x) for x in c[g + "_sales"] if gh.days_between(x[0]) <= 200]
            else:
                c.pop(g, None)
        c["snaps"] = [x for x in c.get("snaps", []) if x[0] != TODAY and gh.days_between(x[0]) <= 400] + \
                     [[TODAY, p10.get("p"), p10.get("n7"), p9.get("p"), p9.get("n7")]]
        if len(lines) < 15:
            lines.append(f"- {e['name']} ({e['set']} {e['number']}): PSA 10 {('$' + format(p10['p'], ',.0f') + ' (' + str(p10['n']) + ' sold)') if p10 else '—'}"
                         f" · PSA 9 {('$' + format(p9['p'], ',.0f') + ' (' + str(p9['n']) + ' sold)') if p9 else '—'}")

    data["usage"] = usage
    status = "FAILED" if stop and ("refused" in stop or "plan" in stop) else \
             "PARTIAL" if (stop and "cap" not in stop) or (problems and not checked) else "OK"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    L = [f"## {icon} tcgapi.net {status} — {TODAY}" + (" (test run, nothing saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped: {stop}.", ""]
    L += [f"- Slabs looked up: **{checked}** · with PSA 10 / 9 eBay sales {priced} · requests {api.used} "
          f"({usage['days'].get(TODAY, 0)} of {cfg['ta_max_per_day']} allowed today)",
          f"- Sales thrown out: {dropped['title']} by title (wrong grade/grader, lots, other cards…), "
          f"{dropped['offer']} accepted Best Offers, {dropped['price']} price outliers",
          f"- Price cap ${src.cap:,.0f} (max_slab_price) · took {secs // 60}m {secs % 60}s"]
    if lines:
        L += [""] + lines
    if api.keys:
        L += ["", "<details><summary>Fields the API returned (for tuning)</summary>", "", "`" + ", ".join(sorted(api.keys)[:80]) + "`", "</details>"]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {p}" for p in problems[:10]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        if not KEY:
            head = "tcgapi.net isn't set up yet: add the TCGAPI_KEY secret (see the README)."
        elif status == "FAILED":
            head = f"tcgapi.net failed: {stop}."
        else:
            head = f"tcgapi.net looked up {checked} slabs: {priced} with PSA 10 / 9 eBay sales."
        botlog.summary(("Test run (nothing saved): " if DRY else "") + head,
                       "warn" if not KEY else {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"),
                       [f"{api.used} requests", f"{dropped['price']} outlier sales dropped"])
    except Exception as x:
        print(f"(admin log summary skipped: {x})")
    saved = not DRY and api.used > 0
    if saved:
        data.update({"about": "Written by scripts/tcgapi_bot.py from tcgapi.net eBay comps. Read by scripts/graded_heat.py.",
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
