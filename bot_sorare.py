import os
import time
import threading
import requests
import hashlib
import unicodedata
import re

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

# Può essere asset ID, slug oppure vuota.
KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

# Chiave privata Stark.
# NON viene mai stampata nei log.
SORARE_STARK_PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# SICUREZZA
# ============================================================

# SEMPRE DRY RUN.
#
# Nessun rifiuto.
# Nessuna controproposta.
# Nessuna transazione reale.
DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

PREZZO_MINIMO_CENTESIMI = 30

PREZZO_MASSIMO_CENTESIMI = 80

PAGAMENTO_PER_CARTA_CENTESIMI = 20


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
# CAMPIONATI COPERTI
# ============================================================

# Lista confermata dall'utente.
#
# IMPORTANTE:
# Sorare normalmente restituisce lo slug della competizione.
# Per questo ogni campionato ha uno o più alias/slug possibili.
#
# Il controllo viene fatto sullo slug restituito dall'API.
#
# Non viene più utilizzato:
#
#     len(competizioni) > 0
#
# perché avere una competizione attiva NON significa
# automaticamente che sia un campionato coperto.
# ============================================================

CAMPIONATI_COPERTI = {

    "English League": [
        "premier-league",
        "english-premier-league",
        "premier-league-players",
        "english-league",
    ],

    "Ligue 1": [
        "ligue-1",
        "ligue-1-uber-eats",
        "ligue-1-mcdonalds",
    ],

    "LALIGA EA SPORTS": [
        "laliga-ea-sports",
        "laliga",
        "la-liga",
        "laliga-santander",
    ],

    "Bundesliga": [
        "bundesliga",
        "bundesliga-players",
    ],

    "Liga Portugal": [
        "liga-portugal",
        "primeira-liga",
        "portuguese-primeira-liga",
    ],

    "Eredivisie": [
        "eredivisie",
        "eredivisie-players",
    ],

    "Jupiler Pro League": [
        "jupiler-pro-league",
        "belgian-pro-league",
        "pro-league",
    ],

    "Scottish Premiership": [
        "scottish-premiership",
        "scottish-premiership-players",
    ],

    "J.League": [
        "j-league",
        "j1-league",
        "j-league-1",
        "j1",
    ],

    "Seconda divisione inglese": [
        "championship",
        "english-championship",
        "efl-championship",
        "english-second-division",
        "second-division",
    ],

    "Austrian Bundesliga": [
        "austrian-bundesliga",
        "bundesliga-austria",
        "austria-bundesliga",
    ],

    "Croatian HNL": [
        "croatian-hnl",
        "hnl",
        "1-hnl",
        "croatian-first-football-league",
    ],

    "2. Bundesliga": [
        "2-bundesliga",
        "2.-bundesliga",
        "bundesliga-2",
        "german-2-bundesliga",
    ],

    "Ligue 2": [
        "ligue-2",
        "ligue-2-bkt",
        "ligue-2-bkt-players",
    ],

    "MLS": [
        "major-league-soccer",
        "mls",
        "major-league-soccer-players",
    ],

    "K League": [
        "k-league",
        "k-league-1",
        "k-league-1-players",
        "kleague-1",
    ],

    "Turchia": [
        "super-lig",
        "turkish-super-lig",
        "turkey-super-lig",
        "super-lig-players",
    ],

    "Danimarca": [
        "superliga",
        "danish-superliga",
        "denmark-superliga",
        "superligaen",
    ],

    "Serie A": [
        "serie-a",
        "serie-a-players",
        "italian-serie-a",
    ],

    "Brasile": [
        "brasileirao-serie-a",
        "brasileirao",
        "brazilian-serie-a",
        "brazil-serie-a",
        "serie-a-brazil",
    ],

    "Russia": [
        "russian-premier-league",
        "russia-premier-league",
        "premier-liga",
        "russian-premier-liga",
    ],

    "Serie B": [
        "serie-b",
        "serie-b-players",
        "italian-serie-b",
    ],

    "Perù": [
        "liga-1",
        "liga-1-peru",
        "peruvian-liga-1",
        "peru-liga-1",
    ],

    "Colombia": [
        "primera-a",
        "primera-a-colombia",
        "colombian-primera-a",
        "liga-betplay",
    ],

    "Messico": [
        "liga-mx",
        "mexican-liga-mx",
        "liga-mx-players",
    ],

    "LALIGA 2": [
        "laliga-2",
        "la-liga-2",
        "segunda-division",
        "segunda-division-players",
        "laliga-hypermotion",
    ],
}


