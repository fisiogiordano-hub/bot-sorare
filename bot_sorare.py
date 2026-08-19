import os
import time
import threading
import requests
import hashlib

from decimal import Decimal
from flask import Flask


app = Flask(__name__)


# ============================================================
# CONFIGURAZIONE
# ============================================================

SORARE_TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    ""
).strip()

SORARE_JWT_AUD = os.getenv(
    "SORARE_JWT_AUD",
    ""
).strip()

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

SORARE_STARK_PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# SICUREZZA
# ============================================================

# IMPORTANTE:
#
# Il bot rimane SEMPRE in DRY RUN.
#
# Nessun rifiuto reale.
# Nessuna controproposta reale.
# Nessuna transazione reale.

DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

PREZZO_MINIMO_CENTESIMI = 30
PREZZO_MASSIMO_CENTESIMI = 80

PAGAMENTO_PER_CARTA_CENTESIMI = 20


# ============================================================
# CAMPIONATI COPERTI
# ============================================================
#
# Lista DEFINITIVA.
#
# Sono 26 campionati LOGICI.
#
# Gli slug Sorare possono avere varianti diverse.
# Per questo la verifica utilizza una funzione di
# normalizzazione e degli alias.
#
# REGOLA FONDAMENTALE:
#
# 1. Il giocatore deve avere una squadra ATTIVA.
# 2. Solo la squadra ATTIVA viene analizzata.
# 3. La squadra deve avere almeno una competizione
#    attiva compatibile con uno dei 26 campionati.
#
# Se activeClub è assente:
#
#       -> CARTA NON IDONEA
#
# Anche se Sorare dovesse restituire vecchie competizioni
# nella scheda del giocatore.
# ============================================================

CAMPIONATI_COPERTI = {
    "english-league": {
        "nome": "English League",
        "alias": {
            "english-league",
            "premier-league-eng",
        },
    },

    "ligue-1-fr": {
        "nome": "Ligue 1",
        "alias": {
            "ligue-1-fr",
            "ligue-1",
        },
    },

    "laliga-es": {
        "nome": "LALIGA EA SPORTS",
        "alias": {
            "laliga-es",
            "laliga",
            "la-liga",
            "laliga-ea-sports",
        },
    },

    "bundesliga-de": {
        "nome": "Bundesliga",
        "alias": {
            "bundesliga-de",
            "bundesliga",
        },
    },

    "liga-portugal": {
        "nome": "Liga Portugal",
        "alias": {
            "liga-portugal",
            "primeira-liga-pt",
            "liga-portugal-pt",
        },
    },

    "eredivisie-nl": {
        "nome": "Eredivisie",
        "alias": {
            "eredivisie-nl",
            "eredivisie",
        },
    },

    "jupiler-pro-league-be": {
        "nome": "Jupiler Pro League",
        "alias": {
            "jupiler-pro-league-be",
            "jupiler-pro-league",
        },
    },

    "scottish-premiership-sco": {
        "nome": "Scottish Premiership",
        "alias": {
            "scottish-premiership-sco",
            "scottish-premiership",
        },
    },

    "jleague-jp": {
        "nome": "J.League",
        "alias": {
            "jleague-jp",
            "j1-league-jp",
            "j-league",
            "j1-league",
        },
    },

    "second-division-eng": {
        "nome": "Seconda divisione inglese",
        "alias": {
            "second-division-eng",
            "championship-eng",
            "english-championship",
            "championship",
        },
    },

    "austrian-bundesliga-at": {
        "nome": "Austrian Bundesliga",
        "alias": {
            "austrian-bundesliga-at",
            "austrian-bundesliga",
            "bundesliga-at",
        },
    },

    "croatian-hnl-hr": {
        "nome": "Croatian HNL",
        "alias": {
            "croatian-hnl-hr",
            "croatian-first-league-hr",
            "croatian-first-league",
            "croatian-hnl",
        },
    },

    "2-bundesliga-de": {
        "nome": "2. Bundesliga",
        "alias": {
            "2-bundesliga-de",
            "2-bundesliga",
        },
    },

    "ligue-2-fr": {
        "nome": "Ligue 2",
        "alias": {
            "ligue-2-fr",
            "ligue-2",
        },
    },

    "mls-us": {
        "nome": "MLS",
        "alias": {
            "mls-us",
            "major-league-soccer-us",
            "major-league-soccer",
            "mls",
        },
    },

    "k-league-1-kr": {
        "nome": "K League",
        "alias": {
            "k-league-1-kr",
            "k-league-1",
            "k-league",
        },
    },

    "super-lig-tr": {
        "nome": "Turchia",
        "alias": {
            "super-lig-tr",
            "super-lig",
            "turkish-super-lig",
        },
    },

    "superliga-dk": {
        "nome": "Danimarca",
        "alias": {
            "superliga-dk",
            "superliga",
            "danish-superliga",
        },
    },

    "serie-a-it": {
        "nome": "Serie A",
        "alias": {
            "serie-a-it",
            "serie-a",
        },
    },

    "brasileirao-serie-a-br": {
        "nome": "Brasile",
        "alias": {
            "brasileirao-serie-a-br",
            "brasileirao-serie-a",
            "brasileirao",
            "serie-a-br",
        },
    },

    "premier-liga-ru": {
        "nome": "Russia",
        "alias": {
            "premier-liga-ru",
            "russian-premier-league",
            "premier-liga",
            "russia-premier-league",
        },
    },

    "serie-b-it": {
        "nome": "Serie B",
        "alias": {
            "serie-b-it",
            "serie-b",
        },
    },

    "liga-1-peru": {
        "nome": "Perù",
        "alias": {
            "liga-1-peru",
            "liga-1-pe",
            "liga-1",
            "peruvian-primera-division",
        },
    },

    "primera-a-colombia": {
        "nome": "Colombia",
        "alias": {
            "primera-a-colombia",
            "liga-betplay-col",
            "primera-a",
            "liga-betplay",
        },
    },

    "liga-mx": {
        "nome": "Messico",
        "alias": {
            "liga-mx",
        },
    },

    "laliga-2-es": {
        "nome": "LALIGA 2",
        "alias": {
            "laliga-2-es",
            "laliga-hypermotion",
            "laliga-2",
            "segunda-division-spain",
        },
    },
}


