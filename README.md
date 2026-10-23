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

## Quick check (card shows)
* **Quick check** tab: type a card number (like `4/102`) or a name. Each card shows a small table per condition:
  **Market** price, what a **Vendor** pays at your vendor %, and **Flip up** = your profit if you buy at that
  condition's market price and a vendor buys it one condition higher (e.g. LP → NM). The best flip is highlighted
  and results are sorted best flip first.
* Cards rank by **vendor profit** by default; switch to eBay in the "Sell to" box (remembered on that device).

## Admin log (passcode)
The **Admin** tab (4-digit passcode) shows what every bot did today and this week in plain English, e.g.
"Successful price update: checked 1,899 cards and 412 prices changed". Problems, and scheduled bots that didn't
run, are listed at the top, and a red notice shows on the other tabs (on devices where Admin is unlocked).
**Copy for Claude** copies a problem with its full report so it can be pasted into a chat.
Every workflow ends with `scripts/log_run.sh`, which adds the run to `botlog.json` (`scripts/botlog.py`, kept 35
days). The tab also reads GitHub's public list of Actions runs to catch crashes and missed runs.

## Graded Heat (passcode)
The **Graded Heat** tab (same passcode as the Flip Finder) lists PSA 10 / PSA 9 slabs of popular Pokémon that look
ready to move, filtered to the most you'd spend on one slab (type a budget, default $1,000; "PSA 10 or 9" shows a
PSA 9 when the 10 is over budget). Mostly 1999–2020 cards, with modern cards capped at about a quarter of the list.
It only downloads `graded/heat.json` when you open the tab, so Quick check stays light on show-day data.

**Setup (once):** make a free account at pokemonpricetracker.com, copy your API key, then in GitHub go to
**Settings → Secrets and variables → Actions → New repository secret**, name it `PPT_API_KEY` and paste the key.
Then Actions → *Graded Heat* → **Run workflow** to start it (or wait for the next morning).

`.github/workflows/graded-heat.yml` runs `scripts/graded_heat.py` every morning (~8:13 am Pacific):
* **Cards it watches:** popular Pokémon (the `popular` list in `discovery_config.json`) already on the site or in
  the Flip Finder list, plus Mon–Thu a few spare TCG Price Lookup requests (`graded_pool_max_requests` in
  `api_budget.json`, default 8) to find chase cards from eras the site barely has (Sword & Shield → Mega Evolution, more WOTC/EX).
* **Prices:** PSA 10 / 9 eBay sold prices, sales counts and history from PokemonPriceTracker (free plan ≈ 30 slabs
  a day). Every check is saved in `graded/state.json`, so it builds its own history over time. Hot cards are
  re-checked every 3 days, others weekly, cold ones every 3 weeks.
* **Heat score (0–100):** Pokémon popularity (tier 1 like Charizard/Gengar/Umbreon counts most), era (vintage and
  mid-era above modern), PSA 10 change over 4 and 12 weeks (an early rise scores best; a card that already doubled
  is marked "Already spiked" and ranks lower), sales picking up, the same Pokémon rising on its other slabs, upcoming
  sets/products featuring it, thin sales (counts against) and low PSA 10 pop / gem rate (counts for).
* **Stages:** Breaking out (PSA 10 up 12%+ in 4 weeks), Warming up (sales or sibling slabs rising while the price is
  still flat — often the earliest sign), Steady, Already spiked, Cooling, First check.

Settings: `scripts/graded_config.json` (tier-1 list, era points, how often to re-check, and the **catalysts** list:
add a line when a new set or product spotlights a Pokémon; `kind: "reprint"` lowers the score instead). PSA
population isn't on PokemonPriceTracker's free plan, so pop/gem rate only shows for cards with pop counts on the site.
It's a watchlist, not a prediction: hype can be manufactured and reverse fast.

## Card file size
Empty fields (graded prices and pop counts that were never filled in, blank gem/verdict/notes) are left out of
the era files, so the phone downloads less. The site treats a missing field as empty. The bots
(`refresh_prices.py`, `approve_candidates.py`) strip empty fields whenever they save.

