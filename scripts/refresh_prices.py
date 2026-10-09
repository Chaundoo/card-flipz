#!/usr/bin/env python3
"""Daily raw-price update for Chaundoo Card Flipz (runs in GitHub Actions).

Prices come from the TCG Price Lookup API (https://tcgpricelookup.com), which
carries TCGplayer market prices per condition. Nothing here contacts TCGplayer;
the site's "TCGplayer" listing links are plain links built from each card's tcgId.

How the week is spread out
  * Every day the job updates about 1/7 of the cards (DAILY_SHARE), oldest first:
    cards that have never been synced come first, then the ones synced longest ago.
  * It never uses more than DAILY_REQUESTS API requests (default 90 of the free
    plan's 100/day) and also stops when the API says the daily quota is used up.
  * Requests are spaced several seconds apart to respect the burst limit.

Only raw.nm / raw.lp / raw.mp / raw.dmg and "updated" are changed on a card.
Graded values, pop counts, notes, images, pairings and tcgId stay as they are.

Matching cards to TCG Price Lookup is remembered in scripts/tcgpl_state.json
(kept out of the era files so the site stays light on mobile data).

Settings (environment variables):
  TCGPL_API_KEY   your API key (GitHub secret)                       required
  DAILY_REQUESTS  max API requests per run                           default 90
  DAILY_SHARE     cards per run (0 = total cards / 7, rounded up)    default 0
  DRY_RUN         "true" = fetch prices but don't save anything      default false
  TCGPL_BASE      API base URL (only for testing)
"""
import json, os, sys, time, random, datetime, re, math
import urllib.request, urllib.error, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERAS_DIR = os.path.join(ROOT, "eras")
MANIFEST = os.path.join(ERAS_DIR, "eras.json")
STATE = os.path.join(ROOT, "scripts", "tcgpl_state.json")
LOG = os.path.join(ROOT, "PRICE_LOG.md")
REPORT = os.path.join(ROOT, "run_report.md")

API_KEY = (os.environ.get("TCGPL_API_KEY") or "").strip()
BASE = (os.environ.get("TCGPL_BASE") or "https://api.tcgpricelookup.com/v1").rstrip("/")
DAILY_REQUESTS = int(os.environ.get("DAILY_REQUESTS") or 90)
DAILY_SHARE = int(os.environ.get("DAILY_SHARE") or 0)
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() == "true"
PAUSE = (float(os.environ.get("PAUSE_MIN") or 4), float(os.environ.get("PAUSE_MAX") or 8))
BATCH = 20                # cards per batch lookup
PAGE = 100                # cards per page when listing a set
RETRY_FAILED_DAYS = 7     # a card that couldn't be matched is retried after this many days
KEEP_LOG_RUNS = 30
SRC = "TCGplayer market (via TCG Price Lookup)"
SRC_MID = "TCGplayer mid price (via TCG Price Lookup; no market price)"
UA = "card-flipz-price-updater/1.0 (+https://github.com/Chaundoo/card-flipz)"

TODAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8))).date().isoformat()


class Stop(Exception):
    """Stop the whole run (quota used up, bad key...)."""


# ---------------------------------------------------------------- API
class Api:
    def __init__(self, budget):
        self.budget = budget
        self.used = 0
        self.remaining = None   # what the API says is left today
        self.last = 0.0

    def can(self, n=1):
        left = self.budget - self.used
        if self.remaining is not None:
            left = min(left, self.remaining - 2)   # keep a couple for you to use by hand
        return left >= n

    def get(self, params):
        if not self.can():
            raise Stop("daily request budget used")
        url = BASE + "/cards/search?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            wait = random.uniform(*PAUSE) - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            req = urllib.request.Request(url, headers={"X-API-Key": API_KEY, "Accept": "application/json", "User-Agent": UA})
            self.used += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._quota(r.headers)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                self._quota(e.headers)
                body = e.read().decode("utf-8", "replace")[:200]
                if e.code in (401, 403):
                    raise Stop(f"the API rejected the key (HTTP {e.code}). Check the TCGPL_API_KEY secret. {body}")
                if e.code == 429:
                    if "daily" in body.lower():
                        raise Stop("TCG Price Lookup's daily request limit is used up")
                    time.sleep(float(e.headers.get("Retry-After") or 10) + 1)
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"network error: {e}")
        raise RuntimeError("gave up after retries")

    def _quota(self, h):
        try:
            if h and h.get("X-RateLimit-Remaining") is not None:
                self.remaining = int(h.get("X-RateLimit-Remaining"))
        except ValueError:
            pass


