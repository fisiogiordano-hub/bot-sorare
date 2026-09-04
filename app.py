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

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SWAP_AUTO_ACCEPT = os.getenv("SWAP_AUTO_ACCEPT", "false").lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28

INTERVAL = 10
TIMEOUT = 25
MIN_LIVE_LISTINGS = 5

SWAP_MIN = 1.20
SWAP_MAX = 1.25

BOT_VERSION = "22.1-SWAP"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

usd_rate = None
usd_rate_time = 0
usd_lock = threading.Lock()

worker_started = False
worker_lock = threading.Lock()


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    token = TOKEN
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
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
        "variables": variables or {}
    }

    for attempt in range(3):

        try:
            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                wait = int(
                    r.headers.get("Retry-After", attempt + 2)
                )
                time.sleep(min(wait, 15))
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            try:
                data = r.json()
            except ValueError:
                print("❌ JSON Sorare non valido", flush=True)
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
            print(f"❌ HTTP Sorare: {e}", flush=True)
            time.sleep(attempt + 1)

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            return None

    return None


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):

    global coverage_cache, coverage_time

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and now - coverage_time < 3600
        ):
            return set(coverage_cache)

        cached = set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={"User-Agent": f"Sorare-Bot/{BOT_VERSION}"}
        )

        if r.status_code != 200:
            print(
                f"⚠️ Coverage HTTP {r.status_code}",
                flush=True
            )
            return cached

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            r.text,
            re.IGNORECASE
        )

        result = {
            norm(x) for x in matches if norm(x)
        }

        if not result:
            print(
                "⚠️ Nessuna competizione Football trovata",
                flush=True
            )
            return cached

        with coverage_lock:
            coverage_cache = result
            coverage_time = time.time()

        print(
            f"🌐 Sorare Coverage aggiornata: "
            f"{len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:
        print(f"⚠️ Coverage: {e}", flush=True)
        return cached


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
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key account: "
        + ("PRESENTE" if user.get("starkKey")
           else "NON DISPONIBILE"),
        flush=True
    )

    return True


# ============================================================
# OFFERTE
# ============================================================

