"""app.py — GUI Streamlit per MioDottore scraper.

Avvio:  streamlit run app.py

Riusa lo stesso motore di query della CLI (medici.query_engine) — nessuna
logica duplicata. Permette scraping on-demand e consultazione con ordinamento
nativo per colonna del dataframe.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from medici.storage import Storage
from medici.query_engine import Filtro, avvisi, search, stats
from medici.scraper import risolvi_sorgente, scrape_search
from medici.sorgenti import elenco as sorgenti_disponibili, etichetta_di

# Senza questo i log INFO dello scraper ("Pagina 3: 20 card -> 31 righe") non
# comparivano da nessuna parte: ne' nella GUI ne' nel terminale di streamlit.
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

# Stesso nome della repo e della CLI: la tab del browser e il titolo in pagina
# devono far riconoscere il progetto, non descriverlo.
st.set_page_config(page_title="Cerca medici", layout="wide")
st.title("🩺 Cerca medici")


@st.cache_resource
def storage_corrente() -> Storage:
    return Storage()


# Streamlit riesegue app.py a ogni interazione ma NON re-importa i moduli:
# dopo una modifica a medici/*.py la classe in memoria resta quella vecchia
# finche' non si riavvia `streamlit run`. Senza questo controllo il primo uso di
# un metodo nuovo faceva morire lo script con un AttributeError - e siccome
# accadeva PRIMA del blocco di scraping, anche "Avvia scraping" smetteva di
# rispondere. Meglio un avviso leggibile e una GUI che continua a funzionare.
_API_ATTESE = ("eta_dati_ore", "riepilogo_query", "servizi_noti",
               "clear_query_tranne", "sorgenti_presenti")
_MANCANTI = [m for m in _API_ATTESE if not hasattr(Storage, m)]
if _MANCANTI:
    st.sidebar.warning(
        "Streamlit sta usando una versione vecchia del codice "
        f"(manca `Storage.{_MANCANTI[0]}`). Riavvia `streamlit run app.py` "
        "dal terminale: alcune funzioni sono disattivate.")


def annullamento_richiesto() -> bool:
    """True se l'utente ha premuto Stop (o ha interagito con un altro widget).

    Streamlit non consegna i click a uno script gia' in esecuzione: l'unico
    segnale che arriva davvero e' la richiesta di stop/rerun registrata nel
    contesto. La leggiamo senza consumarla — `on_scriptrunner_yield()`
    azzererebbe una RERUN pendente e la GUI resterebbe indietro. E' API interna:
    se cambia, si degrada a "non annullabile" invece di rompere lo scraping.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        ctx = get_script_run_ctx()
        if ctx is None or ctx.script_requests is None:
            return False        # fuori da uno ScriptRunner (import, test)
        return ctx.script_requests._state.name != "CONTINUE"
    except Exception:
        return False


# Quale sottoinsieme del database sta guardando l'utente. La tabella mostra
# tutte le ricerche insieme, quindi dopo aver scaricato «dentista» le sue righe
# arriverebbero in mezzo a quelle di «podologo»: questa vista le separa senza
# cancellare niente dal DB (riscaricarle costa ore di richieste ai portali).
VISTA_TUTTO = None          # tutte le ricerche presenti
VISTA_VUOTA = "__vuota__"   # nessuna: tabella pulita, in attesa di una ricerca

# I widget della sezione «2. Filtri»: senza una `key` Streamlit non permette di
# riportarli al default.
_CHIAVI_FILTRI = (
    "f_fonti", "f_spec", "f_citta", "f_serv", "f_serv_scelto", "f_prezzo_min",
    "f_prezzo_max", "f_ordina", "f_desc", "f_min_rec", "f_min_rating",
    "f_solo_prezzo", "f_solo_pren", "f_unifica", "f_entro", "f_vicino",
    "f_limite", "mostra_tecniche")