# ============================================================
# COSTRUZIONE INDICE CAMPIONATI
# ============================================================

def normalizza_testo(valore):

    if valore is None:

        return ""

    testo = str(
        valore
    ).strip().lower()

    testo = unicodedata.normalize(
        "NFKD",
        testo
    )

    testo = "".join(
        carattere
        for carattere in testo
        if not unicodedata.combining(carattere)
    )

    testo = testo.replace(
        "’",
        "'"
    )

    testo = testo.replace(
        "_",
        "-"
    )

    testo = re.sub(
        r"[^a-z0-9]+",
        "-",
        testo
    )

    testo = re.sub(
        r"-+",
        "-",
        testo
    )

    return testo.strip("-")


CAMPIONATI_COPERTI_NORMALIZZATI = {}

for nome_campionato, alias in CAMPIONATI_COPERTI.items():

    for valore in alias:

        valore_normalizzato = normalizza_testo(
            valore
        )

        if valore_normalizzato:

            CAMPIONATI_COPERTI_NORMALIZZATI[
                valore_normalizzato
            ] = nome_campionato


# ============================================================
# STATO
# ============================================================

offerte_gia_analizzate = set()

monitoraggio_avviato = False

lock_avvio = threading.Lock()


# ============================================================
# CONTROLLO CAMPIONATO
# ============================================================

def trova_campionato_coperto(competizioni):

    """
    Riceve le competizioni attive restituite da Sorare.

    Restituisce il nome del campionato coperto trovato
    oppure None.

    Il confronto viene effettuato sullo slug.
    """

    if not competizioni:

        return None

    for competizione in competizioni:

        if not competizione:

            continue

        slug = str(
            competizione.get("slug")
            or ""
        ).strip()

        slug_normalizzato = normalizza_testo(
            slug
        )

        if not slug_normalizzato:

            continue

        campionato = (
            CAMPIONATI_COPERTI_NORMALIZZATI.get(
                slug_normalizzato
            )
        )

        if campionato:

            return campionato

    return None


# ============================================================
# TEST LOCALE FIRMA STARK
# ============================================================

def test_firma_stark():

    print("")
    print("========================================")
    print("🔐 TEST LOCALE FIRMA STARK")
    print("========================================")

    # --------------------------------------------------------
    # Controllo variabile ambiente
    # --------------------------------------------------------

    if not SORARE_STARK_PRIVATE_KEY:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY non configurata."
        )

        print(
            "========================================"
        )

        return False

    print(
        "✅ SORARE_STARK_PRIVATE_KEY presente."
    )

    # --------------------------------------------------------
    # Normalizzazione chiave
    # --------------------------------------------------------

    chiave = (
        SORARE_STARK_PRIVATE_KEY.strip()
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

    # --------------------------------------------------------
    # Controllo esadecimale
    # --------------------------------------------------------

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
    # Import Starknet
    # --------------------------------------------------------

    try:

        from starknet_py.net.signer.stark_curve import (
            PrivateKey,
            message_signature,
        )

        print(
            "✅ API Starknet importate correttamente."
        )

    except Exception as e:

        print(
            "❌ Impossibile importare starknet-py."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    # --------------------------------------------------------
    # Inizializzazione private key
    # --------------------------------------------------------

    try:

        private_key = PrivateKey(
            private_key_int
        )

    except Exception as e:

        print(
            "❌ Impossibile inizializzare la chiave Stark."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    print(
        "✅ Private key Stark inizializzata."
    )

    # --------------------------------------------------------
    # Derivazione public key
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
    # Messaggio di test
    # --------------------------------------------------------

    messaggio_testo = (
        "SORARE_LOCAL_SIGNATURE_TEST"
    )

    # --------------------------------------------------------
    # Hash SHA-256
    # --------------------------------------------------------

    try:

        digest = hashlib.sha256(
            messaggio_testo.encode("utf-8")
        ).digest()

        messaggio_hash = int.from_bytes(
            digest,
            byteorder="big"
        )

    except Exception as e:

        print(
            "❌ Impossibile creare l'hash di test."
        )

        print(
            f"   Dettaglio: {e}"
        )

        return False

    # --------------------------------------------------------
    # Campo primo Stark
    # --------------------------------------------------------

    STARK_FIELD_PRIME = (
        (2 ** 251)
        + (17 * (2 ** 192))
        + 1
    )

    messaggio_hash %= STARK_FIELD_PRIME

    print(
        "✅ Hash di test Stark preparato."
    )

    # --------------------------------------------------------
    # Generazione firma
    # --------------------------------------------------------

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
    # Controllo struttura firma
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
    # Verifica firma
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

        print(
            "========================================"
        )

        return False

    # --------------------------------------------------------
    # RISULTATO POSITIVO
    # --------------------------------------------------------

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

    print("")

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

    if not token.lower().startswith("bearer "):

        token = f"Bearer {token}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
    }

    if SORARE_JWT_AUD:

        headers["JWT-AUD"] = SORARE_JWT_AUD

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def esegui_query(query, variables=None):

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
            f"🌐 Sorare HTTP: {response.status_code}"
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
        connessione.get("nodes")
        or []
    )


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(asset_ids):

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

def leggi_eur_cents(amounts):

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
# PREZZO DA LIVE SALE
# ============================================================

def prezzo_da_live_sale(carta):

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
# PREZZO DA PUBLIC MIN PRICE
# ============================================================

def prezzo_da_public_min_price(carta):

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

def recupera_prezzo_floor(carta):

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
        f"      🔎 Ricerca prezzo floor: {slug}"
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
            "      ⚠️ lowestPriceCard non disponibile."
        )

    valori = []

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
            f"      ⚠️ Prezzo non disponibile per {nome}."
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
# COMPETIZIONI CARTA
# ============================================================

