"""Test di regressione del parser prezzo (il punto piu' fragile) e del parsing card.

Eseguire con: python -m pytest tests/ -q   (oppure python tests/test_parser.py)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from medici.parser import parse_prezzo, parse_search_html, count_cards  # noqa: E402


# --- parse_prezzo: i 4 formati del piano + range + non-breaking space -------

def test_prezzo_fisso():
    p = parse_prezzo("50 €")
    assert p.valore == 50.0 and p.e_da_min is False


def test_prezzo_nbsp():
    # separatore non-breaking space, come nell'HTML reale
    p = parse_prezzo("90\xa0€")
    assert p.valore == 90.0 and p.e_da_min is False


def test_prezzo_da_min():
    p = parse_prezzo("da 120 €")
    assert p.valore == 120.0 and p.e_da_min is True


def test_prezzo_da_min_nbsp():
    p = parse_prezzo("da 30\xa0€")
    assert p.valore == 30.0 and p.e_da_min is True


def test_prezzo_non_disponibile():
    p = parse_prezzo("Prezzo non disponibile")
    assert p.valore is None


def test_prezzo_gratuito():
    p = parse_prezzo("Prestazione gratuita")
    assert p.valore == 0.0


def test_prezzo_range():
    p = parse_prezzo("50-90 €")
    assert p.valore == 50.0 and p.e_da_min is True


def test_prezzo_migliaia():
    p = parse_prezzo("1.200 €")
    assert p.valore == 1200.0


def test_prezzo_vuoto():
    assert parse_prezzo("").valore is None
    assert parse_prezzo(None).valore is None


# --- parsing su HTML reale (se disponibile) ---------------------------------

def test_parse_su_pagina_reale():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pagina_test.html")
    if not os.path.exists(path):
        return  # smoke opzionale, salta se non c'è la pagina scaricata
    html = open(path, encoding="utf-8").read()
    assert count_cards(html) > 0
    righe = parse_search_html(html)
    assert len(righe) >= count_cards(html)  # >=1 riga (indirizzo) per card
    # almeno una riga con prezzo numerico e una con nome
    assert any(r.prezzo is not None for r in righe)
    assert all(r.nome for r in righe)


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            ok += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{ok}/{len(funcs)} test passati")
    sys.exit(0 if ok == len(funcs) else 1)
