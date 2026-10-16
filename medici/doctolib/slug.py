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
2. Se non e' riconosciuta, la tabella `SINONIMI` traduce i nomi che i due
   portali danno in modo diverso alla stessa cosa (`dentista` -> `odontoiatra`).
3. Poi la cache locale `data/doctolib_specialita.json` (i 130 slug ufficiali
   del sitemap), ma si accetta **da soli** solo cio' che non puo' essere
   un'altra specializzazione: un contenimento a parole senza rivali
   (`dermatologo` -> `dermatologo-venereologo`) o un refuso evidente
   (`cardiolgo`, somiglianza >= `SOGLIA_REFUSO`).
4. Tutto il resto fallisce **rumorosamente** elencando gli slug piu' simili. Mai
   restituire 0 risultati in silenzio, e soprattutto mai i risultati di
   un'altra specializzazione: e' la stessa lezione dello smoke test dello
   scraper.

Il punto 3 era piu' permissivo e sul campo ha tradotto `dentista` in
`dietista`, `oculista` in `psicanalista`, `otorino` in `laboratorio` e
`ortopedico` in `tecnico-ortopedico`: ricerche partite senza errore, sulla
specializzazione sbagliata. Una somiglianza ortografica dice che due parole si
scrivono in modo simile, non che indicano lo stesso medico.

Citta'
------
Non serve un endpoint di autocomplete delle localita' (che non e' stato
individuato: `/patient-health-search/api/v1/places` e `/location/autocomplete`
rispondono entrambi 404). La landing stessa fa da validatore, e in piu'
restituisce la forma canonica: «reggio nell'emilia» diventa
`provincia-di-reggio-emilia` senza che noi si debba sapere come.

Non sempre, pero': `/cardiologo/roma` redirige alla ricerca nazionale, e Roma
esiste solo come `citta-metropolitana-di-roma`. Per questo, prima di dichiarare
ignota una citta', si provano le forme in `_FORME_CITTA` (verificato a ottobre
2026 sui capoluoghi principali: era l'unico caso).
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

# Stessa specializzazione, nome diverso: a sinistra lo slug di MioDottore (cio'
# che l'utente scrive, perche' il progetto e' nato li'), a destra quello di
# Doctolib. Solo equivalenze certe: dove la corrispondenza e' discutibile
# (`flebologo`: angiologo o chirurgo vascolare?) meglio un errore con le
# alternative che una scelta fatta al posto dell'utente.
SINONIMI = {
    "dentista": "odontoiatra",
    "oculista": "oftalmologo",
    "ortopedico": "ortopedico-traumatologo",
    "otorino": "otorinolaringoiatra",
    "ginecologo": "ginecologo-ostetrico",
    "medico-di-base": "medico-di-medicina-generale",
    "medico-competente": "medico-del-lavoro",
    "medico-genetista": "genetista",
    "medico-sportivo": "medico-dello-sport",
    "radiologo": "radiologo-diagnostico",
    "nutrizionista": "biologo-nutrizionista",
}

# Sopra questa somiglianza (`difflib`, 0..1) una parola e' un refuso dello slug
# e si corregge da sola. Tarata su coppie reali: i refusi veri stanno tutti
# sopra 0.92 (`cardiolgo` 0.947, `urolgo` 0.923), le traduzioni sbagliate sotto
# (`dentista`->`dietista` 0.875, `radiologo`->`cardiologo` 0.842). Tra le due,
# meglio un suggerimento in piu' che una ricerca sulla specializzazione sbagliata.
SOGLIA_REFUSO = 0.92

# Come Doctolib puo' chiamare una citta' che non riconosce per nome. Si provano
# solo dopo che la forma diretta e' fallita, quindi non costano nulla nel caso
# normale.
_FORME_CITTA = ("citta-metropolitana-di-{}", "provincia-di-{}")


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


def _per_parola(voluto: str, noti: Sequence[str]) -> List[str]:
    """Gli slug che contengono tutte le parole significative di `voluto`.

    Le parole troppo comuni (`medico`, `chirurgo`) non contano, altrimenti
    «medico dello sport» pescherebbe mezzo elenco.
    """
    parole_utili = _parole(voluto) - _PAROLE_DEBOLI
    trovati = [s for s in noti if parole_utili and parole_utili <= _parole(s)]
    trovati.sort(key=len)   # il piu' corto e' il piu' specifico
    return trovati


