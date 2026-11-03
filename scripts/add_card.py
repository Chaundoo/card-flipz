#!/usr/bin/env python3
"""Add one card to Chaundoo Card Flipz from a TCGplayer and/or PriceCharting link (runs in GitHub Actions).

Triggered by a GitHub issue titled "Add card: ..." (the site's "+ Add from link" button makes it).
The issue body can hold a TCGplayer product link, a PriceCharting card link, or both:

  TCGplayer link: https://www.tcgplayer.com/product/89955/pokemon-emerald-torchic?printing=Reverse+Holofoil
  PriceCharting link: https://www.pricecharting.com/game/pokemon-emerald/torchic-reverse-holo-69
  Printing: auto

How it finds the card
* TCGplayer link: the product number is the exact match. The card name comes from the words in the
  link; when the link is only a number (share links, search results), the bot asks TCGplayer for the
  product's name once (one request, like the weekly sales check), then searches TCG Price Lookup.
* PriceCharting link: the bot only READS the link (it never opens PriceCharting). The link holds the
  set, card name, number and printing (e.g. "reverse-holo"), which narrow the search and pick the printing.
* Both links: the TCGplayer number decides the card, the PriceCharting link picks the printing and is
  saved on the card so its PriceCharting button opens that exact page.
* Not on TCG Price Lookup (some stamped / alternate printings): the card is still added from the link
  details with blank prices and a "no API data" note, so it can be tracked and priced by hand.

Skips duplicates (same TCGplayer product + printing, or the same PriceCharting page).
Writes the reply for the issue to run_report.md and status=added|duplicate|failed to $GITHUB_OUTPUT.
Uses the TCGPL_API_KEY secret (1-6 requests per card).
"""
import json, os, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp   # reuse the API client and matching helpers

ROOT = rp.ROOT
SITE = "https://chaundoo.github.io/card-flipz/"
LABELS = {"holofoil": "Holo", "reverse holofoil": "Reverse Holo", "normal": "Non-Holo",
          "1st edition holofoil": "1st Edition Holo", "1st edition": "1st Edition",
          "unlimited holofoil": "Unlimited Holo", "unlimited": "Unlimited"}
STOP = {"pokemon", "tcg", "card", "cards", "and"}
TCG_DETAILS = "https://mp-search-api.tcgplayer.com/v1/product/{id}/details"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
}


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


# ---------------------------------------------------------------- links
def parse_tcg(text):
    m = re.search(r"tcgplayer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?product/(\d+)(?:/([^?#\s)]*))?(?:\?([^#\s)]*))?", text, re.I)
    if not m:
        return None
    printing = ""
    for kv in (m.group(3) or "").split("&"):
        k, _, v = kv.partition("=")
        if k.lower() == "printing":
            printing = urllib.parse.unquote(v.replace("+", " ")).strip()
    return {"id": m.group(1), "slug": (m.group(2) or "").strip("/")}, printing


DESCRIBE = {"holo", "stamped", "stamp", "promo", "foil", "silver", "border", "staff", "prerelease", "cosmos",
            "cracked", "ice", "galaxy", "jumbo", "oversized", "error"}
SUFFIX = {"ex": "ex", "gx": "GX", "v": "V", "vmax": "VMAX", "vstar": "VSTAR", "lv": "LV.", "x": "X"}


def title_name(words):
    return " ".join(SUFFIX.get(w, w.capitalize()) if i else w.capitalize() for i, w in enumerate(words))


PC_PRINT = [  # PriceCharting slug words -> TCGplayer printing (most specific first)
    (("1st", "edition", "holo"), "1st edition holofoil"),
    (("1st", "edition"), "1st edition"),
    (("reverse", "holo"), "reverse holofoil"),
    (("unlimited", "holo"), "unlimited holofoil"),
]


