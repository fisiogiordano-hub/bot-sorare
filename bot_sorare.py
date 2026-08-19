import os
import time
import threading
import hashlib
import importlib
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set

import requests
from flask import Flask


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIGURAZIONE
# ============================================================

SORARE_API_URL = "https://api.sorare.com/graphql"

SORARE_TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    "",
).strip()

SORARE_JWT_AUD = os.getenv(
    "SORARE_JWT_AUD",
    "",
).strip()

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    "",
).strip()

SORARE_STARK_PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    "",
).strip()


# ============================================================
# SICUREZZA
# ============================================================

# IMPORTANTE:
#
# Il bot è SEMPRE in DRY RUN.
#
# NON vengono mai eseguite:
#
# - rejectTokenOffer
# - counterTokenOffer
# - acceptTokenOffer
# - transazioni
# - mutation Sorare
#
DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

PREZZO_MINIMO_CENTESIMI = 30
PREZZO_MASSIMO_CENTESIMI = 80

PAGAMENTO_PER_CARTA_CENTESIMI = 20

INTERVALLO_CONTROLLO_SECONDI = 10

HTTP_TIMEOUT_SECONDI = 30

MAX_OFFERTE_PER_RICHIESTA = 50

MAX_RETRY_HTTP = 2


# ============================================================
# CAMPIONATI COPERTI
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


NUMERO_CAMPIONATI = len(CAMPIONATI_COPERTI)


# ============================================================
# KULENOVIC
# ============================================================

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET_ID = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATO GLOBALE
# ============================================================

offerte_gia_analizzate: Set[str] = set()

monitoraggio_avviato = False

monitor_thread: Optional[threading.Thread] = None

stop_event = threading.Event()

lock_avvio = threading.Lock()

session = requests.Session()


# ============================================================
# NORMALIZZAZIONE
# ============================================================

def normalizza_slug(valore: Any) -> str:
    if valore is None:
        return ""

    return str(valore).strip().lower()


# ============================================================
# TROVA CAMPIONATO
# ============================================================

def trova_campionato_coperto(slug: Any) -> Optional[str]:

    slug_normalizzato = normalizza_slug(slug)

    if not slug_normalizzato:
        return None

    for dati in CAMPIONATI_COPERTI.values():

        alias = dati.get("alias", set())

        if slug_normalizzato in alias:
            return dati.get("nome")

    return None


# ============================================================
# STAMPA CAMPIONATI
# ============================================================

def stampa_campionati_coperti() -> None:

    print("🏆 REGOLA CAMPIONATI:")
    print(
        f"   {NUMERO_CAMPIONATI} campionati coperti."
    )

    for dati in CAMPIONATI_COPERTI.values():
        print(
            f"   • {dati['nome']}"
        )


# ============================================================
# TEST CONFIGURAZIONE
# ============================================================

def verifica_configurazione() -> bool:

    print("")
    print("========================================")
    print("🔧 VERIFICA CONFIGURAZIONE")
    print("========================================")

    ok = True

    if SORARE_TOKEN:
        print("✅ SORARE_JWT_TOKEN presente.")
    else:
        print(
            "❌ SORARE_JWT_TOKEN NON configurato."
        )
        ok = False

    if SORARE_JWT_AUD:
        print("✅ SORARE_JWT_AUD presente.")
    else:
        print(
            "⚠️ SORARE_JWT_AUD non configurato."
        )

    if KULENOVIC_ID:
        print("✅ KULENOVIC_ID presente.")
    else:
        print(
            "⚠️ KULENOVIC_ID non configurato."
        )

    if SORARE_STARK_PRIVATE_KEY:
        print(
            "✅ SORARE_STARK_PRIVATE_KEY presente."
        )
    else:
        print(
            "⚠️ SORARE_STARK_PRIVATE_KEY non configurata."
        )

    print("")
    print(
        f"🟡 DRY_RUN = {DRY_RUN}"
    )

    if not DRY_RUN:
        print(
            "❌ BLOCCO DI SICUREZZA: "
            "DRY_RUN deve essere True."
        )
        ok = False

    print("========================================")

    return ok


