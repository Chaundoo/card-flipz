#!/usr/bin/env python3
"""Add one card to Chaundoo Card Flipz from a TCGplayer link (runs in GitHub Actions).

Triggered by a GitHub issue titled "Add card: ..." whose body contains a TCGplayer
product link (the site's "+ Add from TCGplayer link" button makes this issue).

* Reads the product number and the name words from the link text. It never opens
  TCGplayer; the card is looked up on TCG Price Lookup with your API key.
* Skips duplicates (same TCGplayer product + same printing already on the site).
* Adds the card with its raw condition prices to the right era file, so the vendor and
  condition-flip numbers work right away. Graded values are left empty.
* Writes the reply for the issue to run_report.md and status=added|duplicate|failed
  to $GITHUB_OUTPUT.

Uses the same TCGPL_API_KEY secret as the daily price update (1-5 requests per card).
"""
import json, os, re, sys, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp   # reuse the API client and matching helpers

ROOT = rp.ROOT
SITE = "https://chaundoo.github.io/card-flipz/"
LABELS = {"holofoil": "Holo", "reverse holofoil": "Reverse Holo", "normal": "Non-Holo",
          "1st edition holofoil": "1st Edition Holo", "1st edition": "1st Edition",
          "unlimited holofoil": "Unlimited Holo", "unlimited": "Unlimited"}
STOP = {"pokemon", "tcg", "card", "cards", "and"}


def out(status, text):
    open(rp.REPORT, "w", encoding="utf-8").write(text + "\n")
    print(text)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\n")
    return 0 if status != "failed" else 1


def issue_text():
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if ev and os.path.exists(ev):
        e = json.load(open(ev, encoding="utf-8"))
        i = e.get("issue") or {}
        return (i.get("title") or "") + "\n" + (i.get("body") or "")
    return os.environ.get("ISSUE_TEXT", "")


def parse(text):
    m = re.search(r"tcgplayer\.com/product/(\d+)(?:/([^?#\s)]*))?(?:\?([^#\s)]*))?", text, re.I)
    if not m:
        return None
    printing = ""
    for kv in (m.group(3) or "").split("&"):
        k, _, v = kv.partition("=")
        if k.lower() == "printing":
            printing = re.sub(r"\+", " ", v)
    pm = re.search(r"(?im)^\s*printing:\s*(.+?)\s*$", text)
    if pm and pm.group(1).strip().lower() not in ("auto", "auto (from the link)", ""):
        printing = pm.group(1).strip()
    import urllib.parse
    return {"id": m.group(1), "slug": (m.group(2) or "").strip("/"), "printing": urllib.parse.unquote(printing).strip()}


def printing_of(c):
    return (c.get("tcgVariant") or rp.wanted_printing(c.get("variant"))).lower()


