#!/usr/bin/env python3
"""Weekly raw-price refresh for Chaundoo Card Flipz (runs in GitHub Actions).

For every card with a tcgId it pulls TCGplayer's 90-day per-condition sales
history and refreshes ONLY raw.nm / raw.lp / raw.mp / raw.dmg, plus the
"TCGplayer sales (90 days)" liquidity line and "updated". Everything else
(graded, pop, gem, verdict, notes, images, pairings) is left exactly as is.

Cards are stored one file per era: eras/<id>/<id>.json, listed in
eras/eras.json. Only the era files that actually changed are rewritten.

It also records how every request went (ok / no sales / blocked / error),
writes PRICE_LOG.md and run_report.md, and exits with code 1 when TCGplayer
blocked requests, so GitHub emails you.

Settings (environment variables):
  LIMIT    only check the first N cards (0 = all)   default 0
  DRY_RUN  "true" = check prices but don't save the card files
"""
import json, os, sys, time, random, threading, datetime, re
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERAS_DIR = os.path.join(ROOT, "eras")
MANIFEST = os.path.join(ERAS_DIR, "eras.json")
LOG = os.path.join(ROOT, "PRICE_LOG.md")
REPORT = os.path.join(ROOT, "run_report.md")

LIMIT = int(os.environ.get("LIMIT") or 0)
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() == "true"
WORKERS = 3            # gentle: ~5-7 requests per second total
PAUSE = 0.4            # seconds each worker waits between cards
KEEP_LOG_RUNS = 30     # PRICE_LOG.md keeps the latest 30 runs

URL = "https://infinite-api.tcgplayer.com/price/history/{id}/detailed?range=quarter"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
}
COND = {"Near Mint": "nm", "Lightly Played": "lp", "Moderately Played": "mp", "Damaged": "dmg"}
LABEL = {"nm": "NM", "lp": "LP", "mp": "MP", "dmg": "DMG"}

TODAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8))).date()  # Pacific-ish


class Blocked(Exception):
    pass


class NoData(Exception):
    pass