def azzera_filtri() -> None:
    """Riporta i filtri ai default cancellando le loro chiavi.

    Va fatto in un callback, cioe' PRIMA che i widget vengano ricreati:
    assegnare st.session_state["f_spec"] dopo averlo istanziato solleva
    StreamlitAPIException.
    """
    for chiave in _CHIAVI_FILTRI:
        st.session_state.pop(chiave, None)


def nuova_ricerca() -> None:
    azzera_filtri()
    st.session_state["vista"] = VISTA_VUOTA


st.sidebar.button(
    "🔄 Nuova ricerca da zero", on_click=nuova_ricerca, width="stretch",
    help="Svuota la tabella e azzera i filtri, per ripartire con un'altra "
         "specializzazione. I dati già scaricati restano nel database e si "
         "rivedono con un clic: non serve rifare lo scraping.")

st.sidebar.header("1. Scraping (popola il DB)")
with st.sidebar.form("scrape_form"):
    sc_spec = st.text_input("Specializzazione", value="podologo")
    sc_citta = st.text_input("Città", value="Milano")
    # L'elenco viene dal registro delle sorgenti: registrarne una nuova la fa
    # comparire qui senza toccare la GUI.
    sc_fonti = st.multiselect(
        "Portali", sorgenti_disponibili(), default=sorgenti_disponibili(),
        format_func=etichetta_di,
        help="Ogni portale viene interrogato separatamente e i risultati "
             "finiscono nello stesso database, distinti dalla colonna «fonte».")
    sc_pagine = st.number_input("Max pagine", 1, 40, 5)
    sc_disp = st.checkbox("Raccogli disponibilità (endpoint /api/v3 — vedi README)")
    sc_giorni = st.number_input("Giorni disponibilità", 1, 14, 5)
    submitted = st.form_submit_button("Avvia scraping")
st.sidebar.caption("Per fermarlo: **Stop** in alto a destra. I dati già "
                   "scaricati restano nel database; poi ricarica la pagina "
                   "(Streamlit chiude la sessione dopo uno stop).")

# Quanto sono vecchi i dati di QUESTA ricerca: senza, l'unico modo per saperlo
# era riscaricare tutto — cioe' proprio la cosa che si voleva evitare.
_eta = (None if _MANCANTI
        else storage_corrente().eta_dati_ore(sc_spec, sc_citta))
if _eta is not None:
    if _eta < 1:
        st.sidebar.success(f"Dati di {sc_spec}/{sc_citta} aggiornati "
                           f"{int(_eta * 60)} minuti fa: non serve riscaricarli.")
    elif _eta < 48:
        st.sidebar.info(f"Dati di {sc_spec}/{sc_citta} di {int(_eta)} ore fa. "
                        "I prezzi cambiano di rado; le disponibilità in ore.")
    else:
        st.sidebar.warning(f"Dati di {sc_spec}/{sc_citta} di "
                           f"{int(_eta / 24)} giorni fa.")

if submitted and not sc_fonti:
    st.warning("Seleziona almeno un portale da interrogare.")
