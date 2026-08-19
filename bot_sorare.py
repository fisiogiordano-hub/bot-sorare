import os
import time
import threading
import subprocess
import json
import uuid
import requests

from decimal import Decimal
from flask import Flask, jsonify


# ============================================================
# APP / CONFIG
# ============================================================

app = Flask(__name__)

SORARE_API_URL = "https://api.sorare.com/graphql"

SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
SORARE_JWT_AUD = os.getenv("SORARE_JWT_AUD", "").strip()
SORARE_API_KEY = os.getenv("SORARE_API_KEY", "").strip()
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()
SORARE_STARK_PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY", ""
).strip()

# ============================================================
# MODALITÀ REALE
# ============================================================

DRY_RUN = False

PREZZO_MINIMO = 30
PREZZO_MASSIMO = 80
PAGAMENTO_PER_CARTA = 20

INTERVALLO_CONTROLLO = 10
TIMEOUT_HTTP = 30


KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET_ID = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI = {
    "english-league": "English League",
    "premier-league-eng": "English League",
    "premier-league": "English League",

    "ligue-1-fr": "Ligue 1",
    "ligue-1": "Ligue 1",

    "laliga-es": "LALIGA EA SPORTS",
    "laliga": "LALIGA EA SPORTS",
    "la-liga": "LALIGA EA SPORTS",
    "laliga-ea-sports": "LALIGA EA SPORTS",

    "bundesliga-de": "Bundesliga",
    "bundesliga": "Bundesliga",

    "liga-portugal": "Liga Portugal",
    "primeira-liga-pt": "Liga Portugal",
    "liga-portugal-pt": "Liga Portugal",

    "eredivisie-nl": "Eredivisie",
    "eredivisie": "Eredivisie",

    "jupiler-pro-league-be": "Jupiler Pro League",
    "jupiler-pro-league": "Jupiler Pro League",

    "scottish-premiership-sco": "Scottish Premiership",
    "scottish-premiership": "Scottish Premiership",

    "jleague-jp": "J.League",
    "j1-league-jp": "J.League",
    "j-league": "J.League",
    "j1-league": "J.League",

    "second-division-eng": "Seconda divisione inglese",
    "championship-eng": "Seconda divisione inglese",
    "english-championship": "Seconda divisione inglese",
    "championship": "Seconda divisione inglese",

    "austrian-bundesliga-at": "Austrian Bundesliga",
    "austrian-bundesliga": "Austrian Bundesliga",
    "bundesliga-at": "Austrian Bundesliga",

    "croatian-hnl-hr": "Croatian HNL",
    "croatian-first-league-hr": "Croatian HNL",
    "croatian-first-league": "Croatian HNL",
    "croatian-hnl": "Croatian HNL",
    "supersport-hnl": "Croatian HNL",

    "2-bundesliga-de": "2. Bundesliga",
    "2-bundesliga": "2. Bundesliga",

    "ligue-2-fr": "Ligue 2",
    "ligue-2": "Ligue 2",

    "mls-us": "MLS",
    "major-league-soccer-us": "MLS",
    "major-league-soccer": "MLS",
    "mls": "MLS",

    "k-league-1-kr": "K League",
    "k-league-1": "K League",
    "k-league": "K League",

    "super-lig-tr": "Turchia",
    "super-lig": "Turchia",
    "turkish-super-lig": "Turchia",

    "superliga-dk": "Danimarca",
    "superliga": "Danimarca",
    "danish-superliga": "Danimarca",

    "serie-a-it": "Serie A",
    "serie-a": "Serie A",

    "brasileirao-serie-a-br": "Brasile",
    "brasileirao-serie-a": "Brasile",
    "brasileirao": "Brasile",
    "serie-a-br": "Brasile",

    "premier-liga-ru": "Russia",
    "russian-premier-league": "Russia",
    "premier-liga": "Russia",
    "russia-premier-league": "Russia",

    "serie-b-it": "Serie B",
    "serie-b": "Serie B",

    "liga-1-peru": "Perù",
    "liga-1-pe": "Perù",
    "peruvian-primera-division": "Perù",

    "primera-a-colombia": "Colombia",
    "liga-betplay-col": "Colombia",
    "primera-a": "Colombia",
    "liga-betplay": "Colombia",

    "liga-mx": "Messico",

    "laliga-2-es": "LALIGA 2",
    "laliga-hypermotion": "LALIGA 2",
    "laliga-2": "LALIGA 2",
    "segunda-division-spain": "LALIGA 2",
}


