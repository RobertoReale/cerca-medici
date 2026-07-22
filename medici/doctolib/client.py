"""Strato HTTP su Doctolib.it: header, endpoint, paginazione. Niente dominio.

Come funziona la ricerca su Doctolib (verificato con richieste reali, senza
browser headless):

1. **Landing page** `GET /{spec-slug}/{citta-slug}` — non contiene i medici (e'
   una SPA React) ma contiene gli identificativi che servono al passo 2, in due
   assegnazioni JavaScript: `window.keyword` e `window.place`.
2. **Ricerca** `POST /patient-health-search/api/v1/hcp/search?page=N` con quel
   `place` dentro il body. Nessuna autenticazione, nessun cookie: a differenza
   di MioDottore non serve estrarre alcun token. Risponde `206 Partial Content`
   e dichiara il totale nell'header `content-range: 0-19/611`.
3. **Disponibilita'** `GET /search/availabilities.json` — anche questa aperta.

Tre trappole verificate sul campo, tutte e tre gia' costate un debug:

- **Senza gli header `Sec-Fetch-*` e `sec-ch-ua` la landing risponde 404**, con
  una pagina "Doctolib 404" perfettamente formata. Non un 403, non un captcha:
  un 404 finto, che sembra «citta' inesistente». Per questo `HEADERS_HTML` non
  e' negoziabile.
- **L'oggetto `place` va rispedito integro.** Con una versione ridotta (senza
  `viewport`/`zipcodes`) il totale scende da 611 a 458: e' il viewport a
  definire l'area di ricerca. Non va ricostruito a mano.
- **`limit` degli slot e' il numero di GIORNI e il massimo e' 7**; chiedendone
  di piu' l'endpoint risponde 400.

*** AVVISO robots.txt ***
`/patient-health-search*` e `/*availabilities*` sono entrambi Disallow nel
robots.txt di Doctolib — stessa situazione di `/api/v3/` su MioDottore. Per
questo la raccolta disponibilita' resta OPT-IN e i tempi di attesa del `Fetcher`
non vanno abbassati (vedi README).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlparse

from ..fetcher import Fetcher, FetcherHTTP, FetchError

log = logging.getLogger("medici.doctolib.client")

BASE_URL = "https://www.doctolib.it"
URL_RICERCA = f"{BASE_URL}/patient-health-search/api/v1/hcp/search"
URL_SLOT = f"{BASE_URL}/search/availabilities.json"

# Quanti risultati per pagina restituisce l'API. Non e' configurabile lato
# client: lo dichiara il server nel `content-range`, e infatti `PaginaDoctolib`
# non usa questa costante per decidere se ha finito — la usa solo chi deve
# stimare il numero di pagine prima di iniziare.
PER_PAGINA = 20

# Massimo accettato da `availabilities.json`: oltre, HTTP 400 con
# "limit: must be less than or equal to 7".
MAX_GIORNI_SLOT = 7

# Header browser completi. Toglierne uno dei `Sec-Fetch-*` o `sec-ch-ua*` fa
# rispondere 404 alla landing (vedi il docstring del modulo).
HEADERS_HTML = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="126", "Not)A;Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

HEADERS_JSON = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Marcatori di una pagina di challenge. Il default di `Fetcher.looks_blocked` non
# va bene qui: usa la presenza delle card di MioDottore come prova che la pagina
# sia buona, e l'HTML di Doctolib contiene «verifica di sicurezza» dentro una
# traduzione — verrebbe scambiato per un captcha a ogni richiesta.
_MARCATORI_BLOCCO = (
    "/cdn-cgi/challenge-platform",   # Cloudflare Turnstile / JS challenge
    "cf-browser-verification",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "captcha-delivery.com",          # DataDome
    "/px/captcha",                   # PerimeterX
)


def rileva_blocco(html: str) -> bool:
    """True se la risposta e' una pagina di challenge invece del contenuto.

    Deliberatamente stretta: cerca solo marcatori che in una pagina normale non
    compaiono mai. Un falso positivo qui ferma l'intero run con `BlockedError`,
    che e' molto peggio di un blocco non riconosciuto (che si manifesta comunque
    come 0 risultati allo smoke test).
    """
    low = html.lower()
    return any(m in low for m in _MARCATORI_BLOCCO)


class LandingNonTrovata(FetchError):
    """Doctolib non ha riconosciuto la ricerca `/{spec}/{citta}`.

    `cosa` dice **quale dei due** pezzi non e' stato riconosciuto
    (`"specializzazione"`, `"citta"` o `"pagina"` quando non e' deducibile):
    e' l'informazione che permette a `slug.py` di correggere il pezzo giusto
    invece di tirare a indovinare.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 cosa: str = "pagina"):
        super().__init__(message, status)
        self.cosa = cosa