elif submitted:
    # Uno spinner con testo fisso non distingue "sta lavorando" da "e' piantato".
    # Qui mostriamo l'URL interrogato e ogni passo man mano che avviene.
    with st.status(f"Scraping {sc_spec}/{sc_citta}…", expanded=True) as stato:
        sorgenti = [risolvi_sorgente(n) for n in sc_fonti]
        for _src in sorgenti:
            st.caption(f"{_src.etichetta}: {_src.url_ricerca(sc_spec, sc_citta)}")
        barra = st.progress(0.0)
        riga_stato = st.empty()

        def avanzamento(messaggio: str, frazione=None) -> None:
            # Con uno stop pendente, ogni chiamata a un widget farebbe alzare a
            # Streamlit la sua StopException proprio qui, a meta' di una pagina.
            # Tacendo, lasciamo che sia `annullato` a fermare il ciclo al giro
            # dopo: lo scraper chiude i suoi conti e il DB resta coerente.
            if annullamento_richiesto():
                return
            riga_stato.write(messaggio)
            if frazione is not None:
                barra.progress(min(max(frazione, 0.0), 1.0))

        # Riusiamo la connessione in cache invece di aprirne una seconda e
        # svuotare la cache: due connessioni in scrittura sullo stesso file
        # (piu' quella vecchia mai chiusa) portavano a lock e a connessioni
        # orfane. Storage e' ora thread-safe, quindi condividerla e' corretto.
        reports = []
        for _src in sorgenti:
            if annullamento_richiesto():
                break
            reports.append(scrape_search(
                sc_spec, sc_citta, sorgente=_src, storage=storage_corrente(),
                max_pagine=int(sc_pagine),
                disponibilita=sc_disp,
                giorni_disponibilita=int(sc_giorni),
                progress=avanzamento,
                annullato=annullamento_richiesto))
        barra.progress(1.0)
        tot_righe = sum(r.righe for r in reports)
        esito = "complete" if tot_righe else "error"
        stato.update(label=f"Scraping {sc_spec}/{sc_citta}: {tot_righe} righe "
                           f"da {len(reports)} portali", state=esito,
                     expanded=False)

    # Un portale puo' bloccarsi mentre l'altro riesce: un esito unico
    # nasconderebbe proprio l'informazione che serve per capire cosa rifare.
    for rep in reports:
        nome = etichetta_di(rep.sorgente)
        if rep.bloccato:
            st.error(f"{nome}: bloccato — {rep.errore}")
        elif rep.annullato:
            st.info(f"{nome}: interrotto, {rep.righe} righe già salvate.")
        elif rep.errore:
            # Il portale ha gia' spiegato perché non ha prodotto nulla (slug
            # inesistente, città non coperta): quel messaggio contiene i
            # suggerimenti, il generico «0 righe» no.
            st.warning(f"{nome}: {rep.errore}"
                       + (f" {rep.righe} righe ({rep.nuove} nuove) sono "
                          f"comunque nel database." if rep.righe else ""))
        elif rep.righe == 0:
            st.warning(f"{nome}: 0 righe. Città non riconosciuta o formato cambiato?")
        else:
            msg = (f"{nome}: {rep.righe} righe da {rep.pagine} pagine "
                   f"({rep.nuove} nuove rispetto a prima).")
            if sc_disp:
                msg += f" {rep.slot} slot di disponibilità."
            st.success(msg)

    # Chi ha appena scaricato «dentista» vuole vedere i dentisti, non ritrovarli
    # sparsi in mezzo ai podologi di ieri. Le altre ricerche restano nel DB, a un
    # clic di distanza.
    if any(rep.righe for rep in reports):
        st.session_state["vista"] = (sc_spec, sc_citta)


storage = storage_corrente()

# I filtri lavorano SOLO sul database: cambiare servizio o prezzo non fa
# ripartire nessuno scraping. Mostrare cosa c'e' gia' evita di riscaricarlo.
gia_presenti = [] if _MANCANTI else storage.riepilogo_query()
if gia_presenti:
    with st.sidebar.expander(f"Dati già nel database ({len(gia_presenti)} ricerche)"):
        st.dataframe(pd.DataFrame([dict(r) for r in gia_presenti]),
                     hide_index=True, width="stretch")
        st.caption("I filtri qui sotto interrogano questi dati: nessuna nuova "
                   "richiesta al sito.")

st.sidebar.header("2. Filtri di ricerca")
st.sidebar.button(
    "🧹 Azzera solo i filtri", on_click=azzera_filtri, width="stretch",
    help="Riporta i filtri qui sotto ai valori iniziali senza toccare la "
         "tabella né il database.")

# Le opzioni sono i portali PRESENTI NEL DB, non quelli registrati: offrire un
# filtro su una fonte mai scaricata darebbe solo zero risultati.
_fonti_db = [] if _MANCANTI else storage.sorgenti_presenti()
f_fonti = st.sidebar.multiselect(
    "Fonti", _fonti_db, default=_fonti_db, format_func=etichetta_di, key="f_fonti",
    help="Deseleziona un portale per escluderlo dai risultati mostrati.")
