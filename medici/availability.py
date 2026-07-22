"""Fase 3-bis — Disponibilita (date/orari) per indirizzo esatto.

RISULTATO DELLA RICOGNIZIONE (Passo A/B della Fase 3-bis, caso favorevole):

L'endpoint reale degli slot e' stato catturato dal tab Network di un browser
reale (via Chrome DevTools) ed e' replicabile con una semplice richiesta HTTP,
SENZA browser headless:

    GET https://www.miodottore.it/api/v3/doctors/{doctor_id}/addresses/{address_id}/slots
        ?start=<ISO8601>&end=<ISO8601>
        &with[]=slot.doctor_id&with[]=slot.address_id&with[]=slot.with_booked
        &includingSaasOnlyCalendar=false

    Header richiesto:  Authorization: Bearer <ACCESS_TOKEN>

Il token NON e' generato in modo complesso lato client: e' incorporato
staticamente nell'HTML di ogni pagina (ricerca o profilo) come:

    ZLApp.APICredentials = { 'ACCESS_TOKEN': '<hex>' , ... }

quindi lo estraiamo con un regex dalla stessa pagina HTML che gia' scarichiamo
per anagrafica/prezzi. Nessun cookie di sessione e' necessario (verificato:
funziona con solo il Bearer token, senza cookie).

Risposta: JSON `application/vnd.docplanner+json` con `_items[]`, ogni item:
    { "start": "2026-07-22T16:00:00+02:00", "calendar_id": ..., "doctor_id": ...,
      "address_id": ..., "booking_url": "...", "booked": false }

*** AVVISO robots.txt ***
Questo endpoint vive sotto `/api/v3/` su www.miodottore.it, path DISALLOWED dal
robots.txt del sito (a differenza delle pagine di ricerca HTML, permesse). Per
questo la raccolta disponibilita' e' OPT-IN (flag `--disponibilita` nella CLI /
checkbox nella GUI), non parte di default. Usarla con parsimonia e solo per uso
personale, come discusso nella sezione Rischi del piano.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Callable, List, Optional

try:
    from zoneinfo import ZoneInfo
    _ROME = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover - tzdata assente (Windows senza `tzdata`)
    _ROME = None

from .fetcher import Fetcher, FetcherGet, FetchError, BASE_URL

log = logging.getLogger("medici.availability")

_TOKEN_RE = re.compile(r"ACCESS_TOKEN'\s*:\s*'([A-Za-z0-9_\-]+)'")


def extract_access_token(html: str) -> Optional[str]:
    """Estrae ZLApp.APICredentials.ACCESS_TOKEN dall'HTML di una pagina."""
    m = _TOKEN_RE.search(html)
    return m.group(1) if m else None


