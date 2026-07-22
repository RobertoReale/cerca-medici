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

---

## Cosa fa (verificato end-to-end)

- **Anagrafica + prezzi** da due portali. Su MioDottore dalle pagine di ricerca
  HTML server-side, su Doctolib da un'API JSON: in nessuno dei due casi serve un
  browser headless.
- **Disponibilità (date/orari)** su entrambi: su MioDottore via
  `GET /api/v3/doctors/{id}/addresses/{addr}/slots`, autenticato con un `Bearer`
  token che il sito incorpora nell'HTML (`ZLApp.APICredentials.ACCESS_TOKEN`);
  su Doctolib via `GET /search/availabilities.json`, che non chiede
  autenticazione.
- **Più portali**: ogni riga porta la colonna `sorgente`; si filtra e si
  confronta per fonte (vedi [Più portali](#più-portali---fonte)).
- **Unificazione dei duplicati**: lo stesso medico su entrambi i portali
  diventa una riga sola, con il prezzo minimo e l'elenco di quanto chiede
  ciascun portale (vedi [Unificare i duplicati](#unificare-i-duplicati---unifica)).
- **Ordinamento/filtri**: prezzo (crescente, NULL in coda), rating, numero
  recensioni, prima disponibilità, distanza da un punto (lat,lng).
- **Storage** SQLite + export CSV.
- **Due interfacce**: CLI (`cerca.py`) e GUI Streamlit (`app.py`), che
  condividono lo stesso motore di query (nessuna logica duplicata).

---

## Installazione

```bash
pip install -r requirements.txt
```

Python 3.10+. Le dipendenze `streamlit`/`pandas` servono solo per la GUI; la CLI
gira con `requests`, `beautifulsoup4`, `lxml`, `tabulate`.

---

## Uso — CLI

### 1. Scraping (popola il DB locale `data/medici.db`)

```bash
# Solo anagrafica + prezzi, su tutti i portali registrati (default)
python cerca.py scrape --specializzazione podologo --citta Milano

# Anche disponibilità (endpoint disallowed su entrambi i siti — vedi robots.txt)
python cerca.py scrape --specializzazione podologo --citta Milano --disponibilita --giorni 5

# Su un portale specifico
python cerca.py scrape --specializzazione cardiologo --citta Milano --fonte doctolib

# Su due portali in un colpo solo
python cerca.py scrape --specializzazione cardiologo --citta Milano --fonte miodottore doctolib
```

I due siti non chiamano le specializzazioni allo stesso modo: `dermatologo`
esiste su MioDottore e su Doctolib si chiama `dermatologo-venereologo`. La
traduzione è automatica e, quando non riesce, l'errore **elenca le alternative**
invece di restituire zero risultati in silenzio.

### 2. Consultazione (interroga il DB)

```bash
# Il podologo/indirizzo più economico
python cerca.py cerca --specializzazione podologo --citta Milano --ordina prezzo --limite 20

# Ordina per rating, solo chi ha ≥50 recensioni
python cerca.py cerca --specializzazione podologo --citta Milano --ordina rating --min-recensioni 50

# Chi ha uno slot entro 3 giorni, ordinato per prezzo
python cerca.py cerca --specializzazione podologo --citta Milano --disponibile-entro 3 --ordina prezzo

# Più vicino a un punto (lat,lng), es. Duomo di Milano
python cerca.py cerca --specializzazione podologo --citta Milano --ordina distanza --vicino-a 45.4642,9.1900

# Un servizio entro un budget — nessuna nuova richiesta al sito, filtra il DB
python cerca.py cerca --servizio "visita controllo" --prezzo-max 50

# Confronta i due portali: una riga per medico, prezzo minimo, chi lo pratica
python cerca.py cerca --specializzazione cardiologo --citta Milano --unifica

# Esporta il risultato filtrato in CSV
python cerca.py cerca --specializzazione podologo --citta Milano --ordina prezzo --export risultati.csv
```

Opzioni utili: `--fonte`, `--unifica`, `--solo-con-prezzo`,
`--solo-prenotabili`, `--min-rating`, `--prezzo-max`, `--prezzo-min`,
`--servizio "prima visita"`, `--desc`, `--limite N`, `--verbose`.

`--servizio` cerca **per parole, in qualsiasi ordine**: `"visita controllo"`
trova sia «Visita podologica di controllo» sia «Visita di controllo podologica».
Le preposizioni (`di`, `della`, …) vengono ignorate.

`cerca` legge **solo il database locale**: cambiare servizio, prezzo o
ordinamento non fa ripartire nessuno scraping. `scrape` va rieseguito solo per
aggiornare i dati o per una città/specializzazione nuova. Durante uno `scrape`,
`Ctrl-C` interrompe in modo pulito: le pagine già scaricate restano salvate.

Rieseguire `scrape` sulla stessa coppia specializzazione/città **aggiorna** i
dati: i vecchi restano finché i nuovi non sono arrivati, e solo a fine corsa
vengono rimossi i medici che non compaiono più tra i risultati. Un run
interrotto non toglie quindi nulla a quanto avevi già. Attenzione però al
`--max-pagine`: un giro più corto del precedente considera "spariti" i medici
delle pagine che non ha visitato, e li rimuove.

---

## Uso — GUI (Streamlit)

```bash
streamlit run app.py
```

Form con gli stessi filtri della CLI, selezione dei portali sia in scraping sia
in consultazione, tabella con ordinamento nativo per colonna, export CSV. Gira
solo in locale, nessun deployment.

Gli identificativi interni, le coordinate e i timestamp restano nascosti dietro
«Mostra le colonne tecniche»: servono a verificare un dato sospetto, non a
scegliere un medico, e in mezzo alle altre colonne obbligavano a scorrere di
lato per arrivare al prezzo.

---

## Architettura

```
medici/
  testo.py            slugify condiviso (accenti, separatori) + console UTF-8
  fetcher.py          requests + rate limiting 1-3s, retry/backoff, get() e post(), rilevamento blocco
  scraper.py          Paginazione, retry, annullamento, sostituzione dei dati vecchi — non sa di che sito parla
  storage.py          SQLite (tabelle risultati + disponibilita) + export CSV + migrazioni
  query_engine.py     Logica condivisa CLI/GUI: filtri, ordinamento, haversine
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

cerca.py              CLI
app.py                GUI Streamlit
```

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
python cerca.py scrape --specializzazione podologo --citta Milano --fonte miodottore
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
(listini non allineati, servizi diversi con lo stesso nome, un prezzo vecchio da
una parte). Se un portale non pubblica il prezzo, il cluster dice «n/d» invece di
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

### Robustezza

- **Rate limiting**: pausa randomizzata 1–3 s tra le richieste, retry con backoff
  su 429/5xx. Se un sito risponde con una challenge/captcha, lo scraper **si
  ferma** su quel portale invece di ritentare all'infinito.
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
  sitemap ufficiale). Serve ai suggerimenti quando uno slug non esiste.

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
