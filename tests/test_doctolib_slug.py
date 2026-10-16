"""Risoluzione degli slug Doctolib, su un finto sitemap e un finto client.

Il criterio di uscita della fase 2 e' che i casi reali risolvano e che quello
impossibile fallisca **dicendo cosa scrivere invece**: un errore che elenca
alternative si corregge in cinque secondi, «0 risultati» no.
"""
import pytest

from medici.doctolib.client import ContestoDoctolib, LandingNonTrovata
from medici.doctolib.slug import (SINONIMI, SlugNonRisolto, accettabili,
                                  candidati, carica_specialita, risolvi,
                                  slug_dal_sitemap)

# Un estratto dei 130 slug veri, scelto per contenere le insidie: due slug che
# cominciano allo stesso modo, uno composto e uno che contiene una parola
# generica.
NOTI = [
    "cardiologo", "cardiochirurgo", "dermatologo-venereologo", "podologo",
    "medico-di-medicina-generale", "medico-dello-sport", "psicologo",
    "psicoterapeuta", "ginecologo-ostetrico", "ortopedico-traumatologo",
]

SITEMAP = """<?xml version="1.0" encoding="utf-8" ?><urlset>
<url><loc>https://www.doctolib.it/medico-di-medicina-generale</loc></url>
<url><loc>https://www.doctolib.it/cardiologo</loc></url>
<url><loc>https://www.doctolib.it/dermatologo-venereologo</loc></url>
</urlset>"""


class ClientFinto:
    """Conosce solo le coppie che gli si dichiarano; per tutto il resto imita
    Doctolib, cioe' redirige (e quindi solleva `LandingNonTrovata` con la
    diagnosi giusta)."""

    def __init__(self, coppie_valide, citta_valide=("milano",)):
        self.valide = set(coppie_valide)
        self.citta_valide = set(citta_valide)
        self.chiamate = []

    def contesto_ricerca(self, spec_slug, city_slug):
        self.chiamate.append((spec_slug, city_slug))
        if (spec_slug, city_slug) in self.valide:
            return ContestoDoctolib(spec_slug, city_slug,
                                    keyword={"name": spec_slug},
                                    place={"name": city_slug.capitalize()})
        if spec_slug in {s for s, _ in self.valide}:
            # La specializzazione esiste: allora e' la citta' a non esistere,
            # ed e' quello che dice il redirect a `/{spec}`.
            raise LandingNonTrovata("redirect a /{spec}", 200, "citta")
        raise LandingNonTrovata("redirect a /", 200, "specializzazione")


# --- sitemap e cache --------------------------------------------------------

def test_slug_estratti_dal_sitemap():
    assert slug_dal_sitemap(SITEMAP) == ["medico-di-medicina-generale",
                                         "cardiologo", "dermatologo-venereologo"]


def test_cache_assente_non_e_un_errore(tmp_path):
    """Senza cache si perde il suggerimento, non la funzionalita'."""
    assert carica_specialita(tmp_path / "non-esiste.json") == []


def test_cache_illeggibile_viene_ignorata(tmp_path):
    p = tmp_path / "rotta.json"
    p.write_text("{ non json", encoding="utf-8")
    assert carica_specialita(p) == []


def test_cache_reale_del_progetto_e_popolata():
    """Se qualcuno la cancella o la svuota, i suggerimenti spariscono in
    silenzio: meglio accorgersene qui."""
    assert len(carica_specialita()) > 100


# --- match ------------------------------------------------------------------

def test_contenimento_a_parole_batte_la_somiglianza():
    assert candidati("dermatologo", NOTI)[0] == "dermatologo-venereologo"


def test_una_sola_parola_utile_basta_a_trovare_lo_slug_composto():
    assert candidati("ostetrico", NOTI) == ["ginecologo-ostetrico"]


