"""Fase 2 — da «podologo a Milano» a qualcosa che Doctolib capisce.

Il problema e' che i due portali non chiamano le cose allo stesso modo:
`podologo` su Doctolib non esiste, `dermatologo` si chiama
`dermatologo-venereologo`, e la citta' non e' una stringa ma un oggetto `place`
che vive solo dentro una landing page.

Chi dei due e' sbagliato lo dice gia' il client
-----------------------------------------------
Il piano prevedeva qui un trucco: ritentare la citta' con una specializzazione
che esiste ovunque (`medico-di-medicina-generale`) per capire quale dei due
pezzi facesse fallire la landing. Sul campo si e' scoperto che **non serve**,
perche' Doctolib non risponde 404: redirige, e la destinazione del redirect dice
gia' tutto (`/cardiologo` = citta' ignota, `/` = specializzazione ignota — vedi
la tabella in `client.contesto_ricerca`). `LandingNonTrovata.cosa` porta quella
diagnosi fin qui, e la sonda resta solo sul percorso d'errore, per distinguere
«questa specializzazione non esiste» da «il sito si comporta in modo strano».

Il trucco del piano avrebbe funzionato lo stesso, ma sarebbe costato una
richiesta in piu' *a ogni* ricerca sbagliata e avrebbe dato una diagnosi
indiretta dove ce n'e' una diretta.

Specializzazione
----------------
1. `slugify` dell'input e prova diretta.
2. Se non e' riconosciuta, si cerca nella cache locale
   `data/doctolib_specialita.json` (i 130 slug ufficiali del sitemap) con un
   match a parole: `dermatologo` -> `dermatologo-venereologo`.
3. Se ancora niente, si fallisce **rumorosamente** elencando gli slug piu'
   simili. Mai restituire 0 risultati in silenzio: e' la stessa lezione dello
   smoke test dello scraper.

Citta'
------
Non serve un endpoint di autocomplete delle localita' (che non e' stato
individuato: `/patient-health-search/api/v1/places` e `/location/autocomplete`
rispondono entrambi 404). La landing stessa fa da validatore, e in piu'
restituisce la forma canonica: «reggio nell'emilia» diventa
`provincia-di-reggio-emilia` senza che noi si debba sapere come.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence

from ..fetcher import FetchError
from ..testo import slugify
from .client import (BASE_URL, ClientDoctolib, ClientLanding, ContestoDoctolib,
                     LandingNonTrovata)

log = logging.getLogger("medici.doctolib.slug")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SPECIALITA_PATH = DATA_DIR / "doctolib_specialita.json"
SITEMAP_URL = f"{BASE_URL}/sitemap_specialities/1.xml"

# Esiste in ogni citta' in cui Doctolib opera: e' il perno del trucco descritto
# sopra per capire se a mancare e' la citta' o la specializzazione.
SPEC_DI_RIFERIMENTO = "medico-di-medicina-generale"

# Parole che compaiono in troppi slug per discriminare qualcosa.
_PAROLE_DEBOLI = {"medico", "chirurgo", "di", "e", "del", "della"}


class SlugNonRisolto(ValueError):
    """La ricerca non e' traducibile in una URL Doctolib.

    Porta `alternative` per poterle mostrare in modo diverso a seconda del
    contesto (CLI, GUI) senza dover ri-parsare il messaggio.
    """

    def __init__(self, messaggio: str, alternative: Sequence[str] = ()):
        super().__init__(messaggio)
        self.alternative = list(alternative)


# --- cache delle specializzazioni -------------------------------------------

def carica_specialita(path: Optional[Path] = None) -> List[str]:
    """Gli slug noti. Lista vuota se la cache non c'e' ancora: la risoluzione
    diretta continua a funzionare, si perde solo il suggerimento."""
    path = Path(path or SPECIALITA_PATH)
    if not path.exists():
        log.info("Cache specializzazioni assente (%s): rigenerala con "
                 "`python -m medici.doctolib.slug --aggiorna`.", path)
        return []
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("Cache specializzazioni illeggibile (%s): la ignoro.", e)
        return []
    if isinstance(dati, dict):           # formato {"slug": [...]}
        dati = dati.get("slug", [])
    return [s for s in dati if isinstance(s, str)]


def slug_dal_sitemap(xml: str) -> List[str]:
    """Estrae gli slug da `sitemap_specialities/1.xml`.

    Regex e non un parser XML perche' il documento e' piatto e regolare: una sola
    `<loc>` per `<url>`, nessun annidamento in cui perdersi.
    """
    return [m for m in re.findall(
        r"<loc>\s*" + re.escape(BASE_URL) + r"/([^<\s/]+)\s*</loc>", xml)]


def aggiorna_specialita(client: Optional[ClientDoctolib] = None,
                        path: Optional[Path] = None) -> List[str]:
    """Riscarica il sitemap e riscrive la cache. Da rifare quando gli slug
    cambiano — con la stessa cadenza (~mensile) di `data/filters.json`."""
    client = client or ClientDoctolib()
    path = Path(path or SPECIALITA_PATH)
    xml = client.fetcher.get(SITEMAP_URL).html
    slug = slug_dal_sitemap(xml)
    if not slug:
        raise SlugNonRisolto(f"Nessuno slug estratto da {SITEMAP_URL}: "
                             "formato del sitemap cambiato?")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(slug), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    log.info("Salvati %d slug in %s", len(slug), path)
    return slug


# --- match ------------------------------------------------------------------

def _parole(slug: str) -> set:
    return {p for p in slug.split("-") if p}


def candidati(voluto: str, noti: Sequence[str]) -> List[str]:
    """Slug plausibili per `voluto`, dal piu' al meno probabile.

    Due criteri, in quest'ordine:
    1. **contenimento a parole** — `dermatologo` sta dentro
       `dermatologo-venereologo`: e' il caso reale piu' frequente, ed e' una
       corrispondenza esatta, non una somiglianza;
    2. **somiglianza ortografica** (`difflib`) — recupera i refusi.

    Le parole troppo comuni (`medico`, `chirurgo`) sono escluse dal criterio 1,
    altrimenti «medico dello sport» pescherebbe mezzo elenco.
    """
    voluto = slugify(voluto)
    if not voluto:
        return []
    parole_utili = _parole(voluto) - _PAROLE_DEBOLI
    per_parola = [s for s in noti
                  if parole_utili and parole_utili <= _parole(s)]
    per_parola.sort(key=len)   # il piu' corto e' il piu' specifico
    simili = [s for s in difflib.get_close_matches(voluto, noti, n=5, cutoff=0.6)
              if s not in per_parola]
    return per_parola + simili


# --- risoluzione ------------------------------------------------------------

def risolvi(specializzazione: str, citta: str, *,
            client: Optional[ClientLanding] = None,
            noti: Optional[Sequence[str]] = None) -> ContestoDoctolib:
    """Contesto pronto per l'API, oppure `SlugNonRisolto` con le alternative.

    Costa **una** richiesta nel caso normale e due o tre quando lo slug va
    corretto: la landing di ripiego serve anche da sonda per la citta', quindi
    non c'e' una richiesta dedicata solo a quella diagnosi.
    """
    client = client or ClientDoctolib()
    citta_slug = slugify(citta)
    spec_slug = slugify(specializzazione)
    if not spec_slug or not citta_slug:
        raise SlugNonRisolto(
            f"Ricerca incompleta: specializzazione={specializzazione!r}, "
            f"citta={citta!r}")

    def _citta_ignota() -> SlugNonRisolto:
        # Fermarsi e' l'unica cosa corretta: la pagina su cui Doctolib ci ha
        # rimandato contiene medici veri, di Milano, che finirebbero nel DB
        # etichettati con la citta' chiesta dall'utente.
        return SlugNonRisolto(
            f"Doctolib non conosce la citta' {citta!r} (slug {citta_slug!r}) e "
            f"reindirizza a una ricerca nazionale. Riprova con il nome del "
            f"comune per esteso o con il capoluogo piu' vicino.")

    try:
        return client.contesto_ricerca(spec_slug, citta_slug)
    except LandingNonTrovata as e:
        if e.cosa == "citta":
            raise _citta_ignota() from None
        log.info("/%s/%s non riconosciuta (%s): cerco lo slug giusto.",
                 spec_slug, citta_slug, e.cosa)

    # Resta il caso "specializzazione" (o l'indecidibile "pagina"): proviamo le
    # alternative dall'elenco ufficiale.
    alternative = candidati(spec_slug, list(noti) if noti is not None
                            else carica_specialita())
    for alt in alternative:
        try:
            ctx = client.contesto_ricerca(alt, citta_slug)
        except LandingNonTrovata as e:
            if e.cosa == "citta":
                # Con `alt` la specializzazione ha risolto: il pezzo che manca
                # e' la citta'. Le alternative rimanenti fallirebbero tutte
                # allo stesso modo, e il messaggio d'errore finale darebbe la
                # colpa alla specializzazione, che invece era giusta.
                raise _citta_ignota() from None
            continue
        log.info("Specializzazione %r risolta in %r su Doctolib.",
                 specializzazione, alt)
        return ctx

    # Nessuna alternativa ha funzionato. Prima di dare la colpa all'utente,
    # una sonda con una specializzazione che esiste ovunque: se fallisce anche
    # quella, il problema non e' cio' che ha scritto.
    try:
        client.contesto_ricerca(SPEC_DI_RIFERIMENTO, citta_slug)
    except (LandingNonTrovata, FetchError):
        raise SlugNonRisolto(
            f"Doctolib non risponde come previsto nemmeno per "
            f"{SPEC_DI_RIFERIMENTO!r} a {citta!r}: non e' la tua ricerca a "
            f"essere sbagliata. Sito cambiato o richieste bloccate.") from None

    raise SlugNonRisolto(
        f"Specializzazione {specializzazione!r} sconosciuta a Doctolib "
        f"(la citta' {citta!r} invece esiste)."
        + (f" Forse intendevi: {', '.join(alternative[:5])}." if alternative
           else " Rigenera l'elenco con "
                "`python -m medici.doctolib.slug --aggiorna`."),
        alternative)


def _main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser(
        description="Risoluzione degli slug Doctolib (specializzazione/citta).")
    p.add_argument("--aggiorna", action="store_true",
                   help="Riscarica l'elenco delle specializzazioni dal sitemap")
    p.add_argument("specializzazione", nargs="?")
    p.add_argument("citta", nargs="?")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.aggiorna:
        slug = aggiorna_specialita()
        print(f"{len(slug)} specializzazioni salvate in {SPECIALITA_PATH}")
        return 0
    if not (args.specializzazione and args.citta):
        p.error("servono specializzazione e citta, oppure --aggiorna")
    try:
        ctx = risolvi(args.specializzazione, args.citta)
    except SlugNonRisolto as e:
        print(e)
        return 1
    print(f"{ctx.url}  ->  {ctx.nome_specializzazione} a {ctx.nome_citta}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
