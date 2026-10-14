#!/usr/bin/env python3
"""Run the outlier check over every card already on the site (no API requests).

Uses the same check as the daily price update (refresh_prices.clean_prices): when a
lower condition is priced above a higher one because of an odd sale, that price is
replaced with an estimate. New prices get checked automatically by the daily job;
this just cleans up what's already there. Safe to run again.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_prices as rp

manifest = json.load(open(rp.MANIFEST, encoding="utf-8"))
files = []
for era in manifest["eras"]:
    path = os.path.join(rp.ERAS_DIR, era["id"], era["id"] + ".json")
    if os.path.exists(path):
        files.append((path, json.load(open(path, encoding="utf-8"))))
cards = [c for _, d in files for c in d.get("cards", [])]
rp.learn_ratios(cards)

lines, stamp = [], str(int(time.time()))
for path, data in files:
    changed = False
    for c in data.get("cards", []):
        raw = c.get("raw") or {}
        prices = {k: (float(raw[k]["price"]), bool(raw[k].get("est"))) for k in rp.CONDS
                  if isinstance((raw.get(k) or {}).get("price"), (int, float)) and raw[k]["price"] > 0}
        out, fixes = rp.clean_prices(prices)
        real = [f for f in fixes if f[1] is not None]
        if not real:
            continue
        for k, old, new, why in real:
            e = raw[k]
            e["price"], e["est"] = out[k][0], True
            e["src"] = f"Estimated: TCGplayer showed ${old:,.2f}, which looked like an outlier sale ({why})"
        changed = True
        lines.append(f"- {rp.label(c)}: " + ", ".join(f"{k.upper()} ${a:,.2f} → ${out[k][0]:,.2f}" for k, a, _, _ in real))
    if changed:
        data["version"] = stamp
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

report = f"## Outlier price cleanup — {len(lines)} cards fixed\n\n" + "\n".join(lines) + "\n"
print(report)
summ = os.environ.get("GITHUB_STEP_SUMMARY")
if summ:
    open(summ, "a", encoding="utf-8").write(report)
