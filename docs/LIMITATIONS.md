# Limitations

Everything the planner does not model, or models only approximately, in one place.
Each entry is taken from an existing reason string or marker. **Source** says where it lives:
`rules` = `poecraft/rules/mechanics.json` (the `features` list is the "What the planner models"
card in the UI; its "not supported" items are also printed at the end of every CLI plan), `planner`
= `poecraft/engine/planner.py`, `eldritch` = `poecraft/engine/eldritch.py`, `README` = README.md.

**Flagged today** means the user sees it without reading the code: the feature card, a CLI line, a
step or branch badge (`approximate`, `not computed`, `not supported`), a plan note, or an
infeasibility reason.

## a. Not supported at all

These never appear in a plan.

| Item | Consequence | Flagged today | Source |
|---|---|---|---|
| Recombinators, Synthesis | No plan uses them | Feature card + CLI "Not supported" line | rules `features` |
| Beastcrafting | **Not supported (missing recipe data):** the export has no recipe file - not which beasts a recipe needs, its cost, or what it adds/rerolls. The outcome mods of some recipes exist in mods.json (Aspect suffixes 'of Farrul', 'of Saqawal', 'of Fenumus', 'of Craiceann'; no spawn weights), with nothing linking them to a recipe. A target only these give (e.g. "Aspect of the Cat", or "TEXT (Beastcraft)") is infeasible with that reason | Yes: infeasibility reason (CLI + UI 422); "Beastcraft" entries in the UI mod list carry the reason; own feature row and CLI line "Not supported (missing recipe data): Beastcrafting - …" | rules `beastcraft_missing`, `data/special.py` |
| Tangled Fossil | **Not supported (missing fossil mod data):** the random weights it applies ("Random effects revealed when resonator is fully socketed") are not in the data. Excluded from fossil search | Yes: own feature row + CLI line; reason on a "(Tangled)" target | rules `fossil_effects`, `engine/special.py` |
| Glyphic Fossil | **Not supported (missing fossil mod data):** which Corrupt Essence modifier it adds and how it is picked (essences.json has no corruption-only flag or per-modifier chance). Excluded from fossil search | Same | same |
| Fractured Fossil | **Not supported (missing fossil mod data):** which modifier "Fracture a random modifier" picks. Excluded from fossil search. (The Fracturing Orb, approved earlier as approximate with a uniform-pick assumption, is the supported fracture route) | Same | same |
| Sanctified Fossil | **Not supported (missing fossil mod data):** how much "High Level modifiers are more common" changes the weights, and the exact "lucky" value rule. Excluded from fossil search | Same | same |
| Opulent Fossil | **Not supported (missing fossil mod data):** how "No Tagless modifiers" is applied - only a description; its weights (drop x10) are in the data. Excluded from fossil search | Same | same |
| Gilded Fossil | **Not supported (no stat target):** it only adds "Item sells for much more to vendors". Excluded from fossil search | Same | same |
| Bloodstained Fossil | **Not supported (no stat target):** it corrupts the item and adds a corrupted implicit (implicits are not planner targets; a corrupted item cannot be crafted further). Excluded from fossil search | Same | same |
| Delirium/Heist special crafts | No plan uses them | Feature card + CLI line | rules `features` |
| Jewels, flasks, maps, tinctures | These bases cannot be selected | Feature card + CLI line | rules `features`, `config.SUPPORTED_ITEM_CLASSES` |
| Metacraft + Alteration | Not applicable: on a magic item the metacraft fills the only slot of its side | Feature card + CLI line | rules `features` |
| Eldritch Chaos / Exalted / Annulment (dominance) | No plan uses them | Feature card + CLI line | rules `features` |
| Eldritch implicits on amulets | Target is infeasible: "Eldritch currency does not apply to amulets (per currency descriptions)" | Infeasibility reason | eldritch `options()` |
| Multimod combined with Alteration chains | Two bench crafts are only offered with reroll families | Feature card note | rules `features` |
| Eldritch targets in recovery mode (`--have`) | Refused: "Recovery mode works on explicit mods only; Eldritch implicits are not modelled in recovery." | Error message | planner `recover()` |

