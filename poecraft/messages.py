"""Language-free explanation text for plans: message keys + parameters, rendered at the edge.

The planner never builds finished sentences. It builds Text objects - a message key from poecraft/locales/<code>.json
plus parameters - and the CLI / server render the whole result into one language at the end:

    M("m.alch.action", alch=CUR("Orb of Alchemy"))                 a message (template from the locale files)
    G("base", item_id, english) / CUR(english_name)                 a game-data name, via i18n.game_text()
    GL(mod_id, english_label, english_text)                         a mod label ('+# to maximum Life'), localized
    P(0.0068)                                                       a probability: "0.68% (1 in 147)"
    J(parts, sep) / a + b                                           joins

So one computed plan can be shown in any language without re-planning, and its ids and numbers are identical in
every language. Game-data names are never part of a template: they are parameters, looked up by internal id.
A key missing in a language falls back to English with the "[EN] " mark (the web UI turns it into an EN badge);
a game name with no localized string is marked the same way. str(text) is the English rendering.
"""
from __future__ import annotations

from . import i18n

EN_MARK = i18n.FALLBACK_MARK


class Text:
    """Base class: render(lang) -> str. str() renders English, so Text works where a str did before."""

    def render(self, lang: str) -> str:             # pragma: no cover - abstract
        raise NotImplementedError

    def __str__(self) -> str:
        return self.render(i18n.DEFAULT)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self!s})"

    def __add__(self, other) -> "J":
        return J([self, other])

    def __radd__(self, other) -> "J":
        return J([other, self])

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __eq__(self, other) -> bool:
        if isinstance(other, (Text, str)):
            return str(self) == str(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))

    def __len__(self) -> int:
        return len(str(self))

    def startswith(self, prefix) -> bool:
        return str(self).startswith(prefix)

    def __contains__(self, item) -> bool:
        return item in str(self)


def r(value, lang: str):
    """Render one parameter value."""
    if isinstance(value, Text):
        return value.render(lang)
    return value


class M(Text):
    """A message: template `key` from the locale files, formatted with params (rendered first)."""
    __slots__ = ("key", "params")

    def __init__(self, key: str, **params):
        self.key, self.params = key, params

    def render(self, lang: str) -> str:
        template, fell_back = i18n.lookup(lang, self.key)
        values = {k: r(v, lang) for k, v in self.params.items()}
        try:
            text = template.format(**values)
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"message {self.key!r} ({lang}) cannot be formatted with {sorted(values)}: {e}") from e
        return (EN_MARK + text) if fell_back and lang != i18n.DEFAULT else text


class J(Text):
    """Parts joined by `sep` (each part a Text or a plain string)."""
    __slots__ = ("parts", "sep")

    def __init__(self, parts, sep: str = ""):
        self.parts, self.sep = list(parts), sep

    def render(self, lang: str) -> str:
        sep = str(r(self.sep, lang))            # the separator may itself be a message (" and " / " or ")
        return sep.join(str(r(p, lang)) for p in self.parts if p is not None and p != "")


class G(Text):
    """A game-data name by internal id (base item, essence, fossil, currency) or a mod text by mod id."""
    __slots__ = ("kind", "id", "english")

    def __init__(self, kind: str, internal_id, english):
        self.kind, self.id, self.english = kind, internal_id, str(english)

    def render(self, lang: str) -> str:
        text, ok = i18n.game_text(self.kind, self.id, self.english, lang)
        return text if ok else EN_MARK + text


class CUR(Text):
    """A currency / item named in English (price keys, rules): looked up by id via its unique English name."""
    __slots__ = ("english",)

    def __init__(self, english):
        self.english = str(english)

    def render(self, lang: str) -> str:
        text, ok = i18n.currency_text(self.english, lang)
        return text if ok else EN_MARK + text


class N(Text):
    """A game name with no locale data yet (influence names, item classes, bench masters): English, marked."""
    __slots__ = ("english",)

    def __init__(self, english):
        self.english = str(english)

    def render(self, lang: str) -> str:
        return self.english if lang == i18n.DEFAULT else EN_MARK + self.english


class GL(Text):
    """A mod family label ('+# to maximum Life'), localized by the mod id it was made from."""
    __slots__ = ("mod_id", "label", "english_text")

    def __init__(self, mod_id, label, english_text):
        self.mod_id, self.label, self.english_text = mod_id, str(label), english_text

    def render(self, lang: str) -> str:
        text, ok = i18n.game_label(self.mod_id, self.label, self.english_text, lang)
        return text if ok else EN_MARK + text


class Cap(Text):
    """Another Text with its first letter upper-cased (after rendering, so it works in every language)."""
    __slots__ = ("inner",)

    def __init__(self, inner):
        self.inner = inner

    def render(self, lang: str) -> str:
        text = str(r(self.inner, lang))
        if text.startswith(EN_MARK):
            return EN_MARK + text[len(EN_MARK):][:1].upper() + text[len(EN_MARK) + 1:]
        return text[:1].upper() + text[1:]


class P(Text):
    """A probability: '12.3%', '0.678% (1 in 147)'."""
    __slots__ = ("p",)

    def __init__(self, p: float):
        self.p = p

    def render(self, lang: str) -> str:
        p = self.p
        if p <= 0:
            return "0%"
        if p >= 0.01:
            return f"{p * 100:.1f}%"
        return M("m.fmt.p_small", pct=f"{p * 100:.3g}", n=f"{1 / p:,.0f}").render(lang)


def keys(obj, out: set | None = None) -> set:
    """Every message key used anywhere in `obj` (a result, a list, a Text)."""
    out = set() if out is None else out
    if isinstance(obj, M):
        out.add(obj.key)
        for v in obj.params.values():
            keys(v, out)
    elif isinstance(obj, J):
        keys(obj.sep, out)
        for p in obj.parts:
            keys(p, out)
    elif isinstance(obj, Cap):
        keys(obj.inner, out)
    elif isinstance(obj, Text) and isinstance(getattr(obj, "key", None), str):
        out.add(obj.key)                        # other message-like texts (e.g. feature rows)
    elif isinstance(obj, dict):
        for v in obj.values():
            keys(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys(v, out)
    return out


def missing_keys(obj, lang: str) -> list[str]:
    """Message keys used in `obj` that have no template in `lang` (they render in English with the EN mark)."""
    if lang == i18n.DEFAULT:
        return []
    return sorted(k for k in keys(obj) if i18n.lookup(lang, k)[1] and not i18n.lookup(i18n.DEFAULT, k)[1])


def render(obj, lang: str = i18n.DEFAULT):
    """A result (dicts / lists / tuples of plain values and Text) with every Text rendered into `lang`."""
    if isinstance(obj, Text):
        return obj.render(lang)
    if isinstance(obj, dict):
        return {k: render(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, lang) for v in obj]
    if isinstance(obj, tuple):
        return tuple(render(v, lang) for v in obj)
    return obj