def parse_pc(text):
    m = re.search(r"pricecharting\.com/game/([a-z0-9\-]+)/([a-z0-9\-%]+)", text, re.I)
    if not m:
        return None
    console, prod = m.group(1).lower(), urllib.parse.unquote(m.group(2)).lower()
    set_words = [w for w in console.split("-") if w and w not in ("pokemon",)]
    words = [w for w in prod.split("-") if w]
    number = ""
    if words and re.fullmatch(r"[a-z]{0,4}\d+[a-z]?", words[-1]) and len(words) > 1:
        number = words.pop()
    printing, cut = "", len(words)
    for seq, pr in PC_PRINT:
        n = len(seq)
        for i in range(len(words) - n + 1):
            if tuple(words[i:i + n]) == seq:
                printing, cut = pr, min(cut, i)
                break
        if printing:
            break
    name_words = words[:cut] if printing else words
    # words like "holo", "stamped", "silver border" describe the printing, not the card's name
    extra = [w for w in name_words if w in DESCRIBE]
    name_words = [w for w in name_words if w not in DESCRIBE]
    return {"url": f"https://www.pricecharting.com/game/{console}/{prod}", "set_words": set_words,
            "name_words": name_words, "number": number, "printing": printing, "extra": extra,
            "promo": "promo" in set_words}


def tcg_details(pid):
    """Name, set and number of a TCGplayer product (one request). None if TCGplayer won't say."""
    try:
        req = urllib.request.Request(TCG_DETAILS.format(id=pid), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"TCGplayer product lookup failed: {e}")
        return None
    j = j.get("results", [j])[0] if isinstance(j, dict) and isinstance(j.get("results"), list) and j["results"] else j
    if not isinstance(j, dict):
        return None
    attrs = j.get("customAttributes") or {}
    name = j.get("productName") or j.get("name") or ""
    if not name:
        return None
    return {"name": name, "set": j.get("setName") or j.get("set") or "", "number": attrs.get("number") or ""}


# ---------------------------------------------------------------- helpers
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


def words_of(s):
    return [w for w in re.split(r"[^a-z0-9]+", str(s or "").lower()) if w]


def sets_for(set_words, setlist):
    """Sets whose name contains every PriceCharting set word ("emerald" -> "EX Emerald"), best first."""
    if not set_words:
        return []
    want = set(set_words)
    hits = [s for s in setlist if want <= set(words_of(s.get("name")))]
    return sorted(hits, key=lambda s: len(s.get("name") or ""))


def name_ok(rec, name_words):
    rn = set(words_of(rec.get("name")))
    return bool(name_words) and all(w in rn for w in name_words if not w.isdigit())


def pick_printing(cands, want):
    def v(r):
        return (r.get("variant") or "normal").lower()
    if want:
        return next((r for r in cands if v(r) == want), None) or \
               next((r for r in cands if want.startswith("unlimited") and v(r) == want.replace("unlimited ", "")), None)
    if len(cands) == 1:
        return cands[0]
    return None


def price_list(cands):
    bits = []
    for r in cands:
        nm = rp.cond_prices(r).get("nm", (None,))[0]
        bits.append(f"**{r.get('variant') or 'Normal'}**" + (f" (NM ${nm:,.2f})" if nm else ""))
    return ", ".join(bits)