def get_offers():

    data = graphql("""
        query {
            currentUser {
                pendingTokenOffersReceived(first: 50) {
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
        user.get("pendingTokenOffersReceived", {})
        .get("nodes")
        or []
    )


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_ids):

    ids = list(dict.fromkeys(
        str(x).strip()
        for x in asset_ids
        if x
    ))

    if not ids:
        return []

    data = graphql("""
        query Cards($assetIds: [String!]!) {
            anyCards(assetIds: $assetIds) {
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
    """, {"assetIds": ids})

    if not data or data.get("errors"):
        return []

    return (
        ((data.get("data") or {}).get("anyCards"))
        or []
    )


# ============================================================
# USD / EUR
# ============================================================

def usd_eur():

    global usd_rate, usd_rate_time

    now = time.time()

    with usd_lock:

        if usd_rate and now - usd_rate_time < 300:
            return usd_rate

        try:
            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={"from": "USD", "to": "EUR"},
                timeout=10
            )

            if r.status_code != 200:
                return None

            rate = float(
                (r.json().get("rates") or {}).get("EUR")
            )

            if rate <= 0:
                return None

            usd_rate = rate
            usd_rate_time = now

            print(
                f"💱 1 USD = {rate:.6f} EUR",
                flush=True
            )

            return rate

        except Exception as e:
            print(f"❌ USD/EUR: {e}", flush=True)
            return None


def price_eur_cents(amounts):

    if not isinstance(amounts, dict):
        return None

    try:
        eur = int(amounts.get("eurCents"))
        if eur > 0:
            return eur
    except (TypeError, ValueError):
        pass

    try:
        usd = float(amounts.get("usdCents"))
    except (TypeError, ValueError):
        usd = 0

    if usd > 0:
        rate = usd_eur()
        if rate:
            return int(round(usd * rate))

    return None


def cash_offered(offer):

    amounts = (
        (offer.get("senderSide") or {})
        .get("amounts")
        or {}
    )

    return price_eur_cents(amounts) or 0


# ============================================================
# LIVE FLOOR
# ============================================================

def get_live_floor(card):

    player = card.get("anyPlayer") or {}
    player_slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except (TypeError, ValueError):
        return None

    if not player_slug or not rarity:
        return None

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
        return None

    offers = (
        (
            ((data.get("data") or {}).get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        )
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender = offer.get("senderSide") or {}

        match = False

        for c in sender.get("anyCards") or []:

            try:
                c_season = int(c.get("seasonYear"))
            except (TypeError, ValueError):
                continue

            if (
                norm((c.get("anyPlayer") or {}).get("slug"))
                == player_slug
                and norm(c.get("rarityTyped")) == rarity
                and c_season == season
            ):
                match = True
                break

        if not match:
            continue

        amount = (
            (offer.get("receiverSide") or {})
            .get("amounts")
            or {}
        )

        price = price_eur_cents(amount)

        if price is not None:
            prices.append(price)

    if len(prices) < MIN_LIVE_LISTINGS:
        print(
            f"      ⚠️ Inserzioni valide: "
            f"{len(prices)}/{MIN_LIVE_LISTINGS}",
            flush=True
        )
        return None

    floor = min(prices)

    print(
        f"      💰 FLOOR LIVE: €{floor / 100:.2f}",
        flush=True
    )

    return floor


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        KSLUG.lower(),
        KASSET.lower()
    }

    if KID:
        wanted.add(KID.lower())

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def covered(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active = [
        norm(c.get("slug"))
        for c in club.get("activeCompetitions") or []
        if isinstance(c, dict) and c.get("slug")
    ]

    active = list(dict.fromkeys(active))
    coverage = load_coverage()

    return active, [x for x in active if x in coverage]


# ============================================================
# VALID CARD
# ============================================================

def valid_card(card):

    name = card.get("name") or card.get("slug") or "Carta"
    player = card.get("anyPlayer") or {}

    print(f"   📄 {name}", flush=True)

    try:
        age = int(player.get("age"))
    except (TypeError, ValueError):
        print("      ❌ Età sconosciuta", flush=True)
        return False, None

    if age >= MAX_AGE:
        print("      ❌ Età troppo alta", flush=True)
        return False, None

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        print("      ❌ Rarità non LIMITED", flush=True)
        return False, None

    floor = get_live_floor(card)

    if floor is None:
        print(
            "      ❌ Prezzo non trovato",
            flush=True
        )
        return False, None

    print(
        f"      💰 Floor: €{floor / 100:.2f}",
        flush=True
    )

    if not MIN_PRICE <= floor <= MAX_PRICE:
        print("      ❌ Prezzo fuori range", flush=True)
        return False, floor

    active, cov = covered(card)

    if not active or not cov:
        print(
            "      ❌ Nessuna competizione coperta",
            flush=True
        )
        return False, floor

    print("      ✅ CARTA VALIDA", flush=True)

    return True, floor


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):

    blockchain_id = norm(
        offer.get("blockchainId")
    )

    if not blockchain_id:
        print("❌ blockchainId mancante", flush=True)
        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: reject simulato",
            flush=True
        )
        return True

    data = graphql("""
        mutation Reject($input: rejectOfferInput!) {
            rejectOffer(input: $input) {
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
            "blockchainId": blockchain_id,
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ Reject: {e.get('message', 'Errore')}",
                flush=True
            )
        return False

    print("✅ Offerta rifiutata", flush=True)
    return True


# ============================================================
# SIGN
# ============================================================

def sign_authorizations(authorizations):

    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    if not STARK:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    script = r'''
const fs = require("fs");
const { signAuthorizationRequest } = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function build(a) {

    const r = a.request;

    if (!r)
        throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        && r.amount != null
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
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
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
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
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
            fingerprint: a.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: r.nonce,
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
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip() or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(offer, cards):

    receiver = norm(
        (offer.get("sender") or {}).get("slug")
    )

    ids = [
        str(c["assetId"]).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not ids:
        return False

    amount = len(ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: "
        f"{len(ids)} carta/e → €{amount / 100:.2f}",
        flush=True
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: controproposta simulata",
            flush=True
        )
        return True

    prepare_input = {
        "receiveAssetIds": ids,
        "sendAssetIds": [],
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR"
        },
        "receiverSlug": receiver,
        "settlementCurrencies": ["EUR"],
        "clientMutationId": str(uuid.uuid4())
    }

    data = graphql("""
        mutation PrepareOffer($input: prepareOfferInput!) {
            prepareOffer(input: $input) {
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
    """, {"input": prepare_input})

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print("❌ prepareOffer fallito", flush=True)
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ prepareOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )
        return False

    auth = result.get("authorizations") or []

    if not auth:
        print(
            "❌ Nessuna autorizzazione",
            flush=True
        )
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return False

    if not approvals:
        return False

    create_input = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "receiveAssetIds": ids,
        "sendAssetIds": [],
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR"
        },
        "receiverSlug": receiver,
        "clientMutationId": str(uuid.uuid4())
    }

    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {
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
    """, {"input": create_input})

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ createDirectOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )
        return False

    token_offer = result.get("tokenOffer") or {}

    if not token_offer.get("id"):
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
# ACCEPT SWAP
# ============================================================

def get_exchange_rate_id():

    data = graphql("""
        query {
            config {
                exchangeRate {
                    id
                }
            }
        }
    """)

    return (
        (
            ((data or {}).get("data") or {})
            .get("config")
            or {}
        )
        .get("exchangeRate", {})
        .get("id")
    )


def accept_offer(offer):

    offer_id = norm(offer.get("id"))

    if not offer_id:
        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: ACCETTAZIONE SWAP SIMULATA",
            flush=True
        )
        return True

    exchange_rate_id = get_exchange_rate_id()

    if not exchange_rate_id:
        print(
            "❌ ExchangeRate ID non disponibile",
            flush=True
        )
        return False

    settlement = {
        "currency": "WEI",
        "paymentMethod": "WALLET",
        "exchangeRateId": exchange_rate_id
    }

    data = graphql("""
        mutation PrepareAcceptOffer(
            $input: prepareAcceptOfferInput!
        ) {
            prepareAcceptOffer(input: $input) {

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
        "input": {
            "offerId": offer_id,
            "settlementInfo": settlement
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareAcceptOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ prepareAcceptOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )
        return False

    auth = result.get("authorizations") or []

    if not auth:
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma ACCEPT: {e}", flush=True)
        return False

    data = graphql("""
        mutation AcceptOffer($input: acceptOfferInput!) {
            acceptOffer(input: $input) {
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
            "approvals": approvals,
            "offerId": offer_id,
            "settlementInfo": settlement,
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("acceptOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ acceptOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True
            )
        return False

    print("✅ SWAP ACCETTATO", flush=True)
    return True


# ============================================================
# SWAP
#
# REGOLA:
# SE ANCHE UNA SOLA CARTA NON HA PREZZO
# → RIFIUTO IMMEDIATO.
# ============================================================

def analyze_swap(offer):

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

    if not sender_cards or not receiver_cards:
        return

    offer_id = norm(offer.get("id"))

    print(
        f"\n🔄 SWAP RICEVUTO: {offer_id}",
        flush=True
    )

    # Carte che cediamo
    give_ids = [
        c.get("assetId")
        for c in receiver_cards
        if c.get("assetId")
    ]

    # Carte che riceviamo
    receive_ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:
        reject_offer(offer)
        return

    give = card_details(give_ids)
    receive = card_details(receive_ids)

    if len(give) != len(give_ids) or len(receive) != len(receive_ids):
        print(
            "❌ Impossibile verificare tutte le carte → RIFIUTO",
            flush=True
        )
        reject_offer(offer)
        return

    # --------------------------------------------------------
    # KULENOVIC PROTETTO
    # --------------------------------------------------------

    if any(is_kulenovic(c) for c in give):
        print(
            "🔒 KULENOVIC PROTETTO → SWAP RIFIUTATO",
            flush=True
        )
        reject_offer(offer)
        return

    # --------------------------------------------------------
    # PREZZI CARTE CEDUTE
    # --------------------------------------------------------

    given_total = 0

    for card in give:

        floor = get_live_floor(card)

        if floor is None:
            print(
                "❌ Prezzo mancante su carta ceduta "
                "→ SWAP RIFIUTATO",
                flush=True
            )
            reject_offer(offer)
            return

        given_total += floor

    # --------------------------------------------------------
    # CARTE RICEVUTE
    # --------------------------------------------------------

    received_total = 0

    for card in receive:

        ok, floor = valid_card(card)

        # Se prezzo mancante:
        # RIFIUTO IMMEDIATO.
        if floor is None:
            print(
                "❌ Prezzo mancante su carta ricevuta "
                "→ SWAP RIFIUTATO",
                flush=True
            )
            reject_offer(offer)
            return

        # Carta con prezzo trovato ma fuori dai parametri
        # → swap rifiutato.
        if not ok:
            print(
                "❌ Carta ricevuta non valida "
                "→ SWAP RIFIUTATO",
                flush=True
            )
            reject_offer(offer)
            return

        received_total += floor

    if given_total <= 0:
        reject_offer(offer)
        return

    # --------------------------------------------------------
    # CASH
    # --------------------------------------------------------

    cash = cash_offered(offer)
    total_received = received_total + cash

    minimum = int(round(given_total * SWAP_MIN))
    maximum = int(round(given_total * SWAP_MAX))

    print(
        f"\n📤 Ceduto: €{given_total / 100:.2f}",
        flush=True
    )

    print(
        f"📥 Carte ricevute: €{received_total / 100:.2f}",
        flush=True
    )

    print(
        f"💶 Cash manager: €{cash / 100:.2f}",
        flush=True
    )

    print(
        f"📥 Totale ricevuto: €{total_received / 100:.2f}",
        flush=True
    )

    print(
        f"📈 Range richiesto: "
        f"€{minimum / 100:.2f} - "
        f"€{maximum / 100:.2f}",
        flush=True
    )

    # --------------------------------------------------------
    # DECISIONE
    # --------------------------------------------------------

    if total_received < minimum:
        print(
            "🔴 SWAP RIFIUTATO: sotto +20%",
            flush=True
        )
        reject_offer(offer)
        return

    if total_received > maximum:
        print(
            "🔴 SWAP RIFIUTATO: sopra +25%",
            flush=True
        )
        reject_offer(offer)
        return

    premium = (
        total_received / given_total - 1
    ) * 100

    print(
        f"✅ SWAP APPROVABILE: +{premium:.2f}%",
        flush=True
    )

    if not SWAP_AUTO_ACCEPT:
        print(
            "🛑 SWAP_AUTO_ACCEPT=False → nessuna azione",
            flush=True
        )
        return

    accept_offer(offer)


# ============================================================
# AUTOBUY
#
# REGOLA:
# - carte con prezzo valido → controproposta
# - carte senza prezzo → escluse
# - se NESSUNA carta ha prezzo valido → rifiuto
# ============================================================

def process_autobuy(offer):

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    if not any(is_kulenovic(c) for c in receiver_cards):
        return

    offer_id = norm(offer.get("id"))

    print(
        f"\n📨 AUTOBUY OFFER {offer_id}",
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
        reject_offer(offer)
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "⚠️ Impossibile verificare tutte le carte",
            flush=True
        )
        return

    valid = []

    for card in cards:

        ok, floor = valid_card(card)

        # Prezzo assente:
        # carta semplicemente esclusa.
        if floor is None:
            print(
                "🟡 Carta esclusa: prezzo non trovato",
                flush=True
            )
            continue

        if ok:
            valid.append(card)

    # NESSUNA carta valida
    if not valid:
        print(
            "🔴 Nessuna carta con prezzo valido "
            "→ RIFIUTO",
            flush=True
        )
        reject_offer(offer)
        return

    # Almeno una carta valida
    if counter_offer(offer, valid):

        reject_offer(offer)

    else:

        print(
            "🟡 Controproposta fallita "
            "→ originale lasciata pending",
            flush=True
        )


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):

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

    # SWAP
    if sender_cards and receiver_cards:
        analyze_swap(offer)
        return

    # AUTOBUY
    if any(is_kulenovic(c) for c in receiver_cards):
        process_autobuy(offer)


# ============================================================
# WORKER
# ============================================================

def worker():

    print("🤖 BOT AVVIATO", flush=True)
    print(
        f"📦 VERSIONE BOT: {BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        f"🔄 SWAP_AUTO_ACCEPT={SWAP_AUTO_ACCEPT}",
        flush=True
    )

    print(
        f"💰 AutoBuy: €{PAY_PER_CARD / 100:.2f}/carta",
        flush=True
    )

    print(
        f"📊 AutoBuy floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"🎂 Età: < {MAX_AGE}",
        flush=True
    )

    print(
        f"📊 Inserzioni minime: {MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        f"🔄 SWAP: "
        f"+{(SWAP_MIN - 1) * 100:.0f}% / "
        f"+{(SWAP_MAX - 1) * 100:.0f}%",
        flush=True
    )

    print(
        "💶 SWAP CASH: solo cash già offerto dal manager",
        flush=True
    )

    print(
        "🚫 SWAP NON aggiunge cash",
        flush=True
    )

    print(
        "💰 PRICE SOURCE: liveSingleSaleOffers",
        flush=True
    )

    print(
        "🎯 MATCH PRICE: player + rarity + season",
        flush=True
    )

    print(
        "💶 EUR: eurCents | 💵 USD: usdCents → EUR",
        flush=True
    )

    print(
        "🚫 WEI: escluso",
        flush=True
    )

    print(
        "🔒 KULENOVIC: PROTETTO SE CEDUTO",
        flush=True
    )

    covered_comp = load_coverage(force=True)

    if not covered_comp:
        print(
            "❌ Coverage non disponibile → bot fermato",
            flush=True
        )
        return

    print(
        f"🏆 Competizioni Football coperte: "
        f"{len(covered_comp)}",
        flush=True
    )

    if not check_account():
        return

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: {len(offers)}",
                flush=True
            )

            for offer in offers:

                try:
                    process_offer(offer)

                except Exception as e:
                    print(
                        f"❌ Errore offerta: {e}",
                        flush=True
                    )

            time.sleep(INTERVAL)

        except Exception as e:

            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(INTERVAL)


# ============================================================
# START
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
        covered = set(coverage_cache)

    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": BOT_VERSION,

        "dry_run": DRY_RUN,
        "swap_auto_accept": SWAP_AUTO_ACCEPT,

        "pay_per_card_cents": PAY_PER_CARD,

        "interval_seconds": INTERVAL,

        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "max_age": MAX_AGE,
        "min_live_listings": MIN_LIVE_LISTINGS,

        "swap_min_multiplier": SWAP_MIN,
        "swap_max_multiplier": SWAP_MAX,

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

        "wei_excluded_from_fiat_floor":
            True,

        "unknown_price_action":
            "REJECT_SWAP_OR_EXCLUDE_AUTOBUY_CARD",

        "accept_offer_enabled":
            True
    })


@app.get("/health")
def health():

    with coverage_lock:
        coverage_loaded = bool(coverage_cache)

    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
        "worker_started": worker_started,
        "coverage_loaded": coverage_loaded,
        "dry_run": DRY_RUN,
        "swap_auto_accept": SWAP_AUTO_ACCEPT
    })


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