if _fonti_db and not f_fonti:
    # Senza questo avviso l'unico segnale sarebbe un generico "nessun risultato",
    # con tutti gli altri filtri sotto accusa al posto di quello giusto.
    st.sidebar.warning("Nessuna fonte selezionata: non c'è nulla da mostrare.")
f_spec = st.sidebar.text_input("Specializzazione (filtro)", value="", key="f_spec")
f_citta = st.sidebar.text_input("Città (filtro)", value="", key="f_citta")
f_serv = st.sidebar.text_input(
    "Servizio (parole chiave)", value="", key="f_serv",
    help="Cerca per parole, in qualsiasi ordine: «visita controllo» trova sia "
         "«Visita podologica di controllo» sia «Visita di controllo podologica».")
servizi = [] if _MANCANTI else storage.servizi_noti()
if servizi:
    scelto = st.sidebar.selectbox("…oppure scegli tra quelli trovati",
                                  ["—"] + servizi, key="f_serv_scelto")
    if scelto != "—" and not f_serv:
        f_serv = scelto
c_min, c_max = st.sidebar.columns(2)
f_prezzo_min = c_min.number_input("Prezzo min €", 0, 5000, 0, 10, key="f_prezzo_min")
f_prezzo_max = c_max.number_input("Prezzo max € (0=off)", 0, 5000, 0, 10,
                                  key="f_prezzo_max")
f_ordina = st.sidebar.selectbox(
    "Ordina per", ["prezzo", "rating", "recensioni", "disponibilita", "distanza"],
    key="f_ordina")
f_desc = st.sidebar.checkbox(
    "Inverti ordinamento", key="f_desc",
    help="Prezzo: dal più caro. Rating/recensioni: dal peggiore.")
f_min_rec = st.sidebar.slider("Min recensioni", 0, 1000, 0, 10, key="f_min_rec")
f_min_rating = st.sidebar.slider("Min rating", 0.0, 5.0, 0.0, 0.5, key="f_min_rating")
f_solo_prezzo = st.sidebar.checkbox("Solo con prezzo pubblicato", key="f_solo_prezzo")
f_solo_pren = st.sidebar.checkbox(
    "Solo prenotabili online", key="f_solo_pren",
    help="Esclude chi è elencato con il listino ma non ha attivato l'agenda "
         "su MioDottore (sul profilo appare «Inattivo»: si prenota per telefono).")
f_unifica = st.sidebar.checkbox(
    "Unisci lo stesso medico tra portali", key="f_unifica",
    help="Fonde in una riga sola le schede che i due portali hanno dello stesso "
         "medico (stesso cognome, sedi a meno di 150 m). Il prezzo mostrato è "
         "il minimo; la colonna «disponibile_su» elenca quanto chiede ciascun "
         "portale. Togli la spunta per rivedere le righe grezze.")
f_entro = st.sidebar.number_input("Disponibile entro (giorni, 0=off)", 0, 30, 0,
                                  key="f_entro")
f_vicino = st.sidebar.text_input("Vicino a (lat,lng) per distanza", value="",
                                 key="f_vicino")
f_limite = st.sidebar.number_input("Limite risultati (0=tutti)", 0, 5000, 100,
                                   key="f_limite")


def _vicino():
    if not f_vicino.strip():
        return None, None
    try:
        lat, lng = f_vicino.split(",")
        return float(lat), float(lng)
    except ValueError:
        st.sidebar.error("Formato: lat,lng (es. 45.4642,9.1900)")
        return None, None


vlat, vlng = _vicino()

if storage.count_risultati() == 0:
    st.info("Il database è vuoto. Usa il pannello di scraping a sinistra per iniziare.")
    st.stop()

_vista = st.session_state.get("vista", VISTA_TUTTO)
if _vista == VISTA_VUOTA:
    st.info("Tabella pulita. Imposta una nuova ricerca nel pannello a sinistra "
            "e premi **Avvia scraping**.")
    # Senza questa via di ritorno «pulisci» sarebbe indistinguibile da
    # «cancella»: i dati ci sono ancora, ma l'utente non avrebbe modo di saperlo.
    st.button("Rivedi i dati già nel database",
              on_click=lambda: st.session_state.update(vista=VISTA_TUTTO))
    st.stop()