def _somiglianza(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def candidati(voluto: str, noti: Sequence[str]) -> List[str]:
    """Slug plausibili per `voluto`, dal piu' al meno probabile: sono i
    suggerimenti da mostrare all'utente, non scelte da applicare da soli
    (quelle sono in `accettabili`).

    Tre criteri, in quest'ordine:
    1. **sinonimo** noto (`dentista` -> `odontoiatra`);
    2. **contenimento a parole** — `dermatologo` sta dentro
       `dermatologo-venereologo`: e' il caso reale piu' frequente, ed e' una
       corrispondenza esatta, non una somiglianza;
    3. **somiglianza ortografica** (`difflib`) — recupera i refusi.
    """
    voluto = slugify(voluto)
    if not voluto:
        return []
    sinonimo = [SINONIMI[voluto]] if voluto in SINONIMI else []
    simili = difflib.get_close_matches(voluto, noti, n=5, cutoff=0.6)
    return list(dict.fromkeys(sinonimo + _per_parola(voluto, noti) + simili))


def accettabili(voluto: str, noti: Sequence[str]) -> List[str]:
    """Le alternative che si possono provare **senza chiedere**: quelle che non
    possono indicare un'altra specializzazione.

    - il sinonimo noto;
    - il contenimento a parole, ma solo se **unico**: `ortopedico` sta sia in
      `ortopedico-traumatologo` sia in `tecnico-ortopedico`, e scegliere il piu'
      corto voleva dire cercare tecnici invece di medici;
    - i refusi evidenti (somiglianza >= `SOGLIA_REFUSO`).
    """
    voluto = slugify(voluto)
    if not voluto:
        return []
    fuori = [SINONIMI[voluto]] if voluto in SINONIMI else []
    per_parola = _per_parola(voluto, noti)
    if len(per_parola) == 1:
        fuori += per_parola
    refusi = sorted((s for s in noti if _somiglianza(voluto, s) >= SOGLIA_REFUSO),
                    key=lambda s: -_somiglianza(voluto, s))
    return list(dict.fromkeys(fuori + refusi))


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

    def _prova(spec: str) -> ContestoDoctolib:
        """`spec` nella citta' chiesta, o in una delle sue forme alternative.

        Solleva `LandingNonTrovata` se a non esistere e' la specializzazione,
        `SlugNonRisolto` se e' la citta' (e nessuna forma alternativa esiste).
        """
        try:
            return client.contesto_ricerca(spec, citta_slug)
        except LandingNonTrovata as e:
            if e.cosa != "citta":
                raise
        # La specializzazione e' valida, la citta' no: forse Doctolib la
        # conosce con un altro nome. Un redirect di questi tentativi e' sempre
        # verso `/{spec}`, quindi non c'e' rischio di accettare la citta'
        # sbagliata: `contesto_ricerca` lo rifiuterebbe come prima.
        for forma in _FORME_CITTA:
            try:
                ctx = client.contesto_ricerca(spec, forma.format(citta_slug))
            except LandingNonTrovata:
                continue
            log.info("Doctolib conosce %r solo come %r: i risultati coprono "
                     "tutta l'area.", citta, ctx.city_slug)
            return ctx
        raise _citta_ignota()

    try:
        return _prova(spec_slug)
    except LandingNonTrovata as e:
        log.info("/%s/%s non riconosciuta (%s): cerco lo slug giusto.",
                 spec_slug, citta_slug, e.cosa)

    # Resta il caso "specializzazione" (o l'indecidibile "pagina"): proviamo le
    # alternative sicure dall'elenco ufficiale. Una `SlugNonRisolto` da `_prova`
    # vuol dire che con `alt` la specializzazione ha risolto e a mancare e' la
    # citta': le alternative rimanenti fallirebbero tutte allo stesso modo, e
    # il messaggio finale darebbe la colpa alla specializzazione, che invece
    # era giusta. Per questo la si lascia propagare.
    elenco = list(noti) if noti is not None else carica_specialita()
    for alt in accettabili(spec_slug, elenco):
        try:
            ctx = _prova(alt)
        except LandingNonTrovata:
            continue
        log.info("Specializzazione %r risolta in %r su Doctolib.",
                 specializzazione, alt)
        return ctx

    # Nessuna alternativa ha funzionato. Prima di dare la colpa all'utente,
    # una sonda con una specializzazione che esiste ovunque: se fallisce anche
    # quella, il problema non e' cio' che ha scritto.
    try:
        _prova(SPEC_DI_RIFERIMENTO)
    except (LandingNonTrovata, FetchError):
        raise SlugNonRisolto(
            f"Doctolib non risponde come previsto nemmeno per "
            f"{SPEC_DI_RIFERIMENTO!r} a {citta!r}: non e' la tua ricerca a "
            f"essere sbagliata. Sito cambiato o richieste bloccate.") from None

    alternative = candidati(spec_slug, elenco)
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
    from ..testo import console_utf8
    console_utf8()                            # console Windows: «Città»
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
