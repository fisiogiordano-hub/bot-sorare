import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
import requests

from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()


# ============================================================
# SICUREZZA
#
# DRY_RUN:
#   true  = nessuna operazione reale
#   false = operazioni reali consentite
#
# SWAP_AUTO_ACCEPT:
#   false = lo swap viene valutato ma NON accettato
#   true  = se DRY_RUN=false, lo swap approvabile viene
#           accettato automaticamente
# ============================================================

DRY_RUN = os.getenv(
    "DRY_RUN",
    "false"
).lower() == "true"

SWAP_AUTO_ACCEPT = os.getenv(
    "SWAP_AUTO_ACCEPT",
    "false"
).lower() == "true"


# ============================================================
# AUTOBUY
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

PAY_PER_CARD = 20

MAX_AGE = 28

INTERVAL = 10
TIMEOUT = 25

MIN_LIVE_LISTINGS = 5


# ============================================================
# SWAP
# ============================================================

SWAP_MIN_MULTIPLIER = 1.20
SWAP_MAX_MULTIPLIER = 1.25


# ============================================================
# VERSIONE
# ============================================================

BOT_VERSION = "22.0-SWAP-NO-RETRY"


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATE
#
# Ogni offerta viene processata una sola volta per runtime.
#
# NON esiste più nessun sistema di retry del prezzo.
# ============================================================

processed = set()

state_lock = threading.Lock()

worker_lock = threading.Lock()
worker_started = False


# ============================================================
# USD
# ============================================================

usd_rate = None
usd_rate_time = 0

usd_lock = threading.Lock()

USD_CACHE_SECONDS = 300


# ============================================================
# COVERAGE
# ============================================================

coverage_cache = set()
coverage_time = 0

coverage_lock = threading.Lock()

COVERAGE_CACHE_SECONDS = 3600


# ============================================================
# NORMALIZE
# ============================================================

def normalize(value):

    return str(value or "").strip().lower()


# ============================================================
# MARK OFFER AS PROCESSED
#
# L'offerta viene aggiunta allo stato una sola volta.
#
# IMPORTANTE:
# questo stato vive nella memoria del processo.
# Dopo un restart del servizio, il set viene ricreato.
# ============================================================

def mark_processed(offer_id):

    if not offer_id:
        return

    with state_lock:

        processed.add(
            normalize(offer_id)
        )


def is_processed(offer_id):

    if not offer_id:
        return False

    with state_lock:

        return normalize(offer_id) in processed


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):

    global coverage_cache
    global coverage_time

    now = time.time()

    with coverage_lock:

        cached = set(coverage_cache)
        cached_time = coverage_time

    if (
        not force
        and cached
        and now - cached_time
        < COVERAGE_CACHE_SECONDS
    ):

        return cached

    try:

        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-Bot/{BOT_VERSION}"
            }
        )

        if r.status_code != 200:

            print(
                f"⚠️ Coverage HTTP "
                f"{r.status_code}",
                flush=True
            )

            return cached

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            r.text,
            re.IGNORECASE
        )

        result = {
            normalize(x)
            for x in matches
            if normalize(x)
        }

        if not result:

            print(
                "⚠️ Nessuna competizione "
                "Football trovata",
                flush=True
            )

            return cached

        with coverage_lock:

            coverage_cache = set(result)
            coverage_time = time.time()

        print(
            f"🌐 Sorare Coverage aggiornata: "
            f"{len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:

        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )

        return cached


# ============================================================
# GRAPHQL HEADERS
# ============================================================

