# PoE Craft Planner (Path of Exile 1)

Pick a base item and the mods you want. The planner compares crafting strategies and shows the
cheapest one step by step: which currency to use, what the item should look like after each step,
what to do when a step fails, and the probability and expected cost of each step.

Runs locally. Needs **Python 3.10+** and nothing else (standard library only).

## Install & run

```bash
# Windows
run.bat
# Linux / macOS
./run.sh
```

On first launch this downloads the game data (~30 MB), builds `data/poecraft.db` and opens
http://127.0.0.1:8765/ in your browser.

Manual equivalent:

```bash
python -m poecraft update        # download RePoE data + build the database
python -m poecraft serve         # web UI (add --port 9000 / --no-browser if needed)
python -m poecraft prices update # fetch + cache live poe.ninja prices (also: prices show)
```

### Live prices (poe.ninja)

`serve` refreshes prices in the background at startup, and the UI has an **Update prices** button.
`plan` refreshes before planning; add `--no-live-prices` to skip it.
Prices come from `GET /poe1/api/economy/exchange/current/overview` for Currency, Fragment, Fossil,
Resonator and Essence. By default the current challenge league is used (the first entry of
`/poe1/api/economy/leagues`). Pick another with `--league Standard` or `POECRAFT_LEAGUE=Standard`.

- Snapshot: `data/prices_live.json`. Raw responses and ETags: `data/ninja_cache/`.
  `config/prices.json` is never overwritten and stays the fallback for anything poe.ninja lacks
  (e.g. the Harvest reforge cost).
- Polite use: requests are sequential and send a descriptive User-Agent. Responses are cached
  for at least 5 minutes, or for the server's `max-age` if longer (capped at 30 minutes), then
  revalidated with `If-None-Match`. Clicking Update repeatedly does not trigger new requests.
  Set `POECRAFT_CONTACT=you@example.com` to add a contact to the User-Agent, as poe.ninja asks.
- Failures never crash: if poe.ninja is unreachable or the league is unknown, the last snapshot
  is used if it is under 24 h old (labelled "stale"). Otherwise `config/prices.json` estimates
  are used. The CLI and UI both show a warning.

### Languages

The web UI has a language selector in the top-right corner (English, Русский, Deutsch); the choice is kept in
the browser's localStorage. The CLI takes `--lang en|ru|de`. Only UI text is localized: labels, buttons,
headings, table headers, status words. UI strings live in `poecraft/locales/<code>.json`; adding a language
means adding a file (no code changes). A string missing from a language is shown in English with an **EN** mark.

**Game data** (base item, mod, essence, fossil and currency names) is shown in the chosen language from the
game's own localized files - RePoE's per-language exports - matched by internal id, never machine-translated.
`python -m poecraft update` downloads `<Language>/base_items` and `<Language>/mods` for every language in
`config.LOCALE_LANGUAGES`, checks that they are the same build as the English export (identical ids and
non-localized fields; otherwise the language is refused and the old file kept) and writes
`data/locale_game_<code>.json`. Re-run it each league. A name with no localized string is shown in English with
the EN mark; item classes and influence names have no locale data yet.

**Plan explanations** (step details, notes, infeasibility reasons, warnings, strategy names, comparison rows,
recovery options) are message keys (`m.*`) in the same locale files. The planner builds language-free message
objects (`poecraft/messages.py`); the CLI and the server render one computed plan into any language, so switching
language never re-plans and ids and numbers are identical in every language. Game-data names inside a sentence are
parameters filled by `game_text()`. A key missing in a language falls back to English with the EN mark, and the
plan lists the missing keys. Short quotes of game texts in single quotes (currency descriptions) stay English:
the locale data has names, not descriptions. Item classes, influence names and bench master names have no locale
data yet and are shown in English with the EN mark.

### Importing an item (paste / screenshot)

