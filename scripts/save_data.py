"""Save pairings + profit log changes sent from the site.

The site's "Save to GitHub" button opens an issue titled "Save data: ..." whose body holds a JSON block:
  {"v":1, "pairings":{"up":[...], "del":[ids]}, "shows":{"up":[...], "del":[ids]}}
"up" items replace the item with the same id (or are added); "del" ids are removed.
This script checks the data, writes pairings.json / profitlog.json, and leaves a report in run_report.md.
"""
import json, os, re, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONDS = {"nm", "lp", "mp", "dmg"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_ITEMS = 500


class Bad(Exception):
    pass


def out(status):
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"status={status}\n")


def report(text):
    with open(os.path.join(ROOT, "run_report.md"), "w") as f:
        f.write(text)


def need(cond, msg):
    if not cond:
        raise Bad(msg)


def is_money(v):
    return v == "" or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e7)


def check_keys(obj, keys, what):
    need(isinstance(obj, dict), f"{what} isn't an object")
    extra = set(obj) - set(keys)
    missing = set(keys) - set(obj)
    need(not extra and not missing, f"{what} has unexpected fields ({', '.join(sorted(extra | missing))})")


def check_id(v, what):
    need(isinstance(v, str) and ID_RE.match(v), f"{what} has a bad id")


def check_str(v, n, what):
    need(isinstance(v, str) and len(v) <= n, f"{what} is too long or not text")


def check_pairing(p):
    check_keys(p, ["id", "name", "created", "items"], "A pairing")
    check_id(p["id"], "A pairing")
    check_str(p["name"], 60, "A pairing name")
    check_str(p["created"], 10, "A pairing date")
    need(isinstance(p["items"], list) and len(p["items"]) <= MAX_ITEMS, f"Pairing '{p['name']}' has too many cards")
    for it in p["items"]:
        check_keys(it, ["cardId", "bought", "sellAs", "paid"], f"A card in '{p['name']}'")
        check_str(it["cardId"], 120, "A card id")
        need(it["bought"] in CONDS and it["sellAs"] in CONDS, f"A card in '{p['name']}' has an unknown condition")
        need(is_money(it["paid"]), f"A card in '{p['name']}' has a bad price paid")


B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
MAX_CT = 60000  # one encrypted card show (cards + notes)


def check_show(s):
    """Card shows arrive locked (encrypted on the phone with the Profit Log passcode).
    The bot never sees the passcode or what's inside; it only checks the shape and stores it."""
    check_keys(s, ["id", "iv", "ct"], "A card show")
    check_id(s["id"], "A card show")
    need(isinstance(s["iv"], str) and len(s["iv"]) == 16 and B64_RE.match(s["iv"]), "A card show isn't locked properly (bad iv)")
    need(isinstance(s["ct"], str) and 24 <= len(s["ct"]) <= MAX_CT and B64_RE.match(s["ct"]),
         "A card show isn't locked properly, or its notes are too long")


def apply(items, change, check, new_first):
    need(isinstance(change, dict), "Bad change list")
    ups, dels = change.get("up", []), change.get("del", [])
    need(isinstance(ups, list) and isinstance(dels, list), "Bad change list")
    for u in ups:
        check(u)
    for d in dels:
        check_id(d, "A delete")
    by_id = {x.get("id"): i for i, x in enumerate(items)}
    added = updated = 0
    for u in ups:
        if u["id"] in by_id:
            items[by_id[u["id"]]] = u
            updated += 1
        else:
            if new_first:
                items.insert(0, u)
            else:
                items.append(u)
            added += 1
        by_id = {x.get("id"): i for i, x in enumerate(items)}
    gone = set(dels)
    before = len(items)
    items[:] = [x for x in items if x.get("id") not in gone]
    return added, updated, before - len(items)


def load(path, key):
    try:
        with open(path) as f:
            d = json.load(f)
        if isinstance(d.get(key), list):
            return d
    except FileNotFoundError:
        pass
    return {"version": "0", key: []}


def save(path, d):
    with open(path, "w") as f:
        json.dump(d, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")


def main():
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        body = (json.load(f).get("issue") or {}).get("body") or ""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    raw = m.group(1) if m else body[body.find("{"): body.rfind("}") + 1]
    try:
        payload = json.loads(raw)
    except Exception:
        raise Bad("I couldn't find the saved data in this issue. Go back to the site and tap Save to GitHub again "
                  "(if it said the changes were copied, paste them into the description box before pressing Create).")
    need(isinstance(payload, dict) and payload.get("v") == 1, "This doesn't look like data from the site.")

    stamp = str(int(time.time()))
    lines = []
    for key, fname, check, new_first, label in (
        ("pairings", "pairings.json", check_pairing, True, "pairing"),
        ("shows", "profitlog.json", check_show, False, "card show"),
    ):
        change = payload.get(key) or {"up": [], "del": []}
        if not change.get("up") and not change.get("del"):
            continue
        path = os.path.join(ROOT, fname)
        d = load(path, key)
        if key == "shows":
            need(isinstance(d.get("enc"), dict), "profitlog.json has no lock settings (enc), so locked card shows can't be saved")
        a, u, r = apply(d[key], change, check, new_first)
        d["version"] = stamp
        save(path, d)
        parts = [f"{n} {w}" for n, w in ((a, "added"), (u, "updated"), (r, "deleted")) if n]
        lines.append(f"- **{label.capitalize()}s:** " + (", ".join(parts) if parts else "no changes"))

    if not lines:
        report("ℹ️ Nothing to save: this issue didn't contain any changes.\n")
        out("nothing")
        return
    try:
        import botlog
        botlog.summary("Saved your changes to the site: " + "; ".join(l.replace("- ", "", 1).replace("**", "") for l in lines) + ".", "ok")
    except Exception:
        pass
    report("✅ Saved to the site:\n\n" + "\n".join(lines) + "\n\nRefresh the site (or tap **Check**) and the changes show for everyone.\n")
    out("saved")


if __name__ == "__main__":
    try:
        main()
    except Bad as e:
        report(f"❌ Not saved: {e}\n\nNothing was changed. Your changes are still on your phone; fix the problem on the site and tap Save to GitHub again.\n")
        out("error")
    except Exception as e:  # unexpected crash: still reply on the issue
        report(f"❌ Not saved: the save script crashed ({type(e).__name__}). Nothing was changed; your changes are still on your phone.\n")
        out("error")
        sys.exit(0)
