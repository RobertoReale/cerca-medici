"""Fase 5 — Storage SQLite + export CSV.

Due tabelle: `risultati` (anagrafica/prezzi, una riga per indirizzo) e
`disponibilita` (slot orari, timestamp proprio perche' il dato invecchia in ore).

Ogni riga porta la `sorgente` da cui proviene. Non e' una colonna decorativa:
gli identificativi (`result_id`, `address_id`) sono numeri interni di ciascun
portale e due portali diversi possono benissimo usare lo stesso numero per
medici diversi. Senza `sorgente` nelle chiavi, un run su un sito
sovrascriverebbe o cancellerebbe le righe di un altro.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .parser import Riga

_DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB = _DATA / "medici.db"
# Nome usato finche' il progetto si chiamava "miodottore". Chi ha gia' raccolto
# dati non deve accorgersi del rinomino: se esiste solo il file vecchio, lo
# usiamo (vedi `_percorso_effettivo`). Nessuna copia, nessuna migrazione: e' lo
# stesso schema, cambia solo il nome del file.
DB_STORICO = _DATA / "miodottore.db"


def _percorso_effettivo(db_path) -> Path:
    """Il DB da aprire davvero.

    Con `db_path` esplicito non si discute. Senza, si preferisce `medici.db`; si
    ricade su `miodottore.db` solo se quello nuovo non esiste ancora e il
    vecchio si': cosi' un aggiornamento del progetto non fa sparire i dati
    raccolti, e nemmeno ne crea due copie che divergono in silenzio.
    """
    if db_path:
        return Path(db_path)
    if not DEFAULT_DB.exists() and DB_STORICO.exists():
        return DB_STORICO
    return DEFAULT_DB

# Usata come default ovunque: i DB creati prima che le sorgenti esistessero
# contengono solo dati MioDottore, e devono continuare a rispondere alle stesse
# query dopo la migrazione.
SORGENTE_DEFAULT = "miodottore"

# Tabelle e indici sono separati apposta: su un DB creato dalla versione
# precedente gli indici citano `sorgente`, una colonna che esiste solo dopo la
# migrazione. Creare tutto insieme falliva con "no such column: sorgente".
# Ordine corretto: tabelle -> migrazione -> indici.
SCHEMA_TABELLE = """
CREATE TABLE IF NOT EXISTS risultati (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sorgente TEXT NOT NULL DEFAULT 'miodottore',
    result_id TEXT,
    address_id TEXT,
    nome TEXT,
    tipo TEXT,
    specializzazione TEXT,
    citta TEXT,
    indirizzo TEXT,
    provincia TEXT,
    servizio TEXT,
    servizio_id TEXT,
    prezzo REAL,
    prezzo_e_da_min INTEGER,
    prezzo_raw TEXT,
    rating REAL,
    n_recensioni INTEGER,
    lat REAL,
    lng REAL,
    url_profilo TEXT,
    ha_calendario INTEGER,
    query_specializzazione TEXT,
    query_citta TEXT,
    data_scraping TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS disponibilita (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sorgente TEXT NOT NULL DEFAULT 'miodottore',
    result_id TEXT,
    address_id TEXT,
    servizio_id TEXT,
    data DATE,
    orario TEXT,
    prenotabile INTEGER,
    data_scraping TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_INDICI = """
CREATE INDEX IF NOT EXISTS idx_ris_query ON risultati(query_specializzazione, query_citta);
CREATE INDEX IF NOT EXISTS idx_ris_prezzo ON risultati(prezzo);
CREATE INDEX IF NOT EXISTS idx_ris_addr_v2 ON risultati(sorgente, result_id, address_id);
CREATE INDEX IF NOT EXISTS idx_disp_addr_v2
    ON disponibilita(sorgente, result_id, address_id, data);
"""

# Indici sostituiti da versioni con piu' colonne. `CREATE INDEX IF NOT EXISTS`
# non ridefinisce un indice esistente: senza questo elenco le versioni vecchie
# sopravviverebbero per sempre accanto alle nuove. Il suffisso `_v2` nei nomi
# nuovi e' quello che rende il ricambio esplicito invece che indovinato
# leggendo l'SQL memorizzato.
INDICI_OBSOLETI = ("idx_ris_addr", "idx_disp_addr")

# La paginazione del sito ripete gli stessi risultati su pagine diverse (il
# ranking e' dinamico), quindi un singolo run inseriva righe duplicate che poi
# comparivano piu' volte nell'elenco. Questo indice le rende impossibili e fa
# funzionare l'`INSERT OR REPLACE` come upsert. COALESCE perche' in SQLite due
# NULL sono considerati distinti in un indice UNIQUE.
#
# `_v2` nel nome: la versione precedente non aveva `sorgente` e va sostituita,
# non affiancata (vedi `_migra`). Tenerle entrambe farebbe fallire con
# IntegrityError il primo inserimento di una seconda sorgente che riusa un id.
UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ris_unico_v2 ON risultati(
    sorgente, query_specializzazione, query_citta,
    COALESCE(result_id,''), COALESCE(address_id,''), COALESCE(servizio,''))
"""

DEDUP = """
DELETE FROM risultati WHERE id NOT IN (
    SELECT MIN(id) FROM risultati GROUP BY
        sorgente, query_specializzazione, query_citta,
        COALESCE(result_id,''), COALESCE(address_id,''), COALESCE(servizio,''))
"""


class Storage:
    """Wrapper SQLite thread-safe.

    Streamlit esegue lo script in thread diversi (ScriptRunner) e tiene l'oggetto
    in `@st.cache_resource`: con la connessione legata al thread di creazione
    (default di sqlite3) la seconda interazione con la GUI esplode con
    "SQLite objects created in a thread can only be used in that same thread".
    Apriamo quindi con `check_same_thread=False` e serializziamo noi gli accessi
    con un lock — sqlite3 e' compilato in modalita' serialized, il lock ci
    protegge dall'interleaving di execute+commit tra thread.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = _percorso_effettivo(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            # WAL: letture e scrittura non si bloccano a vicenda (in Streamlit
            # la tabella viene interrogata mentre lo scraping sta scrivendo).
            # busy_timeout: aspetta invece di fallire subito con "database is
            # locked". synchronous=NORMAL e' sicuro in WAL e molto piu' veloce
            # sui commit per-pagina.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA_TABELLE)
            self._migra()
            self.conn.executescript(SCHEMA_INDICI)
            try:
                self.conn.execute(UNIQUE_INDEX)
            except sqlite3.IntegrityError:
                # DB preesistente con duplicati: li ripuliamo e riproviamo.
                self.conn.execute(DEDUP)
                self.conn.execute(UNIQUE_INDEX)
            self.conn.commit()

    def _migra(self) -> None:
        """Porta un DB creato prima delle sorgenti allo schema attuale.

        `CREATE TABLE IF NOT EXISTS` non tocca le tabelle gia' esistenti: senza
        questo passaggio un database gia' popolato continuerebbe a girare senza
        colonna `sorgente` e il primo INSERT fallirebbe con "no such column".
        Le righe preesistenti sono tutte MioDottore, che e' anche il DEFAULT
        della colonna: nessun backfill necessario.
        """
        for tabella in ("risultati", "disponibilita"):
            colonne = {r[1] for r in
                       self.conn.execute(f"PRAGMA table_info({tabella})")}
            if "sorgente" not in colonne:
                self.conn.execute(
                    f"ALTER TABLE {tabella} ADD COLUMN sorgente TEXT NOT NULL "
                    f"DEFAULT '{SORGENTE_DEFAULT}'")
        # L'indice unico vecchio non include `sorgente`: lasciarlo in piedi
        # impedirebbe a due portali di usare lo stesso id per medici diversi.
        self.conn.execute("DROP INDEX IF EXISTS idx_ris_unico")
        # Gli altri indici sono solo prestazioni, ma vanno comunque rimpiazzati:
        # SCHEMA_INDICI ricrea le versioni `_v2` subito dopo.
        for nome in INDICI_OBSOLETI:
            self.conn.execute(f"DROP INDEX IF EXISTS {nome}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- inserimento risultati ----------------------------------------------
    def insert_righe(self, righe: Iterable[Riga], query_spec: str, query_citta: str,
                     sorgente: str = SORGENTE_DEFAULT) -> int:
        """Inserisce (upsert) le righe e ritorna quante sono NUOVE.

        Il conteggio e' il delta reale sulla tabella, non `len(rows)`: con la
        paginazione che ripete risultati, le righe gia' viste vengono aggiornate
        e non devono essere contate due volte nel report finale.
        """
        rows = []
        for r in righe:
            d = r.as_dict()
            rows.append((
                sorgente,
                d["result_id"], d["address_id"], d["nome"], d["tipo"],
                d["specializzazione"], d["citta"], d["indirizzo"], d["provincia"],
                d["servizio"], d["servizio_id"], d["prezzo"],
                1 if d["prezzo_e_da_min"] else 0, d["prezzo_raw"],
                d["rating"], d["n_recensioni"], d["lat"], d["lng"],
                d["url_profilo"], 1 if d["ha_calendario"] else 0,
                query_spec, query_citta,
            ))
        with self._lock:
            prima = self.count_risultati()
            self.conn.executemany(
                """INSERT OR REPLACE INTO risultati
                   (sorgente,
                    result_id,address_id,nome,tipo,specializzazione,citta,indirizzo,
                    provincia,servizio,servizio_id,prezzo,prezzo_e_da_min,prezzo_raw,
                    rating,n_recensioni,lat,lng,url_profilo,ha_calendario,
                    query_specializzazione,query_citta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self.conn.commit()
            return self.count_risultati() - prima

    def clear_query(self, query_spec: str, query_citta: str,
                    sorgente: str = SORGENTE_DEFAULT) -> int:
        """Rimuove i risultati precedenti per la stessa (specializzazione, citta)."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM risultati WHERE sorgente=? AND "
                "query_specializzazione=? AND query_citta=?",
                (sorgente, query_spec, query_citta),
            )
            self.conn.commit()
        return cur.rowcount

    # Chiave logica di una riga, la stessa dell'indice UNIQUE.
    _CHIAVE_SQL = ("COALESCE(result_id,'')||'|'||COALESCE(address_id,'')||'|'"
                   "||COALESCE(servizio,'')")

    def clear_query_tranne(self, query_spec: str, query_citta: str,
                           chiavi: Iterable[tuple],
                           sorgente: str = SORGENTE_DEFAULT) -> int:
        """Rimuove le righe della ricerca che `chiavi` NON contiene.

        Solo per `sorgente`: un run su un portale non deve mai cancellare i
        risultati raccolti dagli altri, che non ha nemmeno interrogato.

        Sostituisce il vecchio "cancella tutto e poi riscarica": i dati
        precedenti restano finche' i nuovi non sono arrivati, cosi' uno scraping
        interrotto a meta' non lascia l'utente con meno di quanto aveva.

        Il confronto e' sulle chiavi e non sull'orario di scaricamento perche'
        `CURRENT_TIMESTAMP` ha risoluzione al secondo: due run ravvicinati
        cadrebbero nello stesso istante e il vecchio non verrebbe mai rimosso.
        """
        chiavi = list(chiavi)
        if not chiavi:
            # Nessuna chiave = nessuna prova che il sito non abbia piu' nulla:
            # meglio non toccare niente che azzerare la ricerca.
            return 0
        with self._lock:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS _visti (k TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM _visti")
            self.conn.executemany(
                "INSERT OR IGNORE INTO _visti (k) VALUES (?)",
                [("|".join(str(c) if c is not None else "" for c in ch),)
                 for ch in chiavi])
            cur = self.conn.execute(
                f"DELETE FROM risultati WHERE sorgente=? AND "
                f"query_specializzazione=? AND "
                f"query_citta=? AND {self._CHIAVE_SQL} NOT IN (SELECT k FROM _visti)",
                (sorgente, query_spec, query_citta),
            )
            self.conn.commit()
        return cur.rowcount

    def eta_dati_ore(self, query_spec: str, query_citta: str,
                     sorgente: Optional[str] = None) -> Optional[float]:
        """Da quante ore risalgono i dati di questa ricerca (None se assenti).

        Senza `sorgente` guarda i dati piu' recenti di qualunque portale: e'
        quello che serve alla GUI per dire "non serve riscaricare".
        """
        sql = ("SELECT (julianday('now') - julianday(MAX(data_scraping))) * 24 "
               "FROM risultati WHERE query_specializzazione=? AND query_citta=?")
        params: list = [query_spec, query_citta]
        if sorgente:
            sql += " AND sorgente=?"
            params.append(sorgente)
        r = self.query(sql, params)
        return r[0][0] if r and r[0][0] is not None else None

    # -- disponibilita -------------------------------------------------------
    def insert_disponibilita(self, slots: Sequence[dict],
                             sorgente: str = SORGENTE_DEFAULT) -> int:
        rows = [(
            sorgente,
            s.get("result_id"), s.get("address_id"), s.get("servizio_id"),
            s.get("data"), s.get("orario"), 1 if s.get("prenotabile", True) else 0,
        ) for s in slots]
        with self._lock:
            self.conn.executemany(
                """INSERT INTO disponibilita
                   (sorgente,result_id,address_id,servizio_id,data,orario,prenotabile)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
            self.conn.commit()
        return len(rows)

    def clear_disponibilita(self, result_id: str, address_id: str,
                            sorgente: str = SORGENTE_DEFAULT) -> int:
        """Slot vecchi di un calendario, prima di reinserire i nuovi.

        La chiave e' (sorgente, medico, indirizzo), non il solo `address_id`:
        - `sorgente` perche' `address_id` e' un numero interno del portale e due
          portali possono averne uno uguale per indirizzi diversi;
        - `result_id` perche' su un portale che identifica la *sede* (e non la
          coppia medico-sede) tutti i medici di un poliambulatorio condividono
          lo stesso `address_id`: cancellare per solo indirizzo butterebbe via
          gli slot dei colleghi appena raccolti. Su MioDottore non capita, ma e'
          esattamente il caso del `practiceId` di Doctolib.
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM disponibilita WHERE sorgente=? AND result_id=? "
                "AND address_id=?",
                (sorgente, result_id, address_id))
            self.conn.commit()
        return cur.rowcount

    # -- query generiche -----------------------------------------------------
    def query(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def riepilogo_query(self) -> List[sqlite3.Row]:
        """Cosa c'e' gia' nel DB, per non rifare uno scraping gia' fatto.

        Il GROUP BY usa i nomi VERI delle colonne, non gli alias: `citta` e'
        anche una colonna della tabella (la citta' del medico, che dentro una
        stessa ricerca varia — «podologo/Milano» trova studi a Sesto, Cologno…).
        SQLite in GROUP BY preferisce la colonna all'alias, quindi raggruppava
        per citta' del medico e una sola ricerca compariva spezzata in decine di
        righe nel pannello «Dati gia' nel database».
        """
        return self.query(
            """SELECT sorgente, query_specializzazione AS spec, query_citta AS citta,
                      COUNT(*) AS righe, MAX(data_scraping) AS ultimo
               FROM risultati
               GROUP BY sorgente, query_specializzazione, query_citta
               ORDER BY ultimo DESC""")

    def sorgenti_presenti(self) -> List[str]:
        """Quali portali hanno gia' dati nel DB (per popolare i filtri)."""
        return [r[0] for r in self.query(
            "SELECT DISTINCT sorgente FROM risultati ORDER BY sorgente")]

    def servizi_noti(self) -> List[str]:
        """Nomi di servizio presenti nel DB (per proporli invece di indovinarli)."""
        return [r[0] for r in self.query(
            "SELECT DISTINCT servizio FROM risultati WHERE servizio IS NOT NULL "
            "AND servizio <> '' ORDER BY servizio")]

    def count_risultati(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM risultati").fetchone()[0]

    def count_disponibilita(self) -> int:
        """Quanti slot orari ci sono. Zero significa che nessuno scraping e'
        mai stato fatto con l'opzione «disponibilita»: e' l'unica spiegazione
        di un filtro per disponibilita' che non trova niente (vedi
        `query_engine.avvisi`)."""
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM disponibilita").fetchone()[0]

    # -- export --------------------------------------------------------------
    @staticmethod
    def export_rows_csv(rows: Sequence, path: Path) -> int:
        """Scrive le righe in CSV con BOM (Excel). Accetta `sqlite3.Row` o dict:
        entrambi espongono `.keys()` e l'accesso per chiave."""
        path = Path(path)
        if not rows:
            path.write_text("", encoding="utf-8")
            return 0
        cols = rows[0].keys()
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        return len(rows)

    @staticmethod
    def export_rows_jsonl(rows: Sequence, path: Path) -> int:
        """Scrive le righe in JSON Lines (un oggetto per riga, senza BOM):
        un run interrotto lascia comunque un file leggibile fino all'ultima
        riga scritta, e piu' run si concatenano senza dover rileggere tutto."""
        path = Path(path)
        if not rows:
            path.write_text("", encoding="utf-8")
            return 0
        cols = rows[0].keys()
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({c: r[c] for c in cols}, ensure_ascii=False))
                f.write("\n")
        return len(rows)