# Da qui in poi la vista e' solo «tutto» (None) o una coppia (spec, citta):
# il caso «vuota» e' gia' uscito con st.stop() qui sopra.
vista: tuple | None = _vista if isinstance(_vista, tuple) else None
if vista is not None:
    riga, tasto = st.columns([4, 1])
    riga.caption(f"Stai vedendo solo la ricerca **{vista[0]} / {vista[1]}**. "
                 "Le altre sono comunque nel database.")
    tasto.button("Mostra tutte le ricerche", width="stretch",
                 on_click=lambda: st.session_state.update(vista=VISTA_TUTTO))

filtro = Filtro(
    specializzazione=f_spec or None,
    citta=f_citta or None,
    ricerca=vista,
    servizio=f_serv or None,
    # Tutte selezionate = nessun filtro: evita una IN(...) inutile e lascia
    # passare eventuali fonti dismesse ancora presenti nel DB.
    sorgenti=None if set(f_fonti) == set(_fonti_db) else f_fonti,
    min_recensioni=int(f_min_rec),
    min_rating=float(f_min_rating),
    prezzo_max=float(f_prezzo_max) if f_prezzo_max else None,
    prezzo_min=float(f_prezzo_min) if f_prezzo_min else None,
    solo_con_prezzo=f_solo_prezzo,
    solo_prenotabili=f_solo_pren,
    disponibile_entro_giorni=int(f_entro) if f_entro else None,
    unifica_duplicati=f_unifica,
    ordina=f_ordina,
    discendente=f_desc,
    limite=int(f_limite) if f_limite else None,
    vicino_lat=vlat,
    vicino_lng=vlng,
)

# Un filtro che non puo' funzionare (ordinamento per distanza senza un punto di
# riferimento, disponibilità mai raccolte) si manifesta come «nessun risultato»:
# senza questo avviso l'utente sospetta gli altri filtri, che sono innocenti.
for avviso in avvisi(filtro, storage):
    st.warning(avviso)

results = search(storage, filtro)
s = stats(results)

colonne = st.columns(4 if f_unifica else 3)
colonne[0].metric("Risultati", s["totale"])
colonne[1].metric("Con prezzo", s["con_prezzo"])
colonne[2].metric("Senza prezzo", s["senza_prezzo"])
if f_unifica:
    # Quanti cluster hanno davvero unito qualcosa: e' il numero che dice se il
    # clustering sta funzionando o se sta fondendo tutto (o niente).
    colonne[3].metric("Su più portali",
                      sum(1 for r in results if (r.get("n_fonti") or 1) > 1))

# Con la sidebar lunga, i filtri attivi finiscono sotto la piega: chi guarda
# solo la tabella non ha modo di sapere che sta vedendo un sottoinsieme, e
# attribuisce allo scraping dei risultati mancanti che ha escluso da solo.
_attivi = [etichetta for attivo, etichetta in (
    (f_spec, f"specializzazione «{f_spec}»"),
    (f_citta, f"città «{f_citta}»"),
    (f_serv, f"servizio «{f_serv}»"),
    (_fonti_db and set(f_fonti) != set(_fonti_db),
     f"solo {len(f_fonti)} portali su {len(_fonti_db)}"),
    (f_prezzo_min, f"da {f_prezzo_min} €"),
    (f_prezzo_max, f"fino a {f_prezzo_max} €"),
    (f_min_rec, f"min {f_min_rec} recensioni"),
    (f_min_rating, f"min {f_min_rating} di rating"),
    (f_solo_prezzo, "solo con prezzo"),
    (f_solo_pren, "solo prenotabili online"),
    (f_entro, f"disponibili entro {f_entro} giorni"),
) if attivo]
if _attivi:
    st.caption("Filtri attivi: " + " · ".join(_attivi)
               + " — «Azzera i filtri» nella barra a sinistra li toglie tutti.")

