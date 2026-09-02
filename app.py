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

# =========================
# CONFIG
# =========================

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 25
UNKNOWN_PRICE_RETRY = 60

# NUOVO: minimo numero di inserzioni live richieste
MIN_LIVE_LISTINGS = 5

BOT_VERSION = "20.7"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# =========================
# STATE
# =========================

processed = set()
unknown_price = {}

state_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

usd_rate = None
usd_rate_time = 0
usd_lock = threading.Lock()

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

COVERAGE_CACHE_SECONDS = 3600
USD_CACHE_SECONDS = 300


# =========================
# COVERAGE
# =========================

def normalize(value):
    return str(value or "").strip().lower()


def load_coverage(force=False):
    global coverage_cache, coverage_time

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and now - coverage_time < COVERAGE_CACHE_SECONDS
        ):
            return set(coverage_cache)

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
                return set(coverage_cache)

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
                    "⚠️ Nessuna competizione Football trovata",
                    flush=True
                )
                return set(coverage_cache)

            coverage_cache = result
            coverage_time = now

            print(
                f"🌐 Sorare Coverage aggiornata: {len(result)} competizioni",
                flush=True
            )

            return set(result)

        except Exception as e:
            print(f"⚠️ Coverage: {e}", flush=True)
            return set(coverage_cache)


# =========================
# GRAPHQL
# =========================

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

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                wait = int(
                    r.headers.get("Retry-After", attempt + 2)
                )
                time.sleep(min(wait, 15))
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: {r.text[:500]}",
                    flush=True
                )
                time.sleep(attempt + 1)
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
            time.sleep(attempt + 1)

        except Exception as e:
            print(
                f"❌ GraphQL: {e}",
                flush=True
            )
            return None

    return None


# =========================
# ACCOUNT / OFFERS
# =========================

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
        f"✅ Sorare: {user.get('nickname') or user.get('slug')}",
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


# =========================
# CARD DETAILS
# =========================

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


# =========================
# USD -> EUR
# =========================

def usd_eur():
    global usd_rate, usd_rate_time

    now = time.time()

    with usd_lock:
        if (
            usd_rate
            and now - usd_rate_time < USD_CACHE_SECONDS
        ):
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
            print(
                f"❌ USD/EUR: {e}",
                flush=True
            )
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

    if amounts.get("wei") is not None:
        print(
            "🚫 WEI escluso dal floor FIAT",
            flush=True
        )

    return None


# =========================
# LIVE FLOOR
# =========================

def get_live_floor(card):
    player = card.get("anyPlayer") or {}

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

    except (TypeError, ValueError):
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
            ((data.get("data") or {}).get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        ).get("nodes")
        or []
    )

    prices = []

    for offer in offers:
        sender = offer.get("senderSide") or {}

        compatible = False

        for c in sender.get("anyCards") or []:
            c_player = normalize(
                (c.get("anyPlayer") or {}).get("slug")
            )

            try:
                c_season = int(
                    c.get("seasonYear")
                )

            except (TypeError, ValueError):
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

        price = price_eur_cents(amounts)

        if price is not None:
            prices.append(price)

    # ==========================================
    # NUOVO CONTROLLO:
    # ALMENO 5 INSERZIONI VALIDE
    # ==========================================

    if len(prices) < MIN_LIVE_LISTINGS:
        print(
            f"      ⚠️ Inserzioni valide: {len(prices)}/{MIN_LIVE_LISTINGS} "
            f"→ CARTA ESCLUSA",
            flush=True
        )

        return None, "invalid"

    floor = min(prices)

    print(
        f"      📊 Inserzioni valide: {len(prices)}",
        flush=True
    )

    print(
        f"      💰 FLOOR LIVE: €{floor / 100:.2f}",
        flush=True
    )

    return floor, "valid"


# =========================
# KULENOVIC / COMPETITIONS
# =========================

def is_kulenovic(card):
    wanted = {
        KSLUG.lower(),
        KASSET.lower()
    }

    if KID:
        wanted.add(KID.lower())

    return (
        normalize(card.get("assetId")) in wanted
        or normalize(card.get("slug")) in wanted
    )


