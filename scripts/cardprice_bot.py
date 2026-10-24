#!/usr/bin/env python3
"""CardPrice bot: graded prices, PSA/CGC/BGS pop counts and price history from CardPrice.com's free API.

Runs every morning before the Graded Slabs bot (.github/workflows/cardprice.yml) and writes
graded/cardprice.json, which scripts/graded_heat.py reads. It never changes the site's cards.

Why a second source: PokemonPriceTracker's free plan checks ~30 slabs a day and only shares 3 days of
history. CardPrice's API is open (no key), allows 600 requests a minute and shares graded prices, pop
counts and a price-history series, so it can cover the whole pool every few days. The Graded Slabs bot
then shows its pop counts and history, and checks the slabs CardPrice sees rising first.

API (https://cardprice.com/developers, "shapes may evolve, so code defensively"):
  GET /api/search?q=...            -> {results:[{id, slug, name, set_name, number, price}], total}
  GET /api/card/:idOrSlug          -> {card:{id,name,number}, set:{id,name}, latestPrice, variants:[...], graded:{psa10, grade9, ...}, ...}
  GET /api/graded-history?cardId=&days=&printing= -> {series:[{date, ungraded, grade7..grade95, psa10, bgs10, cgc10, sgc10}]}
                                      (used by CardPrice's own card pages; monthly years back, daily lately)
  population on /api/card: [{company:'psa', total, gem, dist:[count of 1s .. 10s], updated_at}]
  GET /api/movers?days=7&minPrice=25&limit=50 -> {gainers:[{name, card_id, current, previous, pct}], losers:[...]}
Terms: free to display with attribution (the tab links to cardprice.com), not for resale or bulk
redistribution. Only the slabs on the Graded Slabs list are shown on the site.

Environment (optional): DRY_RUN "true" = report only; CP_CARDS = max cards this run; CP_BASE = API base (tests).
"""
import json, os, re, sys, time, datetime, statistics
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp
import graded_heat as gh

ROOT = rp.ROOT
OUT = os.path.join(ROOT, "graded", "cardprice.json")
CFG_PATH = os.path.join(ROOT, "scripts", "graded_config.json")
BASE = (os.environ.get("CP_BASE") or "https://api.cardprice.com").rstrip("/")
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
TODAY = rp.TODAY
UA = "card-flipz-graded-heat/1.0 (+https://github.com/Chaundoo/card-flipz; cached daily)"
DEFAULTS = {"cp_max_cards": 150, "cp_pause_seconds": 1.0, "cp_recheck_days": 3, "cp_retry_unmatched_days": 30, "cp_history_days": 365}


class Stop(Exception):
    pass


class CP:
    def __init__(self, pause):
        self.pause, self.last, self.used, self.keys = pause, 0.0, 0, {}

    def get(self, path, params=None):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(3):
            wait = self.pause - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.used += 1
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:160]
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise Stop(f"CardPrice refused the request (HTTP {e.code}); the API may need a key now. {body}")
                if e.code == 429:
                    try:
                        wait = float(e.headers.get("Retry-After") or 30)
                    except ValueError:
                        wait = 30
                    if wait > 120:
                        raise Stop(f"CardPrice asked to wait {int(wait)} s (rate limit); stopped for today")
                    time.sleep(wait + 1)
                    continue
                if e.code >= 500 and attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                raise RuntimeError(f"network error: {e}")
        raise RuntimeError("gave up after retries")

    def note(self, where, obj, depth=0, prefix=""):
        """Remember which fields the API sends (shown in the run report for tuning)."""
        if depth > 3:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                self.keys.setdefault(where, set()).add(prefix + k)
                if isinstance(v, (dict, list)):
                    self.note(where, v, depth + 1, prefix + k + ".")
        elif isinstance(obj, list) and obj:
            self.note(where, obj[0], depth + 1, prefix + "[].")


