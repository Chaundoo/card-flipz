#!/usr/bin/env python3
"""Weekly TCGplayer sales check for Chaundoo Card Flipz (runs in GitHub Actions).

TCG Price Lookup gives TCGplayer's "market" price, which on thin cards can be one odd sale
(e.g. Typhlosion NM $540 when LP is $76). This job looks at TCGplayer's own sales history,
but ONLY for the cards that need it:
  * cards with an estimated ("est.") price, e.g. an NM spike the daily update caught
  * the top flips on the site (by dollar profit and by % return)
  * the top Flip Finder candidates
For each one it takes the real sales of the last 90 days per condition, throws out sales that
are far off from the other conditions (manipulated / odd sales) and saves the median sale.
Results go in scripts/tcgplayer_verified.json and onto the cards. The daily TCG Price Lookup
update keeps using these prices while they're fresh (trust_days in scripts/tcgplayer_config.json).

When it runs: once a week at a random time Monday-Thursday (Pacific). The workflow wakes up
every two hours on those days; this script picks the week's slot from the week number, so the
time changes every week, then waits a random number of minutes before starting. Requests are
one at a time with a random pause. Settings: scripts/tcgplayer_config.json.

Usage
  python3 scripts/tcgplayer_check.py               normal run (checks the weekly slot first)
  NOW=true python3 scripts/tcgplayer_check.py      run right away (manual runs)
  DRY_RUN=true ...                                 look and report, save nothing
  LIMIT=10 ...                                     only check 10 cards
  python3 scripts/tcgplayer_check.py --apply [--log FILE]
                                                   only put the saved checks on the cards
                                                   (used when a save raced another job)
"""
import json, os, sys, time, random, datetime, hashlib, statistics
import urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp

ROOT = rp.ROOT
CFG_PATH = rp.TP_CONFIG
VERIFIED = rp.VERIFIED
CANDS = os.path.join(ROOT, "candidates.json")
SECTION = os.path.join(ROOT, "tcgplayer_section.md")

NOW = (os.environ.get("NOW") or "").lower() == "true"
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
LIMIT = int(os.environ.get("LIMIT") or 0)
TODAY = rp.TODAY
CONDS = rp.CONDS
FLIPS = [("dmg", "mp"), ("mp", "lp"), ("lp", "nm")]
COND = {"Near Mint": "nm", "Lightly Played": "lp", "Moderately Played": "mp", "Damaged": "dmg"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

URL = "https://infinite-api.tcgplayer.com/price/history/{id}/detailed?range=quarter"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
}


class Blocked(Exception):
    pass


class NoData(Exception):
    pass


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def money(x):
    return f"${x:,.2f}"


def pacific_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8)))


# ---------------------------------------------------------------- when to run
def weekly_slot(cfg, now):
    """This week's (weekday index, Pacific hour), picked from the week number so it changes every week."""
    days = [DAYS.index(d[:3].title()) for d in cfg["slot_days"]]
    slots = [(d, h) for d in days for h in cfg["slot_hours_pacific"]]
    y, w, _ = now.isocalendar()
    n = int(hashlib.sha256(f"card-flipz-{y}-{w}".encode()).hexdigest(), 16)
    return slots[n % len(slots)]


def should_run(cfg, verified):
    """(run?, why). Runs at the first wake-up at or after this week's slot, once per week."""
    now = pacific_now()
    monday = (now - datetime.timedelta(days=now.weekday())).date().isoformat()
    if (verified.get("last_run") or "") >= monday:
        return False, f"already ran this week ({verified['last_run']})"
    day, hour = weekly_slot(cfg, now)
    slot = f"{DAYS[day]} ~{hour}:00 Pacific"
    if now.weekday() > max(DAYS.index(d[:3].title()) for d in cfg["slot_days"]):
        return False, f"it's {DAYS[now.weekday()]}; this week's slot ({slot}) was missed, next week gets a new one"
    if (now.weekday(), now.hour) < (day, hour):
        return False, f"this week's slot is {slot}; not yet"
    return True, f"this week's slot is {slot}"


