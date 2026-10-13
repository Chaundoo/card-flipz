# card-flipz

Chaundoo Card Flipz — https://chaundoo.github.io/card-flipz

## Where the cards are saved
Cards are split into one file per era, so no single file gets near GitHub's 25 MB upload limit:

```
eras/eras.json            list of eras (and set-name patterns used to guess a new set's era)
eras/wotc/wotc.json       Wizards of the Coast (1999–2003)
eras/ex/ex.json           EX (2003–2007)
eras/dp-hgss/dp-hgss.json Diamond & Pearl / HeartGold SoulSilver (2007–2011)
eras/bw/bw.json           Black & White (2011–2013)
eras/xy/xy.json           XY (2014–2016)
eras/sm/sm.json           Sun & Moon (2017–2019)
eras/swsh/swsh.json       Sword & Shield (2020–2023)
eras/sv/sv.json           Scarlet & Violet (2023–2025)
eras/me/me.json           Mega Evolution (2025–)
eras/other/other.json     other promos & products
pairings.json             saved pairings
profitlog.json            saved profit log (card shows)
```

## Saving edits from the site
When you save a card, the site downloads only the era file(s) that changed (e.g. `sv.json`).
Upload each one into its own folder: **eras → sv → Add file → Upload files**.

## Saving pairings and the profit log
Pairings and the profit log save from the site, no downloading needed. Every change is kept on the
phone right away; tap **Save to GitHub** in the bar at the bottom and press **Create** on the GitHub
page that opens. `.github/workflows/save-data.yml` (`scripts/save_data.py`) writes the changes into
`pairings.json` / `profitlog.json` and closes the issue (about a minute). Only you and collaborators
can save this way. The repo is public, so the saved pairings and profit log are public too.

## Adding a new era later
Add an entry to `eras/eras.json` above `"other"` (id, name, years, set-name patterns), then create
`eras/<id>/<id>.json` containing `{"era":"<id>","version":"0","cards":[]}`.

## Weekly price refresh
`.github/workflows/price-refresh.yml` runs `scripts/refresh_prices.py` every Friday. It reads every
era file, refreshes raw prices, and only rewrites the era files that changed.

## Flip Finder (new flip cards, approved by you)

The **Flip Finder** tab lists new cards the bot found with a good condition flip. Nothing is added to the site until you pick it.

* **Finding:** `.github/workflows/find-candidates.yml` runs early **Monday to Thursday** (your no-show days), after the daily price update, and scans 2010-2020 sets with whatever TCG Price Lookup requests are left. Friday to Sunday it does nothing, so show-day card adds keep their requests. Each set is re-checked about once a week. It writes `candidates.json`.
* **Picking:** on the tab, tap cards or use **Select all**, then **Add to site** (or **Hide** to never see a card again). That opens a GitHub issue; press **Create**.
* **Adding:** `approve-candidates.yml` copies the picked cards into the era files using the prices saved when they were found. It uses **no** API requests.
* **Settings:** `scripts/discovery_config.json` (vendor %, min/max buy price, minimum return, request budget, which years/sets, popular Pokémon, words to skip).
* **Test it without saving anything:** Actions → *Flip Finder* → Run workflow (dry run is on by default). The report lists the fields the API returned, so the trainer and price checks can be tuned.