# ---------------------------------------------------------------- fetching
def fetch(tcg_id):
    """Return the parsed JSON for one product. Raises Blocked / NoData / Exception."""
    last = None
    for attempt in range(4):
        req = urllib.request.Request(URL.format(id=tcg_id), headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                # 200 but HTML = a bot-check / challenge page
                raise Blocked("got a web page instead of data (bot check)")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NoData("TCGplayer has no price history for this product (404)")
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
        except Exception as e:  # timeouts, DNS, resets
            last = Exception(f"network error: {e}")
        time.sleep([2, 6, 15, 0][attempt] + random.random())
    raise last


# ---------------------------------------------------------------- matching the printing
def wanted_printing(label):
    l = label.lower()
    holo = "holo" in l and "non-holo" not in l
    if "reverse" in l:
        return "Reverse Holofoil"
    if "1st edition" in l:
        return "1st Edition Holofoil" if holo else "1st Edition"
    if "unlimited" in l:
        return "Unlimited Holofoil" if holo else "Unlimited"
    if "non-holo" in l or "normal" in l:
        return "Normal"
    return "Holofoil"


def pick_variant(card, variants):
    """variants: {printing: {cond: {...}}}. Returns printing name or None."""
    if not variants:
        return None
    saved = card.get("tcgVariant")
    if saved in variants:
        return saved
    want = wanted_printing(card.get("variant", ""))
    if want in variants:
        return want
    if len(variants) == 1:
        return next(iter(variants))
    # Several printings and none matches the label: pick the one whose prices
    # are closest to what the card already shows.
    best, best_d = None, None
    for v, conds in variants.items():
        d, n = 0.0, 0
        for c, info in conds.items():
            old = (card.get("raw", {}).get(c) or {}).get("price")
            if isinstance(old, (int, float)) and old > 0 and info["market"] > 0:
                d += abs(info["market"] - old) / old
                n += 1
        if n and (best_d is None or d / n < best_d):
            best, best_d = v, d / n
    return best


def parse(j):
    out = {}
    for r in (j or {}).get("result") or []:
        if r.get("language", "English") != "English":
            continue
        c = COND.get(r.get("condition"))
        if not c:
            continue
        buckets = r.get("buckets") or []
        sales = [b for b in buckets if float(b.get("quantitySold") or 0) > 0]
        out.setdefault(r.get("variant") or "Normal", {})[c] = {
            "market": float(buckets[0].get("marketPrice") or 0) if buckets else 0.0,
            "qty": int(float(r.get("totalQuantitySold") or 0)),
            "sales": [(b["bucketStartDate"][:10], float(b.get("lowSalePrice") or 0),
                       float(b.get("highSalePrice") or 0), int(float(b.get("quantitySold") or 0)))
                      for b in sales[:3]],
        }
    return out


def money(x):
    return f"${x:,.2f}"


def build_raw(info):
    """New raw.<cond> dict from one condition's data, or None if no sales in 90 days."""
    if not info["sales"] or info["qty"] < 1:
        return None
    pts = [(lo + hi) / 2 for _, lo, hi, _ in info["sales"]]
    avg = sum(pts) / len(pts)
    m = info["market"]
    parts = []
    for d, lo, hi, q in info["sales"]:
        rng = money(lo) if abs(lo - hi) < 0.005 else f"{money(lo)}–{money(hi)}"
        parts.append(f"sold {rng} ~{d[5:7]}/{d[8:10]}" + (f" x{q}" if q > 1 else ""))
    sold = ", ".join(parts)
    if m <= 0 or abs(m - avg) / avg > 0.4:
        price = round(avg, 2)
        src = f"TCGplayer avg of recent sales; market {money(m)} lags ({sold}; {info['qty']} sales/qtr)"
        est = True
    else:
        price = round(m, 2)
        src = f"TCGplayer market ({sold}; {info['qty']} sales/qtr)"
        est = info["qty"] < 3
    return {"price": price, "date": info["sales"][0][0], "src": src, "est": est}


# ---------------------------------------------------------------- main
def main():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    files = []      # [(path, data)]
    file_of = {}    # card id -> index in files
    all_cards = []
    for era in manifest["eras"]:
        path = os.path.join(ERAS_DIR, era["id"], era["id"] + ".json")
        if not os.path.exists(path):
            continue
        data = json.load(open(path, encoding="utf-8"))
        files.append((path, data))
        for c in data.get("cards", []):
            file_of[c["id"]] = len(files) - 1
            all_cards.append(c)
    dirty = set()
    cards = [c for c in all_cards if c.get("tcgId")]
    if LIMIT:
        cards = cards[:LIMIT]
    results = {}
    lock = threading.Lock()
    stop = threading.Event()
    counter = {"done": 0, "blocked": 0, "streak": 0}

    def work(card):
        if stop.is_set():
            return
        try:
            res = ("ok", parse(fetch(card["tcgId"])))
        except Blocked as e:
            res = ("blocked", str(e))
        except NoData as e:
            res = ("nodata", str(e))
        except Exception as e:
            res = ("error", str(e))
        with lock:
            results[card["id"]] = res
            counter["done"] += 1
            if res[0] == "blocked":
                counter["blocked"] += 1
                counter["streak"] += 1
            else:
                counter["streak"] = 0
            # Clearly blocked: stop early instead of hammering TCGplayer.
            if (counter["done"] >= 30 and counter["blocked"] / counter["done"] >= 0.9) or counter["streak"] >= 60:
                stop.set()
        time.sleep(PAUSE * (1 + random.random() * 0.75))

    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, cards))
    secs = int(time.time() - t0)

    # apply
    stats = {"updated": 0, "nosales": 0, "nomatch": 0, "nodata": 0, "blocked": 0, "error": 0, "skipped": 0}
    problems, moves = [], []
    for card in cards:
        r = results.get(card["id"])
        name = f"{card['name']} ({card.get('variant','')}) — {card.get('set','')} {card.get('number','')}"
        if r is None:
            stats["skipped"] += 1
            continue
        kind, payload = r
        if kind != "ok":
            stats[kind] += 1
            problems.append((kind, name, payload))
            continue
        if not payload:
            stats["nodata"] += 1
            problems.append(("nodata", name, "TCGplayer returned no sales data for this product"))
            continue
        v = pick_variant(card, payload)
        if not v:
            stats["nomatch"] += 1
            problems.append(("nomatch", name, "couldn't tell which printing this card is"))
            continue
        changed = False
        raw = card.setdefault("raw", {})
        for c in ("nm", "lp", "mp", "dmg"):
            info = payload[v].get(c)
            new = build_raw(info) if info else None
            if not new:
                continue  # no sales this quarter: keep the old price
            old = (raw.get(c) or {}).get("price")
            if c == "nm" and isinstance(old, (int, float)) and old > 0 and abs(new["price"] - old) / old >= 0.15:
                moves.append((name, old, new["price"]))
            raw[c] = new
            changed = True
        if card.get("liquidity", "").startswith("TCGplayer sales (90 days)") or not card.get("liquidity"):
            q = {c: (payload[v].get(c) or {}).get("qty", 0) for c in ("nm", "lp", "mp", "dmg")}
            card["liquidity"] = f"TCGplayer sales (90 days): NM {q['nm']} · LP {q['lp']} · MP {q['mp']} · DMG {q['dmg']}"
        card["tcgVariant"] = v
        if changed:
            card["updated"] = TODAY.isoformat()
            dirty.add(file_of[card["id"]])
            stats["updated"] += 1
        else:
            stats["nosales"] += 1

    checked = len(cards) - stats["skipped"]
    reached = checked - stats["blocked"] - stats["error"]
    if stop.is_set() and reached < len(cards) * 0.5:
        status = "BLOCKED"
    elif stats["blocked"] or stats["error"] > max(5, len(cards) * 0.02):
        status = "PARTIAL"
    else:
        status = "OK"
    icon = {"OK": "✅", "PARTIAL": "⚠️", "BLOCKED": "❌"}[status]
    saved = (status != "BLOCKED") and not DRY_RUN and stats["updated"] > 0

    if saved:
        stamp = str(int(time.time()))
        for i in sorted(dirty):
            path, data = files[i]
            data["version"] = stamp
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # ---- report
    head = {
        "OK": "TCGplayer let the requests through.",
        "PARTIAL": "Some requests went through, but TCGplayer blocked or failed others (their old prices were kept).",
        "BLOCKED": "TCGplayer blocked the requests. Nothing was changed on the site.",
    }[status]
    lines = [f"## {icon} {status} — price refresh {TODAY.isoformat()}" + (" (test run, not saved)" if DRY_RUN else ""), "",
             head, "",
             f"- Cards checked: **{checked}** of {len(cards)}" + (f" (limited to first {LIMIT})" if LIMIT else "") + f" in {secs // 60}m {secs % 60}s",
             f"- ✅ Prices updated: **{stats['updated']}**",
             f"- ➖ Reached, but no sales in 90 days (kept old prices): {stats['nosales']}",
             f"- ❌ Blocked by TCGplayer: **{stats['blocked']}**",
             f"- ⚠️ Other errors (timeouts, server errors): {stats['error']}",
             f"- ❔ No price history on TCGplayer: {stats['nodata']}",
             f"- ❔ Couldn't match the printing: {stats['nomatch']}",
             ]
    if stats["skipped"]:
        lines.append(f"- ⏹️ Not checked (run stopped early): {stats['skipped']}")
    lines.append(f"- Era files saved: **{len(dirty) if saved else 'none'}**" + (f" of {len(files)}" if saved else ""))
    if moves:
        moves.sort(key=lambda m: -abs(m[2] - m[1]) / m[1])
        lines += ["", "**Biggest NM moves (15%+):**"] + [f"- {n}: {money(o)} → {money(nw)}" for n, o, nw in moves[:10]]
    if problems:
        lines += ["", f"**Problem cards ({len(problems)}):**"]
        for kind, n, why in problems[:40]:
            lines.append(f"- {kind}: {n} — {why}")
        if len(problems) > 40:
            lines.append(f"- …and {len(problems) - 40} more (see the Actions run log)")
            for kind, n, why in problems[40:]:
                print(f"{kind}: {n} — {why}")
    report = "\n".join(lines) + "\n"
    open(REPORT, "w", encoding="utf-8").write(report)
    print(report)

    if not DRY_RUN:
        old = open(LOG, encoding="utf-8").read() if os.path.exists(LOG) else ""
        runs = [r.rstrip() + "\n" for r in re.findall(r"(?ms)^## .*?(?=^## |\Z)", old)]
        body = "# Price refresh log\n\nNewest first. Written automatically by the weekly GitHub job.\n\n" + \
               "\n".join([report] + runs[:KEEP_LOG_RUNS - 1])
        open(LOG, "w", encoding="utf-8").write(body)

    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\nsaved={'true' if saved else 'false'}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(report)
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
