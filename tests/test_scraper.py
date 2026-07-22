"""Regressioni sulla paginazione: una pagina vuota transitoria non deve
troncare silenziosamente la raccolta."""
from medici.fetcher import FetchError, FetchResult
from medici.scraper import scrape_search
from medici.storage import Storage


def _pagina(n_card: int, primo_id: int = 0) -> str:
    cards = "".join(
        f'<div data-id="result-item" data-result-id="{primo_id + i}" '
        f'data-doctor-name="Dr. {primo_id + i}" '
        f'data-ga4-entity-type="doctor"></div>'
        for i in range(n_card)
    )
    return f"<html><body>{cards}</body></html>"


class FetcherAPagine:
    """Restituisce le pagine programmate, una per chiamata."""

    def __init__(self, pagine):
        self.pagine = list(pagine)
        self.chiamate = []

    def get_search_page(self, spec, citta, page=1):
        self.chiamate.append(page)
        html = self.pagine.pop(0) if self.pagine else _pagina(0)
        # Un elemento della lista puo' essere un'eccezione: e' cosi' che si
        # programma un guasto di rete a una pagina precisa.
        if isinstance(html, Exception):
            raise html
        return FetchResult(url=f"http://x?page={page}", status=200, html=html)

    def get(self, url, **kw):
        # Fa parte del protocollo `FetcherRicerca` (serve alle disponibilita'),
        # ma questi test non le chiedono: se qualcuno ci arriva, e' un bug.
        raise AssertionError(f"richiesta HTTP inattesa: {url}")


def test_pagina_vuota_transitoria_non_ferma_la_paginazione(tmp_path):
    """Pagina 2 vuota per un singhiozzo: al secondo tentativo arrivano i dati."""
    f = FetcherAPagine([
        _pagina(2, 0),      # pagina 1
        _pagina(0),         # pagina 2: vuota per errore
        _pagina(2, 10),     # pagina 2: retry, dati veri
        _pagina(0),         # pagina 3: vuota
        _pagina(0),         # pagina 3: retry, vuota davvero -> stop
    ])
    st = Storage(tmp_path / "t.db")
    rep = scrape_search("podologo", "Milano", fetcher=f, storage=st, max_pagine=10)
    st.close()
    assert rep.righe == 4, "le righe della pagina 2 sono andate perse"
    assert rep.pagine == 2
    assert f.chiamate == [1, 2, 2, 3, 3]


def test_fine_paginazione_reale(tmp_path):
    f = FetcherAPagine([_pagina(2, 0), _pagina(0), _pagina(0)])
    st = Storage(tmp_path / "t.db")
    rep = scrape_search("podologo", "Milano", fetcher=f, storage=st, max_pagine=10)
    st.close()
    assert (rep.righe, rep.pagine) == (2, 1)


def test_progress_riporta_ogni_pagina(tmp_path):
    """L'avanzamento e' l'unico modo per distinguere 'sta lavorando' da 'e' fermo'."""
    eventi = []
    f = FetcherAPagine([_pagina(2, 0), _pagina(2, 10), _pagina(0), _pagina(0)])
    st = Storage(tmp_path / "t.db")
    scrape_search("podologo", "Milano", fetcher=f, storage=st, max_pagine=10,
                  progress=lambda msg, frac=None: eventi.append(msg))
    st.close()
    assert any("pagina 1" in e.lower() for e in eventi)
    assert any("2 schede" in e for e in eventi)
    assert any("4 righe" in e for e in eventi), eventi


def test_senza_progress_non_esplode(tmp_path):
    """`progress` e' opzionale: la CLI puo' non passarlo."""
    f = FetcherAPagine([_pagina(1, 0), _pagina(0), _pagina(0)])
    st = Storage(tmp_path / "t.db")
    assert scrape_search("x", "y", fetcher=f, storage=st).righe == 1
    st.close()


def test_annullamento_conserva_il_raccolto(tmp_path):
    """Stop a meta' corsa: le pagine gia' scaricate restano nel DB."""
    f = FetcherAPagine([_pagina(2, 0), _pagina(2, 10), _pagina(2, 20)])
    st = Storage(tmp_path / "t.db")
    scaricate = []
    rep = scrape_search(
        "podologo", "Milano", fetcher=f, storage=st, max_pagine=10,
        progress=lambda msg, frac=None: scaricate.append(msg),
        # Ferma dopo la prima pagina.
        annullato=lambda: len(f.chiamate) >= 1)
    assert rep.annullato is True
    assert rep.righe == 2
    assert st.count_risultati() == 2, "i dati raccolti sono stati persi"
    st.close()
    assert f.chiamate == [1], "ha continuato a scaricare dopo l'annullamento"