def competitions(card):
    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    result = []

    for c in club.get("activeCompetitions") or []:
        if isinstance(c, dict) and c.get("slug"):
            slug = normalize(c["slug"])

            if slug:
                result.append(slug)

    return list(dict.fromkeys(result))


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

    return active, covered, not_covered


# =========================
# VALID CARD
# =========================

def valid_card(card):
    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))

    except (TypeError, ValueError):
        print(
            f"   📄 {name}: ❌ età sconosciuta",
            flush=True
        )
        return False, "invalid"

    rarity = normalize(
        card.get("rarityTyped")
    ).upper()

    print(
        f"   📄 {name}",
        flush=True
    )

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

    if rarity != "LIMITED":
        print(
            f"      ❌ Rarità: {rarity}",
            flush=True
        )
        return False, "invalid"

    # ==========================================
    # FLOOR + MINIMO 5 INSERZIONI
    # ==========================================

    price, price_reason = get_live_floor(card)

    if price_reason == "invalid":
        print(
            "      ❌ Meno di 5 inserzioni valide",
            flush=True
        )
        return False, "invalid"

    if price is None:
        print(
            "      🟡 Prezzo sconosciuto → PENDING",
            flush=True
        )
        return False, "unknown_price"

    print(
        f"      💰 Floor: €{price / 100:.2f}",
        flush=True
    )

    if not MIN_PRICE <= price <= MAX_PRICE:
        print(
            "      ❌ Prezzo fuori range",
            flush=True
        )
        return False, "invalid"

    active, covered, not_covered = covered_competitions(card)

    if not active:
        print(
            "      ❌ Nessuna activeCompetition",
            flush=True
        )
        return False, "invalid"

    print(
        f"      🏆 Active: {', '.join(active)}",
        flush=True
    )

    if covered:
        print(
            f"      ✅ Covered: {', '.join(covered)}",
            flush=True
        )

    if not_covered:
        print(
            f"      🚫 Non covered: {', '.join(not_covered)}",
            flush=True
        )

    if not covered:
        print(
            "      ❌ Nessuna competizione coperta da Sorare",
            flush=True
        )
        return False, "invalid"

    print(
        "      ✅ CARTA VALIDA",
        flush=True
    )

    return True, "valid"


# =========================
# REJECT
# =========================

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

    print(
        "✅ Offerta originale rifiutata",
        flush=True
    )

    return True


