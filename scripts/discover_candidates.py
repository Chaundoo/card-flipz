#!/usr/bin/env python3
"""Flip Finder: look for NEW cards worth flipping and list them for your approval.

Runs in GitHub Actions on Monday-Thursday only (the days you are not at card shows), after the
daily price update, so it uses API requests the price update and your show-day card adds don't need.

What it does
  1. Gets the list of sets from TCG Price Lookup (one request, remembered for 30 days) and keeps the
     2010-2020 era sets (settings in scripts/discovery_config.json).
  2. Scans the sets that were scanned longest ago (never-scanned first) until the request budget is
     used up. Every card is checked at the vendor rate in the settings (default 70%).
  3. Keeps cards with a good condition flip: buy one condition, sell one step up to a vendor, buy price
     over $5, good percentage return, no trainers/energy, not already on the site, not hidden by you.
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
    """Same client as the price update, but keeps a bigger or smaller reserve of today's requests."""
    def __init__(self, budget, reserve):
        super().__init__(budget)
        self.reserve = reserve

    def can(self, n=1):
        left = self.budget - self.used
        if self.remaining is not None:
            left = min(left, self.remaining - self.reserve)
        return left >= n

    def get_path(self, path, params):
        """A request to another endpoint (used for the set list). One retry on a rate-limit reply."""
        import urllib.request, urllib.error, urllib.parse, random
        if not self.can():
            raise rp.Stop("daily request budget used")
        url = rp.BASE + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            wait = random.uniform(*rp.PAUSE) - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            req = urllib.request.Request(url, headers={"X-API-Key": rp.API_KEY, "Accept": "application/json", "User-Agent": rp.UA})
            self.used += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._quota(r.headers)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                self._quota(e.headers)
                body = e.read().decode("utf-8", "replace")[:200]
                if e.code in (401, 403):
                    raise rp.Stop(f"the API rejected the key (HTTP {e.code}). {body}")
                if e.code == 429 and attempt == 0:
                    if "daily" in body.lower():
                        raise rp.Stop("TCG Price Lookup's daily request limit is used up")
                    time.sleep(float(e.headers.get("Retry-After") or 10) + 1)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}")
        raise RuntimeError("gave up after retries")


def load_cfg():
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    cfg["daily_requests"] = int(os.environ.get("DISC_REQUESTS") or cfg.get("daily_requests") or 60)
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


def pick_sets(raw_sets, cfg):
    """[{'name','slug','year'}] for the sets to watch."""
    lo, hi = cfg["years"]
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
        ok = (lo <= y <= hi) if y else (rp.norm(name) in fallback)
        if ok:
            out.append({"name": name, "slug": slug, "year": y})
    return out


def get_sets(api, dstate, cfg, problems):
    cache = dstate.get("setlist") or {}
    if cache.get("sets") and days_ago(cache.get("fetched", "")) < 30:
        return pick_sets(cache["sets"], cfg), "remembered"
    if cache.get("failed") and days_ago(cache["failed"]) < 30 and not cache.get("sets"):
        return fallback_sets(dstate, cfg), "fallback (set list endpoint didn't work before)"
    try:
        j = api.get_path("/sets", {"game": "pokemon", "limit": 500})
        raw = rp.records(j)
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
    for n in cfg["sets_fallback"]:
        slug = known.get(n) or "pokemon--" + re.sub(r"[^a-z0-9]+", "-", n.lower().replace("&", "and")).strip("-")
        out.append({"name": n, "slug": slug, "year": None})
    return out


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
        self.count = {k: 0 for k in ("seen", "onsite", "dismissed", "trainer", "energy", "noprice", "noflip", "notcg", "kept")}
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
    why = skip_reason(rec, cfg, hp_ok)
    if why:
        ctx.count[why] += 1
        return None
    prices = rp.cond_prices(rec)
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
            p, est = prices[k]
            raw[k] = {"price": round(p, 2), "date": when, "src": rp.SRC_MID if est else rp.SRC, "est": est}
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
        "set": set_name, "number": rec.get("number") or "", "raw": raw, "graded": {}, "gem": "", "verdict": "",
        "notes": "", "image": rec.get("image_url") or "", "updated": TODAY, "tcgId": tid, "tcgVariant": printing,
        "era": era, "found": TODAY, "pkey": rec.get("id"), "setSlug": slug, "score": round(score, 1),
        "flags": flags, "pop": popular,
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
    prev = jload(CANDS, {"cards": []})
    prev_cards = [c for c in prev.get("cards", []) if isinstance(c, dict) and c.get("id")]
    taken |= {c["id"] for c in prev_cards}

    reserve = cfg["reserve_requests_show_days"] if show_day else cfg["reserve_requests"]
    api = DApi(cfg["daily_requests"], reserve)
    ctx = Ctx(cfg, have, taken, dismissed, manifest, cards_by_era)
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
        if (str(c.get("tcgId")), str(c.get("tcgVariant", "")).lower()) in have or c.get("pkey") in dismissed:
            continue                                  # already added or hidden
        if c.get("set") in rescanned:
            continue                                  # its set was re-checked and it no longer qualifies
        if days_ago(c.get("updated") or "") > cfg["max_age_days"]:
            continue                                  # price info too old
        keep.append(c)
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
        f" (already on site {ctx.count['onsite']}, hidden by you {ctx.count['dismissed']}, trainers/energy {ctx.count['trainer'] + ctx.count['energy']},"
        f" missing prices {ctx.count['noprice']}, no good flip {ctx.count['noflip']}, no TCGplayer id {ctx.count['notcg']})",
        f"- On the Flip Finder list now: **{len(merged)}** ({new_count} new today) · took {secs // 60}m {secs % 60}s",
    ]
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

    saved = not DRY and status != "FAILED"
    if saved:
        stamp = str(int(time.time()))
        jdump(CANDS, {"version": stamp, "generated": TODAY, "vendor_pct": cfg["vendor_pct"],
                      "sets_scanned": len(scanned), "sets_watched": len(set_pool), "cards": merged}, separators=(",", ":"))
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
