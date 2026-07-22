"""Fase 1 — Filter Mapper.

Estrae il blocco `AVAILABLE_FILTERS` incorporato nell'HTML delle pagine di
ricerca di MioDottore e lo salva in `data/filters.json`. Costruisce dizionari
di lookup nome->id per prestazioni (services), malattie, assicurazioni, lingue.

Le specializzazioni su MioDottore non sono un filtro `AVAILABLE_FILTERS`: sono
uno *slug* nell'URL (es. `/podologo/milano`). Per quello raccogliamo una
mappatura osservata dagli attributi `data-eec-specialization-name` presenti
nelle card di risultato (vedi `parser.extract_specializations_seen`).

Questa tabella cambia raramente: rigenerarla ~1 volta al mese, non ad ogni run.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from .testo import slugify

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FILTERS_PATH = DATA_DIR / "filters.json"

# Gruppi di AVAILABLE_FILTERS che hanno items con {key, name} utili come lookup.
_LOOKUP_GROUPS = ("services", "diseases", "insurances", "languages")


_slugify = slugify  # alias storico: l'implementazione vive in testo.py


def extract_available_filters(html: str) -> List[dict]:
    """Estrae l'array JSON assegnato a `AVAILABLE_FILTERS:` nell'HTML.

    Usa un parser a bilanciamento di parentesi quadre invece di un regex,
    perche l'array e' grande e contiene stringhe con caratteri speciali.
    """
    marker = "AVAILABLE_FILTERS:"
    idx = html.find(marker)
    if idx == -1:
        raise ValueError("AVAILABLE_FILTERS non trovato nell'HTML (markup cambiato?)")
    start = html.index("[", idx)
    depth = 0
    end = None
    in_str = False
    escape = False
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        raise ValueError("Array AVAILABLE_FILTERS non bilanciato")
    return json.loads(html[start:end])


def build_lookups(filters: List[dict]) -> Dict[str, Dict[str, int]]:
    """Costruisce {gruppo: {slug_nome: id}} per i gruppi con items key/name."""
    lookups: Dict[str, Dict[str, int]] = {}
    for group in filters:
        name = group.get("name", "")
        if name not in _LOOKUP_GROUPS:
            continue
        mapping: Dict[str, int] = {}
        for item in group.get("items", []):
            key = item.get("key")
            label = item.get("name")
            if isinstance(key, int) and isinstance(label, str):
                mapping[_slugify(label)] = key
        if mapping:
            lookups[name] = mapping
    return lookups


def save_filters(html: str, path: Path = FILTERS_PATH) -> dict:
    """Estrae, costruisce i lookup e salva su disco. Ritorna il dict salvato."""
    filters = extract_available_filters(html)
    lookups = build_lookups(filters)
    payload = {
        "groups": [g.get("name") for g in filters],
        "lookups": lookups,
        "counts": {name: len(m) for name, m in lookups.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


@lru_cache(maxsize=4)
def _load_filters_cached(path: Path, mtime: float) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_filters(path: Path = FILTERS_PATH) -> Optional[dict]:
    """Legge filters.json con cache invalidata dall'mtime del file.

    Il file pesa ~78 KB e `service_id()` lo rileggeva e riparsava ad ogni
    singola chiamata; includere l'mtime nella chiave evita di servire dati
    stantii dopo una rigenerazione.
    """
    if not path.exists():
        return None
    return _load_filters_cached(path, path.stat().st_mtime)


def service_id(name: str, path: Path = FILTERS_PATH) -> Optional[int]:
    """Ritorna l'id di una prestazione dato il nome (fuzzy su slug)."""
    data = load_filters(path)
    if not data:
        return None
    services = data.get("lookups", {}).get("services", {})
    slug = _slugify(name)
    if slug in services:
        return services[slug]
    # match parziale: il nome cercato e' contenuto in una prestazione nota
    candidates = [v for k, v in services.items() if slug in k]
    return candidates[0] if len(candidates) == 1 else None


if __name__ == "__main__":
    # Rigenerazione manuale da un file HTML gia' scaricato:
    #   python -m medici.filters pagina_test.html
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else "pagina_test.html"
    html = Path(src).read_text(encoding="utf-8", errors="replace")
    payload = save_filters(html)
    print(f"Salvato {FILTERS_PATH}")
    print("Gruppi:", payload["groups"])
    print("Conteggi lookup:", payload["counts"])
