#!/usr/bin/env python3
"""Sleepers: older, pricier cards that hide behind a cheap printing on TCGplayer and barely sell.

The pattern (e.g. Torchic 69/106 EX Emerald):
  * TCGplayer lists every printing of a card on ONE product page and opens it on the cheapest one
    (Torchic normal, $0.93). The expensive printing (the Emerald-stamped reverse holo, $160.99) only
    shows up after you pick it under Printing, so it gets little attention and few sales.
  * TCG Price Lookup returns one record per printing, so the Flip Finder's set scans already see both.
    A product whose expensive printing is alt_ratio times (or more) its cheap printing is a "hidden printing".
  * The weekly TCGplayer sales check then counts that printing's real sales (90 days): how many, when the
    last one was, and how far apart the prices were. Few sales + hidden printing = a sleeper.
The eBay side is checked by hand: every sleeper gets a PriceCharting link (its eBay sold list) and an
eBay sold-listings link on the site.

Files
  scripts/sleeper_config.json   settings (eras, price range, hidden gap, how many to check)
  scripts/sleeper_state.json    written by the Flip Finder: the sleeper list + hidden printings it saw
  scripts/tcgplayer_verified.json  the sales check saves each printing's sales counts here ("sales")
  sleepers.json                 what the site reads (built from the three above by build_output())

Who calls what
  discover_candidates.py  scan_set() for every scanned set, then merge() + save_state() + build_output()
  tcgplayer_check.py      check_picks() to add sleepers / older site cards, sales_stats() per card,
                          build_output() after saving
  approve_candidates.py   drop picked sleepers from the list, sleeper settings, build_output()
Run by hand to rebuild sleepers.json:  python3 scripts/sleepers.py
"""
import json, os, re, sys, math, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp   # price helpers, era files, TODAY
import add_card as ac         # labels, slugs, era guess

ROOT = rp.ROOT
CFG_PATH = os.path.join(ROOT, "scripts", "sleeper_config.json")
STATE = os.path.join(ROOT, "scripts", "sleeper_state.json")
OUT = os.path.join(ROOT, "sleepers.json")
TODAY = rp.TODAY
PREFIX = "slp-"          # sleeper ids on the list (taken off when a card is added to the site)
EXTRA = ("era", "found", "pkey", "setSlug", "score", "flags", "pop", "hid", "low")   # list-only fields

DEFAULT_CFG = {
    "eras": ["wotc", "ex", "dp-hgss"], "extra_sets": ["^Nintendo Promos$", "^Kids WB Promos$"],
    "min_price": 20, "max_price": 0, "alt_ratio": 3.0, "require_popular": True, "skip_stamped": True,
    "max_candidates": 60, "max_age_days": 30, "stale_days": 14, "snooze_days": 30,
    "thin": {"very_low_max_sales": 2, "low_max_sales": 6, "quiet_days": 30, "wide_spread": 1.8},
    "check_candidates": 25, "check_site_cards": 35, "recheck_days": 21,
}
HISTORY_COND = {"Near Mint": "nm", "Lightly Played": "lp", "Moderately Played": "mp",
                "Heavily Played": "hp", "Damaged": "dmg"}


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    got = jload(CFG_PATH, {})
    for k, v in got.items():
        if k == "thin" and isinstance(v, dict):
            cfg["thin"].update(v)
        elif k in cfg:
            cfg[k] = v
    return cfg


def settings_of(cfg):
    """What the site shows (and its Sleepers panel can change)."""
    return {"eras": cfg["eras"], "extra_sets": cfg["extra_sets"], "min_price": cfg["min_price"],
            "max_price": cfg["max_price"], "alt_ratio": cfg["alt_ratio"], "thin": cfg["thin"],
            "stale_days": cfg["stale_days"]}


def days_ago(d):
    try:
        return (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(str(d)[:10])).days
    except (TypeError, ValueError):
        return 9999


# ---------------------------------------------------------------- prices
def ref_price(prices):
    """Near Mint price of one printing, or an estimate from the best condition it has.
    prices: {'nm': (price, est), ...}  ->  (price, condition used) or None"""
    for k in ("nm", "lp", "mp", "dmg"):
        if k in prices and prices[k][0] and prices[k][0] > 0:
            p = prices[k][0] if k == "nm" else prices[k][0] / rp.RATIOS.get(k, rp.DEFAULT_RATIOS[k])
            return round(p, 2), k
    return None


