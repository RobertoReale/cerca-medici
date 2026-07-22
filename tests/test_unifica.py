"""Unificazione dei duplicati tra portali.

I casi che contano sono quelli in cui il clustering **sbaglia**: fondere due
medici diversi e' peggio che non fondere nulla, perche' fa sparire una riga vera
e attribuisce a qualcuno il prezzo di qualcun altro.
"""
from typing import Optional

import pytest

from medici.query_engine import Filtro, search
from medici.storage import Storage
from medici.unifica import SOGLIA_METRI, chiave_nome, raggruppa, unifica

# ~0.001 grado di latitudine = ~111 m. Serve a costruire coppie a distanza nota.
_GRADO_M = 111_320.0


def _riga(nome, sorgente="miodottore", *, lat: Optional[float] = 45.4642,
          lng: Optional[float] = 9.19, prezzo: Optional[float] = 100.0,
          tipo="doctor", **kw):
    base = dict(nome=nome, sorgente=sorgente, lat=lat, lng=lng, prezzo=prezzo,
                tipo=tipo, prezzo_e_da_min=False, n_recensioni=0,
                prima_disponibilita=None)
    base.update(kw)
    return base


def _spostata(riga, metri):
    return {**riga, "lat": riga["lat"] + metri / _GRADO_M}


def _etichetta(nome):
    return {"miodottore": "MioDottore", "doctolib": "Doctolib"}.get(nome, nome)


def _unifica(righe):
    return unifica(righe, etichetta=_etichetta)


# --- normalizzazione del nome -----------------------------------------------

@pytest.mark.parametrize("nome", [
    "Dott.ssa Martina Nuzzo", "Dr. Martina Nuzzo", "Prof. MARTINA NUZZO",
    "Dott. Martina Nuzzò",
])
def test_titoli_e_accenti_non_contano(nome):
    assert chiave_nome(nome) == "nuzzo"


def test_per_un_medico_conta_il_cognome():
    """MioDottore da' il nome intero, Doctolib lo da' spezzato: l'ultima parola
    e' l'unico pezzo che le due fonti scrivono allo stesso modo."""
    assert chiave_nome("Dott.ssa Maria Teresa Rossi") == "rossi"


def test_per_una_struttura_conta_il_nome_intero():
    """Due poliambulatori nello stesso isolato possono finire entrambi con
    «Milano»: confrontare solo l'ultima parola li fonderebbe."""
    a = chiave_nome("Centro Medico San Marco Milano", tipo="clinic")
    b = chiave_nome("Poliambulatorio Sant'Anna Milano", tipo="clinic")
    assert a != b


def test_nome_vuoto_non_ha_chiave():
    assert chiave_nome(None) is None
    assert chiave_nome("Dott.") is None


# --- clustering -------------------------------------------------------------

def test_stesso_cognome_a_cinquanta_metri_e_un_cluster():
    a = _riga("Dott.ssa Martina Nuzzo", "miodottore")
    b = _spostata(_riga("Dott. Martina Nuzzo", "doctolib"), 50)
    assert len(raggruppa([a, b])) == 1


def test_stesso_cognome_a_cinquecento_metri_sono_due():
    a = _riga("Dott.ssa Martina Nuzzo", "miodottore")
    b = _spostata(_riga("Dott. Martina Nuzzo", "doctolib"), 500)
    assert len(raggruppa([a, b])) == 2


def test_stesso_indirizzo_cognomi_diversi_restano_due():
    """Un poliambulatorio con trenta medici non e' un medico solo: e' il motivo
    per cui la geografia da sola non basta."""
    a = _riga("Dott. Mario Rossi", "miodottore")
    b = _riga("Dott. Luigi Bianchi", "doctolib")
    assert len(raggruppa([a, b])) == 2


def test_titoli_diversi_stesso_medico():
    a = _riga("Dott. Martina Nuzzo", "miodottore")
    b = _spostata(_riga("Dr. Martina Nuzzo", "doctolib"), 20)
    assert len(raggruppa([a, b])) == 1


def test_due_righe_dello_stesso_portale_non_si_fondono():
    """Su MioDottore due righe dello stesso medico allo stesso indirizzo sono
    servizi diversi con prezzi diversi: fonderle cancellerebbe proprio
    l'informazione che le distingue."""
    a = _riga("Dott. Mario Rossi", "miodottore", servizio="Prima visita", prezzo=120.0)
    b = _riga("Dott. Mario Rossi", "miodottore", servizio="Controllo", prezzo=80.0)
    assert len(raggruppa([a, b])) == 2


def test_righe_senza_coordinate_restano_sole():
    """Senza posizione non c'e' modo di distinguere l'omonimo dal duplicato:
    unire alla cieca sarebbe peggio che non unire."""
    a = _riga("Dott. Mario Rossi", "miodottore", lat=None, lng=None)
    b = _riga("Dott. Mario Rossi", "doctolib", lat=None, lng=None)
    assert len(raggruppa([a, b])) == 2


def test_soglia_configurabile():
    a = _riga("Dott. Mario Rossi", "miodottore")
    b = _spostata(_riga("Dott. Mario Rossi", "doctolib"), 300)
    assert len(raggruppa([a, b])) == 2
    assert len(raggruppa([a, b], soglia_metri=500)) == 1


def test_soglia_di_default_documentata():
    assert SOGLIA_METRI == 150.0


