"""Regressioni sul Fetcher: encoding, brotli, retry."""
import importlib.util
import time
from typing import Optional

import pytest
import requests

from medici.fetcher import ACCEPT_ENCODING, RETRY_STATUS, Fetcher, FetchError


def _resp(body: bytes, content_type: str, status: int = 200,
          headers: Optional[dict] = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers["Content-Type"] = content_type
    r.headers.update(headers or {})
    r.url = "http://x"
    return r


def test_decode_forza_utf8_senza_charset():
    """Senza charset requests ripiega su ISO-8859-1 e gli accenti diventano mojibake."""
    body = "Città – 50 €".encode("utf-8")
    assert Fetcher._decode(_resp(body, "text/html")) == "Città – 50 €"


def test_decode_rispetta_charset_dichiarato():
    body = "Città".encode("utf-8")
    assert Fetcher._decode(_resp(body, "text/html; charset=utf-8")) == "Città"


def test_brotli_annunciato_solo_se_supportato():
    """Annunciare `br` senza saperlo decomprimere restituisce byte illeggibili."""
    supportato = bool(importlib.util.find_spec("brotli")
                      or importlib.util.find_spec("brotlicffi"))
    assert ("br" in ACCEPT_ENCODING) == supportato


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_temporanei_sono_ritentati(status):
    assert status in RETRY_STATUS


def test_nessuna_attesa_dopo_ultimo_tentativo(monkeypatch):
    """Prima si dormiva anche dopo l'ultimo retry: ~30s buttati per URL."""
    dormite = []
    monkeypatch.setattr(time, "sleep", dormite.append)
    f = Fetcher(min_delay=0, max_delay=0, max_retries=3)
    monkeypatch.setattr(f.session, "request",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    with pytest.raises(FetchError):
        f.get("http://x")
    assert len(dormite) == 2, f"attese {dormite} per 3 tentativi"


# --- POST (serve alle API di ricerca JSON) ----------------------------------

def _fetcher_che_registra(monkeypatch, risposte):
    """Fetcher senza attese, che annota metodo/url/body di ogni chiamata."""
    f = Fetcher(min_delay=0, max_delay=0, max_retries=3)
    chiamate = []

    def finto(metodo, url, **kw):
        chiamate.append((metodo, url, kw.get("json"), kw.get("headers")))
        r = risposte.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(f.session, "request", finto)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return f, chiamate


def test_post_manda_il_body_json(monkeypatch):
    f, chiamate = _fetcher_che_registra(monkeypatch, [_resp(b"{}", "application/json")])
    f.post("http://x", json={"keyword": "cardiologo"})
    metodo, url, body, _ = chiamate[0]
    assert (metodo, url, body) == ("POST", "http://x", {"keyword": "cardiologo"})


def test_post_ritenta_come_il_get(monkeypatch):
    """Il motivo per cui `post` non e' una chiamata diretta a `requests`: senza
    la stessa catena di retry, un 429 su una pagina di ricerca perderebbe venti
    medici e nessuno se ne accorgerebbe."""
    f, chiamate = _fetcher_che_registra(monkeypatch, [
        _resp(b"", "application/json", 429),
        _resp(b'{"ok":1}', "application/json"),
    ])
    res = f.post("http://x", json={})
    assert [c[0] for c in chiamate] == ["POST", "POST"]
    assert res.json() == {"ok": 1}


def test_post_rispetta_il_rate_limiting(monkeypatch):
    """Il ritardo tra richieste e' condiviso con i GET: e' quello che tiene il
    traffico due ordini di grandezza sotto il rate limit dichiarato dai siti."""
    f, _ = _fetcher_che_registra(monkeypatch, [_resp(b"{}", "application/json")] * 2)
    f.min_delay = f.max_delay = 99.0
    atteso = []
    monkeypatch.setattr(time, "sleep", atteso.append)
    f.get("http://x")
    f.post("http://x", json={})
    assert atteso, "la seconda richiesta e' partita senza attendere"


def test_headers_della_risposta_disponibili(monkeypatch):
    """`content-range` vive negli header: senza, la paginazione di Doctolib non
    saprebbe mai quando fermarsi."""
    f, _ = _fetcher_che_registra(monkeypatch, [
        _resp(b"{}", "application/json", headers={"content-range": "0-19/611"})])
    assert f.post("http://x").headers["content-range"] == "0-19/611"


def test_url_finale_registrato(monkeypatch):
    """Doctolib segnala una citta' sconosciuta con un redirect, non con un 404:
    senza l'URL di arrivo la differenza sarebbe invisibile."""
    resp = _resp(b"{}", "text/html")
    resp.url = "http://x/altrove"
    f, _ = _fetcher_che_registra(monkeypatch, [resp])
    assert f.get("http://x").url_finale == "http://x/altrove"


def test_json_su_risposta_non_json_e_esplicito(monkeypatch):
    """Un endpoint JSON che risponde HTML e' quasi sempre una pagina d'errore:
    meglio un errore qui che un KeyError dieci righe piu' in la'."""
    f, _ = _fetcher_che_registra(monkeypatch, [_resp(b"<html>", "text/html")])
    with pytest.raises(FetchError, match="non JSON"):
        f.post("http://x").json()


def test_rilevatore_di_blocco_sostituibile(monkeypatch):
    """Ogni sito ha i suoi marcatori: quello di MioDottore, applicato a
    Doctolib, darebbe un falso positivo a ogni richiesta."""
    from medici.fetcher import BlockedError
    f, _ = _fetcher_che_registra(monkeypatch, [_resp(b"tutto bene", "text/html")])
    f.rileva_blocco = lambda html: "tutto bene" in html
    with pytest.raises(BlockedError):
        f.get("http://x")
