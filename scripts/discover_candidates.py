#!/usr/bin/env python3
"""Flip Finder: look for NEW cards worth flipping and list them for your approval.

Runs in GitHub Actions on Monday-Thursday only (the days you are not at card shows), after the
daily price update, so it uses API requests the price update and your show-day card adds don't need.

What it does
  1. Gets the list of sets from TCG Price Lookup (one request, remembered for 30 days) and keeps the
     2010-2020 era sets (settings in scripts/discovery_config.json).
     Which eras to scan, and the profit / return / buy-price limits, can be changed from the site's
     Flip Finder tab ("What to look for"); that saves them into scripts/discovery_config.json.
  2. Scans the sets that were scanned longest ago (never-scanned first) until the request budget is
     used up. Every card is checked at the vendor rate in the settings (default 70%).
  3. Keeps cards with a good condition flip: buy one condition, sell one step up to a vendor, buy price
     over $5, good percentage return, no trainers/energy, not already on the site, not hidden by you.
     Only popular Pokémon (the "popular" list in the settings) and no stamped cards (prerelease, staff,
     league, championship...), since vendors rarely want those.
     Cards you declined ("Not for me" on the site) stay away for decline_days, and the finder learns
     from your picks: Pokémon, sets and card types you keep declining rank lower, ones you add rank higher.
     Cards that sit on the list untouched for stale_days (default 3) are taken off and marked
     "not interested for now" for snooze_days (default 30), so the list stays fresh.
  4. Writes candidates.json (ranked, with the condition prices). The site's "Flip Finder" tab shows it and
     you choose which cards move into the site. Nothing is added to the site by this script.

Needs the same TCGPL_API_KEY secret as the daily price update.

Settings (environment variables, all optional)
  DRY_RUN     "true" = look, print a report, save nothing
  FORCE       "true" = run even on Friday-Sunday (manual runs)
  DISC_REQUESTS   max API requests this run (default from config, 60)
  PAUSE_MIN / PAUSE_MAX   seconds between requests (same as the price update)
"""
import json, os, re, sys, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp   # API client + price helpers
import add_card as ac         # era guess + printing labels

ROOT = rp.ROOT
CFG_PATH = os.path.join(ROOT, "scripts", "discovery_config.json")
DSTATE = os.path.join(ROOT, "scripts", "discovery_state.json")
CANDS = os.path.join(ROOT, "candidates.json")
PT = datetime.timezone(datetime.timedelta(hours=-8))
SCAN_DAYS = (0, 1, 2, 3)          # Monday..Thursday (Pacific)
FLIPS = [("dmg", "mp"), ("mp", "lp"), ("lp", "nm")]
FORCE = (os.environ.get("FORCE") or "").lower() == "true"
DRY = (os.environ.get("DRY_RUN") or "").lower() == "true"
TODAY = rp.TODAY


class DApi(rp.Api):
    """Same client as the price update; keeps today's reserve from scripts/api_budget.json free."""


def load_cfg():
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    budget = rp.load_budget()
    cfg["daily_requests"] = int(os.environ.get("DISC_REQUESTS") or budget["flip_finder_max_requests"])
    cfg["reserve_requests"] = int(budget["keep_free_weekdays"])
    cfg["reserve_requests_show_days"] = int(budget["keep_free_show_days"])
    return cfg


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jdump(path, obj, **kw):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, **kw)


def days_ago(d):
    try:
        return (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(d[:10])).days
    except (TypeError, ValueError):
        return 9999


def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


# ---------------------------------------------------------------- sets
def set_year(s):
    for k in ("release_date", "released_at", "releaseDate", "released", "date"):
        v = s.get(k)
        if isinstance(v, str) and re.match(r"\d{4}", v):
            return int(v[:4])
    return None


YEAR_ERAS = [(2003, "wotc"), (2007, "ex"), (2010, "dp-hgss"), (2013, "bw"), (2016, "xy"), (2019, "sm"),
             (2022, "swsh"), (2024, "sv"), (9999, "me")]


def set_era(name, year=None, manifest=None):
    """Era id for a set: the site's era name patterns first, then the release year."""
    manifest = manifest or jload(rp.MANIFEST, {"eras": []})
    e = ac.era_for(name, manifest, {})
    if e != "other" or not year:
        return e
    return next(eid for y, eid in YEAR_ERAS if year <= y)