# ============================================================
# CONTROLLO NUMERO CAMPIONATI
# ============================================================

NUMERO_CAMPIONATI = len(
    CAMPIONATI_COPERTI
)


# ============================================================
# SORARE API
# ============================================================

SORARE_API_URL = (
    "https://api.sorare.com/graphql"
)


# ============================================================
# KULENOVIC
# ============================================================

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET_ID = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATO
# ============================================================

offerte_gia_analizzate = set()

monitoraggio_avviato = False

lock_avvio = threading.Lock()


# ============================================================
# NORMALIZZAZIONE SLUG
# ============================================================

def normalizza_slug(valore):

    if valore is None:
        return ""

    valore = str(valore).strip().lower()

    return valore


# ============================================================
# TROVA CAMPIONATO COPERTO
# ============================================================

def trova_campionato_coperto(slug):

    slug_normalizzato = normalizza_slug(
        slug
    )

    if not slug_normalizzato:
        return None

    for dati in CAMPIONATI_COPERTI.values():

        alias = dati.get(
            "alias",
            set()
        )

        if slug_normalizzato in alias:

            return dati.get(
                "nome"
            )

    return None


# ============================================================
# STAMPA CAMPIONATI
# ============================================================

def stampa_campionati_coperti():

    print(
        "🏆 REGOLA CAMPIONATI:"
    )

    print(
        f"   {NUMERO_CAMPIONATI} campionati coperti."
    )

    for dati in CAMPIONATI_COPERTI.values():

        print(
            f"   • {dati['nome']}"
        )


# ============================================================
# TEST LOCALE FIRMA STARK
# ============================================================
#
# Questo test NON esegue nessuna mutation Sorare.
#
# Se la versione di starknet-py installata non espone
# il vecchio modulo interno, NON facciamo fallire
# l'intero bot.
#
# La modalità DRY RUN rimane comunque attiva.
# ============================================================

