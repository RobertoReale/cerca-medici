"""Fase 6 — lo stesso medico su due portali diventa una riga sola.

Il problema
-----------
Non esiste **nessun identificativo condiviso** tra MioDottore e Doctolib: il
`result_id` 1620432 sull'uno e sull'altro sono due persone diverse. L'unica cosa
che entrambi pubblicano per ogni sede e' la posizione, e quella e' confrontabile.

    Due righe sono lo stesso medico se il **cognome normalizzato coincide**
    *e* le due sedi distano **meno di 150 m**.

Servono entrambi i criteri: solo il cognome sbaglia sugli omonimi (a Milano ce
ne sono), solo la geografia fonderebbe in uno i trenta medici di un
poliambulatorio.

Perche' non si comprime e basta
-------------------------------
La regola e' **unificare le righe, non i dati**: il cluster mostra il prezzo
minimo, ma i prezzi dei singoli portali restano visibili accanto
(`disponibile_su`). Tenere solo un prezzo «rappresentativo» butterebbe via
proprio l'informazione per cui si cerca su due portali:

1. **dove** si prenota a quel prezzo — un minimo senza il portale che lo pratica
   non e' azionabile;
2. **che i due portali non concordano** — venti euro di scarto sullo stesso
   medico sono un segnale (listini non allineati, servizi diversi con lo stesso
   nome, un prezzo vecchio da una parte).

Per lo stesso motivo, se un portale non pubblica il prezzo il cluster dice
«Doctolib n/d» e non riempie il buco con il valore dell'altro.

Perche' non e' una colonna nel database
---------------------------------------
Il clustering dipende da quali fonti l'utente ha selezionato e da quali dati
sono freschi: calcolarlo a ogni query e' insieme piu' semplice e piu' corretto
che mantenerlo salvato. Il costo e' trascurabile perche' le righe si indicizzano
per cella geografica invece di confrontarle a coppie: su migliaia di righe si
resta lineari.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .query_engine import haversine_km

# Le due piattaforme geocodificano lo stesso indirizzo con scarti di qualche
# decina di metri (stesso portone, coordinate diverse): 150 m li assorbe senza
# arrivare all'edificio accanto. E' il numero da ritarare se i cluster
# sbagliano, ed e' l'unico parametro davvero empirico di questo modulo.
SOGLIA_METRI = 150.0

# Le celle si dimensionano SULLA soglia, non su un numero fisso di decimali.
# Con celle piu' piccole della soglia, due righe entro la distanza buona possono
# finire a due celle di distanza e i vicini immediati non le vedono: il duplicato
# non viene riconosciuto e nessuno se ne accorge, perche' il risultato sbagliato
# (due righe invece di una) e' anche quello che si vede senza unificazione.
_METRI_PER_GRADO_LAT = 111_320.0
# Un grado di longitudine vale meno di uno di latitudine, e sempre meno man mano
# che si sale. 0,65 e' il coseno a ~49°N: sopra tutta l'Italia, quindi le celle
# in longitudine non sono mai piu' strette della soglia. Sbagliare per eccesso
# costa qualche confronto in piu'; sbagliare per difetto perde duplicati.
_COS_MINIMO = 0.65

# Titoli da togliere prima di confrontare i nomi. MioDottore scrive «Dott.ssa»,
# Doctolib «Dott.» nello stesso campo `title`: senza questa lista lo stesso
# medico avrebbe due cognomi diversi.
_TITOLI = {"dott", "dottssa", "dottsa", "dr", "drssa", "dra", "prof", "profssa",
           "sig", "sigra", "med", "chir"}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _senza_accenti(testo: str) -> str:
    testo = unicodedata.normalize("NFKD", testo)
    return "".join(c for c in testo if not unicodedata.combining(c))


def parole_nome(nome: Optional[str]) -> List[str]:
    """Il nome ridotto alle parole che lo identificano: niente titoli, niente
    accenti, niente punteggiatura, tutto minuscolo."""
    if not nome:
        return []
    pulito = _NON_ALNUM.sub(" ", _senza_accenti(nome).lower())
    return [p for p in pulito.split() if p and p not in _TITOLI]


def chiave_nome(nome: Optional[str], tipo: Optional[str] = None) -> Optional[str]:
    """La parte del nome su cui si confrontano due righe.

    Per un professionista e' il **cognome**, cioe' l'ultima parola: MioDottore
    da' il nome intero in un campo solo, Doctolib lo da' spezzato, e l'ultima
    parola e' l'unico pezzo che le due fonti scrivono allo stesso modo.

    Per una struttura (`tipo == "clinic"`) e' invece il nome **intero**: due
    poliambulatori diversi nello stesso isolato possono finire entrambi con
    «Marco» o «Milano», e confrontare solo l'ultima parola li fonderebbe.
    """
    parole = parole_nome(nome)
    if not parole:
        return None
    return " ".join(parole) if tipo == "clinic" else parole[-1]


def _passi(soglia_metri: float) -> Tuple[float, float]:
    """Lato della cella in gradi (latitudine, longitudine), mai < soglia."""
    lat = soglia_metri / _METRI_PER_GRADO_LAT
    return lat, lat / _COS_MINIMO


def _cella(lat: float, lng: float, passi: Tuple[float, float]) -> Tuple[int, int]:
    return (int(lat // passi[0]), int(lng // passi[1]))


def _celle_vicine(lat: float, lng: float, passi: Tuple[float, float]
                  ) -> Iterable[Tuple[int, int]]:
    """La cella e le 8 attorno. Bastano proprio perche' il lato e' >= soglia:
    due punti abbastanza vicini da fondersi non possono cadere a due celle di
    distanza."""
    y, x = _cella(lat, lng, passi)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yield (y + dy, x + dx)


def _prezzo_ordinabile(riga: dict) -> tuple:
    """Chiave per «il prezzo piu' basso, i mancanti in fondo»."""
    p = riga.get("prezzo")
    return (p is None, p if p is not None else 0.0,
            -(riga.get("n_recensioni") or 0))