# ---------------------------------------------------------------- matching a card to CardPrice
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def num_core(n):
    """'034/119' -> '34', 'SWSH050' -> 'swsh50', 'SV49/SV94' -> 'sv49'"""
    n = str(n or "").split("/")[0].lower().strip()
    return re.sub(r"^([a-z]*)0+(?=\d)", r"\1", re.sub(r"[^a-z0-9]", "", n))


SET_NOISE = re.compile(r"\b(swsh\d+|sv\d+|sm\d+|xy\d+|bw\d+|pokemon|tcg|the|and|set|base set)\b")


def set_words(s):
    return set(SET_NOISE.sub(" ", norm(s)).split())


def match(e, results):
    want_num, want_name, want_set = num_core(e["number"]), norm(e["name"]), set_words(e["set"])
    best, best_score = None, 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        sc = 0
        if num_core(r.get("number")) == want_num and want_num:
            sc += 5
        rn = norm(r.get("name"))
        if rn == want_name:
            sc += 4
        elif want_name and (want_name in rn or rn in want_name):
            sc += 2
        st = r.get("set")
        sw = set_words(r.get("set_name") or (st.get("name") if isinstance(st, dict) else st))
        if want_set and sw:
            sc += 4 * len(want_set & sw) / max(len(want_set), 1)
        if sc > best_score:
            best, best_score = r, sc
    return best if best_score >= 8 else None          # number + (name or set) must agree


# ---------------------------------------------------------------- reading a card
GRADE_KEYS = {"psa10": ("psa10", "psa_10", "PSA 10", "psa-10"), "psa9": ("psa9", "psa_9", "grade9", "grade_9", "PSA 9"),
              "psa8": ("psa8", "psa_8", "grade8", "PSA 8")}


def money_of(v):
    if isinstance(v, dict):
        for k in ("price", "market", "value", "median", "avg", "average"):
            if gh.fnum(v.get(k)):
                return gh.fnum(v.get(k))
        return None
    return gh.fnum(v)


def graded_prices(card):
    g = card.get("graded") if isinstance(card.get("graded"), dict) else {}
    out = {}
    for ours, keys in GRADE_KEYS.items():
        for k in keys:
            p = money_of(g.get(k))
            if p:
                out[ours] = round(p, 2)
                break
    return out


def pop_list(card):
    """CardPrice's population: [{company:'psa', total, gem (= 10s), dist:[1s..10s]}, ...]."""
    out = {}
    for r in card.get("population") or []:
        if not isinstance(r, dict) or r.get("printing") not in ("", None):
            continue
        co = str(r.get("company") or "").lower()
        if co not in ("psa", "cgc", "bgs", "sgc", "tag"):
            continue
        dist = r.get("dist") if isinstance(r.get("dist"), list) else []
        n10 = r.get("gem") if isinstance(r.get("gem"), int) else (dist[9] if len(dist) >= 10 else None)
        if isinstance(n10, int):
            out[f"{co}10"] = n10
        if len(dist) >= 9 and isinstance(dist[8], int):
            out[f"{co}9"] = dist[8]
        if isinstance(r.get("total"), int):
            out[f"{co}Total"] = r["total"]
        if co == "psa" and r.get("updated_at"):
            out["asOf"] = str(r["updated_at"])[:10]
    return out


def find_pop(obj, depth=0):
    """Look anywhere in the card for population counts. Returns {'psa10','psa9','psaTotal', 'cgc10', ...}."""
    out = {}
    if depth > 4 or not isinstance(obj, (dict, list)):
        return out
    items = obj.items() if isinstance(obj, dict) else enumerate(obj)
    for k, v in items:
        ks = str(k).lower()
        if isinstance(v, dict) and re.search(r"pop|population|census", ks):
            out.update(read_pop_block(v))
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and re.search(r"pop", ks):
            m = re.search(r"(psa|cgc|bgs)[ _-]?(10|9)", ks)
            if m:
                out[f"{m.group(1)}{m.group(2)}"] = int(v)
            elif re.search(r"total", ks):
                out.setdefault("psaTotal", int(v))
        elif isinstance(v, (dict, list)):
            for kk, vv in find_pop(v, depth + 1).items():
                out.setdefault(kk, vv)
    return out