## Saving edits from the site
When you save a card, the site downloads only the era file(s) that changed (e.g. `sv.json`).
Upload each one into its own folder: **eras → sv → Add file → Upload files**.

## Saving pairings and the profit log
Pairings and the profit log save from the site, no downloading needed. Every change is kept on the
phone right away; tap **Save to GitHub** in the bar at the bottom and press **Create** on the GitHub
page that opens. `.github/workflows/save-data.yml` (`scripts/save_data.py`) writes the changes into
`pairings.json` / `profitlog.json` and closes the issue (about a minute). Only you and collaborators
can save this way. The repo is public, so saved pairings are public too.

## Profit Log lock (passcode)
The **Profit Log** tab asks for a 4-digit passcode. Each card show (cards, prices, hours and notes) is
encrypted on the phone before it's saved (AES-GCM, key made from the passcode with PBKDF2), so
`profitlog.json` and the "Save data" issues only hold scrambled text. The passcode is never stored in the
repo. Once it's entered on a device, that device stays unlocked until you tap **Lock on this device**.
Each card show also has a **Notes** box for your thoughts on the show; notes save with the rest of the log.
A 4-digit code keeps casual visitors out but could be guessed by someone determined, so don't put
anything truly sensitive in the notes.

## Adding a new era later
Add an entry to `eras/eras.json` above `"other"` (id, name, years, set-name patterns), then create
`eras/<id>/<id>.json` containing `{"era":"<id>","version":"0","cards":[]}`.

## Daily price update
`.github/workflows/price-refresh.yml` runs `scripts/refresh_prices.py` Monday to Friday with TCG Price
Lookup prices. Odd prices (a lower condition above a higher one, or NM far above LP from one strange
sale) are replaced with an estimate marked "est." until the TCGplayer sales check looks at them.

## Weekly TCGplayer sales check
`.github/workflows/tcgplayer-check.yml` runs `scripts/tcgplayer_check.py` once a week at a **random
time Monday to Thursday** (the slot changes every week; it wakes up every two hours on those days and
only goes ahead at that week's slot, after a random wait). It looks at TCGplayer's real sales from the
last 90 days, but only for cards with an estimated price, the top flips, and the top Flip Finder
cards (about 150 cards, one request at a time with random pauses). Odd sales are thrown out and the
median sale is saved in `scripts/tcgplayer_verified.json` and on the cards; the daily update keeps
those prices for 11 days. Settings: `scripts/tcgplayer_config.json`. Run it now: Actions →
*TCGplayer sales check* → Run workflow. Results are posted to the "Price update log" issue.

## Flip Finder (new flip cards, approved by you)

The **Flip Finder** tab lists new cards the bot found with a good condition flip. Nothing is added to the site until you pick it.

* **Finding:** `.github/workflows/find-candidates.yml` runs early **Monday to Thursday** (your no-show days), after the daily price update, and scans 2010-2020 sets with whatever TCG Price Lookup requests are left. Friday to Sunday it does nothing, so show-day card adds keep their requests. Each set is re-checked about once a week. It writes `candidates.json`.
* **What it suggests:** only popular Pokémon (the `popular` list in the settings) and no stamped cards (prerelease, staff, league, championship… see `stamp_patterns`), since vendors rarely buy those.
* **Picking:** on the tab, tap cards or use **Select all**, then **Add to site**, **Not for me** (off the list for 90 days) or **Never show** (gone for good). That opens a GitHub issue; press **Create**.
* **Learning:** every add / decline is remembered in `scripts/discovery_state.json` (the Pokémon, set, card type and price range). Pokémon, sets and card types you keep declining rank lower on the next search; ones you add rank higher. Cards you just leave alone stay on the list until their set is re-checked and they no longer qualify, their prices are 3 weeks old, or better cards push them out.
* **Adding:** `approve-candidates.yml` copies the picked cards into the era files using the prices saved when they were found. It uses **no** API requests.
* **Settings:** `scripts/discovery_config.json` (vendor %, min/max buy price, minimum return, request budget, which years/sets, popular Pokémon, words to skip).
* **Test it without saving anything:** Actions → *Flip Finder* → Run workflow (dry run is on by default). The report lists the fields the API returned, so the trainer and price checks can be tuned.