**Paste (reliable):** hover the item in game, press Ctrl+C (or Ctrl+Alt+C for the advanced text with tiers), paste
into "Paste an item" in the web UI or pass it to the CLI:

    python -m poecraft plan --paste-file item.txt                 # base, item level, influences, mods as targets
    python -m poecraft plan --paste "Item Class: Boots
Rarity: Rare
..."   # a literal 
 is a line break
    python -m poecraft plan --paste-file item.txt --paste-as have --target "Cold Resistance>=36"   # finish my item

English, Russian and German item text is read. Base names and mod lines are matched against the game's own strings
by internal id (`poecraft/engine/itemtext.py`); hybrid mods are recognised over two lines (and by the advanced
copy's headers); fractured / crafted / implicit markers are read; a line that matches nothing on this base (a unique
or new-patch mod) is reported and skipped, never fatal. "Import as have" sends the item's mods to the recovery
planner (`/api/recover`, `--have`).

**Screenshot (beta):** the standard library cannot read text from an image, so screenshots go to the OCR.space
web API - only if you set `POECRAFT_OCR_SPACE_KEY` (your own key from https://ocr.space/ocrapi). Without it nothing
is sent and the UI asks for a paste. The recognised text lands in the paste box and every OCR import carries
"OCR may misread numbers. Please verify the stats before planning." Free tier: images up to 1 MB.

### Templates and "Find on Trade"

**Templates:** save the current base, item level, influences, targets (with thresholds) and objective under a name.
Web UI: "Templates" in the targets card (stored in this browser). CLI: `--save-template "Endgame Boots"`,
`--load-template "Endgame Boots"` (extra `--target` are added), `python -m poecraft templates [--delete NAME]`
(stored in `data/templates.json`, or the file named by `POECRAFT_TEMPLATES`).

**Find on Trade:** turns the targets into a search on the official trade site and opens it. Mods are mapped to trade
stat ids through the trade site's own stat list (`/api/trade/data/stats`, cached for a week): the trade texts are the
planner's mod labels, local stats carry "(Local)"; a line without a trade stat is left out and named. Thresholds
become minimums (first number of the mod); hybrid lines become one filter per stat. The league is the current one:
the league of the live prices if the trade site lists it, otherwise the first league the trade site lists, otherwise
Standard with a warning; the UI has a league selector. The link is a short search link from
`POST /api/trade/search/{league}`; if that fails, the link carries the whole query (`?q=`). CLI: `--trade`
(`--trade-league` to override). The trade site is only contacted when you ask for a link.

## TensChep Workshop (pages)

The web server is the **TensChep Workshop** hub - the entry point for the project's tools - with the crafting
planner as its first tool:

| Path | Page |
|---|---|
| `/` (also `/hub`) | the hub: one card per tool (`poecraft/web/hub.html`, `hub.js`, `hub.css`) |
| `/craft` | the crafting planner (`poecraft/web/index.html`, `app.js`); its header links back to the hub |

| `/evaluator` | the Item Evaluator: price-check a pasted item on the trade site (`evaluator.html`, `evaluator.js`) |

**Item Evaluator** (`poecraft/engine/evaluator.py`, `POST /api/evaluate`): paste any item (English, Russian, German).
It searches the official trade site in the current league, online sellers first, and lists up to 15 listings,
online first and cheapest first (prices converted to chaos with the live prices) - never one "average" price.
- Rares: the first search uses every modifier, Eldritch implicits, influences and item level. With no result,
  properties are dropped **cheapest to recreate first**, measured for this item by the planner: the Chaos Orb spend
  to roll a modifier at its tier (how rare the roll is), the bench cost of a crafted modifier, the Ember / Ichor
  cost of an Eldritch implicit, the influence Exalt's price for an influence (free to drop when the influenced
  modifiers imply it), item level first. The core - the most expensive half of the modifiers (at most 3) - is never
  dropped. The page shows the ranking, every search with what it dropped, and a link to the search.
