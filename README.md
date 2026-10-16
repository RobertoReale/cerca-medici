# cerca-medici — ricerca medici con prezzi e disponibilità, su più portali

Software di ricerca che i portali medici **non offrono**: cerca medici per
specializzazione/città, ne estrae **anagrafica, prezzi per indirizzo esatto e
disponibilità (date/orari)**, e permette di **ordinare per prezzo** (oltre che
per rating, recensioni, distanza, prima disponibilità).

MioDottore espone un solo ordinamento (`recommended`) e nessun ordinamento per
prezzo: questo strumento colma quel vuoto.

Il progetto nasce su **MioDottore.it**, ma non è più legato a un sito solo: i
portali sono **sorgenti intercambiabili** e l'utente sceglie se cercare su uno,
sull'altro o su entrambi. Oggi sono registrati **MioDottore.it** e
**Doctolib.it**, e lo stesso medico trovato su tutti e due può essere presentato
in una riga sola con i prezzi affiancati (`--unifica`).

> Uso personale. Rispetta i Termini di Servizio dei siti e le note su
> `robots.txt` più sotto. Non ridistribuire i dati raccolti.

**Per iniziare in trenta secondi:**

```bash
pip install -r requirements.txt
python cerca.py scrape --specializzazione cardiologo --citta Milano   # scarica
python cerca.py cerca --citta Milano --ordina prezzo --limite 20    # consulta
streamlit run app.py                                                # o con la GUI
```

---

## Indice

