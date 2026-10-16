"""Regressioni su Storage: la connessione deve reggere l'uso multi-thread.

Streamlit tiene lo Storage in `@st.cache_resource` ed esegue lo script in
thread diversi: con il default di sqlite3 (`check_same_thread=True`) la seconda
interazione con la GUI falliva con ProgrammingError.
"""
import threading

from medici.parser import Riga
from medici.storage import Storage


def _riga(prezzo=50.0, servizio="Visita"):
    return Riga(result_id="1", address_id="2", nome="Dr. X", tipo="doctor",
                specializzazione="podologo", citta="Milano", indirizzo="Via A 1",
                provincia="MI", servizio=servizio, servizio_id="9", prezzo=prezzo,
                prezzo_e_da_min=False, prezzo_raw=f"{prezzo} €", rating=4.5,
                n_recensioni=10, lat=45.0, lng=9.0, url_profilo="http://x",
                ha_calendario=True)


def test_stessa_riga_su_piu_pagine_non_duplica(tmp_path):
    """La paginazione del sito ripete risultati: devono restare righe uniche."""
    st = Storage(tmp_path / "t.db")
    assert st.insert_righe([_riga()], "podologo", "Milano") == 1
    assert st.insert_righe([_riga(prezzo=60.0)], "podologo", "Milano") == 0
    assert st.count_risultati() == 1
    # l'upsert tiene il valore piu' recente
    assert st.query("SELECT prezzo FROM risultati")[0]["prezzo"] == 60.0
    st.close()


def test_prezzi_simbolici_gia_salvati_vengono_corretti(tmp_path):
    """Un DB scaricato prima del filtro sui prezzi simbolici li ha salvati come
    prezzi veri: riaprendolo vanno tolti, senza perdere il testo originale."""
    st = Storage(tmp_path / "t.db")
    st.insert_righe([_riga(prezzo=1.0), _riga(prezzo=0.0, servizio="Gratis"),
                     _riga(servizio="Altro")], "podologo", "Milano")
    st.close()
    st = Storage(tmp_path / "t.db")
    righe = {r["servizio"]: r for r in
             st.query("SELECT servizio, prezzo, prezzo_raw FROM risultati")}
    assert righe["Visita"]["prezzo"] is None
    assert righe["Visita"]["prezzo_raw"] == "1.0 €"
    assert righe["Gratis"]["prezzo"] == 0.0
    assert righe["Altro"]["prezzo"] == 50.0
    st.close()


def test_uso_da_thread_diverso(tmp_path):
    st = Storage(tmp_path / "t.db")
    errori = []

    def leggi():
        try:
            st.count_risultati()
        except Exception as e:  # pragma: no cover - fallisce = regressione
            errori.append(e)

    t = threading.Thread(target=leggi)
    t.start()
    t.join()
    st.close()
    assert not errori, errori


def test_scritture_concorrenti(tmp_path):
    st = Storage(tmp_path / "t.db")
    slot = [{"result_id": "1", "address_id": "2", "servizio_id": None,
             "data": "2026-07-22", "orario": "10:00", "prenotabile": True}]
    errori = []

    def scrivi():
        try:
            for _ in range(20):
                st.insert_disponibilita(slot)
        except Exception as e:  # pragma: no cover
            errori.append(e)

    ts = [threading.Thread(target=scrivi) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errori, errori
    assert st.query("SELECT COUNT(*) c FROM disponibilita")[0]["c"] == 80
    st.close()


def test_riepilogo_raggruppa_per_ricerca_non_per_citta_del_medico(tmp_path):
    """`citta` e' sia l'alias di `query_citta` sia una colonna vera (la citta'
    del medico): SQLite in GROUP BY preferisce la colonna, e una sola ricerca
    compariva spezzata in una riga per comune trovato."""
    from medici.parser import Riga

    def riga(citta):
        return Riga(result_id=citta, address_id="1", nome=f"Dr. {citta}",
                    tipo="doctor", specializzazione="podologo", citta=citta,
                    indirizzo="Via A 1", provincia="MI", servizio="Visita",
                    servizio_id=None, prezzo=10.0, prezzo_e_da_min=False,
                    prezzo_raw="10 €", rating=None, n_recensioni=None,
                    lat=None, lng=None, url_profilo=None, ha_calendario=True)

    st = Storage(tmp_path / "t.db")
    # Una sola ricerca "podologo/Milano" che trova studi in tre comuni diversi.
    st.insert_righe([riga("Milano"), riga("Sesto San Giovanni"),
                     riga("Cologno Monzese")], "podologo", "Milano")
    r = st.riepilogo_query()
    st.close()
    assert len(r) == 1, f"una ricerca spezzata in {len(r)} righe"
    assert (r[0]["spec"], r[0]["citta"], r[0]["righe"]) == ("podologo", "Milano", 3)
