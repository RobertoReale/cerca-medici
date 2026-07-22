"""Regressioni sul multi-sorgente.

Il rischio vero di questo refactoring non e' che una sorgente non funzioni: e'
che DUE sorgenti si pestino i piedi nel database, perche' `result_id` e
`address_id` sono numeri interni di ciascun portale e possono coincidere per
medici completamente diversi. I test qui sotto coprono esattamente quel caso.
"""
import pytest

from medici.parser import Riga
from medici.query_engine import Filtro, search
from medici.scraper import scrape_search
from medici.sorgenti import (ChiaveSlot, Pagina, RaccoglitoreSlot, Sorgente,
                             SorgenteSconosciuta, elenco, ottieni)
from medici.storage import Storage


def _riga(rid="1", aid="2", nome="Dr. X", prezzo=50.0, calendario=True, **kw):
    base: dict = dict(result_id=rid, address_id=aid, nome=nome, tipo="doctor",
                specializzazione="podologo", citta="Milano", indirizzo="Via A 1",
                provincia="MI", servizio="Visita", servizio_id="9", prezzo=prezzo,
                prezzo_e_da_min=False, prezzo_raw=f"{prezzo} €", rating=4.5,
                n_recensioni=10, lat=45.0, lng=9.0, url_profilo="http://x",
                ha_calendario=calendario)
    base.update(kw)
    return Riga(**base)


class SorgenteFinta(Sorgente):
    """Sorgente di test: restituisce pagine gia' pronte, senza rete."""

    supporta_disponibilita = True

    def __init__(self, nome, pagine, slot_per_chiave=None):
        self.nome = nome
        self.etichetta = nome.capitalize()
        self._pagine = list(pagine)
        self._slot = slot_per_chiave or {}
        self.chiamate = []

    def url_ricerca(self, specializzazione, citta):
        return f"https://{self.nome}.example/{specializzazione}/{citta}"

    def scarica_pagina(self, specializzazione, citta, page):
        self.chiamate.append(page)
        if page <= len(self._pagine):
            righe = self._pagine[page - 1]
            return Pagina(righe=righe, n_schede=len(righe), contesto="ctx")
        return Pagina(righe=[], n_schede=0, contesto="ctx")

    def chiave_slot(self, riga):
        # Stessa condizione delle sorgenti vere: senza calendario, o senza gli
        # id con cui si identifica un'agenda, non c'e' niente da chiedere.
        if not riga.ha_calendario or not (riga.result_id and riga.address_id):
            return None
        return ChiaveSlot(riga.result_id, riga.address_id,
                          dati=tuple(sorted(riga.extra.items())))

    def raccoglitore_slot(self, contesto, *, ricarica_contesto=None):
        slot = self._slot

        class _R(RaccoglitoreSlot):
            def slots(self, chiave, giorni):
                # Chiave completa: le sorgenti che identificano la SEDE (e non
                # la coppia medico-sede) hanno lo stesso address_id per medici
                # diversi, ed e' proprio quel caso che i test qui sotto coprono.
                return slot.get((chiave.result_id, chiave.address_id),
                                slot.get(chiave.address_id, []))

        return _R()


# --- isolamento tra sorgenti ------------------------------------------------

def test_stessi_id_su_due_sorgenti_restano_righe_distinte(tmp_path):
    """Due portali possono usare lo stesso id per medici diversi: senza
    `sorgente` nell'indice unico, il secondo sovrascriveva il primo."""
    st = Storage(tmp_path / "t.db")
    a = SorgenteFinta("alfa", [[_riga(rid="7", aid="7", nome="Dr. Alfa")]])
    b = SorgenteFinta("beta", [[_riga(rid="7", aid="7", nome="Dr. Beta")]])
    scrape_search("podologo", "Milano", sorgente=a, storage=st)
    scrape_search("podologo", "Milano", sorgente=b, storage=st)
    nomi = sorted(r["nome"] for r in st.query("SELECT nome FROM risultati"))
    st.close()
    assert nomi == ["Dr. Alfa", "Dr. Beta"], nomi


def test_run_su_una_sorgente_non_cancella_le_altre(tmp_path):
    """La sostituzione di fine corsa deve restare confinata alla sua sorgente."""
    st = Storage(tmp_path / "t.db")
    a = SorgenteFinta("alfa", [[_riga(rid="1", nome="Dr. Alfa")]])
    scrape_search("podologo", "Milano", sorgente=a, storage=st)
    # Beta trova medici completamente diversi per la stessa ricerca.
    b = SorgenteFinta("beta", [[_riga(rid="9", aid="9", nome="Dr. Beta")]])
    scrape_search("podologo", "Milano", sorgente=b, storage=st)
    n = st.count_risultati()
    st.close()
    assert n == 2, "il secondo run ha cancellato i dati del primo"