def read_pop_block(b):
    """{'psa': {'10': 4892, '9': 2572, 'total': 9000}, 'cgc': {...}} or {'psa10': 4892, ...} or [{'grader','grade','count'}]."""
    out = {}
    def put(grader, grade, n):
        if not isinstance(n, (int, float)) or isinstance(n, bool):
            return
        grader = (grader or "psa").lower()
        grade = str(grade).lower().replace("grade", "").replace("_", "").strip()
        if grade in ("10", "9"):
            out[f"{grader}{grade}"] = int(n)
        elif grade in ("total", "all", "graded"):
            out[f"{grader}Total"] = int(n)
    if isinstance(b, list):
        for r in b:
            if isinstance(r, dict):
                put(r.get("grader") or r.get("company"), r.get("grade"), r.get("count") or r.get("pop") or r.get("population"))
        return out
    for k, v in b.items():
        ks = str(k).lower()
        if ks in ("psa", "cgc", "bgs", "tag") and isinstance(v, dict):
            for g, n in v.items():
                put(ks, g, n if not isinstance(n, dict) else (n.get("count") or n.get("pop")))
        elif isinstance(v, list) and ks in ("psa", "cgc", "bgs"):
            for r in v:
                if isinstance(r, dict):
                    put(ks, r.get("grade"), r.get("count") or r.get("pop"))
        else:
            m = re.match(r"(psa|cgc|bgs|tag)?[ _-]?(10|9|total)$", ks)
            if m:
                put(m.group(1) or "psa", m.group(2), v)
    return out


GH_KEYS = {"psa10": "psa10", "grade9": "psa9", "psa9": "psa9"}