def ref_of_card(c):
    """ref_price() for a saved card / candidate (raw entries)."""
    p = {k: (e["price"], bool(e.get("est"))) for k, e in (c.get("raw") or {}).items()
         if isinstance(e, dict) and isinstance(e.get("price"), (int, float)) and e["price"] > 0}
    r = ref_price(p)
    return r[0] if r else 0


def in_older(set_name, era, cfg):
    """Is this set one of the 'older' ones sleepers are about?"""
    if era in (cfg.get("eras") or []):
        return True
    for p in cfg.get("extra_sets") or []:
        try:
            if re.search(p, set_name or "", re.I):
                return True
        except re.error:
            pass
    return False


def pre_score(ratio, price, popular):
    """Rank before sales are counted: bigger hidden gap first, then popularity, then price."""
    return round(min(70.0, 10 * math.log2(max(ratio, 1.0))) + (10 if popular else 0) + 5 * math.log10(max(price, 1.0)), 1)


# ---------------------------------------------------------------- one set (called by the Flip Finder)
def scan_set(recs, set_name, slug, era, ctx, cfg, hidden, dc):
    """recs: every TCG Price Lookup record of one set (one per printing). Records hidden printings in
    `hidden` (all of them, site cards too) and returns new sleeper candidates (cards not on the site).
    ctx / dc: the Flip Finder's context and module (site cards, hidden/declined cards, trainer + stamp checks)."""
    groups = {}
    for r in recs:
        try:
            tid = int(r.get("tcgplayer_id"))
        except (TypeError, ValueError):
            continue
        groups.setdefault(tid, []).append(r)
    hp_ok = any(r.get("hp") for r in recs)
    out = []
    for tid, rs in groups.items():
        if len(rs) < 2:
            continue
        priced = []
        for r in rs:
            prices, fixes = rp.checked_prices(r)
            ref = ref_price(prices)
            if ref:
                priced.append((r, prices, fixes, ref))
        if len(priced) < 2:
            continue
        cheap = min(priced, key=lambda x: x[3][0])
        cheap_v = cheap[0].get("variant") or "Normal"
        for r, prices, fixes, ref in priced:
            if r is cheap[0]:
                continue
            ratio = ref[0] / max(cheap[3][0], 0.01)
            if ratio < float(cfg["alt_ratio"]) or ref[0] < float(cfg["min_price"]):
                continue
            if dc.skip_reason(r, ctx.cfg, hp_ok):
                continue                       # trainers / energy
            printing = r.get("variant") or "Normal"
            key = f"{tid}|{printing.lower()}"
            hidden[key] = {"cv": ac.LABELS.get(cheap_v.lower(), cheap_v), "cp": round(cheap[3][0], 2),
                           "r": round(ratio, 1), "p": ref[0], "d": TODAY}
            # ---- a candidate for the list?
            if (str(tid), printing.lower()) in ctx.have or r.get("id") in ctx.dismissed or r.get("id") in ctx.declined:
                continue
            if cfg.get("max_price") and ref[0] > float(cfg["max_price"]):
                continue
            look = {"name": r.get("name") or "", "variant": printing}
            popular = dc.is_popular(look, ctx)
            if cfg.get("require_popular", True) and not popular:
                continue
            if cfg.get("skip_stamped", True) and dc.stamped(look, ctx.cfg):
                continue
            when = (r.get("last_price_update") or r.get("updated_at") or TODAY)[:10]
            raw = {k: rp.raw_entry(k, prices[k], when, fixes) for k in ("nm", "lp", "mp", "dmg") if k in prices}
            card_like = {"tcgId": tid, "tcgVariant": printing}
            rp.overlay_verified(card_like, raw, ctx.verified)
            lows = rp.listing_lows(r)
            low = min(lows.items(), key=lambda kv: kv[1]) if lows else None
            base = "-".join(x for x in (ac.slugify(r.get("name")), ac.slugify(r.get("number")), str(tid), ac.slugify(printing)[:10]) if x)
            out.append({
                "id": PREFIX + base, "name": r.get("name") or "", "variant": ac.LABELS.get(printing.lower(), printing),
                "set": set_name, "number": r.get("number") or "", "raw": raw, "image": r.get("image_url") or "",
                "updated": TODAY, "tcgId": tid, "tcgVariant": printing, "era": era, "found": TODAY,
                "pkey": r.get("id"), "setSlug": slug, "pop": bool(popular),
                "hid": {"cv": ac.LABELS.get(cheap_v.lower(), cheap_v), "cp": round(cheap[3][0], 2), "r": round(ratio, 1)},
                "low": {"c": low[0], "p": round(low[1], 2)} if low else None,
                "score": pre_score(ratio, ref[0], popular),
            })
    return out


