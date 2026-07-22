"""Test del motore di query: ordinamento prezzo (NULL in coda), haversine, filtri."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from medici.query_engine import (  # noqa: E402
    _sort, avvisi, haversine_km, search, termini_servizio, Filtro)
from medici.storage import Storage  # noqa: E402


def _db(tmp_path, righe):
    """DB minimo: (servizio, prezzo) per riga."""
    st = Storage(tmp_path / "q.db")
    for i, (servizio, prezzo) in enumerate(righe):
        st.conn.execute(
            "INSERT INTO risultati (result_id,address_id,nome,servizio,prezzo,"
            "query_specializzazione,query_citta) VALUES (?,?,?,?,?,?,?)",
            (str(i), str(i), f"Dr. {i}", servizio, prezzo, "podologo", "Milano"))
    st.conn.commit()
    return st


def test_servizio_trova_le_varianti_del_nome(tmp_path):
    """Il sito nomina lo stesso servizio in modi diversi: cercare la frase
    esatta ne trovava una sola."""
    st = _db(tmp_path, [("Visita podologica di controllo", 39.0),
                        ("Visita di controllo podologica", 45.0),
                        ("Ortesi in silicone", 30.0)])
    trovati = [r["servizio"] for r in
               search(st, Filtro(servizio="visita di controllo", ordina="prezzo"))]
    st.close()
    assert len(trovati) == 2, trovati
    assert "Ortesi in silicone" not in trovati


# --- avvisi: i filtri che non possono funzionare ----------------------------

def test_distanza_senza_punto_di_riferimento_e_un_avviso():
    """Senza coordinate l'ordinamento per distanza non ordina niente, e senza
    avviso sembra solo che il programma ignori la richiesta."""
    detto = avvisi(Filtro(ordina="distanza"))
    assert len(detto) == 1 and "lat,lng" in detto[0]
    # Con il punto di riferimento non c'e' nulla da segnalare.
    assert avvisi(Filtro(ordina="distanza", vicino_lat=45.4, vicino_lng=9.1)) == []


def test_filtro_disponibilita_su_db_senza_slot_e_un_avviso(tmp_path):
    """Chi non ha mai scaricato le disponibilità vedrebbe zero risultati e
    darebbe la colpa agli altri filtri."""
    st = _db(tmp_path, [("Visita", 50.0)])
    detto = avvisi(Filtro(disponibile_entro_giorni=3), st)
    st.close()
    assert len(detto) == 1 and "disponibilità" in detto[0]


def test_nessun_avviso_su_un_filtro_normale(tmp_path):
    st = _db(tmp_path, [("Visita", 50.0)])
    assert avvisi(Filtro(ordina="prezzo", prezzo_max=100), st) == []
    st.close()


def test_termini_servizio_scarta_le_preposizioni():
    assert termini_servizio("visita di controllo") == ["visita", "controllo"]
    # Solo stopword: meglio la frase intera che l'intero database.
    assert termini_servizio("di") == ["di"]


def test_stesso_medico_a_pari_prezzo_resta_adiacente(tmp_path):
    """Le sedi di uno stesso medico non devono sparpagliarsi tra gli altri."""
    st = Storage(tmp_path / "q.db")
    for i, (nome, indirizzo) in enumerate(
            [("Caputo", "Via B"), ("Bianchi", "Via C"),
             ("Caputo", "Via A"), ("Alberti", "Via D")]):
        st.conn.execute(
            "INSERT INTO risultati (result_id,address_id,nome,indirizzo,servizio,"
            "prezzo,query_specializzazione,query_citta) VALUES (?,?,?,?,?,?,?,?)",
            (str(i), str(i), nome, indirizzo, "Visita", 60.0, "podologo", "Milano"))
    st.conn.commit()
    out = [(r["nome"], r["indirizzo"]) for r in search(st, Filtro(ordina="prezzo"))]
    st.close()
    assert out == [("Alberti", "Via D"), ("Bianchi", "Via C"),
                   ("Caputo", "Via A"), ("Caputo", "Via B")], out


def test_solo_prenotabili(tmp_path):
    """Chi non ha agenda online compare col listino ma non si prenota da qui."""
    st = Storage(tmp_path / "q.db")
    for i, (nome, cal) in enumerate([("Con agenda", 1), ("Inattivo", 0)]):
        st.conn.execute(
            "INSERT INTO risultati (result_id,address_id,nome,servizio,prezzo,"
            "ha_calendario,query_specializzazione,query_citta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(i), str(i), nome, "Visita", 60.0, cal, "podologo", "Milano"))
    st.conn.commit()
    tutti = search(st, Filtro(ordina="prezzo"))
    solo = search(st, Filtro(ordina="prezzo", solo_prenotabili=True))
    st.close()
    assert len(tutti) == 2
    assert [r["nome"] for r in solo] == ["Con agenda"]


def test_prezzo_max(tmp_path):
    st = _db(tmp_path, [("A", 30.0), ("B", 90.0), ("C", None)])
    r = search(st, Filtro(prezzo_max=50, ordina="prezzo"))
    st.close()
    # Chi non pubblica il prezzo non puo' rientrare in un budget massimo.
    assert [x["servizio"] for x in r] == ["A"]


def test_prezzo_min_e_max_insieme(tmp_path):
    st = _db(tmp_path, [("A", 30.0), ("B", 60.0), ("C", 90.0)])
    r = search(st, Filtro(prezzo_min=40, prezzo_max=80, ordina="prezzo"))
    st.close()
    assert [x["servizio"] for x in r] == ["B"]


def test_sort_prezzo_null_in_coda():
    data = [{"prezzo": 50}, {"prezzo": None}, {"prezzo": 10}, {"prezzo": None}, {"prezzo": 30}]
    out = _sort(list(data), Filtro(ordina="prezzo"))
    valori = [r["prezzo"] for r in out]
    assert valori[:3] == [10, 30, 50]      # crescente
    assert valori[3:] == [None, None]      # NULL sempre in coda


def test_sort_prezzo_desc_null_ancora_in_coda():
    data = [{"prezzo": 50}, {"prezzo": None}, {"prezzo": 10}]
    out = _sort(list(data), Filtro(ordina="prezzo", discendente=True))
    valori = [r["prezzo"] for r in out]
    assert valori == [50, 10, None]


def test_sort_rating_desc_default():
    data = [{"rating": 3.0}, {"rating": 5.0}, {"rating": None}, {"rating": 4.0}]
    out = _sort(list(data), Filtro(ordina="rating"))
    assert [r["rating"] for r in out][:3] == [5.0, 4.0, 3.0]  # rating alto prima


def test_haversine_note():
    # Duomo Milano -> Colosseo Roma ~ 477 km
    d = haversine_km(45.4642, 9.1900, 41.8902, 12.4922)
    # `haversine_km` ritorna None con coordinate mancanti: escluderlo prima del
    # confronto e' anche il fallimento piu' leggibile se dovesse regredire.
    assert d is not None
    assert 470 < d < 490


def test_haversine_none():
    assert haversine_km(None, 9.0, 45.0, 9.0) is None


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in funcs:
        try:
            fn(); print(f"PASS {fn.__name__}"); ok += 1
        except Exception:
            print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{ok}/{len(funcs)} test passati")
    sys.exit(0 if ok == len(funcs) else 1)