def recupera_competizioni_carta(carta):

    competizioni = []

    player = (
        carta.get("anyPlayer")
        or {}
    )

    club = (
        player.get("activeClub")
        or {}
    )

    competizioni_player = (
        club.get("activeCompetitions")
        or []
    )

    for competizione in competizioni_player:

        if competizione:

            competizioni.append(
                competizione
            )

    team = (
        carta.get("anyTeam")
        or {}
    )

    competizioni_team = (
        team.get("activeCompetitions")
        or []
    )

    for competizione in competizioni_team:

        if competizione:

            competizioni.append(
                competizione
            )

    # Rimuove duplicati per slug.
    risultati = []

    slug_visti = set()

    for competizione in competizioni:

        slug = str(
            competizione.get("slug")
            or ""
        ).strip()

        slug_norm = normalizza_testo(
            slug
        )

        if not slug_norm:

            continue

        if slug_norm in slug_visti:

            continue

        slug_visti.add(
            slug_norm
        )

        risultati.append(
            {
                "slug": slug
            }
        )

    return risultati


# ============================================================
# CONTROLLO CARTA
# ============================================================

def analizza_carta(carta):

    asset_id = (
        carta.get("assetId")
    )

    slug = (
        carta.get("slug")
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
    ).upper()

    # --------------------------------------------------------
    # COMPETIZIONI
    # --------------------------------------------------------

    competizioni = recupera_competizioni_carta(
        carta
    )

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    prezzo = recupera_prezzo_floor(
        carta
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

    # --------------------------------------------------------
    # CAMPIONATO COPERTO
    # --------------------------------------------------------

    campionato_coperto = (
        trova_campionato_coperto(
            competizioni
        )
    )

    # --------------------------------------------------------
    # RARITÀ
    # --------------------------------------------------------

    rarita_ok = (
        rarita == "LIMITED"
    )

    # --------------------------------------------------------
    # IDONEITÀ FINALE
    # --------------------------------------------------------

    idonea = (
        rarita_ok
        and prezzo_ok
        and campionato_coperto is not None
    )

    # ========================================================
    # LOG
    # ========================================================

    print("")

    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: {asset_id}"
    )

    print(
        f"      Slug: {slug or 'N/D'}"
    )

    print(
        f"      Rarità: {rarita or 'N/D'}"
    )

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    if prezzo is not None:

        print(
            f"      Prezzo floor: "
            f"€{prezzo:.2f}"
        )

        if prezzo < prezzo_minimo:

            print(
                "      🔴 Prezzo inferiore al minimo "
                "di €0,30"
            )

        elif prezzo <= prezzo_massimo:

            print(
                "      🟢 Prezzo tra €0,30 e €0,80"
            )

        else:

            print(
                "      🔴 Prezzo superiore al massimo "
                "di €0,80"
            )

    else:

        print(
            "      Prezzo floor: N/D"
        )

        print(
            "      🔴 Prezzo NON verificabile"
        )

    # --------------------------------------------------------
    # COMPETIZIONI
    # --------------------------------------------------------

    print("")

    print(
        "      🏆 COMPETIZIONI ATTIVE:"
    )

    if not competizioni:

        print(
            "         Nessuna competizione attiva."
        )

    else:

        for competizione in competizioni:

            slug_competizione = str(
                competizione.get("slug")
                or ""
            ).strip()

            print(
                f"         • {slug_competizione}"
            )

    if campionato_coperto:

        print(
            f"      🟢 CAMPIONATO COPERTO: "
            f"{campionato_coperto}"
        )

    else:

        print(
            "      🔴 CAMPIONATO NON COPERTO"
        )

        if competizioni:

            print(
                "      🔎 Nessuno degli slug trovati "
                "è presente nella lista dei campionati coperti."
            )

    # --------------------------------------------------------
    # RARITÀ
    # --------------------------------------------------------

    if rarita_ok:

        print(
            "      🟢 Rarità LIMITED"
        )

    else:

        print(
            "      🔴 Rarità NON valida"
        )

    # --------------------------------------------------------
    # RISULTATO FINALE
    # --------------------------------------------------------

    if idonea:

        print(
            "      =================================="
        )

        print(
            "      ✅ CARTA IDONEA"
        )

        print(
            "      =================================="
        )

    else:

        print(
            "      =================================="
        )

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

