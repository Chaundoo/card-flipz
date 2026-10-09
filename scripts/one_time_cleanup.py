#!/usr/bin/env python3
"""One-time cleanup (run once from the Actions tab, then it can be deleted).

* Removes sale counts / sale dates from raw-price source labels in every era file
* Removes the "TCGplayer sales (90 days)" sales-volume lines
* Removes the Sales volume box and the Sale date column from index.html
Safe to run again: it only changes things that still need changing.
"""
import json, os, re, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = {'TCGplayer: no NM sales/qtr': 'TCGplayer (thin sales)', 'TCGplayer: 1-2 sales/qtr': 'TCGplayer (thin sales)',
       'TCGplayer: no LP sales/qtr': 'TCGplayer (thin sales)', 'Only 1 TCGplayer sale in qtr': 'TCGplayer last sale',
       'Only 1 TCGplayer DMG sale in qtr': 'TCGplayer last sale'}
KEEP = {"TCGplayer market (via TCG Price Lookup)", "TCGplayer mid price (via TCG Price Lookup; no market price)"}


def clean(s):
    if not s or s in KEEP:
        return s
    t = s.strip()
    if re.match(r'(?i)^(TCGplayer( product| LP| NM| MP| DMG)? market)', t):
        return "TCGplayer market"
    if re.search(r'(?i)\bavg\b', t):
        return "TCGplayer avg of recent sales"
    if re.search(r'(?i)(only|last) TCGplayer sale|only 1 sale', t):
        return "TCGplayer last sale"
    if re.search(r'(?i)(~|est\.? ?)\d+% of NM', t):
        pct = re.search(r'(\d+)% of NM', t).group(1)
        return "Estimate (~" + pct + "% of NM)"
    t = re.sub(r'\s*\([^()]*\)', '', t)
    t = re.sub(r'\s*\([^()]*\)', '', t)
    t = t.split(';')[0].strip()
    return FIX.get(t, t)


m = json.load(open(os.path.join(ROOT, "eras", "eras.json"), encoding="utf-8"))
for e in m["eras"]:
    p = os.path.join(ROOT, "eras", e["id"], e["id"] + ".json")
    if not os.path.exists(p):
        continue
    d = json.load(open(p, encoding="utf-8"))
    n = 0
    for c in d["cards"]:
        for v in (c.get("raw") or {}).values():
            if v and v.get("src") and clean(v["src"]) != v["src"]:
                v["src"] = clean(v["src"])
                n += 1
        if (c.get("liquidity") or "").startswith("TCGplayer sales (90 days)"):
            c.pop("liquidity")
            n += 1
    if n:
        d["version"] = str(int(time.time()))
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"{e['id']}: {n} changes")

p = os.path.join(ROOT, "index.html")
s = open(p, encoding="utf-8").read()
R = [
    ("""'<div class="facts">'+(ADV?'<div class="fact"><span>Gem rate / population</span>'+esc(c.gem||"—")+'</div>':'')+'<div class="fact"><span>Sales volume</span>'+esc(c.liquidity||"—")+'</div></div>'+""",
     """(ADV?'<div class="facts"><div class="fact"><span>Gem rate / population</span>'+esc(c.gem||"—")+'</div></div>':'')+"""),
    ("""(x.est?'<span class="pill est">est.</span> ':'')+fmtDate(x.date)+(x.src?' · '+esc(x.src):'')""",
     """(x.est?'<span class="pill est">est.</span> ':'')+esc(x.src||"")"""),
    ("""<th>Sale date</th><th>Source</th><th class="r">Jump to NM</th>""", """<th>Source</th><th class="r">Jump to NM</th>"""),
    ("""'</td><td class="num">'+fmtDate(x.date)+'</td><td>'+esc(x.src||"")+'</td><td class="r num">'+(i===0""",
     """'</td><td>'+esc(x.src||"")+'</td><td class="r num">'+(i===0"""),
    ("""<span>Source</span><span>Sale date</span><span>Est.</span>""", """<span>Source</span><span>Price date</span><span>Est.</span>"""),
]
done = 0
for a, b in R:
    if a in s:
        s = s.replace(a, b)
        done += 1
open(p, "w", encoding="utf-8").write(s)
print(f"index.html: {done} edits")
