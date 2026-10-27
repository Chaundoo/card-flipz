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

## Graded Slabs (passcode)
The **Graded Slabs** tab (same passcode as the Flip Finder) lists PSA 10 / PSA 9 slabs of popular Pokémon that look
ready to move, filtered to the most you'd spend on one slab (the budget box starts at the bots' price cap; "PSA 10 or 9"
shows a PSA 9 when the 10 is over budget).

**Price cap (`max_slab_price` in `scripts/graded_config.json`, now $1,500):** the bots only spend requests on, and the
tab only lists, slabs with a PSA 10 or PSA 9 at or under this. Change the number and commit to raise or lower it; the
next morning's runs and the tab's default budget follow it. When only the PSA 10 price is known, the PSA 9 is guessed at
about a third of it (`sources.psa9_share_of_psa10_guess`). CardPrice still re-checks over-cap slabs every 30 days
(`cp_over_cap_recheck_days`) so slabs that drop back under the cap come back.

**Four slab bots, one after another every morning** (they share the concurrency group `graded-bots`):
1. **CardPrice lookups** (`cardprice.yml`, ~8:47 am Pacific in summer): the broad sweep of graded price estimates, pop
   counts and price history (free, ~400 slabs a day).
2. **tcgapi.dev raw radar** (`tcgapi.yml`, starts when CardPrice finishes): tcgapi.dev's **free** plan has no eBay or
   graded data, so this bot doesn't price slabs. It watches the raw (ungraded) TCGplayer market price of slab-pool cards
   and its 7 / 30-day change. Cards whose raw price is rising (`ta_riser_7d` / `ta_riser_30d`) jump to the front of the
   Graded Slabs bot's sales checks and get a small heat bonus (shared with CardPrice's movers, never both). Slabs heating
   up go first, then mid-era cards. **Never more than 90 of the free 100 requests per UTC day**: its own count saved in
   `graded/tcgapi.json` (test runs count too), tcgapi.dev's own counter (stops at 10 left), and a stop on any
   rate-limit answer. `ta_max_per_day` can't be set above 90.
3. **pokemon-api.com lookups** (`pokemon-api.yml`, starts when tcgapi.dev finishes): eBay PSA 10 / 9 median and how many
   sold, spent on slabs heating up, your priority Pokémon (mid-era first), and slabs CardPrice couldn't price.
4. **Graded Slabs** (`graded-heat.yml`, starts when pokemon-api.com finishes): PokemonPriceTracker checks (~30 slabs a
   day), then the price check, then scores everything and writes the tab's list.

**Mid-era first:** `focus_eras` in `scripts/graded_config.json` (DP/HGSS, Black & White, XY, Sun & Moon) are checked
first by every slab bot and score a little higher (`era_points`, `focus_sort_bonus`). Change the list to change the focus.

**The price check (which PSA 10 / 9 price to believe)** — `scripts/slab_sources.py`. Every source gets things wrong in
its own way (pokemon-api.com matching the wrong card, one odd PokemonPriceTracker sale, a CardPrice estimate far off), so
the tab no longer shows whichever price came in last. For each slab the bot collects every source's price plus the PSA 10
/ Grade 9 saved on your site's card, then:
- throws out prices that can't be right for that card's raw NM price (a PSA 10 under ~1× raw or over ~250× raw, a PSA 9
  under ~0.6× or over ~40×; `sources.raw_bounds`),
- combines the rest as a weighted median (sales count, recency, and each source's track record — `trust` in
  `graded/sources.json`, learned every morning), throwing out any more than 60% from the others,
- makes sure PSA 10 is above PSA 9 (re-doing the less certain grade, or flagging both),
- gives each slab a confidence: **confirmed** (2+ sources agree within 25%), one sales source, **CardPrice estimate
  only**, or **unconfirmed**. Unconfirmed slabs show **Verify price** instead of Breaking out / Warming up, and their
  trend and heat are held back. CardPrice-only trends need a 25%+ rise to count as Breaking out, and a 100%+ jump with no
  sales behind it is treated as a data glitch.
The "Why it's on the list" notes say which prices were ignored and why. `graded/sources.json` also records which source
last checked each slab; PokemonPriceTracker and pokemon-api.com skip slabs the other checked in the last 2 days
(`sources.skip_if_other_checked_days`), except slabs heating up, which get a second source on purpose. Running a slab bot
by hand does not start the next one (so a day's requests aren't spent twice).

**tcgapi.dev setup (once):** make a free account at [tcgapi.dev](https://tcgapi.dev/signup/), create an API key (it
starts with `tcg_live_`), and save it as the repository secret `TCGAPI_KEY` (Settings → Secrets and variables →
Actions), replacing the old tcgapi.net key. Stay on the free plan: the bot only uses free endpoints. The free plan is
for non-commercial use; check tcgapi.dev's terms if the site is used for a business.

**pokemon-api.com setup (once):** make a free RapidAPI account, open pokemon-api.com's API on RapidAPI (link on
pokemon-api.com), subscribe to the free **Basic** plan, copy your `X-RapidAPI-Key`, and save it as the repository secret
`POKEMON_API_KEY` (Settings → Secrets and variables → Actions). The free Basic plan is 100 requests a day (30 a minute),
and requests over 100 are billed, so the bot never passes `pa_max_per_day` (90) and also stops when RapidAPI says only
`pa_keep_free` (5) are left. Mostly 1999–2020 cards, with modern cards capped at about a quarter of the list.
It only downloads `graded/heat.json` when you open the tab, so Quick check stays light on show-day data.

**Setup (once):** make a free account at pokemonpricetracker.com, copy your API key, then in GitHub go to
**Settings → Secrets and variables → Actions → New repository secret**, name it `PPT_API_KEY` and paste the key.
Then Actions → *Graded Slabs* → **Run workflow** to start it (or wait for the next morning).

`.github/workflows/graded-heat.yml` runs `scripts/graded_heat.py` every morning, right after the pokemon-api.com bot:
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

Settings: `scripts/graded_config.json` (your **priority** Pokémon list, most important first: their slabs on the verge show first and get checked first, then other popular Pokémon; era points, how often to re-check, and the **catalysts** list:
add a line when a new set or product spotlights a Pokémon; `kind: "reprint"` lowers the score instead). PSA
population isn't on PokemonPriceTracker's free plan, so pop/gem rate only shows for cards with pop counts on the site.
**Charts:** each slab has a *Price & sales chart* (money over time): the line is the bot's price checks (median sale over the last 7 days), dots are days with sales (bigger = more sold), and the dashed line is the median over ~4 months. PokemonPriceTracker's free plan only shares the last few days of sales, so the chart grows each time the bot checks the slab. Chart points live in `graded/charts.json`, downloaded only when you open a chart.
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
