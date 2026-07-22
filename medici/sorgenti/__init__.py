"""Astrazione "sorgente di dati": il contratto che ogni portale deve rispettare.

Perche' esiste
--------------
Fino a qui `scraper.py` conosceva MioDottore direttamente: chiamava
`fetcher.get_search_page()`, contava le card con un selettore CSS suo e
costruiva l'URL degli slot con il formato di quel sito. Aggiungere un secondo
portale (Doctolib, che non e' nemmeno HTML ma una API JSON) avrebbe significato
riempire lo scraper di `if`.

Qui separiamo le due cose:

- **Cosa cambia da sito a sito** — come si scarica una pagina, come si estraggono
  le righe, come si chiedono gli slot: sta dentro una `Sorgente`.
- **Cosa NON cambia** — paginazione, retry su pagina vuota, smoke test,
  annullamento, sostituzione dei dati vecchi, salvataggio: resta in `scraper.py`,
  scritto una volta sola.

Tutto il resto della pipeline (`storage`, `query_engine`, `cerca.py`, `app.py`)
ragiona su `Riga` e non sa nemmeno che le sorgenti esistano.

Come si aggiunge una sorgente
-----------------------------
1. Sottoclasse di `Sorgente` che implementa `url_ricerca` e `scarica_pagina`.
2. Se il sito espone le disponibilita', anche `chiave_slot` e `raccoglitore_slot`.
3. Una riga di `registra(...)` in fondo al modulo.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..parser import Riga

__all__ = [
    "Sorgente", "Pagina", "ChiaveSlot", "RaccoglitoreSlot",
    "registra", "ottieni", "elenco", "etichetta_di", "SorgenteSconosciuta",
    "RicercaNonValida",
]


class SorgenteSconosciuta(KeyError):
    """Nome di sorgente non registrato. Messaggio con l'elenco di quelli validi."""


class RicercaNonValida(ValueError):
    """La sorgente non sa esprimere questa (specializzazione, citta').

    Non e' un errore di rete e non ha senso ritentarla: i portali non usano gli
    stessi nomi (`dermatologo` su Doctolib si chiama `dermatologo-venereologo`)
    e non coprono le stesse citta'. Lo scraper la riporta come esito negativo
    della singola sorgente, senza fermare le altre — il messaggio e' rivolto
    all'utente e deve dire cosa scrivere invece.
    """


@dataclass
class Pagina:
    """Il risultato di UNA pagina di ricerca, in forma indipendente dal sito.

    `n_schede` e `righe` sono numeri diversi apposta: su MioDottore una scheda
    (un medico) puo' produrre piu' righe, una per indirizzo. Lo scraper usa
    `n_schede` per decidere se la paginazione e' finita, perche' e' quello il
    segnale che il sito ha davvero smesso di dare risultati.
    """

    righe: List[Riga] = field(default_factory=list)
    n_schede: int = 0
    # Opaco, deciso dalla sorgente: MioDottore ci mette l'HTML (contiene
    # l'ACCESS_TOKEN per gli slot), un client JSON ci mettera' i suoi parametri
    # di sessione. Lo scraper lo trasporta e basta, non lo interpreta.
    contesto: Any = None
    # True quando la sorgente SA gia' che non ci sono altre pagine (p.es. un
    # header `content-range: 40-59/59`). Evita la richiesta "a vuoto" di
    # conferma che su HTML e' invece inevitabile.
    ultima: bool = False


@dataclass(frozen=True)
class ChiaveSlot:
    """Come si identifica il calendario di una riga, per chiedere gli slot.

    `result_id`/`address_id` sono comuni a tutti (servono a `storage` per legare
    gli slot alla riga giusta). `dati` e' lo spazio per quello che serve solo a
    una sorgente: MioDottore non usa nulla, Doctolib deve passare
    `visit_motive_id` e `agenda_ids`.

    Frozen perche' lo scraper deduplica le chiavi con `dict.fromkeys()`: senza
    hashability lo stesso indirizzo verrebbe interrogato una volta per riga.
    """

    result_id: str
    address_id: str
    dati: tuple = ()


