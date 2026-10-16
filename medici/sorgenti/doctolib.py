"""Sorgente Doctolib.it — mappatura dell'API JSON sul contratto `Sorgente`.

Le richieste HTTP stanno in `medici/doctolib/`; qui c'e' solo la traduzione dei
record di Doctolib in `Riga` e la gestione degli slot. E' molto meno codice
della sorgente MioDottore, e non e' un caso: dietro c'e' un'API JSON invece di
un HTML da spolpare con i selettori.

Due differenze di sostanza rispetto a MioDottore, non di forma:

1. **Il prezzo e' uno solo per medico**, ed e' quello del servizio che ha fatto
   match con la ricerca (`matchedVisitMotive`), non il listino. Cercare
   «cardiologo» e «ecocardiografia» sullo stesso medico da' due prezzi diversi,
   entrambi veri. Il modello «una riga per indirizzo» di MioDottore diventa qui
   «una riga per medico/sede».
2. **`practiceId` e' la SEDE, non la coppia medico-sede.** I medici di un
   poliambulatorio lo condividono. Il codice che ne dipende (cancellazione degli
   slot, join delle disponibilita') correla gia' su
   `(sorgente, result_id, address_id)` ed e' coperto da test; ma la garanzia di
   unicita' che vale su MioDottore **qui non c'e'**, e nessuna scorciatoia
   basata sul solo indirizzo va reintrodotta.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..doctolib.client import BASE_URL, ClientDoctolib, ContestoDoctolib
from ..doctolib.slug import SlugNonRisolto, risolvi
from ..fetcher import FetcherHTTP
from ..parser import Riga, prezzo_plausibile
from . import (ChiaveSlot, Pagina, RaccoglitoreSlot, RicercaNonValida, Sorgente,
               registra)

log = logging.getLogger("medici.sorgenti.doctolib")

NOME = "doctolib"
ETICHETTA = "Doctolib"

# `type` del record -> il `tipo` che usa il resto del progetto. Allinea la
# semantica di `chiave_slot` e dei filtri a quella di MioDottore, che distingue
# i professionisti dalle strutture.
_TIPI = {"INDIVIDUAL_PRACTITIONER": "doctor", "ORGANIZATION": "clinic"}

# `fee.type` che significa "prezzo variabile, questo e' il minimo": stessa
# semantica del «da 30 €» di MioDottore.
_FEE_VARIABILE = "VARIES_DEPENDING_ON_HCP"


def _str(x) -> Optional[str]:
    """`None` resta `None`; tutto il resto diventa stringa.

    Gli id di Doctolib sono numeri, quelli di MioDottore stringhe, e il DB li
    tiene in colonne TEXT: senza questa conversione `result_id` sarebbe `1620432`
    da una parte e `'1620432'` dall'altra, e i confronti fallirebbero in modo
    invisibile.
    """
    return None if x is None else str(x)


def riga_da_medico(m: dict) -> Riga:
    """Un record dell'API -> una `Riga`.

    Tollerante sui campi opzionali (prezzo, rating, motivo di visita: mancano in
    una minoranza consistente di record) e severa su quelli strutturali
    (`references`): se cambiassero, e' giusto accorgersene subito.

    Attenzione a `references.type`: i record `directory_doctor` sono schede da
    elenco e non clienti Doctolib, quindi hanno `practiceId: null` — cioe'
    `address_id` a `None`. Sono anche quelli senza agenda e senza prezzo.
    """
    rif = m.get("references") or {}
    loc = m.get("location") or {}
    mv = m.get("matchedVisitMotive") or {}
    fee = mv.get("fee") or {}
    rating = m.get("rating") or {}
    prezzo = fee.get("amount")
    e_da_min = fee.get("type") == _FEE_VARIABILE

    # Doctolib spezza il nome in tre pezzi e ne lascia vuoti alcuni ("title" e'
    # spesso ""); MioDottore lo da' gia' intero. Ricomponiamo per avere lo
    # stesso formato nelle due fonti — e' anche cio' che rende confrontabili i
    # nomi nell'unificazione dei duplicati.
    nome = " ".join(p for p in (m.get("title"), m.get("firstName"), m.get("name"))
                    if p and p.strip()) or None

    link = m.get("link") or ""
    agende = [a for a in (mv.get("agendaIds") or []) if a is not None]

    return Riga(
        result_id=_str(rif.get("id")),
        address_id=_str(rif.get("practiceId")),
        nome=nome,
        # `or ""`: `type` puo' mancare, e cercare `None` in una mappa di stringhe
        # darebbe la risposta giusta (None) per il motivo sbagliato.
        tipo=_TIPI.get(m.get("type") or ""),
        specializzazione=(m.get("speciality") or {}).get("name"),
        citta=loc.get("city"),
        indirizzo=loc.get("address"),
        # Doctolib non pubblica la provincia. Derivarla dal CAP sarebbe una
        # tabella da mantenere per una colonna che nessun filtro usa: meglio
        # None, che dice la verita'.
        provincia=None,
        servizio=mv.get("name"),
        servizio_id=_str(mv.get("visitMotiveId")),
        prezzo=(prezzo_plausibile(float(prezzo))
                if isinstance(prezzo, (int, float)) else None),
        prezzo_e_da_min=e_da_min,
        # Ricostruito: l'API da' un numero, non una stringa. Serve a confrontare
        # a occhio le due fonti sullo stesso medico, quindi imita la forma di
        # MioDottore («da 90 €») invece di inventarne una nuova.
        prezzo_raw=(f"{'da ' if e_da_min else ''}{prezzo:g} €"
                    if isinstance(prezzo, (int, float)) else None),
        rating=rating.get("value"),
        n_recensioni=rating.get("totalUserReviews"),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        url_profilo=(BASE_URL + link) if link.startswith("/") else (link or None),
        ha_calendario="onlineBooking" in (m.get("services") or []),
        # Fuori dal DB: servono solo a `chiave_slot` per sapere quale calendario
        # interrogare. `agenda_ids` e' una tupla perche' `ChiaveSlot` e' frozen e
        # viene messa in un set per la deduplica.
        extra={"visit_motive_id": mv.get("visitMotiveId"),
               "agenda_ids": tuple(agende),
               "practice_id": rif.get("practiceId")},
    )


def _motivo_breve(motivo: Optional[str]) -> str:
    """Il `reason` di Doctolib ridotto a una categoria da contare.

    Il testo completo contiene il numero di telefono dello studio, quindi due
    calendari chiusi per lo stesso motivo non avrebbero mai lo stesso testo.
    """
    if motivo and "non offre" in motivo.lower():
        return "non accettano prenotazioni online"
    return "senza posti liberi nel periodo"


class _SlotDoctolib(RaccoglitoreSlot):
    def __init__(self, client: ClientDoctolib):
        self._client = client
        self._vuoti: Counter = Counter()

    def riepilogo(self) -> Optional[str]:
        if not self._vuoti:
            return None
        parti = [f"{n} {motivo}" for motivo, n in self._vuoti.most_common()]
        return f"calendari vuoti: {', '.join(parti)}"

    def slots(self, chiave: ChiaveSlot, giorni: int) -> List[dict]:
        visit_motive_id, agenda_ids = chiave.dati
        grezzi, motivo = self._client.disponibilita(
            chiave.address_id, visit_motive_id, agenda_ids, giorni=giorni)
        if not grezzi:
            self._vuoti[_motivo_breve(motivo)] += 1
        # `prenotabile` e' sempre True: Doctolib pubblica solo gli orari liberi,
        # non ha l'equivalente del campo `booked` di MioDottore. Cio' che manca
        # e' occupato *oppure* fuori orario, e i due casi non si distinguono.
        return [{"result_id": chiave.result_id,
                 "address_id": chiave.address_id,
                 "servizio_id": _str(visit_motive_id),
                 "data": s["data"], "orario": s["orario"],
                 "prenotabile": True}
                for s in grezzi]


class SorgenteDoctolib(Sorgente):
    nome = NOME
    etichetta = ETICHETTA
    supporta_disponibilita = True

    def __init__(self, fetcher: Optional[FetcherHTTP] = None,
                 client: Optional[ClientDoctolib] = None):
        # `client` iniettabile: i test lo sostituiscono con uno finto e non
        # toccano la rete. `fetcher` resta accettato perche' e' cosi' che
        # `risolvi_sorgente` istanzia le sorgenti.
        self.client = client or ClientDoctolib(fetcher=fetcher)
        self._contesti: Dict[Tuple[str, str], ContestoDoctolib] = {}

    # -- ricerca -------------------------------------------------------------
    def _contesto(self, specializzazione: str, citta: str) -> ContestoDoctolib:
        """Risolve gli slug UNA volta per ricerca e tiene il risultato.

        Senza cache ogni pagina pagherebbe la landing da 1,5 MB: su una ricerca
        da 30 pagine sarebbero 45 MB e altrettanti secondi di attesa per un dato
        che non cambia.
        """
        chiave = (specializzazione, citta)
        if chiave not in self._contesti:
            try:
                self._contesti[chiave] = risolvi(specializzazione, citta,
                                                 client=self.client)
            except SlugNonRisolto as e:
                # Tradotta nel linguaggio del contratto: lo scraper la riporta
                # come esito della singola sorgente e prosegue con le altre.
                raise RicercaNonValida(str(e)) from None
        return self._contesti[chiave]

    def url_ricerca(self, specializzazione: str, citta: str) -> str:
        """URL leggibile PRIMA di aver risolto gli slug: nessuna richiesta.

        Viene stampato all'avvio, quando ancora non si sa se «dermatologo»
        esiste su Doctolib. Se lo slug verra' corretto, il log lo dira'.
        """
        ctx = self._contesti.get((specializzazione, citta))
        if ctx is not None:
            return ctx.url
        from ..testo import slugify
        return f"{BASE_URL}/{slugify(specializzazione)}/{slugify(citta)}"

    def scarica_pagina(self, specializzazione: str, citta: str, page: int) -> Pagina:
        ctx = self._contesto(specializzazione, citta)
        pag = self.client.cerca(ctx, page)
        righe = [riga_da_medico(m) for m in pag.medici]
        # `n_schede` = i record ricevuti: su Doctolib una scheda e' una riga (un
        # solo prezzo per medico), quindi i due numeri coincidono sempre.
        return Pagina(righe=righe, n_schede=len(pag.medici), contesto=ctx,
                      ultima=pag.ultima)

    # -- disponibilita -------------------------------------------------------
    def chiave_slot(self, riga: Riga) -> Optional[ChiaveSlot]:
        agende = riga.extra.get("agenda_ids") or ()
        motivo = riga.extra.get("visit_motive_id")
        # Senza agenda o senza motivo di visita l'endpoint risponde comunque,
        # ma con `availabilities: []`: e' una richiesta di rete buttata via.
        if not riga.ha_calendario or not agende or motivo is None:
            return None
        if not (riga.result_id and riga.address_id):
            return None
        return ChiaveSlot(result_id=riga.result_id, address_id=riga.address_id,
                          dati=(motivo, tuple(agende)))

    def raccoglitore_slot(
        self,
        contesto: Any,
        *,
        ricarica_contesto: Optional[Callable[[], Any]] = None,
    ) -> Optional[RaccoglitoreSlot]:
        # Nessuna credenziale da estrarre e nessuna da rinnovare: l'endpoint
        # degli slot di Doctolib e' aperto (a differenza del Bearer token che
        # MioDottore incorpora nell'HTML). Il raccoglitore e' quindi sempre
        # costruibile, e `ricarica_contesto` non serve.
        return _SlotDoctolib(self.client)


registra(NOME, SorgenteDoctolib, ETICHETTA)