def test_omonimi_lontani_in_una_citta_grande():
    """Tre Rossi in tre zone diverse restano tre: e' il caso che il criterio
    geografico esiste per proteggere."""
    righe = [_riga("Dott. Mario Rossi", "miodottore"),
             _spostata(_riga("Dott. Mario Rossi", "doctolib"), 2000),
             _spostata(_riga("Dott. Mario Rossi", "doctolib"), 5000)]
    assert len(raggruppa(righe)) == 3


# --- fusione ----------------------------------------------------------------

def test_il_prezzo_del_cluster_e_il_minimo():
    a = _riga("Dott.ssa Martina Nuzzo", "miodottore", prezzo=120.0)
    b = _spostata(_riga("Dott. Martina Nuzzo", "doctolib", prezzo=100.0), 50)
    assert _unifica([a, b])[0]["prezzo"] == 100.0


def test_i_prezzi_dei_singoli_portali_restano_visibili():
    """E' l'informazione per cui si cerca su due portali: che costano diverso."""
    a = _riga("Dott.ssa Martina Nuzzo", "miodottore", prezzo=120.0)
    b = _spostata(_riga("Dott. Martina Nuzzo", "doctolib", prezzo=100.0), 50)
    testo = _unifica([a, b])[0]["disponibile_su"]
    assert "Doctolib 100 €" in testo and "MioDottore 120 €" in testo


def test_prezzo_mancante_dichiarato_non_riempito():
    """Se un portale non pubblica il prezzo, il cluster deve dirlo: la mancanza
    va vista, non colmata col valore dell'altro."""
    a = _riga("Dott. Mario Rossi", "miodottore", prezzo=120.0)
    b = _spostata(_riga("Dott. Mario Rossi", "doctolib", prezzo=None), 50)
    fuso = _unifica([a, b])[0]
    assert fuso["prezzo"] == 120.0
    assert "Doctolib n/d" in fuso["disponibile_su"]


def test_prezzo_da_minimo_riportato_come_tale():
    a = _riga("Dott. Mario Rossi", "miodottore", prezzo=90.0, prezzo_e_da_min=True)
    assert "da 90 €" in _unifica([a])[0]["disponibile_su"]


def test_prima_disponibilita_e_la_piu_vicina_del_cluster():
    a = _riga("Dott. Mario Rossi", "miodottore", prima_disponibilita="2099-06-01 09:00")
    b = _spostata(_riga("Dott. Mario Rossi", "doctolib",
                        prima_disponibilita="2099-01-01 15:00"), 50)
    assert _unifica([a, b])[0]["prima_disponibilita"] == "2099-01-01 15:00"


def test_n_fonti_presente_anche_sulle_righe_sole():
    """Una colonna che appare e scompare a seconda dei dati e' peggio di una
    colonna che a volte vale 1."""
    solo = _unifica([_riga("Dott. Mario Rossi")])[0]
    assert solo["n_fonti"] == 1 and solo["disponibile_su"]


def test_gli_originali_non_vengono_modificati():
    a = _riga("Dott. Mario Rossi", "miodottore", prezzo=120.0)
    b = _spostata(_riga("Dott. Mario Rossi", "doctolib", prezzo=100.0), 50)
    _unifica([a, b])
    assert a["prezzo"] == 120.0 and "disponibile_su" not in a


# --- integrazione con le query ----------------------------------------------

def _popola(tmp_path):
    from medici.parser import Riga
    from medici.scraper import scrape_search
    from test_sorgenti import SorgenteFinta

    def riga(nome, prezzo, lat, rid="1"):
        return Riga(result_id=rid, address_id="1", nome=nome, tipo="doctor",
                    specializzazione="cardiologo", citta="Milano",
                    indirizzo="Via A 1", provincia=None, servizio="Visita",
                    servizio_id="9", prezzo=prezzo, prezzo_e_da_min=False,
                    prezzo_raw=None, rating=4.5, n_recensioni=10, lat=lat,
                    lng=9.19, url_profilo=None, ha_calendario=False)

    st = Storage(tmp_path / "t.db")
    scrape_search("cardiologo", "Milano", storage=st, sorgente=SorgenteFinta(
        "miodottore", [[riga("Dott.ssa Martina Nuzzo", 120.0, 45.4642, "1"),
                        riga("Dott. Luigi Bianchi", 200.0, 45.4642, "2")]]))
    scrape_search("cardiologo", "Milano", storage=st, sorgente=SorgenteFinta(
        "doctolib", [[riga("Dott. Martina Nuzzo", 100.0, 45.46425)]]))
    return st


def test_search_unifica_e_ordina_sul_minimo(tmp_path):
    st = _popola(tmp_path)
    grezze = search(st, Filtro())
    unite = search(st, Filtro(unifica_duplicati=True))
    st.close()
    assert len(grezze) == 3
    assert len(unite) == 2, "il cluster non e' stato riconosciuto"
    assert [r["prezzo"] for r in unite] == [100.0, 200.0]


def test_limite_applicato_dopo_l_unificazione(tmp_path):
    """Con il LIMIT in SQL la seconda riga del cluster verrebbe tagliata prima
    di fondere, e il «minimo tra i portali» sarebbe calcolato su meta' dati."""
    st = _popola(tmp_path)
    unite = search(st, Filtro(unifica_duplicati=True, limite=1))
    st.close()
    assert len(unite) == 1
    assert unite[0]["prezzo"] == 100.0
    assert unite[0]["n_fonti"] == 2