# ---------------------------------------------------------------- the list
def fits(c, cfg):
    p = ref_of_card(c)
    if p < float(cfg["min_price"]) or (cfg.get("max_price") and p > float(cfg["max_price"])):
        return False
    if (c.get("hid") or {}).get("r", 0) < float(cfg["alt_ratio"]):
        return False
    return in_older(c.get("set"), c.get("era"), cfg)


def merge(prev, fresh, rescanned, have, dismissed, declined, dstate, cfg):
    """Combine this run's sleepers with the list from before. Returns (list, snoozed)."""
    fresh_by = {(c["tcgId"], c["tcgVariant"].lower()): c for c in fresh}
    keep = []
    for c in prev:
        k = (c.get("tcgId"), str(c.get("tcgVariant", "")).lower())
        if k in fresh_by:
            fresh_by[k]["found"] = c.get("found") or fresh_by[k]["found"]   # keep first-seen date
            fresh_by[k]["id"] = c["id"]
            continue
        if (str(c.get("tcgId")), k[1]) in have or c.get("pkey") in dismissed or c.get("pkey") in declined:
            continue                                  # added to the site, hidden or declined
        if c.get("setSlug") in rescanned:
            continue                                  # its set was scanned again and it no longer qualifies
        if days_ago(c.get("updated") or "") > int(cfg["max_age_days"]):
            continue                                  # prices too old
        if not fits(c, cfg):
            continue                                  # outside the sleeper settings now
        keep.append(c)
    allc = keep + list(fresh_by.values())
    snoozed = []
    stale, snooze = int(cfg.get("stale_days") or 0), int(cfg.get("snooze_days") or 30)
    if stale > 0:
        until = (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=snooze)).isoformat()
        dstate.setdefault("declined", {})
        left = []
        for c in allc:
            if days_ago(c.get("found") or TODAY) >= stale and c.get("pkey"):
                dstate["declined"][c["pkey"]] = {"until": until, "auto": True, "sleeper": True,
                                                 "card": f"{c['name']} ({c['variant']}) {c['set']} {c['number']}"}
                snoozed.append(c)
            else:
                left.append(c)
        allc = left
    verified = jload(rp.VERIFIED, {}).get("cards", {})

    def rank(c):
        s = sales_of(verified.get(rp.printing_key(c) or "") or {})
        t = thin_score(s, c.get("hid"), c.get("pop"), cfg) if s else None
        return (t is not None, t or 0, c.get("score") or 0)
    allc.sort(key=rank, reverse=True)
    return allc[:int(cfg["max_candidates"])], snoozed


def load_state():
    st = jload(STATE, {})
    st.setdefault("cands", [])
    st.setdefault("hidden", {})
    return st


def save_state(st):
    st["hidden"] = {k: v for k, v in st.get("hidden", {}).items() if days_ago(v.get("d", "")) <= 60}
    st["about"] = ("Written by scripts/discover_candidates.py (sleeper list + hidden printings it saw while scanning sets) "
                   "and scripts/approve_candidates.py (picked sleepers come off). sleepers.json is built from this.")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0, sort_keys=True)