# ---------------------------------------------------------------- picking cards
def best_flip(raw, fee_pct, min_buy):
    best = None
    for b, s in FLIPS:
        pb = (raw.get(b) or {}).get("price")
        ps = (raw.get(s) or {}).get("price")
        if not (isinstance(pb, (int, float)) and isinstance(ps, (int, float))) or pb < min_buy:
            continue
        net = ps * (1 - fee_pct / 100) - pb
        if net > 0 and (best is None or net > best[0]):
            best = (net, net / pb * 100)
    return best


def has_estimate(card):
    return any(isinstance(e, dict) and (e.get("est") or str(e.get("src", "")).startswith("Estimated"))
               for e in (card.get("raw") or {}).values())


def pick_cards(cfg, site_cards, cands, done_recently):
    picks = {}   # key -> [card, reasons]

    def add(card, why):
        k = rp.printing_key(card)
        if not k or k in done_recently:
            return
        picks.setdefault(k, [card, []])
        if why not in picks[k][1]:
            picks[k][1].append(why)

    flips = []
    for c in site_cards:
        f = best_flip(c.get("raw") or {}, cfg["ebay_fee_pct"], cfg["min_buy"])
        if f:
            flips.append((c, f))
    top_net = [c for c, _ in sorted(flips, key=lambda x: -x[1][0])[:cfg["top_by_profit"]]]
    top_roi = [c for c, _ in sorted(flips, key=lambda x: -x[1][1])[:cfg["top_by_return"]]]
    top_ids = {id(c) for c in top_net + top_roi}
    est = [c for c in site_cards if has_estimate(c)]
    est.sort(key=lambda c: -(((c.get("raw") or {}).get("nm") or {}).get("price") or 0))

    for c in est:                       # skewed AND a top flip: most important
        if id(c) in top_ids:
            add(c, "estimate")
            add(c, "top flip")
    for c in top_net + top_roi:
        add(c, "top flip")
    for c in sorted(cands, key=lambda c: -(c.get("score") or 0))[:cfg["top_flip_finder"]]:
        add(c, "Flip Finder")
    for c in est:
        add(c, "estimate")
    out = list(picks.values())
    cap = LIMIT or cfg["max_cards"]
    return out[:cap], max(0, len(out) - cap)