def history_series(h):
    """/api/graded-history -> {'series': [{date, ungraded, grade9, psa10, ...}]}: monthly years back, daily lately."""
    rows = (h or {}).get("series") if isinstance(h, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "psa10" in rows[0]:
        out = {}
        for r in rows:
            d = gh._date_of(r.get("date") or "")
            for k, ours in GH_KEYS.items():
                p = gh.fnum(r.get(k))
                if d and p:
                    out.setdefault(ours, {})[d] = round(p, 2)
        return {k: sorted(v.items()) for k, v in out.items()}
    return history_series_any(h)


def history_series_any(h):
    """CardPrice history -> {'raw': [(date, price)], 'psa10': [...], 'psa9': [...]} (whatever it carries)."""
    out = {}
    rows = h
    if isinstance(h, dict):
        for k in ("history", "series", "points", "data", "prices"):
            if isinstance(h.get(k), (list, dict)):
                rows = h[k]
                break
    if isinstance(rows, dict):                       # {date: value} or {psa10: [...], raw: [...]}
        if all(gh._date_of(k) for k in list(rows)[:3]):
            rows = [{"date": k, **(v if isinstance(v, dict) else {"price": v})} for k, v in rows.items()]
        else:
            for k, v in rows.items():
                key = "psa10" if re.search(r"psa.?10|grade.?10", k, re.I) else "psa9" if re.search(r"psa.?9|grade.?9", k, re.I) else "raw"
                pts = gh.parse_history(v)
                if pts:
                    out[key] = [(d, round(p, 2)) for d, p, _ in pts]
            return out
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = next((gh._date_of(r.get(k)) for k in ("date", "day", "d", "t", "timestamp", "asOf") if gh._date_of(r.get(k))), None)
        if not d:
            continue
        for k, v in r.items():
            kl = k.lower()
            key = "psa10" if re.search(r"psa.?10|grade.?10|gem", kl) else "psa9" if re.search(r"psa.?9|grade.?9", kl) else \
                  "raw" if kl in ("price", "market", "value", "close", "nm", "raw", "market_price") else None
            p = money_of(v)
            if key and p:
                out.setdefault(key, []).append((d, round(p, 2)))
    return {k: sorted(set(v)) for k, v in out.items()}


# ---------------------------------------------------------------- main
def main():
    cfg = {**DEFAULTS, **(gh.jload(CFG_PATH, {}) or {})}
    names = gh.Names(cfg, gh.jload(gh.DISC_CFG, {}))
    manifest = gh.jload(rp.MANIFEST, {"eras": []})
    gstate = gh.jload(gh.GSTATE, {})
    pool = {str(k): v for k, v in gh.pool_from_site(names, manifest).items()}
    for k, v in (gstate.get("pool") or {}).items():
        pool.setdefault(k, v)
    data = gh.jload(OUT, {})
    cards = data.setdefault("cards", {})
    on_list = {c["id"] for c in gh.jload(gh.HEAT, {}).get("cards", [])}
    cp = CP(float(cfg["cp_pause_seconds"]))
    t0, problems, stop = time.time(), [], None

    # what to check: slabs on the Graded Slabs list first, then by the same priority as the PSA checks
    todo = []
    for key, e in pool.items():
        c = cards.get(key) or {}
        if c.get("none") and gh.days_between(c.get("checked", "")) < cfg["cp_retry_unmatched_days"]:
            continue
        age = gh.days_between(c.get("checked", ""))
        if age < int(os.environ.get("CP_RECHECK_DAYS") or cfg["cp_recheck_days"]):
            continue
        if not c and (not e.get("nm") or e["nm"] < cfg["min_raw_nm"]):
            continue
        rank = (0 if key in on_list else 1 if c else 2, -gh.prior(e, cfg, names))
        todo.append((rank, key))
    todo.sort()
    limit = int(os.environ.get("CP_CARDS") or cfg["cp_max_cards"])
    checked = matched = with_graded = with_pop = with_hist = 0
    sample = {}

    try:
        mv = cp.get("/api/movers", {"days": 7, "minPrice": 25, "limit": 50})
        cp.note("movers", mv)
        if isinstance(mv, dict):
            data["movers"] = {"asOf": mv.get("asOf") or TODAY,
                              "gainers": [{"id": str(g.get("card_id") or g.get("id")), "name": g.get("name"), "pct": g.get("pct")}
                                          for g in (mv.get("gainers") or [])[:50] if isinstance(g, dict)]}
    except Stop as x:
        stop = str(x)
    except Exception as x:
        problems.append(f"movers: {str(x)[:120]}")

    for _, key in todo[:limit] if not stop else []:
        e = pool[key]
        c = cards.setdefault(key, {})
        try:
            if not c.get("slug"):
                q = re.sub(r"\s+", " ", f"{e['name']} {e['set']}").strip()
                s = cp.get("/api/search", {"q": q})
                cp.note("search", s)
                hit = match(e, (s or {}).get("results") if isinstance(s, dict) else s)
                if not hit:
                    s = cp.get("/api/search", {"q": f"{e['name']} {gh_num(e['number'])}"})
                    hit = match(e, (s or {}).get("results") if isinstance(s, dict) else s)
                if not hit:
                    c.update({"none": True, "checked": TODAY})
                    checked += 1
                    continue
                c.pop("none", None)
                c["slug"], c["cpid"] = hit.get("slug") or str(hit.get("id")), str(hit.get("id") or "")
            card = cp.get("/api/card/" + urllib.parse.quote(str(c["slug"])))
            checked += 1
            if not isinstance(card, dict):
                c.update({"slug": None, "checked": TODAY})      # slug went stale: search again next time
                continue
            cp.note("card", card)
            matched += 1
            inner = card.get("card") if isinstance(card.get("card"), dict) else {}
            c["cpid"] = str(inner.get("id") or c.get("cpid") or "")
            c["checked"] = TODAY
            gp = graded_prices(card)
            if gp:
                with_graded += 1
                c["graded"] = gp
                c.setdefault("gsnaps", [])
                c["gsnaps"] = [s for s in c["gsnaps"] if s[0] != TODAY and gh.days_between(s[0]) <= 400] + [[TODAY, gp.get("psa10"), gp.get("psa9")]]
            pop = pop_list(card) or find_pop(card)
            if pop:
                with_pop += 1
                c["pop"] = pop
                c["popsnaps"] = [s for s in c.get("popsnaps", []) if s[0] != TODAY and gh.days_between(s[0]) <= 400] + \
                                [[TODAY, pop.get("psa10"), pop.get("psaTotal")]]
            raw = [v for v in (card.get("variants") or []) if isinstance(v, dict)]
            if raw:
                c["ch7"], c["ch30"] = raw[0].get("change7d"), raw[0].get("change30d")
            if c.get("cpid"):
                # graded price history (the endpoint CardPrice's own card pages use for their PSA 10 chart)
                h = cp.get("/api/graded-history", {"cardId": c["cpid"], "days": cfg["cp_history_days"], "printing": ""})
                cp.note("history", h)
                if not sample and h:
                    sample = {"card": json.dumps(card)[:700], "history": json.dumps(h)[:700]}
                ser = history_series(h) if h else {}
                if ser:
                    with_hist += 1
                    c["hist"] = {k: weekly([x for x in v if gh.days_between(x[0]) <= 400], 60) for k, v in ser.items() if k in ("psa10", "psa9")}
        except Stop as x:
            stop = str(x)
            break
        except Exception as x:
            problems.append(f"{e['name']} ({e['set']}): {str(x)[:120]}")
            if len(problems) >= 8 and not matched:
                stop = "8 cards in a row failed, so CardPrice may be down; stopped to be safe"
                break

    status = "FAILED" if stop and "refused" in stop else "PARTIAL" if (stop or (problems and not matched)) else "OK"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    L = [f"## {icon} CardPrice {status} — {TODAY}" + (" (test run, nothing saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped early: {stop}.", ""]
    L += [f"- Cards looked up: **{checked}** · found on CardPrice {matched} · with graded prices {with_graded} · with pop counts {with_pop} · with price history {with_hist}",
          f"- API calls: {cp.used} · took {secs // 60}m {secs % 60}s",
          f"- CardPrice movers (7 days): {len((data.get('movers') or {}).get('gainers', []))} gainers saved"]
    if cp.keys:
        L += ["", "<details><summary>Fields CardPrice returned (for tuning)</summary>", ""] + \
             [f"- {w}: `{', '.join(sorted(v)[:80])}`" for w, v in cp.keys.items()] + ["</details>"]
    if sample:
        L += ["", "<details><summary>Sample response</summary>", "", "```", sample["card"], "", sample["history"], "```", "</details>"]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {p}" for p in problems[:15]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        botlog.summary(("Test run (nothing saved): " if DRY else "") +
                       (f"CardPrice failed: {stop}." if status == "FAILED" else
                        f"CardPrice looked up {checked} slabs: {with_graded} with graded prices, {with_pop} with pop counts."),
                       {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"), [f"{cp.used} CardPrice API calls"])
    except Exception as x:
        print(f"(admin log summary skipped: {x})")
    saved = not DRY and status != "FAILED" and checked > 0
    if saved:
        data.update({"about": "Written by scripts/cardprice_bot.py from CardPrice.com's free API (cardprice.com). Read by scripts/graded_heat.py.",
                     "generated": TODAY})
        gh.jdump(OUT, data, separators=(",", ":"), sort_keys=True)
    open(rp.REPORT, "w", encoding="utf-8").write(report)
    print(report)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"status={status}\nsaved={'true' if saved else 'false'}\nchecked={checked}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(report)
    return 1 if status == "FAILED" else 0


def weekly(pts, keep=30):
    """One point per week (the last price seen that week), newest 30 weeks."""
    wk = {}
    for d, p in pts:
        y, w, _ = datetime.date.fromisoformat(d).isocalendar()
        wk[(y, w)] = [d, p]
    return [wk[k] for k in sorted(wk)][-keep:]


def gh_num(n):
    return str(n or "").split("/")[0]


if __name__ == "__main__":
    sys.exit(main())
