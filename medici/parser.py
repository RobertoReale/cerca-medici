"""Fase 3 — Parser anagrafica + prezzi.

Estrae una riga *per indirizzo* (non per medico) da ogni card di risultato
delle pagine di ricerca di MioDottore. Un medico con N indirizzi produce N
righe, ciascuna con il proprio prezzo/servizio (design deciso nel piano).

Il punto piu' fragile e' `parse_prezzo`: il separatore tra numero ed euro e'
un NON-BREAKING SPACE (\\xa0), non uno spazio normale. Coperto da test unitari.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

from .testo import slugify

# --- Normalizzazione prezzo -------------------------------------------------

# Numero italiano: "1.234,50" o "50" o "1234". Catturiamo cifre con separatori.
_PRICE_NUM_RE = re.compile(r"(\d[\d.\s ]*(?:,\d+)?)")


def _to_float(raw_num: str) -> Optional[float]:
    """'1.234,50' -> 1234.5 ; '50' -> 50.0 . Gestisce sep. migliaia/decimali IT."""
    s = raw_num.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    # Formato italiano: '.' migliaia, ',' decimali.
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# Sotto questa cifra un prezzo non e' un prezzo: e' il segnaposto che alcuni
# medici mettono per non pubblicare il listino. Visto sul campo: «1 €» su
# MioDottore per visita + ECG + ecocolordoppler, che lo stesso medico su
# Doctolib fa pagare 200 €. Preso per buono, finisce in cima a ogni ordinamento
# per prezzo — proprio la funzione per cui esiste il progetto. Lo 0 resta: e'
# «Prestazione gratuita», un'informazione vera.
PREZZO_SIMBOLICO_SOTTO = 5.0


def prezzo_plausibile(valore: Optional[float]) -> Optional[float]:
    """`valore`, oppure None se e' un prezzo simbolico (vedi sopra).

    Il testo originale resta in `prezzo_raw`: chi vuole verificare lo trova li'.
    """
    if valore is not None and 0 < valore < PREZZO_SIMBOLICO_SOTTO:
        return None
    return valore


@dataclass
class Prezzo:
    valore: Optional[float]  # None = non disponibile
    e_da_min: bool  # True se testo era "da X€" (prezzo minimo, non fisso)
    raw: str


def parse_prezzo(testo: Optional[str]) -> Prezzo:
    """Interpreta il testo grezzo del prezzo. Casi coperti dai test:

    - "50 €" / "90\\xa0€"        -> valore=50/90, e_da_min=False
    - "da 120 €" / "da 30\\xa0€" -> valore=120/30, e_da_min=True
    - "Prezzo non disponibile"    -> valore=None
    - "Prestazione gratuita"      -> valore=0.0
    - "50-90 €" (range)           -> valore=50 (minimo), e_da_min=True
    - "1 €" (segnaposto)          -> valore=None (vedi `prezzo_plausibile`)
    """
    raw = (testo or "").strip()
    # Il sito usa NBSP ( ) e NARROW NBSP ( ) come separatore: li
    # normalizziamo una volta sola, cosi' i confronti sotto ("da ", "non
    # disponibile") coprono entrambe le varianti senza condizioni duplicate.
    low = raw.lower().replace(" ", " ").replace(" ", " ")

    if not raw:
        return Prezzo(None, False, raw)
    if "gratu" in low:  # "Prestazione gratuita" / "gratis"
        return Prezzo(0.0, False, raw)
    if "non disponibile" in low:
        return Prezzo(None, False, raw)
    if "€" not in raw and "eur" not in low and not any(ch.isdigit() for ch in raw):
        return Prezzo(None, False, raw)

    e_da = low.startswith("da ") or "-" in low.split("€")[0]

    nums = _PRICE_NUM_RE.findall(raw)
    valori = [v for v in (_to_float(n) for n in nums) if v is not None]
    if not valori:
        return Prezzo(None, False, raw)
    # In un range prendiamo il minimo e marchiamo come "da".
    valore = prezzo_plausibile(min(valori))
    return Prezzo(valore, e_da and valore is not None, raw)


# --- Parsing card -----------------------------------------------------------


@dataclass
class Riga:
    result_id: Optional[str]
    address_id: Optional[str]
    nome: Optional[str]
    tipo: Optional[str]  # 'doctor' | 'clinic'
    specializzazione: Optional[str]
    citta: Optional[str]
    indirizzo: Optional[str]
    provincia: Optional[str]
    servizio: Optional[str]
    servizio_id: Optional[str]
    prezzo: Optional[float]
    prezzo_e_da_min: bool
    prezzo_raw: Optional[str]
    rating: Optional[float]
    n_recensioni: Optional[int]
    lat: Optional[float]
    lng: Optional[float]
    url_profilo: Optional[str]
    ha_calendario: bool
    # Campi che servono a UNA sola sorgente e non meritano una colonna nel DB.
    # Vivono solo in memoria, dentro il run che li ha prodotti: servono a
    # `Sorgente.chiave_slot()` per sapere quale calendario interrogare (p.es.
    # Doctolib ha bisogno di visit_motive_id e agenda_ids, MioDottore di nulla).
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x) -> Optional[int]:
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def _text(node) -> Optional[str]:
    if node is None:
        return None
    return " ".join(node.get_text(" ", strip=True).split()) or None


def make_soup(html) -> BeautifulSoup:
    """Costruisce l'albero una volta sola (idempotente se gia' una BeautifulSoup).

    Le pagine pesano ~650 KB e costruire l'albero costa ~50-80 ms: prima
    `count_cards` e `parse_search_html` ne creavano uno ciascuno sullo stesso
    HTML, raddoppiando il lavoro per ogni pagina scaricata.
    """
    if isinstance(html, BeautifulSoup):
        return html
    return BeautifulSoup(html, "lxml")


def parse_search_html(html) -> List[Riga]:
    """Parsa una pagina di ricerca e ritorna una lista di Riga (una per indirizzo).

    Accetta HTML grezzo o una BeautifulSoup gia' costruita (vedi `make_soup`).
    """
    soup = make_soup(html)
    cards = soup.select('[data-id="result-item"]')
    righe: List[Riga] = []

    for card in cards:
        result_id = card.get("data-result-id")
        nome = card.get("data-doctor-name")
        url = card.get("data-doctor-url")
        tipo = card.get("data-ga4-entity-type") or card.get("data-test-entity-type")
        rating = _f(card.get("data-eec-stars-rating"))
        n_rec = _i(card.get("data-eec-opinions-count"))
        specializ = card.get("data-eec-specialization-name")
        citta_card = card.get("data-eec-address-cities")
        ha_cal = card.has_attr("data-eec-has-calendar")

        addr_blocks = card.select('[data-id="result-address-item"]')

        # Servizi/prezzi nella card, in ordine di documento.
        service_nodes = card.select('[data-test-id="available-service"]')

        if not addr_blocks:
            # Nessun indirizzo esplicito: salviamo comunque una riga anagrafica.
            righe.append(_riga_from(card, None, None, None, result_id, nome, url,
                                    tipo, rating, n_rec, specializ, citta_card, ha_cal))
            continue

        for k, addr in enumerate(addr_blocks):
            servizio_node = service_nodes[k] if k < len(service_nodes) else None
            prezzo_node = _price_node_for(addr, servizio_node, card)
            righe.append(_riga_from(card, addr, servizio_node, prezzo_node,
                                    result_id, nome, url, tipo, rating, n_rec,
                                    specializ, citta_card, ha_cal))
    return righe


def _price_node_for(addr, servizio_node, card=None):
    """Trova il <p ... font-weight-bold> del prezzo vicino al servizio/indirizzo.

    Il prezzo e' un <p class="... font-weight-bold"> (text-nowrap) che segue il
    nodo del servizio. Cerchiamo prima dopo il servizio, poi nell'indirizzo.

    La ricerca e' vincolata dentro `card`: `find_all_next` attraversa TUTTO il
    documento, quindi l'ultimo servizio di una card senza prezzo pescava il
    prezzo della card successiva (medico sbagliato).
    """
    def is_price(tag):
        if tag.name != "p":
            return False
        cls = tag.get("class") or []
        if "font-weight-bold" not in cls:
            return False
        txt = tag.get_text(strip=True)
        return "€" in txt or "gratu" in txt.lower() or "non disponibile" in txt.lower()

    if servizio_node is not None:
        for sib in servizio_node.find_all_next("p"):
            if card is not None and card not in sib.parents:
                break  # uscito dalla card corrente: il prezzo non e' suo
            if is_price(sib):
                return sib
            # non andare troppo lontano: fermati al prossimo servizio
            if sib.get("data-test-id") == "available-service":
                break
    # fallback: cerca dentro/vicino al blocco indirizzo
    scope = addr.parent if addr is not None else None
    if scope is not None:
        for p in scope.find_all("p"):
            if is_price(p):
                return p
    return None


def _riga_from(card, addr, servizio_node, prezzo_node, result_id, nome, url,
               tipo, rating, n_rec, specializ, citta_card, ha_cal) -> Riga:
    address_id = (addr.get("data-address-id") if addr is not None
                  else card.get("data-address-id"))
    lat = _f(addr.get("data-lat")) if addr is not None else None
    lng = _f(addr.get("data-lng")) if addr is not None else None

    indirizzo = provincia = citta = None
    if addr is not None:
        street_meta = addr.find("meta", attrs={"itemprop": "streetAddress"})
        city_meta = addr.find("meta", attrs={"itemprop": "addressLocality"})
        prov_meta = addr.find("meta", attrs={"itemprop": "addressRegion"})
        if street_meta and street_meta.get("content"):
            indirizzo = street_meta["content"].strip()
        else:
            indirizzo = _text(addr.select_one("span.text-truncate"))
        citta = city_meta["content"].strip() if city_meta and city_meta.get("content") else citta_card
        provincia = prov_meta["content"].strip() if prov_meta and prov_meta.get("content") else None
    citta = citta or citta_card

    servizio = _text(servizio_node)
    servizio_id = servizio_node.get("data-service-id") if servizio_node else None
    prezzo = parse_prezzo(_text(prezzo_node))

    return Riga(
        result_id=result_id,
        address_id=address_id,
        nome=nome,
        tipo=tipo,
        specializzazione=specializ,
        citta=citta,
        indirizzo=indirizzo,
        provincia=provincia,
        servizio=servizio,
        servizio_id=servizio_id,
        prezzo=prezzo.valore,
        prezzo_e_da_min=prezzo.e_da_min,
        prezzo_raw=prezzo.raw or None,
        rating=rating,
        n_recensioni=n_rec,
        lat=lat,
        lng=lng,
        url_profilo=url,
        ha_calendario=ha_cal,
    )


def count_cards(html) -> int:
    """Numero di card nella pagina. 0 su HTTP 200 = possibile blocco/fine pagine."""
    return len(make_soup(html).select('[data-id="result-item"]'))


def extract_specializations_seen(html) -> dict:
    """Mappa {slug_specializzazione: nome} osservata nelle card (per URL building)."""
    seen = {}
    for card in make_soup(html).select('[data-id="result-item"]'):
        # BeautifulSoup puo' restituire una lista per gli attributi multivalore:
        # solo una stringa e' un nome di specializzazione.
        name = card.get("data-eec-specialization-name")
        if isinstance(name, str) and name:
            seen[slugify(name)] = name
    return seen
