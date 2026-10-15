#!/usr/bin/env python3
"""Raw-price update for Chaundoo Card Flipz (runs in GitHub Actions).

Prices come from the TCG Price Lookup API (https://tcgpricelookup.com), which
carries TCGplayer market prices per condition. Nothing here contacts TCGplayer;
the site's "TCGplayer" listing links are plain links built from each card's tcgId.

How a run works (Monday-Friday mornings, see .github/workflows/price-refresh.yml)
  1. Prices: every matched card is refreshed, 100 cards per request (the API's batch
     limit). ~1,900 cards is about 19 requests and a couple of minutes.
  2. Matching: cards that aren't matched to TCG Price Lookup yet are grouped by set.
     Each set's card list is downloaded once (100 cards per request) and every
     unmatched card in it is matched from that list. Set names come from the API's
     full set list, kept in scripts/tcgpl_state.json. Skipped on show days.
  Requests are spaced 3.5 s apart (the free plan allows 1 per 3 s).

Request budget (scripts/api_budget.json): the free plan has 100 requests a day,
reset at 00:00 UTC (5 pm Pacific in summer, 4 pm in winter). A run never uses more
than price_update_max_requests and always leaves keep_free_weekdays requests unused,
or keep_free_show_days on show days (Friday-Sunday) for adding cards at shows.

Odd one-off sales: before saving, the four condition prices are checked against each
other (see "outlier check" below). If a lower condition is priced above a higher one,
e.g. LP $999 over NM $797 because of one strange sale, that price is replaced with an
estimate from the other conditions and marked "est." on the site. An NM price far
above LP (e.g. NM $540 vs LP $76 from one strange NM sale) is also replaced with an
estimate, and the card is queued for the weekly TCGplayer sales check.

TCGplayer sales check: scripts/tcgplayer_check.py (random time Monday-Thursday) looks at
the real TCGplayer sales for skewed and top-flip cards and saves them in
scripts/tcgplayer_verified.json. While a checked price is fresh (see "trust_days" in
scripts/tcgplayer_config.json) it wins over the TCG Price Lookup price for that condition.

Only raw.nm / raw.lp / raw.mp / raw.dmg and "updated" are changed on a card.
Graded values, pop counts, notes, images, pairings and tcgId stay as they are.

Settings (environment variables, mostly for running by hand):
  TCGPL_API_KEY   your API key (GitHub secret)                       required
  DAILY_REQUESTS  max API requests this run (blank = api_budget.json)
  DAILY_SHARE     max cards to refresh this run (0 = all)            default 0
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
BUDGET_FILE = os.path.join(ROOT, "scripts", "api_budget.json")
DEFAULT_BUDGET = {"price_update_max_requests": 60, "flip_finder_max_requests": 85,
                  "keep_free_weekdays": 10, "keep_free_show_days": 70, "show_days_utc": ["Fri", "Sat", "Sun"]}


def load_budget():
    b = dict(DEFAULT_BUDGET)
    try:
        b.update({k: v for k, v in json.load(open(BUDGET_FILE, encoding="utf-8")).items() if k in DEFAULT_BUDGET})
    except (OSError, ValueError):
        pass
    return b


def is_show_day(budget=None):
    """Show days are counted in UTC because that's when the API's daily count resets."""
    days = [d[:3].title() for d in (budget or load_budget())["show_days_utc"]]
    return datetime.datetime.now(datetime.timezone.utc).strftime("%a") in days


def keep_free(budget=None):
    b = budget or load_budget()
    return int(b["keep_free_show_days"] if is_show_day(b) else b["keep_free_weekdays"])


DAILY_REQUESTS = int(os.environ.get("DAILY_REQUESTS") or 0) or int(load_budget()["price_update_max_requests"])
DAILY_SHARE = int(os.environ.get("DAILY_SHARE") or 0)
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() == "true"
PAUSE = (float(os.environ.get("PAUSE_MIN") or 3.4), float(os.environ.get("PAUSE_MAX") or 3.7))
BATCH = 100               # cards per batch lookup (the API's limit)
SETLIST_DAYS = 14         # re-download the API's set list this often
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
    def __init__(self, budget, reserve=2):
        self.budget = budget
        self.reserve = reserve  # requests that must stay unused today
        self.used = 0
        self.remaining = None   # what the API says is left today
        self.last = 0.0

    def can(self, n=1):
        left = self.budget - self.used
        if self.remaining is not None:
            left = min(left, self.remaining - self.reserve)
        return left >= n

    def get(self, params):
        return self.get_path("/cards/search", params)

    def get_path(self, path, params):
        if not self.can():
            raise Stop("request budget for this run used (the rest is kept free)")
        url = BASE + path + "?" + urllib.parse.urlencode(params)
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


SET_PREFIX = re.compile(r"^\s*(?:pok[eé]mon\s*[-:]\s*)?(?:xy|sm|bw|dp|hgss|swsh\s*\d*|sv\s*\d*|me\s*\d*)\s*[-:]\s*", re.I)


def set_keys(name):
    """'XY - Roaring Skies', 'Roaring Skies' and 'SWSH07: Evolving Skies' style names all match."""
    full = norm(name)
    short = norm(SET_PREFIX.sub("", str(name or "")))
    return {k for k in (full, short) if k}


def same_set(a, b):
    return bool(set_keys(a) & set_keys(b))


def match(card, recs):
    tid = str(card.get("tcgId") or "")
    by_tid = [r for r in recs if tid and str(r.get("tcgplayer_id") or "") == tid]
    n, nm, st = num_key(card.get("number")), norm(card.get("name")), card.get("set")
    by_card = [r for r in recs if num_key(r.get("number")) == n
               and (not st or same_set((r.get("set") or {}).get("name"), st) or same_set(r.get("set_name"), st))
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


# ---------------------------------------------------------------- outlier check
# TCGplayer's "market" price is an average of a few recent sales, so on thin cards one
# odd (or manipulated) sale can push LP above NM, MP above LP, and so on. Before a
# price is saved, the four conditions are checked against each other:
#   * Two conditions "agree" when the worse one is not above the better one (a little
#     noise is allowed) and not absurdly far below it, judged by how far apart those
#     two conditions usually are across all the cards on the site.
#   * The biggest group of conditions that all agree with each other is trusted. Ties
#     go to the group whose spacing looks most normal, and away from prices that sit
#     well above TCGplayer's cheapest current listing of that condition or better.
#   * A condition outside that group is rebuilt from the trusted ones using the usual
#     spacing, kept between its neighbours, and never above the cheapest listing of
#     that condition or better. It is saved as an estimate ("est.") with the odd TCGplayer
#     price kept in the source note so you can see what was replaced.
#   * If nothing can be trusted (only two prices that disagree, etc.), the worse
#     condition is simply capped at the better one, so it can't show a fake flip profit.
CONDS = ("nm", "lp", "mp", "dmg")
DEFAULT_RATIOS = {"nm": 1.0, "lp": 0.57, "mp": 0.38, "dmg": 0.20}   # usual price vs NM
NOISE_PCT, NOISE_USD = 0.08, 0.50   # worse condition may sit this far above the better one
FLOOR = 0.10                        # ...and down to 10% of the usual spacing below it (big NM premiums are real)
LISTING_GAP = 1.5                   # a sale 1.5x the cheapest same-or-better listing looks odd
NM_SPIKE = 3.5                      # NM above 3.5x LP (or 6x MP with no LP) is treated as one odd NM sale
NM_SPIKE_MP = 6.0
NM_SPIKE_WHY = "far above the lower conditions, likely one odd NM sale; waiting for the weekly TCGplayer sales check"
RATIOS = dict(DEFAULT_RATIOS)


def learn_ratios(cards):
    """Usual LP/MP/DMG price as a share of NM, from cards whose ladder already looks right."""
    import statistics
    got = {k: [] for k in CONDS[1:]}
    for c in cards:
        p = {k: ((c.get("raw") or {}).get(k) or {}).get("price") for k in CONDS}
        p = {k: v for k, v in p.items() if isinstance(v, (int, float)) and v > 0}
        vals = [p[k] for k in CONDS if k in p]
        if "nm" not in p or any(b > a for a, b in zip(vals, vals[1:])):
            continue
        for k in got:
            if k in p:
                got[k].append(p[k] / p["nm"])
    out = dict(DEFAULT_RATIOS)
    for k, v in got.items():
        if len(v) >= 30:
            out[k] = round(statistics.median(v), 3)
    RATIOS.clear()
    RATIOS.update(out)
    return out


def _agree(b, w, pb, pw):
    """b is the better condition, w the worse one."""
    usual = RATIOS[w] / RATIOS[b]
    # (a worse copy selling under $3 says little, so the "too far below" test skips it)
    return pw <= pb * (1 + NOISE_PCT) + NOISE_USD and (pw >= pb * usual * FLOOR or pw < 3)


def clean_prices(prices, lows=None):
    """prices: {'nm': (price, est), ...}  lows: {'nm': cheapest listing, ...} (optional).
    Returns (cleaned prices, [(cond, old, new, why), ...])."""
    import itertools
    lows = lows or {}
    have = [k for k in CONDS if k in prices]
    if len(have) < 2:
        return prices, []
    p = {k: prices[k][0] for k in have}
    cheapest = {}   # cheapest listing of this condition or better
    best = None
    for k in CONDS:
        if isinstance(lows.get(k), (int, float)) and lows[k] > 0:
            best = lows[k] if best is None else min(best, lows[k])
        cheapest[k] = best
    odd = {k for k in have if cheapest[k] and p[k] > LISTING_GAP * cheapest[k]}

    def spacing_miss(group):
        nm_equiv = [math.log(p[k] / RATIOS[k]) for k in group]
        mid = sum(nm_equiv) / len(nm_equiv)
        return sum((x - mid) ** 2 for x in nm_equiv)

    def dropped_high(group):
        # when two groups fit equally well, assume the odd sale is a too-HIGH one
        # (the usual manipulated-sale case) and keep the cheaper reading
        mid = sum(math.log(p[k] / RATIOS[k]) for k in group) / len(group)
        return sum(math.log(p[k] / RATIOS[k]) - mid for k in have if k not in group)

    pick_group, pick_score = None, None
    for size in range(len(have), 1, -1):
        for group in itertools.combinations(have, size):
            if all(_agree(a, b, p[a], p[b]) for a, b in itertools.combinations(group, 2)):
                score = (len(group), -len(odd & set(group)), dropped_high(group), -spacing_miss(group))
                if pick_score is None or score > pick_score:
                    pick_group, pick_score = group, score
        if pick_group:
            break

    out, fixes = dict(prices), []
    if pick_group and len(pick_group) >= max(2, len(have) - 1):
        trusted = set(pick_group)
        for k in have:
            if k in trusted:
                continue
            guess = math.exp(sum(math.log(p[t] * RATIOS[k] / RATIOS[t]) for t in trusted) / len(trusted))
            above = [p[t] for t in trusted if CONDS.index(t) < CONDS.index(k)]
            below = [p[t] for t in trusted if CONDS.index(t) > CONDS.index(k)]
            if below and p[k] < max(below):
                # a too-LOW price (e.g. one cheap NM sale) is only raised as far as it
                # has to go, so it never creates a flip profit that isn't really there
                guess = max(below)
            if above:
                guess = min(guess, min(above))
            if below:
                guess = max(guess, max(below))
            if cheapest[k]:
                guess = min(guess, cheapest[k])
            why = "higher than a better condition" if any(p[k] > v * (1 + NOISE_PCT) + NOISE_USD for v in above) \
                else "out of line with the other conditions"
            out[k] = (round(guess, 2), True)
            fixes.append((k, p[k], out[k][0], why))
    elif len(have) > 2:
        fixes.append(("?", None, None, "conditions disagree too much to tell which sale is off"))
    # NM spike: one odd NM sale far above LP passes the checks above (big NM premiums are real),
    # but it shows a fake LP -> NM flip. Use the usual NM/LP spacing until TCGplayer sales confirm it.
    if "nm" in out and not any(f[0] == "nm" for f in fixes):
        ref = next(((k, out[k][0]) for k in ("lp", "mp") if k in out and out[k][0] >= 3), None)
        if ref and out["nm"][0] > (NM_SPIKE if ref[0] == "lp" else NM_SPIKE_MP) * ref[1]:
            old_nm = out["nm"][0]
            guess = ref[1] / RATIOS[ref[0]]
            if cheapest.get("nm"):
                guess = min(guess, cheapest["nm"])
            guess = max(guess, ref[1])
            out["nm"] = (round(guess, 2), True)
            fixes.append(("nm", old_nm, out["nm"][0], NM_SPIKE_WHY))
    # last pass: no condition may be priced above a better one
    top = None
    for k in CONDS:
        if k not in out:
            continue
        v = out[k][0]
        if top is not None and v > top:
            if not any(f[0] == k for f in fixes):
                fixes.append((k, v, top, "capped at the better condition's price"))
            else:
                fixes = [(f[0], f[1], top, f[3]) if f[0] == k else f for f in fixes]
            out[k] = (top, True)
            v = top
        top = v if top is None else min(top, v)
    return out, fixes


def listing_lows(rec):
    out = {}
    for k, v in (((rec.get("prices") or {}).get("raw")) or {}).items():
        c = COND_KEYS.get(norm(k))
        t = (v or {}).get("tcgplayer") or {}
        if c and isinstance(t.get("low"), (int, float)) and t["low"] > 0:
            out[c] = float(t["low"])
    return out


def checked_prices(rec):
    """cond_prices() with the outlier check applied. Returns (prices, fixes)."""
    return clean_prices(cond_prices(rec), listing_lows(rec))


# ---------------------------------------------------------------- TCGplayer-checked prices
VERIFIED = os.path.join(ROOT, "scripts", "tcgplayer_verified.json")
TP_CONFIG = os.path.join(ROOT, "scripts", "tcgplayer_config.json")


def printing_key(card):
    """'<tcgId>|<printing>' — the same for a site card and a Flip Finder candidate."""
    if not card.get("tcgId"):
        return None
    return f"{card['tcgId']}|{(card.get('tcgVariant') or wanted_printing(card.get('variant'))).lower()}"


def load_verified():
    """{key: {'checked': date, 'prices': {cond: raw entry}}} for checks still fresh enough to trust."""
    try:
        days = int(json.load(open(TP_CONFIG, encoding="utf-8")).get("trust_days", 11))
    except (OSError, ValueError):
        days = 11
    try:
        data = json.load(open(VERIFIED, encoding="utf-8")).get("cards", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in data.items():
        try:
            age = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(v.get("checked", "")[:10])).days
        except ValueError:
            continue
        if age <= days and v.get("prices"):
            out[k] = v
    return out


def overlay_verified(card, raw, verified):
    """Put fresh TCGplayer-checked prices on raw (in place). Returns the conditions that changed."""
    v = verified.get(printing_key(card) or "")
    if not v:
        return []
    changed = []
    for c, e in v["prices"].items():
        if c in CONDS and isinstance(e, dict) and isinstance(e.get("price"), (int, float)) and raw.get(c) != e:
            raw[c] = dict(e)
            changed.append(c)
    # a TCG Price Lookup price may not sit above a better TCGplayer-checked one
    locked = set(v["prices"])
    top = None
    for c in CONDS:
        e = raw.get(c)
        if not isinstance(e, dict) or not isinstance(e.get("price"), (int, float)):
            continue
        if top is not None and e["price"] > top and c not in locked:
            raw[c] = dict(e, price=round(top, 2), est=True,
                          src=f"Estimated: capped at the TCGplayer-checked price of a better condition (was ${e['price']:,.2f})")
            changed.append(c)
        top = raw[c]["price"] if top is None else min(top, raw[c]["price"])
    return changed


def raw_entry(cond, price, when, fixes=()):
    """The {price, date, src, est} saved on a card for one condition."""
    p, est = price
    fix = next((f for f in fixes if f[0] == cond and f[1] is not None), None)
    if fix:
        src = f"Estimated: TCGplayer showed ${fix[1]:,.2f}, which looked like an outlier sale ({fix[3]})"
    else:
        src = SRC_MID if est else SRC
    return {"price": round(p, 2), "date": when, "src": src, "est": est}


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

    budget = load_budget()
    show_day = is_show_day(budget)
    reserve = keep_free(budget)
    learn_ratios(cards)
    verified = load_verified()

    def recently_failed(c):
        s = st.get(c["id"]) or {}
        if s.get("id") or not s.get("failed"):
            return False
        return (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(s["failed"])).days < RETRY_FAILED_DAYS

    api = Api(DAILY_REQUESTS, reserve)
    done, problems, moves, outliers = set(), [], [], []
    stats = {"updated": 0, "same": 0, "noprice": 0, "nomatch": 0, "error": 0, "matched": 0}
    used = {"prices": 0, "match": 0, "setlist": 0}
    dirty = set()
    set_cache = {}
    stop_reason = None
    limit = DAILY_SHARE or 10 ** 9   # max cards this run

    def apply(card, rec):
        s = st.setdefault(card["id"], {})
        if not s.get("id"):
            stats["matched"] += 1
        s["id"] = rec.get("id")
        s.pop("failed", None)
        s["synced"] = TODAY
        if rec.get("variant"):
            s["variant"] = rec["variant"]
        done.add(card["id"])
        prices, fixes = checked_prices(rec)
        if fixes:
            outliers.append((label(card), fixes))
        if not prices:
            stats["noprice"] += 1
            return
        when = (rec.get("last_price_update") or rec.get("updated_at") or TODAY)[:10]
        raw = card.setdefault("raw", {})
        new_raw = dict(raw)
        for c in ("nm", "lp", "mp", "dmg"):
            if c in prices:   # no price for a condition: keep the old one
                new_raw[c] = raw_entry(c, prices[c], when, fixes)
        overlay_verified(card, new_raw, verified)   # fresh TCGplayer-checked prices win
        changed = False
        for c in ("nm", "lp", "mp", "dmg"):
            old, new = raw.get(c) or {}, new_raw.get(c)
            if new is None or old == new:
                continue
            if c == "nm" and isinstance(old.get("price"), (int, float)) and old["price"] > 0 \
                    and abs(new["price"] - old["price"]) / old["price"] >= 0.15:
                moves.append((label(card), old["price"], new["price"]))
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

    def counted(kind, fn, *a):
        before = api.used
        try:
            return fn(*a)
        finally:
            used[kind] += api.used - before

    def list_set(slug):
        if slug in set_cache:
            return set_cache[slug]
        out, offset = [], 0
        while True:
            j = api.get({"game": "pokemon", "set": slug, "limit": PAGE, "offset": offset})
            page = records(j)
            out += page
            total = int((j or {}).get("total") or 0)
            offset += len(page) or PAGE
            if not page or (total and offset >= total) or (not total and len(page) < PAGE):
                break
        set_cache[slug] = out
        return out

    def set_list():
        """The API's full set list (paged), remembered for SETLIST_DAYS."""
        cache = state.get("setlist") or {}
        try:
            age = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(cache.get("fetched", "")[:10])).days
        except ValueError:
            age = 9999
        if cache.get("sets") and (age < SETLIST_DAYS or show_day):
            return cache["sets"]
        out, offset = [], 0
        for _ in range(10):
            j = api.get_path("/sets", {"game": "pokemon", "limit": 200, "offset": offset})
            page = records(j)
            out += [{"name": x.get("name"), "slug": x.get("slug")} for x in page if x.get("slug")]
            total = int((j or {}).get("total") or 0)
            offset += len(page)
            if not page or (total and offset >= total) or (not total and len(page) < 200):
                break
        if out:
            state["setlist"] = {"fetched": TODAY, "sets": out}
        return out or cache.get("sets") or []

    t0 = time.time()
    try:
        if not API_KEY:
            raise Stop("no API key yet. Add the TCGPL_API_KEY secret in the repo settings")

        # 1) prices for every matched card, 100 per request, oldest first
        due = [c for c in cards if (st.get(c["id"]) or {}).get("id") and (st[c["id"]].get("synced") or "") < TODAY]
        due.sort(key=lambda c: st[c["id"]].get("synced") or "")
        due = due[:limit]
        for i in range(0, len(due), BATCH):
            batch = due[i:i + BATCH]
            try:
                j = counted("prices", api.get, {"ids": ",".join(st[c["id"]]["id"] for c in batch), "limit": len(batch)})
            except Stop:
                raise
            except Exception as e:
                stats["error"] += 1
                problems.append(("error", f"batch of {len(batch)} cards", str(e)))
                if stats["error"] >= 4:
                    raise Stop("too many errors in a row; stopped to be safe")
                continue
            got = {r.get("id"): r for r in records(j)}
            for c in batch:
                r = got.get(st[c["id"]]["id"])
                if r:
                    apply(c, r)
                else:
                    st[c["id"]].pop("id", None)   # gets re-matched in step 2
                    problems.append(("rematch", label(c), "TCG Price Lookup didn't return this card by its id; re-matching it"))

        # 2) match new cards, one set listing at a time (not on show days)
        todo = [c for c in cards if not (st.get(c["id"]) or {}).get("id") and c["id"] not in done
                and not recently_failed(c)]
        if todo and len(done) < limit and not show_day:
            slug_of = {}
            for x in counted("setlist", set_list):
                for k in set_keys(x["name"]):
                    slug_of.setdefault(k, x["slug"])

            def find_slug(set_name):
                if state["sets"].get(set_name):
                    return state["sets"][set_name]
                for k in sorted(set_keys(set_name), key=len, reverse=True):
                    if k in slug_of:
                        return slug_of[k]
                return None

            groups = {}
            for c in todo:
                groups.setdefault(find_slug(c.get("set")), []).append(c)
            unknown = groups.pop(None, [])
            # biggest groups first: the most cards matched per request
            for slug, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                if len(done) >= limit or not api.can():
                    break
                try:
                    recs = counted("match", list_set, slug)
                except Stop:
                    raise
                except Exception as e:
                    stats["error"] += 1
                    problems.append(("error", f"set {slug}", str(e)))
                    continue
                for c in group:
                    r = match(c, recs)
                    if r:
                        apply(c, r)
                        state["sets"].setdefault(c["set"], slug)
                    else:
                        unknown.append(c)   # maybe it's listed under another set name: search for it
            # cards whose set isn't known: one search each, then reuse that set's listing
            for c in unknown:
                if c["id"] in done:
                    continue
                if len(done) >= limit or not api.can():
                    break
                try:
                    r = match(c, records(counted("match", api.get, {"game": "pokemon", "q": f"{c['name']} {num_key(c.get('number'))}", "limit": 50})))
                except Stop:
                    raise
                except Exception as e:
                    stats["error"] += 1
                    problems.append(("error", label(c), str(e)))
                    continue
                if r:
                    apply(c, r)
                    slug = (r.get("set") or {}).get("slug")
                    if slug:
                        state["sets"].setdefault(c["set"], slug)
                else:
                    fail(c, "nomatch", "couldn't find this card on TCG Price Lookup")
    except Stop as e:
        stop_reason = str(e)
    secs = int(time.time() - t0)

    unmatched = sum(1 for c in cards if not (st.get(c["id"]) or {}).get("id"))
    stale = sum(1 for c in cards if (st.get(c["id"]) or {}).get("id") and (st[c["id"]].get("synced") or "") < TODAY)
    if not API_KEY or (stop_reason and "rejected" in stop_reason):
        status = "FAILED"
    elif stats["error"] or (stop_reason and not done and (stale or unmatched)):
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
        f"- Cards refreshed: **{len(done)}** of {len(cards)} in {secs // 60}m {secs % 60}s"
        + (" · all matched cards are up to date" if not stale else f" · {stale} matched cards still waiting"),
        f"- API requests used: **{api.used}** (prices {used['prices']}, matching new cards {used['match']}, set list {used['setlist']}; "
        f"max {DAILY_REQUESTS})" + (f" · API says **{api.remaining}** left today" if api.remaining is not None else ""),
        f"- Kept free for adding cards: {reserve} requests" + (" (show day: matching new cards waits until Monday)" if show_day else ""),
        f"- 🔗 Newly matched cards: **{stats['matched']}** · still unmatched: {unmatched}",
        f"- ✅ Prices changed: **{stats['updated']}**",
        f"- ➖ Checked, prices unchanged: {stats['same']}",
        f"- ❔ No TCGplayer price on TCG Price Lookup (kept old prices): {stats['noprice']}",
        f"- ❔ Couldn't match (tried again in {RETRY_FAILED_DAYS} days): {stats['nomatch']}",
        f"- ⚠️ Errors: {stats['error']}",
        f"- Era files saved: **{len(dirty) if saved else 'none'}**",
    ]
    if moves:
        moves.sort(key=lambda m: -abs(m[2] - m[1]) / m[1])
        lines += ["", "**Biggest NM moves (15%+):**"] + [f"- {n}: ${o:,.2f} → ${nw:,.2f}" for n, o, nw in moves[:10]]
    if outliers:
        lines += ["", f"**Outlier prices fixed ({len(outliers)}):** a lower condition was priced above a higher one, or NM sat far "
                      "above LP (usually one odd sale), so that price was replaced with an estimate marked \"est.\" on the site. "
                      "The weekly TCGplayer sales check replaces these estimates with real sales."]
        for n, fx in outliers[:15]:
            bits = [f"{k.upper()} ${a:,.2f} → ${b:,.2f}" for k, a, b, _ in fx if a is not None]
            lines.append(f"- {n}: " + (", ".join(bits) or "conditions disagree; check by hand"))
        if len(outliers) > 15:
            lines.append(f"- …and {len(outliers) - 15} more")
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