def test_slot_non_si_mescolano_tra_sorgenti(tmp_path):
    """`address_id` uguale su due portali: gli slot dell'uno non devono
    comparire come disponibilita' dell'altro."""
    st = Storage(tmp_path / "t.db")
    slot_alfa = [{"result_id": "7", "address_id": "7", "data": "2099-01-01",
                  "orario": "09:00", "prenotabile": True}]
    a = SorgenteFinta("alfa", [[_riga(rid="7", aid="7")]],
                      slot_per_chiave={"7": slot_alfa})
    b = SorgenteFinta("beta", [[_riga(rid="7", aid="7")]])  # nessuno slot
    scrape_search("podologo", "Milano", sorgente=a, storage=st, disponibilita=True)
    scrape_search("podologo", "Milano", sorgente=b, storage=st, disponibilita=True)

    prime = {r["sorgente"]: r["prima_disponibilita"]
             for r in search(st, Filtro(ordina="disponibilita"))}
    st.close()
    assert prime["alfa"] is not None
    assert prime["beta"] is None, "beta ha ereditato gli slot di alfa"


def test_raccolta_slot_di_beta_non_cancella_quelli_di_alfa(tmp_path):
    """`clear_disponibilita` prima cancellava per solo `address_id`."""
    st = Storage(tmp_path / "t.db")
    slot = [{"result_id": "7", "address_id": "7", "data": "2099-01-01",
             "orario": "09:00", "prenotabile": True}]
    a = SorgenteFinta("alfa", [[_riga(rid="7", aid="7")]], slot_per_chiave={"7": slot})
    scrape_search("podologo", "Milano", sorgente=a, storage=st, disponibilita=True)
    assert st.query("SELECT COUNT(*) FROM disponibilita")[0][0] == 1

    b = SorgenteFinta("beta", [[_riga(rid="7", aid="7")]])
    scrape_search("podologo", "Milano", sorgente=b, storage=st, disponibilita=True)
    n = st.query("SELECT COUNT(*) FROM disponibilita WHERE sorgente='alfa'")[0][0]
    st.close()
    assert n == 1, "beta ha cancellato gli slot di alfa"


# --- filtro per sorgente ----------------------------------------------------

def test_filtro_sorgenti(tmp_path):
    st = Storage(tmp_path / "t.db")
    scrape_search("podologo", "Milano", storage=st,
                  sorgente=SorgenteFinta("alfa", [[_riga(rid="1", nome="Dr. A")]]))
    scrape_search("podologo", "Milano", storage=st,
                  sorgente=SorgenteFinta("beta", [[_riga(rid="2", nome="Dr. B")]]))
    tutti = search(st, Filtro())
    solo_beta = search(st, Filtro(sorgenti=["beta"]))
    st.close()
    assert len(tutti) == 2
    assert [r["nome"] for r in solo_beta] == ["Dr. B"]


def test_nessuna_sorgente_selezionata_non_mostra_tutto(tmp_path):
    """Lista vuota = "nessuna fonte", non "nessun filtro". Prima, deselezionare
    tutti i portali nella GUI faceva ricomparire l'intero database."""
    st = Storage(tmp_path / "t.db")
    scrape_search("podologo", "Milano", storage=st,
                  sorgente=SorgenteFinta("alfa", [[_riga()]]))
    vuoto = search(st, Filtro(sorgenti=[]))
    tutti = search(st, Filtro(sorgenti=None))
    st.close()
    assert vuoto == []
    assert len(tutti) == 1


def test_slot_non_si_mescolano_tra_medici_della_stessa_sede(tmp_path):
    """Un portale puo' identificare la SEDE invece della coppia medico-sede
    (il `practiceId` di Doctolib): due medici dello stesso poliambulatorio
    condividono l'`address_id`. Gli slot dell'uno non sono quelli dell'altro,
    e raccogliere i secondi non deve cancellare i primi."""
    st = Storage(tmp_path / "t.db")
    slot_a = [{"result_id": "1", "address_id": "99", "data": "2099-01-01",
               "orario": "09:00", "prenotabile": True}]
    slot_b = [{"result_id": "2", "address_id": "99", "data": "2099-06-01",
               "orario": "15:00", "prenotabile": True}]
    src = SorgenteFinta(
        "poliambulatorio",
        [[_riga(rid="1", aid="99", nome="Dr. Uno"),
          _riga(rid="2", aid="99", nome="Dr. Due")]],
        slot_per_chiave={("1", "99"): slot_a, ("2", "99"): slot_b})
    scrape_search("podologo", "Milano", sorgente=src, storage=st,
                  disponibilita=True)

    prime = {r["nome"]: r["prima_disponibilita"]
             for r in search(st, Filtro(ordina="disponibilita"))}
    n = st.query("SELECT COUNT(*) FROM disponibilita")[0][0]
    st.close()
    assert n == 2, "la raccolta del secondo medico ha cancellato gli slot del primo"
    assert prime["Dr. Uno"].startswith("2099-01-01")
    assert prime["Dr. Due"].startswith("2099-06-01"), \
        "Dr. Due ha ereditato la disponibilità del collega di studio"


def test_sorgente_finisce_nei_risultati(tmp_path):
    st = Storage(tmp_path / "t.db")
    scrape_search("podologo", "Milano", storage=st,
                  sorgente=SorgenteFinta("alfa", [[_riga()]]))
    r = search(st, Filtro())[0]
    st.close()
    assert r["sorgente"] == "alfa"