# ---------------------------------------------------------------- sales (called by the TCGplayer check)
def sales_stats(rows, days=90):
    """rows: TCGplayer price-history rows for ONE printing (one per condition).
    Returns {'n': sales in `days`, 'last': newest sale date, 'c': {cond: [count, low, high]}}."""
    since = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days)).isoformat()
    n, last, per = 0, "", {}
    for r in rows:
        c = HISTORY_COND.get(r.get("condition"), "other")
        for b in r.get("buckets") or []:
            d = (b.get("bucketStartDate") or "")[:10]
            try:
                q = int(float(b.get("quantitySold") or 0))
                lo, hi = float(b.get("lowSalePrice") or 0), float(b.get("highSalePrice") or 0)
            except (TypeError, ValueError):
                continue
            if q <= 0 or lo <= 0 or d < since:
                continue
            hi = hi or lo
            n += q
            last = max(last, d)
            e = per.setdefault(c, [0, lo, hi])
            e[0] += q
            e[1], e[2] = min(e[1], lo), max(e[2], hi)
    return {"n": n, "last": last or None, "c": {k: [v[0], round(v[1], 2), round(v[2], 2)] for k, v in per.items()}, "at": TODAY}


ODD = re.compile(r"\$[\d,]+(?:\.\d+)?")


def approx_sales(v):
    """Older checks saved only prices; their notes say how many sales each condition had, so use those."""
    n, last, per = 0, "", {}
    for c, e in (v.get("prices") or {}).items():
        if not isinstance(e, dict):
            continue
        src = str(e.get("src") or "")
        m = re.search(r"median of (\d+) sale", src)
        k = int(m.group(1)) if m else 0
        odd = re.search(r"odd sales?:? ([^)]*)\)", src) or re.search(r"odd (\$[^ ]+) sale", src)
        if odd:
            k += len(ODD.findall(odd.group(1))) or 1
        if not m and not odd and "sales showed" in src:
            k = max(k, 1)
        if not k:
            continue
        n += k
        p = e.get("price")
        per[c] = [k, p, p]
        if not e.get("est") and e.get("date"):
            last = max(last, str(e["date"])[:10])
    if not n:
        return None
    return {"n": n, "last": last or None, "c": per, "ap": 1}


def sales_of(v):
    """Sales stats saved for one printing (exact counts, or estimated from an older check's notes)."""
    if not v:
        return None
    s = v.get("sales")
    if s is None:
        s = approx_sales(v)
    if s is None:
        return None
    s = dict(s)
    s["at"] = (s.get("at") or v.get("checked") or "")[:10]
    # "Market lags sales": each condition's median sale (odd sales thrown out) with how many sales and the
    # newest, so the site can compare it with TCGplayer's market price: {cond: [median, sales, date]}
    m = {}
    for c, e in (v.get("prices") or {}).items():
        hit = re.match(r"TCGplayer sales: median of (\d+) sale", str((e or {}).get("src") or ""))
        if hit and isinstance(e.get("price"), (int, float)) and not e.get("est"):
            m[c] = [round(e["price"], 2), int(hit.group(1)), str(e.get("date") or "")[:10]]
    if m:
        s["m"] = m
    return s


def quiet_days(s):
    """Days without a sale at the time the sales were counted (None = no sale in 90 days)."""
    if not s.get("last"):
        return None
    try:
        ref = datetime.date.fromisoformat((s.get("at") or TODAY)[:10])
        return max(0, (ref - datetime.date.fromisoformat(s["last"][:10])).days)
    except ValueError:
        return None


def spread(s):
    """Biggest high/low gap between sales of the same condition (needs 2+ sales)."""
    best = None
    for k, (n, lo, hi) in (s.get("c") or {}).items():
        if n >= 2 and lo and hi and lo > 0:
            best = max(best or 0, hi / lo)
    return best


def thin_score(s, hid, pop, cfg):
    """0-100: how quiet the market is (few sales, long since the last one, prices all over, hidden printing)."""
    n = s.get("n") or 0
    d = quiet_days(s)
    sp = spread(s)
    a = 40 if n == 0 else 35 if n == 1 else 30 if n == 2 else 22 if n <= 4 else 12 if n <= 8 else 5 if n <= 15 else 0
    b = 25 if d is None or d >= 60 else 18 if d >= 30 else 10 if d >= 14 else 3
    c = 0 if sp is None else 15 if sp >= 2.5 else 10 if sp >= cfg["thin"]["wide_spread"] else 5 if sp >= 1.3 else 0
    r = (hid or {}).get("r") or 0
    e = 15 if r >= 10 else 12 if r >= 5 else 8 if r >= 3 else 0
    return min(100, a + b + c + e + (5 if pop else 0))