- [Cosa fa](#cosa-fa-verificato-end-to-end)
- [Installazione](#installazione)
- [Uso — CLI](#uso--cli): [opzioni globali](#opzioni-globali) ·
  [`scrape`](#1-scrape--popola-il-db-locale) · [`cerca`](#2-cerca--interroga-il-db) ·
  [`stato`](#3-stato--cosa-cè-già-nel-db) · [codici di uscita](#codici-di-uscita)
- [Uso — GUI](#uso--gui-streamlit)
- [CLI e GUI fanno le stesse cose](#cli-e-gui-fanno-le-stesse-cose) — la
  corrispondenza opzione per opzione
- [Architettura](#architettura): [più portali](#più-portali---fonte) ·
  [duplicati](#due-tipi-di-duplicato-due-trattamenti) ·
  [unificazione](#unificare-i-duplicati---unifica) · [schema DB](#schema-db)
- [Note su `robots.txt` e limiti](#note-su-robotstxt-e-limiti)
- [Manutenzione](#manutenzione) · [Test](#test)

---

## Cosa fa (verificato end-to-end)

- **Anagrafica + prezzi** da due portali. Su MioDottore dalle pagine di ricerca
  HTML server-side, su Doctolib da un'API JSON: in nessuno dei due casi serve un
  browser headless.
- **Disponibilità (date/orari)** su entrambi: su MioDottore via
  `GET /api/v3/doctors/{id}/addresses/{addr}/slots`, autenticato con un `Bearer`
  token che il sito incorpora nell'HTML (`ZLApp.APICredentials.ACCESS_TOKEN`);
  su Doctolib via `GET /search/availabilities.json`, che non chiede
  autenticazione. **Su Doctolib, in pratica, quasi sempre vuote**: vedi
  [Limiti dei dati](#limiti-dei-dati).
- **Più portali**: ogni riga porta la colonna `sorgente`; si filtra e si
  confronta per fonte (vedi [Più portali](#più-portali---fonte)).
- **Unificazione dei duplicati**: lo stesso medico su entrambi i portali
  diventa una riga sola, con il prezzo minimo e l'elenco di quanto chiede
  ciascun portale (vedi [Unificare i duplicati](#unificare-i-duplicati---unifica)).
- **Ordinamento/filtri**: prezzo (crescente, NULL in coda), rating, numero
  recensioni, prima disponibilità, distanza da un punto (lat,lng).
- **Storage** SQLite + export CSV o JSON Lines.
- **Due interfacce equivalenti**: CLI (`cerca.py`) e GUI Streamlit (`app.py`).
  Condividono lo stesso motore di query — nessuna logica duplicata — e **fanno
  le stesse cose**: tutto quello che si fa da una si fa anche dall'altra, con la
  corrispondenza opzione per opzione in
  [CLI e GUI fanno le stesse cose](#cli-e-gui-fanno-le-stesse-cose).

---

## Installazione

```bash
pip install -r requirements.txt
```

Python 3.10+. Le dipendenze `streamlit`/`pandas` servono solo per la GUI; la CLI
gira con `requests`, `beautifulsoup4`, `lxml`, `tabulate`.

Il progetto **non si installa**: si esegue dalla sua cartella (`python cerca.py`,
`streamlit run app.py`).

---

## Uso — CLI

```bash
python cerca.py [--db PERCORSO] [--verbose] {scrape | cerca | stato} [opzioni]
```

Tre comandi, con una divisione netta: **`scrape` è l'unico che tocca la rete**,
`cerca` e `stato` leggono soltanto il database locale.

| Comando | Cosa fa | Tocca la rete |
|---|---|---|
| `scrape` | Scarica anagrafica, prezzi e (opzionale) disponibilità nel DB | sì |
| `cerca` | Interroga il DB: filtri, ordinamento, unificazione, export | no |
| `stato` | Dice cosa c'è già nel DB e quanto è vecchio | no |

`--help` funziona su tutti: `python cerca.py cerca --help` elenca le opzioni di
quel comando.

### Opzioni globali

Vanno **prima** del comando (`python cerca.py --verbose scrape …`).

| Opzione | Default | Cosa fa |
|---|---|---|
| `--db PERCORSO` | `data/medici.db`, con fallback su `data/miodottore.db` se esiste solo quello | Database SQLite su cui lavorare. Un percorso diverso è una raccolta separata; un file inesistente viene creato vuoto |
| `--verbose`, `-v` | spento | Log dettagliato (pagina per pagina, quante schede, quante righe). Va su `stderr` |
| `--help`, `-h` | — | Aiuto, del programma o del singolo comando |

### 1. `scrape` — popola il DB locale

| Opzione | Default | Cosa fa |
|---|---|---|
| `--specializzazione TESTO` | **obbligatoria** | Come la scrivi sul portale (`cardiologo`, `dermatologo`) |
| `--citta TESTO` | **obbligatoria** | Città da cercare (`Milano`) |
| `--fonte NOME [NOME…]` | `tutte` | Portali da interrogare: `miodottore`, `doctolib`, `tutte`. Ripetibile: `--fonte miodottore doctolib` |
| `--max-pagine N` | `40` | Quante pagine di risultati scaricare al massimo |
| `--disponibilita` | spento | Raccogli anche gli slot orari (endpoint disallowed — vedi [robots.txt](#note-su-robotstxt-e-limiti)) |
| `--giorni N` | `5` | Giorni di disponibilità da raccogliere. Ha effetto solo con `--disponibilita` |

```bash
# Solo anagrafica + prezzi, su tutti i portali registrati (default)
python cerca.py scrape --specializzazione cardiologo --citta Milano

# Anche disponibilità (endpoint disallowed su entrambi i siti — vedi robots.txt)
python cerca.py scrape --specializzazione cardiologo --citta Milano --disponibilita --giorni 5

# Su un portale specifico
python cerca.py scrape --specializzazione cardiologo --citta Milano --fonte doctolib

# Su due portali in un colpo solo
python cerca.py scrape --specializzazione cardiologo --citta Milano --fonte miodottore doctolib

# Smoke test prima di un run grande: una pagina sola, con il log aperto
python cerca.py --verbose scrape --specializzazione cardiologo --citta Milano --max-pagine 1
```

I due siti non chiamano le specializzazioni allo stesso modo: `dermatologo`
esiste su MioDottore e su Doctolib si chiama `dermatologo-venereologo`,
`dentista` è `odontoiatra`, `oculista` è `oftalmologo`. La traduzione è
automatica solo quando è certa (una tabella di sinonimi, un nome contenuto in
un solo slug, un refuso evidente come `cardiolgo`); negli altri casi l'errore
**elenca le alternative** invece di scegliere al posto tuo. Lo stesso vale per
le città: Doctolib conosce Roma solo come `citta-metropolitana-di-roma`, e la
ricerca ripiega lì da sola. Quando il portale ha cercato con nomi diversi da
quelli scritti, l'esito lo dice («Cercato come: …»).

Se quella ricerca è già nel DB, `scrape` lo dice prima di partire («hai già dati
di cardiologo/Milano di 3 h fa») e propone il comando per rivederli senza
riscaricarli. È una nota, non un blocco: aggiornare resta una tua scelta.

Rieseguire `scrape` sulla stessa coppia specializzazione/città **aggiorna** i
dati: i vecchi restano finché i nuovi non sono arrivati, e solo a fine corsa
vengono rimossi i medici che non compaiono più tra i risultati. Un run
interrotto non toglie quindi nulla a quanto avevi già. Attenzione però al
`--max-pagine`: un giro più corto del precedente considera "spariti" i medici
delle pagine che non ha visitato, e li rimuove.

Durante uno `scrape`, `Ctrl-C` interrompe in modo pulito: le pagine già
scaricate restano salvate.

### 2. `cerca` — interroga il DB

Nessuna opzione è obbligatoria: `python cerca.py cerca` mostra tutto quello che
c'è, ordinato per prezzo.

**Cosa mostrare**

| Opzione | Default | Cosa fa |
|---|---|---|
| `--specializzazione TESTO` | tutte | Filtro **ampio**: prende sia la ricerca con cui la riga è stata scaricata, sia la specializzazione dichiarata dal medico |
| `--citta TESTO` | tutte | Come sopra, per la città |
| `--ricerca SPEC CITTA` | — | Filtro **esatto**: solo lo scaricamento lanciato con quei due termini. Serve quando hai scaricato più specializzazioni e `--specializzazione` (che allarga) le mescolerebbe. L'elenco è in `cerca.py stato` |
| `--fonte NOME [NOME…]` | `tutte` | Da quali portali mostrare i risultati già scaricati |
| `--servizio "PAROLE"` | tutti | Parole chiave del nome del servizio, in qualsiasi ordine |

**Filtri**

| Opzione | Default | Cosa fa |
|---|---|---|
| `--prezzo-min €` | — | Spesa minima (esclude chi non pubblica il prezzo) |
| `--prezzo-max €` | — | Spesa massima (esclude chi non pubblica il prezzo) |
| `--min-recensioni N` | `0` | Solo chi ha almeno N recensioni |
| `--min-rating VOTO` | `0` | Solo chi ha almeno questo voto (0–5) |
| `--solo-con-prezzo` | spento | Escludi chi non ha prezzo pubblicato |
| `--solo-prenotabili` | spento | Solo chi ha l'agenda online attiva (gli altri si prenotano per telefono) |
| `--disponibile-entro N` | — | Solo chi ha uno slot entro N giorni. Richiede uno `scrape --disponibilita` |

**Ordinamento e presentazione**

| Opzione | Default | Cosa fa |
|---|---|---|
| `--ordina CRITERIO` | `prezzo` | `prezzo`, `rating`, `recensioni`, `disponibilita`, `distanza` |
| `--desc` (alias `--inverti`) | spento | Inverte l'ordine **naturale** del criterio (prezzo: dal più caro; rating: dal peggiore) |
| `--vicino-a LAT,LNG` | — | Punto di riferimento: aggiunge la distanza e abilita `--ordina distanza` |
| `--unifica` | spento | Fondi in una riga sola lo stesso medico trovato su più portali |
| `--limite N` | tutti | Massimo numero di risultati |
| `--colonne base\|tutte\|tecniche` | `base` | Quante colonne mostrare (vedi sotto) |
| `--export FILE` | — | Scrive i risultati su file: `.csv` (con BOM, per Excel) o `.jsonl`/`.ndjson` |

Ogni criterio di ordinamento ha il suo **verso naturale** — prezzo,
disponibilità e distanza crescenti; rating e recensioni decrescenti — e tiene i
valori mancanti **in coda in entrambi i versi**: `--desc` inverte l'ordine, non
fa risalire i buchi.

`--colonne` è l'equivalente della casella «mostra le colonne tecniche» della
GUI, con un gradino in più perché il terminale non ha lo scorrimento
orizzontale:

- `base` — le colonne con cui si sceglie un medico: nome, indirizzo, città,
  servizio, prezzo, rating, recensioni (più fonte, distanza o prima
  disponibilità quando servono davvero a distinguere);
- `tutte` — tutte le colonne utili, compresi `url_profilo`, `ha_calendario`,
  `tipo`, `provincia`;
- `tecniche` — anche `id`, `result_id`, `address_id`, `lat`/`lng`,
  `data_scraping`, `prezzo_raw`.

`--colonne` **non tocca `--export`**, che scrive sempre tutte le colonne: un
file di dati serve a rileggerlo altrove, e sono proprio gli identificativi e le
coordinate a servire lì.

```bash
# Il cardiologo/indirizzo più economico
python cerca.py cerca --specializzazione cardiologo --citta Milano --ordina prezzo --limite 20

# Ordina per rating, solo chi ha ≥50 recensioni
python cerca.py cerca --specializzazione cardiologo --citta Milano --ordina rating --min-recensioni 50

# Chi ha uno slot entro 3 giorni, ordinato per prezzo
python cerca.py cerca --specializzazione cardiologo --citta Milano --disponibile-entro 3 --ordina prezzo

# Più vicino a un punto (lat,lng), es. Duomo di Milano
python cerca.py cerca --specializzazione cardiologo --citta Milano --ordina distanza --vicino-a 45.4642,9.1900

# Un servizio entro un budget — nessuna nuova richiesta al sito, filtra il DB
python cerca.py cerca --servizio "visita controllo" --prezzo-max 50

# Confronta i due portali: una riga per medico, prezzo minimo, chi lo pratica
python cerca.py cerca --specializzazione cardiologo --citta Milano --unifica

# Solo l'ultimo scaricamento di cardiologo/Milano, con tutte le colonne utili
python cerca.py cerca --ricerca cardiologo Milano --colonne tutte

# Esporta il risultato filtrato in CSV
python cerca.py cerca --specializzazione cardiologo --citta Milano --ordina prezzo --export risultati.csv

# Esporta in JSON Lines (formato scelto dall'estensione): un oggetto per riga,
# cosi' un export interrotto a meta' resta comunque leggibile fino all'ultima
# riga completa
python cerca.py cerca --specializzazione cardiologo --citta Milano --ordina prezzo --export risultati.jsonl
```

`--servizio` cerca **per parole, in qualsiasi ordine**: `"visita controllo"`
trova sia «Visita cardiologica di controllo» sia «Visita di controllo cardiologica».
Le preposizioni (`di`, `della`, …) vengono ignorate.

`cerca` legge **solo il database locale**: cambiare servizio, prezzo o
ordinamento non fa ripartire nessuno scraping. `scrape` va rieseguito solo per
aggiornare i dati o per una città/specializzazione nuova.

Un filtro che non **può** funzionare — ordinare per distanza senza `--vicino-a`,
filtrare per disponibilità senza averle mai raccolte — produce un avviso su
`stderr` prima dei risultati, non un elenco vuoto da interpretare.

### 3. `stato` — cosa c'è già nel DB

Nessuna richiesta ai portali: serve a sapere se una ricerca è già stata fatta —
e quanto è vecchia — prima di spendere minuti a rifarla. È l'equivalente del
pannello «Dati già nel database» della GUI.

| Opzione | Default | Cosa fa |
|---|---|---|
| `--servizi` | spento | Elenca anche i nomi di servizio presenti, da usare con `cerca --servizio` |
| `--fonti` | spento | Elenca anche i portali registrati e quelli che hanno già dati nel DB |

```bash
python cerca.py stato
python cerca.py stato --servizi --fonti
python cerca.py --db altra_raccolta.db stato
```

```
Database: data/medici.db
Righe: 412 · slot di disponibilità: 1180

Ricerche già scaricate (3):
┌────────────┬────────────────────┬─────────┬─────────┬─────────────────────┬───────┐
│ Fonte      │ Specializzazione   │ Città   │   Righe │ Scaricata           │ Età   │
├────────────┼────────────────────┼─────────┼─────────┼─────────────────────┼───────┤
│ Doctolib   │ cardiologo         │ Milano  │      96 │ 2026-08-06 11:29:44 │ 2 h   │
│ MioDottore │ dermatologo        │ Milano  │     214 │ 2026-08-05 09:12:03 │ 28 h  │
│ Doctolib   │ dermatologo        │ Milano  │     102 │ 2026-08-05 09:10:51 │ 28 h  │
└────────────┴────────────────────┴─────────┴─────────┴─────────────────────┴───────┘
Rivedile senza riscaricare nulla: python cerca.py cerca --ricerca <specializzazione> <città>
```

### Codici di uscita

Perché `cerca.py` si possa incatenare in uno script senza rileggerne l'output.

| Codice | Quando |
|---|---|
| `0` | Tutto bene. Anche «nessun risultato con questi filtri»: la query ha funzionato, la risposta è che non c'è niente |
| `1` | `scrape` non ha portato **nessuna** riga da nessun portale; oppure `cerca` su un DB vuoto |
| `2` | Un portale si è bloccato (challenge/captcha) e nessun altro ha portato dati; oppure un argomento non valido (`--vicino-a` malformato) |
| `130` | `scrape` interrotto con `Ctrl-C`. Le pagine già scaricate restano salvate |

Con più portali l'esito è misto per costruzione: uno può bloccarsi e l'altro
riuscire. Ogni portale riporta la sua riga, e il codice di uscita descrive il
caso peggiore **solo se nessuno** ha portato dati.

---

## Uso — GUI (Streamlit)

```bash
streamlit run app.py
```

Gira solo in locale, nessun deployment. La barra a sinistra è divisa nelle
stesse fasi della CLI:

- **⚙️ Impostazioni avanzate** — il database su cui lavorare e il log
  dettagliato: gli equivalenti di `--db` e `--verbose`. Chiuso di default,
  perché quasi sempre vanno bene i valori predefiniti.
- **🔄 Nuova ricerca da zero** — svuota la tabella e azzera i filtri per
  ripartire con un'altra specializzazione. **I dati restano nel database**: si
  rivedono con un clic, senza rifare lo scraping.
- **1. Scraping** — specializzazione, città, portali, max pagine, disponibilità
  e giorni: gli stessi parametri di `cerca.py scrape`. Sopra il modulo compare
  da quanto tempo risalgono i dati di quella ricerca, per non riscaricarli
  inutilmente; sotto, il pannello «Dati già nel database» elenca tutto quello
  che c'è (l'equivalente di `cerca.py stato`).
- **🧹 Azzera solo i filtri** — riporta i filtri ai valori iniziali senza
  toccare la tabella né il database.
- **2. Filtri di ricerca** — gli stessi di `cerca.py cerca`, applicati **solo al
  database**: cambiarli non fa partire nessuna richiesta ai portali.

Per fermare uno scraping a metà: **Stop** in alto a destra (l'equivalente di
`Ctrl-C`). I dati già scaricati restano nel database; poi ricarica la pagina,
perché Streamlit chiude la sessione dopo uno stop.

Nella tabella si ordina anche **cliccando le intestazioni di colonna** — comodo
per i criteri che `--ordina` non copre — e i link ai profili sono cliccabili.
Gli identificativi interni, le coordinate e i timestamp restano nascosti dietro
«Mostra le colonne tecniche»: servono a verificare un dato sospetto, non a
scegliere un medico, e in mezzo alle altre colonne obbligavano a scorrere di
lato per arrivare al prezzo.

---

## CLI e GUI fanno le stesse cose

Le due interfacce non sono una il sottoinsieme dell'altra: condividono il motore
di query, la selezione delle colonne e il formato di export, e ogni funzione ha
il suo equivalente dall'altra parte.

| Cosa vuoi fare | CLI | GUI |
|---|---|---|
| Scegliere il database | `--db PERCORSO` | ⚙️ Impostazioni avanzate → «Database SQLite» |
| Vedere il log dettagliato | `--verbose` | ⚙️ Impostazioni avanzate → «Log dettagliato» (esce nel terminale) |
| Scaricare i dati | `scrape --specializzazione … --citta …` | 1. Scraping → «Avvia scraping» |
| Scegliere i portali | `--fonte miodottore doctolib` | multiselect «Portali» (scraping) e «Fonti» (filtri) |
| Limitare le pagine | `--max-pagine N` | «Max pagine» |
| Raccogliere le disponibilità | `--disponibilita --giorni N` | casella «Raccogli disponibilità» + «Giorni disponibilità» |
| Fermare uno scraping | `Ctrl-C` | **Stop** in alto a destra |
| Sapere cosa c'è già nel DB | `stato` | pannello «Dati già nel database» |
| Sapere quanto sono vecchi i dati | colonna «Età» di `stato`; nota all'avvio di `scrape` | banner sopra il modulo di scraping |
| Elencare i servizi trovati | `stato --servizi` | «…oppure scegli tra quelli trovati» |
| Elencare i portali | `stato --fonti` | opzioni dei due multiselect |
| Filtrare per specializzazione/città | `--specializzazione` / `--citta` | «Specializzazione (filtro)» / «Città (filtro)» |
| Vedere una sola ricerca scaricata | `--ricerca SPEC CITTA` | «Stai vedendo solo la ricerca X / Y» dopo uno scraping, con «Mostra tutte le ricerche» per uscirne |
| Filtrare per servizio | `--servizio "visita controllo"` | «Servizio (parole chiave)» |
| Prezzo, rating, recensioni | `--prezzo-min/--prezzo-max`, `--min-rating`, `--min-recensioni` | campi e slider omonimi |
| Solo con prezzo / prenotabili | `--solo-con-prezzo`, `--solo-prenotabili` | caselle omonime |
| Disponibili entro N giorni | `--disponibile-entro N` | «Disponibile entro (giorni, 0=off)» |
| Ordinare | `--ordina …` + `--desc` | «Ordina per» + «Inverti ordinamento», o un clic sull'intestazione di colonna |
| Ordinare per distanza | `--vicino-a 45.4642,9.1900` | «Vicino a (lat,lng) per distanza» |
| Unire lo stesso medico tra portali | `--unifica` | «Unisci lo stesso medico tra portali» |
| Limitare i risultati | `--limite N` | «Limite risultati (0=tutti)» |
| Vedere id, coordinate, date | `--colonne tecniche` | «Mostra le colonne tecniche» |
| Esportare | `--export file.csv` / `file.jsonl` | «⬇️ Esporta CSV» / «⬇️ Esporta JSON Lines» |
| Azzerare i filtri | non serve: ogni comando parte pulito | «🧹 Azzera solo i filtri» |
| Ripartire da zero | non serve: ogni comando parte pulito | «🔄 Nuova ricerca da zero» |

Le uniche due asimmetrie sono quelle inevitabili, e vengono dal fatto che la GUI
ha uno stato e la CLI no:

- le due **scorciatoie di azzeramento** esistono solo nella GUI, perché nella
  CLI ogni comando riparte già pulito;
- l'**ordinamento cliccando le intestazioni** è nativo del dataframe di
  Streamlit e vale su qualsiasi colonna; `--ordina` copre i cinque criteri che
  hanno una regola definita (compresi i valori mancanti, che restano in coda).

L'export è lo **stesso file** dalle due parti: stesse colonne, stesso ordine,
tutte incluse — anche quelle nascoste nella tabella.

---

## Architettura

```
medici/
  testo.py            slugify condiviso (accenti, separatori) + console UTF-8
  fetcher.py          requests + rate limiting 1-3s, retry/backoff, get() e post(), rilevamento blocco
  scraper.py          Paginazione, retry, annullamento, sostituzione dei dati vecchi — non sa di che sito parla
  storage.py          SQLite (tabelle risultati + disponibilita) + export CSV/JSONL + migrazioni
  query_engine.py     Logica condivisa CLI/GUI: filtri, ordinamento, haversine, deduplica per id,
                      avvisi sui filtri impossibili, quali colonne mostrare e in che ordine
  unifica.py          Cluster cross-portale: stesso cognome + sedi entro 150 m

  sorgenti/           IL CONTRATTO: `Sorgente`, `Pagina`, `ChiaveSlot` + registro
    miodottore.py       adatta i tre moduli qui sotto al contratto
    doctolib.py         mappa i record JSON su `Riga`

  filters.py          MioDottore: AVAILABLE_FILTERS dall'HTML -> data/filters.json
  parser.py           MioDottore: BeautifulSoup, una Riga per indirizzo; parse_prezzo (NBSP, "da", range)
  availability.py     MioDottore: endpoint /slots + ACCESS_TOKEN estratto dall'HTML

  doctolib/
    client.py         Doctolib: header, landing, ricerca JSON, slot
    slug.py           Doctolib: «dermatologo a Milano» -> URL che il sito capisce

cerca.py              CLI: comandi scrape / cerca / stato
app.py                GUI Streamlit
```

`cerca.py` e `app.py` sono **due presentazioni della stessa logica**: filtri,
ordinamento, avvisi, scelta delle colonne ed export vivono in `medici/`, e le
due interfacce si limitano a raccogliere i parametri e a disegnare il risultato.
È il motivo per cui la parità fra le due sta in piedi da sola: una regola nuova
compare da entrambe le parti senza doverla riscrivere.

Il pacchetto si chiamava `miodottore` quando il progetto interrogava un sito
solo. Il nome è rimasto **solo** dove indica davvero quel portale
(`parser.py`, `availability.py`, `filters.py`, `sorgenti/miodottore.py` e il
valore `sorgente = "miodottore"` nel database).

`parser.py` definisce anche `Riga`, che è il formato comune a **tutte** le
sorgenti: sta lì per ragioni storiche, non perché appartenga a MioDottore.

### Più portali (`--fonte`)

Lo scraper non conosce nessun sito: chiede pagine a una **`Sorgente`** e riceve
`Riga` già normalizzate. Aggiungerne una significa scrivere una sottoclasse di
`Sorgente` e registrarla — CLI e GUI la mostrano da sole, senza altre modifiche.

```bash
python cerca.py scrape --specializzazione cardiologo --citta Milano --fonte miodottore
python cerca.py cerca --fonte tutte --ordina prezzo     # default: tutte
```

Ogni riga porta la colonna `sorgente`. **Non è decorativa**: `result_id` e
`address_id` sono numeri interni di ciascun portale, e due portali possono usare
lo stesso numero per medici diversi. La sorgente fa parte della chiave unica,
della pulizia di fine run e del join con le disponibilità — senza, un run su un
sito sovrascriverebbe o cancellerebbe i dati di un altro.

**Cercare su entrambi**: sia la CLI (`--fonte a b`) sia la GUI (multiselect
«Portali») interrogano più portali nello stesso comando, e i risultati finiscono
nello stesso database distinti dalla fonte. Un portale che si blocca o che non
conosce quella specializzazione non ferma gli altri: viene riportato a parte.

### Due tipi di duplicato, due trattamenti

**Lo stesso portale, due ricerche** (sempre, automatico). I bacini di località
vicine si sovrappongono: la stessa specializzazione cercata in due comuni
confinanti trova due volte lo studio che sta in mezzo. Nel DB le copie restano — sono
scaricamenti distinti, con date proprie, e uno non deve sovrascrivere l'altro —
ma nei risultati sono **una riga sola**: stesso portale, stesso `result_id`,
stesso `address_id`, stesso servizio è lo stesso record, non c'è niente da
indovinare. Vale la copia scaricata più di recente. Il raggruppamento avviene
prima di `ORDER BY`/`LIMIT`, altrimenti «limite 100» renderebbe 100 righe di cui
alcune sono la stessa e altrettanti risultati veri sparirebbero dal fondo.

**Due portali diversi** (opzionale, `--unifica`): sotto.

### Unificare i duplicati (`--unifica`)

Nessun identificativo è condiviso tra i due portali: il `result_id` 1620432
sull'uno e sull'altro sono due persone diverse. L'unica cosa che entrambi
pubblicano per ogni sede è la posizione.

> Due righe sono lo stesso medico se il **cognome normalizzato coincide** *e* le
> due sedi distano **meno di 150 m**.

Servono entrambi i criteri: solo il cognome sbaglia sugli omonimi, solo la
geografia fonderebbe in uno i trenta medici di un poliambulatorio.

Il cluster diventa **una riga sola**, ma i prezzi dei singoli portali **non
vengono buttati via**:

| Nome | Indirizzo | Prezzo | Disponibile su |
|---|---|---|---|
| Dott. Stefano Madoi | Via Boscovich 40 | **100 €** | MioDottore 100 € · Doctolib 180 € |
| Dott. Gerardo Lo Russo | Via Illirico 6a | **200 €** | Doctolib 200 € · MioDottore n/d |

Perché non si comprime e basta: un minimo senza il portale che lo pratica non è
prenotabile, e uno scarto di 80 € sullo stesso medico è di per sé un'informazione
(listini non allineati, un prezzo vecchio da una parte). Quando invece i portali
quotano **prestazioni diverse** — capita: la prima visita su uno, un trattamento
specifico sull'altro — il nome della prestazione compare accanto a ogni prezzo
(`Doctolib 50 € (Prima visita cardiologica) · MioDottore 70 € (Elettrocardiogramma)`),
perché altrimenti i due numeri si leggerebbero come uno scarto
di listino sulla stessa cosa. Se un portale non pubblica il prezzo, il cluster dice «n/d» invece di
riempire il buco con il valore dell'altro.

L'unificazione è **opzionale** e spenta di default (`--unifica`, checkbox nella
GUI): toglierla rimette le righe grezze per fonte, ed è la via d'uscita quando
il clustering sbaglia. La soglia dei 150 m è in `medici/unifica.py`
(`SOGLIA_METRI`) ed è l'unico parametro empirico del modulo: le due piattaforme
geocodificano lo stesso portone con scarti di qualche decina di metri.

### Schema DB

- `risultati`: una riga per indirizzo (**sorgente**, result_id, address_id, nome,
  tipo, specializzazione, città, indirizzo, servizio, **prezzo**,
  prezzo_e_da_min, rating, n_recensioni, lat/lng, url_profilo, query\_\*).
- `disponibilita`: uno slot per riga (**sorgente**, result_id, address_id, data,
  orario, prenotabile) con `data_scraping` proprio — il dato invecchia in ore,
  quindi va rifatto lo scraping mirato prima di prenotare davvero.

Gli slot sono legati alla terna **(sorgente, result_id, address_id)**, non al
solo indirizzo: su un portale che identifica la *sede* invece della coppia
medico-sede, tutti i medici di un poliambulatorio condividono l'`address_id` e
si vedrebbero attribuita la disponibilità del collega.

I database creati con le versioni precedenti vengono **migrati all'apertura**:
la colonna `sorgente` viene aggiunta con default `miodottore` e gli indici
ricostruiti. Nessuna azione manuale.

Il file di default è `data/medici.db`. Se esiste solo il vecchio
`data/miodottore.db`, viene usato quello: il rinomino del progetto non fa
sparire i dati già raccolti.

---

## Note su `robots.txt` e limiti

### MioDottore.it

- Le **pagine di ricerca** (`/{specializzazione}/{città}`) sono permesse da
  `robots.txt` — usate sempre questa forma, non `/cerca?` (disallowed).
- L'**endpoint disponibilità** vive sotto `/api/v3/`, path **disallowed**. Per
  questo la raccolta disponibilità è **opt-in** (`--disponibilita`), non parte di
  default.

### Doctolib.it

- Le **landing di ricerca** (`/{specializzazione}/{città}`) sono permesse, e sono
  quelle da cui si leggono gli identificativi della ricerca.
- **`/patient-health-search*`** (l'API che restituisce i medici) e
  **`/*availabilities*`** (gli slot) sono entrambi **disallowed**. È esattamente
  la stessa situazione di `/api/v3/` su MioDottore, e vale **la stessa
  politica**: uso personale, disponibilità solo su richiesta esplicita,
  parsimonia. Il rate limit dichiarato dal sito è 1800 richieste/minuto: il
  nostro traffico sta due ordini di grandezza sotto, e **i tempi di attesa non
  vanno abbassati**.

### Limiti dei dati

- **Prezzi pubblicati solo da una minoranza** di medici (~1/3–1/4 su
  MioDottore, meglio su Doctolib): è un limite delle piattaforme, non dello
  scraper. L'output indica sempre «N su M hanno un prezzo pubblicato».
- **Su Doctolib il prezzo è quello del servizio che ha fatto match** con la
  ricerca (`matchedVisitMotive`), **non il listino completo**: cercare
  «cardiologo» e cercare «ecocardiografia» sullo stesso medico dà due prezzi
  diversi, entrambi veri. Il listino intero esiste solo sulla pagina profilo e
  costerebbe una richiesta per medico: non è implementato.
- Di conseguenza il modello «una riga per indirizzo» di MioDottore su Doctolib
  diventa «una riga per medico/sede».
- **Doctolib non pubblica la provincia** e per gli slot non distingue «occupato»
  da «agenda chiusa»: espone solo gli orari liberi.
- **Disponibilità Doctolib quasi sempre vuote.** A ottobre 2026 l'endpoint
  risponde «non offre il servizio di prenotazione online» alla grande
  maggioranza dei calendari, anche di medici che la ricerca segna come
  prenotabili (su 20 cardiologi e 25 psicologi a Milano: zero slot). Non è un
  errore dello scraper: l'esito riporta il motivo («calendari vuoti: 15 non
  accettano prenotazioni online, 2 senza posti liberi nel periodo»). Su
  MioDottore le disponibilità arrivano regolarmente.
- **Prezzi simbolici**: alcuni medici pubblicano «1 €» per non mostrare il
  listino (lo stesso medico su Doctolib chiede 200 €). Sotto i 5 € il prezzo
  viene trattato come non pubblicato, così non finisce in cima all'ordinamento;
  il testo originale resta in `prezzo_raw`. Lo 0 («Prestazione gratuita»)
  resta un prezzo.

### Robustezza

- **Rate limiting adattivo**: pausa randomizzata tra le richieste, retry con
  backoff su 429/5xx. Il ritardo di base non è fisso: si allarga quando il
  sito risponde 429/5xx/timeout e si restringe quando torna a rispondere
  pulito, ma non scende mai sotto la finestra configurata (1–3 s di default):
  si muove tra `max_delay` e un tetto di 3× `max_delay`, così a regime la pausa
  resta randomizzata invece di stabilizzarsi su un intervallo fisso. Se un
  sito risponde con una challenge/captcha, lo scraper **si ferma** su quel
  portale invece di ritentare all'infinito.
- **Doctolib non risponde 404 a una ricerca che non riconosce: redirige.** Una
  città sconosciuta finisce su una ricerca nazionale che restituisce medici
  *veri* di Milano — dati plausibili e sbagliati. Lo scraper riconosce il
  redirect e **si rifiuta di salvare**, dicendo quale dei due termini non è stato
  capito.
- **Un guasto di rete è un esito, non un traceback**: se una pagina non arriva
  nemmeno dopo i retry, il portale riporta l'errore e gli altri proseguono. La
  sostituzione dei dati vecchi viene saltata, perché le pagine mancanti non
  provano che quei medici siano spariti dal sito.
- **Filtri che non possono funzionare**: ordinare per distanza senza un punto di
  riferimento, o filtrare per disponibilità senza averle mai raccolte, produce
  un avviso — non un elenco vuoto da interpretare (`query_engine.avvisi`).

---

## Manutenzione

Il rischio principale è strutturale: se un portale cambia il markup HTML, il
parser di quella sorgente può rompersi. Mitigazioni integrate:

- **Fallimento rumoroso**: 0 card sulla pagina 1 → errore visibile, non un
  dataset vuoto salvato in silenzio.
- **Isolamento**: un portale bloccato viene escluso dal resto del giro, gli
  altri proseguono.
- **Smoke test**: esegui `python cerca.py scrape ... --max-pagine 1 --verbose`
  prima di run grandi. Per il solo client Doctolib:
  `python -m medici.doctolib.client cardiologo milano`.
- **Filtri MioDottore**: rigenera `data/filters.json` ~1 volta al mese con
  `python -m medici.filters <pagina_scaricata.html>`.
- **Specializzazioni Doctolib**: rigenera `data/doctolib_specialita.json` con la
  stessa cadenza — `python -m medici.doctolib.slug --aggiorna` (130 slug dal
  sitemap ufficiale). Serve ai suggerimenti quando uno slug non esiste. I nomi
  che i due portali danno in modo diverso alla stessa specializzazione stanno
  invece in `SINONIMI` (`medici/doctolib/slug.py`), da aggiornare a mano.

Sulle due sorgenti il rischio non è simmetrico: un selettore CSS che smette di
matchare è **silenzioso** (0 righe, nessun errore), un campo JSON rinominato è
**rumoroso** (i test con fixture falliscono). È il motivo per cui la sorgente
Doctolib è più corta e più solida di quella HTML.

---

## Test

```bash
python -m pytest tests -q
```

**Nessun test tocca la rete**: le risposte reali sono congelate in
`tests/fixtures/` (vedi il README lì dentro). Una suite che dipende da un sito
vivo fallisce quando quel sito ha una brutta giornata e passa quando ha smesso di
funzionare senza che nessuno se ne accorga.

| File | Cosa protegge |
|---|---|
| `test_parser.py` | `parse_prezzo` (NBSP, «da», range) e il parsing delle card MioDottore |
| `test_query_engine.py` | ordinamento (NULL in coda), haversine, filtri |
| `test_sorgenti.py` | il multi-portale: due sorgenti con gli stessi identificativi non devono sovrascriversi, cancellarsi o scambiarsi le disponibilità |
| `test_doctolib_parser.py` | la mappatura JSON→`Riga` su una risposta reale: se Doctolib rinomina un campo, questo test cade |
| `test_doctolib_client.py` | paginazione (l'off-by-one che farebbe **perdere l'ultima pagina**) e la lettura dei redirect |
| `test_doctolib_slug.py` | la traduzione delle specializzazioni e i messaggi d'errore che propongono alternative |
| `test_unifica.py` | il clustering cross-portale, soprattutto i casi in cui **non** deve unire |
| `test_fetcher.py` | encoding, brotli, retry, e il `post()` che le API JSON richiedono |
| `test_scraper.py` | paginazione (una pagina vuota transitoria non tronca la raccolta), annullamento, e i guasti di rete che non devono cancellare i dati precedenti |
| `test_availability.py` | slot MioDottore: 4xx tollerati, token rinnovato dopo un 401, offset orario stagionale |
| `test_storage.py` | deduplica delle righe che la paginazione ripete, e l'uso da più thread (la GUI interroga il DB mentre lo scraping ci scrive) |
