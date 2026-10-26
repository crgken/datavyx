"""Literal unit-label validation; no natural-language intent or phrase routing.

The model selects an explicit source token. The server verifies that token's
canonical currency code using CLDR names, or preserves an unnormalized unit.
This does not scan user utterances for currency keywords or guess missing units.
"""
from functools import lru_cache

from babel.numbers import get_currency_name, list_currencies

from .model import key


@lru_cache(maxsize=1)
def currency_labels():
    labels = {}
    for code in list_currencies():
        for label in [code, *(get_currency_name(code, locale=locale) for locale in ("zh_Hans", "en"))]:
            labels.setdefault(key(label), set()).add(code)
    return labels


def unit_label_matches(literal, canonical):
    if literal.strip() == canonical.strip():
        return True
    return currency_labels().get(key(literal)) == {canonical}