def headers():

    if not TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):

        token = "Bearer " + token

    h = {

        "Authorization": token,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            f"Sorare-Bot/{BOT_VERSION}",
    }

    if AUD:

        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):

    payload = {

        "query": query,

        "variables":
            variables or {}
    }

    for attempt in range(3):

        try:

            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP "
                f"{r.status_code}",
                flush=True
            )

            if r.status_code == 429:

                wait = int(
                    r.headers.get(
                        "Retry-After",
                        attempt + 2
                    )
                )

                time.sleep(
                    min(wait, 15)
                )

                continue

            if r.status_code != 200:

                print(
                    f"❌ Sorare HTTP "
                    f"{r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            try:

                data = r.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True
                )

                return None

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:3000],
                    flush=True
                )

            return data

        except requests.RequestException as e:

            print(
                f"❌ HTTP Sorare: {e}",
                flush=True
            )

            time.sleep(
                attempt + 1
            )

        except Exception as e:

            print(
                f"❌ GraphQL: {e}",
                flush=True
            )

            return None

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ Account Sorare non verificato",
            flush=True
        )

        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key account: "
        + (
            "PRESENTE"
            if user.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    return True


# ============================================================
# OFFERTE PENDENTI
#
# senderSide:
#   carte + cash che IL MANAGER CI OFFRE
#
# receiverSide:
#   carte + cash che IL MANAGER RICHIEDE
# ============================================================

def get_offers():

    data = graphql("""
        query {

            currentUser {

                pendingTokenOffersReceived(
                    first: 50
                ) {

                    nodes {

                        id
                        blockchainId
                        status

                        sender {

                            ... on User {

                                slug
                                nickname
                            }
                        }

                        senderSide {

                            amounts {

                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

                            anyCards {

                                assetId
                                slug
                                collection
                            }
                        }

                        receiverSide {

                            amounts {

                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

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
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    return (
        user
        .get(
            "pendingTokenOffersReceived",
            {}
        )
        .get("nodes")
        or []
    )


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_ids):

    ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
        )
    )

    if not ids:

        return []

    data = graphql("""
        query Cards($assetIds: [String!]!) {

            anyCards(
                assetIds: $assetIds
            ) {

                assetId
                slug
                name
                rarityTyped
                seasonYear

                anyPlayer {

                    slug
                    displayName
                    age

                    activeClub {

                        slug
                        name

                        activeCompetitions {

                            slug
                        }
                    }
                }
            }
        }
    """, {
        "assetIds": ids
    })

    if not data or data.get("errors"):

        return []

    return (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )


# ============================================================
# USD -> EUR
# ============================================================

def usd_eur():

    global usd_rate
    global usd_rate_time

    now = time.time()

    with usd_lock:

        if (
            usd_rate
            and now - usd_rate_time
            < USD_CACHE_SECONDS
        ):

            return usd_rate

        try:

            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from": "USD",
                    "to": "EUR"
                },
                timeout=10
            )

            if r.status_code != 200:

                return None

            rate = float(
                (r.json().get("rates") or {})
                .get("EUR")
            )

            if rate <= 0:

                return None

            usd_rate = rate
            usd_rate_time = now

            print(
                f"💱 1 USD = "
                f"{rate:.6f} EUR",
                flush=True
            )

            return rate

        except Exception as e:

            print(
                f"❌ USD/EUR: {e}",
                flush=True
            )

            return None


# ============================================================
# AMOUNT -> EUR CENTS
# ============================================================

def price_eur_cents(amounts):

    if not isinstance(amounts, dict):

        return None

    # EUR diretto

    try:

        eur = int(
            amounts.get("eurCents")
        )

        if eur > 0:

            return eur

    except (
        TypeError,
        ValueError
    ):

        pass

    # USD -> EUR

    try:

        usd = float(
            amounts.get("usdCents")
        )

    except (
        TypeError,
        ValueError
    ):

        usd = 0

    if usd > 0:

        rate = usd_eur()

        if rate:

            return int(
                round(usd * rate)
            )

    # WEI escluso

    if amounts.get("wei") is not None:

        print(
            "🚫 WEI escluso dal calcolo FIAT",
            flush=True
        )

    return None


# ============================================================
# CASH OFFERTO DAL MANAGER
# ============================================================

def get_cash_offered_eur_cents(offer):

    sender_side = (
        offer.get("senderSide")
        or {}
    )

    amounts = (
        sender_side.get("amounts")
        or {}
    )

    value = price_eur_cents(
        amounts
    )

    if value is None:

        return 0

    return value


# ============================================================
# LIVE FLOOR
#
# Restituisce:
#
#   (floor, "valid")
#   (None, "unknown_price")
#   (None, "invalid")
#
# NON esiste più alcun retry.
# ============================================================

def get_live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = normalize(
        player.get("slug")
    )

    rarity = normalize(
        card.get("rarityTyped")
    )

    try:

        season = int(
            card.get("seasonYear")
        )

    except (
        TypeError,
        ValueError
    ):

        return None, "unknown_price"

    if not player_slug or not rarity:

        return None, "unknown_price"

    data = graphql("""
        query LiveSales(
            $playerSlug: String,
            $first: Int
        ) {

            tokens {

                liveSingleSaleOffers(
                    playerSlug: $playerSlug
                    first: $first
                ) {

                    nodes {

                        id

                        senderSide {

                            amounts {

                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

                            anyCards {

                                assetId
                                rarityTyped
                                seasonYear

                                anyPlayer {

                                    slug
                                }
                            }
                        }

                        receiverSide {

                            amounts {

                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }
                        }
                    }
                }
            }
        }
    """, {
        "playerSlug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):

        return None, "unknown_price"

    offers = (
        (
            ((data.get("data") or {})
            .get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        )
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender = (
            offer.get("senderSide")
            or {}
        )

        compatible = False

        for c in (
            sender.get("anyCards")
            or []
        ):

            c_player = normalize(
                (c.get("anyPlayer") or {})
                .get("slug")
            )

            try:

                c_season = int(
                    c.get("seasonYear")
                )

            except (
                TypeError,
                ValueError
            ):

                c_season = None

            if (
                c_player == player_slug
                and normalize(
                    c.get("rarityTyped")
                ) == rarity
                and c_season == season
            ):

                compatible = True

                break

        if not compatible:

            continue

        amounts = (
            (offer.get("receiverSide") or {})
            .get("amounts")
            or {}
        )

        price = price_eur_cents(
            amounts
        )

        if price is not None:

            prices.append(price)

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"      ⚠️ Inserzioni valide: "
            f"{len(prices)}/"
            f"{MIN_LIVE_LISTINGS}"
            f" → PREZZO NON DISPONIBILE",
            flush=True
        )

        return None, "invalid"

    floor = min(prices)

    print(
        f"      📊 Inserzioni valide: "
        f"{len(prices)}",
        flush=True
    )

    print(
        f"      💰 FLOOR LIVE: "
        f"€{floor / 100:.2f}",
        flush=True
    )

    return floor, "valid"


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {

        KSLUG.lower(),

        KASSET.lower()
    }

    if KID:

        wanted.add(
            KID.lower()
        )

    return (

        normalize(
            card.get("assetId")
        ) in wanted

        or

        normalize(
            card.get("slug")
        ) in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def competitions(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    result = []

    for c in (
        club.get("activeCompetitions")
        or []
    ):

        if (
            isinstance(c, dict)
            and c.get("slug")
        ):

            slug = normalize(
                c["slug"]
            )

            if slug:

                result.append(slug)

    return list(
        dict.fromkeys(result)
    )


def covered_competitions(card):

    active = competitions(card)

    coverage = load_coverage()

    covered = [
        x for x in active
        if x in coverage
    ]

    not_covered = [
        x for x in active
        if x not in coverage
    ]

    return (
        active,
        covered,
        not_covered
    )


# ============================================================
# VALID CARD
#
# Usata per le carte che il manager ci offre.
#
# IMPORTANTE:
# Se il prezzo non è disponibile, la carta NON è valida
# e viene esclusa dalla controproposta.
# ============================================================

def valid_card(card):

    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    player = (
        card.get("anyPlayer")
        or {}
    )

    print(
        f"   📄 {name}",
        flush=True
    )

    # ETÀ

    try:

        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError
    ):

        print(
            "      ❌ Età sconosciuta",
            flush=True
        )

        return False, "invalid"

    print(
        f"      🎂 Età: {age}",
        flush=True
    )

    if age >= MAX_AGE:

        print(
            "      ❌ Età troppo alta",
            flush=True
        )

        return False, "invalid"

    # RARITÀ

    rarity = normalize(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
            flush=True
        )

        return False, "invalid"

    # FLOOR

    price, price_reason = get_live_floor(
        card
    )

    if price_reason == "invalid":

        print(
            "      ❌ Prezzo non disponibile "
            "o meno di 5 inserzioni",
            flush=True
        )

        return False, "unknown_price"

    if price is None:

        print(
            "      ❌ Prezzo sconosciuto "
            "→ CARTA ESCLUSA",
            flush=True
        )

        return False, "unknown_price"

    print(
        f"      💰 Floor: "
        f"€{price / 100:.2f}",
        flush=True
    )

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo fuori range",
            flush=True
        )

        return False, "invalid"

    # COVERAGE

    active, covered, not_covered = (
        covered_competitions(card)
    )

    if not active:

        print(
            "      ❌ Nessuna "
            "activeCompetition",
            flush=True
        )

        return False, "invalid"

    print(
        f"      🏆 Active: "
        f"{', '.join(active)}",
        flush=True
    )

    if covered:

        print(
            f"      ✅ Covered: "
            f"{', '.join(covered)}",
            flush=True
        )

    if not_covered:

        print(
            f"      🚫 Non covered: "
            f"{', '.join(not_covered)}",
            flush=True
        )

    if not covered:

        print(
            "      ❌ Nessuna competizione "
            "coperta da Sorare",
            flush=True
        )

        return False, "invalid"

    print(
        "      ✅ CARTA VALIDA",
        flush=True
    )

    return True, "valid"


# ============================================================
# REJECT OFFER
# ============================================================

def reject_offer(offer):

    blockchain_id = normalize(
        offer.get("blockchainId")
    )

    if not blockchain_id:

        print(
            "❌ blockchainId mancante",
            flush=True
        )

        return False

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "reject simulato",
            flush=True
        )

        return True

    data = graphql("""
        mutation Reject(
            $input: rejectOfferInput!
        ) {

            rejectOffer(
                input: $input
            ) {

                tokenOffer {

                    id
                    status
                }

                errors {

                    message
                }
            }
        }
    """, {

        "input": {

            "blockchainId":
                blockchain_id,

            "clientMutationId":
                str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result:

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for e in errors:

            print(
                f"❌ Reject: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )

        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True
    )

    return True


# ============================================================
# SIGN AUTHORIZATIONS
# ============================================================

def sign_authorizations(authorizations):

    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:

        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not STARK:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    script = r'''
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function build(a) {

    const r = a.request;

    if (!r) {

        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        &&
        r.amount != null
    ) {

        r.amount = BigInt(r.amount);
    }

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            r
        );

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            starkexTransferApproval: {

                nonce:
                    r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            starkexLimitOrderApproval: {

                nonce:
                    r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            mangopayWalletTransferApproval: {

                nonce:
                    r.nonce,

                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(build)
    )
);
'''

    p = subprocess.run(

        [
            node,
            "-e",
            script
        ],

        input=json.dumps({

            "privateKey":
                STARK,

            "authorizations":
                authorizations
        }),

        text=True,

        capture_output=True,

        timeout=TIMEOUT
    )

    if p.returncode != 0:

        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        p.stdout
    )


# ============================================================
# COUNTER OFFER AUTOBUY
# ============================================================

def counter_offer(
    offer,
    cards
):

    receiver = normalize(
        (offer.get("sender") or {})
        .get("slug")
    )

    ids = [

        str(c["assetId"]).strip()

        for c in cards

        if c.get("assetId")
    ]

    if not receiver or not ids:

        return False

    amount = (
        len(ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 Controproposta: "
        f"{len(ids)} carta/e "
        f"→ €{amount / 100:.2f}",
        flush=True
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True
        )

        return True

    prepare_input = {

        "receiveAssetIds":
            ids,

        "sendAssetIds":
            [],

        "sendAmount": {

            "amount":
                str(amount),

            "currency":
                "EUR"
        },

        "receiverSlug":
            receiver,

        "settlementCurrencies":
            [
                "EUR"
            ],

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation PrepareOffer(
            $input: prepareOfferInput!
        ) {

            prepareOffer(
                input: $input
            ) {

                authorizations {

                    fingerprint

                    request {

                        __typename

                        ... on StarkexTransferAuthorizationRequest {

                            amount
                            condition
                            expirationTimestamp
                            nonce
                            receiverPublicKey
                            receiverVaultId
                            senderVaultId
                            token

                            feeInfoUser {

                                feeLimit
                                sourceVaultId
                                tokenId
                            }
                        }

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
    """, {

        "input":
            prepare_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer: "
            "nessun risultato",
            flush=True
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for e in errors:

            print(
                f"❌ prepareOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )

        return False

    auth = (
        result.get("authorizations")
        or []
    )

    if not auth:

        print(
            "❌ Nessuna autorizzazione",
            flush=True
        )

        return False

    try:

        approvals = sign_authorizations(
            auth
        )

    except Exception as e:

        print(
            f"❌ Firma: {e}",
            flush=True
        )

        return False

    if not approvals:

        print(
            "❌ Nessuna approval generata",
            flush=True
        )

        return False

    deal_id = str(
        uuid.uuid4()
    )

    create_input = {

        "approvals":
            approvals,

        "dealId":
            deal_id,

        "receiveAssetIds":
            ids,

        "sendAssetIds":
            [],

        "sendAmount": {

            "amount":
                str(amount),

            "currency":
                "EUR"
        },

        "receiverSlug":
            receiver,

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {

            createDirectOffer(
                input: $input
            ) {

                tokenOffer {

                    id
                    blockchainId
                    status
                }

                errors {

                    message
                }
            }
        }
    """, {

        "input":
            create_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result:

        print(
            "❌ createDirectOffer: "
            "nessun risultato",
            flush=True
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for e in errors:

            print(
                f"❌ createDirectOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    if not token_offer.get("id"):

        print(
            "❌ createDirectOffer: "
            "tokenOffer senza ID",
            flush=True
        )

        return False

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{token_offer['id']}",
        flush=True
    )

    print(
        f"💰 €{amount / 100:.2f}",
        flush=True
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True
    )

    return True


# ============================================================
# EXCHANGE RATE
# ============================================================

def get_exchange_rate_id():

    data = graphql("""
        query ConfigQuery {

            config {

                exchangeRate {

                    id
                }
            }
        }
    """)

    exchange_rate = (
        ((data or {}).get("data") or {})
        .get("config")
        or {}
    ).get("exchangeRate")

    if not exchange_rate:

        return None

    return exchange_rate.get("id")


# ============================================================
# PREPARE ACCEPT OFFER
# ============================================================

def prepare_accept_offer(
    offer_id
):

    exchange_rate_id = (
        get_exchange_rate_id()
    )

    if not exchange_rate_id:

        print(
            "❌ ExchangeRate ID "
            "non disponibile",
            flush=True
        )

        return None, None

    input_data = {

        "offerId":
            offer_id,

        "settlementInfo": {

            "currency":
                "WEI",

            "paymentMethod":
                "WALLET",

            "exchangeRateId":
                exchange_rate_id
        }
    }

    data = graphql("""
        mutation PrepareAcceptOffer(
            $input: prepareAcceptOfferInput!
        ) {

            prepareAcceptOffer(
                input: $input
            ) {

                authorizations {

                    fingerprint

                    request {

                        __typename

                        ... on StarkexTransferAuthorizationRequest {

                            amount
                            condition
                            expirationTimestamp
                            nonce
                            receiverPublicKey
                            receiverVaultId
                            senderVaultId
                            token

                            feeInfoUser {

                                feeLimit
                                sourceVaultId
                                tokenId
                            }
                        }

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
    """, {

        "input":
            input_data
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareAcceptOffer")
    )

    if not result:

        print(
            "❌ prepareAcceptOffer: "
            "nessun risultato",
            flush=True
        )

        return None, None

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for e in errors:

            print(
                f"❌ prepareAcceptOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )

        return None, None

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:

        print(
            "❌ prepareAcceptOffer: "
            "nessuna autorizzazione",
            flush=True
        )

        return None, None

    return (
        authorizations,
        exchange_rate_id
    )


# ============================================================
# ACCEPT OFFER
# ============================================================

def accept_offer(
    offer
):

    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:

        print(
            "❌ ID offerta mancante",
            flush=True
        )

        return False

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "ACCETTAZIONE SWAP SIMULATA",
            flush=True
        )

        print(
            f"   Offer ID: {offer_id}",
            flush=True
        )

        return True

    authorizations, exchange_rate_id = (
        prepare_accept_offer(
            offer_id
        )
    )

    if not authorizations:

        return False

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:

        print(
            f"❌ Firma ACCEPT: {e}",
            flush=True
        )

        return False

    if not approvals:

        print(
            "❌ Nessuna approval "
            "per ACCEPT",
            flush=True
        )

        return False

    accept_input = {

        "approvals":
            approvals,

        "offerId":
            offer_id,

        "settlementInfo": {

            "currency":
                "WEI",

            "paymentMethod":
                "WALLET",

            "exchangeRateId":
                exchange_rate_id
        },

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation AcceptOffer(
            $input: acceptOfferInput!
        ) {

            acceptOffer(
                input: $input
            ) {

                tokenOffer {

                    id
                    status
                }

                errors {

                    message
                }
            }
        }
    """, {

        "input":
            accept_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("acceptOffer")
    )

    if not result:

        print(
            "❌ acceptOffer: "
            "nessun risultato",
            flush=True
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for e in errors:

            print(
                f"❌ acceptOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    print(
        "✅ SWAP ACCETTATO",
        flush=True
    )

    print(
        f"   Offer ID: "
        f"{token_offer.get('id')}",
        flush=True
    )

    print(
        f"   Status: "
        f"{token_offer.get('status')}",
        flush=True
    )

    return True


# ============================================================
# SWAP DETECTION
# ============================================================

def is_swap_offer(
    offer
):

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    return bool(
        sender_cards
        and receiver_cards
    )


# ============================================================
# ANALISI SWAP
#
# senderSide:
#   CARTE CHE IL MANAGER CI OFFRE
#
# receiverSide:
#   NOSTRE CARTE CHE IL MANAGER VUOLE
#
# CASH:
#   SOLO QUELLO GIÀ PRESENTE NEL senderSide
#
# NUOVA REGOLA PREZZO:
#
#   - prezzo nostra carta sconosciuto:
#       SWAP RIFIUTATO
#
#   - prezzo carta del manager sconosciuto:
#       CARTA ESCLUSA
#
# NON ESISTE RETRY.
# ============================================================

def analyze_swap(
    offer
):

    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:

        return

    if is_processed(
        offer_id
    ):

        return

    # ========================================================
    # BLOCCO IMMEDIATO
    #
    # L'offerta viene considerata processata appena entra
    # nella funzione.
    #
    # In questo modo non viene ripresa al ciclo successivo,
    # anche se una successiva operazione dovesse fallire.
    # ========================================================

    mark_processed(
        offer_id
    )

    sender_side = (
        offer.get("senderSide")
        or {}
    )

    receiver_side = (
        offer.get("receiverSide")
        or {}
    )

    cards_they_give = (
        sender_side.get("anyCards")
        or []
    )

    cards_we_give = (
        receiver_side.get("anyCards")
        or []
    )

    if not cards_they_give:

        return

    if not cards_we_give:

        return

    print(
        "\n" + "=" * 70,
        flush=True
    )

    print(
        f"🔄 SWAP RICEVUTO: "
        f"{offer_id}",
        flush=True
    )

    sender = (
        offer.get("sender")
        or {}
    )

    print(
        f"👤 Manager: "
        f"{sender.get('nickname') or sender.get('slug')}",
        flush=True
    )

    print(
        "=" * 70,
        flush=True
    )

    # ========================================================
    # ID CARTE
    # ========================================================

    give_ids = [

        c.get("assetId")

        for c in cards_we_give

        if c.get("assetId")
    ]

    receive_ids = [

        c.get("assetId")

        for c in cards_they_give

        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:

        print(
            "❌ ID carte mancanti → SWAP RIFIUTATO",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # DETTAGLI
    # ========================================================

    cards_we_give_details = (
        card_details(
            give_ids
        )
    )

    cards_they_give_details = (
        card_details(
            receive_ids
        )
    )

    if (
        len(cards_we_give_details)
        != len(give_ids)
    ):

        print(
            "❌ Impossibile verificare "
            "tutte le nostre carte",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    if (
        len(cards_they_give_details)
        != len(receive_ids)
    ):

        print(
            "❌ Impossibile verificare "
            "tutte le carte ricevute",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # KULENOVIC PROTETTO
    # ========================================================

    for card in cards_we_give_details:

        if is_kulenovic(card):

            name = (
                card.get("name")
                or card.get("slug")
                or "Kulenovic"
            )

            print(
                "\n🔒 KULENOVIC PROTETTO",
                flush=True
            )

            print(
                f"   📤 {name}",
                flush=True
            )

            print(
                "   🚫 QUESTA CARTA NON PUÒ "
                "ESSERE CEDUTA",
                flush=True
            )

            print(
                "🔴 SWAP RIFIUTATO",
                flush=True
            )

            reject_offer(
                offer
            )

            return

    # ========================================================
    # KULENOVIC DEL MANAGER
    # ========================================================

    for card in cards_they_give_details:

        if is_kulenovic(card):

            name = (
                card.get("name")
                or card.get("slug")
                or "Kulenovic"
            )

            print(
                "\n🎯 KULENOVIC DEL MANAGER",
                flush=True
            )

            print(
                f"   📥 {name}",
                flush=True
            )

            print(
                "   ✅ PUÒ ESSERE RICEVUTO",
                flush=True
            )

    # ========================================================
    # FLOOR NOSTRE CARTE
    #
    # Se il prezzo di una nostra carta non è disponibile,
    # NON possiamo calcolare correttamente il valore ceduto.
    #
    # Quindi lo swap viene rifiutato immediatamente.
    # ========================================================

    total_given_floor = 0

    for card in cards_we_give_details:

        name = (
            card.get("name")
            or card.get("slug")
            or "Carta"
        )

        print(
            f"\n📤 CEDIAMO: {name}",
            flush=True
        )

        floor, reason = (
            get_live_floor(card)
        )

        if floor is None:

            print(
                "   ❌ Floor sconosciuto",
                flush=True
            )

            print(
                "   🔴 IMPOSSIBILE VALUTARE "
                "IL VALORE CEDUTO",
                flush=True
            )

            print(
                "🔴 SWAP RIFIUTATO",
                flush=True
            )

            reject_offer(
                offer
            )

            return

        total_given_floor += floor

        print(
            f"   💰 Floor ceduta: "
            f"€{floor / 100:.2f}",
            flush=True
        )

    # ========================================================
    # FLOOR CARTE MANAGER
    #
    # Qui NON rifiutiamo tutto lo swap se una carta non ha
    # prezzo.
    #
    # La carta viene semplicemente esclusa dal nuovo deal.
    # ========================================================

    valid_received_cards = []

    total_received_floor = 0

    for card in cards_they_give_details:

        name = (
            card.get("name")
            or card.get("slug")
            or "Carta"
        )

        print(
            f"\n📥 RICEVIAMO: {name}",
            flush=True
        )

        ok, reason = valid_card(
            card
        )

        if not ok:

            if reason == "unknown_price":

                print(
                    "   🟡 Prezzo non disponibile",
                    flush=True
                )

                print(
                    "   🚫 CARTA ESCLUSA "
                    "DALLA CONTROPROPOSTA",
                    flush=True
                )

                continue

            print(
                "   ❌ Carta non valida",
                flush=True
            )

            print(
                "   🚫 CARTA ESCLUSA "
                "DALLA CONTROPROPOSTA",
                flush=True
            )

            continue

        floor, floor_reason = (
            get_live_floor(card)
        )

        if floor is None:

            print(
                "   🟡 Floor non disponibile",
                flush=True
            )

            print(
                "   🚫 CARTA ESCLUSA "
                "DALLA CONTROPROPOSTA",
                flush=True
            )

            continue

        valid_received_cards.append(
            card
        )

        total_received_floor += floor

        print(
            f"   💰 Floor ricevuta: "
            f"€{floor / 100:.2f}",
            flush=True
        )

    # ========================================================
    # SE NESSUNA CARTA RICEVUTA È VALIDA
    # ========================================================

    if not valid_received_cards:

        print(
            "\n❌ NESSUNA CARTA RICEVIBILE",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # CASH
    #
    # Solo cash già presente nel senderSide.
    # Il bot NON aggiunge cash.
    # ========================================================

    cash_eur = (
        get_cash_offered_eur_cents(
            offer
        )
    )

    print(
        "\n💶 CASH GIÀ OFFERTO DAL MANAGER: "
        f"€{cash_eur / 100:.2f}",
        flush=True
    )

    # ========================================================
    # TOTALI
    # ========================================================

    total_received = (
        total_received_floor
        + cash_eur
    )

    minimum_required = int(
        round(
            total_given_floor
            * SWAP_MIN_MULTIPLIER
        )
    )

    maximum_allowed = int(
        round(
            total_given_floor
            * SWAP_MAX_MULTIPLIER
        )
    )

    print(
        "\n" + "-" * 70,
        flush=True
    )

    print(
        f"📤 FLOOR TOTALE CEDUTO: "
        f"€{total_given_floor / 100:.2f}",
        flush=True
    )

    print(
        f"📥 FLOOR CARTE RICEVUTE "
        f"UTILIZZABILI: "
        f"€{total_received_floor / 100:.2f}",
        flush=True
    )

    print(
        f"💶 CASH GIÀ OFFERTO: "
        f"€{cash_eur / 100:.2f}",
        flush=True
    )

    print(
        f"📥 VALORE TOTALE RICEVUTO: "
        f"€{total_received / 100:.2f}",
        flush=True
    )

    print(
        f"📈 MINIMO RICHIESTO (+20%): "
        f"€{minimum_required / 100:.2f}",
        flush=True
    )

    print(
        f"📈 MASSIMO CONSENTITO (+25%): "
        f"€{maximum_allowed / 100:.2f}",
        flush=True
    )

    # ========================================================
    # SICUREZZA
    # ========================================================

    if total_given_floor <= 0:

        print(
            "❌ Valore ceduto non valido",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # DECISIONE
    # ========================================================

    if total_received < minimum_required:

        print(
            "\n❌ SWAP RIFIUTATO",
            flush=True
        )

        print(
            "   Motivo: valore ricevuto "
            "inferiore al +20%",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    if total_received > maximum_allowed:

        print(
            "\n❌ SWAP RIFIUTATO",
            flush=True
        )

        print(
            "   Motivo: valore ricevuto "
            "superiore al +25%",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # APPROVABILE
    # ========================================================

    percentage = (

        (
            total_received
            / total_given_floor
        )

        - 1

    ) * 100

    print(
        "\n✅ SWAP APPROVABILE",
        flush=True
    )

    print(
        f"📈 Premium effettivo: "
        f"+{percentage:.2f}%",
        flush=True
    )

    # ========================================================
    # AUTO ACCEPT
    # ========================================================

    if not SWAP_AUTO_ACCEPT:

        print(
            "🛑 SWAP AUTO ACCEPT: OFF",
            flush=True
        )

        print(
            "   Nessuna azione eseguita",
            flush=True
        )

        return

    print(
        "⚠️ SWAP AUTO ACCEPT: ON",
        flush=True
    )

    if accept_offer(
        offer
    ):

        print(
            "✅ Accettazione completata",
            flush=True
        )

    else:

        print(
            "❌ Accettazione SWAP fallita",
            flush=True
        )


# ============================================================
# AUTOBUY PROCESS
#
# NUOVA LOGICA:
#
# Ogni carta viene valutata singolarmente.
#
# Se il prezzo non viene trovato:
#   → carta esclusa
#
# Le altre carte possono comunque entrare
# nella controproposta.
#
# Se nessuna carta rimane:
#   → offerta rifiutata.
# ============================================================

def process_autobuy_offer(
    offer
):

    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:

        return

    if is_processed(
        offer_id
    ):

        return

    # ========================================================
    # BLOCCO IMMEDIATO
    # ========================================================

    mark_processed(
        offer_id
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    # L'AutoBuy riguarda offerte che
    # richiedono il nostro Kulenovic.

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):

        return

    print(
        f"\n📨 AUTOBUY OFFER "
        f"{offer_id}",
        flush=True
    )

    sender = (
        offer.get("sender")
        or {}
    )

    print(
        f"👤 Manager: "
        f"{sender.get('nickname') or sender.get('slug')}",
        flush=True
    )

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    ids = [

        c.get("assetId")

        for c in sender_cards

        if c.get("assetId")
    ]

    if not ids:

        print(
            "❌ Nessuna carta ricevuta",
            flush=True
        )

        print(
            "🔴 OFFERTA RIFIUTATA",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    cards = card_details(
        ids
    )

    if len(cards) != len(ids):

        print(
            "❌ Impossibile verificare "
            "tutte le carte",
            flush=True
        )

        print(
            "🔴 OFFERTA RIFIUTATA",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # VALUTAZIONE INDIVIDUALE
    # ========================================================

    valid = []

    for card in cards:

        try:

            ok, reason = valid_card(
                card
            )

            if ok:

                valid.append(card)

                continue

            if reason == "unknown_price":

                print(
                    "🟡 Prezzo non disponibile "
                    "→ CARTA ESCLUSA",
                    flush=True
                )

                continue

            print(
                "🚫 Carta non valida "
                "→ CARTA ESCLUSA",
                flush=True
            )

        except Exception as e:

            print(
                f"❌ Errore carta: {e}",
                flush=True
            )

            print(
                "🚫 Carta esclusa",
                flush=True
            )

    # ========================================================
    # NESSUNA CARTA VALIDA
    # ========================================================

    if not valid:

        print(
            "\n🔴 NESSUNA CARTA VALIDA",
            flush=True
        )

        print(
            "🔴 OFFERTA RIFIUTATA",
            flush=True
        )

        reject_offer(
            offer
        )

        return

    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    print(
        f"\n✅ Carte utilizzabili: "
        f"{len(valid)}/{len(cards)}",
        flush=True
    )

    if len(valid) < len(cards):

        print(
            "🚫 Una o più carte sono state "
            "escluse dalla controproposta",
            flush=True
        )

    if counter_offer(
        offer,
        valid
    ):

        if reject_offer(
            offer
        ):

            print(
                "✅ Offerta originale "
                "rifiutata dopo la controproposta",
                flush=True
            )

        else:

            print(
                "⚠️ Controproposta creata "
                "ma originale non rifiutata",
                flush=True
            )

    else:

        print(
            "❌ Controproposta fallita",
            flush=True
        )

        print(
            "🔴 Offerta originale rifiutata",
            flush=True
        )

        reject_offer(
            offer
        )


# ============================================================
# PROCESS OFFER
#
# PRIORITÀ:
#
# 1. Se ci sono carte su entrambe le parti:
#       → SWAP
#
# 2. Altrimenti se il manager vuole il nostro Kulenovic:
#       → AUTOBUY
#
# Ogni ramo marca l'offerta come processata.
# ============================================================

def process_offer(
    offer
):

    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:

        return

    if is_processed(
        offer_id
    ):

        return

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    # ========================================================
    # SWAP
    # ========================================================

    if (
        sender_cards
        and receiver_cards
    ):

        analyze_swap(
            offer
        )

        return

    # ========================================================
    # AUTOBUY
    # ========================================================

    if any(
        is_kulenovic(c)
        for c in receiver_cards
    ):

        process_autobuy_offer(
            offer
        )

        return

    # ========================================================
    # OFFERTA NON INTERESSANTE
    #
    # La marchiamo comunque come processata per evitare
    # che venga rivalutata a ogni ciclo.
    # ========================================================

    mark_processed(
        offer_id
    )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 BOT AVVIATO",
        flush=True
    )

    print(
        f"📦 VERSIONE BOT: "
        f"{BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        f"🔄 SWAP_AUTO_ACCEPT="
        f"{SWAP_AUTO_ACCEPT}",
        flush=True
    )

    print(
        "💰 AutoBuy pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} "
        "per carta",
        flush=True
    )

    print(
        "📊 AutoBuy floor: "
        f"€{MIN_PRICE / 100:.2f}"
        f" - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"🎂 Età: < {MAX_AGE}",
        flush=True
    )

    print(
        f"📊 Inserzioni minime: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        f"🔄 SWAP: "
        f"+{(SWAP_MIN_MULTIPLIER - 1) * 100:.0f}%"
        f" / "
        f"+{(SWAP_MAX_MULTIPLIER - 1) * 100:.0f}%",
        flush=True
    )

    print(
        "💶 SWAP CASH: "
        "solo cash già offerto dal manager",
        flush=True
    )

    print(
        "🚫 SWAP NON aggiunge cash",
        flush=True
    )

    print(
        "💰 PRICE SOURCE: "
        "liveSingleSaleOffers",
        flush=True
    )

    print(
        "🎯 MATCH PRICE: "
        "player + rarity + season",
        flush=True
    )

    print(
        "💶 EUR: eurCents",
        flush=True
    )

    print(
        "💵 USD: usdCents → EUR",
        flush=True
    )

    print(
        "🚫 WEI: escluso",
        flush=True
    )

    print(
        "🚫 PRICE UNKNOWN: "
        "NO RETRY / CARTA ESCLUSA",
        flush=True
    )

    print(
        "🔒 KULENOVIC: "
        "PROTETTO SE CEDUTO",
        flush=True
    )

    print(
        "🎯 KULENOVIC MANAGER: "
        "RICEVIBILE",
        flush=True
    )

    covered = load_coverage(
        force=True
    )

    if not covered:

        print(
            "❌ IMPOSSIBILE CARICARE "
            "SORARE COVERAGE",
            flush=True
        )

        print(
            "❌ Il bot NON partirà",
            flush=True
        )

        return

    print(
        f"🏆 Competizioni Football coperte: "
        f"{len(covered)}",
        flush=True
    )

    if not check_account():

        print(
            "❌ Account non verificato",
            flush=True
        )

        return

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True
            )

            for offer in offers:

                try:

                    process_offer(
                        offer
                    )

                except Exception as e:

                    offer_id = normalize(
                        offer.get("id")
                    )

                    print(
                        f"❌ Errore offerta "
                        f"{offer_id}: {e}",
                        flush=True
                    )

                    # Anche in caso di errore imprevisto,
                    # l'offerta NON viene riprocessata.
                    mark_processed(
                        offer_id
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as e:

            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# START WORKER
# ============================================================

def start_worker():

    global worker_started

    with worker_lock:

        if worker_started:

            return

        worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    with coverage_lock:

        covered = set(
            coverage_cache
        )

    with state_lock:

        processed_count = len(
            processed
        )

    return jsonify({

        "status":
            "online",

        "bot":
            "sorare",

        "version":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "swap_auto_accept":
            SWAP_AUTO_ACCEPT,

        "pay_per_card_cents":
            PAY_PER_CARD,

        "interval_seconds":
            INTERVAL,

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "max_age":
            MAX_AGE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "swap_min_multiplier":
            SWAP_MIN_MULTIPLIER,

        "swap_max_multiplier":
            SWAP_MAX_MULTIPLIER,

        "swap_cash_mode":
            "MANAGER_OFFERED_CASH_ONLY",

        "competition_mode":
            "SORARE_OFFICIAL_FOOTBALL_COVERAGE",

        "covered_competitions_count":
            len(covered),

        "covered_competitions":
            sorted(covered),

        "coverage_source":
            COVERAGE_URL,

        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

        "price_eur_mode":
            "EUR_DIRECT_OR_USD_CONVERTED",

        "usd_conversion":
            True,

        "wei_conversion":
            False,

        "wei_excluded_from_fiat_floor":
            True,

        "latest_english_auction_fallback":
            False,

        "public_min_prices":
            False,

        "lowest_price_card":
            False,

        "token_prices":
            False,

        "unknown_price_action":
            "EXCLUDE_CARD_NO_RETRY",

        "unknown_price_retry_seconds":
            False,

        "offer_reprocessing":
            False,

        "processed_offers_in_memory":
            processed_count,

        "prepare_offer_type":
            False,

        "create_direct_offer_deal_id":
            True,

        "accept_offer_enabled":
            True
    })


@app.get("/health")
def health():

    with coverage_lock:

        coverage_loaded = bool(
            coverage_cache
        )

    with state_lock:

        processed_count = len(
            processed
        )

    return jsonify({

        "status":
            "ok",

        "bot":
            "running",

        "version":
            BOT_VERSION,

        "worker_started":
            worker_started,

        "coverage_loaded":
            coverage_loaded,

        "dry_run":
            DRY_RUN,

        "swap_auto_accept":
            SWAP_AUTO_ACCEPT,

        "offer_reprocessing":
            False,

        "processed_offers_in_memory":
            processed_count
    })


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