def _iso(dt: datetime) -> str:
    """ISO8601 con l'offset REALE di Europe/Rome per quella data.

    Prima era hardcoded +02:00 (CEST): da fine ottobre a fine marzo l'offset
    corretto e' +01:00 e la finestra richiesta risultava sfalsata di un'ora.
    """
    if _ROME is not None:
        dt = dt.replace(tzinfo=_ROME)
        off = dt.utcoffset() or timedelta(0)
        hours = int(off.total_seconds() // 3600)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f"+{hours:02d}:00"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+01:00"


def build_slots_url(doctor_id: str, address_id: str, giorni: int = 5,
                    start: Optional[datetime] = None) -> str:
    if start is None:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # `giorni` arriva dall'utente (`--giorni`): con 0 o un negativo la finestra
    # finirebbe PRIMA di iniziare e l'endpoint risponderebbe con niente, senza
    # che nulla dica perche'. Un giorno e' il minimo che ha senso chiedere.
    end = (start + timedelta(days=max(1, giorni) - 1)).replace(
        hour=23, minute=59, second=59)
    params = (
        f"start={_iso(start)}&end={_iso(end)}"
        "&with%5B%5D=address.nearest_slot_after_end"
        "&with%5B%5D=slots.address_state"
        "&with%5B%5D=slot.doctor_id"
        "&with%5B%5D=slot.address_id"
        "&with%5B%5D=slot.with_booked"
        "&includingSaasOnlyCalendar=false"
    )
    return (f"{BASE_URL}/api/v3/doctors/{doctor_id}/addresses/{address_id}"
            f"/slots?{params}")


class AvailabilityFetcher:
    """Recupera gli slot via HTTP diretto usando il Bearer token estratto dall'HTML."""

    def __init__(self, access_token: str, fetcher: Optional[FetcherGet] = None,
                 rinnova_token: Optional[Callable[[], Optional[str]]] = None):
        """`rinnova_token`: callable che riscarica una pagina HTML e ne estrae un
        ACCESS_TOKEN fresco. Senza, un token scaduto a meta' run fa fallire con
        401 tutti gli indirizzi rimanenti senza possibilita' di recupero."""
        if not access_token:
            raise ValueError("access_token mancante: estrarlo con extract_access_token()")
        self.fetcher = fetcher or Fetcher()
        self.rinnova_token = rinnova_token
        self._set_token(access_token)

    def _set_token(self, access_token: str) -> None:
        self.access_token = access_token
        # NB: header applicati per-richiesta, non sulla sessione. Il Fetcher e'
        # condiviso con lo scraping HTML e un `Authorization` residuo (piu'
        # `Accept: application/json`) alterava le pagine di ricerca successive.
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/plain, */*",
        }

    def _rinnova(self) -> bool:
        """Tenta un rinnovo del token. True se ne ha ottenuto uno nuovo."""
        if self.rinnova_token is None:
            return False
        try:
            nuovo = self.rinnova_token()
        except Exception as e:  # il rinnovo non deve far esplodere il run
            log.warning("Rinnovo token fallito: %s", e)
            return False
        if not nuovo or nuovo == self.access_token:
            return False
        log.info("ACCESS_TOKEN rinnovato dopo un 401.")
        self._set_token(nuovo)
        return True

    def fetch_slots(self, doctor_id: str, address_id: str, giorni: int = 5,
                    start: Optional[datetime] = None) -> List[dict]:
        """Ritorna una lista di slot normalizzati per un indirizzo.

        Ogni slot: {result_id, address_id, calendar_id, data, orario,
                    prenotabile, booking_url}
        """
        url = build_slots_url(doctor_id, address_id, giorni, start)
        try:
            res = self.fetcher.get(url, headers=self._headers)
        except FetchError as e:
            if e.status in (401, 403) and self._rinnova():
                # Token scaduto: con uno fresco l'indirizzo e' ancora recuperabile.
                try:
                    res = self.fetcher.get(url, headers=self._headers)
                except FetchError as e2:
                    log.debug("Slot non disponibili dopo rinnovo per %s/%s (%s)",
                              doctor_id, address_id, e2)
                    return []
            else:
                # 400/404 sono FISIOLOGICI qui: l'endpoint esiste solo per entita'
                # "doctor" con calendario attivo. Cliniche, studi senza agenda
                # online e indirizzi senza calendario rispondono 404 (coppia
                # doctor/address inesistente) o 400. Non e' un errore da segnalare
                # rumorosamente: significa solo "niente slot per questo indirizzo".
                log.debug("Nessun calendario per doctor=%s address=%s (%s)",
                          doctor_id, address_id, e)
                return []
        try:
            data = json.loads(res.html)
        except json.JSONDecodeError:
            log.error("Risposta slots non JSON per doctor=%s address=%s",
                      doctor_id, address_id)
            return []
        items = data.get("_items", []) if isinstance(data, dict) else []
        slots: List[dict] = []
        for it in items:
            start_str = it.get("start")
            data_g = orario = None
            if start_str:
                # "2026-07-22T16:00:00+02:00" -> data + orario
                m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", start_str)
                if m:
                    data_g, orario = m.group(1), m.group(2)
            slots.append({
                "result_id": str(it.get("doctor_id") or doctor_id),
                "address_id": str(it.get("address_id") or address_id),
                "servizio_id": None,
                "calendar_id": it.get("calendar_id"),
                "data": data_g,
                "orario": orario,
                "prenotabile": not it.get("booked", False),
                "booking_url": it.get("booking_url"),
            })
        log.info("Slots doctor=%s address=%s: %d", doctor_id, address_id, len(slots))
        return slots


def fetch_slots_for_rows(html_con_token: str, coppie_doctor_address, *,
                         giorni: int = 5,
                         fetcher: Optional[FetcherGet] = None) -> List[dict]:
    """Helper: estrae il token dall'HTML e recupera gli slot per una lista di
    (doctor_id, address_id). Ritorna la lista piatta di tutti gli slot."""
    token = extract_access_token(html_con_token)
    if not token:
        raise ValueError("ACCESS_TOKEN non trovato nell'HTML (markup cambiato?)")
    af = AvailabilityFetcher(token, fetcher=fetcher)
    out: List[dict] = []
    for doctor_id, address_id in coppie_doctor_address:
        out.extend(af.fetch_slots(doctor_id, address_id, giorni=giorni))
    return out