## b. Modelled approximately

These appear in plans, flagged `approximate`.

| Item | Consequence | Flagged today | Source |
|---|---|---|---|
| Harvest tag reforge | Odds assume "first mod forced from the tag, remaining mods rolled like a Chaos Orb"; the real algorithm is not public. Its fast and simulated odds have disagreed in past runs | Strategy + step badge with the reason | rules `harvest_note` |
| Prefixes/Suffixes Cannot Be Changed + Chaos Orb | The number of new mods with a locked side is assumed to follow the normal rare distribution | Recovery step badge + note | planner `_LOCK_CHAOS_NOTE` |
| Annulment removing an essence-guaranteed mod | Counted as a restart, so the cost is an upper bound | Step badge + note | planner `_ESSENCE_ANNUL_NOTE` |
| Eldritch implicits (Embers / Ichors) | Currency-to-tier link taken from the currency descriptions; no Eldritch presence assumed | Strategy + step badge with the reason | rules `eldritch_note` |
| Shaper / Elder / Conqueror Exalted Orbs | Odds assume both affix sides are open, so the real chance can be higher (cost overstated) | Step badge with the reason | rules `influence_exalt_note` |
| Awakener's Orb | Assumes one influenced mod from each item is kept (community rule, not in the data) | Strategy + step badge with the reason | rules `awakener_note` |
| Unveil options | Assumes the 3 options are different outcomes of the veiled mod's side, drawn by spawn weight without replacement, never sharing a group with a mod already on the item (only the outcomes and weights are in the data) | "Unveil (Jun)" step badge with the reason; comparison flag "unveil and …: calculated, not simulated" | rules `veiled_options_note`, `engine/veiled.py` |
| Aisling (Research, rank 3) — **modelled from memory, not from data** (behaviour as described in the request) | Not in the data at all. Assumes no existing mod is removed. If the real Research craft removes a random mod, the cost here is understated and the success chance overstated | Yes: on the Aisling step and once in the plan notes: "Aisling model assumes no existing mod is removed. If the real Research craft removes a random mod, the cost here is understated and the success chance overstated." | planner `_AISLING_REMOVE_NOTE` |
| Aisling (Research, rank 3) details | Not in the data at all. Modelled as the request describes: adds one veiled mod in a free slot without removing a mod, on the side the plan kept free. If it really removes a random mod (like the Veiled Exalted Orb in the data), the Aisling numbers do not apply | Aisling step badge + note with exactly this reason | rules `aisling_note` |
| Veiled Chaos Orb | Data: rerolls the whole rare "including a random Veiled modifier". Assumed: the veiled mod is a prefix or suffix with 50% each and counts toward the normal 4–6 mod count | Unveil step detail "50% assumed (not in the data)" + strategy note | rules `veiled_chaos_note`, `veiled_side_chance` |
| Annulment retry after a wrong unveil (Aisling) | Uniform over the explicit targets + the wrong unveil; other random mods on the item are ignored | `approximate` badge on that on-fail branch | planner `_aisling_branches` |
| Faceted Fossil (supported for targets it adds, e.g. "+1 to Level of Socketed Intelligence Gems (Faceted)") | Data: gem weight x10 and 3 added mods with spawn weights. Assumed: the added mods join the normal pool at their spawn weights, with the fossil's tag weights applied to them too (the data lists them, not how they are weighted). Offered only for a target it adds, alone or with the best ordinary fossils for the other targets; not part of the general fossil search | Yes: approximate badge + plan note "Special fossil: Faceted Fossil (game data: More Gem modifiers). …" | rules `fossil_effects`, `engine/special.py` |
| Hollow Fossil (supported for "Has 1 Abyssal Socket (Hollow)") | Data: the forced mod (a suffix) and the allowed item tags. Assumed: it counts toward the usual 4-6 mod count, like an essence's guaranteed mod | Yes: approximate badge + plan note "Special fossil: Hollow Fossil (game data: Has an Abyssal socket). …" | same |
| Fracturing Orb | Assumes every mod on the item is equally likely to be fractured (the data says only "a random modifier"); the odds are the average of 1/(number of mods) | Strategy + "Fracturing Orb" step badge with the reason; comparison flag "Fracturing Orb phase: calculated, not simulated" | rules `fracture_note`, planner `_fracture_steps` |
| Analytic odds (non-target mods pooled per affix) | Fast-calculation odds can differ slightly from reality | Every recommended plan is checked against a 20,000-item simulation; disagreeing plans are demoted with the reason; the table shows simulated cost and 95% range | analytic module docstring, planner `_disagree_text` |
| Simulation stopped by a budget cap | Fewer than the target number of simulated attempts | `low-confidence: only N simulated attempts …` flag | planner `_flag_low_confidence` |
| Bench crafts for local defences | Which bases accept them is inferred from the base's defence types (the export has no spawn tags for bench crafts) | Yes: when the offered craft has a local Armour/Evasion/Energy Shield stat, its bench step is `approximate` with "bench craft eligibility is inferred from the base's defence types; the export has no spawn tags for bench crafts". Other crafts are filtered by item class only and stay `computed` | planner `_BENCH_INFERRED_NOTE`, `ItemContext.bench_inferred` |

