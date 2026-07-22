"""Fase 2 — Fetcher con resilienza.

Requisiti (dal piano):
- Rate limiting randomizzato 1-3s tra richieste (non fisso).
- Retry con backoff esponenziale su 429/503/timeout.
- User-Agent realistico Chrome desktop.
- Rilevamento blocco soft: HTTP 200 ma 0 card / marcatore captcha reale.
- Costruzione URL nella forma permessa `/{specializzazione}/{citta}` (no `/cerca?`).
"""
from __future__ import annotations

import importlib.util
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import requests

from .testo import slugify

log = logging.getLogger("medici.fetcher")

BASE_URL = "https://www.miodottore.it"

# Annunciamo `br` SOLO se sappiamo decomprimerlo: requests supporta brotli
# soltanto con `brotli`/`brotlicffi` installati. Dichiararlo senza averlo
# significa ricevere byte compressi che `resp.text` decodifica in spazzatura:
# 0 card, nessun errore, e lo scraper conclude "fine paginazione".
_HAS_BROTLI = bool(importlib.util.find_spec("brotli")
                   or importlib.util.find_spec("brotlicffi"))
ACCEPT_ENCODING = "gzip, deflate, br" if _HAS_BROTLI else "gzip, deflate"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept-Encoding": ACCEPT_ENCODING,
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Status su cui vale la pena riprovare: rate limit e guasti temporanei lato
# server (prima erano solo 429/503, ma un 502 dietro CDN e' altrettanto comune).
RETRY_STATUS = (429, 500, 502, 503, 504)


class BlockedError(RuntimeError):
    """Sollevata quando si rileva un blocco soft (captcha / 0 risultati anomali)."""


class FetchError(RuntimeError):
    """Errore di rete non recuperabile dopo i retry.

    `status` porta il codice HTTP quando ne esiste uno (None per timeout e
    errori di connessione): serve a distinguere un 401 — token scaduto, si puo'
    rinnovare e riprovare — da un 404, che invece e' definitivo.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


slugify_city = slugify  # alias storico: l'implementazione vive in testo.py


def build_search_url(specializzazione: str, citta: str, page: int = 1) -> str:
    """URL di ricerca permesso da robots.txt: /{specializzazione}/{citta}?page=N."""
    spec = slugify_city(specializzazione)
    city = slugify_city(citta)
    url = f"{BASE_URL}/{spec}/{city}"
    if page and page > 1:
        url += f"?page={page}"
    return url


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    from_cache: bool = False
    # URL a cui si e' arrivati davvero. Diverso da `url` quando il sito
    # redirige, ed e' un'informazione che *significa* qualcosa: Doctolib manda
    # una citta' che non conosce su `/{specializzazione}` senza dirlo, e senza
    # questo campo la differenza sarebbe invisibile (vedi `doctolib/client.py`).
    url_finale: str = ""
    # Header della risposta. Servono a chi pagina su un'API: Doctolib dichiara il
    # totale dei risultati in `content-range: 0-19/611`, cioe' fuori dal body.
    headers: dict = field(default_factory=dict)

    def json(self):
        """Body decodificato come JSON. Solleva `FetchError` se non lo e'.

        Un endpoint JSON che risponde HTML e' quasi sempre una pagina di errore
        o di challenge: meglio un errore esplicito qui che un `KeyError` dieci
        righe piu' in la' su un dizionario che non esiste.
        """
        import json as _json
        try:
            return _json.loads(self.html)
        except ValueError as e:
            raise FetchError(f"risposta non JSON da {self.url}: {e}",
                             self.status) from None


# --- cosa serve davvero, a chi riceve un fetcher ----------------------------
# I moduli che ricevono un fetcher iniettato non usano la classe `Fetcher`: ne
# usano UNO o DUE metodi. Dichiararlo con dei Protocol dice esattamente quali,
# e rende legittimi (anche per il type checker) i fetcher finti dei test, che
# esistono proprio per non toccare la rete.
#
# I parametri sono posizionali-solo: chi implementa il protocollo e' libero di
# chiamarli come vuole (`spec` invece di `specializzazione`), ed e' cio' che
# rende il contratto sul comportamento e non sui nomi.

class FetcherGet(Protocol):
    """Il minimo: una GET. Basta a chi interroga un endpoint gia' noto."""

    def get(self, url: str, /, *, expect_cards: bool = ...,
            headers: Optional[dict] = ...) -> "FetchResult": ...


class FetcherHTTP(FetcherGet, Protocol):
    """GET + POST: le API di ricerca JSON vogliono un POST."""

    def post(self, url: str, /, *, json: Optional[dict] = ...,
             headers: Optional[dict] = ...) -> "FetchResult": ...


class FetcherRicerca(FetcherGet, Protocol):
    """GET + la pagina di ricerca di MioDottore, con l'URL costruito dentro."""

    def get_search_page(self, specializzazione: str, citta: str,
                        page: int = ..., /) -> "FetchResult": ...