def test_i_candidati_per_parola_vengono_dal_piu_corto():
    """A parita' di contenimento, lo slug piu' corto e' quello che aggiunge meno
    roba a cio' che l'utente ha chiesto — cioe' il piu' probabile."""
    noti = NOTI + ["psicologo-del-lavoro"]
    # In coda ci sono anche i simili ortografici (podologo, cardiologo…): qui
    # conta l'ordine dei match per parola, che sono quelli certi.
    assert candidati("psicologo", noti)[:2] == ["psicologo", "psicologo-del-lavoro"]


def test_refuso_recuperato_per_somiglianza():
    assert "cardiologo" in candidati("cardiolgo", NOTI)


def test_parole_generiche_non_pescano_mezzo_elenco():
    """`medico` e' dentro `medico-di-medicina-generale` e `medico-dello-sport`:
    senza la lista di parole deboli, «medico» proporrebbe tutto."""
    assert candidati("medico", NOTI) == []


def test_input_vuoto_non_propone_nulla():
    assert candidati("   ", NOTI) == []


# --- risoluzione ------------------------------------------------------------

def test_slug_diretto_costa_una_sola_richiesta():
    c = ClientFinto([("cardiologo", "milano")])
    ctx = risolvi("Cardiologo", "Milano", client=c, noti=NOTI)
    assert ctx.spec_slug == "cardiologo"
    assert len(c.chiamate) == 1


def test_slug_corretto_dall_elenco():
    c = ClientFinto([("dermatologo-venereologo", "milano")])
    ctx = risolvi("Dermatologo", "Milano", client=c, noti=NOTI)
    assert ctx.spec_slug == "dermatologo-venereologo"


def test_citta_ignota_si_ferma_subito():
    """Non deve proseguire con le alternative: la pagina su cui Doctolib
    reindirizza contiene medici veri di un'altra citta'."""
    c = ClientFinto([("cardiologo", "milano")])
    with pytest.raises(SlugNonRisolto, match="citta"):
        risolvi("Cardiologo", "Pinco", client=c, noti=NOTI)


def test_citta_ignota_scoperta_solo_dopo_aver_corretto_la_specializzazione():
    """`/dermatologo/pinco` redirige a `/`: sembra colpa della
    specializzazione. Solo provando `dermatologo-venereologo` si scopre che il
    pezzo sbagliato era la citta', e il messaggio deve dirlo."""
    c = ClientFinto([("dermatologo-venereologo", "milano")])
    with pytest.raises(SlugNonRisolto, match="citta"):
        risolvi("Dermatologo", "Pinco", client=c, noti=NOTI)


def test_refuso_risolto_senza_che_l_utente_se_ne_accorga():
    c = ClientFinto([("cardiologo", "milano")])
    assert risolvi("cardiolgo", "Milano", client=c, noti=NOTI).spec_slug == "cardiologo"


def test_specializzazione_inesistente_elenca_le_alternative():
    """Doctolib non copre questa specializzazione: nemmeno lo slug piu' simile
    risponde. L'errore deve comunque proporre qualcosa — «0 risultati» non si
    corregge, «forse intendevi cardiologo» si'."""
    c = ClientFinto([("medico-di-medicina-generale", "milano")])
    with pytest.raises(SlugNonRisolto) as e:
        risolvi("cardiolgo", "Milano", client=c, noti=NOTI)
    assert "cardiologo" in e.value.alternative
    assert "cardiologo" in str(e.value)


def test_sito_che_non_risponde_non_e_colpa_dell_utente():
    """Se fallisce anche `medico-di-medicina-generale`, il messaggio non deve
    accusare cio' che l'utente ha scritto."""
    c = ClientFinto([])           # nemmeno la specializzazione di riferimento
    with pytest.raises(SlugNonRisolto, match="non e' la tua ricerca"):
        risolvi("cardiologo", "Milano", client=c, noti=NOTI)


def test_ricerca_incompleta():
    with pytest.raises(SlugNonRisolto):
        risolvi("", "Milano", client=ClientFinto([]), noti=NOTI)


# --- traduzioni sbagliate viste sul campo -----------------------------------
# Prima di `accettabili`, il primo candidato che rispondeva veniva preso per
# buono: queste ricerche partivano senza errore sulla specializzazione sbagliata.

