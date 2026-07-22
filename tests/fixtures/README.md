# Fixture

Risposte reali, catturate una volta e congelate qui. Servono a far girare i test
**senza rete**: una suite che dipende da un sito vivo fallisce quando quel sito
ha una brutta giornata, e passa quando ha smesso di funzionare ma nessuno se n'e'
accorto.

| File | Origine |
|---|---|
| `doctolib_search.json` | `POST /patient-health-search/api/v1/hcp/search` per `cardiologo`/`milano`. Sei record scelti (su 611) perche' insieme coprono ogni caso della mappatura: `ORGANIZATION` e `INDIVIDUAL_PRACTITIONER`, prezzo fisso e `VARIES_DEPENDING_ON_HCP`, prezzo assente, rating presente e assente, e un profilo **senza agenda online** (`services: []`, nessun `matchedVisitMotive`). `total` e' quello vero della ricerca completa. |
| `doctolib_availabilities_vuote.json` | `GET /search/availabilities.json` per un medico che non prenota online: e' la risposta che l'endpoint da' piu' spesso, con il campo `reason` che lo spiega. Catturata. |
| `doctolib_availabilities.json` | Stessa forma, **con** slot. Non catturata: nei tentativi fatti nessun medico interrogato aveva slot liberi nella finestra di 7 giorni. Ricostruita dalla risposta verificata durante la ricognizione del piano (§1.2), di cui riproduce la struttura esatta — inclusa la giornata con `slots: []` in mezzo, che e' il caso da cui nasce l'appiattimento in `client.slots()`. |

Se un giorno la mappatura cambia, il test che usa `doctolib_search.json` deve
fallire: e' l'unico modo per accorgersi che i campi non significano piu' quello
che significavano.