def _formatta_prezzo(riga: dict) -> str:
    p = riga.get("prezzo")
    if p is None:
        return "n/d"
    return f"{'da ' if riga.get('prezzo_e_da_min') else ''}{p:g} €"


def raggruppa(righe: Sequence[dict], soglia_metri: float = SOGLIA_METRI
              ) -> List[List[dict]]:
    """Le righe divise in cluster (liste), nell'ordine di ingresso.

    Un cluster contiene **al massimo una riga per portale**: due righe della
    stessa fonte con lo stesso medico sono servizi diversi allo stesso indirizzo
    (e' il modello di MioDottore: una riga per indirizzo/servizio), e fonderle
    cancellerebbe proprio i prezzi che le distinguono.
    """
    cluster: List[List[dict]] = []
    # cella -> indici dei cluster che hanno almeno una riga li' dentro.
    indice: Dict[Tuple[int, int], List[int]] = {}
    passi = _passi(soglia_metri)

    for riga in righe:
        lat, lng = riga.get("lat"), riga.get("lng")
        chiave = chiave_nome(riga.get("nome"), riga.get("tipo"))
        trovato = None
        if chiave is not None and lat is not None and lng is not None:
            visti = set()
            for cel in _celle_vicine(lat, lng, passi):
                for i in indice.get(cel, ()):
                    if i in visti:
                        continue
                    visti.add(i)
                    capo = cluster[i][0]
                    if chiave_nome(capo.get("nome"), capo.get("tipo")) != chiave:
                        continue
                    if any(r.get("sorgente") == riga.get("sorgente")
                           for r in cluster[i]):
                        continue
                    d = haversine_km(lat, lng, capo.get("lat"), capo.get("lng"))
                    if d is not None and d * 1000 <= soglia_metri:
                        trovato = i
                        break
                if trovato is not None:
                    break
        if trovato is None:
            cluster.append([riga])
            trovato = len(cluster) - 1
        else:
            cluster[trovato].append(riga)
        if lat is not None and lng is not None:
            indice.setdefault(_cella(lat, lng, passi), []).append(trovato)
    return cluster


def _fondi(gruppo: Sequence[dict], etichetta) -> dict:
    """Un cluster -> la riga da mostrare.

    Il rappresentante e' la riga **col prezzo piu' basso** (i senza prezzo per
    ultimi, a parita' vince chi ha piu' recensioni): cosi' la colonna «Prezzo»
    e' gia' il minimo del cluster senza mescolare i campi di righe diverse, e
    l'ordinamento per prezzo e' automaticamente quello giusto.

    Rating e recensioni restano **quelli del rappresentante**, non una media:
    sono conteggi di due comunita' di utenti distinte e sommarli o mediarli
    inventerebbe un numero che nessuno dei due portali pubblica.
    """
    ordinate = sorted(gruppo, key=_prezzo_ordinabile)
    fuso = dict(ordinate[0])
    # Prima disponibilita': qui il minimo ha senso e non inventa nulla — e' la
    # data piu' vicina in cui quel medico e' prenotabile *da qualche parte*.
    prime = [d for d in (r.get("prima_disponibilita") for r in gruppo) if d]
    if prime:
        fuso["prima_disponibilita"] = min(prime)
    fuso["n_fonti"] = len({r.get("sorgente") for r in gruppo})
    # Ordine stabile e leggibile: prima il portale piu' economico, cioe' lo
    # stesso ordine con cui e' stato scelto il rappresentante.
    fuso["disponibile_su"] = " · ".join(
        f"{etichetta(r.get('sorgente') or '')} {_formatta_prezzo(r)}"
        for r in ordinate)
    return fuso


def unifica(righe: Sequence[dict], soglia_metri: float = SOGLIA_METRI,
            etichetta=None) -> List[dict]:
    """Le righe con i duplicati cross-portale fusi. Non modifica gli originali.

    Ogni riga del risultato ha in piu' `n_fonti` e `disponibile_su`; le righe
    rimaste sole le hanno comunque (con `n_fonti = 1`), cosi' la tabella non ha
    colonne che appaiono e scompaiono a seconda dei dati.
    """
    if etichetta is None:
        from .sorgenti import etichetta_di
        etichetta = etichetta_di
    return [_fondi(g, etichetta) for g in raggruppa(righe, soglia_metri)]