# ---------------------------------------------------------------- main
def main():
    text = issue_text()
    tcg = parse_tcg(text)
    link, link_pr = tcg if tcg else (None, "")
    pc = parse_pc(text)
    if not link and not pc:
        return out("failed", "❌ I couldn't find a TCGplayer or PriceCharting card link in this issue. They look like "
                             "`https://www.tcgplayer.com/product/12345/...` or "
                             "`https://www.pricecharting.com/game/pokemon-emerald/torchic-reverse-holo-69`.")
    if not rp.API_KEY:
        return out("failed", "❌ The TCGPL_API_KEY secret is missing, so I can't look the card up.")

    # printing: what you picked > the TCGplayer link's ?printing= > the PriceCharting link's words
    picked = ""
    pm = re.search(r"(?im)^\s*printing:\s*(.+?)\s*$", text)
    if pm and pm.group(1).strip().lower() not in ("auto", "auto (from the link)", ""):
        picked = pm.group(1).strip()
    want = (picked or link_pr or (pc or {}).get("printing") or "").lower()

    manifest = json.load(open(rp.MANIFEST, encoding="utf-8"))
    files, cards_by_era, all_cards = {}, {}, []
    for e in manifest["eras"]:
        p = os.path.join(rp.ERAS_DIR, e["id"], e["id"] + ".json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            files[e["id"]] = (p, d)
            cards_by_era[e["id"]] = d.get("cards", [])
            all_cards += d.get("cards", [])

    def dup_reply(c):
        return out("duplicate", f"↩️ Already on the site: **{c['name']} ({c.get('variant', '')})** — "
                                f"{c.get('set', '')} {c.get('number', '')}. Nothing was added.\n\n"
                                f"[Open it on the site]({SITE}#{c['id']})\n\nIf you meant a different printing, "
                                f"add it again and pick the printing.")

    same = [c for c in all_cards if link and str(c.get("tcgId") or "") == link["id"]]
    for c in same:
        if want and printing_of(c) == want:
            return dup_reply(c)
    if pc:
        for c in all_cards:
            if (c.get("pcUrl") or "").lower() == pc["url"]:
                return dup_reply(c)

    state = json.load(open(rp.STATE, encoding="utf-8")) if os.path.exists(rp.STATE) else {"cards": {}, "sets": {}}
    setlist = (state.get("setlist") or {}).get("sets") or [{"name": k, "slug": v} for k, v in (state.get("sets") or {}).items()]
    notes = []

    # What to search for
    details = None
    words = [w for w in (link["slug"].split("-") if link else []) if w and w.lower() not in STOP]
    if link and not words:
        details = tcg_details(link["id"])
        if details:
            words = [w for w in words_of(details["name"]) if w not in STOP]
            notes.append(f"The TCGplayer link had only a product number; TCGplayer says it's **{details['name']}**"
                         + (f" ({details['set']})" if details["set"] else "") + ".")
    pc_sets = sets_for(pc["set_words"], setlist) if pc and not pc["promo"] else []

    searches = []   # (params, why)
    if pc and pc["name_words"]:
        q = " ".join(pc["name_words"])
        for s in pc_sets[:2]:
            searches.append(({"game": "pokemon", "set": s["slug"], "q": q, "limit": 100}, f"{q!r} in {s['name']}"))
    if details and details.get("set"):
        for s in setlist:
            if rp.same_set(s.get("name"), details["set"]):
                searches.append(({"game": "pokemon", "set": s["slug"], "q": " ".join(words), "limit": 100},
                                 f"{' '.join(words)!r} in {s['name']}"))
                break
    for q in (" ".join(words), " ".join(words[-3:]), " ".join(words[-2:]), " ".join(words[:3]), words[-1] if words else ""):
        if q:
            searches.append(({"game": "pokemon", "q": q, "limit": 100}, repr(q)))
    if pc and pc["name_words"]:
        searches.append(({"game": "pokemon", "q": " ".join(pc["name_words"]), "limit": 100}, repr(" ".join(pc["name_words"]))))
    seen, uniq = set(), []
    for p, why in searches:
        k = json.dumps(p, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append((p, why))
    searches = uniq[:6]
    if not searches:
        return out("failed", "❌ The TCGplayer link has no card name in it (only the product number) and TCGplayer "
                             "wouldn't tell me the name. Add the card's **PriceCharting link** too, or copy the full "
                             "link from the card's TCGplayer page.")

    def fits(r):
        if link:
            return str(r.get("tcgplayer_id") or "") == link["id"]
        # PriceCharting only: same number, same name, and the right set (promos: any set)
        if pc["number"] and rp.num_key(r.get("number")) != rp.num_key(pc["number"]):
            return False
        if not name_ok(r, pc["name_words"]):
            return False
        if pc_sets:
            sn = (r.get("set") or {}).get("name") or r.get("set_name") or ""
            return any(rp.same_set(sn, s["name"]) for s in pc_sets)
        return True

    def pc_fits(r):
        if not pc:
            return False
        if pc["number"] and rp.num_key(r.get("number")) != rp.num_key(pc["number"]):
            return False
        return name_ok(r, pc["name_words"])

    api = rp.Api(6)
    cands, tried, pc_hits = [], [], []
    try:
        for params, why in searches:
            if not api.can():
                break
            tried.append(why)
            recs = rp.records(api.get(params))
            cands = [r for r in recs if fits(r)]
            pc_hits += [r for r in recs if pc_fits(r)]
            if cands:
                break
    except rp.Stop as e:
        return out("failed", f"❌ Couldn't look the card up: {e}.")
    except Exception as e:
        return out("failed", f"❌ TCG Price Lookup gave an error: {e}. Try again later.")

    if link and pc and cands:
        # both links: make sure they're the same card
        r0 = cands[0]
        if pc["number"] and rp.num_key(r0.get("number")) != rp.num_key(pc["number"]) or \
           pc["name_words"] and not name_ok(r0, pc["name_words"]):
            return out("failed", f"❌ The two links look like different cards: TCGplayer #{link['id']} is "
                                 f"**{r0.get('name')}** {r0.get('number', '')}, but the PriceCharting link is "
                                 f"**{title_name(pc['name_words'])} #{pc['number']}**. Check the links and add it again.")

    if link and pc and not cands:
        other = next((r for r in pc_hits if str(r.get("tcgplayer_id") or "") not in ("", link["id"])), None)
        if other:
            return out("failed", f"❌ The two links look like different cards: the PriceCharting link is "
                                 f"**{other.get('name')}** {other.get('number', '')} (TCGplayer #{other.get('tcgplayer_id')}), "
                                 f"but the TCGplayer link is product #{link['id']}. Check the links and add it again.")
    if not cands:
        return add_manual(link, pc, details, want, picked, tried, manifest, files, cards_by_era, all_cards, setlist, pc_sets, notes)

    rec = pick_printing(cands, want)
    if not rec and not want:
        # PriceCharting names reverse holo / 1st edition, so no printing word means the plain card
        plain = [r for r in cands if (r.get("variant") or "normal").lower() in ("holofoil", "normal")]
        if pc and len(plain) == 1:
            rec = plain[0]
    if not rec:
        if want:
            return out("failed", f"❌ That card doesn't come in **{want.title()}** on TCG Price Lookup. "
                                 f"Printings found: {price_list(cands)}. Add it again and pick one of those.")
        return out("failed", f"❔ This card comes in more than one printing: {price_list(cands)}. "
                             f"Add it again and pick the printing you want.")

    printing = (rec.get("variant") or "Normal")
    tid = str(rec.get("tcgplayer_id") or (link or {}).get("id") or "")
    for c in all_cards:
        if tid and str(c.get("tcgId") or "") == tid and printing_of(c) == printing.lower():
            if pc and not c.get("pcUrl"):
                notes.append("Tip: re-adding won't save the PriceCharting link on an existing card.")
            return dup_reply(c)

    rp.learn_ratios(all_cards)
    prices, fixes = rp.checked_prices(rec)
    when = (rec.get("last_price_update") or rec.get("updated_at") or rp.TODAY)[:10]
    raw = {}
    for k in ("nm", "lp", "mp", "dmg"):
        if k in prices:
            raw[k] = rp.raw_entry(k, prices[k], when, fixes)
    set_name = (rec.get("set") or {}).get("name") or rec.get("set_name") or ""
    card = {
        "name": rec.get("name") or "", "variant": LABELS.get(printing.lower(), printing),
        "set": set_name, "number": rec.get("number") or "", "raw": raw,
        "image": rec.get("image_url") or "", "updated": rp.TODAY, "tcgVariant": printing,
    }
    if tid:
        card["tcgId"] = int(tid)
    if pc:
        card["pcUrl"] = pc["url"]
    cid, era = save_card(card, set_name, manifest, files, cards_by_era, all_cards, tid or slugify(printing))

    state.setdefault("cards", {})[cid] = {"id": rec.get("id"), "synced": rp.TODAY, "variant": printing}
    if (rec.get("set") or {}).get("slug"):
        state.setdefault("sets", {}).setdefault(set_name, rec["set"]["slug"])
    with open(rp.STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)

    lines = notes + ([""] if notes else []) + [
        f"✅ Added **{card['name']} ({card['variant']})** — {set_name} {card['number']} to the "
        f"{era_name(manifest, era)} era.", "", "| Condition | Price |", "|---|---|"]
    for k, lab in (("nm", "NM"), ("lp", "LP"), ("mp", "MP"), ("dmg", "DMG")):
        x = raw.get(k)
        cell = ("$%.2f" % x["price"] + (" (est.)" if x["est"] else "")) if x else "—"
        lines.append("| " + lab + " | " + cell + " |")
    lines += ["", f"Source: TCGplayer market (via TCG Price Lookup)."
                  + (" The PriceCharting link is saved on the card." if pc else "")
                  + f" It shows on the site in about a minute: [open card]({SITE}#{cid})"]
    if not raw:
        lines += ["", "⚠️ TCG Price Lookup had no condition prices for it yet, so the prices are blank for now. "
                      "The daily update fills them in when they appear."]
    return out("added", "\n".join(lines))


def era_name(manifest, era):
    return next((e["name"] for e in manifest["eras"] if e["id"] == era), era)


def save_card(card, set_name, manifest, files, cards_by_era, all_cards, tail):
    era = era_for(set_name, manifest, cards_by_era)
    if era not in files:
        era = "other"
    taken = {c["id"] for c in all_cards}
    base = "-".join(x for x in (slugify(card["name"]), slugify(card.get("number")), slugify(tail)) if x)
    cid = base if base not in taken else f"{base}-{slugify(card.get('tcgVariant') or card.get('variant'))[:8]}"
    n = 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    card = {"id": cid, **card}
    path, data = files[era]
    data.setdefault("cards", []).append(card)
    data["version"] = str(int(time.time()))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return cid, era


def add_manual(link, pc, details, want, picked, tried, manifest, files, cards_by_era, all_cards, setlist, pc_sets, notes):
    """Not on TCG Price Lookup: add it from the link details with blank prices, if we know enough."""
    name = set_name = number = ""
    if details:
        name, set_name, number = details["name"], details["set"], details["number"]
    if pc:
        name = name or title_name(pc["name_words"])
        number = number or pc["number"]
        if not set_name:
            set_name = pc_sets[0]["name"] if pc_sets else ("Promo" if pc["promo"] else " ".join(pc["set_words"]).title())
    searched = ", ".join(tried) or "nothing"
    if not name or not (pc or details):
        what = f"TCGplayer product {link['id']}" if link else "that card"
        return out("failed", f"❌ I couldn't find {what} on TCG Price Lookup (searched: {searched}). It may not be "
                             f"listed there. Add its **PriceCharting link** too and I'll add it with blank prices "
                             f"so you can track it.")
    printing = want or "normal"
    if want == "" and pc and pc["extra"] and "holo" in pc["extra"]:
        printing = "holofoil"
    dup = next((c for c in all_cards if rp.norm(c.get("name")) == rp.norm(name) and rp.num_key(c.get("number")) == rp.num_key(number)
                and rp.same_set(c.get("set"), set_name) and printing_of(c) == printing.lower()), None)
    if dup:
        return out("duplicate", f"↩️ Already on the site: **{dup['name']} ({dup.get('variant', '')})** — "
                                f"{dup.get('set', '')} {dup.get('number', '')}. Nothing was added.\n\n[Open it on the site]({SITE}#{dup['id']})")
    label = LABELS.get(printing.lower(), printing.title())
    desc = [w for w in (pc or {}).get("extra", []) if w not in ("holo", "foil", "promo")]
    if desc:
        label += " · " + " ".join(w.capitalize() for w in desc)
    card = {"name": name, "variant": label, "set": set_name, "number": number, "raw": {},
            "updated": rp.TODAY, "tcgVariant": printing.title(), "noApi": True,
            "notes": "Not on TCG Price Lookup: prices aren't updated automatically. Check PriceCharting / eBay sold."}
    if link:
        card["tcgId"] = int(link["id"])
    if pc:
        card["pcUrl"] = pc["url"]
    cid, era = save_card(card, set_name, manifest, files, cards_by_era, all_cards, (link or {}).get("id") or "pc")
    lines = notes + ([""] if notes else []) + [
        f"⚠️ Added **{name} ({label})** — {set_name} {number} to the {era_name(manifest, era)} era, "
        f"**without prices**.", "",
        f"TCG Price Lookup doesn't list this card or printing (searched: {searched}), which is common for "
        f"stamped and alternate printings. The card is on the site so you can track it; its PriceCharting"
        + (" (saved link)" if pc else "") + " and eBay sold buttons open its sales. "
        f"[Open card]({SITE}#{cid})"]
    return out("added", "\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
