"""Client Doctolib: paginazione, diagnosi dei redirect, slot. Senza rete.

Il `Fetcher` e' sostituito da un finto che restituisce risposte preparate: cio'
che si vuole verificare non e' che `requests` funzioni, ma che l'off-by-one
della paginazione e la lettura dei redirect siano giusti — sono le due cose che,
sbagliate, perdono dati in silenzio.
"""
import json
from pathlib import Path

import pytest

from medici.doctolib.client import (BASE_URL, ClientDoctolib, LandingNonTrovata,
                                    _totale_da_content_range, rileva_blocco)
from medici.fetcher import FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
RICERCA = json.loads((FIXTURES / "doctolib_search.json").read_text(encoding="utf-8"))


class FetcherFinto:
    """Risponde con cio' che gli si mette in `risposte`, e registra le chiamate."""

    def __init__(self, risposte=None):
        self.risposte = list(risposte or [])
        self.chiamate = []

    def _prossima(self, url):
        self.chiamate.append(url)
        if not self.risposte:
            raise AssertionError(f"richiesta inattesa: {url}")
        r = self.risposte.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._prossima(url)

    def post(self, url, **kw):
        return self._prossima(url)


def _res(corpo, *, url="https://www.doctolib.it/x", url_finale=None,
         headers=None, status=200) -> FetchResult:
    if not isinstance(corpo, str):
        corpo = json.dumps(corpo)
    return FetchResult(url=url, status=status, html=corpo,
                       url_finale=url_finale or url, headers=headers or {})


def _landing(spec="cardiologo", citta="milano", *, place=None, url_finale=None):
    place = place if place is not None else {"id": 620500, "name": "Milano",
                                             "slug": citta}
    html = ("<html><script>window.keyword = "
            + json.dumps({"slug": spec, "name": spec.capitalize()})
            + ";\nwindow.place = " + json.dumps(place) + ";</script></html>")
    url = f"{BASE_URL}/{spec}/{citta}"
    return _res(html, url=url, url_finale=url_finale or url)


# --- landing e diagnosi dei redirect ----------------------------------------

def test_contesto_estrae_keyword_e_place():
    c = ClientDoctolib(fetcher=FetcherFinto([_landing()]))
    ctx = c.contesto_ricerca("cardiologo", "milano")
    assert ctx.place["id"] == 620500
    assert (ctx.nome_citta, ctx.nome_specializzazione) == ("Milano", "Cardiologo")


def test_place_con_graffe_nelle_stringhe():
    """`raw_decode` e non un regex: le traduzioni dentro `place` contengono
    graffe, e qualunque regex ragionevole troncherebbe l'oggetto a meta'."""
    place = {"id": 1, "name": "Milano", "msg": "usa {questo} e {quello}",
             "viewport": {"ne": {"lat": 1.0}}}
    c = ClientDoctolib(fetcher=FetcherFinto([_landing(place=place)]))
    ctx = c.contesto_ricerca("cardiologo", "milano")
    assert ctx.place["viewport"]["ne"]["lat"] == 1.0
    assert ctx.place["msg"].endswith("{quello}")


def test_citta_ignota_riconosciuta_dal_redirect():
    """Doctolib manda una citta' che non conosce su `/{spec}` e serve 200 con i
    medici di Milano. E' il caso piu' pericoloso del sito: senza questo
    controllo, cercare a Pinco salverebbe cardiologi milanesi sotto «Pinco»."""
    c = ClientDoctolib(fetcher=FetcherFinto([
        _landing(citta="pinco", url_finale=f"{BASE_URL}/cardiologo")]))
    with pytest.raises(LandingNonTrovata) as e:
        c.contesto_ricerca("cardiologo", "pinco")
    assert e.value.cosa == "citta"


def test_specializzazione_ignota_riconosciuta_dal_redirect():
    c = ClientDoctolib(fetcher=FetcherFinto([
        _res("<html>homepage</html>", url_finale=f"{BASE_URL}/")]))
    with pytest.raises(LandingNonTrovata) as e:
        c.contesto_ricerca("inesistente", "milano")
    assert e.value.cosa == "specializzazione"


def test_citta_normalizzata_viene_accettata_e_registrata():
    """`reggio-nell-emilia` -> `provincia-di-reggio-emilia`: e' una risposta
    valida, non un errore, e da quel momento vale lo slug canonico."""
    c = ClientDoctolib(fetcher=FetcherFinto([_landing(
        citta="reggio-nell-emilia",
        url_finale=f"{BASE_URL}/cardiologo/provincia-di-reggio-emilia")]))
    ctx = c.contesto_ricerca("cardiologo", "reggio-nell-emilia")
    assert ctx.city_slug == "provincia-di-reggio-emilia"


def test_place_mancante_su_percorso_giusto_fallisce_rumorosamente():
    """Se la SPA cambiasse, meglio un errore esplicito che 0 risultati."""
    c = ClientDoctolib(fetcher=FetcherFinto([_res(
        "<html>niente</html>", url=f"{BASE_URL}/cardiologo/milano",
        url_finale=f"{BASE_URL}/cardiologo/milano")]))
    with pytest.raises(FetchError, match="window.place"):
        c.contesto_ricerca("cardiologo", "milano")


# --- paginazione ------------------------------------------------------------

@pytest.mark.parametrize("content_range,attesi", [
    ("0-19/611", (19, 611)),
    ("40-59/59", (59, 59)),
    (None, (None, None)),
    ("robaccia", (None, None)),
])
def test_lettura_content_range(content_range, attesi):
    assert _totale_da_content_range(content_range) == attesi