def level(s, cfg):
    """'very' (very low sales), 'low' or None."""
    if not s:
        return None
    t = cfg["thin"]
    n = s.get("n") or 0
    d = quiet_days(s)
    d = 9999 if d is None else d
    if n <= int(t["very_low_max_sales"]):
        return "very"
    if n <= int(t["low_max_sales"]) or d >= int(t["quiet_days"]):
        return "low"
    return None


def site_category(c, cfg):
    """An older, pricier card already on the site: the kind that gets a Low sales flag."""
    return in_older(c.get("set"), c.get("era"), cfg) and ref_of_card(c) >= float(cfg["min_price"])


def check_picks(verified, cfg):
    """[(card, why)] the weekly TCGplayer check should count sales for: sleeper candidates first,
    then older site cards (hidden printings first, then the priciest)."""
    st = load_state()

    def due(c):
        v = verified.get(rp.printing_key(c) or "") or {}
        at = (v.get("sales") or {}).get("at") or v.get("checked", "")
        return not ("sales" in v and days_ago(at) < int(cfg["recheck_days"]))
    out = []
    cands = sorted(st["cands"], key=lambda c: -(c.get("score") or 0))
    out += [(c, "sleeper") for c in cands if due(c)][:int(cfg["check_candidates"])]
    site = [c for c in site_cards_all() if c.get("tcgId") and site_category(c, cfg) and due(c)]
    site.sort(key=lambda c: (rp.printing_key(c) in st["hidden"], ref_of_card(c)), reverse=True)
    out += [(c, "older card sales") for c in site[:int(cfg["check_site_cards"])]]
    return out


# ---------------------------------------------------------------- sleepers.json (what the site reads)
def site_cards_all():
    manifest = jload(rp.MANIFEST, {"eras": []})
    out = []
    for e in manifest["eras"]:
        d = jload(os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json"), {})
        for c in d.get("cards", []):
            c = dict(c)
            c["era"] = e["id"]
            out.append(c)
    return out


def build_output():
    """Write sleepers.json. Only rewritten (new version) when something in it changed."""
    cfg = load_cfg()
    st = load_state()
    verified = jload(rp.VERIFIED, {}).get("cards", {})
    site = site_cards_all()
    have = {(str(c.get("tcgId") or ""), ac.printing_of(c)) for c in site if c.get("tcgId")}
    cands = [c for c in st["cands"] if (str(c.get("tcgId")), str(c.get("tcgVariant", "")).lower()) not in have]
    keys = {rp.printing_key(c) for c in site + cands} - {None}
    sales = {}
    for k in sorted(keys):
        s = sales_of(verified.get(k) or {})
        if s:
            sales[k] = s
    hidden = {k: {kk: v[kk] for kk in ("cv", "cp", "r")} for k, v in st["hidden"].items() if k in keys}
    body = {"generated": TODAY, "settings": settings_of(cfg), "cards": cands, "sales": sales, "hidden": hidden}
    old = jload(OUT, {})
    same = {k: old.get(k) for k in ("settings", "cards", "sales", "hidden")} == \
           {k: body[k] for k in ("settings", "cards", "sales", "hidden")}
    if same and old.get("version"):
        return False
    body = dict({"version": str(int(time.time()))}, **body)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, separators=(",", ":"))
    return True


def summary():
    """A few plain-English facts about sleepers.json (for reports)."""
    d = jload(OUT, {})
    cfg = load_cfg()
    site = site_cards_all()
    flagged = 0
    for c in site:
        s = (d.get("sales") or {}).get(rp.printing_key(c) or "")
        if s and site_category(c, cfg) and level(s, cfg):
            flagged += 1
    return {"candidates": len(d.get("cards") or []), "counted": len(d.get("sales") or {}), "flagged": flagged}


if __name__ == "__main__":
    changed = build_output()
    print(("sleepers.json updated: " if changed else "sleepers.json already up to date: ") + json.dumps(summary()))
