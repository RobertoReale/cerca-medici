#!/usr/bin/env python3
"""cerca.py — CLI di MioDottore scraper.

Due modalita':
  1. Scraping:   scarica anagrafica/prezzi (e opzionalmente disponibilita).
  2. Consultazione: interroga il DB locale con ordinamento/filtri.

Esempi:
  # Scrape (popola il DB)
  python cerca.py scrape --specializzazione podologo --citta Milano
  python cerca.py scrape --specializzazione podologo --citta Milano --disponibilita

  # Consulta (legge dal DB)
  python cerca.py cerca --specializzazione podologo --citta Milano --ordina prezzo --limite 20
  python cerca.py cerca --specializzazione podologo --citta Milano --ordina rating --min-recensioni 50
  python cerca.py cerca --specializzazione podologo --citta Milano --disponibile-entro 3 --ordina prezzo
  python cerca.py cerca --specializzazione podologo --citta Milano --ordina distanza --vicino-a 45.4642,9.1900
  python cerca.py cerca ... --export risultati.csv
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from tabulate import tabulate

from medici.storage import Storage
from medici.query_engine import Filtro, avvisi, search, stats
from medici.scraper import risolvi_sorgente, scrape_search
from medici.sorgenti import elenco as sorgenti_disponibili, etichetta_di
from medici.testo import console_utf8


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --- comando scrape ---------------------------------------------------------

def _progress_console(messaggio: str, frazione=None) -> None:
    """Avanzamento su una riga sola, riscritta in loco (stderr, non inquina i pipe).

    Senza, `scrape` restava muto per minuti e non si distingueva un download in
    corso da un blocco.
    """
    pct = f" [{frazione * 100:3.0f}%]" if frazione is not None else ""
    print(f"\r\033[K{messaggio}{pct}", end="", file=sys.stderr, flush=True)


def _fonti_richieste(args) -> list:
    """Sorgenti su cui operare. `--fonte tutte` = tutte quelle registrate."""
    if "tutte" in args.fonte:
        return sorgenti_disponibili()
    # dict.fromkeys: dedup mantenendo l'ordine in cui l'utente le ha scritte.
    return list(dict.fromkeys(args.fonte))


def cmd_scrape(args) -> int:
    storage = Storage(args.db)
    fonti = _fonti_richieste(args)

    # Ctrl-C: invece di un traceback a meta' pagina, alziamo un flag e lasciamo
    # che lo scraper chiuda il giro e riporti quanto ha gia' salvato.
    fermato = []
    try:
        precedente = signal.signal(signal.SIGINT,
                                   lambda *_: fermato.append(True))
    except ValueError:      # non siamo nel thread principale
        precedente = None
    reports = []
    try:
        for nome in fonti:
            if fermato:
                break
            src = risolvi_sorgente(nome)
            print(f"[{src.etichetta}] Cerco su: "
                  f"{src.url_ricerca(args.specializzazione, args.citta)}",
                  file=sys.stderr)
            reports.append(scrape_search(
                args.specializzazione, args.citta,
                sorgente=src,
                storage=storage,
                max_pagine=args.max_pagine,
                disponibilita=args.disponibilita,
                giorni_disponibilita=args.giorni,
                # Con --verbose il log gia' racconta tutto: evitiamo che le due
                # cose si sovrascrivano a vicenda sulla stessa riga.
                progress=None if args.verbose else _progress_console,
                annullato=lambda: bool(fermato),
            ))
    finally:
        if precedente is not None:
            signal.signal(signal.SIGINT, precedente)
        storage.close()
        if not args.verbose:
            print("\r\033[K", end="", file=sys.stderr, flush=True)

    # Con piu' fonti il risultato e' misto: una puo' bloccarsi e l'altra
    # riuscire. Riportiamo riga per riga, e il codice di uscita descrive il
    # caso peggiore solo se NESSUNA fonte ha portato dati.
    for rep in reports:
        etichetta = etichetta_di(rep.sorgente)
        if rep.annullato:
            print(f"[{etichetta}] Interrotto: {rep.righe} righe salvate da "
                  f"{rep.pagine} pagine.", file=sys.stderr)
        elif rep.bloccato:
            print(f"[{etichetta}] BLOCCATO: {rep.errore}", file=sys.stderr)
        elif rep.errore:
            # La sorgente sa gia' perche' non ha prodotto nulla (p.es. la
            # specializzazione non esiste su quel portale, con i suggerimenti):
            # ripetere il generico "0 righe" nasconderebbe proprio la risposta.
            print(f"[{etichetta}] {rep.errore}", file=sys.stderr)
            if rep.righe:
                # Una raccolta fermata a meta' ha comunque salvato qualcosa:
                # senza questa riga sembrerebbe che il run non abbia dato nulla.
                print(f"[{etichetta}] {rep.righe} righe ({rep.nuove} nuove) da "
                      f"{rep.pagine} pagine sono comunque nel database.")
        elif rep.righe == 0:
            print(f"[{etichetta}] ATTENZIONE: 0 righe estratte. Città non "
                  f"riconosciuta o formato cambiato? Controlla con --verbose.",
                  file=sys.stderr)
        else:
            msg = (f"[{etichetta}] OK: {rep.righe} righe ({rep.nuove} nuove) da "
                   f"{rep.pagine} pagine per {rep.specializzazione}/{rep.citta}")
            if args.disponibilita:
                msg += f", {rep.slot} slot di disponibilità"
            print(msg)

    if any(r.righe for r in reports):
        return 0
    if any(r.annullato for r in reports):
        return 130
    if any(r.bloccato for r in reports):
        return 2
    return 1


# --- comando cerca ----------------------------------------------------------

def _parse_vicino_a(val):
    if not val:
        return None, None
    try:
        lat, lng = val.split(",")
        return float(lat), float(lng)
    except ValueError:
        print("--vicino-a deve essere 'lat,lng' (es. 45.4642,9.1900)", file=sys.stderr)
        sys.exit(2)


def _fmt_prezzo(r):
    if r.get("prezzo") is None:
        return "n/d"
    p = f"{r['prezzo']:.0f} €"
    return ("da " + p) if r.get("prezzo_e_da_min") else p


def cmd_cerca(args) -> int:
    vicino_lat, vicino_lng = _parse_vicino_a(args.vicino_a)
    # Nella consultazione "tutte" significa "non filtrare per sorgente": lascia
    # passare anche i dati di portali non piu' registrati ma ancora nel DB.
    fonti = None if "tutte" in args.fonte else _fonti_richieste(args)
    f = Filtro(
        specializzazione=args.specializzazione,
        citta=args.citta,
        servizio=args.servizio,
        sorgenti=fonti,
        min_recensioni=args.min_recensioni,
        min_rating=args.min_rating,
        prezzo_max=args.prezzo_max,
        prezzo_min=args.prezzo_min,
        solo_con_prezzo=args.solo_con_prezzo,
        solo_prenotabili=args.solo_prenotabili,
        disponibile_entro_giorni=args.disponibile_entro,
        unifica_duplicati=args.unifica,
        ordina=args.ordina,
        discendente=args.desc,
        limite=args.limite,
        vicino_lat=vicino_lat,
        vicino_lng=vicino_lng,
    )
    with Storage(args.db) as st:
        if st.count_risultati() == 0:
            print("DB vuoto: esegui prima 'python cerca.py scrape ...'", file=sys.stderr)
            return 1
        # Prima dei risultati: un filtro che non puo' funzionare va detto adesso,
        # non lasciato dedurre da un elenco vuoto (vedi `query_engine.avvisi`).
        for avviso in avvisi(f, st):
            print(f"ATTENZIONE: {avviso}", file=sys.stderr)
        results = search(st, f)

    if not results:
        print("Nessun risultato con questi filtri.")
        return 0

    s = stats(results)
    # La colonna Fonte compare solo quando serve davvero a distinguere: con un
    # solo portale nel risultato sarebbe una colonna costante che ruba spazio.
    # Con `--unifica` la sostituisce «Disponibile su», che dice di piu'.
    piu_fonti = not args.unifica and len({r.get("sorgente") for r in results}) > 1
    # Colonne che variano leggermente in base al criterio di ordinamento.
    rows = []
    for r in results:
        row = {
            **({"Fonte": etichetta_di(r.get("sorgente") or "")} if piu_fonti else {}),
            "Nome": (r.get("nome") or "")[:32],
            "Indirizzo": (r.get("indirizzo") or "")[:28],
            "Città": r.get("citta") or "",
            "Servizio": (r.get("servizio") or "")[:26],
            "Prezzo": _fmt_prezzo(r),
            "Rating": r.get("rating") if r.get("rating") is not None else "",
            "Rec.": r.get("n_recensioni") or 0,
        }
        if args.unifica:
            row["Disponibile su"] = r.get("disponibile_su") or ""
        if args.ordina == "disponibilita" or args.disponibile_entro:
            row = {"1a disp.": r.get("prima_disponibilita") or "—", **row}
        if args.ordina == "distanza" and r.get("distanza_km") is not None:
            # Con l'unita' dentro la cella: `tabulate` riconosce "8.0" come
            # numero e lo ristampa come "8", buttando via il decimale — che a
            # distanze urbane e' proprio la parte che distingue due studi.
            row["Distanza"] = f"{r['distanza_km']:.1f} km"
        rows.append(row)

    print(tabulate(rows, headers="keys", tablefmt="simple_outline"))
    # Forma neutra: «1 su 1 risultati hanno» non si puo' leggere.
    print(f"\nPrezzo pubblicato: {s['con_prezzo']} risultati su {s['totale']} "
          f"(ordinamento: {args.ordina}).")
    if args.unifica:
        uniti = sum(1 for r in results if (r.get("n_fonti") or 1) > 1)
        print(f"{uniti} " + ("risultato è" if uniti == 1 else "risultati sono")
              + " lo stesso medico su più portali. Senza --unifica si vedono "
                "le righe grezze, una per fonte.")

    if args.export:
        # Un solo punto di verita' per formato (CSV con BOM per Excel, o
        # JSONL): qui c'era una seconda implementazione che poteva divergere.
        # L'estensione sceglie il formato: .jsonl/.ndjson -> JSON Lines,
        # altrimenti CSV.
        if args.export.lower().endswith((".jsonl", ".ndjson")):
            n = Storage.export_rows_jsonl(results, args.export)
        else:
            n = Storage.export_rows_csv(results, args.export)
        print(f"Esportato: {args.export} ({n} righe)")
    return 0


def _positivo(val: str) -> int:
    """Intero >= 1. Con 0 o un negativo `--max-pagine` non scaricherebbe nulla e
    `--giorni` chiederebbe una finestra che finisce prima di iniziare: entrambi
    esiti silenziosi, indistinguibili da «il sito non ha risposto»."""
    n = int(val)
    if n < 1:
        raise argparse.ArgumentTypeError(f"deve essere almeno 1, non {n}")
    return n


def _aggiungi_fonte(parser, aiuto: str) -> None:
    """Opzione `--fonte`, con le scelte lette dal registro delle sorgenti.

    Cosi' registrare un nuovo portale lo fa comparire nella CLI senza toccare
    questo file.
    """
    scelte = sorgenti_disponibili() + ["tutte"]
    parser.add_argument("--fonte", choices=scelte, nargs="+",
                        default=["tutte"], metavar="FONTE",
                        help=f"{aiuto} Valori: {', '.join(scelte)} "
                             f"(default: tutte). Ripetibile: --fonte a b")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ricerca medici con prezzi e disponibilità su più portali")
    p.add_argument("--db", default=None,
                   help="Percorso DB SQLite (default: data/medici.db, con "
                        "fallback sul vecchio data/miodottore.db se presente)")
    p.add_argument("--verbose", "-v", action="store_true", help="Log dettagliato")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="Scarica i dati nel DB locale")
    sc.add_argument("--specializzazione", required=True)
    sc.add_argument("--citta", required=True)
    _aggiungi_fonte(sc, "Portali da interrogare.")
    sc.add_argument("--max-pagine", type=_positivo, default=40)
    sc.add_argument("--disponibilita", action="store_true",
                    help="Raccogli anche gli slot orari (endpoint /api/v3 — vedi README)")
    sc.add_argument("--giorni", type=_positivo, default=5,
                    help="Giorni di disponibilità da raccogliere")
    sc.set_defaults(func=cmd_scrape)

    ce = sub.add_parser("cerca", help="Interroga il DB locale")
    ce.add_argument("--specializzazione")
    ce.add_argument("--citta")
    _aggiungi_fonte(ce, "Da quali portali mostrare i risultati già scaricati.")
    ce.add_argument("--servizio",
                    help="Parole chiave del servizio, in qualsiasi ordine: "
                         "'visita controllo' trova anche 'Visita podologica di "
                         "controllo'")
    ce.add_argument("--prezzo-max", type=float, default=None,
                    help="Spesa massima in € (esclude chi non pubblica il prezzo)")
    ce.add_argument("--prezzo-min", type=float, default=None)
    ce.add_argument("--ordina", choices=["prezzo", "rating", "recensioni", "disponibilita", "distanza"],
                    default="prezzo")
    ce.add_argument("--desc", "--inverti", dest="desc", action="store_true",
                    help="Inverte l'ordine naturale del criterio (prezzo: dal piu' "
                         "caro; rating: dal peggiore)")
    ce.add_argument("--min-recensioni", type=int, default=0)
    ce.add_argument("--min-rating", type=float, default=0.0)
    ce.add_argument("--solo-con-prezzo", action="store_true",
                    help="Escludi chi non ha prezzo pubblicato")
    ce.add_argument("--solo-prenotabili", action="store_true",
                    help="Solo chi ha l'agenda online attiva (gli altri si "
                         "prenotano per telefono)")
    ce.add_argument("--disponibile-entro", type=int, default=None,
                    help="Solo chi ha uno slot entro N giorni")
    ce.add_argument("--unifica", action="store_true",
                    help="Fondi in una riga sola lo stesso medico trovato su "
                         "più portali: il prezzo mostrato è il minimo, la "
                         "colonna «Disponibile su» li elenca tutti")
    ce.add_argument("--vicino-a", default=None, help="lat,lng per ordinare per distanza")
    ce.add_argument("--limite", type=int, default=None)
    ce.add_argument("--export", default=None,
                    help="Esporta i risultati: .csv per CSV (default), "
                         ".jsonl/.ndjson per JSON Lines")
    ce.set_defaults(func=cmd_cerca)
    return p


def main(argv=None) -> int:
    # Console Windows: forza UTF-8 per €, accenti e caratteri di tabella.
    console_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