def test_firma_stark():

    print("")
    print("========================================")
    print("🔐 TEST LOCALE FIRMA STARK")
    print("========================================")

    if not SORARE_STARK_PRIVATE_KEY:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY non configurata."
        )

        return False

    print(
        "✅ SORARE_STARK_PRIVATE_KEY presente."
    )

    chiave = SORARE_STARK_PRIVATE_KEY.strip()

    if chiave.lower().startswith("0x"):

        chiave_hex = chiave[2:]

    else:

        chiave_hex = chiave

    if not chiave_hex:

        print(
            "❌ Chiave Stark vuota."
        )

        return False

    try:

        private_key_int = int(
            chiave_hex,
            16
        )

    except ValueError:

        print(
            "❌ La chiave non è un valore esadecimale valido."
        )

        return False

    if private_key_int <= 0:

        print(
            "❌ La chiave privata non è valida."
        )

        return False

    print(
        "✅ Formato esadecimale verificato."
    )

    # --------------------------------------------------------
    # IMPORT COMPATIBILE
    # --------------------------------------------------------

    PrivateKey = None
    message_signature = None

    import_error = None

    possibili_import = [

        (
            "starknet_py.net.signer.stark_curve",
            "PrivateKey",
            "message_signature",
        ),

        (
            "starknet_py.net.signer.stark_curve",
            "PrivateKey",
            None,
        ),
    ]

    for (
        modulo,
        nome_private_key,
        nome_signature,
    ) in possibili_import:

        try:

            import importlib

            mod = importlib.import_module(
                modulo
            )

            PrivateKey = getattr(
                mod,
                nome_private_key,
                None
            )

            if nome_signature:

                message_signature = getattr(
                    mod,
                    nome_signature,
                    None
                )

            if PrivateKey is not None:

                break

        except Exception as e:

            import_error = e

    if PrivateKey is None:

        print(
            "⚠️ API locale starknet-py non compatibile "
            "con questo test."
        )

        if import_error:

            print(
                f"   Dettaglio: {import_error}"
            )

        print(
            "🟡 Il bot rimane in DRY RUN."
        )

        print(
            "🟡 Nessuna operazione reale verrà eseguita."
        )

        return False

    print(
        "✅ API Starknet importate correttamente."
    )

    # --------------------------------------------------------
    # PRIVATE KEY
    # --------------------------------------------------------

    try:

        private_key = PrivateKey(
            private_key_int
        )

    except Exception as e:

        print(
            "❌ Impossibile inizializzare "
            "la chiave Stark."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Private key Stark inizializzata."
    )

    # --------------------------------------------------------
    # PUBLIC KEY
    # --------------------------------------------------------

    try:

        public_key = private_key.public_key

    except Exception as e:

        print(
            "❌ Impossibile derivare la public key."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Public key Stark derivata."
    )

    # --------------------------------------------------------
    # HASH TEST
    # --------------------------------------------------------

    try:

        messaggio_testo = (
            "SORARE_LOCAL_SIGNATURE_TEST"
        )

        digest = hashlib.sha256(
            messaggio_testo.encode("utf-8")
        ).digest()

        messaggio_hash = int.from_bytes(
            digest,
            byteorder="big"
        )

        STARK_FIELD_PRIME = (
            (2 ** 251)
            + (17 * (2 ** 192))
            + 1
        )

        messaggio_hash %= STARK_FIELD_PRIME

    except Exception as e:

        print(
            "❌ Impossibile creare l'hash di test."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Hash di test Stark preparato."
    )

    # --------------------------------------------------------
    # FIRMA
    # --------------------------------------------------------

    if message_signature is None:

        print(
            "⚠️ Funzione message_signature "
            "non disponibile nella versione installata."
        )

        print(
            "🟡 Il test locale della firma viene saltato."
        )

        print(
            "🟡 Il bot rimane in DRY RUN."
        )

        return False

    try:

        firma = message_signature(
            private_key,
            messaggio_hash
        )

    except Exception as e:

        print(
            "❌ Generazione firma fallita."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    if not firma:

        print(
            "❌ Firma non generata."
        )

        return False

    print(
        "✅ Firma Stark generata."
    )

    # --------------------------------------------------------
    # R / S
    # --------------------------------------------------------

    try:

        r = firma[0]
        s = firma[1]

    except Exception as e:

        print(
            "❌ Struttura della firma inattesa."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    if r is None or s is None:

        print(
            "❌ Firma priva dei valori r/s."
        )

        return False

    print(
        "✅ Firma contiene r e s."
    )

    # --------------------------------------------------------
    # VERIFICA
    # --------------------------------------------------------

    try:

        verificata = public_key.verify(
            messaggio_hash,
            firma
        )

    except Exception as e:

        print(
            "❌ Errore durante la verifica locale."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    if not verificata:

        print(
            "❌ VERIFICA FIRMA FALLITA."
        )

        return False

    print(
        "✅ FIRMA VERIFICATA LOCALMENTE."
    )

    print(
        "🟢 Private key funzionante."
    )

    print(
        "🟢 Public key derivata correttamente."
    )

    print(
        "🟢 Coppia firma/verifica funzionante."
    )

    print(
        "🟡 Questo NON è un test di una mutation Sorare."
    )

    print(
        "🟡 Nessuna mutation Sorare eseguita."
    )

    print(
        "🟡 Nessuna transazione eseguita."
    )

    print(
        "🟡 Nessuna offerta modificata."
    )

    print(
        "========================================"
    )

    return True


# ============================================================
# HEADERS
# ============================================================

def crea_headers():

    if not SORARE_TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato."
        )

    token = SORARE_TOKEN

    if not token.lower().startswith(
        "bearer "
    ):

        token = f"Bearer {token}"

    headers = {

        "Content-Type": "application/json",

        "Accept": "application/json",

        "Authorization": token,
    }

    if SORARE_JWT_AUD:

        headers["JWT-AUD"] = (
            SORARE_JWT_AUD
        )

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def esegui_query(
    query,
    variables=None
):

    payload = {

        "query": query,

        "variables": variables or {},
    }

    try:

        response = requests.post(

            SORARE_API_URL,

            json=payload,

            headers=crea_headers(),

            timeout=30,
        )

        print(
            f"🌐 Sorare HTTP: "
            f"{response.status_code}"
        )

        if response.status_code != 200:

            print(
                "❌ Risposta HTTP non valida:"
            )

            print(
                response.text[:3000]
            )

            return None

        try:

            risultato = response.json()

        except ValueError:

            print(
                "❌ Risposta Sorare non JSON."
            )

            print(
                response.text[:3000]
            )

            return None

        errori = risultato.get(
            "errors"
        )

        if errori:

            print(
                "❌ Errori GraphQL:"
            )

            for errore in errori:

                print(
                    "- "
                    + str(
                        errore.get(
                            "message",
                            "Errore sconosciuto",
                        )
                    )
                )

            return None

        return risultato

    except requests.RequestException as e:

        print(
            f"❌ Errore HTTP Sorare: {e}"
        )

        return None

    except Exception as e:

        print(
            f"❌ Errore richiesta Sorare: {e}"
        )

        return None


# ============================================================
# ACCOUNT
# ============================================================

def verifica_account():

    query = """

    query CurrentUserTest {

        currentUser {

            slug

            nickname

        }
    }

    """

    risultato = esegui_query(
        query
    )

    if not risultato:

        return False

    user = (
        risultato
        .get("data", {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ Sorare non ha restituito currentUser."
        )

        return False

    print("")
    print("========================================")
    print("✅ AUTENTICAZIONE SORARE RIUSCITA")

    print(
        f"👤 Manager: "
        f"{user.get('nickname') or 'N/D'}"
    )

    print(
        f"🔗 Slug: "
        f"{user.get('slug') or 'N/D'}"
    )

    print("========================================")
    print("")

    return True


# ============================================================
# OFFERTE PENDING
# ============================================================

def recupera_offerte():

    query = """

    query PendingOffers {

        currentUser {

            pendingTokenOffersReceived(
                first: 50
            ) {

                nodes {

                    id

                    status

                    sender {

                        ... on User {

                            slug

                            nickname
                        }
                    }

                    senderSide {

                        anyCards {

                            assetId

                            slug

                            collection
                        }
                    }

                    receiverSide {

                        anyCards {

                            assetId

                            slug

                            collection
                        }
                    }
                }
            }
        }
    }

    """

    risultato = esegui_query(
        query
    )

    if not risultato:

        return []

    user = (
        risultato
        .get("data", {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ currentUser assente."
        )

        return []

    connessione = (
        user.get(
            "pendingTokenOffersReceived"
        )
        or {}
    )

    return (
        connessione.get(
            "nodes"
        )
        or []
    )


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(
    asset_ids
):

    if not asset_ids:

        return []

    query = """

    query CardDetails(
        $assetIds: [String!]
    ) {

        anyCards(
            assetIds: $assetIds
        ) {

            assetId

            slug

            name

            rarityTyped

            collection

            anyPlayer {

                displayName

                slug

                activeClub {

                    slug

                    name

                    activeCompetitions {

                        slug
                    }
                }
            }

            anyTeam {

                name

                activeCompetitions {

                    slug
                }
            }

            lowestPriceCard {

                assetId

                slug

                name

                rarityTyped

                seasonYear

                liveSingleSaleOffer {

                    receiverSide {

                        amounts {

                            eurCents
                        }
                    }
                }

                publicMinPrices {

                    eurCents
                }
            }

            lowestPriceCardAnySeason {

                assetId

                slug

                name

                rarityTyped

                seasonYear

                liveSingleSaleOffer {

                    receiverSide {

                        amounts {

                            eurCents
                        }
                    }
                }

                publicMinPrices {

                    eurCents
                }
            }
        }
    }

    """

    risultato = esegui_query(

        query,

        {
            "assetIds": asset_ids
        }
    )

    if not risultato:

        return []

    carte = (
        risultato
        .get("data", {})
        .get("anyCards")
        or []
    )

    return carte


# ============================================================
# LETTURA EUR CENTS
# ============================================================

def leggi_eur_cents(
    amounts
):

    if not amounts:

        return None

    valore = amounts.get(
        "eurCents"
    )

    if valore is None:

        return None

    try:

        valore = int(
            valore
        )

    except (
        ValueError,
        TypeError,
    ):

        return None

    if valore <= 0:

        return None

    return valore


# ============================================================
# PREZZO LIVE SALE
# ============================================================

def prezzo_da_live_sale(
    carta
):

    if not carta:

        return None

    offerta = (
        carta.get(
            "liveSingleSaleOffer"
        )
        or {}
    )

    receiver_side = (
        offerta.get(
            "receiverSide"
        )
        or {}
    )

    amounts = (
        receiver_side.get(
            "amounts"
        )
        or {}
    )

    eur_cents = leggi_eur_cents(
        amounts
    )

    if eur_cents is None:

        return None

    return (
        Decimal(eur_cents)
        / Decimal("100")
    )


# ============================================================
# PREZZO PUBLIC MIN
# ============================================================

def prezzo_da_public_min_price(
    carta
):

    if not carta:

        return None

    amounts = (
        carta.get(
            "publicMinPrices"
        )
        or {}
    )

    eur_cents = leggi_eur_cents(
        amounts
    )

    if eur_cents is None:

        return None

    return (
        Decimal(eur_cents)
        / Decimal("100")
    )


# ============================================================
# PREZZO FLOOR
# ============================================================

def recupera_prezzo_floor(
    carta
):

    slug = str(
        carta.get("slug")
        or ""
    ).strip()

    nome = str(
        carta.get("name")
        or slug
        or "Carta sconosciuta"
    ).strip()

    if not slug:

        print(
            "      ⚠️ Slug carta assente."
        )

        return None

    print(
        f"      🔎 Ricerca prezzo floor: "
        f"{slug}"
    )

    lowest_price_card = (
        carta.get(
            "lowestPriceCard"
        )
        or {}
    )

    lowest_slug = str(
        lowest_price_card.get(
            "slug"
        )
        or ""
    ).strip()

    if lowest_slug:

        print(
            f"      🎯 Carta floor trovata: "
            f"{lowest_slug}"
        )

    else:

        print(
            "      ⚠️ lowestPriceCard "
            "non disponibile."
        )

    valori = []

    prezzo_live = (
        prezzo_da_live_sale(
            lowest_price_card
        )
    )

    if prezzo_live is not None:

        valori.append(
            prezzo_live
        )

        print(
            f"      💰 Offerta vendita: "
            f"€{prezzo_live:.2f}"
        )

    prezzo_public = (
        prezzo_da_public_min_price(
            lowest_price_card
        )
    )

    if prezzo_public is not None:

        valori.append(
            prezzo_public
        )

        print(
            f"      💰 Public min price: "
            f"€{prezzo_public:.2f}"
        )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    if not valori:

        prezzo_live_carta = (
            prezzo_da_live_sale(
                carta
            )
        )

        if prezzo_live_carta is not None:

            valori.append(
                prezzo_live_carta
            )

            print(
                f"      💰 Offerta carta: "
                f"€{prezzo_live_carta:.2f}"
            )

        prezzo_public_carta = (
            prezzo_da_public_min_price(
                carta
            )
        )

        if prezzo_public_carta is not None:

            valori.append(
                prezzo_public_carta
            )

            print(
                f"      💰 Public min carta: "
                f"€{prezzo_public_carta:.2f}"
            )

    if not valori:

        print(
            f"      ⚠️ Prezzo non disponibile "
            f"per {nome}."
        )

        return None

    floor = min(
        valori
    )

    print(
        f"      ✅ FLOOR VERIFICATO: "
        f"€{floor:.2f}"
    )

    return floor


# ============================================================
# CONTROLLO SQUADRA E CAMPIONATO
# ============================================================
#
# REGOLA CRITICA:
#
# activeClub mancante:
#
#       SEMPRE NON IDONEO
#
# Non vengono considerate:
#
# - vecchie squadre
# - vecchie competizioni
# - competizioni generiche del giocatore
#
# Viene considerata ESCLUSIVAMENTE:
#
#       anyPlayer.activeClub
#
# e le sue:
#
#       activeCompetitions
#
# ============================================================

def controlla_squadra_e_campionato(
    carta
):

    player = (
        carta.get(
            "anyPlayer"
        )
        or {}
    )

    player_name = (
        player.get(
            "displayName"
        )
        or player.get(
            "slug"
        )
        or carta.get(
            "name"
        )
        or "Giocatore sconosciuto"
    )

    active_club = (
        player.get(
            "activeClub"
        )
    )

    # ========================================================
    # NESSUNA SQUADRA ATTIVA
    # ========================================================

    if not active_club:

        print(
            f"      🏟️ Squadra attiva: NESSUNA"
        )

        print(
            f"      👤 Giocatore: "
            f"{player_name}"
        )

        print(
            "      🔴 GIOCATORE SENZA SQUADRA"
        )

        print(
            "      🔴 CAMPIONATO NON VALIDO"
        )

        return False

    # ========================================================
    # SQUADRA ATTIVA
    # ========================================================

    club_name = str(
        active_club.get(
            "name"
        )
        or active_club.get(
            "slug"
        )
        or ""
    ).strip()

    club_slug = normalizza_slug(
        active_club.get(
            "slug"
        )
    )

    print(
        f"      🏟️ Squadra attiva: "
        f"{club_name or 'N/D'}"
    )

    if club_slug:

        print(
            f"      🏷️ Squadra slug: "
            f"{club_slug}"
        )

    else:

        print(
            "      ⚠️ Slug squadra assente."
        )

    # ========================================================
    # COMPETIZIONI DELLA SQUADRA ATTIVA
    # ========================================================

    competizioni = (
        active_club.get(
            "activeCompetitions"
        )
        or []
    )

    if not competizioni:

        print(
            "      🔴 Nessuna competizione "
            "attiva sulla squadra."
        )

        print(
            "      🔴 CAMPIONATO NON COPERTO"
        )

        return False

    print(
        "      🏆 COMPETIZIONI ATTIVE "
        "DELLA SQUADRA:"
    )

    campionati_trovati = []

    for competizione in competizioni:

        if not isinstance(
            competizione,
            dict,
        ):

            continue

        slug = normalizza_slug(
            competizione.get(
                "slug"
            )
        )

        if not slug:

            continue

        print(
            f"         • {slug}"
        )

        nome_campionato = (
            trova_campionato_coperto(
                slug
            )
        )

        if nome_campionato:

            campionati_trovati.append(
                nome_campionato
            )

    # ========================================================
    # CAMPIONATO COPERTO
    # ========================================================

    if campionati_trovati:

        campionati_unici = list(
            dict.fromkeys(
                campionati_trovati
            )
        )

        print(
            "      🟢 CAMPIONATO COPERTO"
        )

        for nome in campionati_unici:

            print(
                f"         🟢 {nome}"
            )

        return True

    # ========================================================
    # NESSUN CAMPIONATO COPERTO
    # ========================================================

    print(
        "      🔴 CAMPIONATO NON COPERTO"
    )

    print(
        "      🔎 Nessuna delle competizioni "
        "della squadra attiva è presente "
        "nella lista dei campionati coperti."
    )

    return False


# ============================================================
# CONTROLLO CARTA
# ============================================================

def analizza_carta(
    carta
):

    asset_id = (
        carta.get(
            "assetId"
        )
    )

    slug = (
        carta.get(
            "slug"
        )
    )

    nome = (
        carta.get(
            "name"
        )
        or slug
        or asset_id
        or "Carta sconosciuta"
    )

    rarita = str(
        carta.get(
            "rarityTyped"
        )
        or ""
    ).upper()

    # ========================================================
    # PREZZO
    # ========================================================

    prezzo = (
        recupera_prezzo_floor(
            carta
        )
    )

    prezzo_verificabile = (
        prezzo is not None
    )

    prezzo_minimo = (
        Decimal(
            PREZZO_MINIMO_CENTESIMI
        )
        / Decimal("100")
    )

    prezzo_massimo = (
        Decimal(
            PREZZO_MASSIMO_CENTESIMI
        )
        / Decimal("100")
    )

    prezzo_ok = (
        prezzo_verificabile
        and prezzo >= prezzo_minimo
        and prezzo <= prezzo_massimo
    )

    # ========================================================
    # RARITÀ
    # ========================================================

    rarita_ok = (
        rarita == "LIMITED"
    )

    # ========================================================
    # SQUADRA + CAMPIONATO
    # ========================================================

    campionato_coperto = (
        controlla_squadra_e_campionato(
            carta
        )
    )

    # ========================================================
    # IDONEITÀ
    # ========================================================

    idonea = (
        rarita_ok
        and prezzo_ok
        and campionato_coperto
    )

    # ========================================================
    # LOG
    # ========================================================

    print("")

    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: "
        f"{asset_id}"
    )

    print(
        f"      Slug: "
        f"{slug or 'N/D'}"
    )

    print(
        f"      Rarità: "
        f"{rarita or 'N/D'}"
    )

    if prezzo is not None:

        print(
            f"      Prezzo floor: "
            f"€{prezzo:.2f}"
        )

        if prezzo < prezzo_minimo:

            print(
                "      🔴 Prezzo inferiore "
                "al minimo di €0,30"
            )

        elif prezzo <= prezzo_massimo:

            print(
                "      🟢 Prezzo tra "
                "€0,30 e €0,80"
            )

        else:

            print(
                "      🔴 Prezzo superiore "
                "al massimo di €0,80"
            )

    else:

        print(
            "      Prezzo floor: N/D"
        )

        print(
            "      🔴 Prezzo NON verificabile"
        )

    if rarita_ok:

        print(
            "      🟢 Rarità LIMITED"
        )

    else:

        print(
            "      🔴 Rarità NON valida"
        )

    print(
        "      =================================="
    )

    if idonea:

        print(
            "      🟢 CARTA IDONEA"
        )

    else:

        print(
            "      ❌ CARTA NON IDONEA"
        )

    print(
        "      =================================="
    )

    return idonea


# ============================================================
# KULENOVIC
# ============================================================

def controlla_kulenovic(
    carte_richieste
):

    print("")

    print(
        "🔎 CARTA/E RICHIESTA/E "
        "DAL MANAGER:"
    )

    kulenovic_presente = False

    configurato = (
        KULENOVIC_ID.strip()
        if KULENOVIC_ID
        else ""
    )

    for carta in carte_richieste:

        asset_id = str(
            carta.get(
                "assetId"
            )
            or ""
        ).strip()

        slug = str(
            carta.get(
                "slug"
            )
            or ""
        ).strip()

        collection = str(
            carta.get(
                "collection"
            )
            or ""
        ).strip()

        print(
            f"   Asset ID: {asset_id}"
        )

        print(
            f"   Slug: {slug}"
        )

        print(
            f"   Collection: {collection}"
        )

        match_configurazione = (
            bool(configurato)
            and (
                asset_id.lower()
                == configurato.lower()
                or
                slug.lower()
                == configurato.lower()
            )
        )

        match_slug = (
            slug.lower()
            == KULENOVIC_SLUG.lower()
        )

        match_asset = (
            asset_id.lower()
            == KULENOVIC_ASSET_ID.lower()
        )

        if (
            match_configurazione
            or match_slug
            or match_asset
        ):

            kulenovic_presente = True

            print(
                "   🎯 KULENOVIC RICONOSCIUTO"
            )

    if kulenovic_presente:

        print(
            "🎯 KULENOVIC RICONOSCIUTO!"
        )

    else:

        print(
            "ℹ️ Kulenovic non riconosciuto "
            "nell'offerta."
        )

        print(
            "ℹ️ L'offerta viene comunque "
            "analizzata in DRY RUN."
        )

    return kulenovic_presente


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(
    offerta
):

    offerta_id = (
        offerta.get(
            "id"
        )
    )

    if not offerta_id:

        return

    if offerta_id in offerte_gia_analizzate:

        return

    offerte_gia_analizzate.add(
        offerta_id
    )

    print("")

    print(
        "========================================"
    )

    print(
        "📨 NUOVA OFFERTA"
    )

    print(
        f"🆔 ID: {offerta_id}"
    )

    print(
        f"📌 Stato: "
        f"{offerta.get('status')}"
    )

    sender = (
        offerta.get(
            "sender"
        )
        or {}
    )

    nickname = (
        sender.get(
            "nickname"
        )
        or sender.get(
            "slug"
        )
        or "Sconosciuto"
    )

    print(
        f"👤 Manager: "
        f"{nickname}"
    )

    sender_side = (
        offerta.get(
            "senderSide"
        )
        or {}
    )

    receiver_side = (
        offerta.get(
            "receiverSide"
        )
        or {}
    )

    carte_offerte = (
        sender_side.get(
            "anyCards"
        )
        or []
    )

    carte_che_diamo = (
        receiver_side.get(
            "anyCards"
        )
        or []
    )

    print(
        f"📦 Carte offerte: "
        f"{len(carte_offerte)}"
    )

    print(
        f"📦 Carte richieste: "
        f"{len(carte_che_diamo)}"
    )

    kulenovic_presente = (
        controlla_kulenovic(
            carte_che_diamo
        )
    )

    asset_ids = [

        carta.get(
            "assetId"
        )

        for carta in carte_offerte

        if carta.get(
            "assetId"
        )
    ]

    if not asset_ids:

        print("")

        print(
            "🔴 DECISIONE: RIFIUTARE"
        )

        print(
            "   Motivo: nessuna carta ricevuta."
        )

        print(
            "🟡 DRY RUN: nessuna operazione "
            "eseguita."
        )

        print(
            "----------------------------------------"
        )

        return

    dettagli = (
        recupera_dettagli_carte(
            asset_ids
        )
    )

    if not dettagli:

        print(
            "⚠️ Impossibile recuperare "
            "i dettagli delle carte."
        )

        print(
            "----------------------------------------"
        )

        return

    carte_idonee = []

    print("")

    print(
        "🔎 ANALISI DELLE CARTE RICEVUTE:"
    )

    for carta in dettagli:

        if analizza_carta(
            carta
        ):

            carte_idonee.append(
                carta
            )

    numero_totale = len(
        asset_ids
    )

    numero_idonee = len(
        carte_idonee
    )

    numero_non_idonee = (
        numero_totale
        - numero_idonee
    )

    print("")

    print(
        "----------------------------------------"
    )

    print(
        f"📊 CARTE TOTALI: "
        f"{numero_totale}"
    )

    print(
        f"📊 CARTE IDONEE: "
        f"{numero_idonee}"
    )

    print(
        f"📊 CARTE NON IDONEE: "
        f"{numero_non_idonee}"
    )

    # ========================================================
    # NESSUNA CARTA IDONEA
    # ========================================================

    if numero_idonee == 0:

        print("")

        print(
            "🔴 DECISIONE: RIFIUTARE L'OFFERTA"
        )

        print(
            "   Motivo: nessuna carta ricevuta "
            "è idonea."
        )

        print("")

        print(
            "🟡 DRY RUN: nessun rifiuto eseguito."
        )

        print(
            "----------------------------------------"
        )

        return

    # ========================================================
    # PAGAMENTO
    # ========================================================

    pagamento_centesimi = (
        numero_idonee
        * PAGAMENTO_PER_CARTA_CENTESIMI
    )

    pagamento_euro = (
        Decimal(
            pagamento_centesimi
        )
        / Decimal("100")
    )

    print("")

    print(
        "🟢 DECISIONE: CONTROPROPOSTA"
    )

    print("")

    print(
        "📤 DALLA PROPOSTA VIENE RIMOSSA:"
    )

    if kulenovic_presente:

        print(
            "   ❌ Kulenovic"
        )

    else:

        print(
            "   ℹ️ Kulenovic non presente"
        )

    print("")

    print(
        "🗑️ VENGONO ELIMINATE "
        "LE CARTE NON IDONEE:"
    )

    if numero_non_idonee == 0:

        print(
            "   Nessuna"
        )

    else:

        print(
            f"   ❌ {numero_non_idonee} carta/e"
        )

    print("")

    print(
        "📥 RIMANGONO SOLO LE CARTE IDONEE:"
    )

    for carta in carte_idonee:

        nome_carta = (
            carta.get(
                "name"
            )
            or carta.get(
                "slug"
            )
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ {nome_carta}"
        )

    print("")

    print(
        f"💰 PAGAMENTO AL MANAGER: "
        f"€{pagamento_euro:.2f}"
    )

    print(
        f"   {numero_idonee} × €0,20"
    )

    print("")

    print(
        "📋 CONTROPROPOSTA PREVISTA:"
    )

    print(
        "   ❌ Noi NON cediamo Kulenovic"
    )

    for carta in carte_idonee:

        nome_carta = (
            carta.get(
                "name"
            )
            or carta.get(
                "slug"
            )
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ Noi riceviamo: "
            f"{nome_carta}"
        )

    print(
        f"   💰 Noi paghiamo: "
        f"€{pagamento_euro:.2f}"
    )

    print("")

    if DRY_RUN:

        print(
            "🟡 DRY RUN ATTIVO:"
        )

        print(
            "   Nessuna controproposta "
            "è stata inviata."
        )

    print(
        "----------------------------------------"
    )

    print("")


# ============================================================
# MONITORAGGIO
# ============================================================

def monitor_offerte():

    print(
        "🤖 BOT SORARE AVVIATO"
    )

    print(
        "🟡 MODALITÀ DRY RUN ATTIVA"
    )

    print(
        "⚠️ Nessun rifiuto e nessuna "
        "controproposta verranno eseguiti."
    )

    print("")

    print(
        "💰 REGOLA PREZZO: "
        "€0,30 - €0,80"
    )

    print(
        "💰 PAGAMENTO: "
        "€0,20 per ogni carta idonea"
    )

    stampa_campionati_coperti()

    print("")

    firma_ok = test_firma_stark()

    if not firma_ok:

        print(
            "⚠️ TEST FIRMA STARK "
            "NON SUPERATO."
        )

        print(
            "⚠️ Il bot continua in DRY RUN."
        )

        print(
            "⚠️ Nessuna operazione reale "
            "verrà eseguita."
        )

    else:

        print(
            "🟢 TEST FIRMA STARK SUPERATO."
        )

        print(
            "🟡 Il bot rimane comunque "
            "in DRY RUN."
        )

    print("")

    # ========================================================
    # AUTENTICAZIONE
    # ========================================================

    if not verifica_account():

        print(
            "❌ Impossibile autenticarsi "
            "a Sorare."
        )

        return

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            print(
                "🔎 Controllo offerte..."
            )

            offerte = (
                recupera_offerte()
            )

            print(
                f"📨 Offerte pending ricevute: "
                f"{len(offerte)}"
            )

            for offerta in offerte:

                elabora_offerta(
                    offerta
                )

        except Exception as e:

            print(
                f"⚠️ Errore nel ciclo: "
                f"{e}"
            )

        time.sleep(
            10
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    global monitoraggio_avviato

    with lock_avvio:

        if not monitoraggio_avviato:

            monitoraggio_avviato = True

            thread = threading.Thread(

                target=monitor_offerte,

                daemon=True,
            )

            thread.start()

            return (
                "Bot Sorare avviato "
                "in modalità DRY RUN."
            )

    return (
        "Bot Sorare già attivo."
    )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000,
        )
    )

    app.run(

        host="0.0.0.0",

        port=port,
    )
