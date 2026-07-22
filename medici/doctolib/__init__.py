"""Client Doctolib.it — lo strato che parla con il sito.

Diviso in due moduli perche' i due problemi sono indipendenti:

- `client.py`  — come si fanno le richieste (header, endpoint, paginazione).
- `slug.py`    — come si traduce «podologo a Milano» in qualcosa che Doctolib
                 capisce, che e' un problema tutto suo (gli slug delle
                 specializzazioni non coincidono con quelli di MioDottore).

La mappatura dei dati su `Riga` NON sta qui: vive in `medici/sorgenti/doctolib.py`,
insieme al resto del contratto `Sorgente`.

Nessun re-export in questo file: sia `client` sia `slug` sono eseguibili con
`python -m`, e importarli da qui li caricherebbe due volte (con tanto di
RuntimeWarning di runpy). Si importa sempre dal modulo preciso.
"""
