#!/usr/bin/env python3
"""Move Flip Finder candidates you picked on the site into the card list (runs in GitHub Actions).

The site's Flip Finder tab opens an issue titled "Add candidates: ..." whose body holds a JSON block:
    {"v":1, "add":["card-id", ...], "decline":["card-id", ...], "hide":["card-id", ...]}
  add     -> each card is copied from candidates.json (with the condition prices it was found with) into
             its era file, so the vendor and condition-flip numbers work right away.
  decline -> "Not for me": removed from the list and not suggested again for decline_days (90).
  hide    -> removed from the list and never suggested again.
The same workflow also handles "Flip Finder settings: ..." issues from the tab's "What to look for" panel:
    {"v":1, "settings":{"eras":["bw","xy"], "min_net":5, "min_roi_pct":25, "min_buy":5, "max_buy":250,
                        "stale_days":3, "snooze_days":30}}
  -> saved into scripts/discovery_config.json for the next searches, and cards on the list that no longer
     match are taken off right away.
Every pick is also remembered (Pokémon, set, card type, price range) so the Flip Finder ranks
similar cards lower (declined/hidden) or higher (added) on its next search.

This uses NO TCG Price Lookup requests: everything needed is already in candidates.json. The daily
price update refreshes the new cards later like any other card.

Writes the reply for the issue to run_report.md and status=added|nothing|failed to $GITHUB_OUTPUT.
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp
import add_card as ac
import discover_candidates as dc

ROOT = rp.ROOT
CANDS = os.path.join(ROOT, "candidates.json")
DSTATE = os.path.join(ROOT, "scripts", "discovery_state.json")
SITE = ac.SITE
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,90}$")
MAX_IDS = 200
EXTRA = ("era", "found", "pkey", "setSlug", "score", "flags", "pop")   # candidate-only fields, not saved on the site


def out(status, text):
    open(rp.REPORT, "w", encoding="utf-8").write(text + "\n")
    print(text)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\n")
    return 1 if status == "failed" else 0


def issue_text():
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if ev and os.path.exists(ev):
        i = (json.load(open(ev, encoding="utf-8")).get("issue") or {})
        return (i.get("title") or "") + "\n" + (i.get("body") or "")
    return os.environ.get("ISSUE_TEXT", "")


def parse(text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        return None, "I couldn't find the card list in this issue (the JSON block is missing)."
    try:
        p = json.loads(m.group(1))
    except ValueError:
        return None, "The card list in this issue isn't valid JSON, so nothing was changed."
    if not isinstance(p, dict) or p.get("v") != 1:
        return None, "This issue isn't in a format I know (expected version 1)."
    ids = {"settings": None}
    if "settings" in p:
        s, err = check_settings(p["settings"])
        if err:
            return None, err
        ids["settings"] = s
    for key in ("add", "decline", "hide"):
        v = p.get(key, [])
        if not isinstance(v, list) or len(v) > MAX_IDS or not all(isinstance(x, str) and ID_RE.match(x) for x in v):
            return None, f"The '{key}' list has a bad card id or is too long, so nothing was changed."
        ids[key] = list(dict.fromkeys(v))
    ids["hide"] = [x for x in ids["hide"] if x not in set(ids["add"])]
    ids["decline"] = [x for x in ids["decline"] if x not in set(ids["add"]) | set(ids["hide"])]
    return ids, None


LIMITS = {"min_net": (0, 500), "min_roi_pct": (0, 500), "min_buy": (0, 2000), "max_buy": (1, 2000),
          "stale_days": (0, 60), "snooze_days": (1, 365)}


def check_settings(s):
    """Validate the settings sent from the site. Returns (clean dict, error)."""
    if not isinstance(s, dict):
        return None, "The settings in this issue aren't readable, so nothing was changed."
    known = {e["id"] for e in json.load(open(rp.MANIFEST, encoding="utf-8"))["eras"]}
    out_ = {}
    if "eras" in s:
        e = s["eras"]
        if not isinstance(e, list) or not e or not all(isinstance(x, str) and x in known for x in e):
            return None, "Pick at least one era (and only eras the site knows), so nothing was changed."
        out_["eras"] = list(dict.fromkeys(e))
    for k, (lo, hi) in LIMITS.items():
        if k in s:
            v = s[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                return None, f"The setting '{k}' should be a number from {lo} to {hi}, so nothing was changed."
            out_[k] = int(v) if float(v).is_integer() else round(float(v), 2)
    if not out_:
        return None, "No settings were sent, so nothing was changed."
    return out_, None


def apply_settings(s):
    """Save the new settings and take cards that no longer match off the list. Returns the reply text."""
    cfg = json.load(open(dc.CFG_PATH, encoding="utf-8"))
    before = {k: cfg.get(k) for k in s}
    cfg.update(s)
    if cfg.get("min_buy", 0) > cfg.get("max_buy", 300):
        cfg["min_buy"], cfg["max_buy"] = cfg["max_buy"], cfg["min_buy"]
    with open(dc.CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    cands = json.load(open(CANDS, encoding="utf-8")) if os.path.exists(CANDS) else {"cards": []}
    keep = [c for c in cands.get("cards", []) if isinstance(c, dict) and dc.fits(c, cfg)]
    gone = len(cands.get("cards", [])) - len(keep)
    cands["cards"] = keep
    cands["settings"] = dc.settings_of(cfg)
    cands["version"] = str(int(time.time()))
    with open(CANDS, "w", encoding="utf-8") as f:
        json.dump(cands, f, ensure_ascii=False, separators=(",", ":"))
    names = {e["id"]: e["name"] for e in json.load(open(rp.MANIFEST, encoding="utf-8"))["eras"]}
    L = ["⚙️ Flip Finder settings saved. The next search (Monday-Thursday morning) uses them:", "",
         f"- Eras: {', '.join(names.get(e, e) for e in cfg.get('eras') or []) or 'years ' + str(cfg.get('years'))}",
         f"- Buy price: ${cfg['min_buy']} to ${cfg['max_buy']}",
         f"- Profit per card (vendor at {cfg['vendor_pct']}%): at least ${cfg['min_net']}",
         f"- Return: at least {cfg['min_roi_pct']}%",
         f"- Untouched cards come off the list after {cfg.get('stale_days')} days and stay away {cfg.get('snooze_days')} days"]
    changed = [k for k in s if before.get(k) != cfg.get(k)]
    if not changed:
        L += ["", "(These were already the settings, so nothing changed.)"]
    if gone:
        L += ["", f"🧹 Took {gone} card{'s' if gone != 1 else ''} that no longer match off the list now."]
    return "\n".join(L)


def main():
    ids, err = parse(issue_text())
    if err:
        return out("failed", "❌ " + err)
    if ids["settings"]:
        return out("added", apply_settings(ids["settings"]))
    if not ids["add"] and not ids["hide"] and not ids["decline"]:
        return out("nothing", "ℹ️ No cards were picked, so nothing changed.")

    cands = json.load(open(CANDS, encoding="utf-8")) if os.path.exists(CANDS) else {"cards": []}
    by_id = {c["id"]: c for c in cands.get("cards", []) if isinstance(c, dict) and c.get("id")}
    manifest = json.load(open(rp.MANIFEST, encoding="utf-8"))
    files, cards_by_era, all_cards = {}, {}, []
    for e in manifest["eras"]:
        p = os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            files[e["id"]] = (p, d)
            cards_by_era[e["id"]] = d.get("cards", [])
            all_cards += d.get("cards", [])
    have = {(str(c.get("tcgId") or ""), ac.printing_of(c)) for c in all_cards if c.get("tcgId")}
    taken = {c["id"] for c in all_cards}
    state = json.load(open(rp.STATE, encoding="utf-8")) if os.path.exists(rp.STATE) else {"cards": {}, "sets": {}}
    state.setdefault("cards", {})
    state.setdefault("sets", {})
    dstate = json.load(open(DSTATE, encoding="utf-8")) if os.path.exists(DSTATE) else {}
    dstate.setdefault("dismissed", [])
    dstate.setdefault("declined", {})
    dstate.setdefault("taste", [])
    cfg = json.load(open(dc.CFG_PATH, encoding="utf-8"))
    import datetime
    until = (datetime.date.fromisoformat(rp.TODAY) + datetime.timedelta(days=int(cfg.get("decline_days", 90)))).isoformat()

    def learn(c, v):
        dstate["taste"].append({"d": rp.TODAY, "v": v, "f": dc.features(c)})

    added, dupes, missing, touched = [], [], [], set()
    for cid in ids["add"]:
        c = by_id.get(cid)
        if not c:
            missing.append(cid)
            continue
        if (str(c.get("tcgId")), str(c.get("tcgVariant", "")).lower()) in have:
            dupes.append(c)
            continue
        era = c.get("era") if c.get("era") in files else ac.era_for(c.get("set", ""), manifest, cards_by_era)
        if era not in files:
            era = "other"
        card = {k: v for k, v in c.items() if k not in EXTRA}
        new_id = card["id"]
        n = 2
        while new_id in taken:
            new_id, n = f"{card['id']}-{n}", n + 1
        card["id"] = new_id
        card["updated"] = c.get("updated") or rp.TODAY
        files[era][1].setdefault("cards", []).append(card)
        taken.add(new_id)
        have.add((str(c.get("tcgId")), str(c.get("tcgVariant", "")).lower()))
        touched.add(era)
        state["cards"][new_id] = {"id": c.get("pkey"), "synced": rp.TODAY, "variant": c.get("tcgVariant")}
        if c.get("setSlug") and c.get("set"):
            state["sets"].setdefault(c["set"], c["setSlug"])
        added.append((c, era, new_id))
        learn(c, +1)

    hidden = []
    for cid in ids["hide"]:
        c = by_id.get(cid)
        if c:
            if c.get("pkey") and c["pkey"] not in dstate["dismissed"]:
                dstate["dismissed"].append(c["pkey"])
            hidden.append(c)
            learn(c, -2)
    declined = []
    for cid in ids["decline"]:
        c = by_id.get(cid)
        if c:
            if c.get("pkey"):
                dstate["declined"][c["pkey"]] = {"until": until, "card": f"{c['name']} ({c['variant']}) {c['set']} {c['number']}"}
            declined.append(c)
            learn(c, -1)
    # forget expired declines and old picks so the files stay small
    dstate["declined"] = {k: v for k, v in dstate["declined"].items() if (v.get("until") or "") > rp.TODAY}
    dstate["taste"] = dstate["taste"][-600:]

    if not added and not hidden and not dupes and not declined:
        return out("failed", "❌ None of those cards are on the Flip Finder list any more (it may have refreshed). "
                             "Open the Flip Finder tab again and pick from the current list.")

    stamp = str(int(time.time()))
    for era in touched:
        p, d = files[era]
        d["version"] = stamp
        for c in d.get("cards", []):
            rp.slim_card(c)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    gone = {c["id"] for c, _, _ in added} | {c["id"] for c in dupes} | {c["id"] for c in hidden} | {c["id"] for c in declined}
    cands["cards"] = [c for c in cands.get("cards", []) if c.get("id") not in gone]
    cands["version"] = stamp
    with open(CANDS, "w", encoding="utf-8") as f:
        json.dump(cands, f, ensure_ascii=False, separators=(",", ":"))
    with open(rp.STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)
    with open(DSTATE, "w", encoding="utf-8") as f:
        json.dump(dstate, f, ensure_ascii=False, indent=0, sort_keys=True)

    L = []
    if added:
        L += [f"✅ Added **{len(added)}** card{'s' if len(added) != 1 else ''} to the site:", ""]
        for c, era, nid in added:
            L.append(f"- [{c['name']} ({c['variant']})]({SITE}#{nid}) — {c['set']} {c['number']}")
        L += ["", "They show up in about a minute. Prices are as of the scan and refresh with the daily update."]
    if dupes:
        L += ["", f"↩️ Already on the site, skipped: " + ", ".join(f"{c['name']} ({c['variant']})" for c in dupes)]
    if hidden:
        L += ["", f"🙈 Hidden from the Flip Finder for good: " + ", ".join(f"{c['name']} ({c['variant']})" for c in hidden)]
    if declined:
        L += ["", f"👎 Not for you ({len(declined)}), kept off the list until {until}: " + ", ".join(f"{c['name']} ({c['variant']})" for c in declined),
              "", "The Flip Finder will also rank cards like these lower on its next search."]
    if missing:
        L += ["", f"❔ Not on the list any more ({len(missing)}): the list refreshed before this ran."]
    return out("added" if (added or hidden or dupes or declined) else "failed", "\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
