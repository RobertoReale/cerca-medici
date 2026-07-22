"""Sorgente MioDottore.it — adattatore sui moduli storici.

Questo file non contiene logica nuova: prende `fetcher.py`, `parser.py` e
`availability.py`, che restano il codice di riferimento per MioDottore, e li
presenta con l'interfaccia `Sorgente` che ora usa lo scraper.

Il pezzo che vale la pena leggere e' `raccoglitore_slot`: il contesto di
MioDottore e' l'HTML della pagina di ricerca, perche' e' li' dentro che il sito
incorpora l'ACCESS_TOKEN necessario all'endpoint degli slot.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from ..availability import AvailabilityFetcher, extract_access_token
from ..fetcher import Fetcher, FetcherRicerca, build_search_url
from ..parser import Riga, count_cards, make_soup, parse_search_html
from . import ChiaveSlot, Pagina, RaccoglitoreSlot, Sorgente, registra

NOME = "miodottore"
ETICHETTA = "MioDottore"


class _SlotMioDottore(RaccoglitoreSlot):
    def __init__(self, af: AvailabilityFetcher):
        self._af = af

    def slots(self, chiave: ChiaveSlot, giorni: int) -> List[dict]:
        return self._af.fetch_slots(chiave.result_id, chiave.address_id,
                                    giorni=giorni)


class SorgenteMioDottore(Sorgente):
    nome = NOME
    etichetta = ETICHETTA
    supporta_disponibilita = True

    def __init__(self, fetcher: Optional[FetcherRicerca] = None):
        # `fetcher` e' un qualsiasi `FetcherRicerca` (vedi il protocollo in
        # `medici/fetcher.py`): i test ne iniettano uno finto per verificare la
        # paginazione senza rete.
        self.fetcher: FetcherRicerca = fetcher or Fetcher()

    def url_ricerca(self, specializzazione: str, citta: str) -> str:
        return build_search_url(specializzazione, citta)

    def scarica_pagina(self, specializzazione: str, citta: str, page: int) -> Pagina:
        res = self.fetcher.get_search_page(specializzazione, citta, page)
        # Un solo albero per pagina: contare le card e parsarle su due soup
        # diversi raddoppiava il lavoro su HTML da ~650 KB.
        soup = make_soup(res.html)
        return Pagina(
            righe=parse_search_html(soup),
            n_schede=count_cards(soup),
            contesto=res.html,
        )

    def chiave_slot(self, riga: Riga) -> Optional[ChiaveSlot]:
        # L'endpoint /doctors/{id}/addresses/{id}/slots esiste solo per i medici
        # con agenda online: cliniche e profili senza calendario rispondono
        # 400/404. Filtrarli qui e' cio' che tiene il run a decine invece che a
        # centinaia di richieste.
        if not (riga.result_id and riga.address_id):
            return None
        if not riga.ha_calendario or riga.tipo == "clinic":
            return None
        return ChiaveSlot(result_id=riga.result_id, address_id=riga.address_id)

    def raccoglitore_slot(
        self,
        contesto: Any,
        *,
        ricarica_contesto: Optional[Callable[[], Any]] = None,
    ) -> Optional[RaccoglitoreSlot]:
        token = extract_access_token(contesto or "")
        if not token:
            return None

        def rinnova() -> Optional[str]:
            """Un token scaduto a meta' run farebbe fallire con 401 tutti gli
            indirizzi rimanenti: qui ne recuperiamo uno fresco dalla pagina 1."""
            if ricarica_contesto is None:
                return None
            return extract_access_token(ricarica_contesto() or "")

        return _SlotMioDottore(AvailabilityFetcher(
            token, fetcher=self.fetcher, rinnova_token=rinnova))


registra(NOME, SorgenteMioDottore, ETICHETTA)
