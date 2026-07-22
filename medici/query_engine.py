"""Fase 6 — Motore di query/ordinamento/filtro condiviso tra CLI e GUI.

La logica di ricerca e ordinamento vive QUI e basta: `cerca.py` e `app.py` la
richiamano, senza duplicare le regole. Include il calcolo distanza (haversine)
per l'ordinamento "vicino a".
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .storage import Storage

# Criteri di ordinamento supportati.
ORDINA = ("prezzo", "rating", "recensioni", "disponibilita", "distanza")


# Parole di servizio troppo generiche per discriminare: se restassero nel
# filtro, cercare "visita di controllo" escluderebbe "Visita podologica di
# controllo" solo perche' le parole non sono adiacenti.
_STOPWORD = {"di", "del", "dello", "della", "dei", "degli", "delle", "e", "ed",
             "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "a", "al",
             "alla", "ai", "per", "con", "in", "da", "su", "l"}


@dataclass
class Filtro:
    specializzazione: Optional[str] = None
    citta: Optional[str] = None
    servizio: Optional[str] = None            # match a parole sul nome servizio
    # Da quali portali pescare. None = tutti quelli presenti nel DB: e' il
    # comportamento giusto di default, perche' l'utente che ha scaricato da due
    # siti vuole confrontarli, non sceglierne uno.
    sorgenti: Optional[Sequence[str]] = None
    min_recensioni: int = 0
    min_rating: float = 0.0
    prezzo_max: Optional[float] = None
    prezzo_min: Optional[float] = None
    solo_con_prezzo: bool = False
    # Molti medici sono elencati con il listino ma senza agenda online: si
    # prenota solo per telefono. Il dato c'era gia' (`ha_calendario`), mancava
    # il modo di chiedere "mostrami solo chi posso prenotare da qui".
    solo_prenotabili: bool = False
    disponibile_entro_giorni: Optional[int] = None
    # Fonde in una sola riga lo stesso medico trovato su piu' portali (vedi
    # `medici/unifica.py`). Opzionale e spento di default: e' anche la via
    # d'uscita quando il clustering sbaglia, perche' spegnendolo si tornano a
    # vedere le righe grezze per fonte e si capisce cosa e' stato unito.
    unifica_duplicati: bool = False
    ordina: str = "prezzo"
    # INVERTE l'ordine *naturale* del criterio, non "sempre discendente":
    # prezzo/distanza/disponibilita salgono (piu' economico/vicino/prima),
    # rating/recensioni scendono (migliore prima). Con True si ribalta.
    discendente: bool = False
    limite: Optional[int] = None
    # per ordinamento distanza:
    vicino_lat: Optional[float] = None
    vicino_lng: Optional[float] = None


def avvisi(f: Filtro, storage: Optional[Storage] = None) -> List[str]:
    """Cosa, in questo filtro, non puo' funzionare — spiegato all'utente.

    Sono i casi che senza avviso si manifestano come «nessun risultato» o come
    un ordinamento apparentemente casuale: la query e' corretta, e' un dato o un
    parametro a mancare. La regola sta qui e non nelle due interfacce perche' e'
    una sola, e perche' e' proprio il genere di controllo che, duplicato, viene
    aggiornato da una parte e non dall'altra.
    """
    fuori: List[str] = []
    if f.ordina == "distanza" and (f.vicino_lat is None or f.vicino_lng is None):
        fuori.append(
            "Ordinamento per distanza senza un punto di riferimento: indica "
            "«vicino a» nella forma lat,lng (es. 45.4642,9.1900). Senza, "
            "l'ordine resta quello di partenza e la colonna distanza è vuota.")
    if storage is not None and (f.disponibile_entro_giorni is not None
                                or f.ordina == "disponibilita"):
        if storage.count_disponibilita() == 0:
            fuori.append(
                "Nel database non c'è nessuna disponibilità: va raccolta "
                "durante lo scraping (opzione «disponibilità»). Finché manca, "
                "filtrare o ordinare per disponibilità non trova nulla.")
    return fuori


def haversine_km(lat1, lng1, lat2, lng2) -> Optional[float]:
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def termini_servizio(testo: str) -> List[str]:
    """Parole significative di una ricerca per servizio.

    Il sito nomina lo stesso servizio in modi diversi ("Visita podologica di
    controllo", "Visita di controllo patologica"): un LIKE sull'intera frase ne
    trova una sola. Spezzando in parole e richiedendole TUTTE (in qualunque
    ordine) entrambe le varianti rispondono a "visita di controllo".
    """
    parole = [p for p in re.split(r"[^0-9A-Za-zÀ-ÿ]+", testo.lower()) if p]
    utili = [p for p in parole if p not in _STOPWORD and len(p) > 1]
    # Se l'utente cerca solo articoli/preposizioni non resta nulla da filtrare:
    # meglio ricadere sulla frase intera che restituire tutto il DB.
    return utili or parole


def _build_where(f: Filtro) -> tuple[str, list]:
    clauses = []
    params: list = []
    if f.sorgenti is not None:
        # Lista vuota = "nessuna fonte selezionata", che deve dare zero risultati.
        # Trattarla come falsy (nessun filtro) faceva l'opposto: deselezionando
        # tutti i portali nella GUI comparivano di nuovo TUTTI i risultati.
        if not f.sorgenti:
            clauses.append("0")
        else:
            clauses.append("sorgente IN (%s)" % ",".join("?" * len(f.sorgenti)))
            params += list(f.sorgenti)
    if f.specializzazione:
        clauses.append("(query_specializzazione = ? OR specializzazione LIKE ?)")
        params += [f.specializzazione, f"%{f.specializzazione}%"]
    if f.citta:
        clauses.append("(query_citta = ? OR citta LIKE ?)")
        params += [f.citta, f"%{f.citta}%"]
    if f.servizio:
        # Una LIKE per parola, tutte in AND: l'ordine non conta e le parole in
        # mezzo ("podologica") non fanno fallire il match.
        for parola in termini_servizio(f.servizio):
            clauses.append("servizio LIKE ?")
            params.append(f"%{parola}%")
    if f.solo_prenotabili:
        clauses.append("ha_calendario = 1")
    if f.prezzo_max is not None:
        # `prezzo IS NOT NULL` esplicito: senza, chi non pubblica il prezzo
        # verrebbe escluso comunque dal confronto, ma in modo poco leggibile.
        clauses.append("prezzo IS NOT NULL AND prezzo <= ?")
        params.append(float(f.prezzo_max))
    if f.prezzo_min is not None:
        clauses.append("prezzo IS NOT NULL AND prezzo >= ?")
        params.append(float(f.prezzo_min))
    if f.min_recensioni:
        clauses.append("COALESCE(n_recensioni,0) >= ?")
        params.append(f.min_recensioni)
    if f.min_rating:
        clauses.append("COALESCE(rating,0) >= ?")
        params.append(f.min_rating)
    if f.solo_con_prezzo:
        # Ordinando per prezzo i NULL non spariscono: vanno in coda (vedi
        # `_ORDER_SQL` e `_sort`). Si escludono solo se richiesto qui.
        clauses.append("prezzo IS NOT NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _disponibilita_join(f: Filtro) -> tuple[str, str, list]:
    """Se richiesto 'disponibile entro N giorni', calcola la prima disponibilita."""
    if f.disponibile_entro_giorni is None and f.ordina != "disponibilita":
        return "", "", []
    # La correlazione e' su TRE colonne, nessuna ridondante:
    # - `sorgente`: `address_id` e' interno al portale, senza questo vincolo gli
    #   slot di un sito finirebbero al medico omonimo-per-id di un altro;
    # - `result_id`: dove l'`address_id` identifica la sede e non la coppia
    #   medico-sede (il `practiceId` di Doctolib), tutti i medici di un
    #   poliambulatorio si vedrebbero attribuita la disponibilita' del collega
    #   che si libera prima.
    _CORR = ("d.sorgente = risultati.sorgente "
             "AND d.result_id = risultati.result_id "
             "AND d.address_id = risultati.address_id ")
    select_extra = (
        ", (SELECT MIN(d.data || ' ' || d.orario) FROM disponibilita d "
        f"   WHERE {_CORR} AND d.prenotabile = 1 "
        "   AND d.data >= date('now')) AS prima_disponibilita"
    )
    having = ""
    params: list = []
    if f.disponibile_entro_giorni is not None:
        having = (
            " AND EXISTS (SELECT 1 FROM disponibilita d WHERE "
            f"{_CORR} AND d.prenotabile = 1 "
            "AND d.data BETWEEN date('now') AND date('now', ?))"
        )
        params.append(f"+{int(f.disponibile_entro_giorni)} day")
    return select_extra, having, params


# A parita' di criterio l'ordine era quello di inserimento, cioe' casuale per
# chi legge: le due sedi dello stesso medico finivano lontane senza motivo.
# Nome e indirizzo come spareggio le rendono adiacenti; chi ha prezzi diversi
# resta separato, perche' e' proprio l'informazione che si sta cercando.
_SPAREGGIO = "nome, indirizzo"

# ORDER BY equivalente all'ordinamento Python, per farlo fare a SQLite.
# `... IS NULL` per primo tiene i valori mancanti in coda in entrambi i versi.
_ORDER_SQL = {
    "prezzo": "prezzo IS NULL, prezzo {dir}",
    "rating": "COALESCE(rating,0) {dir}",
    "recensioni": "COALESCE(n_recensioni,0) {dir}",
    "disponibilita": "prima_disponibilita IS NULL, prima_disponibilita {dir}",
}
# Verso naturale di ogni criterio: prezzo/disponibilita crescente (piu'
# economico / prima disponibile), rating e recensioni decrescente (migliore).
_ORDER_NATURALE = {"prezzo": "ASC", "rating": "DESC",
                   "recensioni": "DESC", "disponibilita": "ASC"}


def search(storage: Storage, f: Filtro) -> List[dict]:
    if f.ordina not in ORDINA:
        raise ValueError(f"ordina={f.ordina!r} non valido, attesi: {', '.join(ORDINA)}")

    select_extra, avail_having, avail_params = _disponibilita_join(f)
    where, where_params = _build_where(f)
    if avail_having:
        where = where + avail_having if where else " WHERE 1=1" + avail_having

    # La distanza si calcola in Python (SQLite non ha haversine): in quel caso
    # servono tutte le righe. Per gli altri criteri lasciamo ordinare e limitare
    # a SQLite, che usa gli indici invece di materializzare l'intera tabella.
    #
    # L'unificazione impone la stessa rinuncia, e per un motivo piu' stringente:
    # un LIMIT in SQL taglierebbe *prima* di fondere i duplicati, quindi la
    # prima pagina di risultati potrebbe non contenere entrambe le righe di uno
    # stesso medico — e il «prezzo minimo tra i portali» sarebbe calcolato su
    # meta' dei dati.
    in_python = f.ordina == "distanza" or f.unifica_duplicati
    order_sql = limit_sql = ""
    if not in_python:
        verso = _ORDER_NATURALE[f.ordina]
        if f.discendente:
            verso = "ASC" if verso == "DESC" else "DESC"
        order_sql = (" ORDER BY " + _ORDER_SQL[f.ordina].format(dir=verso)
                     + ", " + _SPAREGGIO)
        if f.limite:
            limit_sql = f" LIMIT {int(f.limite)}"

    sql = f"SELECT risultati.*{select_extra} FROM risultati{where}{order_sql}{limit_sql}"
    results = [dict(r) for r in storage.query(sql, where_params + avail_params)]

    if f.unifica_duplicati:
        # Import locale: `unifica` importa `haversine_km` da qui, e a livello di
        # modulo i due si aspetterebbero a vicenda.
        from .unifica import unifica as _unifica
        results = _unifica(results)

    if f.ordina == "distanza" or (f.vicino_lat is not None and f.vicino_lng is not None):
        for r in results:
            r["distanza_km"] = haversine_km(
                f.vicino_lat, f.vicino_lng, r.get("lat"), r.get("lng"))

    if in_python:
        # Lo stesso spareggio dell'ORDER BY: `sorted` e' stabile, quindi basta
        # pre-ordinare qui e il criterio principale lo conserva.
        results.sort(key=lambda r: ((r.get("nome") or ""), (r.get("indirizzo") or "")))
        results = _sort(results, f)
        if f.limite:
            results = results[: f.limite]
    return results


def _sort(results: List[dict], f: Filtro) -> List[dict]:
    reverse = f.discendente
    if f.ordina == "prezzo":
        # NULL sempre in coda (chiave 1 = manca il prezzo), poi per valore.
        # Con discendente=True invertiamo solo il segno del valore, non i NULL.
        sign = -1 if reverse else 1

        def chiave_prezzo(r: dict) -> tuple:
            p = r.get("prezzo")
            return (p is None, sign * float(p or 0.0))

        return sorted(results, key=chiave_prezzo)
    if f.ordina == "rating":
        return sorted(results, key=lambda r: (r.get("rating") or 0), reverse=not reverse)
    if f.ordina == "recensioni":
        return sorted(results, key=lambda r: (r.get("n_recensioni") or 0), reverse=not reverse)
    if f.ordina == "disponibilita":
        return _sort_null_last(results, "prima_disponibilita", reverse)
    if f.ordina == "distanza":
        return _sort_null_last(results, "distanza_km", reverse)
    return results


def _sort_null_last(results: List[dict], campo: str, reverse: bool) -> List[dict]:
    """Ordina per `campo` tenendo i valori mancanti in coda in ENTRAMBI i versi.

    `sorted(..., reverse=True)` da solo porterebbe i None in testa; qui
    separiamo le due liste. Prima `discendente` veniva proprio ignorato per
    disponibilita e distanza, quindi il flag non aveva alcun effetto."""
    con = [r for r in results if r.get(campo) is not None]
    senza = [r for r in results if r.get(campo) is None]
    con.sort(key=lambda r: r[campo], reverse=reverse)
    return con + senza


def stats(results: Sequence[dict]) -> dict:
    """Statistiche utili da mostrare (es. quanti hanno un prezzo)."""
    tot = len(results)
    con_prezzo = sum(1 for r in results if r.get("prezzo") is not None)
    return {"totale": tot, "con_prezzo": con_prezzo,
            "senza_prezzo": tot - con_prezzo}