# =========================
# SIGN
# =========================

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

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(
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
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(p.stdout)


# =========================
# COUNTER OFFER
# =========================

def counter_offer(offer, cards):
    receiver = normalize(
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
        f"🟢 Controproposta: {len(ids)} carta/e "
        f"→ €{amount / 100:.2f}",
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
        print(
            "❌ prepareOffer: nessun risultato",
            flush=True
        )
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

    deal_id = str(uuid.uuid4())

    create_input = {
        "approvals": approvals,
        "dealId": deal_id,
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
        print(
            "❌ createDirectOffer: nessun risultato",
            flush=True
        )
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


# =========================
# RETRY / STATE
# =========================

def retry_unknown(offer_id):
    now = time.time()

    with state_lock:
        last = unknown_price.get(offer_id)

        if last is None:
            unknown_price[offer_id] = now
            return True

        if now - last >= UNKNOWN_PRICE_RETRY:
            unknown_price[offer_id] = now
            return True

    return False


def completed(offer_id):
    with state_lock:
        processed.add(offer_id)
        unknown_price.pop(offer_id, None)


# =========================
# PROCESS OFFER
# =========================

def process_offer(offer):
    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

    if not retry_unknown(offer_id):
        return

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        completed(offer_id)
        return

    print(
        f"\n📨 OFFERTA {offer_id}",
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
        completed(offer_id)
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "⚠️ Impossibile verificare tutte le carte",
            flush=True
        )
        return

    valid = []
    unknown = False

    for card in cards:
        try:
            ok, reason = valid_card(card)

            if ok:
                valid.append(card)

            elif reason == "unknown_price":
                unknown = True

        except Exception as e:
            print(
                f"❌ Errore carta: {e}",
                flush=True
            )
            return

    # ==========================================
    # IMPORTANTE:
    # UNA CARTA ESCLUSA NON BLOCCA LE ALTRE.
    #
    # Ma se esiste una carta con prezzo
    # realmente sconosciuto, manteniamo il
    # comportamento originale: offerta PENDING.
    # ==========================================

    if unknown:
        print(
            "🟡 Prezzo non verificabile "
            "→ offerta lasciata PENDING",
            flush=True
        )
        return

    # ==========================================
    # SOLO SE NESSUNA CARTA È IDONEA:
    # RIFIUTA L'OFFERTA
    # ==========================================

    if not valid:
        print(
            "🔴 Nessuna carta valida → rifiuto",
            flush=True
        )

        if reject_offer(offer):
            completed(offer_id)

        return

    # ==========================================
    # CONTROPROPOSTA SOLO CON LE CARTE IDONEE
    # ==========================================

    if counter_offer(offer, valid):

        if reject_offer(offer):
            completed(offer_id)

        else:
            print(
                "⚠️ Controproposta creata "
                "ma originale non rifiutata",
                flush=True
            )

    else:
        print(
            "🟡 Controproposta fallita "
            "→ originale PENDING",
            flush=True
        )


# =========================
# WORKER
# =========================

def worker():
    print(
        "🤖 BOT AVVIATO",
        flush=True
    )

    print(
        f"📦 VERSIONE BOT: {BOT_VERSION}",
        flush=True
    )

    print(
        "💰 Pagamento: €0.20 per carta",
        flush=True
    )

    print(
        "📊 Range floor: €0.32 - €0.70",
        flush=True
    )

    print(
        "🎂 Età: < 28",
        flush=True
    )

    print(
        f"📊 Inserzioni minime richieste: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    covered = load_coverage(force=True)

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

    print(
        "🛡️ Filtro competition: ACTIVE + COVERED",
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
        "💶 EUR: eurCents",
        flush=True
    )

    print(
        "💵 USD: usdCents → EUR",
        flush=True
    )

    print(
        "🚫 WEI: ESCLUSO dal floor FIAT",
        flush=True
    )

    print(
        "🚫 Conversione wei → EUR: OFF",
        flush=True
    )

    print(
        "🚫 latestEnglishAuction: OFF",
        flush=True
    )

    print(
        "🚫 publicMinPrices: OFF",
        flush=True
    )

    print(
        "🚫 lowestPriceCard: OFF",
        flush=True
    )

    print(
        "🚫 tokenPrices: OFF",
        flush=True
    )

    print(
        "🛡️ PRICE UNKNOWN: LEAVE PENDING",
        flush=True
    )

    print(
        "🔁 RETRY: 60s",
        flush=True
    )

    print(
        "🔑 CREATE DIRECT OFFER: dealId ENABLED",
        flush=True
    )

    print(
        "🚫 PREPARE OFFER: type EXCLUDED",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
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


# =========================
# FLASK
# =========================

@app.get("/")
def home():
    covered = load_coverage()

    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": BOT_VERSION,
        "dry_run": DRY_RUN,
        "pay_per_card_cents": PAY_PER_CARD,
        "interval_seconds": INTERVAL,
        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "max_age": MAX_AGE,

        # NUOVO
        "min_live_listings": MIN_LIVE_LISTINGS,

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

        "usd_conversion": True,
        "wei_conversion": False,
        "wei_excluded_from_fiat_floor": True,
        "latest_english_auction_fallback": False,
        "public_min_prices": False,
        "lowest_price_card": False,
        "token_prices": False,

        "unknown_price_action":
            "LEAVE_PENDING",

        "unknown_price_retry_seconds":
            UNKNOWN_PRICE_RETRY,

        "prepare_offer_type": False,
        "create_direct_offer_deal_id": True
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
        "min_live_listings": MIN_LIVE_LISTINGS
    })


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
        )
    )