# ---------------------------------------------------------------- TCGplayer
def fetch(tcg_id):
    last = None
    for attempt in range(3):
        req = urllib.request.Request(URL.format(id=tcg_id), headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                raise Blocked("got a web page instead of data (bot check)")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NoData("TCGplayer has no sales history for this product")
            if e.code in (401, 403):
                last = Blocked(f"HTTP {e.code} (access denied)")
            elif e.code == 429:
                last = Blocked("HTTP 429 (too many requests)")
            elif e.code >= 500:
                last = Exception(f"HTTP {e.code} (TCGplayer server error)")
            else:
                raise Exception(f"HTTP {e.code}")
        except Blocked as e:
            last = e
        except Exception as e:
            last = Exception(f"network error: {e}")
        time.sleep([8, 25, 0][attempt] + random.random() * 5)
    raise last


def pick_variant(card, results):
    """results: TCGplayer history rows. Returns the rows (English) for this card's printing."""
    rows = [r for r in results if (r.get("language") or "English") == "English"]
    by = {}
    for r in rows:
        by.setdefault((r.get("variant") or "Normal").lower(), []).append(r)
    want = (card.get("tcgVariant") or rp.wanted_printing(card.get("variant"))).lower()
    for w in (want, want.replace("unlimited ", ""), "holofoil" if want == "unlimited holofoil" else None):
        if w and w in by:
            return by[w]
    # one printing only (e.g. a promo listed as "Normal"): fine for a plain holo/non-holo card,
    # but never for reverse holo / 1st edition, where a wrong match would copy another printing's prices
    if len(by) == 1 and want in ("holofoil", "normal") and next(iter(by)) in ("holofoil", "normal"):
        return next(iter(by.values()))
    return None


def sales(row, since):
    """[(date, price, bucket index)] for one condition, newest first. A bucket with several sales
    gives its low and high sale plus its middle for the rest."""
    out = []
    for i, b in enumerate(row.get("buckets") or []):
        d = (b.get("bucketStartDate") or "")[:10]
        q = int(float(b.get("quantitySold") or 0))
        lo, hi = float(b.get("lowSalePrice") or 0), float(b.get("highSalePrice") or 0)
        if q <= 0 or lo <= 0 or d < since:
            continue
        hi = hi or lo
        pts = [lo] if q == 1 else [lo, hi] + [(lo + hi) / 2] * min(q - 2, 8)
        out += [(d, p, i) for p in pts]
    return out


def judge(rows, cfg):
    """{cond: {'price','date','src','est'}} from TCGplayer sales, odd sales thrown out."""
    since = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=cfg["sales_days"])).isoformat()
    by_c = {}
    for r in rows:
        c = COND.get(r.get("condition"))
        if c:
            by_c[c] = r
    s, odd_of = {}, {}
    for c, r in by_c.items():
        v = sales(r, since)
        # 1) a sale far from the same condition's other sales is odd (needs 3+ sales to tell)
        if len(v) >= 3:
            m = statistics.median([p for _, p, _ in v])
            odd_of[c] = [x for x in v if not 0.5 * m <= x[1] <= 2 * m]
            v = [x for x in v if 0.5 * m <= x[1] <= 2 * m]
        s[c] = v
    med = {c: statistics.median([p for _, p, _ in v]) for c, v in s.items() if v}
    lo_k, hi_k = cfg["odd_sale_low"], cfg["odd_sale_high"]

    def expected(c):
        eq = [med[o] * rp.RATIOS[c] / rp.RATIOS[o] for o in med if o != c]
        return statistics.median(eq) if eq else None

    out = {}
    for c, v in s.items():
        exp = expected(c)
        keep, odd = v, list(odd_of.get(c, []))
        # 2) with only 1-2 sales, check them against the other conditions' sales instead
        if len(v) <= 2 and exp:
            keep = [x for x in v if lo_k * exp <= x[1] <= hi_k * exp]
            odd += [x for x in v if x not in keep]
        if not keep and not odd:
            continue
        odd_txt = ", ".join(money(p) for _, p, _ in odd[:3])
        if keep:
            p = statistics.median([x[1] for x in keep])
            src = f"TCGplayer sales: median of {len(keep)} sale{'s' if len(keep) != 1 else ''} in the last {cfg['sales_days']} days"
            if odd:
                src += f" (ignored odd sale{'s' if len(odd) != 1 else ''}: {odd_txt})"
            out[c] = {"price": round(p, 2), "date": keep[0][0], "src": src, "est": False}
            continue
        # only odd sales: use TCGplayer's market price from just before the oldest odd sale
        buckets = by_c[c].get("buckets") or []
        oldest = max(i for _, _, i in odd)
        before = next((float(b.get("marketPrice") or 0) for b in buckets[oldest + 1:]
                       if float(b.get("marketPrice") or 0) > 0), 0)
        if before and (exp is None or lo_k * exp <= before <= hi_k * exp):
            out[c] = {"price": round(before, 2), "date": odd[-1][0], "est": False,
                      "src": f"TCGplayer market price before an odd {odd_txt} sale (only sale{'s' if len(odd) != 1 else ''} in {cfg['sales_days']} days)"}
        elif exp:
            out[c] = {"price": round(exp, 2), "date": TODAY, "est": True,
                      "src": f"Estimated from the other conditions' TCGplayer sales (the only {c.upper()} sale{'s were' if len(odd) != 1 else ' was'} odd: {odd_txt})"}
    # keep the ladder in order: a worse condition can't sit above a better one
    top = None
    for c in CONDS:
        if c not in out:
            continue
        if top is not None and out[c]["price"] > top * 1.08 + 0.5:
            old = out[c]["price"]
            out[c] = {"price": round(top, 2), "date": out[c]["date"], "est": True,
                      "src": f"Estimated: TCGplayer sales showed {money(old)}, above a better condition, so capped"}
        top = out[c]["price"] if top is None else min(top, out[c]["price"])
    return out