def _client_con_ricerca(content_range, medici=None):
    corpo = dict(RICERCA)
    if medici is not None:
        corpo["healthcareProviders"] = medici
    return ClientDoctolib(fetcher=FetcherFinto([
        _res(corpo, headers={"content-range": content_range})]))


def _ctx():
    return ClientDoctolib(fetcher=FetcherFinto([_landing()])
                          ).contesto_ricerca("cardiologo", "milano")


def test_pagina_intermedia_non_e_ultima():
    pag = _client_con_ricerca("0-19/611").cerca(_ctx(), 1)
    assert (pag.totale, pag.ultima) == (611, False)


def test_ultima_pagina_riconosciuta():
    """L'off-by-one qui e' la regressione piu' probabile: sbagliato di uno in un
    verso si scarica una pagina a vuoto, nell'altro si PERDE l'ultima."""
    pag = _client_con_ricerca("40-59/59").cerca(_ctx(), 3)
    assert pag.ultima is True


def test_pagina_piena_che_finisce_esatta_e_ultima():
    """`0-19/20`: venti risultati su venti. Non c'e' una pagina 2."""
    assert _client_con_ricerca("0-19/20").cerca(_ctx(), 1).ultima is True


def test_penultima_pagina_non_e_ultima():
    assert _client_con_ricerca("0-19/21").cerca(_ctx(), 1).ultima is False


def test_senza_content_range_si_usa_il_totale_del_body():
    """L'header e' la fonte migliore, ma il body porta `total`: se un giorno
    l'header sparisse, la paginazione deve continuare a fermarsi."""
    c = ClientDoctolib(fetcher=FetcherFinto([_res({"total": 20,
                                                   "healthcareProviders": []})]))
    assert c.cerca(_ctx(), 1).ultima is True


def test_numerazione_pagine_convertita_per_l_api():
    """Il progetto conta le pagine da 1, l'API da 0: la conversione sta in un
    punto solo e questo test e' cio' che la tiene li'."""
    f = FetcherFinto([_res(RICERCA, headers={"content-range": "20-39/611"})])
    ClientDoctolib(fetcher=f).cerca(_ctx(), 2)
    assert f.chiamate[0].endswith("?page=1")


def test_pagina_zero_e_un_errore_di_programmazione():
    with pytest.raises(ValueError):
        _client_con_ricerca("0-19/611").cerca(_ctx(), 0)


# --- slot -------------------------------------------------------------------

def test_slot_appiattiti_saltando_i_giorni_vuoti():
    corpo = json.loads((FIXTURES / "doctolib_availabilities.json")
                       .read_text(encoding="utf-8"))
    c = ClientDoctolib(fetcher=FetcherFinto([_res(corpo)]))
    assert c.slots(618888, 16021594, [2027771], giorni=7) == [
        {"data": "2026-07-22", "orario": "09:00"},
        {"data": "2026-07-22", "orario": "10:00"},
        {"data": "2026-07-24", "orario": "15:30"},
    ]


def test_nessuna_prenotazione_online_non_e_un_errore():
    corpo = json.loads((FIXTURES / "doctolib_availabilities_vuote.json")
                       .read_text(encoding="utf-8"))
    c = ClientDoctolib(fetcher=FetcherFinto([_res(corpo)]))
    assert c.slots(618888, 1, [2], giorni=7) == []


def test_senza_agende_non_si_chiama_nemmeno_l_endpoint():
    f = FetcherFinto([])          # qualunque richiesta farebbe fallire il test
    assert ClientDoctolib(fetcher=f).slots(1, 2, [], giorni=7) == []
    assert f.chiamate == []


def test_giorni_limitati_al_massimo_accettato():
    """Oltre 7 giorni l'endpoint risponde 400: chiedere di piu' non darebbe
    piu' slot, darebbe zero slot."""
    f = FetcherFinto([_res({"availabilities": []})])
    ClientDoctolib(fetcher=f).slots(1, 2, [3], giorni=30)
    assert "limit=7" in f.chiamate[0]


def test_agende_concatenate_con_trattino():
    f = FetcherFinto([_res({"availabilities": []})])
    ClientDoctolib(fetcher=f).slots(1, 2, [10, 11], giorni=5)
    assert "agenda_ids=10-11" in f.chiamate[0]


def test_errore_di_rete_sugli_slot_non_ferma_il_run():
    f = FetcherFinto([FetchError("HTTP 404", 404)])
    assert ClientDoctolib(fetcher=f).slots(1, 2, [3], giorni=5) == []


# --- rilevamento blocco -----------------------------------------------------

def test_html_normale_di_doctolib_non_e_un_blocco():
    """L'HTML vero contiene «verifica di sicurezza» in una traduzione: il
    rilevatore di MioDottore lo scambierebbe per un captcha a ogni richiesta,
    ed e' il motivo per cui questa sorgente ne porta uno suo."""
    assert rileva_blocco("<html>... verifica di sicurezza ...</html>") is False


@pytest.mark.parametrize("marcatore", [
    "/cdn-cgi/challenge-platform/h/b/orchestrate",
    "Attention Required! | Cloudflare",
    "captcha-delivery.com/captcha",
])
def test_marcatori_di_challenge_riconosciuti(marcatore):
    assert rileva_blocco(f"<html>{marcatore}</html>") is True
