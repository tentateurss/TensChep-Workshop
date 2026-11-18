Timeless seed fixtures are small source subsets, not replacement game data.

- `timeless_ru_329.json`: two resolved Russian lines from the previously working 3.29 `timeless/conquerors.json` (Xibaqua and Avarius). Used only inside scoped parser mocks to retain old-format coverage.
- `timeless_ru_330.json`: the first Russian seed translation entry from RePoE 3.30.0.2.2, downloaded from `https://repoe-fork.github.io/Russian/stat_translations.min.json` on 2026-11-17. Its `{2}` value uses `alternate_tree_version_keystone`, exported as `noop` in `Russian/stat_value_handlers.min.json`. No localized name mapping is inferred.

The 3.30 test checks explicit refusal to calculate this unresolved text. Neither fixture is loaded by the application or inserted into active data.

Unique search fixtures:

- `kaoms_sign_ru.txt`: the supplied Russian advanced item copy, including implicit headers and an unscalable skill level.
- `kaoms_sign_ru_stats.json`: four entries from `https://ru.pathofexile.com/api/trade/data/stats`, retrieved 2026-11-18; used only for controlled matching tests.