# ---------------------------------------------------------------- putting results on cards
def load_all():
    manifest = jload(rp.MANIFEST, {"eras": []})
    files = []
    for e in manifest["eras"]:
        p = os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json")
        if os.path.exists(p):
            files.append((p, jload(p, {"cards": []})))
    cands = jload(CANDS, {"cards": []})
    return files, cands


def apply_all(files, cands, fresh):
    """Put fresh checked prices on every site card and candidate. Returns (cards changed, files to save)."""
    n, dirty = 0, set()
    for i, (_, d) in enumerate(files):
        for c in d.get("cards", []):
            if rp.overlay_verified(c, c.setdefault("raw", {}), fresh):
                c["updated"] = TODAY
                n += 1
                dirty.add(i)
    cand_dirty = False
    for c in cands.get("cards", []):
        if rp.overlay_verified(c, c.setdefault("raw", {}), fresh):
            cand_dirty = True
    return n, dirty, cand_dirty


def save_all(files, dirty, cands, cand_dirty):
    stamp = str(int(time.time()))
    for i in dirty:
        p, d = files[i]
        d["version"] = stamp
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    if cand_dirty:
        cands["version"] = stamp
        with open(CANDS, "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False, separators=(",", ":"))


def prepend_log(section):
    old = open(rp.LOG, encoding="utf-8").read() if os.path.exists(rp.LOG) else ""
    runs = [r.rstrip() + "\n" for r in __import__("re").findall(r"(?ms)^## .*?(?=^## |\Z)", old)]
    body = "# Price update log\n\nNewest first. Written automatically by the daily GitHub job.\n\n" + \
           "\n".join([section] + runs[:rp.KEEP_LOG_RUNS - 1])
    open(rp.LOG, "w", encoding="utf-8").write(body)


def output(**kw):
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- main
def main():
    cfg = jload(CFG_PATH, {})
    verified = jload(VERIFIED, {"cards": {}})
    verified.setdefault("cards", {})

    if "--apply" in sys.argv:
        files, cands = load_all()
        n, dirty, cd = apply_all(files, cands, rp.load_verified())
        save_all(files, dirty, cands, cd)
        if "--log" in sys.argv:
            prepend_log(open(sys.argv[sys.argv.index("--log") + 1], encoding="utf-8").read())
        print(f"Applied saved TCGplayer checks: {n} cards changed.")
        return 0

    if not NOW:
        ok, why = should_run(cfg, verified)
        print(why)
        if not ok:
            output(status="SKIPPED", ran="false")
            return 0
        wait = random.randint(0, int(cfg.get("max_start_delay_minutes", 0)) * 60)
        print(f"Waiting {wait // 60} min {wait % 60} s before starting.")
        time.sleep(wait)

    files, cands = load_all()
    site_cards = [c for _, d in files for c in d.get("cards", [])]
    rp.learn_ratios(site_cards)
    recent = {k for k, v in verified["cards"].items()
              if (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(v.get("checked", "2000-01-01")[:10])).days
              < cfg["recheck_days"]} if not LIMIT else set()
    todo, left_out = pick_cards(cfg, site_cards, cands.get("cards", []), recent)
    print(f"Checking {len(todo)} cards on TCGplayer.")

    stats = {"checked": 0, "nosales": 0, "noprinting": 0, "nodata": 0, "error": 0, "blocked": 0}
    changes, problems = [], []
    blocked_in_a_row, stop = 0, None
    t0 = time.time()
    for n, (card, why) in enumerate(todo):
        if n:
            time.sleep(random.uniform(*cfg["pause_seconds"]))
        name = rp.label(card)
        try:
            j = fetch(card["tcgId"])
            blocked_in_a_row = 0
        except Blocked as e:
            stats["blocked"] += 1
            blocked_in_a_row += 1
            problems.append(("blocked", name, str(e)))
            if blocked_in_a_row >= 3:
                stop = f"TCGplayer blocked 3 requests in a row ({e}); stopped to be safe"
                break
            time.sleep(60)
            continue
        except NoData as e:
            stats["nodata"] += 1
            problems.append(("no data", name, str(e)))
            continue
        except Exception as e:
            stats["error"] += 1
            problems.append(("error", name, str(e)))
            continue
        rows = pick_variant(card, (j or {}).get("result") or [])
        if not rows:
            stats["noprinting"] += 1
            problems.append(("printing", name, "couldn't tell which TCGplayer printing this card is"))
            continue
        res = judge(rows, cfg)
        if not res:
            stats["nosales"] += 1
            continue
        stats["checked"] += 1
        old_raw = card.get("raw") or {}
        diff = []
        for c in CONDS:
            if c in res:
                o = (old_raw.get(c) or {}).get("price")
                if isinstance(o, (int, float)) and o > 0 and abs(res[c]["price"] - o) / o >= 0.15:
                    diff.append(f"{c.upper()} {money(o)} → {money(res[c]['price'])}" + (" est." if res[c]["est"] else ""))
        if diff:
            changes.append((name, why, diff))
        verified["cards"][rp.printing_key(card)] = {"checked": TODAY, "card": name, "why": why, "prices": res}
    secs = int(time.time() - t0)

    # forget old checks
    keep_days = int(cfg.get("forget_days", 60))
    verified["cards"] = {k: v for k, v in verified["cards"].items()
                         if (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(v.get("checked", "2000-01-01")[:10])).days <= keep_days}

    if stop and not stats["checked"]:
        status = "BLOCKED"
    elif stop or stats["error"] or stats["blocked"]:
        status = "PARTIAL"
    else:
        status = "OK"
    if stats["checked"] or status == "OK":
        verified["last_run"] = TODAY   # a fully blocked week tries again at the next wake-up
    verified["about"] = ("Written by scripts/tcgplayer_check.py: real TCGplayer sales per condition for skewed and "
                         "top-flip cards. Keys are '<tcgplayer id>|<printing>'. The daily update keeps these prices "
                         "while they're fresh.")

    icon = {"OK": "✅", "PARTIAL": "⚠️", "BLOCKED": "❌"}[status]
    L = [f"## {icon} {status} — TCGplayer sales check {TODAY}" + (" (test run, not saved)" if DRY else ""), ""]
    if stop:
        L += [f"Stopped early: {stop}.", ""]
    L += [
        f"- Cards looked up on TCGplayer: **{len(todo)}** in {secs // 60}m {secs % 60}s"
        + (f" (another {left_out} waited for next week)" if left_out else ""),
        f"- ✅ Prices checked against real sales: **{stats['checked']}**",
        f"- ➖ No sales in {cfg['sales_days']} days (kept old prices): {stats['nosales']}",
        f"- ❌ Blocked by TCGplayer: **{stats['blocked']}**",
        f"- ⚠️ Errors: {stats['error']} · no history: {stats['nodata']} · printing unclear: {stats['noprinting']}",
    ]
    if changes:
        L += ["", f"**Prices that moved 15%+ after checking real sales ({len(changes)}):**"]
        L += [f"- {n} [{', '.join(w)}]: " + ", ".join(d) for n, w, d in changes[:25]]
        if len(changes) > 25:
            L.append(f"- …and {len(changes) - 25} more")
    if problems:
        L += ["", f"**Problem cards ({len(problems)}):**"] + [f"- {k}: {n} — {w}" for k, n, w in problems[:20]]
    section = "\n".join(L) + "\n"
    open(rp.REPORT, "w", encoding="utf-8").write(section)
    print(section)

    saved = not DRY and (stats["checked"] > 0 or "last_run" in verified)
    if not DRY:
        with open(VERIFIED, "w", encoding="utf-8") as f:
            json.dump(verified, f, ensure_ascii=False, indent=0, sort_keys=True)
        files, cands = load_all()
        n, dirty, cd = apply_all(files, cands, rp.load_verified())
        save_all(files, dirty, cands, cd)
        prepend_log(section)
        open(SECTION, "w", encoding="utf-8").write(section)
    output(status=status, ran="true", saved="true" if saved else "false")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(section)
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
