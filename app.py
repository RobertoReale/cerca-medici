"""app.py — GUI Streamlit di cerca-medici (MioDottore, Doctolib).

Avvio:  streamlit run app.py

Riusa lo stesso motore di query della CLI (medici.query_engine) — nessuna
logica duplicata. Permette scraping on-demand e consultazione con ordinamento
nativo per colonna del dataframe.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from medici.storage import DEFAULT_DB, Storage
from medici.query_engine import (
    ORDINA, Filtro, avvisi, colonne_visibili, search_con_totali,
)
from medici.scraper import risolvi_sorgente, scrape_search
from medici.sorgenti import elenco as sorgenti_disponibili, etichetta_di
from medici.testo import n_pagine

# Senza questo i log INFO dello scraper ("Pagina 3: 20 card -> 31 righe") non
# comparivano da nessuna parte: ne' nella GUI ne' nel terminale di streamlit.
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

# Stesso nome della repo e della CLI: la tab del browser e il titolo in pagina
# devono far riconoscere il progetto, non descriverlo.
st.set_page_config(page_title="Cerca medici", layout="wide")
st.title("🩺 Cerca medici")


@st.cache_resource
def storage_corrente(db_path: str | None = None) -> Storage:
    """La connessione al DB scelto. Una per percorso, tenuta in cache.

    L'argomento fa parte della chiave di cache: cambiare percorso apre una
    seconda connessione invece di riusare quella sbagliata, e tornare al
    percorso di prima non ne riapre una terza.
    """
    return Storage(db_path or None)


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
# arriverebbero in mezzo a quelle di «cardiologo»: questa vista le separa senza
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
    # «Ripartire con un'altra specializzazione»: lasciare scritta la vecchia
    # nel modulo invitava a rilanciare la stessa ricerca. La citta' resta,
    # perche' di solito e' quella che non cambia.
    st.session_state.pop("sc_spec", None)
    st.session_state["vista"] = VISTA_VUOTA


st.sidebar.button(
    "🔄 Nuova ricerca da zero", on_click=nuova_ricerca, width="stretch",
    help="Svuota la tabella e azzera i filtri, per ripartire con un'altra "
         "specializzazione. I dati già scaricati restano nel database e si "
         "rivedono con un clic: non serve rifare lo scraping.")

# Gli equivalenti delle due opzioni globali della CLI (`--db`, `--verbose`).
# Chiuso di default perche' non servono quasi mai, ma senza di loro la GUI era
# inchiodata a un database solo: chi teneva raccolte separate (una per citta',
# o una di prova) poteva usarle solo da riga di comando.
with st.sidebar.expander("⚙️ Impostazioni avanzate"):
    db_scelto = st.text_input(
        "Database SQLite", value="", placeholder=str(DEFAULT_DB),
        help="Lascia vuoto per il database predefinito (data/medici.db, con "
             "fallback sul vecchio data/miodottore.db se presente). È la "
             "stessa opzione di `--db` nella CLI: un percorso diverso apre una "
             "raccolta separata, e un file che non esiste viene creato vuoto.")
    log_esteso = st.checkbox(
        "Log dettagliato", value=True,
        help="Come `--verbose` nella CLI: pagina per pagina, quante schede e "
             "quante righe. Compare nel terminale da cui hai lanciato "
             "`streamlit run`, non in questa pagina.")
# Il livello si applica al logger radice a ogni rerun: e' quello che rende la
# casella effettiva senza riavviare streamlit.
logging.getLogger().setLevel(logging.INFO if log_esteso else logging.WARNING)
DB_SCELTO = db_scelto.strip() or None

st.sidebar.header("1. Scraping (popola il DB)")
with st.sidebar.form("scrape_form"):
    sc_spec = st.text_input("Specializzazione", value="", placeholder="es. cardiologo",
                            key="sc_spec")
    sc_citta = st.text_input("Città", value="Milano")
    # L'elenco viene dal registro delle sorgenti: registrarne una nuova la fa
    # comparire qui senza toccare la GUI.
    sc_fonti = st.multiselect(
        "Portali", sorgenti_disponibili(), default=sorgenti_disponibili(),
        format_func=etichetta_di,
        help="Ogni portale viene interrogato separatamente e i risultati "
             "finiscono nello stesso database, distinti dalla colonna «fonte».")
    # Gli stessi estremi della CLI (`--max-pagine`, `--giorni`), che non ha un
    # tetto: prima la GUI si fermava a 40 pagine e 14 giorni, quindi le raccolte
    # piu' grandi si potevano fare solo da riga di comando.
    sc_pagine = st.number_input(
        "Max pagine", 1, 500, 5,
        help="Attenzione: un giro più corto del precedente considera «spariti» "
             "i medici delle pagine che non ha visitato, e li rimuove.")
    # Un campo solo, con 0 = spento, come «Prezzo max» e «Disponibile entro»
    # piu' sotto. Prima erano una casella e un numero, ma dentro un `st.form` la
    # casella non puo' disattivare il numero (il form non riesegue lo script
    # finche' non lo si invia): si poteva impostare «10 giorni» a casella
    # spenta, senza che succedesse niente.
    sc_giorni = st.number_input(
        "Giorni di disponibilità (0 = non raccoglierle)", 0, 90, 0,
        help="Oltre ad anagrafica e prezzi, scarica gli orari liberi dei "
             "prossimi N giorni. Allunga lo scraping di 1–3 secondi per "
             "indirizzo e usa endpoint che il robots.txt dei portali esclude "
             "(vedi README). Doctolib ne restituisce al massimo 7, e in "
             "pratica quasi sempre nessuno.")
    sc_disp = sc_giorni > 0
    submitted = st.form_submit_button("Avvia scraping", type="primary",
                                      width="stretch")
st.sidebar.caption("Per fermarlo: **Stop** in alto a destra. I dati già "
                   "scaricati restano nel database; poi ricarica la pagina "
                   "(Streamlit chiude la sessione dopo uno stop).")

# Quanto sono vecchi i dati di QUESTA ricerca: senza, l'unico modo per saperlo
# era riscaricare tutto — cioe' proprio la cosa che si voleva evitare.
_eta = (None if _MANCANTI or not sc_spec.strip()
        else storage_corrente(DB_SCELTO).eta_dati_ore(sc_spec.strip(), sc_citta.strip()))
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

if submitted and not sc_spec.strip():
    st.warning("Inserisci una specializzazione da cercare.")
elif submitted and not sc_fonti:
    st.warning("Seleziona almeno un portale da interrogare.")
elif submitted:
    # Uno spinner con testo fisso non distingue "sta lavorando" da "e' piantato".
    # Qui mostriamo l'URL interrogato e ogni passo man mano che avviene.
    with st.status(f"Scraping {sc_spec}/{sc_citta}…", expanded=True) as stato:
        sorgenti = [risolvi_sorgente(n) for n in sc_fonti]
        annunciati = {s.nome: s.url_ricerca(sc_spec, sc_citta) for s in sorgenti}
        for _src in sorgenti:
            st.caption(f"{_src.etichetta}: {annunciati[_src.nome]}")
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
                sc_spec, sc_citta, sorgente=_src, storage=storage_corrente(DB_SCELTO),
                max_pagine=int(sc_pagine),
                disponibilita=sc_disp,
                giorni_disponibilita=int(sc_giorni),
                progress=avanzamento,
                annullato=annullamento_richiesto))
        barra.progress(1.0)
        # Dove il portale ha davvero cercato, se diverso da quanto annunciato
        # (stessa regola della CLI: «dentista» che su Doctolib e' `odontoiatra`).
        tradotte = {s.nome: u for s in sorgenti
                    if (u := s.url_ricerca(sc_spec, sc_citta)) != annunciati[s.nome]}
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
            msg = (f"{nome}: {rep.righe} righe da {n_pagine(rep.pagine)} "
                   f"({rep.nuove} nuove rispetto a prima).")
            if sc_disp:
                msg += f" {rep.slot} slot di disponibilità."
                if rep.nota_slot:
                    msg += f" ({rep.nota_slot.capitalize()}.)"
            if rep.sorgente in tradotte:
                msg += f" Cercato come: {tradotte[rep.sorgente]}"
            st.success(msg)

    # Chi ha appena scaricato «dentista» vuole vedere i dentisti, non ritrovarli
    # sparsi in mezzo ai cardiologi di ieri. Le altre ricerche restano nel DB, a un
    # clic di distanza.
    if any(rep.righe for rep in reports):
        st.session_state["vista"] = (sc_spec, sc_citta)


storage = storage_corrente(DB_SCELTO)

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
# La selezione salvata sopravvive al cambiare delle opzioni, e questo la rendeva
# sbagliata proprio nel momento peggiore: con il DB ancora vuoto non c'e' nessun
# portale da selezionare, quindi resta memorizzata la lista vuota, e subito dopo
# il PRIMO scraping la tabella risultava vuota — con tutti gli altri filtri
# sotto accusa. Stesso effetto cambiando database dalle impostazioni avanzate.
# Quando l'elenco dei portali cambia si riparte quindi da «tutti»; una
# deselezione fatta a mano non lo tocca, perche' li' le opzioni restano quelle.
if st.session_state.get("_fonti_opzioni") != _fonti_db:
    st.session_state["_fonti_opzioni"] = _fonti_db
    st.session_state.pop("f_fonti", None)
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
         "«Visita cardiologica di controllo» sia «Visita di controllo cardiologica».")
servizi = [] if _MANCANTI else storage.servizi_noti()
if servizi:
    scelto = st.sidebar.selectbox("…oppure scegli tra quelli trovati",
                                  ["—"] + servizi, key="f_serv_scelto")
    if scelto != "—" and not f_serv:
        f_serv = scelto
c_min, c_max = st.sidebar.columns(2)
f_prezzo_min = c_min.number_input("Prezzo min €", 0, 5000, 0, 10, key="f_prezzo_min",
                                  help="0 = nessun minimo.")
f_prezzo_max = c_max.number_input("Prezzo max €", 0, 5000, 0, 10, key="f_prezzo_max",
                                  help="0 = nessun massimo.")
# Le chiavi restano quelle di `ORDINA` (sono anche i valori di `--ordina`
# nella CLI): qui cambia solo come si leggono.
_NOMI_ORDINA = {"prezzo": "Prezzo", "rating": "Rating",
                "recensioni": "Numero di recensioni",
                "disponibilita": "Prima disponibilità", "distanza": "Distanza"}
f_ordina = st.sidebar.selectbox(
    "Ordina per", list(ORDINA), key="f_ordina",
    format_func=lambda k: _NOMI_ORDINA.get(str(k), str(k)),
    help="Ogni criterio ha il suo verso naturale (prezzo, disponibilità e "
         "distanza crescenti; rating e recensioni decrescenti) e tiene i valori "
         "mancanti in coda in entrambi i versi.")
f_desc = st.sidebar.checkbox(
    "Inverti ordinamento", key="f_desc",
    help="Prezzo: dal più caro. Rating/recensioni: dal peggiore.")
f_unifica = st.sidebar.checkbox(
    "Unisci lo stesso medico tra portali", key="f_unifica",
    help="Fonde in una riga sola le schede che i due portali hanno dello stesso "
         "medico (stesso cognome, sedi a meno di 150 m). Il prezzo mostrato è "
         "il minimo; la colonna «Disponibile su» elenca quanto chiede ciascun "
         "portale. Togli la spunta per rivedere le righe grezze.")

# I filtri che si usano di rado stanno in un riquadro chiuso: tutti aperti, la
# barra superava i 2500 pixel. Il titolo dice quanti sono attivi, cosi' un
# filtro dimenticato la' dentro non resta invisibile (e la riga «Filtri
# attivi» sopra la tabella li elenca comunque per nome).
_DEFAULT_ALTRI = {"f_min_rec": 0, "f_min_rating": 0.0, "f_solo_prezzo": False,
                  "f_solo_pren": False, "f_entro": 0, "f_vicino": "",
                  "f_limite": 100}
_n_altri = sum(1 for k, v in _DEFAULT_ALTRI.items()
               if st.session_state.get(k, v) != v)
with st.sidebar.expander("Altri filtri" + (f" ({_n_altri} attivi)" if _n_altri else "")):
    f_min_rec = st.number_input("Min recensioni", 0, 10000, 0, 5, key="f_min_rec")
    f_min_rating = st.slider("Min rating", 0.0, 5.0, 0.0, 0.5, key="f_min_rating")
    f_solo_prezzo = st.checkbox("Solo con prezzo pubblicato", key="f_solo_prezzo")
    f_solo_pren = st.checkbox(
        "Solo prenotabili online", key="f_solo_pren",
        help="Esclude chi è elencato con il listino ma non ha un'agenda online: "
             "si prenota solo per telefono.")
    f_entro = st.number_input(
        "Disponibile entro N giorni", 0, 90, 0, key="f_entro",
        help="0 = nessun filtro. Funziona solo se le disponibilità sono state "
             "raccolte durante lo scraping.")
    f_vicino = st.text_input(
        "Vicino a (lat,lng)", value="", key="f_vicino",
        placeholder="45.4642,9.1900",
        help="Aggiunge la colonna della distanza; con «Ordina per: Distanza» "
             "mette in cima i più vicini.")
    f_limite = st.number_input("Righe da mostrare (0 = tutte)", 0, 5000, 100,
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

# Statistiche su tutto cio' che risponde ai filtri, non sulle sole righe
# mostrate: con il limite di 100 e l'ordine per prezzo si leggeva «100 con
# prezzo, 0 senza» anche quando i senza prezzo erano la maggioranza.
results, s = search_con_totali(storage, filtro)

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
               + " — «🧹 Azzera solo i filtri» nella barra a sinistra li toglie "
                 "tutti.")

if not results:
    st.warning("Nessun risultato con questi filtri.")
    st.stop()
if s["mostrati"] < s["totale"]:
    st.caption(f"Mostrate le prime {s['mostrati']} righe su {s['totale']}: "
               "alza «Righe da mostrare» in «Altri filtri» per vederle tutte.")

df = pd.DataFrame(results)
if "sorgente" in df.columns:
    df["sorgente"] = df["sorgente"].map(etichetta_di)

# Quali colonne, e in che ordine: la regola sta in `query_engine`, la stessa che
# usa `cerca.py --colonne`. Gli identificativi interni, le coordinate e i
# timestamp servono a verificare un dato sospetto, non a scegliere un medico, e
# in mezzo alle altre obbligano a scorrere di lato per arrivare al prezzo.
tecniche = st.checkbox("Mostra le colonne tecniche (id, coordinate, data di "
                       "scraping)", value=False, key="mostra_tecniche")
df = df[colonne_visibili(results, tecniche=tecniche)]

# `width="stretch"` e non `use_container_width=True`: quest'ultimo e' deprecato
# da Streamlit 1.49 e la data di rimozione annunciata e' gia' passata — resta un
# avviso a ogni render finche' regge, poi smette di funzionare.
# L'altezza di default mostra una decina di righe e lascia mezza pagina vuota
# sotto: la tabella e' il contenuto principale, quindi cresce con le righe fino
# a un tetto oltre il quale scorre al suo interno. 35 px a riga piu'
# l'intestazione, come le disegna Streamlit.
_ALTEZZA = min(35 * (len(df) + 1) + 3, 735)
st.dataframe(df, width="stretch", hide_index=True, height=_ALTEZZA,
             column_config={
                 "sorgente": st.column_config.TextColumn("Fonte"),
                 "nome": st.column_config.TextColumn("Nome"),
                 "indirizzo": st.column_config.TextColumn("Indirizzo"),
                 "citta": st.column_config.TextColumn("Città"),
                 "servizio": st.column_config.TextColumn("Servizio"),
                 "rating": st.column_config.NumberColumn("Rating", format="%.1f"),
                 "specializzazione": st.column_config.TextColumn(
                     "Specializzazione"),
                 "provincia": st.column_config.TextColumn("Prov."),
                 "tipo": st.column_config.TextColumn("Tipo"),
                 "n_fonti": st.column_config.NumberColumn("Portali"),
                 "disponibile_su": st.column_config.TextColumn(
                     "Disponibile su",
                     help="Quanto chiede ciascun portale per questo medico. "
                          "«n/d» = quel portale non pubblica il prezzo, non "
                          "che il medico non c'è."),
                 # Con l'URL per esteso la colonna spingeva la tabella oltre
                 # il bordo destro, e restava visibile solo una «h».
                 "url_profilo": st.column_config.LinkColumn(
                     "Profilo", display_text="Apri"),
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
                     help="Senza spunta il medico non ha un'agenda online: "
                          "si prenota per telefono."),
             })

# L'export contiene SEMPRE tutte le colonne, comprese quelle nascoste sopra e i
# nomi grezzi delle sorgenti: un file di dati serve a rileggerlo altrove, e
# `id`/`lat`/`data_scraping` sono proprio le colonne che li' servono. E' anche
# quello che scrive `cerca.py --export`, byte per byte le stesse colonne
# nello stesso ordine.
df_export = pd.DataFrame(results)
col_csv, col_jsonl = st.columns(2)
col_csv.download_button(
    "⬇️ Esporta CSV",
    data=df_export.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"medici_{f_ordina}.csv",
    mime="text/csv",
    help="Con BOM, così Excel apre gli accenti e l'€ senza chiedere niente.",
)
col_jsonl.download_button(
    "⬇️ Esporta JSON Lines",
    # `or ""`: senza un file di destinazione `to_json` restituisce sempre la
    # stringa, ma e' dichiarato Optional (scrivendo su file torna None) e
    # `download_button` non accetta None.
    data=df_export.to_json(orient="records", lines=True, force_ascii=False) or "",
    file_name=f"medici_{f_ordina}.jsonl",
    mime="application/x-ndjson",
    help="Un oggetto per riga: un file interrotto a metà scaricamento resta "
         "comunque leggibile fino all'ultima riga completa.",
)
st.caption("Gli export contengono tutte le colonne — anche quelle tecniche "
           "nascoste qui sopra — perché servono a rileggere i dati altrove. "
           "È lo stesso file di `python cerca.py cerca … --export nome.csv`.")

st.caption(f"Prezzo pubblicato: {s['con_prezzo']} risultati su {s['totale']}. "
           "I prezzi sono pubblicati solo da una minoranza dei medici (limite della "
           "piattaforma, non dello scraper).")