# Le insidie vere dell'elenco completo: ogni nome di MioDottore qui sotto ha un
# vicino ortografico o un contenitore che e' un'altra professione.
NOTI_INSIDIE = NOTI + [
    "dietista", "genetista", "odontoiatra", "ortodontista", "psicanalista",
    "oftalmologo", "tecnico-ortopedico", "laboratorio", "otorinolaringoiatra",
    "ematologo", "tecnico-radiologo", "radiologo-diagnostico",
]


@pytest.mark.parametrize("voluto,sbagliato", [
    ("dentista", "dietista"),
    ("oculista", "psicanalista"),
    ("otorino", "laboratorio"),
    ("ortopedico", "tecnico-ortopedico"),
    ("radiologo", "tecnico-radiologo"),
])
def test_nessuna_traduzione_verso_un_altra_professione(voluto, sbagliato):
    assert sbagliato not in accettabili(voluto, NOTI_INSIDIE)


@pytest.mark.parametrize("voluto,giusto", [
    ("dentista", "odontoiatra"),
    ("oculista", "oftalmologo"),
    ("ortopedico", "ortopedico-traumatologo"),
    ("Medico di base", "medico-di-medicina-generale"),
])
def test_i_nomi_di_miodottore_trovano_il_sinonimo(voluto, giusto):
    c = ClientFinto([(giusto, "milano")] + [(s, "milano") for s in NOTI_INSIDIE])
    assert risolvi(voluto, "Milano", client=c, noti=NOTI_INSIDIE).spec_slug == giusto


def test_contenimento_ambiguo_non_si_sceglie_da_solo():
    """Due slug contengono la parola: decidere tocca all'utente."""
    noti = ["tecnico-podologo", "podologo-sportivo"]
    assert accettabili("podologo", noti) == []
    assert set(candidati("podologo", noti)) == set(noti)


def test_ogni_sinonimo_porta_a_uno_slug_vero():
    """Un refuso nella tabella si manifesterebbe solo a runtime, come
    «specializzazione sconosciuta» su un nome che invece e' giusto."""
    noti = set(carica_specialita())
    assert {v for v in SINONIMI.values() if v not in noti} == set()


def test_somiglianza_bassa_resta_un_suggerimento():
    """`dentista` somiglia a `dietista` (0.875): lo si propone, non lo si usa."""
    noti = ["dietista"]
    c = ClientFinto([("dietista", "milano"),
                     ("medico-di-medicina-generale", "milano")])
    with pytest.raises(SlugNonRisolto) as e:
        risolvi("Dentistta", "Milano", client=c, noti=noti)
    assert ("dietista", "milano") not in c.chiamate
    assert "dietista" in e.value.alternative


# --- citta' con un altro nome ------------------------------------------------

def test_roma_esiste_solo_come_citta_metropolitana():
    c = ClientFinto([("cardiologo", "citta-metropolitana-di-roma")],
                    citta_valide=("citta-metropolitana-di-roma",))
    ctx = risolvi("Cardiologo", "Roma", client=c, noti=NOTI)
    assert ctx.city_slug == "citta-metropolitana-di-roma"


def test_forma_alternativa_anche_dopo_aver_corretto_la_specializzazione():
    c = ClientFinto([("dermatologo-venereologo", "citta-metropolitana-di-roma")])
    ctx = risolvi("Dermatologo", "Roma", client=c, noti=NOTI)
    assert (ctx.spec_slug, ctx.city_slug) == ("dermatologo-venereologo",
                                              "citta-metropolitana-di-roma")


def test_sonda_su_citta_ignota_da_la_colpa_alla_citta():
    """Specializzazione e citta' sconosciute insieme: la sonda risolve la
    specializzazione e inciampa sulla citta', quindi e' la citta' a essere
    segnalata — non «sito cambiato»."""
    c = ClientFinto([("medico-di-medicina-generale", "milano")])
    with pytest.raises(SlugNonRisolto, match="citta"):
        risolvi("inesistente", "Pinco", client=c, noti=NOTI)