class RaccoglitoreSlot(ABC):
    """Recupera gli slot di un calendario. Vive quanto un singolo run."""

    @abstractmethod
    def slots(self, chiave: ChiaveSlot, giorni: int) -> List[dict]:
        """Slot normalizzati per `chiave`, o [] se quel calendario non esiste.

        Ogni slot e' un dict con almeno: result_id, address_id, data (YYYY-MM-DD),
        orario (HH:MM), prenotabile (bool). Un calendario assente NON e' un
        errore: si ritorna [].
        """


class Sorgente(ABC):
    """Un portale interrogabile. Un'istanza per run (puo' tenere stato/sessione)."""

    #: Identificatore stabile, finisce nel DB nella colonna `sorgente`.
    nome: str = ""
    #: Come si chiama per un essere umano (CLI, GUI).
    etichetta: str = ""
    #: False = `--disponibilita` viene ignorato con un avviso, non fallisce.
    supporta_disponibilita: bool = False

    @abstractmethod
    def url_ricerca(self, specializzazione: str, citta: str) -> str:
        """URL leggibile della ricerca, da mostrare all'utente prima di partire."""

    @abstractmethod
    def scarica_pagina(self, specializzazione: str, citta: str, page: int) -> Pagina:
        """Scarica e interpreta la pagina `page` (1-based).

        Puo' sollevare `BlockedError` (lo scraper si ferma senza ritentare) o
        `FetchError` (errore di rete gia' ritentato).
        """

    # -- disponibilita (opzionali) ------------------------------------------
    def chiave_slot(self, riga: Riga) -> Optional[ChiaveSlot]:
        """Chiave del calendario di `riga`, o None se quella riga non ne ha uno.

        Il default None significa "questa sorgente non fa disponibilita'".
        Filtrare qui chi non ha agenda online evita centinaia di richieste
        destinate a un 404.
        """
        return None

    def raccoglitore_slot(
        self,
        contesto: Any,
        *,
        ricarica_contesto: Optional[Callable[[], Any]] = None,
    ) -> Optional[RaccoglitoreSlot]:
        """Costruisce il raccoglitore dal `contesto` dell'ultima pagina scaricata.

        `ricarica_contesto` riscarica la pagina 1 e ne ritorna il contesto
        fresco: serve a chi ha credenziali che scadono a meta' run.
        Ritorna None se il contesto non basta (p.es. token non trovato).
        """
        return None


# --- registro ---------------------------------------------------------------
# CLI e GUI non devono avere un elenco di sorgenti scritto a mano: lo leggono da
# qui, cosi' registrarne una nuova la fa comparire ovunque senza altre modifiche.

_REGISTRO: Dict[str, Callable[..., Sorgente]] = {}
_ETICHETTE: Dict[str, str] = {}


def registra(nome: str, costruttore: Callable[..., Sorgente], etichetta: str) -> None:
    _REGISTRO[nome] = costruttore
    _ETICHETTE[nome] = etichetta


def elenco() -> List[str]:
    """Nomi registrati, in ordine di registrazione (il primo e' il default)."""
    return list(_REGISTRO)


def etichetta_di(nome: str) -> str:
    return _ETICHETTE.get(nome, nome)


def ottieni(nome: str, **kwargs) -> Sorgente:
    """Istanzia una sorgente per nome. `kwargs` vanno al costruttore concreto."""
    try:
        costruttore = _REGISTRO[nome]
    except KeyError:
        raise SorgenteSconosciuta(
            f"sorgente {nome!r} sconosciuta; disponibili: "
            f"{', '.join(_REGISTRO) or '(nessuna)'}") from None
    return costruttore(**kwargs)


# Gli import in fondo evitano il ciclo: i moduli delle sorgenti importano da qui.
# Non si usa il nome importato: serve l'effetto collaterale, cioe' la `registra`
# in fondo a ciascun modulo. L'ORDINE conta: `elenco()[0]` e' la sorgente di
# default della CLI.
from . import miodottore  # noqa: E402,F401  # pyright: ignore[reportUnusedImport]
from . import doctolib    # noqa: E402,F401  # pyright: ignore[reportUnusedImport]
