"""Utility di testo condivise: normalizzazione e output su console.

`slugify` viveva duplicata identica in `fetcher.slugify_city` e in
`filters._slugify`: due copie della stessa regola (accenti, case, separatori)
che potevano divergere e far fallire silenziosamente i lookup dei filtri o la
costruzione degli URL. Qui c'e' l'unica implementazione.
"""
from __future__ import annotations

import re
import sys
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """'Reggio Emilia' -> 'reggio-emilia'; toglie accenti e caratteri speciali."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _NON_ALNUM.sub("-", text.lower().strip()).strip("-")


def console_utf8() -> None:
    """Fa parlare UTF-8 alla console di Windows (€, accenti, bordi di tabella).

    Senza, `print` di una tabella con i prezzi muore con UnicodeEncodeError sul
    cp1252 di default. `reconfigure` non esiste su tutti gli stream (pytest ne
    sostituisce di suoi), quindi il fallimento e' previsto e ignorato: l'output
    resta quello di prima, semplicemente senza i caratteri fuori tabella.
    """
    for stream in (sys.stdout, sys.stderr):
        riconfigura = getattr(stream, "reconfigure", None)
        if riconfigura is None:
            continue
        try:
            riconfigura(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def n_pagine(n: int) -> str:
    """«1 pagina», «3 pagine»: l'esito di uno scraping da una pagina sola (il
    caso dello smoke test) diceva «da 1 pagine»."""
    return "1 pagina" if n == 1 else f"{n} pagine"