- Uniques by name (English names; a Russian unique is searched by its base type, with a note), currency, cards,
  fragments, gems and bases by name; currency also shows poe.ninja's price.
- **Timeless Jewels** (Lethal Pride, Brutal Restraint, Glorious Vanity, Militant Faith, Elegant Hubris, Heroic
  Tragedy): the seed and conqueror are read from the pasted text (the game's own wording in English, Russian or
  German) and the inline calculator opens: pick the socket (list, or click it on the passive tree), "Calculate
  effects" lists every keystone and notable in the radius as "old -> new" (small passives grouped), and below it the
  trade listings of exactly this jewel + conqueror + seed. Effects come from Path of Building's precomputed Timeless
  Jewel tables (MIT license, credited in `data/timeless/NOTICE.txt`) and GGG's passive tree export - read, never
  generated. `python -m poecraft timeless update` (also part of `update`; Docker fetches it on first start) downloads
  about 35 MB into `data/timeless/` (about 70 MB on disk).
- **Equivalent seeds**: after "Calculate effects", tick the must-have notables and press "Find Equivalent Seeds".
  Every other seed of the jewel is compared on those notables straight from the tables (one read per notable for the
  whole seed range, well under a second): the same change, or for Glorious Vanity the same passive with rolls at least
  as high. Keystones and the other jewels' small passives depend only on the conqueror, so they are the same for every
  seed. Pricing takes two trade searches: the cheapest 100 listings of this jewel + conqueror (any seed), checked
  locally against the equivalent seeds, then one "at least one of these seeds" search (at most 30 seeds - the trade
  site refuses ~35+) for the ones the first did not reach. With no exact match, the closest seeds are shown with
  "m of n".
- The price comes first: right under the item line, "Cheapest: X (N listings from online sellers)", then the
  listings, then the details. **Compact mode** (Режим оверлея; remembered, or `/evaluator?compact=1`) shows only the
  input and that price - for a small browser window in a corner; there a paste evaluates at once, and "Paste &
  evaluate" reads the clipboard in one click. In Chrome / Edge, "Pop out" opens it in a small always-on-top window
  (Document Picture-in-Picture, no extra software).