# --- contratto della Sorgente ----------------------------------------------

def test_pagina_ultima_ferma_la_paginazione(tmp_path):
    """Chi conosce gia' il totale non deve sprecare una richiesta a vuoto."""

    class ConTotale(SorgenteFinta):
        def scarica_pagina(self, specializzazione, citta, page):
            self.chiamate.append(page)
            return Pagina(righe=[_riga(rid=str(page))], n_schede=1, ultima=True)

    st = Storage(tmp_path / "t.db")
    src = ConTotale("alfa", [])
    rep = scrape_search("podologo", "Milano", sorgente=src, storage=st,
                        max_pagine=10)
    st.close()
    assert src.chiamate == [1], "ha continuato a paginare dopo `ultima=True`"
    assert (rep.righe, rep.pagine) == (1, 1)


def test_disponibilita_ignorata_se_non_supportata(tmp_path):
    """Non deve fallire: la sorgente semplicemente non ha calendari."""

    class SenzaSlot(SorgenteFinta):
        supporta_disponibilita = False

    st = Storage(tmp_path / "t.db")
    rep = scrape_search("podologo", "Milano", storage=st, disponibilita=True,
                        sorgente=SenzaSlot("alfa", [[_riga()]]))
    n = st.query("SELECT COUNT(*) FROM disponibilita")[0][0]
    st.close()
    assert (rep.righe, rep.slot) == (1, 0)
    assert n == 0


def test_chiave_slot_none_salta_la_richiesta(tmp_path):
    """Chi non ha agenda online non va interrogato: e' cio' che tiene il run
    a decine invece che a centinaia di richieste."""
    st = Storage(tmp_path / "t.db")
    src = SorgenteFinta("alfa", [[_riga(rid="1", aid="1", calendario=False)]],
                        slot_per_chiave={"1": [{"result_id": "1",
                                                "address_id": "1",
                                                "data": "2099-01-01",
                                                "orario": "09:00",
                                                "prenotabile": True}]})
    rep = scrape_search("podologo", "Milano", sorgente=src, storage=st,
                        disponibilita=True)
    st.close()
    assert rep.slot == 0


def test_report_porta_il_nome_della_sorgente(tmp_path):
    st = Storage(tmp_path / "t.db")
    rep = scrape_search("podologo", "Milano", storage=st,
                        sorgente=SorgenteFinta("alfa", [[_riga()]]))
    st.close()
    assert rep.sorgente == "alfa"


# --- registro ---------------------------------------------------------------

def test_miodottore_e_registrata_e_default():
    assert "miodottore" in elenco()
    assert elenco()[0] == "miodottore", "il default della CLI cambierebbe"
    assert ottieni("miodottore").supporta_disponibilita is True


def test_sorgente_sconosciuta_elenca_quelle_valide():
    with pytest.raises(SorgenteSconosciuta) as e:
        ottieni("doctolib_inesistente")
    assert "miodottore" in str(e.value)


# --- migrazione di un DB preesistente ---------------------------------------

def test_migrazione_db_senza_colonna_sorgente(tmp_path):
    """Un DB creato dalla versione precedente deve aprirsi, non perdere righe e
    vedersele attribuite a MioDottore."""
    import sqlite3

    db = tmp_path / "vecchio.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE risultati (
            id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT, address_id TEXT,
            nome TEXT, tipo TEXT, specializzazione TEXT, citta TEXT,
            indirizzo TEXT, provincia TEXT, servizio TEXT, servizio_id TEXT,
            prezzo REAL, prezzo_e_da_min INTEGER, prezzo_raw TEXT, rating REAL,
            n_recensioni INTEGER, lat REAL, lng REAL, url_profilo TEXT,
            ha_calendario INTEGER, query_specializzazione TEXT, query_citta TEXT,
            data_scraping TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE disponibilita (
            id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT, address_id TEXT,
            servizio_id TEXT, data DATE, orario TEXT, prenotabile INTEGER,
            data_scraping TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE UNIQUE INDEX idx_ris_unico ON risultati(
            query_specializzazione, query_citta, COALESCE(result_id,''),
            COALESCE(address_id,''), COALESCE(servizio,''));
        INSERT INTO risultati (result_id,address_id,nome,query_specializzazione,
                               query_citta) VALUES ('1','2','Dr. Vecchio',
                               'podologo','Milano');
    """)
    con.commit()
    con.close()

    st = Storage(db)
    righe = st.query("SELECT nome, sorgente FROM risultati")
    indici = [r[0] for r in st.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ris_unico%'")]
    # E il DB migrato deve reggere una seconda sorgente con gli stessi id.
    scrape_search("podologo", "Milano", storage=st,
                  sorgente=SorgenteFinta("beta", [[_riga(rid="1", aid="2",
                                                         nome="Dr. Nuovo")]]))
    totale = st.count_risultati()
    st.close()

    assert [(r[0], r[1]) for r in righe] == [("Dr. Vecchio", "miodottore")]
    assert indici == ["idx_ris_unico_v2"], "il vecchio indice unico e' rimasto"
    assert totale == 2, "gli stessi id su una nuova sorgente si sono sovrascritti"
