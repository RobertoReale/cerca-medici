"""Mappatura JSON Doctolib -> `Riga`, su una risposta reale congelata.

Niente rete: la fixture e' in `tests/fixtures/doctolib_search.json` (vedi il
README li' accanto per il perche' di ogni record). Se Doctolib cambia i nomi dei
campi questi test falliscono, ed e' esattamente lo scopo: un campo rinominato in
silenzio produrrebbe altrimenti righe con prezzo e rating vuoti, che sembrano
dati legittimi.
"""
import json
from pathlib import Path

import pytest

from medici.sorgenti.doctolib import SorgenteDoctolib, riga_da_medico

FIXTURE = Path(__file__).parent / "fixtures" / "doctolib_search.json"


@pytest.fixture(scope="module")
def medici():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["healthcareProviders"]


@pytest.fixture(scope="module")
def per_nome(medici):
    return {m["name"]: riga_da_medico(m) for m in medici}


def test_tutti_i_record_producono_una_riga(medici):
    righe = [riga_da_medico(m) for m in medici]
    assert len(righe) == len(medici)
    # `result_id` c'e' sempre ed e' l'unica cosa non negoziabile: senza,
    # storage e disponibilita' non saprebbero a chi attaccare i dati.
    assert all(r.result_id for r in righe)


def test_profilo_da_elenco_non_ha_sede(per_nome):
    """`references.type == "directory_doctor"` = scheda da elenco, non cliente
    Doctolib: `practiceId` e' `None`. Non e' un dato rotto ed e' anche il motivo
    per cui non ha calendario — ma chi lo legge non deve dare per scontato che
    `address_id` ci sia sempre."""
    assert per_nome["Alimento"].address_id is None


def test_identificativi_sono_stringhe(per_nome):
    """L'API li da' numerici, il DB li tiene in colonne TEXT: se qui restassero
    interi, i confronti con le righe di MioDottore fallirebbero in silenzio."""
    r = per_nome["Giudici"]
    assert (r.result_id, r.address_id) == ("878639", "346801")
    assert isinstance(r.servizio_id, str)


def test_nome_ricomposto_dai_tre_pezzi(per_nome):
    """Doctolib da' `title`/`firstName`/`name` separati, MioDottore il nome
    intero: senza ricomporlo le due fonti non sarebbero confrontabili."""
    assert per_nome["Giudici"].nome == "Dott. Sandra Liliana Giudici"


def test_nome_salta_i_pezzi_vuoti(per_nome):
    """`title` e' spesso la stringa vuota: non deve lasciare spazi doppi."""
    nome = per_nome["Equipe medica Cardiologia"].nome
    assert nome == "Fondazione Don Gnocchi - Equipe medica Cardiologia"
    assert "  " not in nome


def test_tipo_tradotto(per_nome):
    """`chiave_slot` e i filtri distinguono i professionisti dalle strutture:
    il vocabolario deve essere quello del progetto, non quello di Doctolib."""
    assert per_nome["Giudici"].tipo == "doctor"
    assert per_nome["MoveMed Milano"].tipo == "clinic"


def test_prezzo_fisso(per_nome):
    r = per_nome["MoveMed Milano"]
    assert (r.prezzo, r.prezzo_e_da_min) == (50.0, False)
    assert r.prezzo_raw == "50 €"


def test_prezzo_variabile_e_un_minimo(per_nome):
    """`VARIES_DEPENDING_ON_HCP` ha la stessa semantica del «da 30 €» di
    MioDottore: il numero e' un minimo, non il prezzo."""
    r = per_nome["Fondazione Don Gnocchi"]
    assert r.prezzo_e_da_min is True
    assert r.prezzo_raw.startswith("da ")


def test_prezzo_assente_resta_none(per_nome):
    """Il caso piu' comune dopo quello col prezzo: non deve diventare 0.0, che
    ordinato per prezzo finirebbe in cima come «il piu' economico»."""
    r = per_nome["MILANO MEDICA Centro Medico Polispecialistico a Milano"]
    assert r.prezzo is None
    assert r.prezzo_raw is None


def test_rating_assente_resta_none(per_nome):
    senza = per_nome["MoveMed Milano"]
    con = per_nome["Giudici"]
    assert (senza.rating, senza.n_recensioni) == (None, None)
    assert con.rating and con.n_recensioni


def test_coordinate_sempre_presenti(medici):
    """Sono la chiave dell'unificazione cross-portale (fase 6): se mancassero,
    i duplicati non sarebbero riconoscibili."""
    righe = [riga_da_medico(m) for m in medici]
    assert all(r.lat and r.lng for r in righe)


def test_provincia_assente_non_inventata(per_nome):
    assert per_nome["Giudici"].provincia is None


def test_url_profilo_assoluto(per_nome):
    url = per_nome["Giudici"].url_profilo
    assert url.startswith("https://www.doctolib.it/")


def test_senza_agenda_online_niente_calendario(per_nome):
    """`services: []` = profilo di sola consultazione. Interrogare gli slot per
    questi e' traffico buttato: e' cio' che tiene un run a decine di richieste
    invece che a centinaia."""
    r = per_nome["Alimento"]
    assert r.ha_calendario is False
    assert SorgenteDoctolib(client=object()).chiave_slot(r) is None


def test_chiave_slot_porta_motivo_e_agende(per_nome):
    src = SorgenteDoctolib(client=object())
    chiave = src.chiave_slot(per_nome["Giudici"])
    motivo, agende = chiave.dati
    assert isinstance(motivo, int) and isinstance(agende, tuple) and agende
    # Frozen e hashable: lo scraper deduplica le chiavi con `dict.fromkeys`.
    assert {chiave, chiave} == {chiave}


def test_extra_non_finisce_nel_db(per_nome):
    """`extra` vive solo in memoria: `Storage.insert_righe` non lo legge, e se
    qualcuno ci mettesse dati importanti sparirebbero al salvataggio."""
    from medici.storage import Storage
    assert "extra" not in Storage.__dict__.get("_CHIAVE_SQL", "")
    assert set(per_nome["Giudici"].extra) == {"visit_motive_id", "agenda_ids",
                                              "practice_id"}
