import os
import time
import threading
import requests

from decimal import Decimal
from flask import Flask, jsonify


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

# NON MODIFICARE.
#
# Il bot analizza le offerte ma NON:
#
# - rifiuta offerte
# - invia controproposte
# - modifica offerte
# - esegue transazioni
#
# Per sicurezza il valore è forzato a True.

DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

PREZZO_MINIMO_CENTESIMI = 30
PREZZO_MASSIMO_CENTESIMI = 80

PAGAMENTO_PER_CARTA_CENTESIMI = 20

INTERVALLO_CONTROLLO = 10

TIMEOUT_HTTP = 30


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
# CAMPIONATI COPERTI
# ============================================================

CAMPIONATI_COPERTI = {

    "english-league": {
        "nome": "English League",
        "alias": {
            "english-league",
            "premier-league-eng",
            "premier-league",
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
            "supersport-hnl",
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


NUMERO_CAMPIONATI = len(
    CAMPIONATI_COPERTI
)


# ============================================================
# STATO
# ============================================================

offerte_gia_analizzate = set()

offerte_in_elaborazione = set()

stato_lock = threading.Lock()

monitoraggio_avviato = False

monitor_thread = None


# ============================================================
# NORMALIZZAZIONE
# ============================================================

def normalizza_slug(valore):

    if valor_non_valido(valore):
        return ""

    return (
        str(valore)
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )


def valor_non_valido(valore):

    return (
        valore is None
        or str(valore).strip() == ""
    )


# ============================================================
# CAMPIONATO
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

        alias_normalizzati = {
            normalizza_slug(x)
            for x in alias
        }

        if slug_normalizzato in alias_normalizzati:

            return dati.get(
                "nome"
            )

    return None


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
# CONFIGURAZIONE
# ============================================================

def verifica_configurazione():

    print("")
    print("========================================")
    print("🔧 VERIFICA CONFIGURAZIONE")
    print("========================================")

    configurazioni = [
        (
            "SORARE_JWT_TOKEN",
            SORARE_TOKEN,
        ),
        (
            "SORARE_JWT_AUD",
            SORARE_JWT_AUD,
        ),
        (
            "KULENOVIC_ID",
            KULENOVIC_ID,
        ),
        (
            "SORARE_STARK_PRIVATE_KEY",
            SORARE_STARK_PRIVATE_KEY,
        ),
    ]

    tutto_ok = True

    for nome, valore in configurazioni:

        if valore:

            print(
                f"✅ {nome} presente."
            )

        else:

            print(
                f"❌ {nome} NON presente."
            )

            tutto_ok = False

    print(
        f"🟡 DRY_RUN = {DRY_RUN}"
    )

    print(
        "========================================"
    )

    return tutto_ok


# ============================================================
# TEST CHIAVE STARK
# ============================================================
#
# NON proviamo più a importare:
#
# starknet_py.net.signer.stark_curve
#
# perché è un percorso interno/vecchio e nel tuo ambiente
# non esiste.
#
# Il bot NON ha bisogno di questo test per leggere le offerte.
#
# Verifichiamo soltanto che la variabile contenga una chiave
# esadecimale plausibile.
# ============================================================

def test_firma_stark():

    print("")
    print("========================================")
    print("🔐 VERIFICA CHIAVE STARK")
    print("========================================")

    if not SORARE_STARK_PRIVATE_KEY:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY non configurata."
        )

        return False

    chiave = (
        SORARE_STARK_PRIVATE_KEY
        .strip()
    )

    print(
        "✅ SORARE_STARK_PRIVATE_KEY presente."
    )

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
            "❌ La chiave Stark NON è "
            "un valore esadecimale valido."
        )

        return False

    if private_key_int <= 0:

        print(
            "❌ La chiave Stark non è valida."
        )

        return False

    print(
        "✅ Formato esadecimale verificato."
    )

    print(
        "🟡 Test crittografico locale saltato."
    )

    print(
        "🟡 Nessun modulo starknet-py interno "
        "viene importato."
    )

    print(
        "🟢 Il controllo della chiave non "
        "blocca il monitoraggio."
    )

    print(
        "🟡 DRY RUN: nessuna operazione reale."
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

        token = (
            f"Bearer {token}"
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-DryRun-Bot/1.0",
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
    variables=None,
    tentativi=3,
):

    payload = {
        "query": query,
        "variables": variables or {},
    }

    ultimo_errore = None

    for tentativo in range(
        1,
        tentativi + 1
    ):

        try:

            response = requests.post(
                SORARE_API_URL,
                json=payload,
                headers=crea_headers(),
                timeout=TIMEOUT_HTTP,
            )

            print(
                f"🌐 Sorare HTTP: "
                f"{response.status_code}"
            )

            if response.status_code == 429:

                print(
                    "⚠️ Rate limit Sorare."
                )

                if tentativo < tentativi:

                    time.sleep(
                        tentativo * 3
                    )

                    continue

                return None

            if response.status_code != 200:

                print(
                    "❌ Risposta HTTP non valida:"
                )

                print(
                    response.text[:2000]
                )

                ultimo_errore = (
                    f"HTTP {response.status_code}"
                )

                if tentativo < tentativi:

                    time.sleep(
                        tentativo
                    )

                    continue

                return None

            try:

                risultato = response.json()

            except ValueError:

                print(
                    "❌ Risposta Sorare non JSON."
                )

                print(
                    response.text[:2000]
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

                    if isinstance(
                        errore,
                        dict
                    ):

                        print(
                            "- "
                            + str(
                                errore.get(
                                    "message",
                                    "Errore sconosciuto",
                                )
                            )
                        )

                    else:

                        print(
                            "- "
                            + str(errore)
                        )

                return None

            return risultato

        except requests.RequestException as e:

            ultimo_errore = e

            print(
                f"⚠️ Errore HTTP "
                f"(tentativo {tentativo}/"
                f"{tentativi}): {e}"
            )

            if tentativo < tentativi:

                time.sleep(
                    tentativo
                )

        except Exception as e:

            print(
                f"❌ Errore richiesta Sorare: {e}"
            )

            return None

    print(
        f"❌ Richiesta fallita: "
        f"{ultimo_errore}"
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
            "❌ Sorare non ha restituito "
            "currentUser."
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

        return None

    user = (
        risultato
        .get("data", {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ currentUser assente."
        )

        return None

    connessione = (
        user.get(
            "pendingTokenOffersReceived"
        )
        or {}
    )

    nodes = (
        connessione.get(
            "nodes"
        )
    )

    if nodes is None:

        return []

    if not isinstance(
        nodes,
        list
    ):

        print(
            "❌ Formato pending offers inatteso."
        )

        return None

    return nodes


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(
    asset_ids
):

    asset_ids = [
        str(x).strip()
        for x in asset_ids
        if x
    ]

    asset_ids = list(
        dict.fromkeys(
            asset_ids
        )
    )

    if not asset_ids:

        return None

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

        return None

    carte = (
        risultato
        .get("data", {})
        .get("anyCards")
    )

    if carte is None:

        return []

    if not isinstance(
        carte,
        list
    ):

        print(
            "❌ Formato anyCards inatteso."
        )

        return None

    return carte


# ============================================================
# EUR CENTS
# ============================================================

def leggi_eur_cents(
    amounts
):

    if not isinstance(
        amounts,
        dict
    ):

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
# PREZZO LIVE
# ============================================================

def prezzo_da_live_sale(
    carta
):

    if not isinstance(
        carta,
        dict
    ):

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
# PUBLIC MIN PRICE
# ============================================================

def prezzo_da_public_min_price(
    carta
):

    if not isinstance(
        carta,
        dict
    ):

        return None

    valore = carta.get(
        "publicMinPrices"
    )

    # In alcune risposte può arrivare come oggetto.
    if isinstance(
        valore,
        dict
    ):

        eur_cents = leggi_eur_cents(
            valore
        )

    # In altre risposte può essere una lista.
    elif isinstance(
        valore,
        list
    ):

        valori = []

        for item in valore:

            if not isinstance(
                item,
                dict
            ):

                continue

            eur_cents = leggi_eur_cents(
                item
            )

            if eur_cents is not None:

                valori.append(
                    eur_cents
                )

        if not valori:

            return None

        eur_cents = min(
            valori
        )

    else:

        return None

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

    if not isinstance(
        carta,
        dict
    ):

        return None

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

    valori = []

    # ========================================================
    # PRIMA SCELTA: LOWEST PRICE CARD
    # ========================================================

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

    prezzo_live = prezzo_da_live_sale(
        lowest_price_card
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

    # ========================================================
    # SE NON DISPONIBILE, PROVIAMO ANY SEASON
    # ========================================================

    if not valori:

        lowest_any_season = (
            carta.get(
                "lowestPriceCardAnySeason"
            )
            or {}
        )

        if lowest_any_season:

            print(
                "      🔄 Provo "
                "lowestPriceCardAnySeason."
            )

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
                f"      💰 Offerta any season: "
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
                f"      💰 Public min any season: "
                f"€{prezzo_public:.2f}"
            )

    # ========================================================
    # ULTIMO FALLBACK: CARTA ORIGINALE
    # ========================================================

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
# CONTROLLO SQUADRA + CAMPIONATO
# ============================================================
#
# REGOLA:
#
# activeClub è la fonte principale.
#
# NON utilizziamo vecchie squadre.
#
# Se activeClub manca:
#       -> NON IDONEO
#
# Se activeClub esiste ma non ha competizioni:
#       -> NON IDONEO
#
# Se almeno una activeCompetition è coperta:
#       -> IDONEO
#
# ============================================================

def controlla_squadra_e_campionato(
    carta
):

    if not isinstance(
        carta,
        dict
    ):

        return False

    player = (
        carta.get(
            "anyPlayer"
        )
        or {}
    )

    if not isinstance(
        player,
        dict
    ):

        player = {}

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

    active_club = player.get(
        "activeClub"
    )

    # ========================================================
    # NESSUNA SQUADRA ATTIVA
    # ========================================================

    if not active_club:

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

    if not isinstance(
        active_club,
        dict
    ):

        print(
            "      🔴 activeClub ha formato "
            "inatteso."
        )

        return False

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

    competizioni = (
        active_club.get(
            "activeCompetitions"
        )
        or []
    )

    if not isinstance(
        competizioni,
        list
    ):

        print(
            "      🔴 activeCompetitions "
            "ha formato inatteso."
        )

        return False

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
            dict
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

    print(
        "      🔎 Nessuna competizione attiva "
        "della squadra è coperta."
    )

    return False


# ============================================================
# ANALISI CARTA
# ============================================================

def analizza_carta(
    carta
):

    if not isinstance(
        carta,
        dict
    ):

        return False

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
    ).upper().strip()

    # ========================================================
    # PREZZO
    # ========================================================

    prezzo = (
        recupera_prezzo_floor(
            carta
        )
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

    # ========================================================
    # RARITÀ
    # ========================================================

    rarita_ok = (
        rarita == "LIMITED"
    )

    # ========================================================
    # CAMPIONATO
    # ========================================================

    campionato_coperto = (
        controlla_squadra_e_campionato(
            carta
        )
    )

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

        if not isinstance(
            carta,
            dict
        ):

            continue

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

    return kulenovic_presente


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(
    offerta
):

    if not isinstance(
        offerta,
        dict
    ):

        return False

    offerta_id = str(
        offerta.get(
            "id"
        )
        or ""
    ).strip()

    if not offerta_id:

        print(
            "⚠️ Offerta senza ID."
        )

        return False

    # ========================================================
    # EVITA DOPPIA ELABORAZIONE CONCORRENTE
    # ========================================================

    with stato_lock:

        if offerta_id in offerte_gia_analizzate:

            return True

        if offerta_id in offerte_in_elaborazione:

            return True

        offerte_in_elaborazione.add(
            offerta_id
        )

    elaborazione_completata = False

    try:

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

        # ====================================================
        # ASSET ID
        # ====================================================

        asset_ids = []

        for carta in carte_offerte:

            if not isinstance(
                carta,
                dict
            ):

                continue

            asset_id = str(
                carta.get(
                    "assetId"
                )
                or ""
            ).strip()

            if asset_id:

                asset_ids.append(
                    asset_id
                )

        asset_ids = list(
            dict.fromkeys(
                asset_ids
            )
        )

        if not asset_ids:

            print("")
            print(
                "🔴 DECISIONE SIMULATA: "
                "RIFIUTARE"
            )

            print(
                "   Motivo: nessuna carta ricevuta."
            )

            print(
                "🟡 DRY RUN: nessun rifiuto eseguito."
            )

            elaborazione_completata = True

            return True

        # ====================================================
        # DETTAGLI
        # ====================================================

        dettagli = (
            recupera_dettagli_carte(
                asset_ids
            )
        )

        if dettagli is None:

            print(
                "⚠️ Impossibile recuperare "
                "i dettagli delle carte."
            )

            print(
                "⚠️ L'offerta verrà rianalizzata "
                "al prossimo ciclo."
            )

            return False

        if not dettagli:

            print(
                "⚠️ Sorare non ha restituito "
                "dettagli per le carte ricevute."
            )

            return False

        # ====================================================
        # ANALISI
        # ====================================================

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

        numero_non_idonee = max(
            0,
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

        # ====================================================
        # NESSUNA IDONEA
        # ====================================================

        if numero_idonee == 0:

            print("")
            print(
                "🔴 DECISIONE SIMULATA: "
                "RIFIUTARE L'OFFERTA"
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

            elaborazione_completata = True

            return True

        # ====================================================
        # PAGAMENTO
        # ====================================================

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

        # ====================================================
        # CONTROPROPOSTA SIMULATA
        # ====================================================

        print("")
        print(
            "🟢 DECISIONE SIMULATA: "
            "CONTROPROPOSTA"
        )

        print("")

        if kulenovic_presente:

            print(
                "❌ Kulenovic NON viene considerato "
                "come carta da cedere nella "
                "controproposta simulata."
            )

        else:

            print(
                "ℹ️ Kulenovic non presente "
                "tra le carte richieste."
            )

        print("")

        print(
            "🗑️ CARTE NON IDONEE:"
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
            "📥 CARTE IDONEE:"
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
            f"💰 PAGAMENTO SIMULATO: "
            f"€{pagamento_euro:.2f}"
        )

        print(
            f"   {numero_idonee} × €0,20"
        )

        print("")

        print(
            "📋 CONTROPROPOSTA SIMULATA:"
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
            f"   💰 Noi pagheremmo: "
            f"€{pagamento_euro:.2f}"
        )

        print("")

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

        elaborazione_completata = True

        return True

    except Exception as e:

        print(
            f"❌ Errore elaborazione offerta "
            f"{offerta_id}: {e}"
        )

        return False

    finally:

        with stato_lock:

            offerte_in_elaborazione.discard(
                offerta_id
            )

            # IMPORTANTISSIMO:
            #
            # registriamo l'offerta come analizzata
            # SOLO se tutta l'elaborazione è terminata
            # correttamente.
            #
            # Se API/dati falliscono, potrà essere
            # riprovata al ciclo successivo.

            if elaborazione_completata:

                offerte_gia_analizzate.add(
                    offerta_id
                )


# ============================================================
# MONITOR
# ============================================================

def monitor_offerte():

    print("")
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

    verifica_configurazione()

    print("")

    test_firma_stark()

    print("")

    # ========================================================
    # AUTENTICAZIONE
    # ========================================================

    if not verifica_account():

        print(
            "❌ Impossibile autenticarsi "
            "a Sorare."
        )

        print(
            "❌ Monitoraggio terminato."
        )

        return

    print(
        "🟢 MONITORAGGIO OFFERTE ATTIVO."
    )

    print("")

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

            # None = errore API.
            #
            # Non stampiamo "0 offerte", perché sarebbe
            # falso e potrebbe far pensare che non esistano
            # offerte.

            if offerte is None:

                print(
                    "⚠️ Controllo offerte fallito."
                )

            else:

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
            INTERVALLO_CONTROLLO
        )


# ============================================================
# AVVIO MONITOR UNA SOLA VOLTA
# ============================================================

def avvia_monitoraggio():

    global monitoraggio_avviato
    global monitor_thread

    with stato_lock:

        if monitoraggio_avviato:

            return False

        monitoraggio_avviato = True

        monitor_thread = threading.Thread(
            target=monitor_offerte,
            name="sorare-monitor",
            daemon=True,
        )

        monitor_thread.start()

        return True


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/")
def home():

    avviato = (
        avvia_monitoraggio()
    )

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

    return jsonify({
        "status": "ok",
        "bot": "sorare",
        "dry_run": DRY_RUN,
        "monitoraggio_avviato": (
            monitoraggio_avviato
        ),
    })


# ============================================================
# STARTUP
# ============================================================
#
# IMPORTANTE SU RENDER/GUNICORN:
#
# Il monitor NON viene avviato automaticamente qui.
#
# La prima richiesta HTTP / avvia il thread.
#
# Questo evita di creare il thread due volte durante
# l'importazione del modulo da parte di Gunicorn.
#
# Se vuoi una configurazione più robusta per produzione,
# la soluzione migliore è separare Web e Worker in due
# servizi Render distinti.
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    avvia_monitoraggio()

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