def controlla_kulenovic(carte_richieste):

    print("")

    print(
        "🔎 CARTA/E RICHIESTA/E DAL MANAGER:"
    )

    kulenovic_presente = False

    configurato = (
        KULENOVIC_ID.strip()
        if KULENOVIC_ID
        else ""
    )

    for carta in carte_richieste:

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
            "ℹ️ Kulenovic non riconosciuto nell'offerta."
        )

        print(
            "ℹ️ L'offerta viene comunque analizzata in DRY RUN."
        )

    return kulenovic_presente


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offerta):

    offerta_id = (
        offerta.get("id")
    )

    if not offerta_id:

        return

    if offerta_id in offerte_gia_analizzate:

        return

    offerte_gia_analizzate.add(
        offerta_id
    )

    print("")
    print("========================================")
    print("📨 NUOVA OFFERTA")

    print(
        f"🆔 ID: {offerta_id}"
    )

    print(
        f"📌 Stato: {offerta.get('status')}"
    )

    sender = (
        offerta.get("sender")
        or {}
    )

    nickname = (
        sender.get("nickname")
        or sender.get("slug")
        or "Sconosciuto"
    )

    print(
        f"👤 Manager: {nickname}"
    )

    sender_side = (
        offerta.get("senderSide")
        or {}
    )

    receiver_side = (
        offerta.get("receiverSide")
        or {}
    )

    carte_offerte = (
        sender_side.get("anyCards")
        or []
    )

    carte_che_diamo = (
        receiver_side.get("anyCards")
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
        carta.get("assetId")
        for carta in carte_offerte
        if carta.get("assetId")
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
            "🟡 DRY RUN: nessuna operazione eseguita."
        )

        print(
            "----------------------------------------"
        )

        return

    dettagli = recupera_dettagli_carte(
        asset_ids
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

        if analizza_carta(carta):

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

    if numero_idonee == 0:

        print("")

        print(
            "🔴 DECISIONE: RIFIUTARE L'OFFERTA"
        )

        print(
            "   Motivo: nessuna carta ricevuta è idonea."
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
            carta.get("name")
            or carta.get("slug")
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
            carta.get("name")
            or carta.get("slug")
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ Noi riceviamo: {nome_carta}"
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
            "   Nessuna controproposta è stata inviata."
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

    print("")

    print(
        "🏆 REGOLA CAMPIONATI:"
    )

    print(
        f"   {len(CAMPIONATI_COPERTI)} campionati coperti."
    )

    for nome_campionato in CAMPIONATI_COPERTI:

        print(
            f"   • {nome_campionato}"
        )

    print("")

    # ========================================================
    # TEST LOCALE FIRMA
    # ========================================================

    firma_ok = test_firma_stark()

    if not firma_ok:

        print(
            "⚠️ TEST FIRMA STARK NON SUPERATO."
        )

        print(
            "⚠️ Il bot continua in DRY RUN."
        )

        print(
            "⚠️ Nessuna operazione reale verrà eseguita."
        )

    else:

        print(
            "🟢 TEST FIRMA STARK SUPERATO."
        )

        print(
            "🟡 Il bot rimane comunque in DRY RUN."
        )

    print("")

    if not verifica_account():

        print(
            "❌ Impossibile autenticarsi a Sorare."
        )

        return

    while True:

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

                elabora_offerta(
                    offerta
                )

        except Exception as e:

            print(
                f"⚠️ Errore nel ciclo: {e}"
            )

        # ====================================================
        # CONTROLLO OGNI 10 SECONDI
        # ====================================================

        time.sleep(10)


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