# ============================================================
# STATO
# ============================================================

analizzate = set()
in_elaborazione = set()
lock = threading.Lock()


# ============================================================
# UTILITY
# ============================================================

def slug(value):
    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )


def client_id():
    return uuid.uuid4().hex


def headers():
    if not SORARE_TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato."
        )

    token = SORARE_TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-Offer-Bot/2.0",
    }

    if SORARE_JWT_AUD:
        h["JWT-AUD"] = SORARE_JWT_AUD

    if SORARE_API_KEY:
        h["APIKEY"] = SORARE_API_KEY

    return h


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for tentativo in range(1, 4):
        try:
            r = requests.post(
                SORARE_API_URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT_HTTP,
            )

            print(f"🌐 Sorare HTTP: {r.status_code}")

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")

                try:
                    pausa = max(
                        1,
                        int(retry_after)
                    )
                except (ValueError, TypeError):
                    pausa = tentativo * 3

                print(
                    f"⚠️ Rate limit Sorare. "
                    f"Attendo {pausa}s."
                )

                time.sleep(pausa)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: "
                    f"{r.text[:500]}"
                )

                time.sleep(tentativo)
                continue

            try:
                data = r.json()
            except ValueError:
                print("❌ Risposta JSON non valida.")
                return None

            if data.get("errors"):
                print("❌ Errore GraphQL:")

                for error in data["errors"]:
                    print(
                        " -",
                        error.get(
                            "message",
                            str(error)
                        )
                    )

                return None

            return data

        except requests.RequestException as e:
            print(f"⚠️ Errore HTTP: {e}")
            time.sleep(tentativo)

        except Exception as e:
            print(f"❌ Errore: {e}")
            return None

    return None


# ============================================================
# ACCOUNT
# ============================================================

def verifica_account():
    q = """
    query {
        currentUser {
            slug
            nickname
            starkKey
        }
    }
    """

    data = graphql(q)

    if not data:
        return False

    user = (
        data.get("data", {})
        .get("currentUser")
    )

    if not user:
        print("❌ currentUser non disponibile.")
        return False

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

    if user.get("starkKey"):
        print("🔐 Stark key account: presente.")
    else:
        print("⚠️ Stark key account non disponibile.")

    print("========================================")

    return True


# ============================================================
# OFFERTE
# ============================================================