def settings_of(cfg):
    """The settings the site shows and can change (also saved into candidates.json)."""
    return {k: cfg.get(k) for k in ("eras", "min_net", "min_roi_pct", "min_buy", "max_buy", "stale_days", "snooze_days", "vendor_pct")}


def fits(c, cfg):
    """Does a listed card still match the current settings (era + a flip inside the limits)?"""
    eras = cfg.get("eras") or []
    if eras and c.get("era") and c["era"] not in eras:
        return False
    r = routes({k: (v["price"], v.get("est", False)) for k, v in (c.get("raw") or {}).items()
                if isinstance(v, dict) and num(v.get("price"))}, cfg["vendor_pct"])
    return any(cfg["min_buy"] <= x["buy"] <= cfg["max_buy"] and x["net"] >= cfg["min_net"] and x["roi"] >= cfg["min_roi_pct"]
               for x in r)


def pick_sets(raw_sets, cfg):
    """[{'name','slug','year'}] for the sets to watch."""
    lo, hi = cfg["years"]
    eras = cfg.get("eras") or []
    manifest = jload(rp.MANIFEST, {"eras": []})
    fallback = {rp.norm(n) for n in cfg["sets_fallback"]}
    skip = [s.lower() for s in cfg["skip_sets"]]
    out = []
    for s in raw_sets:
        name = s.get("name") or ""
        slug = s.get("slug") or ""
        if not name or not slug:
            continue
        if "pokemon" not in slug.lower() and (s.get("game") or "pokemon").lower() != "pokemon":
            continue
        if any(x in name.lower() for x in skip):
            continue
        y = set_year(s)
        if eras:
            ok = set_era(name, y, manifest) in eras
        else:
            ok = (lo <= y <= hi) if y else (rp.norm(name) in fallback)
        if ok:
            out.append({"name": name, "slug": slug, "year": y})
    return out


def get_sets(api, dstate, cfg, problems):
    cache = dstate.get("setlist") or {}
    # (a list of exactly 200 was cut off by the old one-page download, so get the full one)
    if cache.get("sets") and days_ago(cache.get("fetched", "")) < 30 and len(cache["sets"]) != 200:
        return pick_sets(cache["sets"], cfg), "remembered"
    if cache.get("failed") and days_ago(cache["failed"]) < 30 and not cache.get("sets"):
        return fallback_sets(dstate, cfg), "fallback (set list endpoint didn't work before)"
    try:
        raw, offset = [], 0
        for _ in range(10):   # the list comes in pages of up to 200 sets
            j = api.get_path("/sets", {"game": "pokemon", "limit": 200, "offset": offset})
            page = rp.records(j)
            raw += page
            total = int((j or {}).get("total") or 0)
            offset += len(page)
            if not page or (total and offset >= total) or (not total and len(page) < 200):
                break
        if not raw:
            raise RuntimeError("the set list came back empty")
        dstate["setlist"] = {"fetched": TODAY, "sets": [
            {k: s.get(k) for k in ("name", "slug", "game", "release_date", "released_at", "releaseDate", "released", "date") if s.get(k) is not None}
            for s in raw]}
        return pick_sets(dstate["setlist"]["sets"], cfg), "TCG Price Lookup set list"
    except rp.Stop:
        raise
    except Exception as e:
        dstate["setlist"] = {"failed": TODAY, "why": str(e)[:160], "sets": cache.get("sets") or []}
        problems.append(("sets", "set list", f"couldn't get the set list ({e}); using the fallback list"))
        return fallback_sets(dstate, cfg), "fallback"


def fallback_sets(dstate, cfg):
    """No set list: use the fallback names, with slugs we already know or the usual pokemon--<name> pattern."""
    known = jload(rp.STATE, {}).get("sets", {})
    out = []
    eras = cfg.get("eras") or []
    for n in cfg["sets_fallback"]:
        if eras and set_era(n) not in eras:
            continue
        slug = known.get(n) or "pokemon--" + re.sub(r"[^a-z0-9]+", "-", n.lower().replace("&", "and")).strip("-")
        out.append({"name": n, "slug": slug, "year": None})
    return out


# ---------------------------------------------------------------- your taste (learned from picks)
KIND_RE = [("full art", r"full art"), ("secret rare", r"secret|rainbow"), ("promo", r"promo"),
           ("reverse holo", r"reverse"), ("holo", r"holo"), ("non-holo", r"non-holo|normal")]