@dataclass
class ContestoDoctolib:
    """Cio' che serve per interrogare l'API, estratto dalla landing.

    `place` e `keyword` sono i dizionari *integri* letti dall'HTML: vanno
    rispediti come sono (vedi il docstring del modulo).
    """

    spec_slug: str
    city_slug: str
    keyword: dict = field(default_factory=dict)
    place: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.spec_slug}/{self.city_slug}"

    @property
    def nome_specializzazione(self) -> Optional[str]:
        return self.keyword.get("name")

    @property
    def nome_citta(self) -> Optional[str]:
        return self.place.get("name")


@dataclass
class PaginaDoctolib:
    """Una pagina di risultati piu' cio' che l'header `content-range` dichiara."""

    medici: List[dict]
    totale: Optional[int] = None
    ultima: bool = False


def _estrai_oggetto_js(html: str, variabile: str) -> Optional[dict]:
    """Legge `window.<variabile> = {...}` dall'HTML.

    Con `raw_decode` e non con un regex: sono oggetti annidati che contengono
    stringhe con graffe dentro (i messaggi tradotti), e qualunque regex
    ragionevole li troncherebbe a meta'.
    """
    marker = f"window.{variabile}"
    i = html.find(marker)
    if i < 0:
        return None
    uguale = html.find("=", i + len(marker))
    if uguale < 0:
        return None
    try:
        valore, _ = json.JSONDecoder().raw_decode(html[uguale + 1:].lstrip())
    except ValueError:
        return None
    return valore if isinstance(valore, dict) else None


