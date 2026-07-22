"""Utility di normalizzazione testo condivise.

`slugify` viveva duplicata identica in `fetcher.slugify_city` e in
`filters._slugify`: due copie della stessa regola (accenti, case, separatori)
che potevano divergere e far fallire silenziosamente i lookup dei filtri o la
costruzione degli URL. Qui c'e' l'unica implementazione.
"""
from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """'Reggio Emilia' -> 'reggio-emilia'; toglie accenti e caratteri speciali."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _NON_ALNUM.sub("-", text.lower().strip()).strip("-")