def base_name(name):
    """'Flygon - BW53 (Prerelease) [Staff]' -> 'flygon', 'Alolan Sandslash' -> 'alolan sandslash'."""
    n = re.split(r"\s+-\s+|\s*[\(\[]", name or "")[0]
    n = re.sub(r"\b(EX|GX|V|VMAX|VSTAR|LV\.?X|BREAK|Prime|LEGEND|Star|δ)\b", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


def features(c):
    """What the finder learns from: the Pokémon, the set, the card type and the price range."""
    text = f"{c.get('variant', '')} {c.get('set', '')}".lower()
    kind = next((k for k, pat in KIND_RE if re.search(pat, text)), "other")
    buy = 0
    for k in ("mp", "lp", "dmg", "nm"):
        p = ((c.get("raw") or {}).get(k) or {}).get("price")
        if isinstance(p, (int, float)):
            buy = p
            break
    band = "under $20" if buy < 20 else "$20-50" if buy < 50 else "$50-100" if buy < 100 else "$100+"
    return [f"pokemon:{base_name(c.get('name'))}", f"set:{c.get('set', '')}", f"kind:{kind}", f"price:{band}"]


def taste_counts(dstate, cfg):
    """{feature: (declines, adds)} from your picks in the last taste_days."""
    out = {}
    for e in dstate.get("taste", []):
        if days_ago(e.get("d", "")) > cfg.get("taste_days", 180):
            continue
        for f in e.get("f", []):
            no, yes = out.get(f, (0, 0))
            out[f] = (no + max(0, -e.get("v", 0)), yes + max(0, e.get("v", 0)))
    return out


def taste_factor(c, counts):
    """Score multiplier: each decline of a matching Pokémon/set/type/price range lowers it, each add raises it."""
    m = 1.0
    for f in features(c):
        no, yes = counts.get(f, (0, 0))
        w = 1.0 if f.startswith("pokemon:") else 0.5     # the Pokémon itself counts most
        m *= (0.8 ** (no * w)) * (1.1 ** (yes * w))
    return max(0.2, min(1.6, m))


def declined_now(dstate):
    return {k for k, v in (dstate.get("declined") or {}).items() if (v.get("until") or "") > TODAY}


def stamped(c, cfg):
    text = f"{c.get('name', '')} {c.get('variant', '')}"
    return any(re.search(p, text, re.I) for p in cfg.get("stamp_patterns", []))


def is_popular(c, ctx):
    return any(p.search(c.get("name") or "") for p in ctx.popular)


# ---------------------------------------------------------------- one card
def skip_reason(rec, cfg, hp_ok):
    for k in ("supertype", "card_type", "cardType", "category", "type", "kind"):
        v = rec.get(k)
        l = (" ".join(map(str, v)) if isinstance(v, list) else v if isinstance(v, str) else "").lower()
        if "trainer" in l:
            return "trainer"
        if "energy" in l:
            return "energy"
    if hp_ok and "hp" in rec and rec.get("hp") in (None, "", 0, "0"):
        return "trainer"      # no HP = not a Pokémon
    name = rec.get("name") or ""
    for p in cfg["skip_name_patterns"]:
        if re.search(p, name, re.I):
            return "trainer"
    return None


def low_prices(rec):
    out = {}
    for k, v in (((rec.get("prices") or {}).get("raw")) or {}).items():
        c = rp.COND_KEYS.get(rp.norm(k))
        t = (v or {}).get("tcgplayer") or {}
        if c and num(t.get("low")):
            out[c] = float(t["low"])
    return out


def routes(prices, vp):
    out = []
    for b, s in FLIPS:
        if b in prices and s in prices:
            buy, sell = prices[b][0], prices[s][0]
            net = sell * vp / 100 - buy
            out.append({"from": b, "to": s, "buy": buy, "sell": sell, "net": net, "roi": net / buy * 100,
                        "est": bool(prices[b][1] or prices[s][1])})
    return out


class Ctx:
    def __init__(self, cfg, have, taken, dismissed, manifest, cards_by_era):
        self.cfg, self.have, self.taken, self.dismissed = cfg, have, taken, dismissed
        self.manifest, self.cards_by_era = manifest, cards_by_era
        self.count = {k: 0 for k in ("seen", "onsite", "dismissed", "trainer", "energy", "noprice", "noflip", "notcg", "kept",
                                     "stamped", "unpopular", "declined")}
        self.declined, self.taste, self.verified = set(), {}, {}
        self.keys, self.tp_keys = set(), set()
        self.popular = [re.compile(r"\b" + re.escape(p) + r"\b", re.I) for p in cfg["popular"]]


def build(rec, set_name, slug, hp_ok, ctx):
    cfg = ctx.cfg
    ctx.count["seen"] += 1
    ctx.keys |= set(rec.keys())
    for v in (((rec.get("prices") or {}).get("raw")) or {}).values():
        ctx.tp_keys |= set(((v or {}).get("tcgplayer") or {}).keys())
    try:
        tid = int(rec.get("tcgplayer_id"))
    except (TypeError, ValueError):
        ctx.count["notcg"] += 1
        return None
    printing = rec.get("variant") or "Normal"
    if (str(tid), printing.lower()) in ctx.have:
        ctx.count["onsite"] += 1
        return None
    if rec.get("id") in ctx.dismissed:
        ctx.count["dismissed"] += 1
        return None
    if rec.get("id") in ctx.declined:
        ctx.count["declined"] += 1
        return None
    why = skip_reason(rec, cfg, hp_ok)
    if why:
        ctx.count[why] += 1
        return None
    look = {"name": rec.get("name") or "", "variant": printing}
    if stamped(look, cfg):
        ctx.count["stamped"] += 1
        return None
    if cfg.get("require_popular", True) and not is_popular(look, ctx):
        ctx.count["unpopular"] += 1
        return None
    prices, fixes = rp.checked_prices(rec)   # odd one-off sales are evened out first
    v = ctx.verified.get(f"{tid}|{printing.lower()}")   # real TCGplayer sales win when checked recently
    if v:
        for k, e in v["prices"].items():
            prices[k] = (e["price"], bool(e.get("est")))
    if len(prices) < 2:
        ctx.count["noprice"] += 1
        return None
    good = [r for r in routes(prices, cfg["vendor_pct"])
            if cfg["min_buy"] <= r["buy"] <= cfg["max_buy"] and r["net"] >= cfg["min_net"] and r["roi"] >= cfg["min_roi_pct"]]
    if not good:
        ctx.count["noflip"] += 1
        return None
    best = max(good, key=lambda r: (r["roi"], r["net"]))
    flags = []
    if best["est"]:
        flags.append("est")
    if best["sell"] / best["buy"] >= cfg["big_jump_ratio"]:
        flags.append("bigjump")
    lo = low_prices(rec).get(best["to"])
    if lo and best["sell"] >= 1.5 * lo:
        flags.append("wide")
    popular = any(p.search(rec.get("name") or "") for p in ctx.popular)
    score = best["roi"] * (0.5 if "est" in flags else 1) * (0.75 if "bigjump" in flags else 1) \
        * (0.6 if "wide" in flags else 1) * (1.15 if popular else 1)

    when = (rec.get("last_price_update") or rec.get("updated_at") or TODAY)[:10]
    raw = {}
    for k in ("nm", "lp", "mp", "dmg"):
        if k in prices:
            raw[k] = rp.raw_entry(k, prices[k], when, fixes)
    if v:
        rp.overlay_verified({"tcgId": tid, "tcgVariant": printing}, raw, ctx.verified)
    base = "-".join(x for x in (ac.slugify(rec.get("name")), ac.slugify(rec.get("number")), str(tid)) if x)
    cid = base if base not in ctx.taken else f"{base}-{ac.slugify(printing)[:8]}"
    n = 2
    while cid in ctx.taken:
        cid, n = f"{base}-{n}", n + 1
    ctx.taken.add(cid)
    era = ac.era_for(set_name, ctx.manifest, ctx.cards_by_era)
    ctx.count["kept"] += 1
    return {
        "id": cid, "name": rec.get("name") or "", "variant": ac.LABELS.get(printing.lower(), printing),
        "set": set_name, "number": rec.get("number") or "", "raw": raw,
        "image": rec.get("image_url") or "", "updated": TODAY, "tcgId": tid, "tcgVariant": printing,
        "era": era, "found": TODAY, "pkey": rec.get("id"), "setSlug": slug, "score": round(score, 1),
        "base": round(score, 1), "flags": flags, "pop": popular,
    }


# ---------------------------------------------------------------- main
def main():
    cfg = load_cfg()
    now = datetime.datetime.now(PT)
    show_day = now.weekday() not in SCAN_DAYS
    if show_day and not FORCE:
        msg = f"Skipped: today is {now.strftime('%A')}. The Flip Finder only scans Monday-Thursday so show-day requests stay free."
        print(msg)
        return finish("SKIPPED", msg + "\n", None, saved=False)
    if not rp.API_KEY:
        return finish("FAILED", "❌ The TCGPL_API_KEY secret is missing, so the Flip Finder can't search.\n", None, saved=False)

    manifest = jload(rp.MANIFEST, {"eras": []})
    cards_by_era, site_cards = {}, []
    for e in manifest["eras"]:
        d = jload(os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json"), {})
        cards_by_era[e["id"]] = d.get("cards", [])
        site_cards += d.get("cards", [])
    have = {(str(c.get("tcgId") or ""), ac.printing_of(c)) for c in site_cards if c.get("tcgId")}
    taken = {c["id"] for c in site_cards}

    dstate = jload(DSTATE, {})
    dstate.setdefault("sets", {})
    dstate.setdefault("dismissed", [])
    dismissed = set(dstate["dismissed"])
    declined = declined_now(dstate)
    prev = jload(CANDS, {"cards": []})
    prev_cards = [c for c in prev.get("cards", []) if isinstance(c, dict) and c.get("id")]
    taken |= {c["id"] for c in prev_cards}

    reserve = cfg["reserve_requests_show_days"] if show_day else cfg["reserve_requests"]
    api = DApi(cfg["daily_requests"], reserve)
    ctx = Ctx(cfg, have, taken, dismissed, manifest, cards_by_era)
    ctx.declined, ctx.taste, ctx.verified = declined, taste_counts(dstate, cfg), rp.load_verified()
    problems, stop_reason = [], None
    scanned, partial = [], None
    fresh = []          # candidates found this run
    set_src = "?"
    set_pool = []
    errors_in_row = 0
    t0 = time.time()
    try:
        set_pool, set_src = get_sets(api, dstate, cfg, problems)
        due = [s for s in set_pool if days_ago((dstate["sets"].get(s["slug"]) or {}).get("scanned", "")) >= cfg["min_rescan_days"]]
        due.sort(key=lambda s: (dstate["sets"].get(s["slug"]) or {}).get("scanned") or "")
        for s in due:
            if not api.can():
                break
            recs, offset, total_ok = [], 0, True
            try:
                while True:
                    j = api.get({"game": "pokemon", "set": s["slug"], "limit": rp.PAGE, "offset": offset})
                    page = rp.records(j)
                    recs += page
                    total = int((j or {}).get("total") or 0)
                    offset += rp.PAGE
                    if offset >= total or not page:
                        break
            except rp.Stop:
                total_ok, partial = False, s["name"]
                stop_reason = "request budget for today reached"
            except Exception as e:
                total_ok = False
                errors_in_row += 1
                problems.append(("error", s["name"], str(e)[:160]))
                dstate["sets"].setdefault(s["slug"], {})["error"] = TODAY
                if errors_in_row >= 3:
                    stop_reason = "3 sets in a row failed, so the API may be down; stopped to be safe"
                    break
            else:
                errors_in_row = 0
            hp_ok = any(r.get("hp") for r in recs)
            found = 0
            for r in recs:
                c = build(r, (r.get("set") or {}).get("name") or r.get("set_name") or s["name"], s["slug"], hp_ok, ctx)
                if c:
                    fresh.append(c)
                    found += 1
            if recs and total_ok:
                dstate["sets"][s["slug"]] = {"name": s["name"], "scanned": TODAY, "records": len(recs), "found": found}
                scanned.append((s["name"], len(recs), found))
            elif recs and not total_ok:
                partial = partial or s["name"]
            if not total_ok and stop_reason:
                break
            if total_ok and not recs:
                problems.append(("empty", s["name"], f"no cards came back for '{s['slug']}' (wrong set name/slug?)"))
                dstate["sets"].setdefault(s["slug"], {})["scanned"] = TODAY   # don't retry every night
                dstate["sets"][s["slug"]]["empty"] = True
    except rp.Stop as e:
        stop_reason = str(e)
    except Exception as e:
        problems.append(("error", "run", str(e)[:200]))

    # ---- merge with what was already listed
    rescanned = {s[0] for s in scanned}
    fresh_by_key = {(c["tcgId"], c["tcgVariant"].lower()): c for c in fresh}
    keep = []
    for c in prev_cards:
        k = (c.get("tcgId"), str(c.get("tcgVariant", "")).lower())
        if k in fresh_by_key:
            fresh_by_key[k]["found"] = c.get("found") or fresh_by_key[k]["found"]   # keep first-seen date
            fresh_by_key[k]["id"] = c["id"]
            continue
        if (str(c.get("tcgId")), str(c.get("tcgVariant", "")).lower()) in have or c.get("pkey") in dismissed \
                or c.get("pkey") in declined:
            continue                                  # already added, hidden or declined
        if stamped(c, cfg) or (cfg.get("require_popular", True) and not is_popular(c, ctx)):
            continue                                  # stamped / not a popular Pokémon (settings changed)
        if c.get("set") in rescanned:
            continue                                  # its set was re-checked and it no longer qualifies
        if days_ago(c.get("updated") or "") > cfg["max_age_days"]:
            continue                                  # price info too old
        if not fits(c, cfg):
            continue                                  # outside your Flip Finder settings (era / profit / return / price)
        keep.append(c)
    # ---- cards you left alone for stale_days: off the list, "not interested for now" for snooze_days
    stale_days, snooze = int(cfg.get("stale_days") or 0), int(cfg.get("snooze_days") or 30)
    snoozed = []
    if stale_days > 0:
        until = (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=snooze)).isoformat()
        dstate.setdefault("declined", {})
        dstate.setdefault("taste", [])
        def stale(c):
            return days_ago(c.get("found") or TODAY) >= stale_days
        for c in keep + list(fresh_by_key.values()):
            if stale(c) and c.get("pkey"):
                dstate["declined"][c["pkey"]] = {"until": until, "auto": True,
                                                 "card": f"{c['name']} ({c['variant']}) {c['set']} {c['number']}"}
                dstate["taste"].append({"d": TODAY, "v": -0.5, "f": features(c)})   # a light "not for me"
                snoozed.append(c)
        keep = [c for c in keep if not stale(c)]
        fresh_by_key = {k: c for k, c in fresh_by_key.items() if not stale(c)}
        dstate["taste"] = dstate["taste"][-600:]
    for c in keep + list(fresh_by_key.values()):     # re-rank everything with what your picks taught it
        c.setdefault("base", c.get("score", 0))
        c["score"] = round(c["base"] * taste_factor(c, ctx.taste), 1)
    merged = sorted(keep + list(fresh_by_key.values()), key=lambda c: -c.get("score", 0))[:cfg["max_candidates"]]
    new_count = sum(1 for c in merged if c.get("found") == TODAY)

    never = sum(1 for s in set_pool if not (dstate["sets"].get(s["slug"]) or {}).get("scanned"))
    if stop_reason and not scanned and not partial and "reached" not in stop_reason:
        status = "FAILED" if "rejected" in stop_reason else "PARTIAL"
    elif problems and not scanned:
        status = "PARTIAL"
    else:
        status = "OK"
    secs = int(time.time() - t0)
    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    L = [f"## {icon} Flip Finder {status} — {TODAY}" + (" (test run, nothing saved)" if DRY else ""), ""]
    if stop_reason:
        L += [f"Stopped early: {stop_reason}.", ""]
    L += [
        f"- Sets scanned this run: **{len(scanned)}**" + (f" (+ partly: {partial})" if partial else "") + f" of {len(set_pool)} watched (set list: {set_src}); never scanned yet: {never}",
        f"- API requests used: **{api.used}** of {cfg['daily_requests']}" + (f" (API says {api.remaining} left today; {reserve} kept free)" if api.remaining is not None else ""),
        f"- Cards looked at: {ctx.count['seen']} → **{ctx.count['kept']}** flip candidates"
        f" (already on site {ctx.count['onsite']}, hidden by you {ctx.count['dismissed']}, declined by you {ctx.count['declined']},"
        f" stamped {ctx.count['stamped']}, not a popular Pokémon {ctx.count['unpopular']}, trainers/energy {ctx.count['trainer'] + ctx.count['energy']},"
        f" missing prices {ctx.count['noprice']}, no good flip {ctx.count['noflip']}, no TCGplayer id {ctx.count['notcg']})",
        f"- On the Flip Finder list now: **{len(merged)}** ({new_count} new today) · took {secs // 60}m {secs % 60}s",
        f"- Settings: eras {', '.join(cfg.get('eras') or []) or 'years ' + str(cfg['years'])}; buy ${cfg['min_buy']}-${cfg['max_buy']},"
        f" profit ≥ ${cfg['min_net']}, return ≥ {cfg['min_roi_pct']}%",
    ]
    if snoozed:
        L += [f"- Untouched for {stale_days}+ days, so taken off and marked not interested for {snooze} days: **{len(snoozed)}**"
              + " (" + ", ".join(f"{c['name']} ({c['variant']})" for c in snoozed[:12]) + (" …" if len(snoozed) > 12 else "") + ")"]
    if ctx.keys:
        L += ["", "<details><summary>Fields the API returned (for tuning)</summary>", "",
              "Card fields: `" + ", ".join(sorted(ctx.keys)) + "`", "",
              "Price fields: `" + ", ".join(sorted(ctx.tp_keys)) + "`", "</details>"]
    if scanned:
        L += ["", "**Sets scanned:** " + ", ".join(f"{n} ({r} cards, {f} candidates)" for n, r, f in scanned[:30])]
    top = merged[:8] if DRY else merged[:5]
    if top:
        L += ["", "**Top of the list:**"] + [
            "- {n} ({v}) {s} {no}: buy {f} ${b:.2f} → sell {t} ${p:.2f} at {vp}% — {r:+.0f}%{fl}".format(
                n=c["name"], v=c["variant"], s=c["set"], no=c["number"], f=_best(c, cfg)["from"].upper(), b=_best(c, cfg)["buy"],
                t=_best(c, cfg)["to"].upper(), p=_best(c, cfg)["sell"], vp=cfg["vendor_pct"], r=_best(c, cfg)["roi"],
                fl=(" · flags: " + ", ".join(c["flags"])) if c["flags"] else "") for c in top]
    if problems:
        L += ["", f"**Problems ({len(problems)}):**"] + [f"- {k}: {n} — {w}" for k, n, w in problems[:25]]
    report = "\n".join(L) + "\n"
    try:
        import botlog
        test = "Test run (nothing saved): " if DRY else ""
        if status == "FAILED":
            head = f"{test}Flip Finder failed: {stop_reason or 'see details'}. The list was not changed."
        else:
            head = (f"{test}Flip Finder {'ran fine' if status == 'OK' else 'only partly finished'}: scanned {len(scanned)} sets, "
                    f"looked at {ctx.count['seen']:,} cards and found {ctx.count['kept']} flips. "
                    f"{len(merged)} cards on the list now ({new_count} new).")
        facts = [f"Used {api.used} of {cfg['daily_requests']} API requests"]
        if snoozed:
            facts.append(f"{len(snoozed)} untouched cards taken off for {snooze} days")
        if stop_reason and status != "FAILED":
            facts.append(f"Stopped early: {stop_reason}")
        if problems:
            facts.append(f"{len(problems)} problems (see details)")
        botlog.summary(head, {"OK": "ok", "PARTIAL": "warn"}.get(status, "fail"), facts)
    except Exception as e:
        print(f"(admin log summary skipped: {e})")

    saved = not DRY and status != "FAILED"
    if saved:
        stamp = str(int(time.time()))
        jdump(CANDS, {"version": stamp, "generated": TODAY, "vendor_pct": cfg["vendor_pct"],
                      "sets_scanned": len(scanned), "sets_watched": len(set_pool), "settings": settings_of(cfg),
                      "cards": merged}, separators=(",", ":"))
        dstate["about"] = ("Written by scripts/discover_candidates.py and scripts/approve_candidates.py. Remembers which sets "
                           "the Flip Finder scanned and when, the set list, and the cards you hid. Safe to delete.")
        jdump(DSTATE, dstate, indent=0, sort_keys=True)
    return finish(status, report, new_count, saved=saved, total=len(merged))


def _best(c, cfg):
    r = routes({k: (v["price"], v.get("est", False)) for k, v in c["raw"].items()}, cfg["vendor_pct"])
    return max(r, key=lambda x: (x["roi"], x["net"])) if r else {"from": "?", "to": "?", "buy": 0, "sell": 0, "roi": 0}


def finish(status, report, new, saved, total=0):
    open(rp.REPORT, "w", encoding="utf-8").write(report)
    print(report)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\nsaved={'true' if saved else 'false'}\nnew={new or 0}\ntotal={total}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(report)
    return 1 if status == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