## c. Modelled with known data gaps

The game data export lacks something these mechanics need.

| Gap | Consequence | Flagged today | Source |
|---|---|---|---|
| Orb of Conflict odds (data: the chance "depends on its relative strength", no formula) | Tier 2 / tier 1 Eldritch targets get a **range plan**: a lower bound (every Orb raises the target, value meets the threshold), no upper bound, no middle estimate. Never recommended while any ordinary plan exists; otherwise shown as "SCHEMATIC ONLY" | Yes: range in the cost line, the comparison table and the step; note "Odds for Orb of Conflict are not in the game data. This plan shows a range, not a single number; treat it as a schematic, not a quote." | planner `_range`, `_conflict_part`, `_conflict_steps`, rules `conflict_note` |
| Orb of Conflict mechanics not in the data | Assumed: one Orb moves one tier; the raised value meets the threshold (part of the best case) | Step detail | rules `conflict_note` |
| Orb of Dominance (Maven's Orb) odds; Elevated mods have no spawn weights | Elevated targets get a **range plan**: best case 100% (a lower bound), uniform over the 2 influenced mods 50% (an assumption, shown as a middle figure), worst case 0% (no upper bound). Never recommended while any ordinary plan exists; otherwise "SCHEMATIC ONLY" | Yes: range in the cost line, the comparison table and the step, each bound with its reason; note "Odds for Orb of Dominance are not in the game data. This plan shows a range, not a single number; treat it as a schematic, not a quote." | planner `_range`, `_dominance_strategies`, `_dominance_steps` |
| Link from an Elevated mod to its base influenced mod | Not in the data: derived (same influence, side, group, stats contained). 87 of 106 link; the other 19 are "not supported (missing outcome data)" | Infeasibility reason | `data/elevated.py`, planner `_dominance_report` |
| "Highest tier" before the Orb of Dominance | Checked by the top tier's minimum value, so an overlapping lower tier would also pass | Step detail (rules `dominance_data_note`) | planner `_dominance_strategies` |
| A failed Orb of Dominance | The data does not say the item is destroyed (it removes one influenced mod). The plan redoes the whole preparation - an upper bound for that branch | On-fail branch + step detail | rules `dominance_data_note` |
| Currency-to-tier table for Embers / Ichors | Lesser=T6 … Exceptional=T3 taken from description wording | Approximate badge (see b) | rules `eldritch_note` |
| Gating of Eldritch "presence" versions | Presence-upgraded implicits are left out; crafting is assumed to happen without a presence | Approximate badge + feature card | rules `eldritch_note`, `features` |
| How the 3 unveil options are drawn; prefix/suffix of a random veiled mod; Aisling's Research behaviour | Assumed (see b) | Approximate badges | rules `veiled_options_note`, `veiled_chaos_note`, `aisling_note` |
| How the Fracturing Orb picks a mod | Uniform assumed (see b) | Approximate badge | rules `fracture_note` |
| Whether a Divine Orb can change a fractured value | Assumed not to: the target is Divined before fracturing | Step note | rules `fracture_note` |
| Item level the influence Exalts call "high-level" | Not checked; only the mods' own required level is applied | Step note | rules `influence_exalt_note` |
| Awakener's Orb kept-mod rule | See b | Approximate badge | rules `awakener_note` |
| Rare / magic mod-count distribution | See f | Feature card | rules `rare_mod_count_note`, `magic_mod_count_note` |

## d. Costs not included

| Cost | Consequence | Flagged today | Source |
|---|---|---|---|
| Base items | Never priced. Ordinary plans need one base. A failed influence Exalt or Fracturing Orb needs a fresh base (about 1/p bases per finished item, e.g. ~4.5 for a 22% Exalt, ~4.3 for a 23% Fracturing Orb). Awakener's Orb uses two bases per attempt | Yes: every plan shows "Base item price is not included." under the expected cost (CLI) / cost summary (UI); influence and Awakener steps say it again. Not shown in `--have` recovery output, which reports a recovery policy, not a plan | planner `_BASE_PRICE_NOTE` (`cost_note`), rules `influence_restart_note`, planner `_awakener_steps` |
| Buying a donor / second item for Awakener's Orb | Only the crafting route is costed | Step detail: "Buying one instead is not priced (the price data has currency, not items)" | planner `_awakener_steps` |
| Currency with no live price and no config entry | Priced at the `unknown` fallback (1 c) | Plan warning "No price configured for: …" | `config/prices.json`, planner |
| Essences missing from poe.ninja | Per-tier default price | Warning "Price estimate: … is not listed on poe.ninja" + `estimated` in the price table | planner, `engine/prices.py` |
| Aisling (Research, rank 3) | No market price source: config guess (150 c), marked estimated | Yes: warning + strategy note "Price estimate: Aisling (Research, rank 3) has no live price; … (a rough guess - … set your own)." | planner `_finish_plan`, `config/prices.json` |
| Harvest reforge | Always the config value (poe.ninja lists no reforge price) | Yes, when a Harvest strategy is shown: warning "Price estimate: Harvest Reforge (tag) has no live price; using the estimate 3 c from config/prices.json (edit it each league)." (value from config), the same note on the strategy, and a price-table row with source `config` | planner `_finish_plan`, `config/prices.json` |

**How much this understates real cost:** this can't be quantified from the data the planner has, because it has no item prices.
The shortfall is roughly base price × number of bases used. It's small for ordinary
plans (one base) and grows with the number of restarts for influence plans.

## e. Interactions explicitly not modelled

| Interaction | Consequence | Flagged today | Source |
|---|---|---|---|
| Annulment + regular Exalted Orbs on an influenced item after a failed influence Exalt | Not considered; the plan restarts on a fresh base instead | `not computed` branch on the Exalt step | planner `_influence_step`, rules `influence_restart_note` |
| Exalt / Annulment recovery on the dual-influenced item after Awakener's Orb | Not considered; the plan restarts with a new donor and second item | `not computed` branch | planner `_awakener_steps` |
| Influence target on a base that already has another influence (with Awakener's Orb) | Infeasible | Infeasibility reason | planner `_influence_targets` |
| Two targets from the same influence | Infeasible ("would have to roll on the influenced item, which is not modelled") | Infeasibility reason | planner `_influence_targets` |
| Influence target together with an Eldritch implicit target | Infeasible. The data forbids Eldritch after influence; the reverse order is not stated in the data | Infeasibility reason | planner `_influence_targets` |
| Veiled target with a fractured or influenced target; veiled targets in `--have` | Infeasible / error with the reason | Infeasibility reason; `--have` error text | planner `_veiled_report`, `recover` |
| Annul / metacraft recovery inside a Veiled Chaos plan; a metacraft lock before the Aisling Annulment retry | Not computed (Veiled Chaos misses simply reroll) | `not computed` branch on the unveil step of both strategies | planner `_veiled_strategies` |
| Orb of Dominance with more than 2 influenced mods; an Elevated target with another influenced, veiled or fractured target; more than one Elevated target; a base without the influence (Exalt first); slots other than Body Armour, Boots, Gloves, Helmet | Infeasible with the reason (the class restriction is the currency text) | Infeasibility reason; `not computed` branch for >2 influenced mods | planner `_dominance_report`, `_dominance_steps` |
| Orb of Conflict with a second Eldritch target | Infeasible: the Orb lowers the other implicit | Infeasibility reason | planner `_eldritch_parts` |
| Continuing with a wrongly fractured mod | Not considered; the plan starts over on a fresh base | `not computed` branch on the Fracturing Orb step | planner `_fracture_steps`, rules `fracture_restart_note` |
| Fractured target on an influenced base, two fractured targets, fractured + influenced target | Infeasible ("Cannot be used on Influenced or Fractured items"; the combination with an influence is not modelled) | Infeasibility reason | planner `_fracture_report` |
| Fracturing a crafted (bench-only) mod | Infeasible: the data does not say whether a Fracturing Orb can pick a crafted mod | Infeasibility reason | planner `_fracture_report` |
| Lower-tier essences after fracturing | Not offered: they need a Normal item, and the state of a scoured item with a fractured mod is not modelled; Screaming/Shrieking/Deafening/corruption-only essences are used | Yes: plan note "Lower-tier essences are not offered after fracturing: they require a Normal item." on every fracture plan where such an essence was skipped | planner `_LOWER_ESSENCE_NOTE`, `_essences` (`fixed`) |
| Recovery options in `--have` | Calculated by the recovery solver, not simulated | CLI and API label "calculated … not simulated" | `__main__._print_recovery`, planner `recover()` |
| Eldritch and influence-Exalt parts of a plan | Closed form, not simulated; only the explicit part is simulated | Comparison-table flag + selection note | planner `_comparison`, `_selection_note` |

## Localization

| What | Consequence | Flagged today | Source |
|---|---|---|---|
| Game data: base items, mods, essences, fossils, currency | **Supported (localized by internal id)** from the game's own localized files: RePoE's per-language exports, validated against the English export of the same build and built into `data/locale_game_<code>.json` by `python -m poecraft update`. Russian: 5,041 base items + 36,341 mod texts; German: 5,041 + 36,384. Never machine-translated | Marker top-right (UI) / first CLI line: where game data comes from in this language | `poecraft/data/locales.py`, `poecraft/i18n.py` `game_text` |
| Game-data strings with no localized entry | English | Yes: "EN" mark (UI) / "[EN] " prefix (CLI) on that string | `i18n.localize_plan`, server `_localize_families`, `_localized_prices` |
| Localized mod texts missing a stat line (62 Russian, 19 German) | Left out of the locale files: shown in English | Yes: EN mark; ids listed in each file's `_meta.excluded_incomplete_mod_texts` | `data/locales.py` `build_map` |
| Item classes, influence names, bench master names, veiled/elevated affix names | English (no locale data loaded for them) | Yes: EN mark (UI), "[EN] " (CLI; class filter options) | server, `app.js` |
| A language export that is not the same build as the English one (any id or non-localized field differs) | Refused: no file written, the previous file is kept | Yes: `update` / `build` print "REFUSED" / the mismatch | `data/locales.py` `compare` |
| Price-table names | Looked up as English name -> item id (released currency items with a unique name), then localized by id; other entries (e.g. Harvest reforge, Aisling) stay English | Yes: EN mark | `i18n.currency_text` |
| Planner explanations (step details, plan notes, infeasibility reasons, warnings, strategy names, comparison rows, `--have` options) | **Localized** (en/ru/de): 545 message keys in `poecraft/locales/*.json`, rendered from language-free message objects; game names are `game_text()` parameters. Missing key -> English | Yes: "[EN] " / EN badge on the text, and the plan lists the untranslated keys (CLI line, UI note) | `poecraft/messages.py`, planner (`M(...)`), `messages.missing_keys` |
| Quoted game texts inside explanations (currency descriptions such as 'Cannot be used on Influenced or Fractured items', fossil effect texts, Harvest craft text) | English in every language: the locale data has names, not descriptions - quoting a translation would mean translating game data | Yes: kept in single quotes as quotes | message templates |
| Item classes, influence names (Shaper, Hunter ...), bench master names, Eldritch side names (Searing Exarch / Eater of Worlds) inside explanations | English (no locale data loaded for them) | Yes: "[EN] " / EN badge on the name | `messages.N` |
| CLI target-resolver echo ("'Cold Resistance' -> using '+#% to Cold Resistance'") | Phrase localized; the matched labels are shown as typed / in English | No (it echoes the user's input match) | `poecraft/__main__.py` |
| Recovery table column headers in the CLI | Localized; alignment of ru/de headers is approximate | n/a | locale key `cli.rec.header` |
| UI strings missing from a language file | Shown in English | Yes: "EN" mark (UI) / "[EN] " prefix (CLI) | `poecraft/i18n.py` `t`, `app.js` `tt`/`tx` |

## f. Assumptions that affect all strategies

| Assumption | Consequence | Flagged today | Source |
|---|---|---|---|
| Rare mod count 4/5/6 = 8/12, 3/12, 1/12 (Chaos, Alchemy, Essence, Fossil, Harvest) | All reroll odds depend on it; not published by GGG | Inline note on each plan's reroll step + feature card | rules `rare_mod_count_note` |
| Magic mod count 1 or 2 at 50/50 (Alteration / Transmutation) | All Alteration odds depend on it | Alteration step badge with the reason | rules `magic_mod_count_note` |
| Fossil tag weights multiply for every matching mod tag (weight/100) | All fossil odds depend on it | Fossil strategy note | rules `fossil_note` |
| Whispering–Wailing essences need Orb of Scouring between attempts; Screaming/Shrieking/Deafening/corruption-only reforge a rare directly (from each essence's currency text; checked by a test) | Affects essence costs | On-fail text on the essence step | rules `essence_note` |
| Value thresholds apply to the first number of a mod only | Hybrid / "Adds # to #" mods are judged by their first value | UI target hint; README | README "Known limits" |
| Prices are config estimates when live prices are unavailable | All costs shift with the market | Warning + "estimated prices" badge | `data/ninja.py`, `engine/prices.py` |
| `min_variance` scores expected cost + 2 standard deviations | A risk score, not a literal percentile of the cost | Objective description in every plan | planner `OBJECTIVES` |
| Essence item level restriction (Whispering 35, Muttering 45, Weeping 60, Wailing 75) | Read from essences.json `item_level_restriction`; such an essence is not offered above that item level, with a warning naming the excluded essences. The currency descriptions do not state it - verify in game if it looks wrong | Warning "Lower-tier essences are not offered for item level N" | `ItemContext.essence_options`, `Essence.item_level_restriction` |
| Item import: client field names and line markers in Russian / German ("Класс предмета:", "(неявно)", "Предмет Создателя" ...) | These are game-client UI strings, not in the game-data export; the English ones are exact, the ru/de ones are best-effort keyword matches. Base names and mod lines themselves are matched by the game's own localized strings | Unmatched lines are listed as warnings | `engine/itemtext.py` |
| Item import: Eldritch implicit side in plain Ctrl+C text | When the same stat exists on both sides and the item shows both footers, the side is ambiguous | Warning "Eldritch implicit side is ambiguous, assumed ..." | `engine/itemtext.py` |
| Screenshot OCR (beta) | External service (OCR.space), opt-in via POECRAFT_OCR_SPACE_KEY; numbers can be misread | Warning on every OCR import; recognised text shown for checking | `poecraft/ocr.py` |
| Battle Royale event mods (ids containing "Royale") | Excluded from every mod pool (odds, simulation, recovery, mod search, Dominance tiers): 4 of them carry normal spawn weights in the export but do not roll in standard crafting. Identified only by the id marker - the export has no other field | Rule note `event_only_mod_note` | rules `event_only_mod_markers`, `context.event_only` |
| Trade search: mods mapped by text to the trade site's stats | A target whose text the trade site words differently is left out (named in a warning); thresholds of "Adds # to #" mods use the first number, the trade site filters such stats by their average | Warning per unmapped line | `data/trade.py`, `engine/tradelink.py` |
| Trade search: base type and site | Uses the English base name on www.pathofexile.com (the international site); bases sharing a name (Two-Toned Boots) cannot be told apart there; crafted and rolled versions of a mod are different trade stats - targets search the rolled (explicit) one | — | `engine/tradelink.py` |
| Templates | Web UI templates live in the browser (localStorage), CLI templates in data/templates.json - two separate stores | Hint in the templates panel | `poecraft/templates.py`, `web/app.js` |
| Production server | Standard library HTTP server (threaded, bounded) behind Caddy - not waitress/gunicorn, to keep the project dependency-free and the NDJSON progress stream; one process, so rate limits and caches are per process; plans run one at a time (queue up to 60 s) | 503 "busy", 429 with Retry-After | `server.py` (`BoundedServer`, `Handler._admit`), `ratelimit.py` |
| Item Evaluator: what to drop | Importance = how hard a property is to get (planner's Chaos-spam cost per modifier tier, bench cost, Ember/Ichor cost, influence Exalt price) - a proxy for value, not market data; pseudo totals (total resistances / life) and value ranges beyond "90% of the item's value" are not used | Ranking with the reason per property shown on the page | `engine/evaluator.py` |
| Item Evaluator: request budget | GGG allows 30 searches per 5 min per IP, shared by every user of a public server: at most 5 searches per evaluation, results cached 5 min; when the limit is reached the evaluation stops with a note | Warning on the page | `data/trade.py` (`_slot`, `_note_state`) |
| Item Evaluator: jewels, maps, tinctures | Rare / magic items of classes the crafting data does not hold are searched by base type only (no modifier filters); map tier is not filtered | Note on the page | `engine/evaluator.py` |
| Recovery from a fractured, influenced item | A fresh fractured base with influence cannot be planned (Fracturing Orb rule), so "start over" is priced as rolling the fractured modifier normally - an upper bound | Note in the recovery | `engine/planner.py` |
| Timeless Jewels: equivalent seeds | Equivalence is judged on the ticked notables only; "better" for Glorious Vanity means every roll at least as high (assumed better for every stat); the same conqueror only (other conquerors give the same notables but another keystone); listings beyond the cheapest 100 that are not in the 30-seed search are not seen | Counts on the page | `engine/timeless.py`, `engine/evaluator.py` |
| Eldritch implicit not in the data for a base | Kept as an Eldritch implicit for the price search (trade implicit stat), with a note; the planner cannot plan it | Note on the page | `engine/itemtext.py` |
| Timeless Jewels: data source | Effects are read from Path of Building's precomputed tables (tree version of their last update); a new passive tree before PoB updates its tables shows "not in the tables" for new passives. Abyss "conquered" jewels (Tecrod and the others) are not supported | Per passive on the page | `data/timeless.py`, `engine/timeless.py` |
| Timeless Jewels: language | Passive names and stats are the passive tree export's English texts (GGG publishes it in English only); jewel, conqueror and seed are read in English / Russian / German | EN mark, note on the page | `engine/evaluator.py` |
| Item Evaluator: uniques in Russian / German | Unique names are not in the game data, so a localized unique is searched by base type (all uniques on that base) | Warning on the page | `engine/evaluator.py` |
| Item Evaluator: gems, maps, special items | Searched by name only (gem level / quality, map tier and mods are not filtered) | Note for gems | `engine/evaluator.py` |
| Divine Orbs for value thresholds are optional | The expected cost leaves them out; without them the values meet the thresholds only on the stated share of items. Ranking and the comparison table still include them (cost to your thresholds). Rerolling until the values pass, instead of Divining, is not offered as an alternative | "OPTIONAL (high cost)" step, separate optional cost with the total | planner `Strategy.cost` / `cost_with_divine`, `_metrics` |
| Simulation time limits: 5 s per strategy, 20 s per plan (config / env) | A heavy plan's simulation check may cover fewer runs, or skip strategies; the fast calculation is complete either way | "low confidence (timeout)" on the strategy, "Simulation timed out. Results are approximate." warning, amber progress bar | `config.SIM_TIME_LIMIT_S`, `config.SIM_PHASE_LIMIT_S`, planner `_simulate` |
| Recovery-policy time limits: 5 s per variant, 15 s per plan | With many targets (5-6) the "+ Annul/metacraft recovery" variants are skipped, so a cheaper recovery plan may exist; plans without recovery are complete | Note on the strategy + warning listing the skipped variants | `config.RECOVERY_TIME_LIMIT_S`, `analytic.RecoveryMDP.deadline` |
| Wall-clock limits depend on machine speed | The same plan can time out on a slow machine and not on a fast one (results then differ only in what was checked / which recovery variants exist) | The plan's "simulation" block lists every timeout and skip | planner result `simulation` |
| Time-left estimate | Extrapolated from the current phase's rate; one step (a recovery solve) can take seconds, so it jumps; capped by the simulation phase's hard limit | Shown as "~N s remaining" | `engine/progress.py` |
| Mod search | Server-side (`engine/modsearch.py`, `/api/modsearch`): every word of the query in any order (localized label, English label, tier texts), word endings dropped for words longer than 4 letters, es / ms / hp / res expanded. A matching mod of the same item class that cannot roll on the chosen base is listed separately with the reason: "This mod exists, but cannot roll on this base type (requires X base)". X = the defence properties (base_items `properties`: armour / evasion / energy_shield / ward) shared by every base of the class whose own tags give that mod a spawn weight; influence-only mods are not part of this check | Explanation row with the selected base's defence type and example bases | `engine/modsearch.py` |

## Uncertain or not verifiable from the data

- Orb of Dominance / Orb of Conflict odds: nothing in the export; the plans show ranges, and a range plan is recommended only if its worst case beats the best ordinary plan - which no range plan can today, because none has an upper bound.
- Aisling's Research craft: whether it removes a mod (as the Veiled Exalted Orb does) or only adds one, and which side its veiled mod takes. Nothing about it is in the export (see b).
- Whether an influence Exalt can be applied after an Eldritch implicit: the data doesn't say, so it is treated as not supported (see e).
- Whether Eldritch implicits really need item level 75: taken from `required_level` in the data, but the currency text doesn't mention it.
- How far the "both affix sides open" assumption overstates influence Exalt costs: it depends on the item, and the planner doesn't compute it.

## PoE 3.30 transition

Current data and Timeless effects are not verified for 3.30. Partial prefix/suffix locks are not modelled. Full-lock recipes must be selected by item class and priced using the whole currency basket. See upgrade report (??. LIMITATIONS.md). The configured simulation default is 2000; 20000 is the default production HTTP cap, not the default run count.