- Corrupted / mirrored items are compared with corrupted / mirrored items only (the status line is read in English,
  and in Russian / German with GGG's own words from the localized trade sites); the Planner's import says such an item
  cannot be changed. Rare / magic items of classes the crafting data does not hold (jewels, maps, tinctures) are
  searched by base type, with a note that their modifiers are not filtered.
- At most 5 trade searches per evaluation (`POECRAFT_EVAL_MAX_SEARCHES`), all requests through one limiter that
  stays under GGG's per-IP limits (30 searches / 5 min) and reads their rate-limit headers; results cached 5 min.

Adding a tool to the hub: one entry in `TOOLS` in `poecraft/web/hub.js` (`{id, href, icon}`) plus its
`hub.tool.<id>.title` / `.desc` texts in `poecraft/locales/*.json`. The brand name "TensChep Workshop" is fixed
English text in the pages, never a translation key. The language chosen on either page is shared (localStorage).

## Hosting it publicly (Docker on a VPS)

The image is standard library only (no `pip install`). `docker compose` runs the planner behind
[Caddy](https://caddyserver.com/), which gets and renews the HTTPS certificate.

1. A VPS with Docker and the compose plugin (any 1 vCPU / 1 GB machine works; plans are CPU-bound and run one at a
   time). Point a DNS A record (e.g. `craft.example.org`) at it and open ports 80 and 443.
2. Copy the project to the server, then:

       cp .env.example .env        # set POECRAFT_DOMAIN and POECRAFT_CONTACT (required), the rest is optional
       docker compose up -d --build
       docker compose logs -f app  # first start downloads the game data and builds the database (a few minutes)

3. Open `https://craft.example.org`. Health check: `https://craft.example.org/api/health`.

What production mode (`POECRAFT_ENV=production` / `serve --production`) changes compared with the local tool:

| | |
|---|---|
| Per-IP rate limits | 10 req/s (burst 20) for ordinary API calls, 1 plan per 5 s (burst 3), 1 trade search per s (burst 5), 1 OCR per 50 s (burst 3); over it: HTTP 429 with `Retry-After`. Client IPs come from Caddy's `X-Forwarded-For` (`POECRAFT_TRUST_PROXY=1`, set in the compose file). |
| Trade site protection | Identical searches reuse their link for 5 minutes; at most 3 searches per 10 s go to pathofexile.com for all users together; after a 429 from GGG nothing is sent until its `Retry-After` passed. A search that cannot be sent still gets a working link (the full query in the URL, no request to GGG). |
| Load | at most 64 connections at once (more get 503), 30 s socket timeout, 2 MB request bodies, simulations capped at 20 000 runs, plans queue for at most 60 s (then 503 "busy"). |
| Security | `nosniff`, `X-Frame-Options: DENY`, a Content-Security-Policy (`self` only), no version in `Server`, access log on stdout; the static file handler only serves `poecraft/web`. |
| Prices | refreshed in the background every 30 min; the "Update prices" button does not trigger a fetch. |
| Outgoing requests | every request (poe.ninja, pathofexile.com, RePoE, OCR.space) sends `User-Agent: poecraft-planner/<version> (...; contact: $POECRAFT_CONTACT)` - GGG asks API users to identify themselves. The server prints a warning at start if no contact is set. |
| CORS | off by default; list a separate front end's origin in `POECRAFT_CORS_ORIGINS`. |

All settings are environment variables (see `.env.example`): `POECRAFT_DATA_DIR`, `POECRAFT_CONFIG_DIR`,
`POECRAFT_LEAGUE`, `POECRAFT_CONTACT` / `POECRAFT_USER_AGENT`, `POECRAFT_OCR_SPACE_KEY`, `POECRAFT_CORS_ORIGINS`, the
`POECRAFT_RATE_*` limits and the rest. Limits are per server process (one container = one process).

### Updating for a new league or patch

Game data comes from the RePoE export of the game files, rebuilt after every patch. After a new league launches
(usually a day or two later, once RePoE has the new build):

    docker compose run --rm app update     # downloads mods, bases, essences, fossils, bench crafts, localized texts
    docker compose restart app             # loads the new database
    # locally: python -m poecraft update

Then check:
- **League:** prices and trade links follow the current challenge league automatically (poe.ninja's and the trade
  site's league lists). Pin one with `POECRAFT_LEAGUE` if needed (e.g. for Hardcore).
- **Price estimates:** `config/prices.json` holds the fallback prices used when poe.ninja has none - review them each
  league (the plan says which items used an estimate).
- **New mechanics:** a patch can add currency or crafting the planner does not model; such mods appear as "not found"
  in imports and are not offered as targets until supported. `python -m unittest discover -s tests` should still pass.

### Command line

```bash
python -m poecraft bases regalia
python -m poecraft mods --base "Vaal Regalia" --ilvl 86
python -m poecraft plan --base "Vaal Regalia" --ilvl 86 \
    --target "+# to maximum Life>=140" --target "+# to maximum Energy Shield>=50" \
    --target "Cold Resistance>=36" --target "Lightning Resistance>=36"
python -m poecraft plan --base "Stygian Vise" --influence hunter \
    --target "+# to maximum Life>=90" --target "#% increased maximum Life" --target "Cold Resistance>=36"
```

**Recovery mode:** for a rare you already own, pass its mods with `--have` to get every recovery
option with computed odds (Annul, metacraft + Scour/Annul/Chaos, Exalt, start over). Mod IDs are
listed by `python -m poecraft mods --base "Vaal Regalia" --tiers`.

```bash
python -m poecraft plan --base "Vaal Regalia" --target "+# to maximum Life>=160" \
    --target "Cold Resistance>=42" --target "Lightning Resistance>=25" \
    --have IncreasedLife12 --have ColdResist8 --have LightningResist1
# -> best: Orb of Annulment, 33.3% to remove the blocking Lightning mod, then bench-craft it
```

A target is a piece of mod text plus an optional `>=MIN`. `MIN` applies to the mod's first number.
You can add a `prefix:` / `suffix:` prefix to the text to pick one side when the text matches both.
Add `--json` to get the raw plan. Special targets: `fractured:TEXT>=MIN` (Fracturing Orb first) and
`TEXT (Veiled)>=MIN` (Aisling or Veiled Chaos Orb, then unveil), `TEXT (Elevated Elder)>=MIN` (Orb of Dominance,
range plan; needs `--influence elder`).

### Worked example (output trimmed)

```
=== Vaal Regalia (Body Armour), ilvl 86 ===
--- BEST PLAN: Essence spam: Screaming Essence of Hatred + bench craft ---
Expected cost: 253.2 chaos  (rolling 221.2 + bench 2.0 + divines 30.0)
  attempt: analytic 0.678% | simulated 0.6% (120/20000, 95% CI 0.502-0.717%)
  2. Reroll: Screaming Essence of Hatred           chance 0.678% (1 in 147)
     -> Rare with Life>=140, Cold Res>=36, Lightning Res>=36; free prefix for the bench craft
     on fail: repeat (Orb of Scouring first - this essence tier needs a Normal item)
     on fail: near miss -> Exalted Orb ~5-7%                          [approximate]
     on fail: Prefixes Cannot Be Changed + Scour (~300c) ...          [not computed]
  3. Crafting bench: +(56-69) to maximum Energy Shield
  4. Divine Orb until values meet thresholds      83.3% per Divine
--- Alternative 1: Fossil spam: Frigid + Metallic + Pristine + bench craft   354 c
--- Alternative 2: Harvest reforge: Cold [APPROXIMATE]                      1,212 c
--- Alternative 3: Chaos Orb spam + bench craft                             1,376 c
--- Alternative 4: Alteration → Regal → Exalt + bench craft                 1,475 c
```

## Architecture

```
poecraft/
  config.py              paths, data-source URL, supported item classes
  rules/mechanics.json   crafting rules missing from the game export (mod counts, resonators,
                         influence tags, harvest tags, metacraft ids, feature support table)
  data/fetch.py          step 1: download the RePoE JSON export -> data/raw/ (+ manifest with sha256)
  data/build.py          step 2: normalise into SQLite -> data/poecraft.db
  data/repository.py     loads the DB into dataclasses (Base, Mod, Essence, Fossil, BenchCraft)
  engine/context.py      mod pool for base + ilvl + influences (spawn tags, generation weights,
                         fossil multipliers), tiers, the "family" list shown in the UI, targets
  engine/analytic.py     fast probability engine (weighted mod picks, group exclusion, affix limits)
  engine/simulate.py     Monte Carlo item simulator (independent cross-check)
  engine/planner.py      strategy search, cost model, step/branch generation
  data/ninja.py          live poe.ninja prices: leagues, exchange overview, ETag cache, fallback status
  engine/prices.py       chaos-equivalent prices: live snapshot -> config/prices.json -> defaults
  server.py, web/        stdlib HTTP server + vanilla JS front-end
config/prices.json       fallback currency prices (used when poe.ninja is unavailable)
tests/                   python -m unittest discover -s tests -v
```

**Why this stack:** Python's standard library covers HTTP, SQLite and JSON, so the planner runs
anywhere Python is installed with no build step and no `pip install`. SQLite is the local game
database: one file, easy to inspect, rebuilt from raw data in about a second. The front-end is
plain HTML/JS served by the same process, so a single command starts everything.

### Data source

[RePoE (fork)](https://repoe-fork.github.io/) is a free community export of the PoE 1 game files
(GGPK) to JSON, regenerated after each patch. No account or API key is needed. Files used:
`mods`, `base_items`, `essences`, `fossils`, `crafting_bench_options`, `item_classes`.
From these the database holds every released equipment base (~1,000), every prefix/suffix mod
(regular, influenced, essence-only and crafted: ~6,000) with spawn weights, tags, groups and stat
ranges, plus all essences, fossils and bench recipes.

### Updating for a new league

```bash
python -m poecraft update
```

That re-downloads the export and rebuilds the DB. Then:
- live prices follow the new league automatically (first league on poe.ninja); refresh the
  fallback `config/prices.json` if you want good offline estimates;
- if GGG changes a mechanic, edit `poecraft/rules/mechanics.json` (no code changes needed for
  mod-count odds, resonators, harvest tags or influences).

Use `POECRAFT_REPOE_URL=<mirror>` to download from a mirror.

### Offline data import

Download the six `*.json` (or `*.min.json`) files from https://repoe-fork.github.io/ by hand, then:

```bash
python -m poecraft fetch --from-dir path/to/folder
python -m poecraft build
```

## How plans are computed

1. **Mod pool:** a mod can roll if the first of its spawn tags found on the item has weight > 0
   (base tags plus influence tags), multiplied by its generation weights, with
   `required_level <= item level`. Fossils multiply weights by `weight/100` for each matching
   mod tag.
2. **Strategy families,** each optionally finished with one bench craft:
   Chaos spam, Essence spam (the essence's guaranteed mod plus random mods), Fossil spam (best
   combos of up to 3 fossils found by search), Harvest tag reforge, and
   Alteration(+Augment) → Regal → Exalt with "scour and restart" on a miss.
3. **Probability:** a dynamic program over item states. Groups holding target mods are tracked
   exactly; all other groups are pooled per affix, with remaining weight estimated from the
   size-biased mean. The best strategies are then **re-run as a Monte Carlo simulation**
   (default 2,000 items; `--sims` / the UI selector go up to 100,000), and the UI shows both numbers with a
   95% CI. If they disagree, the plan says so.
   **Time limits (simulation only):** each strategy's simulation stops after 5 s
   (`POECRAFT_SIM_TIME_LIMIT`) and is marked "low confidence (timeout)" with the runs it completed; after 20 s of
   simulation in a plan (`POECRAFT_SIM_PHASE_LIMIT`) the remaining strategies are not simulated. Either way the
   plan carries the warning "Simulation timed out. Results are approximate." The fast calculation is never cut.
   The optional recovery variant (step 5) is the exception: its policy solve gets 5 s per variant and 15 s per plan
   (`POECRAFT_RECOVERY_TIME_LIMIT` / `POECRAFT_RECOVERY_PHASE_LIMIT`), and when it runs out that variant is skipped
   with a note - the same as when it has too many item states. With 5-6 targets this solve could take many minutes.
   Limits are wall-clock, so on a slow machine a plan can show a timeout that a fast one does not.
   **Progress:** the CLI prints progress lines on stderr (`--quiet` turns them off); the web UI streams progress
   from `/api/plan` (`"stream": true`, NDJSON) and shows the phase, X/Y and an estimate of the time left.
4. **Cost:** E[cost] = E[cost of one attempt] / P(success), to an item whose targets are at tiers that can
   reach your thresholds. Divine Orbs that reroll the numbers up to the thresholds are an **optional** step
   ("OPTIONAL (high cost)"): not in the expected cost, shown separately with the share of finished items whose
   values already meet the thresholds and the total with Divines. Strategies are still *ranked* by the cost to
   your thresholds (with Divines), so a cheaper tier that rarely meets them does not beat one that always does;
   the comparison table shows that ranking cost. Bench crafts include re-crafts when the bench range can miss the
   threshold. The Divine before a Fracturing Orb stays in that phase's cost (the value must be set first).
   **Plan layout:** the main plan is a numbered, linear list (`kind: "main"`, `number`). Recovery branches
   (`kind: "recovery"`, `branch_of` = the main step's number) are conditional and optional: each starts with
   "IF <item state>: <action>" and says how often that state comes up. The web UI shows them collapsed under their
   step ("Recovery options for this step"); the CLI prints them after the main plan under "--- RECOVERY BRANCHES ---".
5. **Recovery (`+ Annul/metacraft recovery` variants and `--have`):** value iteration
   (`analytic.RecoveryMDP`) over the same item states picks, for each state, the cheapest of:
   Exalted Orb; Cannot Roll Attack/Caster + Exalt; Orb of Annulment (uniform over removable mods);
   Prefixes/Suffixes Cannot Be Changed + Annul (the metacraft itself can be annulled), + Scour,
   or + Chaos; or start over. The metacraft needs a free slot on its own side and is consumed by
   the Scour/Chaos it protects against. The fixed point with "start over" is solved by policy
   evaluation. The policy is replayed on concrete items in the simulator, and both the
   per-attempt odds and the cost per item are compared.

### What is exact, approximate or not supported

**Full list of limitations: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).**

Shown in the UI ("What the planner models") and on each step/branch:

| Status | Items |
|---|---|
| supported | Chaos/Alchemy, Alteration/Augment/Regal/Exalt, Essences, Fossils (weight-modifying), bench finish, Multimod (2 bench crafts, reroll families), Divine estimate, 2 influences, Orb of Annulment recovery, Prefixes/Suffixes Cannot Be Changed + Scour/Annul, Cannot Roll Attack/Caster + Exalt |
| approximate | rare/magic mod-count odds (community figures), Harvest reforge model, metacraft + Chaos (mod count with a locked side unpublished), Annul hitting an essence mod (counted as restart: upper bound), Eldritch implicits via Embers/Ichors, Shaper/Elder/Conqueror Exalted Orbs, Awakener's Orb, Fracturing Orb (uniform pick assumed), Veiled mods via Aisling / Veiled Chaos Orb (unveil option draw, side and Aisling behaviour assumed), Faceted / Hollow Fossil for the modifiers they add (`TEXT (Hollow)`), Orb of Dominance for Elevated targets and Orb of Conflict for tier 2/1 Eldritch targets (range plans only: odds not in the data, never a single cost), the analytic aggregation (always cross-checked by simulation) |
| not supported | metacraft + Alteration (not applicable: the metacraft fills the magic item's only slot on its side), Eldritch Chaos-Exalted-Annulment / presence upgrades / Eldritch on amulets, Recombinators, Synthesis, Veiled Exalted Orb / Syndicate-member unveils, jewels/flasks |
| not supported (missing data) | Beastcrafting (no recipe data in the export); Tangled, Glyphic, Fractured, Sanctified, Opulent Fossils (missing effect data - each piece named in the plan and docs/LIMITATIONS.md); Gilded, Bloodstained (no stat target) |

Known limits: thresholds apply to the mod's first stat only. Bench restrictions for local
defence crafts are inferred from the base's defence types (the export carries no spawn tags for
bench crafts). Live prices cover what poe.ninja's exchange lists; Harvest reforge cost and
unlisted items use `config/prices.json`.

## PoE 3.30 readiness

**WAITING FOR GAME DATA**: see [upgrade report](docs/POE_3_30_UPGRADE_REPORT.md). Check without changing data: `python -m poecraft update --expect-version 3.30 --dry-run`. Remove `--dry-run` to stage, validate and promote the export with a retained backup. Stop the server before updating. Existing unversioned exports are not certified as 3.30.

## Windows portable

[Download Windows builds from GitHub Releases](https://github.com/tentateurss/TensChep-Workshop/releases). Python is not required for the portable EXE. See [release instructions](docs/RELEASE.md). PoE 3.30 compatibility is not yet confirmed.