# ============================================================
# TEST FIRMA STARK
# ============================================================

def test_firma_stark() -> bool:

    print("")
    print("========================================")
    print("🔐 TEST LOCALE FIRMA STARK")
    print("========================================")

    if not SORARE_STARK_PRIVATE_KEY:

        print(
            "⚠️ SORARE_STARK_PRIVATE_KEY "
            "non configurata."
        )

        print(
            "🟡 Test Stark saltato."
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
            16,
        )

    except ValueError:

        print(
            "❌ La chiave non è "
            "esadecimale valida."
        )

        return False

    if private_key_int <= 0:

        print(
            "❌ La chiave privata "
            "non è valida."
        )

        return False

    print(
        "✅ Formato esadecimale verificato."
    )

    # --------------------------------------------------------
    # IMPORT COMPATIBILE
    # --------------------------------------------------------

    try:

        modulo = importlib.import_module(
            "starknet_py.net.signer.stark_curve"
        )

    except Exception as e:

        print(
            "⚠️ starknet-py non disponibile "
            "o API incompatibile."
        )

        print(
            f"   Dettaglio: {e}"
        )

        print(
            "🟡 Il bot continua in DRY RUN."
        )

        return False

    PrivateKey = getattr(
        modulo,
        "PrivateKey",
        None,
    )

    message_signature = getattr(
        modulo,
        "message_signature",
        None,
    )

    if PrivateKey is None:

        print(
            "⚠️ PrivateKey non disponibile "
            "nella versione installata."
        )

        return False

    if message_signature is None:

        print(
            "⚠️ message_signature non disponibile "
            "nella versione installata."
        )

        print(
            "🟡 Test firma saltato."
        )

        return False

    print(
        "✅ API Starknet importate."
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
            "la private key Stark."
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
            "❌ Impossibile derivare "
            "la public key."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Public key Stark derivata."
    )

    # --------------------------------------------------------
    # HASH
    # --------------------------------------------------------

    try:

        messaggio = (
            "SORARE_LOCAL_SIGNATURE_TEST"
        )

        digest = hashlib.sha256(
            messaggio.encode("utf-8")
        ).digest()

        messaggio_hash = int.from_bytes(
            digest,
            byteorder="big",
        )

        stark_field_prime = (
            (2 ** 251)
            + (17 * (2 ** 192))
            + 1
        )

        messaggio_hash %= (
            stark_field_prime
        )

    except Exception as e:

        print(
            "❌ Impossibile creare "
            "l'hash di test."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Hash di test preparato."
    )

    # --------------------------------------------------------
    # FIRMA
    # --------------------------------------------------------

    try:

        firma = message_signature(
            private_key,
            messaggio_hash,
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

    try:

        r = firma[0]
        s = firma[1]

    except Exception as e:

        print(
            "❌ Struttura firma inattesa."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    if r is None or s is None:

        print(
            "❌ Firma priva di r/s."
        )

        return False

    print(
        "✅ Firma Stark generata."
    )

    # --------------------------------------------------------
    # VERIFICA
    # --------------------------------------------------------

    try:

        verificata = public_key.verify(
            messaggio_hash,
            firma,
        )

    except Exception as e:

        print(
            "❌ Verifica locale fallita."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    if not verificata:

        print(
            "❌ FIRMA NON VERIFICATA."
        )

        return False

    print(
        "✅ FIRMA VERIFICATA LOCALMENTE."
    )

    print(
        "🟢 Test Stark superato."
    )

    print(
        "🟡 Nessuna mutation Sorare eseguita."
    )

    print(
        "🟡 Nessuna transazione eseguita."
    )

    print(
        "========================================"
    )

    return True


# ============================================================
# HEADERS
# ============================================================

def crea_headers() -> Dict[str, str]:

    if not SORARE_TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato."
        )

    token = SORARE_TOKEN

    if not token.lower().startswith("bearer "):

        token = f"Bearer {token}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-Dry-Run-Bot/1.0",
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
    query: str,
    variables: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:

    payload = {
        "query": query,
        "variables": variables or {},
    }

    for tentativo in range(
        MAX_RETRY_HTTP + 1
    ):

        try:

            response = session.post(
                SORARE_API_URL,
                json=payload,
                headers=crea_headers(),
                timeout=HTTP_TIMEOUT_SECONDI,
            )

            print(
                f"🌐 Sorare HTTP: "
                f"{response.status_code}"
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                if tentativo >= MAX_RETRY_HTTP:

                    print(
                        "❌ Rate limit persistente."
                    )

                    return None

                attesa = 2 ** tentativo

                print(
                    f"⏳ Rate limit. "
                    f"Attendo {attesa}s..."
                )

                time.sleep(attesa)

                continue

            # ------------------------------------------------
            # ERRORI SERVER
            # ------------------------------------------------

            if response.status_code >= 500:

                if tentativo >= MAX_RETRY_HTTP:

                    print(
                        "❌ Errore server Sorare."
                    )

                    print(
                        response.text[:2000]
                    )

                    return None

                attesa = 2 ** tentativo

                print(
                    f"⏳ Errore server. "
                    f"Retry tra {attesa}s..."
                )

                time.sleep(attesa)

                continue

            # ------------------------------------------------
            # HTTP NON VALIDO
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    "❌ Risposta HTTP non valida:"
                )

                print(
                    response.text[:3000]
                )

                return None

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

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

            if not isinstance(
                risultato,
                dict,
            ):

                print(
                    "❌ Risposta GraphQL inattesa."
                )

                return None

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            errori = risultato.get(
                "errors"
            )

            if errori:

                print(
                    "❌ Errori GraphQL:"
                )

                for errore in errori:

                    if isinstance(
                        errore,
                        dict,
                    ):

                        messaggio = errore.get(
                            "message",
                            "Errore sconosciuto",
                        )

                    else:

                        messaggio = str(
                            errore
                        )

                    print(
                        f"   - {messaggio}"
                    )

                return None

            return risultato

        except requests.RequestException as e:

            if tentativo >= MAX_RETRY_HTTP:

                print(
                    f"❌ Errore HTTP Sorare: {e}"
                )

                return None

            attesa = 2 ** tentativo

            print(
                f"⏳ Errore HTTP. "
                f"Retry tra {attesa}s..."
            )

            time.sleep(attesa)

        except Exception as e:

            print(
                f"❌ Errore richiesta Sorare: {e}"
            )

            return None

    return None


# ============================================================
# ACCOUNT
# ============================================================

def verifica_account() -> bool:

    query = """
    query CurrentUserTest {
        currentUser {
            slug
            nickname
        }
    }
    """

    risultato = esegui_query(query)

    if not risultato:
        return False

    data = risultato.get("data")

    if not isinstance(data, dict):
        print(
            "❌ Campo data assente."
        )
        return False

    user = data.get(
        "currentUser"
    )

    if not isinstance(user, dict):

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

def recupera_offerte() -> List[Dict[str, Any]]:

    query = """
    query PendingOffers($first: Int!) {
        currentUser {
            pendingTokenOffersReceived(
                first: $first
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
        query,
        {
            "first": MAX_OFFERTE_PER_RICHIESTA,
        },
    )

    if not risultato:
        return []

    data = risultato.get("data")

    if not isinstance(data, dict):
        return []

    user = data.get(
        "currentUser"
    )

    if not isinstance(user, dict):
        return []

    connessione = user.get(
        "pendingTokenOffersReceived"
    )

    if not isinstance(
        connessione,
        dict,
    ):

        return []

    nodes = connessione.get(
        "nodes"
    )

    if not isinstance(
        nodes,
        list,
    ):

        return []

    return [
        offerta
        for offerta in nodes
        if isinstance(offerta, dict)
    ]


# ============================================================
# ESTRAI ASSET ID UNICI
# ============================================================

def estrai_asset_ids(
    carte: List[Dict[str, Any]]
) -> List[str]:

    risultati = []

    gia_presenti = set()

    for carta in carte:

        if not isinstance(
            carta,
            dict,
        ):
            continue

        asset_id = str(
            carta.get(
                "assetId"
            )
            or ""
        ).strip()

        if not asset_id:
            continue

        chiave = asset_id.lower()

        if chiave in gia_presenti:
            continue

        gia_presenti.add(chiave)

        risultati.append(
            asset_id
        )

    return risultati


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(
    asset_ids: List[str]
) -> List[Dict[str, Any]]:

    asset_ids = estrai_asset_ids(
        [
            {
                "assetId": asset_id
            }
            for asset_id in asset_ids
        ]
    )

    if not asset_ids:
        return []

    query = """
    query CardDetails($assetIds: [String!]) {

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
        },
    )

    if not risultato:
        return []

    data = risultato.get(
        "data"
    )

    if not isinstance(
        data,
        dict,
    ):
        return []

    carte = data.get(
        "anyCards"
    )

    if not isinstance(
        carte,
        list,
    ):
        return []

    return [
        carta
        for carta in carte
        if isinstance(carta, dict)
    ]


# ============================================================
# EUR CENTS
# ============================================================

def leggi_eur_cents(
    amounts: Any
) -> Optional[int]:

    if not isinstance(
        amounts,
        dict,
    ):
        return None

    valore = amounts.get(
        "eurCents"
    )

    if valore is None:
        return None

    try:

        valore_int = int(
            valore
        )

    except (
        ValueError,
        TypeError,
    ):

        return None

    if valore_int <= 0:
        return None

    return valore_int


# ============================================================
# PREZZO DA LIVE SALE
# ============================================================

def prezzo_da_live_sale(
    carta: Any
) -> Optional[Decimal]:

    if not isinstance(
        carta,
        dict,
    ):
        return None

    offerta = carta.get(
        "liveSingleSaleOffer"
    )

    if not isinstance(
        offerta,
        dict,
    ):
        return None

    receiver_side = offerta.get(
        "receiverSide"
    )

    if not isinstance(
        receiver_side,
        dict,
    ):
        return None

    amounts = receiver_side.get(
        "amounts"
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
# PUBLIC MIN PRICE
# ============================================================

def prezzo_da_public_min_price(
    carta: Any
) -> Optional[Decimal]:

    if not isinstance(
        carta,
        dict,
    ):
        return None

    public_min = carta.get(
        "publicMinPrices"
    )

    # Sorare può restituire:
    #
    #   dict
    #
    # oppure:
    #
    #   list
    #
    if isinstance(
        public_min,
        dict,
    ):

        eur_cents = leggi_eur_cents(
            public_min
        )

        if eur_cents is not None:

            return (
                Decimal(eur_cents)
                / Decimal("100")
            )

    elif isinstance(
        public_min,
        list,
    ):

        valori = []

        for elemento in public_min:

            eur_cents = leggi_eur_cents(
                elemento
            )

            if eur_cents is not None:

                valori.append(
                    eur_cents
                )

        if valori:

            return (
                Decimal(min(valori))
                / Decimal("100")
            )

    return None


# ============================================================
# PREZZO FLOOR
# ============================================================

def recupera_prezzo_floor(
    carta: Dict[str, Any]
) -> Optional[Decimal]:

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

    valori: List[Decimal] = []

    # --------------------------------------------------------
    # STAGIONE CORRENTE
    # --------------------------------------------------------

    lowest_price_card = carta.get(
        "lowestPriceCard"
    )

    if isinstance(
        lowest_price_card,
        dict,
    ):

        lowest_slug = str(
            lowest_price_card.get(
                "slug"
            )
            or ""
        ).strip()

        if lowest_slug:

            print(
                f"      🎯 Carta floor: "
                f"{lowest_slug}"
            )

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
                f"      💰 Live sale: "
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
                f"      💰 Public min: "
                f"€{prezzo_public:.2f}"
            )

    # --------------------------------------------------------
    # QUALSIASI STAGIONE
    # --------------------------------------------------------

    lowest_any_season = carta.get(
        "lowestPriceCardAnySeason"
    )

    if isinstance(
        lowest_any_season,
        dict,
    ):

        prezzo_live = (
            prezzo_da_live_sale(
                lowest_any_season
            )
        )

        if prezzo_live is not None:

            valori.append(
                prezzo_live
            )

            print(
                f"      💰 Live sale "
                f"(any season): "
                f"€{prezzo_live:.2f}"
            )

        prezzo_public = (
            prezzo_da_public_min_price(
                lowest_any_season
            )
        )

        if prezzo_public is not None:

            valori.append(
                prezzo_public
            )

            print(
                f"      💰 Public min "
                f"(any season): "
                f"€{prezzo_public:.2f}"
            )

    # --------------------------------------------------------
    # FALLBACK CARTA ORIGINALE
    # --------------------------------------------------------

    if not valori:

        prezzo_live = (
            prezzo_da_live_sale(
                carta
            )
        )

        if prezzo_live is not None:

            valori.append(
                prezzo_live
            )

        prezzo_public = (
            prezzo_da_public_min_price(
                carta
            )
        )

        if prezzo_public is not None:

            valori.append(
                prezzo_public
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
# CONTROLLO SQUADRA / CAMPIONATO
# ============================================================

def controlla_squadra_e_campionato(
    carta: Dict[str, Any]
) -> bool:

    player = carta.get(
        "anyPlayer"
    )

    if not isinstance(
        player,
        dict,
    ):

        print(
            "      🔴 Giocatore non disponibile."
        )

        return False

    player_name = (
        player.get("displayName")
        or player.get("slug")
        or carta.get("name")
        or "Giocatore sconosciuto"
    )

    active_club = player.get(
        "activeClub"
    )

    # --------------------------------------------------------
    # NESSUNA SQUADRA ATTIVA
    # --------------------------------------------------------

    if not isinstance(
        active_club,
        dict,
    ):

        print(
            "      🏟️ Squadra attiva: NESSUNA"
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

    club_name = str(
        active_club.get("name")
        or active_club.get("slug")
        or ""
    ).strip()

    club_slug = normalizza_slug(
        active_club.get("slug")
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

    competizioni = active_club.get(
        "activeCompetitions"
    )

    if not isinstance(
        competizioni,
        list,
    ):

        competizioni = []

    if not competizioni:

        print(
            "      🔴 Nessuna competizione "
            "attiva sulla squadra."
        )

        return False

    print(
        "      🏆 COMPETIZIONI ATTIVE:"
    )

    campionati_trovati = []

    for competizione in competizioni:

        if not isinstance(
            competizione,
            dict,
        ):
            continue

        slug = normalizza_slug(
            competizione.get("slug")
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

    print(
        "      🔴 CAMPIONATO NON COPERTO"
    )

    return False


# ============================================================
# ANALISI CARTA
# ============================================================

def analizza_carta(
    carta: Dict[str, Any]
) -> bool:

    if not isinstance(
        carta,
        dict,
    ):

        return False

    asset_id = carta.get(
        "assetId"
    )

    slug = carta.get(
        "slug"
    )

    nome = (
        carta.get("name")
        or slug
        or asset_id
        or "Carta sconosciuta"
    )

    rarita = str(
        carta.get("rarityTyped")
        or ""
    ).strip().upper()

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    prezzo = recupera_prezzo_floor(
        carta
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
        prezzo is not None
        and prezzo >= prezzo_minimo
        and prezzo <= prezzo_massimo
    )

    # --------------------------------------------------------
    # RARITÀ
    # --------------------------------------------------------

    rarita_ok = (
        rarita == "LIMITED"
    )

    # --------------------------------------------------------
    # CAMPIONATO
    # --------------------------------------------------------

    campionato_ok = (
        controlla_squadra_e_campionato(
            carta
        )
    )

    idonea = (
        prezzo_ok
        and rarita_ok
        and campionato_ok
    )

    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    print("")
    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: "
        f"{asset_id or 'N/D'}"
    )

    print(
        f"      Slug: "
        f"{slug or 'N/D'}"
    )

    print(
        f"      Rarità: "
        f"{rarita or 'N/D'}"
    )

    if prezzo is None:

        print(
            "      🔴 Prezzo NON verificabile"
        )

    else:

        print(
            f"      Prezzo floor: "
            f"€{prezzo:.2f}"
        )

        if prezzo < prezzo_minimo:

            print(
                "      🔴 Prezzo sotto €0,30"
            )

        elif prezzo > prezzo_massimo:

            print(
                "      🔴 Prezzo sopra €0,80"
            )

        else:

            print(
                "      🟢 Prezzo tra €0,30 e €0,80"
            )

    if rarita_ok:

        print(
            "      🟢 Rarità LIMITED"
        )

    else:

        print(
            "      🔴 Rarità NON valida"
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
    carte_richieste: List[Dict[str, Any]]
) -> bool:

    print("")
    print(
        "🔎 CARTE RICHIESTE DAL MANAGER:"
    )

    configurato = (
        KULENOVIC_ID.strip()
        if KULENOVIC_ID
        else ""
    )

    for carta in carte_richieste:

        if not isinstance(
            carta,
            dict,
        ):
            continue

        asset_id = str(
            carta.get("assetId")
            or ""
        ).strip()

        slug = str(
            carta.get("slug")
            or ""
        ).strip()

        collection = str(
            carta.get("collection")
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

        asset_lower = asset_id.lower()
        slug_lower = slug.lower()

        match_configurazione = (
            bool(configurato)
            and (
                asset_lower
                == configurato.lower()
                or slug_lower
                == configurato.lower()
            )
        )

        match_slug = (
            slug_lower
            == KULENOVIC_SLUG.lower()
        )

        match_asset = (
            asset_lower
            == KULENOVIC_ASSET_ID.lower()
        )

        if (
            match_configurazione
            or match_slug
            or match_asset
        ):

            print(
                "   🎯 KULENOVIC RICONOSCIUTO"
            )

            return True

    print(
        "ℹ️ Kulenovic non riconosciuto."
    )

    return False


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(
    offerta: Dict[str, Any]
) -> None:

    if not isinstance(
        offerta,
        dict,
    ):
        return

    offerta_id = str(
        offerta.get("id")
        or ""
    ).strip()

    if not offerta_id:
        return

    # --------------------------------------------------------
    # NON RIELABORARE
    # --------------------------------------------------------

    if offerta_id in offerte_gia_analizzate:
        return

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
        f"{offerta.get('status') or 'N/D'}"
    )

    sender = offerta.get(
        "sender"
    )

    if not isinstance(
        sender,
        dict,
    ):
        sender = {}

    nickname = (
        sender.get("nickname")
        or sender.get("slug")
        or "Sconosciuto"
    )

    print(
        f"👤 Manager: {nickname}"
    )

    sender_side = offerta.get(
        "senderSide"
    )

    if not isinstance(
        sender_side,
        dict,
    ):
        sender_side = {}

    receiver_side = offerta.get(
        "receiverSide"
    )

    if not isinstance(
        receiver_side,
        dict,
    ):
        receiver_side = {}

    carte_offerte = (
        sender_side.get("anyCards")
        or []
    )

    carte_che_diamo = (
        receiver_side.get("anyCards")
        or []
    )

    if not isinstance(
        carte_offerte,
        list,
    ):
        carte_offerte = []

    if not isinstance(
        carte_che_diamo,
        list,
    ):
        carte_che_diamo = []

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

    asset_ids = estrai_asset_ids(
        carte_offerte
    )

    # --------------------------------------------------------
    # NESSUNA CARTA
    # --------------------------------------------------------

    if not asset_ids:

        print("")
        print(
            "🔴 DECISIONE TEORICA: RIFIUTARE"
        )

        print(
            "   Motivo: nessuna carta ricevuta."
        )

        print(
            "🟡 DRY RUN: nessun rifiuto eseguito."
        )

        print(
            "----------------------------------------"
        )

        # Ora la lavorazione è conclusa.
        offerte_gia_analizzate.add(
            offerta_id
        )

        return

    # --------------------------------------------------------
    # DETTAGLI
    # --------------------------------------------------------

    dettagli = recupera_dettagli_carte(
        asset_ids
    )

    if not dettagli:

        print(
            "⚠️ Impossibile recuperare "
            "i dettagli delle carte."
        )

        print(
            "⚠️ Offerta NON marcata come analizzata."
        )

        print(
            "----------------------------------------"
        )

        return

    # --------------------------------------------------------
    # MAPPA DETTAGLI
    # --------------------------------------------------------

    dettagli_per_asset = {}

    for carta in dettagli:

        asset_id = str(
            carta.get("assetId")
            or ""
        ).strip()

        if asset_id:

            dettagli_per_asset[
                asset_id.lower()
            ] = carta

    carte_idonee = []

    carte_mancanti = []

    print("")
    print(
        "🔎 ANALISI DELLE CARTE RICEVUTE:"
    )

    # --------------------------------------------------------
    # IMPORTANTE:
    #
    # Analizziamo tutte le carte richieste.
    #
    # Se Sorare non restituisce il dettaglio di una carta,
    # quella carta NON può essere considerata idonea.
    # --------------------------------------------------------

    for asset_id in asset_ids:

        carta = dettagli_per_asset.get(
            asset_id.lower()
        )

        if carta is None:

            print("")
            print(
                f"   ❌ Dettagli mancanti per "
                f"asset {asset_id}"
            )

            carte_mancanti.append(
                asset_id
            )

            continue

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

    # --------------------------------------------------------
    # NESSUNA IDONEA
    # --------------------------------------------------------

    if numero_idonee == 0:

        print("")
        print(
            "🔴 DECISIONE TEORICA: "
            "RIFIUTARE L'OFFERTA"
        )

        print(
            "   Motivo: nessuna carta ricevuta "
            "è idonea."
        )

        print(
            "🟡 DRY RUN: nessun rifiuto eseguito."
        )

        print(
            "----------------------------------------"
        )

        offerte_gia_analizzate.add(
            offerta_id
        )

        return

    # --------------------------------------------------------
    # PAGAMENTO
    # --------------------------------------------------------

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
        "🟢 DECISIONE TEORICA: "
        "CONTROPROPOSTA"
    )

    print("")

    print(
        "📤 DALLA PROPOSTA VERREBBE RIMOSSO:"
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
        "🗑️ VERREBBERO ELIMINATE "
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
        "📥 RIMARREBBERO SOLO "
        "LE CARTE IDONEE:"
    )

    for carta in carte_idonee:

        nome_carta = (
            carta.get("name")
            or carta.get("slug")
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ {nome_carta}"
        )

    print("")

    print(
        f"💰 PAGAMENTO TEORICO: "
        f"€{pagamento_euro:.2f}"
    )

    print(
        f"   {numero_idonee} × €0,20"
    )

    print("")
    print(
        "📋 CONTROPROPOSTA TEORICA:"
    )

    if kulenovic_presente:

        print(
            "   ❌ Noi NON cediamo Kulenovic"
        )

    else:

        print(
            "   ℹ️ Kulenovic non era richiesto."
        )

    for carta in carte_idonee:

        nome_carta = (
            carta.get("name")
            or carta.get("slug")
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ Noi riceviamo: "
            f"{nome_carta}"
        )

    print(
        f"   💰 Noi pagheremmo: "
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
            "   Nessun rifiuto è stato eseguito."
        )

        print(
            "   Nessuna mutation Sorare "
            "è stata eseguita."
        )

    print(
        "----------------------------------------"
    )

    print("")

    # --------------------------------------------------------
    # SOLO DOPO ELABORAZIONE COMPLETA
    # --------------------------------------------------------

    offerte_gia_analizzate.add(
        offerta_id
    )


# ============================================================
# MONITOR
# ============================================================

def monitor_offerte() -> None:

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

    print("")

    stampa_campionati_coperti()

    print("")

    if not verifica_configurazione():

        print(
            "❌ Configurazione non valida."
        )

        print(
            "❌ Monitoraggio NON avviato."
        )

        return

    # --------------------------------------------------------
    # TEST STARK
    # --------------------------------------------------------

    firma_ok = test_firma_stark()

    if firma_ok:

        print(
            "🟢 TEST FIRMA STARK SUPERATO."
        )

    else:

        print(
            "🟡 TEST FIRMA STARK NON SUPERATO "
            "O NON DISPONIBILE."
        )

    print(
        "🟡 Il bot rimane comunque in DRY RUN."
    )

    print("")

    # --------------------------------------------------------
    # AUTENTICAZIONE
    # --------------------------------------------------------

    if not verifica_account():

        print(
            "❌ Impossibile autenticarsi "
            "a Sorare."
        )

        print(
            "❌ Monitoraggio terminato."
        )

        return

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while not stop_event.is_set():

        try:

            print(
                "🔎 Controllo offerte..."
            )

            offerte = recupera_offerte()

            print(
                f"📨 Offerte pending ricevute: "
                f"{len(offerte)}"
            )

            for offerta in offerte:

                if stop_event.is_set():
                    break

                try:

                    elabora_offerta(
                        offerta
                    )

                except Exception as e:

                    print(
                        "❌ Errore elaborando "
                        "una singola offerta:"
                    )

                    print(
                        f"   {e}"
                    )

            # ------------------------------------------------
            # ATTESA INTERRUTTIBILE
            # ------------------------------------------------

            stop_event.wait(
                INTERVALLO_CONTROLLO_SECONDI
            )

        except Exception as e:

            print(
                "⚠️ Errore nel ciclo monitor:"
            )

            print(
                f"   {e}"
            )

            stop_event.wait(
                INTERVALLO_CONTROLLO_SECONDI
            )

    print(
        "🛑 Monitor Sorare terminato."
    )


# ============================================================
# AVVIO MONITOR
# ============================================================

def avvia_monitoraggio() -> bool:

    global monitoraggio_avviato
    global monitor_thread

    with lock_avvio:

        # ----------------------------------------------------
        # THREAD GIÀ ATTIVO
        # ----------------------------------------------------

        if (
            monitor_thread is not None
            and monitor_thread.is_alive()
        ):

            monitoraggio_avviato = True

            return False

        # ----------------------------------------------------
        # RESET EVENT
        # ----------------------------------------------------

        stop_event.clear()

        monitor_thread = threading.Thread(
            target=monitor_offerte,
            name="sorare-monitor",
            daemon=True,
        )

        monitor_thread.start()

        monitoraggio_avviato = True

        return True


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/")
def home():

    avviato = avvia_monitoraggio()

    if avviato:

        return (
            "Bot Sorare avviato "
            "in modalità DRY RUN.",
            200,
        )

    return (
        "Bot Sorare già attivo.",
        200,
    )


@app.route("/health")
def health():

    thread_attivo = (
        monitor_thread is not None
        and monitor_thread.is_alive()
    )

    return {
        "status": "ok",
        "dry_run": DRY_RUN,
        "monitor_attivo": thread_attivo,
    }, 200


# ============================================================
# STOP
# ============================================================

def ferma_monitoraggio() -> None:

    global monitoraggio_avviato

    with lock_avvio:

        stop_event.set()

        monitoraggio_avviato = False


# ============================================================
# AVVIO LOCALE
# ============================================================

if __name__ == "__main__":

    port_string = os.environ.get(
        "PORT",
        "5000",
    )

    try:

        port = int(
            port_string
        )

    except ValueError:

        port = 5000

    print(
        f"🚀 Flask in ascolto sulla porta {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