def recupera_offerte():
    q = """
    query {
        currentUser {
            pendingTokenOffersReceived(first: 50) {
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

    data = graphql(q)

    if not data:
        return None

    user = (
        data.get("data", {})
        .get("currentUser", {})
    )

    pending = (
        user.get("pendingTokenOffersReceived")
        or {}
    )

    return pending.get("nodes") or []


# ============================================================
# DETTAGLI CARTE
# ============================================================

def dettagli_carte(asset_ids):
    asset_ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
        )
    )

    if not asset_ids:
        return []

    q = """
    query CardDetails($assetIds: [String!]) {
        anyCards(assetIds: $assetIds) {
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

    data = graphql(
        q,
        {"assetIds": asset_ids}
    )

    if not data:
        return None

    return (
        data.get("data", {})
        .get("anyCards")
        or []
    )


# ============================================================
# PREZZI
# ============================================================

def prezzo_live(card):
    try:
        cents = (
            card.get("liveSingleSaleOffer", {})
            .get("receiverSide", {})
            .get("amounts", {})
            .get("eurCents")
        )

        if cents is not None:
            return int(cents)

    except (
        ValueError,
        TypeError,
        AttributeError
    ):
        pass

    return None


def prezzo_public(card):
    values = card.get("publicMinPrices")

    if isinstance(values, dict):
        values = [values]

    if not isinstance(values, list):
        return None

    prezzi = []

    for value in values:
        if not isinstance(value, dict):
            continue

        try:
            cents = int(
                value.get("eurCents")
            )

            if cents > 0:
                prezzi.append(cents)

        except (
            ValueError,
            TypeError
        ):
            pass

    return min(prezzi) if prezzi else None


def prezzo_floor(card):
    candidates = []

    for key in (
        "lowestPriceCard",
        "lowestPriceCardAnySeason",
        None,
    ):
        source = (
            card
            if key is None
            else card.get(key) or {}
        )

        live = prezzo_live(source)
        public = prezzo_public(source)

        if live:
            candidates.append(live)

        if public:
            candidates.append(public)

        if candidates:
            break

    return min(candidates) if candidates else None


# ============================================================
# SQUADRA / CAMPIONATO
# ============================================================

def controlla_squadra(card):
    player = card.get("anyPlayer") or {}

    name = (
        player.get("displayName")
        or player.get("slug")
        or card.get("name")
        or "Sconosciuto"
    )

    club = player.get("activeClub")

    # ========================================================
    # REGOLA FONDAMENTALE
    # SENZA SQUADRA = NON IDONEA
    # ========================================================

    if not isinstance(club, dict):
        print("      🏟️ Squadra attiva: NESSUNA")
        print(f"      👤 Giocatore: {name}")
        print("      🔴 GIOCATORE SENZA SQUADRA")
        print("      🔴 OFFERTA DA RIFIUTARE")
        return False

    club_name = (
        club.get("name")
        or club.get("slug")
        or "N/D"
    )

    print(
        f"      🏟️ Squadra attiva: "
        f"{club_name}"
    )

    competitions = (
        club.get("activeCompetitions")
        or []
    )

    if not competitions:
        print(
            "      🔴 Nessuna "
            "competizione attiva."
        )
        print(
            "      🔴 CAMPIONATO NON VALIDO"
        )
        return False

    trovati = []

    for competition in competitions:
        if not isinstance(competition, dict):
            continue

        comp_slug = slug(
            competition.get("slug")
        )

        if comp_slug in CAMPIONATI:
            trovati.append(
                CAMPIONATI[comp_slug]
            )

    if not trovati:
        print(
            "      🔴 CAMPIONATO NON COPERTO"
        )
        return False

    print("      🟢 CAMPIONATO COPERTO")

    for nome in dict.fromkeys(trovati):
        print(f"         🟢 {nome}")

    return True


# ============================================================
# ANALISI CARTA
# ============================================================

def analizza_carta(card):
    if not isinstance(card, dict):
        return False

    nome = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    floor = prezzo_floor(card)

    print(f"\n   📄 {nome}")
    print(
        f"      Asset ID: "
        f"{card.get('assetId') or 'N/D'}"
    )
    print(
        f"      Slug: "
        f"{card.get('slug') or 'N/D'}"
    )
    print(
        f"      Rarità: "
        f"{rarity or 'N/D'}"
    )

    if floor is None:
        print(
            "      🔴 Prezzo floor "
            "NON verificabile"
        )
        prezzo_ok = False

    else:
        print(
            f"      💰 Prezzo floor: "
            f"€{floor / 100:.2f}"
        )

        prezzo_ok = (
            PREZZO_MINIMO
            <= floor
            <= PREZZO_MASSIMO
        )

        if prezzo_ok:
            print(
                "      🟢 Prezzo tra "
                "€0,30 e €0,80"
            )
        else:
            print(
                "      🔴 Prezzo fuori "
                "intervallo"
            )

    rarity_ok = rarity == "LIMITED"

    if rarity_ok:
        print("      🟢 Rarità LIMITED")
    else:
        print(
            "      🔴 Rarità NON valida"
        )

    campionato_ok = controlla_squadra(card)

    idonea = (
        prezzo_ok
        and rarity_ok
        and campionato_ok
    )

    print(
        "      🟢 CARTA IDONEA"
        if idonea
        else "      ❌ CARTA NON IDONEA"
    )

    return idonea


# ============================================================
# KULENOVIC
# ============================================================

def kulenovic_richiesto(cards):
    for card in cards:
        if not isinstance(card, dict):
            continue

        asset = str(
            card.get("assetId") or ""
        ).lower()

        card_slug = str(
            card.get("slug") or ""
        ).lower()

        if (
            asset == KULENOVIC_ASSET_ID.lower()
            or card_slug == KULENOVIC_SLUG.lower()
            or (
                KULENOVIC_ID
                and (
                    asset == KULENOVIC_ID.lower()
                    or card_slug == KULENOVIC_ID.lower()
                )
            )
        ):
            print(
                "🎯 KULENOVIC RICONOSCIUTO!"
            )
            return True

    return False


# ============================================================
# FIRMA STARK
# ============================================================

def firma_authorizations(authorizations):
    """
    La firma delle AuthorizationRequest viene delegata
    all'helper Node che usa @sorare/crypto.

    NON stampare mai la private key nei log.
    """

    if not SORARE_STARK_PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata."
        )

    payload = json.dumps(
        {
            "privateKey":
                SORARE_STARK_PRIVATE_KEY,
            "authorizations":
                authorizations,
        }
    )

    try:
        result = subprocess.run(
            [
                "node",
                "sign_authorizations.js",
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    except FileNotFoundError:
        raise RuntimeError(
            "Node.js non disponibile "
            "su Render."
        )

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Timeout durante la firma Stark."
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Firma Stark fallita: "
            + result.stderr[-1000:]
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            "Risposta dell'helper Stark "
            "non valida."
        )


# ============================================================
# PREPARAZIONE CONTROPROPOSTA
# ============================================================

def prepara_controproposta(
    offer,
    idonee,
    pagamento_cents,
):
    """
    Prepara il flusso DIRECT_OFFER.

    Noi:
      - NON cediamo Kulenovic
      - riceviamo le carte idonee
      - inviamo il pagamento stabilito

    La documentazione Sorare indica:
      prepareOffer
      -> sign authorizations
      -> createDirectOffer
    """

    sender = offer.get("sender") or {}

    receiver_slug = (
        sender.get("slug")
        or ""
    ).strip()

    if not receiver_slug:
        raise RuntimeError(
            "Slug del manager mittente "
            "non disponibile."
        )

    receive_asset_ids = [
        str(card.get("assetId"))
        for card in idonee
        if card.get("assetId")
    ]

    if not receive_asset_ids:
        raise RuntimeError(
            "Nessuna carta idonea."
        )

    amount_eur = Decimal(
        pagamento_cents
    ) / Decimal(100)

    # Sorare usa normalmente il valore
    # monetario nella valuta dichiarata.
    amount_cents = str(
        pagamento_cents
    )

    prepare_query = """
    mutation PrepareOffer(
        $input: prepareOfferInput!
    ) {
        prepareOffer(input: $input) {
            authorizations {
                fingerprint
                request {
                    __typename

                    ... on StarkexLimitOrderAuthorizationRequest {
                        vaultIdSell
                        vaultIdBuy
                        amountSell
                        amountBuy
                        tokenSell
                        tokenBuy
                        nonce
                        expirationTimestamp

                        feeInfo {
                            feeLimit
                            tokenId
                            sourceVaultId
                        }
                    }

                    ... on StarkexTransferAuthorizationRequest {
                        amount
                        condition
                        expirationTimestamp

                        feeInfoUser {
                            feeLimit
                            sourceVaultId
                            tokenId
                        }

                        nonce
                        receiverPublicKey
                        receiverVaultId
                        senderVaultId
                        token
                    }

                    ... on MangopayWalletTransferAuthorizationRequest {
                        nonce
                        amount
                        currency
                        operationHash
                        mangopayWalletId
                    }
                }
            }

            errors {
                message
            }
        }
    }
    """

    prepare_input = {
        "type": "DIRECT_OFFER",
        "sendAssetIds": [],
        "receiveAssetIds": receive_asset_ids,
        "sendAmount": {
            "amount": amount_cents,
            "currency": "EUR",
        },
        "receiverSlug": receiver_slug,
        "clientMutationId": client_id(),
    }

    data = graphql(
        prepare_query,
        {"input": prepare_input},
    )

    if not data:
        raise RuntimeError(
            "prepareOffer non ha restituito "
            "una risposta."
        )

    prepared = (
        data.get("data", {})
        .get("prepareOffer")
    )

    if not prepared:
        raise RuntimeError(
            "prepareOffer non disponibile."
        )

    errors = prepared.get("errors") or []

    if errors:
        messages = "; ".join(
            str(x.get("message", x))
            for x in errors
        )

        raise RuntimeError(
            f"prepareOffer: {messages}"
        )

    authorizations = (
        prepared.get("authorizations")
        or []
    )

    if not authorizations:
        raise RuntimeError(
            "prepareOffer non ha restituito "
            "AuthorizationRequest."
        )

    print(
        f"🔐 AuthorizationRequest ricevute: "
        f"{len(authorizations)}"
    )

    approvals = firma_authorizations(
        authorizations
    )

    if not approvals:
        raise RuntimeError(
            "Nessuna approval prodotta."
        )

    create_query = """
    mutation CreateDirectOffer(
        $input: createDirectOfferInput!
    ) {
        createDirectOffer(input: $input) {
            tokenOffer {
                id
            }

            errors {
                message
            }
        }
    }
    """

    create_input = {
        "approvals": approvals,
        "dealId": client_id(),

        "sendAssetIds": [],
        "receiveAssetIds": receive_asset_ids,

        "sendAmount": {
            "amount": amount_cents,
            "currency": "EUR",
        },

        "receiverSlug": receiver_slug,
        "clientMutationId": client_id(),
    }

    created = graphql(
        create_query,
        {"input": create_input},
    )

    if not created:
        raise RuntimeError(
            "createDirectOffer non ha "
            "restituito una risposta."
        )

    result = (
        created.get("data", {})
        .get("createDirectOffer")
    )

    if not result:
        raise RuntimeError(
            "createDirectOffer non disponibile."
        )

    errors = result.get("errors") or []

    if errors:
        messages = "; ".join(
            str(x.get("message", x))
            for x in errors
        )

        raise RuntimeError(
            f"createDirectOffer: {messages}"
        )

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:
        raise RuntimeError(
            "Sorare non ha restituito "
            "l'ID della nuova offerta."
        )

    print(
        "🟢 CONTROPROPOSTA CREATA"
    )
    print(
        f"🆔 Nuova offerta: {offer_id}"
    )
    print(
        f"💰 Pagamento: €{amount_eur:.2f}"
    )

    return offer_id


# ============================================================
# RIFIUTO
# ============================================================

def rifiuta_offerta(offer_id):
    """
    IMPORTANTE:
    Non viene eseguita una mutation inventata.

    L'API pubblica Sorare documenta il flusso di
    prepareOffer/createDirectOffer e acceptOffer,
    ma non ho trovato una mutation ufficiale
    reject/decline documentata.

    Pertanto questa funzione blocca il bot invece
    di effettuare una chiamata potenzialmente errata.
    """

    raise RuntimeError(
        "RIFIUTO REALE NON IMPLEMENTATO: "
        "nessuna mutation reject/decline ufficiale "
        "documentata nell'API pubblica verificata."
    )


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offer):
    offer_id = str(
        offer.get("id") or ""
    ).strip()

    if not offer_id:
        return

    with lock:
        if (
            offer_id in analizzate
            or offer_id in in_elaborazione
        ):
            return

        in_elaborazione.add(offer_id)

    completata = False

    try:
        sender = offer.get("sender") or {}
        sender_side = (
            offer.get("senderSide") or {}
        )
        receiver_side = (
            offer.get("receiverSide") or {}
        )

        ricevute = (
            sender_side.get("anyCards")
            or []
        )

        richieste = (
            receiver_side.get("anyCards")
            or []
        )

        print("\n========================================")
        print("📨 NUOVA OFFERTA")
        print(f"🆔 ID: {offer_id}")
        print(
            f"📌 Stato: "
            f"{offer.get('status')}"
        )
        print(
            f"👤 Manager: "
            f"{sender.get('nickname') or sender.get('slug') or 'N/D'}"
        )
        print(
            f"📦 Carte offerte: "
            f"{len(ricevute)}"
        )
        print(
            f"📦 Carte richieste: "
            f"{len(richieste)}"
        )

        # ----------------------------------------------------
        # KULENOVIC OBBLIGATORIO
        # ----------------------------------------------------

        if not kulenovic_richiesto(
            richieste
        ):
            print(
                "❌ Kulenovic non richiesto."
            )
            print(
                "🔴 OFFERTA DA RIFIUTARE."
            )

            # NON eseguiamo una mutation
            # non documentata.
            rifiuta_offerta(offer_id)

            completata = True
            return

        # ----------------------------------------------------
        # ASSET RICEVUTI
        # ----------------------------------------------------

        asset_ids = [
            c.get("assetId")
            for c in ricevute
            if (
                isinstance(c, dict)
                and c.get("assetId")
            )
        ]

        if not asset_ids:
            print(
                "🔴 Nessuna carta ricevuta."
            )
            print(
                "🔴 OFFERTA DA RIFIUTARE."
            )

            rifiuta_offerta(offer_id)

            completata = True
            return

        # ----------------------------------------------------
        # DETTAGLI
        # ----------------------------------------------------

        cards = dettagli_carte(
            asset_ids
        )

        if cards is None:
            print(
                "⚠️ Dettagli carte "
                "non disponibili."
            )
            print(
                "⚠️ L'offerta verrà "
                "riprovata."
            )
            return

        idonee = []

        print(
            "\n🔎 ANALISI DELLE "
            "CARTE RICEVUTE:"
        )

        for card in cards:
            if analizza_carta(card):
                idonee.append(card)

        totale = len(asset_ids)
        numero_idonee = len(idonee)
        non_idonee = max(
            0,
            totale - numero_idonee
        )

        print(
            "\n----------------------------------------"
        )
        print(
            f"📊 CARTE TOTALI: {totale}"
        )
        print(
            f"📊 CARTE IDONEE: "
            f"{numero_idonee}"
        )
        print(
            f"📊 CARTE NON IDONEE: "
            f"{non_idonee}"
        )

        # ----------------------------------------------------
        # NESSUNA IDONEA
        # ----------------------------------------------------

        if numero_idonee == 0:
            print(
                "\n🔴 OFFERTA DA RIFIUTARE."
            )
            print(
                "   Motivo: nessuna carta "
                "ricevuta è idonea."
            )

            rifiuta_offerta(
                offer_id
            )

            completata = True
            return

        # ----------------------------------------------------
        # CONTROPROPOSTA
        # ----------------------------------------------------

        pagamento_cents = (
            numero_idonee
            * PAGAMENTO_PER_CARTA
        )

        pagamento = (
            Decimal(pagamento_cents)
            / Decimal(100)
        )

        print(
            "\n🟢 OFFERTA IDONEA "
            "ALLA CONTROPROPOSTA"
        )

        print(
            "❌ Noi NON cediamo Kulenovic."
        )

        for card in idonee:
            nome = (
                card.get("name")
                or card.get("slug")
                or "Carta"
            )

            print(
                f"   ✅ Noi riceviamo: "
                f"{nome}"
            )

        print(
            f"💰 Noi paghiamo: "
            f"€{pagamento:.2f}"
        )

        nuova_offerta = (
            prepara_controproposta(
                offer,
                idonee,
                pagamento_cents,
            )
        )

        print(
            "----------------------------------------"
        )

        print(
            f"🟢 CONTROPROPOSTA INVIATA: "
            f"{nuova_offerta}"
        )

        completata = True

    except Exception as e:
        print(
            f"❌ Errore offerta "
            f"{offer_id}: {e}"
        )

    finally:
        with lock:
            in_elaborazione.discard(
                offer_id
            )

            if completata:
                analizzate.add(
                    offer_id
                )


# ============================================================
# MONITOR
# ============================================================

def monitor():
    print("\n🤖 BOT SORARE AVVIATO")
    print("🟢 MODALITÀ REALE ATTIVA")
    print(
        "⚠️ Le operazioni reali "
        "possono trasferire carte/fondi."
    )

    print(
        "💰 REGOLA PREZZO: "
        "€0,30 - €0,80"
    )

    print(
        "💰 PAGAMENTO: "
        "€0,20 per ogni carta idonea"
    )

    print(
        f"🏆 "
        f"{len(set(CAMPIONATI.values()))} "
        f"campionati coperti."
    )

    print(
        "\n========================================"
    )
    print(
        "🔧 VERIFICA CONFIGURAZIONE"
    )
    print(
        "========================================"
    )

    required = (
        ("SORARE_JWT_TOKEN", SORARE_TOKEN),
        ("SORARE_JWT_AUD", SORARE_JWT_AUD),
        ("KULENOVIC_ID", KULENOVIC_ID),
        (
            "SORARE_STARK_PRIVATE_KEY",
            SORARE_STARK_PRIVATE_KEY,
        ),
    )

    for nome, valore in required:
        print(
            f"✅ {nome} presente."
            if valor
            else
            f"❌ {nome} NON presente."
        )

    if SORARE_API_KEY:
        print(
            "✅ SORARE_API_KEY presente."
        )
    else:
        print(
            "🟡 SORARE_API_KEY non presente."
        )

    print(
        f"🟢 DRY_RUN = {DRY_RUN}"
    )

    # --------------------------------------------------------
    # STARK KEY
    # --------------------------------------------------------

    print(
        "\n🔐 VERIFICA CHIAVE STARK"
    )

    if SORARE_STARK_PRIVATE_KEY:
        try:
            key = (
                SORARE_STARK_PRIVATE_KEY
                .removeprefix("0x")
            )

            int(key, 16)

            print(
                "✅ Formato esadecimale "
                "verificato."
            )

        except ValueError:
            print(
                "❌ Chiave Stark "
                "non esadecimale."
            )
            return
    else:
        print(
            "❌ Chiave Stark assente."
        )
        return

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not verifica_account():
        print(
            "❌ Autenticazione "
            "Sorare fallita."
        )
        return

    print(
        "🟢 MONITORAGGIO "
        "OFFERTE ATTIVO."
    )

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:
        try:
            print(
                "\n🔎 Controllo offerte..."
            )

            offers = recupera_offerte()

            if offers is None:
                print(
                    "⚠️ Controllo offerte "
                    "fallito."
                )

            else:
                print(
                    f"📨 Offerte pending "
                    f"ricevute: {len(offers)}"
                )

                for offer in offers:
                    elabora_offerta(
                        offer
                    )

        except Exception as e:
            print(
                f"⚠️ Errore monitor: {e}"
            )

        time.sleep(
            INTERVALLO_CONTROLLO
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():
    return (
        "Bot Sorare attivo in modalità REALE.",
        200,
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "sorare",
        "dry_run": DRY_RUN,
        "monitoraggio": "attivo",
    })


# ============================================================
# AVVIO
# ============================================================

monitor_thread = threading.Thread(
    target=monitor,
    name="sorare-monitor",
    daemon=True,
)

monitor_thread.start()