if not results:
    st.warning("Nessun risultato con questi filtri.")
    st.stop()

df = pd.DataFrame(results)
if "sorgente" in df.columns:
    df["sorgente"] = df["sorgente"].map(etichetta_di)
# Colonne principali in testa, poi il resto.
preferite = [c for c in [
    "sorgente", "disponibile_su", "nome", "indirizzo", "citta", "servizio",
    "prezzo", "prezzo_e_da_min", "rating", "n_recensioni", "ha_calendario",
    "prima_disponibilita", "distanza_km", "url_profilo",
] if c in df.columns]
altre = [c for c in df.columns if c not in preferite]
df = df[preferite + altre]

# Gli identificativi interni, le coordinate e i timestamp servono a far
# funzionare il programma, non a scegliere un medico: in mezzo alle colonne
# utili obbligano a scorrere di lato per arrivare al prezzo. Restano a un clic
# di distanza perche' sono cio' che serve per verificare un dato sospetto.
TECNICHE = ("id", "result_id", "address_id", "servizio_id", "lat", "lng",
            "query_specializzazione", "query_citta", "data_scraping",
            "prezzo_raw", "n_fonti")
if not st.checkbox("Mostra le colonne tecniche (id, coordinate, data di "
                   "scraping)", value=False, key="mostra_tecniche"):
    df = df.drop(columns=[c for c in TECNICHE if c in df.columns])

# `width="stretch"` e non `use_container_width=True`: quest'ultimo e' deprecato
# da Streamlit 1.49 e la data di rimozione annunciata e' gia' passata — resta un
# avviso a ogni render finche' regge, poi smette di funzionare.
st.dataframe(df, width="stretch", hide_index=True,
             column_config={
                 "sorgente": st.column_config.TextColumn("Fonte"),
                 "disponibile_su": st.column_config.TextColumn(
                     "Disponibile su",
                     help="Quanto chiede ciascun portale per questo medico. "
                          "«n/d» = quel portale non pubblica il prezzo, non "
                          "che il medico non c'è."),
                 "url_profilo": st.column_config.LinkColumn("Profilo"),
                 "prezzo": st.column_config.NumberColumn("Prezzo €", format="%.0f"),
                 "prezzo_e_da_min": st.column_config.CheckboxColumn(
                     "«da»",
                     help="Il prezzo è un minimo («da 30 €»), non una tariffa "
                          "fissa: la visita può costare di più."),
                 "prima_disponibilita": st.column_config.TextColumn(
                     "1ª disponibilità"),
                 "n_recensioni": st.column_config.NumberColumn("Recensioni"),
                 "distanza_km": st.column_config.NumberColumn(
                     "Distanza km", format="%.1f"),
                 "ha_calendario": st.column_config.CheckboxColumn(
                     "Prenot. online",
                     help="Senza spunta il profilo risulta «Inattivo»: "
                          "niente agenda online, si prenota per telefono."),
             })

col_csv, col_jsonl = st.columns(2)
col_csv.download_button(
    "⬇️ Esporta CSV",
    data=df.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"medici_{f_ordina}.csv",
    mime="text/csv",
)
col_jsonl.download_button(
    "⬇️ Esporta JSON Lines",
    # `or ""`: senza un file di destinazione `to_json` restituisce sempre la
    # stringa, ma e' dichiarato Optional (scrivendo su file torna None) e
    # `download_button` non accetta None.
    data=df.to_json(orient="records", lines=True, force_ascii=False) or "",
    file_name=f"medici_{f_ordina}.jsonl",
    mime="application/x-ndjson",
    help="Un oggetto per riga: un file interrotto a metà scaricamento resta "
         "comunque leggibile fino all'ultima riga completa.",
)

st.caption(f"Prezzo pubblicato: {s['con_prezzo']} risultati su {s['totale']}. "
           "I prezzi sono pubblicati solo da una minoranza dei medici (limite della "
           "piattaforma, non dello scraper).")