def _totale_da_content_range(valore: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """`'0-19/611'` -> (fine=19, totale=611). (None, None) se illeggibile."""
    if not valore:
        return None, None
    try:
        intervallo, totale = valore.split("/")
        _, fine = intervallo.split("-")
        return int(fine), int(totale)
    except ValueError:
        log.warning("content-range illeggibile: %r", valore)
        return None, None


class ClientLanding(Protocol):
    """Quel poco di client che serve a `slug.risolvi`: la sola landing.

    La risoluzione degli slug e' la parte con piu' rami (redirect, alternative,
    sonda) ed e' testata con un client finto: dichiarare qui che le basta un
    metodo e' cio' che rende quel finto un'implementazione legittima e non un
    trucco da type checker.
    """

    def contesto_ricerca(self, spec_slug: str, city_slug: str,
                         /) -> "ContestoDoctolib": ...


class ClientDoctolib:
    """Chiamate HTTP a Doctolib. Una istanza per run: riusa la sessione."""

    def __init__(self, fetcher: Optional[FetcherHTTP] = None):
        # `rileva_blocco` nostro: quello di default e' tarato su MioDottore.
        self.fetcher: FetcherHTTP = fetcher or Fetcher(rileva_blocco=rileva_blocco)

    # -- passo 1: landing ----------------------------------------------------
    def contesto_ricerca(self, spec_slug: str, city_slug: str) -> ContestoDoctolib:
        """Scarica la landing e ne estrae `window.keyword` e `window.place`.

        **Doctolib non risponde 404 a una ricerca che non riconosce: redirige.**
        E' il comportamento piu' pericoloso incontrato su questo sito, perche'
        la risposta e' un 200 pieno di medici veri — semplicemente non quelli
        chiesti. Verificato:

        | richiesta                        | dove si finisce   | `window.place` |
        |----------------------------------|-------------------|----------------|
        | `/cardiologo/milano`             | invariata         | Milano         |
        | `/cardiologo/reggio-nell-emilia` | `/cardiologo/provincia-di-reggio-emilia` | quella provincia |
        | `/cardiologo/xyzzyqq`  (citta')  | `/cardiologo`     | **Milano**     |
        | `/inesistente/milano`  (spec.)   | `/`               | assente        |

        La terza riga e' il caso da cui difendersi: chiedendo «cardiologo a
        Pinco» si otterrebbero 611 cardiologi **milanesi** salvati nel DB sotto
        la citta' sbagliata. Per questo la diagnosi guarda il percorso finale
        dopo i redirect e non lo status:

        - due segmenti  -> tutto risolto (il secondo puo' essere la forma
          canonica della citta', come nella seconda riga: si accetta e si
          registra quella);
        - un segmento   -> la citta' non e' stata riconosciuta;
        - zero segmenti -> la specializzazione non e' stata riconosciuta.
        """
        url = f"{BASE_URL}/{spec_slug}/{city_slug}"
        try:
            res = self.fetcher.get(url, headers=HEADERS_HTML)
        except FetchError as e:
            # Il 404 vero non e' piu' il modo in cui il sito segnala "non
            # esiste", ma resta possibile (e senza gli header giusti e' anche
            # un 404 finto): lo trattiamo come non-diagnosticabile.
            if e.status == 404:
                raise LandingNonTrovata(f"404 su {url}", 404, "pagina") from None
            raise

        segmenti = [s for s in urlparse(res.url_finale or url).path.split("/") if s]
        if len(segmenti) < 2:
            cosa = "citta" if segmenti else "specializzazione"
            raise LandingNonTrovata(
                f"{url} rimanda a /{'/'.join(segmenti)}: Doctolib non ha "
                f"riconosciuto la {cosa}.", res.status, cosa)

        place = _estrai_oggetto_js(res.html, "place")
        keyword = _estrai_oggetto_js(res.html, "keyword")
        if not place:
            # Percorso giusto ma niente `window.place`: la SPA e' cambiata.
            # Fallimento rumoroso, perche' e' l'unico modo per accorgersene.
            raise FetchError(
                f"`window.place` non trovato in {res.url_finale or url}: la "
                "pagina e' cambiata oppure la risposta non e' quella attesa.",
                res.status)
        if segmenti[1] != city_slug:
            log.info("Doctolib ha normalizzato la citta' %r in %r (%s).",
                     city_slug, segmenti[1], place.get("name"))
        return ContestoDoctolib(spec_slug=segmenti[0], city_slug=segmenti[1],
                                keyword=keyword or {}, place=place)

    # -- passo 2: ricerca ----------------------------------------------------
    def cerca(self, contesto: ContestoDoctolib, pagina: int = 1) -> PaginaDoctolib:
        """Una pagina di risultati. `pagina` e' 1-based come nel resto del progetto.

        L'API la vuole invece 0-based: la conversione avviene qui e in un punto
        solo, cosi' l'off-by-one ha una sola occasione di esistere ed e' coperto
        da test.
        """
        if pagina < 1:
            raise ValueError(f"pagina={pagina}: la numerazione parte da 1")
        res = self.fetcher.post(
            f"{URL_RICERCA}?page={pagina - 1}",
            headers=HEADERS_JSON,
            json={"keyword": contesto.spec_slug,
                  "location": {"place": contesto.place},
                  "filters": {}},
        )
        dati = res.json()
        medici = dati.get("healthcareProviders") or []
        fine, totale = _totale_da_content_range(
            res.headers.get("content-range") or res.headers.get("Content-Range"))
        if totale is None:
            # Fallback sul body: `total` c'e' quasi sempre, ma l'header e' la
            # fonte piu' precisa perche' dice anche a che punto siamo.
            totale = dati.get("total")
        # `fine` e' l'indice dell'ultimo elemento servito, 0-based: la pagina e'
        # l'ultima quando quell'indice tocca il fondo. Senza header cadiamo sul
        # conteggio locale, che e' altrettanto corretto ma richiede di conoscere
        # quanti risultati abbiamo gia' visto.
        if totale is not None and fine is not None:
            ultima = fine + 1 >= totale
        elif totale is not None:
            ultima = pagina * PER_PAGINA >= totale
        else:
            ultima = not medici
        return PaginaDoctolib(medici=medici, totale=totale, ultima=ultima)

    # -- passo 3: disponibilita ----------------------------------------------
    def slots(self, practice_id, visit_motive_id, agenda_ids: Sequence,
              giorni: int = 5, start: Optional[datetime] = None) -> List[dict]:
        """Slot liberi, gia' appiattiti in `[{data, orario}, ...]`.

        Doctolib espone SOLO gli orari liberi (niente campo `booked` come
        MioDottore): quello che non c'e' e' occupato, e non abbiamo modo di
        distinguerlo da «agenda chiusa». Per questo la sorgente marca tutti gli
        slot come prenotabili.

        Un calendario che non esiste non e' un errore: si ritorna [].
        """
        if not agenda_ids:
            return []
        giorni = max(1, min(int(giorni), MAX_GIORNI_SLOT))
        start = start or datetime.now()
        params = {
            "telehealth": "false",
            "limit": giorni,
            "start_date_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "visit_motive_ids": visit_motive_id,
            # Le agende si concatenano con un trattino, non con la virgola.
            "agenda_ids": "-".join(str(a) for a in agenda_ids),
            "insurance_sector": "private",
            "practice_ids": practice_id,
        }
        url = URL_SLOT + "?" + "&".join(
            f"{k}={v}" for k, v in params.items() if v is not None)
        try:
            res = self.fetcher.get(url, headers=HEADERS_JSON)
            dati = res.json()
        except FetchError as e:
            log.debug("Nessuna disponibilita' per practice=%s motive=%s (%s)",
                      practice_id, visit_motive_id, e)
            return []
        fuori: List[dict] = []
        for giorno in dati.get("availabilities") or []:
            for iso in giorno.get("slots") or []:
                # "2026-07-22T09:00:00.000+02:00" -> ("2026-07-22", "09:00")
                if not isinstance(iso, str) or "T" not in iso:
                    continue
                data_g, resto = iso.split("T", 1)
                fuori.append({"data": data_g, "orario": resto[:5]})
        return fuori


def _stampa_prova(spec_slug: str, city_slug: str) -> int:
    """`python -m medici.doctolib.client cardiologo milano` — criterio di uscita
    della fase 1: verifica a occhio che i tre passi funzionino davvero."""
    from ..testo import console_utf8
    console_utf8()                            # console Windows: € e accenti
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    client = ClientDoctolib()
    ctx = client.contesto_ricerca(spec_slug, city_slug)
    print(f"landing: {ctx.url}")
    print(f"keyword: {ctx.nome_specializzazione} | place: {ctx.nome_citta} "
          f"({ctx.place.get('id')})")
    pag = client.cerca(ctx, 1)
    print(f"total={pag.totale} ultima={pag.ultima} n={len(pag.medici)}")
    for m in pag.medici[:3]:
        mv = m.get("matchedVisitMotive") or {}
        fee = (mv.get("fee") or {}).get("amount")
        print(f"  - {m.get('title','')} {m.get('firstName','')} {m.get('name','')}"
              f" — {mv.get('name')} — {fee if fee is not None else 'n/d'} €")
    return 0


if __name__ == "__main__":  # pragma: no cover - strumento di verifica manuale
    import sys
    if len(sys.argv) != 3:
        print("uso: python -m medici.doctolib.client <spec-slug> <citta-slug>")
        raise SystemExit(2)
    raise SystemExit(_stampa_prova(sys.argv[1], sys.argv[2]))
