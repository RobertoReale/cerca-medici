"""Fase 4 — Orchestrazione: paginazione + copertura multi-ricerca.

Itera le pagine finche' la sorgente smette di restituire schede, con smoke-test
iniziale e fallimento rumoroso (Fase 8) se una pagina che dovrebbe avere schede
ne ha 0.

Questo modulo non sa piu' come e' fatto un sito: chiede pagine a una `Sorgente`
(vedi `medici/sorgenti/`) e riceve `Riga` gia' pronte. Cio' che resta qui e'
la logica che vale per qualunque portale — retry, annullamento, sostituzione dei
dati vecchi solo a fine corsa riuscita.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

from .fetcher import Fetcher, BlockedError
from .sorgenti import (ChiaveSlot, RaccoglitoreSlot, RicercaNonValida, Sorgente,
                       elenco, ottieni)
from .storage import Storage

log = logging.getLogger("medici.scraper")

# Callback di avanzamento: (messaggio, frazione_completata | None).
# La frazione e' None quando il totale non e' noto in anticipo (le pagine si
# scoprono una alla volta), valorizzata quando lo e' (gli indirizzi da
# interrogare per le disponibilita').
Progress = Callable[[str, Optional[float]], None]

# Interrogata prima di ogni richiesta di rete: se ritorna True lo scraping si
# ferma in modo pulito e restituisce quanto raccolto finora (i dati sono gia'
# committati pagina per pagina, quindi nulla va perso).
Annulla = Callable[[], bool]

# Una sorgente si passa per nome ("miodottore") o gia' istanziata.
SorgenteArg = Union[str, Sorgente, None]


@dataclass
class RunReport:
    specializzazione: str
    citta: str
    pagine: int
    righe: int
    slot: int = 0
    # Righe che il DB non aveva ancora: `righe` conta tutto il trovato, `nuove`
    # solo le novita' rispetto allo scaricamento precedente.
    nuove: int = 0
    bloccato: bool = False
    errore: Optional[str] = None
    annullato: bool = False
    # Da quale portale arriva questo report. Con piu' sorgenti in un solo
    # comando, senza questo campo i risultati non sarebbero distinguibili.
    sorgente: str = ""


def risolvi_sorgente(sorgente: SorgenteArg = None,
                     fetcher: Optional[Fetcher] = None) -> Sorgente:
    """Normalizza l'argomento `sorgente` in un'istanza di `Sorgente`.

    Accetta anche None + `fetcher`: e' la forma storica delle chiamate
    (`scrape_search(..., fetcher=...)`), che resta valida e usa la prima
    sorgente registrata.
    """
    if isinstance(sorgente, Sorgente):
        return sorgente
    nome = sorgente or elenco()[0]
    # Passiamo `fetcher` solo se c'e': una sorgente che parla JSON potrebbe non
    # avere quel parametro nel costruttore.
    return ottieni(nome, **({"fetcher": fetcher} if fetcher is not None else {}))


def scrape_search(
    specializzazione: str,
    citta: str,
    *,
    sorgente: SorgenteArg = None,
    fetcher: Optional[Fetcher] = None,
    storage: Optional[Storage] = None,
    max_pagine: int = 40,
    sostituisci: bool = True,
    disponibilita: bool = False,
    giorni_disponibilita: int = 5,
    progress: Optional[Progress] = None,
    annullato: Optional[Annulla] = None,
) -> RunReport:
    """Scrape completo di una coppia (specializzazione, citta) su UNA sorgente.

    Se `disponibilita=True` e la sorgente la supporta, dopo l'anagrafica
    raccoglie anche gli slot orari (vedi gli avvisi su robots.txt nel README).

    `progress` riceve l'avanzamento passo per passo (vedi il tipo `Progress`):
    senza, l'unico segnale di vita e' il log, invisibile nella GUI.
    `annullato` permette di interrompere a meta' senza perdere il raccolto.
    """
    avvisa = progress or (lambda *a, **k: None)
    stop = annullato or (lambda: False)
    own_storage = storage is None
    src = risolvi_sorgente(sorgente, fetcher)
    storage = storage or Storage()

    def report(**kw) -> RunReport:
        kw.setdefault("sorgente", src.nome)
        return RunReport(specializzazione, citta, **kw)

    try:
        # `righe` del report = schede distinte TROVATE in questo run, non righe
        # nuove per il DB: senza la cancellazione iniziale un riscaricamento
        # identico avrebbe delta zero e la GUI l'avrebbe letto come fallimento.
        # Le novita' restano contate a parte, in `nuove`.
        visti: set[tuple] = set()
        nuove = 0
        tot_righe = 0
        pagina = 0
        chiavi_slot: list[ChiaveSlot] = []
        contesto = None
        for page in range(1, max_pagine + 1):
            if stop():
                log.info("Annullato dall'utente alla pagina %d.", page)
                avvisa("Annullato: mantengo i dati già raccolti.", None)
                return report(pagine=pagina, righe=tot_righe, nuove=nuove,
                              annullato=True)
            avvisa(f"[{src.etichetta}] Scarico pagina {page} di "
                   f"{specializzazione}/{citta}…", None)
            try:
                pag = src.scarica_pagina(specializzazione, citta, page)
            except BlockedError as e:
                log.error("BLOCCO rilevato: %s", e)
                return report(pagine=pagina, righe=tot_righe, nuove=nuove,
                              bloccato=True, errore=str(e))
            except RicercaNonValida as e:
                # Questo portale non sa esprimere questa ricerca (slug diverso,
                # citta' non coperta). Non e' un guasto e non si ritenta: e' un
                # esito, che l'utente deve poter leggere per correggere cio' che
                # ha scritto. Le altre sorgenti proseguono.
                log.error("[%s] ricerca non valida: %s", src.etichetta, e)
                avvisa(f"{src.etichetta}: {e}", None)
                return report(pagine=pagina, righe=tot_righe, nuove=nuove,
                              errore=str(e))

            if pag.n_schede == 0:
                # Una pagina vuota puo' essere un singhiozzo del sito, non la
                # fine dei risultati: prima ci si fermava al primo 0 e si
                # perdeva silenziosamente tutta la coda della paginazione.
                # Riproviamo una volta prima di concludere.
                log.info("0 schede a pagina %d: riprovo una volta prima di chiudere.",
                         page)
                avvisa(f"Pagina {page} vuota: riprovo una volta…", None)
                try:
                    pag = src.scarica_pagina(specializzazione, citta, page)
                except BlockedError as e:
                    log.error("BLOCCO rilevato: %s", e)
                    return report(pagine=pagina, righe=tot_righe, nuove=nuove,
                                  bloccato=True, errore=str(e))

            if pag.n_schede == 0:
                if page == 1:
                    # Fallimento rumoroso: pagina 1 vuota = markup/API cambiati
                    # o città inesistente.
                    log.error("SMOKE TEST FALLITO: 0 schede su pagina 1 per %s/%s "
                              "su %s (%s). Formato cambiato o città non "
                              "riconosciuta?", specializzazione, citta,
                              src.etichetta, src.url_ricerca(specializzazione, citta))
                break  # fine paginazione

            n = storage.insert_righe(pag.righe, specializzazione, citta,
                                     sorgente=src.nome)
            nuove += n
            pagina = page
            if pag.contesto is not None:
                # Teniamo il contesto piu' recente: se contiene credenziali a
                # scadenza (MioDottore: l'ACCESS_TOKEN nell'HTML), quello
                # dell'ultima pagina e' il piu' fresco disponibile.
                contesto = pag.contesto
            for r in pag.righe:
                # Stessa chiave dell'indice UNIQUE: le schede che il sito ripete
                # su piu' pagine non vanno contate due volte.
                visti.add((r.result_id, r.address_id, r.servizio))
                chiave = src.chiave_slot(r)
                if chiave is not None:
                    chiavi_slot.append(chiave)
            tot_righe = len(visti)
            log.info("Pagina %d: %d schede -> %d righe (tot %d, %d nuove)",
                     page, pag.n_schede, len(pag.righe), tot_righe, nuove)
            avvisa(f"Pagina {page}: {pag.n_schede} schede → {tot_righe} righe "
                   f"({nuove} nuove)", None)
            if pag.ultima:
                # La sorgente sa gia' di aver esaurito i risultati (p.es. dal
                # totale dichiarato in un header): inutile una richiesta in piu'
                # solo per vederla tornare vuota.
                log.info("La sorgente dichiara esaurita la paginazione.")
                break

        tot_slot = 0
        interrotto = False
        if disponibilita and chiavi_slot:
            if not src.supporta_disponibilita:
                log.warning("%s non espone le disponibilità: opzione ignorata.",
                            src.etichetta)
                avvisa(f"{src.etichetta} non espone le disponibilità: salto.", None)
            else:
                def ricarica():
                    """Contesto fresco dalla pagina 1, per credenziali scadute."""
                    return src.scarica_pagina(specializzazione, citta, 1).contesto

                racc = src.raccoglitore_slot(contesto, ricarica_contesto=ricarica)
                if racc is None:
                    log.error("Contesto insufficiente per le disponibilità "
                              "(credenziali non estratte?): salto la raccolta.")
                else:
                    tot_slot, interrotto = _raccogli_disponibilita(
                        racc, chiavi_slot, storage, src.nome,
                        giorni_disponibilita, progress=progress, annullato=stop)

        # Solo ora che il nuovo giro e' andato a buon fine buttiamo il vecchio:
        # chi non compare piu' tra i risultati del sito esce dal DB.
        # `pagina > 0` e non `tot_righe > 0`: un run che ritrova esattamente gli
        # stessi medici e' riuscito lo stesso, semplicemente non ha novita'.
        if sostituisci and pagina > 0 and not interrotto:
            rimosse = storage.clear_query_tranne(specializzazione, citta, visti,
                                                 sorgente=src.nome)
            if rimosse:
                log.info("Rimosse %d righe non piu' presenti per %s/%s su %s",
                         rimosse, specializzazione, citta, src.nome)

        return report(pagine=pagina, righe=tot_righe, slot=tot_slot,
                      nuove=nuove, annullato=interrotto)
    finally:
        if own_storage:
            storage.close()


def _raccogli_disponibilita(raccoglitore: RaccoglitoreSlot,
                            chiavi: Sequence[ChiaveSlot], storage: Storage,
                            sorgente: str, giorni: int, progress=None,
                            annullato=None) -> tuple[int, bool]:
    """Recupera e salva gli slot per una lista di `ChiaveSlot` uniche.

    Ritorna (slot salvati, interrotto_dall_utente)."""
    avvisa = progress or (lambda *a, **k: None)
    stop = annullato or (lambda: False)
    uniche = list(dict.fromkeys(chiavi))  # dedup preservando l'ordine
    tot = 0
    # Qui il totale e' noto: possiamo dare una percentuale reale invece di uno
    # spinner indefinito, che su decine di indirizzi (1-3s l'uno) e' l'unica
    # informazione utile per capire quanto manca.
    for i, chiave in enumerate(uniche, 1):
        if stop():
            log.info("Annullato dall'utente dopo %d indirizzi.", i - 1)
            avvisa(f"Annullato: {tot} slot già salvati.", None)
            return tot, True
        avvisa(f"Disponibilità: indirizzo {i} di {len(uniche)} "
               f"({tot} slot finora)", i / len(uniche))
        try:
            slots = raccoglitore.slots(chiave, giorni)
        except Exception as e:  # una singola disponibilità fallita non ferma il resto
            log.warning("Slot falliti per %s/%s: %s",
                        chiave.result_id, chiave.address_id, e)
            continue
        # La pulizia avviene DOPO il fetch riuscito: prima si cancellavano gli
        # slot vecchi e poi, se la richiesta falliva, si restava senza dato
        # alcuno per quell'indirizzo.
        storage.clear_disponibilita(chiave.result_id, chiave.address_id,
                                    sorgente=sorgente)
        if slots:
            tot += storage.insert_disponibilita(slots, sorgente=sorgente)
    log.info("Disponibilità: %d slot salvati per %d indirizzi", tot, len(uniche))
    return tot, False


def scrape_many(
    coppie: Iterable[Tuple[str, str]],
    *,
    sorgenti: Optional[Sequence[SorgenteArg]] = None,
    db_path=None,
    max_pagine: int = 40,
    disponibilita: bool = False,
    giorni_disponibilita: int = 5,
    progress: Optional[Progress] = None,
    annullato: Optional[Annulla] = None,
) -> List[RunReport]:
    """Loop esterno su piu' (specializzazione, citta) x piu' sorgenti.

    Riusa una sola connessione al DB e una sola istanza per sorgente, cosi' la
    sessione HTTP (e il suo rate limiting) resta condivisa per tutto il batch.
    """
    istanze = [risolvi_sorgente(s) for s in (sorgenti or [None])]
    storage = Storage(db_path) if db_path else Storage()
    reports: List[RunReport] = []
    stop = annullato or (lambda: False)
    try:
        for spec, citta in coppie:
            for src in list(istanze):
                if stop():
                    return reports
                log.info("=== Scraping %s / %s su %s ===", spec, citta, src.nome)
                rep = scrape_search(spec, citta, sorgente=src, storage=storage,
                                    max_pagine=max_pagine,
                                    disponibilita=disponibilita,
                                    giorni_disponibilita=giorni_disponibilita,
                                    progress=progress, annullato=annullato)
                reports.append(rep)
                if rep.bloccato:
                    # Un blocco riguarda un sito solo: escludiamo QUELLA
                    # sorgente dal resto del batch, non le altre che stanno bene
                    # (prima un blocco interrompeva l'intero giro).
                    log.error("Blocco su %s: la escludo dal resto del batch.",
                              src.nome)
                    istanze = [s for s in istanze if s is not src]
                    if not istanze:
                        return reports
    finally:
        storage.close()
    return reports