def records(j):
    return [r for r in ((j or {}).get("data") or []) if isinstance(r, dict)]


# ---------------------------------------------------------------- matching
def num_key(n):
    n = str(n or "").split("/")[0].strip().lower()
    return n.lstrip("0") or n


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def wanted_printing(label):
    l = (label or "").lower()
    holo = "holo" in l and "non-holo" not in l
    if "reverse" in l:
        return "reverse holofoil"
    if "1st edition" in l:
        return "1st edition holofoil" if holo else "1st edition"
    if "unlimited" in l:
        return "unlimited holofoil" if holo else "unlimited"
    if "non-holo" in l or "normal" in l:
        return "normal"
    return "holofoil"


def pick(card, cands):
    """Choose the right printing among candidate records."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    w = wanted_printing(card.get("variant"))
    for want in (card.get("tcgVariant"), w, w.replace("unlimited ", "") if w.startswith("unlimited") else None):
        if want:
            hit = [r for r in cands if (r.get("variant") or "").lower() == want.lower()]
            if len(hit) >= 1:
                return hit[0]
    # fall back to the one closest to the NM price the card already shows
    old = ((card.get("raw") or {}).get("nm") or {}).get("price")
    if isinstance(old, (int, float)) and old > 0:
        def dist(r):
            p = cond_prices(r).get("nm", (None,))[0]
            return abs(p - old) / old if p else 9e9
        return min(cands, key=dist)
    return None


def match(card, recs):
    tid = str(card.get("tcgId") or "")
    by_tid = [r for r in recs if tid and str(r.get("tcgplayer_id") or "") == tid]
    n, nm, st = num_key(card.get("number")), norm(card.get("name")), norm(card.get("set"))
    by_card = [r for r in recs if num_key(r.get("number")) == n
               and (not st or norm((r.get("set") or {}).get("name")) == st or norm(r.get("set_name")) == st)
               and (nm in norm(r.get("name")) or norm(r.get("name")) in nm)
               and not (tid and r.get("tcgplayer_id") and str(r["tcgplayer_id"]) != tid)]
    # same TCGplayer product first, plus same set/number/name records (other printings)
    seen, c = set(), []
    for r in by_tid + by_card:
        if id(r) not in seen:
            seen.add(id(r))
            c.append(r)
    return pick(card, c)


COND_KEYS = {"nearmint": "nm", "nm": "nm", "lightlyplayed": "lp", "lp": "lp",
             "moderatelyplayed": "mp", "mp": "mp", "damaged": "dmg", "dmg": "dmg"}


def cond_prices(rec):
    """{'nm': (price, is_estimate), ...} from one API record."""
    out = {}
    raw = ((rec.get("prices") or {}).get("raw") or {})
    for k, v in raw.items():
        c = COND_KEYS.get(norm(k))
        t = (v or {}).get("tcgplayer") or {}
        if not c:
            continue
        if isinstance(t.get("market"), (int, float)) and t["market"] > 0:
            out[c] = (float(t["market"]), False)
        elif isinstance(t.get("mid"), (int, float)) and t["mid"] > 0:
            out[c] = (float(t["mid"]), True)
    return out


# ---------------------------------------------------------------- main
def main():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    files, file_of, cards = [], {}, []
    for era in manifest["eras"]:
        path = os.path.join(ERAS_DIR, era["id"], era["id"] + ".json")
        if os.path.exists(path):
            data = json.load(open(path, encoding="utf-8"))
            files.append((path, data))
            for c in data.get("cards", []):
                file_of[c["id"]] = len(files) - 1
                cards.append(c)
    by_id = {c["id"]: c for c in cards}
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    state.setdefault("cards", {})
    state.setdefault("sets", {})
    st = state["cards"]

    share = DAILY_SHARE or math.ceil(len(cards) / 7)

    def sort_key(c):
        s = st.get(c["id"]) or {}
        last = s.get("synced") or ""
        if s.get("failed") and not s.get("id"):
            # unmatched cards wait RETRY_FAILED_DAYS before another try
            d = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(s["failed"])).days
            if d < RETRY_FAILED_DAYS:
                return (2, s["failed"])
            last = s["failed"]
        return (0 if not last else 1, last)

    queue = sorted(cards, key=sort_key)
    queue = [c for c in queue if sort_key(c)[0] != 2]

    api = Api(DAILY_REQUESTS)
    done, problems, moves = set(), [], []
    stats = {"updated": 0, "same": 0, "noprice": 0, "nomatch": 0, "error": 0}
    dirty = set()
    set_cache = {}
    stop_reason = None

    def apply(card, rec):
        s = st.setdefault(card["id"], {})
        s["id"] = rec.get("id")
        s.pop("failed", None)
        s["synced"] = TODAY
        if rec.get("variant"):
            s["variant"] = rec["variant"]
        done.add(card["id"])
        prices = cond_prices(rec)
        if not prices:
            stats["noprice"] += 1
            return
        when = (rec.get("last_price_update") or rec.get("updated_at") or TODAY)[:10]
        raw = card.setdefault("raw", {})
        changed = False
        for c in ("nm", "lp", "mp", "dmg"):
            if c not in prices:
                continue   # no price for this condition: keep the old one
            p, est = prices[c]
            new = {"price": round(p, 2), "date": when, "src": SRC_MID if est else SRC, "est": est}
            old = raw.get(c) or {}
            if c == "nm" and isinstance(old.get("price"), (int, float)) and old["price"] > 0 \
                    and abs(new["price"] - old["price"]) / old["price"] >= 0.15:
                moves.append((label(card), old["price"], new["price"]))
            if old != new:
                raw[c] = new
                changed = True
        if changed:
            card["updated"] = TODAY
            dirty.add(file_of[card["id"]])
            stats["updated"] += 1
        else:
            stats["same"] += 1

    def fail(card, kind, why):
        st.setdefault(card["id"], {})["failed"] = TODAY
        done.add(card["id"])
        stats[kind] += 1
        problems.append((kind, label(card), why))

    def list_set(slug):
        if slug in set_cache:
            return set_cache[slug]
        out, offset = [], 0
        while True:
            j = api.get({"game": "pokemon", "set": slug, "limit": PAGE, "offset": offset})
            out += records(j)
            total = int((j or {}).get("total") or 0)
            offset += PAGE
            if offset >= total or not records(j):
                break
        set_cache[slug] = out
        return out

    def map_set(set_name, recs):
        """Match every not-yet-matched card of this set against a set listing."""
        for c in cards:
            if c["id"] in done or c.get("set") != set_name or (st.get(c["id"]) or {}).get("id"):
                continue
            r = match(c, recs)
            if r:
                apply(c, r)

    t0 = time.time()
    try:
        if not API_KEY:
            raise Stop("no API key yet. Add the TCGPL_API_KEY secret in the repo settings")
        i = 0
        while i < len(queue) and len(done) < share:
            card = queue[i]
            i += 1
            if card["id"] in done:
                continue
            s = st.get(card["id"]) or {}
            try:
                if s.get("id"):
                    # batch lookup of up to 20 already-matched cards, oldest first
                    batch = [card] + [c for c in queue[i:] if c["id"] not in done and (st.get(c["id"]) or {}).get("id")][:BATCH - 1]
                    batch = batch[:max(1, min(BATCH, share - len(done)))]
                    j = api.get({"ids": ",".join(st[c["id"]]["id"] for c in batch), "limit": len(batch)})
                    got = {r.get("id"): r for r in records(j)}
                    for c in batch:
                        r = got.get(st[c["id"]]["id"])
                        if r:
                            apply(c, r)
                        else:
                            st[c["id"]].pop("id", None)   # re-match next time
                            fail(c, "nomatch", "TCG Price Lookup didn't return this card; will re-match it")
                    continue
                slug = state["sets"].get(card.get("set"))
                if slug:
                    recs = list_set(slug)
                    r = match(card, recs)
                    if not r:
                        # maybe the remembered set is wrong for this card; try a direct search
                        r = match(card, records(api.get({"game": "pokemon", "q": f"{card['name']} {num_key(card.get('number'))}", "limit": 50})))
                else:
                    r = match(card, records(api.get({"game": "pokemon", "q": f"{card['name']} {num_key(card.get('number'))}", "limit": 50})))
                    if r and (r.get("set") or {}).get("slug"):
                        state["sets"][card["set"]] = r["set"]["slug"]
                if r:
                    apply(card, r)
                    slug = state["sets"].get(card.get("set"))
                    others = [c for c in queue[i:] if c.get("set") == card.get("set") and c["id"] not in done
                              and not (st.get(c["id"]) or {}).get("id")]
                    if slug and others and len(done) < share:
                        map_set(card["set"], list_set(slug))
                else:
                    fail(card, "nomatch", "couldn't find this card on TCG Price Lookup")
            except Stop:
                raise
            except Exception as e:
                stats["error"] += 1
                problems.append(("error", label(card), str(e)))
                if stats["error"] >= 8:
                    raise Stop("too many errors in a row; stopped to be safe")
    except Stop as e:
        stop_reason = str(e)
    secs = int(time.time() - t0)

    never = sum(1 for c in cards if not (st.get(c["id"]) or {}).get("synced"))
    oldest = min([(st.get(c["id"]) or {}).get("synced") for c in cards if (st.get(c["id"]) or {}).get("synced")] or ["—"])
    if not API_KEY or (stop_reason and "rejected" in stop_reason):
        status = "FAILED"
    elif stats["error"] or (stop_reason and not done):
        status = "PARTIAL"
    else:
        status = "OK"
    saved = not DRY_RUN and status != "FAILED"

    if saved:
        stamp = str(int(time.time()))
        for k in sorted(dirty):
            path, data = files[k]
            data["version"] = stamp
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        state["about"] = ("Written by scripts/refresh_prices.py. Remembers which TCG Price Lookup card each site card is, "
                          "and when it was last synced. Safe to delete (cards just get re-matched).")
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)

    icon = {"OK": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}[status]
    lines = [f"## {icon} {status} — price update {TODAY}" + (" (test run, not saved)" if DRY_RUN else ""), ""]
    if stop_reason:
        lines += [f"Stopped early: {stop_reason}.", ""]
    lines += [
        f"- Cards checked today: **{len(done)}** (daily share {share} of {len(cards)}) in {secs // 60}m {secs % 60}s",
        f"- API requests used: **{api.used}** of {DAILY_REQUESTS}" + (f" (API says {api.remaining} left today)" if api.remaining is not None else ""),
        f"- ✅ Prices changed: **{stats['updated']}**",
        f"- ➖ Checked, prices unchanged: {stats['same']}",
        f"- ❔ No TCGplayer price on TCG Price Lookup (kept old prices): {stats['noprice']}",
        f"- ❔ Couldn't match the card (retried in {RETRY_FAILED_DAYS} days): {stats['nomatch']}",
        f"- ⚠️ Errors: {stats['error']}",
        f"- Cards never synced yet: **{never}** · oldest sync: {oldest}",
        f"- Era files saved: **{len(dirty) if saved else 'none'}**",
    ]
    if moves:
        moves.sort(key=lambda m: -abs(m[2] - m[1]) / m[1])
        lines += ["", "**Biggest NM moves (15%+):**"] + [f"- {n}: ${o:,.2f} → ${nw:,.2f}" for n, o, nw in moves[:10]]
    if problems:
        lines += ["", f"**Problem cards ({len(problems)}):**"] + [f"- {k}: {n} — {w}" for k, n, w in problems[:40]]
        if len(problems) > 40:
            lines.append(f"- …and {len(problems) - 40} more (see the Actions run log)")
            for k, n, w in problems[40:]:
                print(f"{k}: {n} — {w}")
    report = "\n".join(lines) + "\n"
    open(REPORT, "w", encoding="utf-8").write(report)
    print(report)

    if not DRY_RUN:
        old = open(LOG, encoding="utf-8").read() if os.path.exists(LOG) else ""
        runs = [r.rstrip() + "\n" for r in re.findall(r"(?ms)^## .*?(?=^## |\Z)", old)]
        body = "# Price update log\n\nNewest first. Written automatically by the daily GitHub job.\n\n" + \
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


def label(c):
    return f"{c.get('name')} ({c.get('variant', '')}) — {c.get('set', '')} {c.get('number', '')}"


if __name__ == "__main__":
    sys.exit(main())