def test_senza_annullato_scarica_tutto(tmp_path):
    f = FetcherAPagine([_pagina(2, 0), _pagina(2, 10), _pagina(0), _pagina(0)])
    st = Storage(tmp_path / "t.db")
    rep = scrape_search("x", "y", fetcher=f, storage=st, max_pagine=10)
    st.close()
    assert (rep.righe, rep.annullato) == (4, False)


def test_run_interrotto_non_cancella_i_dati_precedenti(tmp_path):
    """Prima la sostituzione cancellava tutto all'inizio: uno stop a meta'
    lasciava l'utente con MENO dati di quanti ne avesse."""
    db = tmp_path / "t.db"
    st = Storage(db)
    scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                  fetcher=FetcherAPagine([_pagina(3, 0), _pagina(0), _pagina(0)]))
    assert st.count_risultati() == 3

    # Secondo giro, fermato prima ancora di scaricare la pagina 1.
    rep = scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                        fetcher=FetcherAPagine([_pagina(3, 100)]),
                        annullato=lambda: True)
    assert rep.annullato is True
    assert st.count_risultati() == 3, "i dati vecchi sono stati buttati"
    st.close()


def test_run_completo_sostituisce_i_medici_spariti(tmp_path):
    """Chi non compare piu' nei risultati va rimosso, ma solo a fine corsa."""
    db = tmp_path / "t.db"
    st = Storage(db)
    scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                  fetcher=FetcherAPagine([_pagina(3, 0), _pagina(0), _pagina(0)]))
    # Secondo giro: uno solo dei tre medici e' ancora online.
    rep = scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                        fetcher=FetcherAPagine([_pagina(1, 0), _pagina(0), _pagina(0)]))
    nomi = [r["nome"] for r in st.query("SELECT nome FROM risultati")]
    st.close()
    assert nomi == ["Dr. 0"], nomi
    # Le stesse schede ritrovate non sono "nuove", ma il run e' riuscito.
    assert (rep.righe, rep.nuove) == (1, 0)


def test_altre_ricerche_non_vengono_toccate(tmp_path):
    st = Storage(tmp_path / "t.db")
    scrape_search("dermatologo", "Roma", storage=st, max_pagine=10,
                  fetcher=FetcherAPagine([_pagina(2, 0), _pagina(0), _pagina(0)]))
    scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                  fetcher=FetcherAPagine([_pagina(2, 0), _pagina(0), _pagina(0)]))
    n = st.count_risultati()
    st.close()
    assert n == 4, "la pulizia ha invaso un'altra ricerca"


# --- guasti di rete ---------------------------------------------------------

def test_rete_giu_e_un_esito_non_un_traceback(tmp_path):
    """Un `FetchError` che sfugge farebbe morire la CLI a metà pagina e, con
    più portali, impedirebbe anche a quelli sani di partire."""
    f = FetcherAPagine([FetchError("connessione rifiutata")])
    st = Storage(tmp_path / "t.db")
    rep = scrape_search("podologo", "Milano", fetcher=f, storage=st, max_pagine=10)
    st.close()
    assert rep.righe == 0 and rep.pagine == 0
    assert rep.errore and "pagina 1" in rep.errore
    assert rep.bloccato is False


def test_guasto_a_meta_corsa_non_cancella_i_dati_precedenti(tmp_path):
    """`visti` contiene solo le pagine arrivate: usarlo per la pulizia finale
    cancellerebbe medici veri, ancora online, raccolti da un run completo."""
    db = tmp_path / "t.db"
    st = Storage(db)
    scrape_search("podologo", "Milano", storage=st, max_pagine=10,
                  fetcher=FetcherAPagine([_pagina(3, 0), _pagina(0), _pagina(0)]))
    assert st.count_risultati() == 3

    # Secondo giro: pagina 1 buona (un solo medico), poi la rete cade.
    rep = scrape_search(
        "podologo", "Milano", storage=st, max_pagine=10,
        fetcher=FetcherAPagine([_pagina(1, 0), _pagina(0),
                                FetchError("timeout")]))
    n = st.count_risultati()
    st.close()
    assert rep.errore, "un run incompleto non può essere riportato come riuscito"
    assert rep.righe == 1
    assert n == 3, "i medici degli altri run sono stati cancellati"


def test_export_csv_accetta_dict(tmp_path):
    """La CLI passa dict, non sqlite3.Row: l'unica implementazione deve reggerli."""
    out = tmp_path / "x.csv"
    n = Storage.export_rows_csv([{"nome": "Dr. À", "prezzo": 50}], out)
    testo = out.read_text(encoding="utf-8-sig")
    assert n == 1
    assert testo.splitlines()[0] == "nome,prezzo"
    assert "Dr. À" in testo