def era_for(set_name, manifest, cards_by_era):
    for era_id, cs in cards_by_era.items():
        if any(c.get("set") == set_name for c in cs):
            return era_id
    for e in manifest["eras"]:
        for pat in e.get("match", []):
            try:
                if re.search(pat, set_name or "", re.I):
                    return e["id"]
            except re.error:
                pass
    return "other"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def main():
    link = parse(issue_text())
    if not link:
        return out("failed", "❌ I couldn't find a TCGplayer card link in this issue. It should look like "
                             "`https://www.tcgplayer.com/product/12345/...`.")
    if not rp.API_KEY:
        return out("failed", "❌ The TCGPL_API_KEY secret is missing, so I can't look the card up.")

    manifest = json.load(open(rp.MANIFEST, encoding="utf-8"))
    files, cards_by_era, all_cards = {}, {}, []
    for e in manifest["eras"]:
        p = os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            files[e["id"]] = (p, d)
            cards_by_era[e["id"]] = d.get("cards", [])
            all_cards += d.get("cards", [])

    want = link["printing"].lower()
    same = [c for c in all_cards if str(c.get("tcgId") or "") == link["id"]]
    for c in same:
        if want and printing_of(c) == want:
            if True:
                return out("duplicate", f"↩️ Already on the site: **{c['name']} ({c.get('variant', '')})** — "
                                        f"{c.get('set', '')} {c.get('number', '')}. Nothing was added.\n\n"
                                        f"[Open it on the site]({SITE}#{c['id']})\n\nIf you meant a different printing, "
                                        f"add it again and pick the printing.")

    # Look the product up on TCG Price Lookup using the words from the link
    words = [w for w in link["slug"].split("-") if w and w.lower() not in STOP]
    queries = []
    for q in (" ".join(words), " ".join(words[-3:]), " ".join(words[-2:]), " ".join(words[:3]), words[-1] if words else ""):
        if q and q not in queries:
            queries.append(q)
    if not queries:
        return out("failed", "❌ The link has no card name in it (only the product number), so I can't search for it. "
                             "Copy the full link from the card's TCGplayer page.")
    api = rp.Api(6)
    cands = []
    try:
        for q in queries:
            recs = rp.records(api.get({"game": "pokemon", "q": q, "limit": 100}))
            cands = [r for r in recs if str(r.get("tcgplayer_id") or "") == link["id"]]
            if cands:
                break
    except rp.Stop as e:
        return out("failed", f"❌ Couldn't look the card up: {e}.")
    except Exception as e:
        return out("failed", f"❌ TCG Price Lookup gave an error: {e}. Try again later.")
    if not cands:
        return out("failed", f"❌ I couldn't find TCGplayer product {link['id']} on TCG Price Lookup "
                             f"(searched: {', '.join(repr(q) for q in queries)}). It may not be listed there yet.")

    # Which printing?
    def v(r):
        return (r.get("variant") or "").lower()
    rec = None
    if want:
        rec = next((r for r in cands if v(r) == want), None) or \
              next((r for r in cands if want.startswith("unlimited") and v(r) == want.replace("unlimited ", "")), None)
        if not rec:
            return out("failed", f"❌ That card doesn't come in **{link['printing']}** on TCG Price Lookup. "
                                 f"Printings found: {', '.join(r.get('variant') or 'Normal' for r in cands)}. "
                                 f"Add it again and pick one of those.")
    elif len(cands) == 1:
        rec = cands[0]
    else:
        return out("failed", f"❔ This card comes in more than one printing: "
                             f"{', '.join(r.get('variant') or 'Normal' for r in cands)}. "
                             f"Add it again and pick the printing you want.")

    printing = (rec.get("variant") or "Normal")
    for c in same:
        if printing_of(c) == printing.lower():
            return out("duplicate", f"↩️ Already on the site: **{c['name']} ({c.get('variant', '')})**. Nothing was added.\n\n"
                                    f"[Open it on the site]({SITE}#{c['id']})")

    prices = rp.cond_prices(rec)
    when = (rec.get("last_price_update") or rec.get("updated_at") or rp.TODAY)[:10]
    raw = {}
    for k in ("nm", "lp", "mp", "dmg"):
        if k in prices:
            p, est = prices[k]
            raw[k] = {"price": round(p, 2), "date": when, "src": rp.SRC_MID if est else rp.SRC, "est": est}
    set_name = (rec.get("set") or {}).get("name") or rec.get("set_name") or ""
    era = era_for(set_name, manifest, cards_by_era)
    if era not in files:
        era = "other"
    taken = {c["id"] for c in all_cards}
    base = "-".join(x for x in (slugify(rec.get("name")), slugify(rec.get("number")), link["id"]) if x)
    cid = base if base not in taken else f"{base}-{slugify(printing)[:8]}"
    n = 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    card = {
        "id": cid, "name": rec.get("name") or "", "variant": LABELS.get(printing.lower(), printing),
        "set": set_name, "number": rec.get("number") or "", "raw": raw, "graded": {},
        "gem": "", "verdict": "", "notes": "", "image": rec.get("image_url") or "",
        "updated": rp.TODAY, "tcgId": int(link["id"]), "tcgVariant": printing,
    }
    path, data = files[era]
    data.setdefault("cards", []).append(card)
    data["version"] = str(int(time.time()))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # remember the match so the daily update can refresh it with batch lookups
    state = json.load(open(rp.STATE, encoding="utf-8")) if os.path.exists(rp.STATE) else {"cards": {}, "sets": {}}
    state.setdefault("cards", {})[cid] = {"id": rec.get("id"), "synced": rp.TODAY, "variant": printing}
    if (rec.get("set") or {}).get("slug"):
        state.setdefault("sets", {}).setdefault(set_name, rec["set"]["slug"])
    with open(rp.STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)

    lines = [f"✅ Added **{card['name']} ({card['variant']})** — {set_name} {card['number']} to the "
             f"{next((e['name'] for e in manifest['eras'] if e['id'] == era), era)} era.", "",
             "| Condition | Price |", "|---|---|"]
    for k, lab in (("nm", "NM"), ("lp", "LP"), ("mp", "MP"), ("dmg", "DMG")):
        x = raw.get(k)
        cell = ("$%.2f" % x["price"] + (" (est.)" if x["est"] else "")) if x else "—"
        lines.append("| " + lab + " | " + cell + " |")
    lines += ["", f"Source: TCGplayer market (via TCG Price Lookup). It shows on the site in about a minute: "
                  f"[open card]({SITE}#{cid})"]
    if not raw:
        lines += ["", "⚠️ TCG Price Lookup had no condition prices for it yet, so the prices are blank for now. "
                      "The daily update fills them in when they appear."]
    return out("added", "\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