class Fetcher:
    def __init__(
        self,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        max_retries: int = 4,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
        rileva_blocco: Optional[Callable[[str], bool]] = None,
    ):
        """`rileva_blocco`: come si riconosce una pagina di challenge su QUESTO
        sito. Il default (`looks_blocked`) e' tarato su MioDottore e usa la
        presenza delle card come prova che la pagina e' buona: applicato a un
        altro portale darebbe falsi positivi (l'HTML di Doctolib contiene la
        stringa «verifica di sicurezza» in una traduzione, senza essere un
        captcha). Ogni sorgente porta quindi il proprio.
        """
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.rileva_blocco = rileva_blocco or self.looks_blocked
        self._last_request_ts: Optional[float] = None

    # -- rate limiting -------------------------------------------------------
    def _respect_rate_limit(self) -> None:
        if self._last_request_ts is None:
            return
        wait = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last_request_ts
        remaining = wait - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # -- blocco soft ---------------------------------------------------------
    @staticmethod
    def looks_blocked(html: str) -> bool:
        """Blocco soft reale: pagina di challenge captcha, non la config statica.

        La config `RECAPTCHA_SITE_KEY` e' SEMPRE presente nell'HTML normale, quindi
        non e' un segnale. Cerchiamo invece marcatori di una pagina di challenge.
        """
        low = html.lower()
        markers = (
            "captcha-delivery.com",       # DataDome
            "g-recaptcha",                 # widget recaptcha renderizzato
            "verifica di sicurezza",
            "unusual traffic",
            "/px/captcha",                 # PerimeterX
        )
        if any(m in low for m in markers) and "data-id=\"result-item\"" not in low:
            return True
        return False

    # -- fetch con retry -----------------------------------------------------
    def get(self, url: str, *, expect_cards: bool = False,
            headers: Optional[dict] = None) -> FetchResult:
        """`headers` sono extra *per singola richiesta*: non sporcano la sessione
        condivisa (l'Authorization degli slot non deve finire sulle pagine HTML)."""
        return self._richiedi("GET", url, headers=headers,
                              expect_cards=expect_cards)

    def post(self, url: str, *, json: Optional[dict] = None,
             headers: Optional[dict] = None) -> FetchResult:
        """POST con la STESSA catena di rate limiting, retry e backoff del GET.

        Esiste perche' le API di ricerca JSON (Doctolib) vogliono un POST: senza,
        l'unica alternativa sarebbe chiamare `requests` a mano, cioe' rinunciare
        proprio alle protezioni per cui questo modulo esiste.
        """
        return self._richiedi("POST", url, headers=headers, json=json)

    def _richiedi(self, metodo: str, url: str, *,
                  headers: Optional[dict] = None,
                  json: Optional[dict] = None,
                  expect_cards: bool = False) -> FetchResult:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._respect_rate_limit()
            try:
                resp = self.session.request(metodo, url, timeout=self.timeout,
                                            headers=headers, json=json)
                self._last_request_ts = time.monotonic()
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if not self._sleep_before_retry(attempt, url, f"timeout/conn: {e}"):
                    break
                continue

            if resp.status_code in RETRY_STATUS:
                last_exc = FetchError(f"HTTP {resp.status_code}", resp.status_code)
                if not self._sleep_before_retry(attempt, url,
                                                f"HTTP {resp.status_code}", resp):
                    break
                continue

            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} su {url}", resp.status_code)

            html = self._decode(resp)
            if self.rileva_blocco(html):
                raise BlockedError(
                    f"Rilevato blocco/captcha su {url} — interrompo invece di "
                    "ritentare all'infinito. Attendi o cambia IP/User-Agent.")

            if expect_cards and "data-id=\"result-item\"" not in html:
                # 200 ma nessuna card dove ce ne aspettiamo: sospetto blocco soft.
                log.info("0 card su %s (HTTP 200) — possibile fine pagine o blocco.", url)

            return FetchResult(url=url, status=resp.status_code, html=html,
                               url_finale=resp.url or url,
                               headers=dict(resp.headers))

        raise FetchError(f"Fallito dopo {self.max_retries} tentativi: {url}",
                         getattr(last_exc, "status", None)) from last_exc

    def _sleep_before_retry(self, attempt: int, url: str, motivo: str,
                            resp: Optional[requests.Response] = None) -> bool:
        """Attende prima del prossimo tentativo. False = era l'ultimo, non attendere.

        Prima si dormiva anche dopo il tentativo finale: fino a ~30s buttati per
        ogni URL destinato comunque a fallire.
        """
        if attempt >= self.max_retries:
            log.warning("%s su %s: esauriti i %d tentativi.",
                        motivo, url, self.max_retries)
            return False
        backoff = self._backoff(attempt, resp)
        log.warning("%s su %s (tentativo %d/%d): retry tra %.1fs",
                    motivo, url, attempt, self.max_retries, backoff)
        time.sleep(backoff)
        return True

    @staticmethod
    def _decode(resp: requests.Response) -> str:
        """Decodifica il body forzando UTF-8 se il server non dichiara il charset.

        Senza `charset` nel Content-Type requests ripiega su ISO-8859-1 (regola
        HTTP/1.1), che su queste pagine trasforma accenti ed € in mojibake.
        """
        if resp.encoding is None or "charset" not in (
                resp.headers.get("Content-Type", "").lower()):
            resp.encoding = "utf-8"
        return resp.text

    def _backoff(self, attempt: int, resp: Optional[requests.Response] = None) -> float:
        if resp is not None and resp.headers.get("Retry-After"):
            try:
                return float(resp.headers["Retry-After"])
            except ValueError:
                pass
        base = min(2 ** attempt, 30)
        return base + random.uniform(0, 1.5)

    def get_search_page(self, specializzazione: str, citta: str, page: int = 1) -> FetchResult:
        url = build_search_url(specializzazione, citta, page)
        return self.get(url, expect_cards=True)
