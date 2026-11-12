"""Exact unit tokens and unambiguous CLDR currency names; no text extraction."""
from functools import lru_cache
import unicodedata
from babel.numbers import get_currency_name, list_currencies


def label_key(value):
    return unicodedata.normalize('NFC', value.strip()).casefold()


@lru_cache(maxsize=1)
def currency_labels():
    labels = {}
    for code in list_currencies():
        names = [code]
        for locale in ('zh_Hans', 'zh_Hant', 'en'):
            names.extend(get_currency_name(code, count=count, locale=locale) for count in (None, 1, 2))
        for name in names:
            labels.setdefault(label_key(name), set()).add(code)
    return labels


def unit_label_matches(literal, unit):
    if label_key(literal) == label_key(unit):
        return True
    # Only a unique full currency name/code can support an ISO currency code.
    # Bare symbols and regional/language inference are deliberately excluded.
    return currency_labels().get(label_key(literal)) == {unit.strip().upper()}
