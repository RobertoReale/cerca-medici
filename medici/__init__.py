"""medici — ricerca medici su piu' portali, con prezzi, disponibilita e ordinamento.

Il pacchetto si chiamava `miodottore` quando il progetto interrogava un sito
solo. Ora i portali sono sorgenti intercambiabili (`medici.sorgenti`) e il nome
del pacchetto descrive il dominio, non la fonte. Il portale MioDottore vive in
`medici/sorgenti/miodottore.py`, che quel nome ce l'ha a ragione.

Pipeline: sorgenti -> scraper -> storage -> query_engine.
Il modulo `availability` copre la Fase 3-bis (calendario/orari per indirizzo)
per la sorgente MioDottore.
"""

__version__ = "2.0.0"
