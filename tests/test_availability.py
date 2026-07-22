"""Regressioni su availability: 4xx tollerati, header non condivisi, offset TZ."""
from datetime import datetime

import pytest

from medici.availability import (AvailabilityFetcher, _iso, build_slots_url,
                                 extract_access_token)
from medici.fetcher import FetchError, FetchResult


class FetcherFinto:
    """Fetcher di test: registra le chiamate e simula la risposta scelta.

    `esiti` permette di programmare una sequenza (es. prima un 401, poi un 200)
    per verificare il rinnovo del token."""

    def __init__(self, risposta: str = "", errore=None, esiti=None):
        self.risposta, self.errore = risposta, errore
        self.esiti = list(esiti or [])
        self.chiamate = []

        class _S:
            headers = {}
        self.session = _S()

    def get(self, url, *, expect_cards=False, headers=None):
        self.chiamate.append((url, headers))
        esito = self.esiti.pop(0) if self.esiti else (self.errore or self.risposta)
        if isinstance(esito, Exception):
            raise esito
        return FetchResult(url=url, status=200, html=esito)


def test_404_non_propaga():
    """Cliniche e medici senza calendario danno 404: niente slot, nessun crash."""
    f = FetcherFinto(errore=FetchError("HTTP 404"))
    af = AvailabilityFetcher("tok", fetcher=f)
    assert af.fetch_slots("1", "2") == []


def test_header_non_finiscono_nella_sessione():
    """L'Authorization deve viaggiare per-richiesta, non sporcare la sessione
    condivisa con lo scraping HTML."""
    f = FetcherFinto(risposta='{"_items": []}')
    af = AvailabilityFetcher("tok", fetcher=f)
    af.fetch_slots("1", "2")
    assert "Authorization" not in f.session.headers
    assert f.chiamate[0][1]["Authorization"] == "Bearer tok"


def test_slot_normalizzati():
    f = FetcherFinto(risposta='{"_items": [{"start": "2026-07-22T16:00:00+02:00",'
                              ' "doctor_id": 7, "address_id": 9, "booked": false}]}')
    af = AvailabilityFetcher("tok", fetcher=f)
    s = af.fetch_slots("7", "9")[0]
    assert (s["data"], s["orario"], s["prenotabile"]) == ("2026-07-22", "16:00", True)


@pytest.mark.parametrize("dt, atteso", [
    (datetime(2026, 7, 22, 0, 0, 0), "+02:00"),   # CEST (ora legale)
    (datetime(2026, 1, 15, 0, 0, 0), "+01:00"),   # CET (ora solare)
])
def test_offset_stagionale(dt, atteso):
    """Prima era hardcoded +02:00: d'inverno la finestra era sfalsata di un'ora."""
    assert _iso(dt).endswith(atteso)


def test_401_rinnova_il_token_e_riprova():
    """Token scaduto a meta' run: con il rinnovo l'indirizzo resta recuperabile."""
    f = FetcherFinto(esiti=[FetchError("HTTP 401", 401), '{"_items": []}'])
    af = AvailabilityFetcher("vecchio", fetcher=f, rinnova_token=lambda: "nuovo")
    assert af.fetch_slots("1", "2") == []
    assert af.access_token == "nuovo"
    assert f.chiamate[1][1]["Authorization"] == "Bearer nuovo"


def test_404_non_tenta_il_rinnovo():
    """Un 404 e' definitivo (niente calendario): rinnovare non servirebbe."""
    rinnovi = []
    f = FetcherFinto(errore=FetchError("HTTP 404", 404))
    af = AvailabilityFetcher("tok", fetcher=f,
                             rinnova_token=lambda: rinnovi.append(1) or "nuovo")
    assert af.fetch_slots("1", "2") == []
    assert rinnovi == []


def test_rinnovo_fallito_non_propaga():
    f = FetcherFinto(errore=FetchError("HTTP 401", 401))
    af = AvailabilityFetcher("tok", fetcher=f,
                             rinnova_token=lambda: (_ for _ in ()).throw(RuntimeError("giù")))
    assert af.fetch_slots("1", "2") == []


def test_finestra_di_zero_giorni_non_finisce_prima_di_iniziare():
    """`--giorni 0` chiedeva una finestra a rovescio: nessuno slot e nessuna
    spiegazione. Il minimo sensato e' un giorno."""
    url = build_slots_url("1", "2", giorni=0,
                          start=datetime(2026, 7, 22, 0, 0, 0))
    assert "start=2026-07-22T00:00:00" in url
    assert "end=2026-07-22T23:59:59" in url


def test_estrazione_token():
    html = "var x; ZLApp.APICredentials = { 'ACCESS_TOKEN' : 'abc123' , }"
    assert extract_access_token(html) == "abc123"
    assert extract_access_token("<html></html>") is None
