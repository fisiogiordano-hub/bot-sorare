import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import requests

from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"

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
TIMEOUT = 20
UNKNOWN_RETRY = 60

BOT_VERSION = "20.2"

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# Campi già verificati dallo schema live.
PREPARE_FIELDS = {
    "clientMutationId",
    "receiveAmount",
    "receiveAssetIds",
    "receiverSlug",
    "sendAmount",
    "sendAssetIds",
    "settlementCurrencies",
}

CREATE_FIELDS = {
    "approvals",
    "clientMutationId",
    "counteredOfferId",
    "dealId",
    "duration",
    "migrationData",
    "receiveAmount",
    "receiveAssetIds",
    "receiverSlug",
    "sendAmount",
    "sendAssetIds",
}

# ============================================================
# STATE
# ============================================================

session = requests.Session()

processed = set()
unknown_retry = {}

state_lock = threading.Lock()
worker_started = False
worker_lock = threading.Lock()

usd_rate = None
usd_rate_time = 0
usd_lock = threading.Lock()

# ============================================================
# HTTP
# ============================================================

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
        "variables": variables or {},
    }

    for attempt in range(3):
        try:
            r = session.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: {r.text[:500]}",
                    flush=True,
                )
                time.sleep(1 + attempt)
                continue

            try:
                data = r.json()
            except ValueError:
                print("❌ JSON Sorare non valido", flush=True)
                return None

            if data.get("errors"):
                print(
                    "❌ GraphQL: " +
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                    )[:1500],
                    flush=True,
                )

            return data

        except requests.RequestException as e:
            print(f"❌ HTTP Sorare: {e}", flush=True)
            time.sleep(1 + attempt)

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

    user = ((data or {}).get("data") or {}).get("currentUser")

    if not user:
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: {user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    print(
        "🔐 Stark key account: " +
        ("PRESENTE" if user.get("starkKey") else "NON DISPONIBILE"),
        flush=True,
    )

    return True


# ============================================================
# OFFERS
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

    user = ((data or {}).get("data") or {}).get("currentUser") or {}

    return (
        (
            user.get("pendingTokenOffersReceived") or {}
        ).get("nodes") or []
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

    return ((data.get("data") or {}).get("anyCards")) or []


# ============================================================
# USD -> EUR
# ============================================================

def get_usd_eur():
    global usd_rate, usd_rate_time

    now = time.time()

    with usd_lock:
        if usd_rate and now - usd_rate_time < 300:
            return usd_rate

        try:
            r = session.get(
                "https://api.frankfurter.app/latest",
                params={"from": "USD", "to": "EUR"},
                timeout=TIMEOUT,
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
                flush=True,
            )

            return rate

        except Exception as e:
            print(f"❌ USD/EUR: {e}", flush=True)
            return None


def usd_to_eur_cents(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    rate = get_usd_eur()

    if not rate:
        return None

    return max(1, int(round(value * rate)))


# ============================================================
# PRICE
# ============================================================

def price_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    eur = amounts.get("eurCents")

    try:
        if eur is not None and int(eur) > 0:
            return int(eur)
    except (TypeError, ValueError):
        pass

    usd = amounts.get("usdCents")

    if usd is not None:
        converted = usd_to_eur_cents(usd)

        if converted:
            return converted

    # WEI NON VIENE MAI CONVERTITO
    return None


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):
    player = card.get("anyPlayer") or {}

    player_slug = str(
        player.get("slug") or ""
    ).strip().lower()

    rarity = str(
        card.get("rarityTyped") or ""
    ).strip().lower()

    try:
        season = int(card.get("seasonYear"))
    except (TypeError, ValueError):
        return None

    if not player_slug or not rarity:
        return None

    print(
        f"      🔎 FLOOR {player_slug} | "
        f"{rarity} | {season}",
        flush=True,
    )

    cursor = None
    prices = []

    while True:
        data = graphql("""
            query LiveSales(
                $playerSlug: String
                $first: Int
                $after: String
            ) {
                tokens {
                    liveSingleSaleOffers(
                        playerSlug: $playerSlug
                        first: $first
                        after: $after
                    ) {
                        nodes {
                            id

                            senderSide {
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

                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
        """, {
            "playerSlug": player_slug,
            "first": 50,
            "after": cursor,
        })

        if not data or data.get("errors"):
            return None

        connection = (
            ((data.get("data") or {}).get("tokens") or {})
            .get("liveSingleSaleOffers") or {}
        )

        nodes = connection.get("nodes") or []

        print(
            f"         📦 offerte: {len(nodes)}",
            flush=True,
        )

        for offer in nodes:
            side = offer.get("senderSide") or {}
            cards = side.get("anyCards") or []

            match = False

            for c in cards:
                p = c.get("anyPlayer") or {}

                if (
                    str(p.get("slug") or "").lower()
                    != player_slug
                ):
                    continue

                if (
                    str(c.get("rarityTyped") or "").lower()
                    != rarity
                ):
                    continue

                try:
                    s = int(c.get("seasonYear"))
                except (TypeError, ValueError):
                    continue

                if s == season:
                    match = True
                    break

            if not match:
                continue

            amounts = (
                offer.get("receiverSide") or {}
            ).get("amounts") or {}

            price = price_eur(amounts)

            if price is not None:
                prices.append(
                    (price, offer.get("id"))
                )

        page = connection.get("pageInfo") or {}

        if not page.get("hasNextPage"):
            break

        next_cursor = page.get("endCursor")

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    if not prices:
        print(
            "      ❌ Nessun floor FIAT verificabile",
            flush=True,
        )
        return None

    floor, offer_id = min(
        prices,
        key=lambda x: x[0],
    )

    print(
        f"      💰 FLOOR LIVE: €{floor / 100:.2f}",
        flush=True,
    )

    print(
        f"         🆔 {offer_id}",
        flush=True,
    )

    return floor


# ============================================================
# CARD FILTER
# ============================================================

def competitions(card):
    club = (card.get("anyPlayer") or {}).get("activeClub") or {}

    return list(dict.fromkeys(
        str(x.get("slug") or "").strip().lower()
        for x in club.get("activeCompetitions") or []
        if isinstance(x, dict) and x.get("slug")
    ))


def valid_card(card):
    name = card.get("name") or card.get("slug") or "Carta"
    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except (TypeError, ValueError):
        return False, "invalid"

    print(
        f"   📄 {name} • {card.get('rarityTyped')}",
        flush=True,
    )

    print(f"      🎂 Età: {age}", flush=True)

    if age >= MAX_AGE:
        return False, "invalid"

    if str(card.get("rarityTyped") or "").upper() != "LIMITED":
        return False, "invalid"

    floor = live_floor(card)

    if floor is None:
        print(
            "      🟡 FLOOR UNKNOWN → PENDING",
            flush=True,
        )
        return False, "unknown_price"

    print(
        f"      💰 Floor: €{floor / 100:.2f}",
        flush=True,
    )

    if not MIN_PRICE <= floor <= MAX_PRICE:
        print(
            "      ❌ Floor fuori range",
            flush=True,
        )
        return False, "invalid"

    club = player.get("activeClub") or {}

    if not club:
        return False, "invalid"

    comps = competitions(card)

    if not comps:
        return False, "invalid"

    print(
        f"      🏟️ {club.get('name') or club.get('slug')}",
        flush=True,
    )

    print(
        f"      🏆 {', '.join(comps)}",
        flush=True,
    )

    print(
        f"      ✅ VALIDATA | {age} anni | "
        f"€{floor / 100:.2f}",
        flush=True,
    )

    return True, "valid"


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):
    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(KID.lower())

    return (
        str(card.get("assetId") or "").lower() in wanted
        or str(card.get("slug") or "").lower() in wanted
    )


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:
        return False

    if DRY_RUN:
        print("🟡 DRY RUN: reject", flush=True)
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
            "clientMutationId": str(uuid.uuid4()),
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result or result.get("errors"):
        print(
            f"❌ Reject: "
            f"{result.get('errors') if result else 'errore'}",
            flush=True,
        )
        return False

    print("✅ Offerta originale rifiutata", flush=True)
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

const input = JSON.parse(fs.readFileSync(0, "utf8"));

function build(a) {
    const r = a.request;

    if (!r) {
        throw new Error("AuthorizationRequest mancante");
    }

    if (
        r.__typename === "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(
        input.privateKey,
        r
    );

    if (r.__typename === "StarkexTransferAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename === "StarkexLimitOrderAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
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
        "Authorization non supportata: " +
        r.__typename
    );
}

process.stdout.write(
    JSON.stringify(input.authorizations.map(build))
);
'''

    p = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
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
    sender = offer.get("sender") or {}
    receiver = str(sender.get("slug") or "").strip()

    asset_ids = [
        str(c["assetId"]).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not asset_ids:
        return False

    amount = len(asset_ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: {len(asset_ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True,
    )

    if DRY_RUN:
        print("🟡 DRY RUN: counter", flush=True)
        return True

    prepare = {
        "receiveAssetIds": asset_ids,
        "sendAssetIds": [],
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },
        "receiverSlug": receiver,
        "settlementCurrencies": ["EUR"],
        "clientMutationId": str(uuid.uuid4()),
    }

    data = graphql("""
        mutation PrepareOffer($input: prepareOfferInput!) {
            prepareOffer(input: $input) {
                authorizations {
                    fingerprint
                    request {
                        __typename

                        ... on
                        StarkexTransferAuthorizationRequest {
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

                        ... on
                        StarkexLimitOrderAuthorizationRequest {
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

                        ... on
                        MangopayWalletTransferAuthorizationRequest {
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
    """, {"input": prepare})

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result or result.get("errors"):
        print(
            f"❌ prepareOffer: "
            f"{result.get('errors') if result else 'errore'}",
            flush=True,
        )
        return False

    auth = result.get("authorizations") or []

    if not auth:
        print("❌ Nessuna autorizzazione", flush=True)
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return False

    create = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "sendAssetIds": [],
        "receiveAssetIds": asset_ids,
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },
        "receiverSlug": receiver,
        "clientMutationId": str(uuid.uuid4()),
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
    """, {"input": create})

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result or result.get("errors"):
        print(
            f"❌ createDirectOffer: "
            f"{result.get('errors') if result else 'errore'}",
            flush=True,
        )
        return False

    token_offer = result.get("tokenOffer") or {}

    if not token_offer.get("id"):
        return False

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{token_offer['id']}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    return True


# ============================================================
# STATE
# ============================================================

def retry_allowed(offer_id):
    now = time.time()

    with state_lock:
        last = unknown_retry.get(offer_id)

        if last is None or now - last >= UNKNOWN_RETRY:
            unknown_retry[offer_id] = now
            return True

    return False


def complete(offer_id):
    with state_lock:
        processed.add(offer_id)
        unknown_retry.pop(offer_id, None)


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):
    offer_id = str(offer.get("id") or "").strip()

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

    if not retry_allowed(offer_id):
        return

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        complete(offer_id)
        return

    print(
        f"\n📨 OFFERTA {offer_id}\n🎯 Kulenovic trovato",
        flush=True,
    )

    ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not ids:
        complete(offer_id)
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "⚠️ Impossibile verificare tutte le carte",
            flush=True,
        )
        return

    valid = []
    unknown = False

    for card in cards:
        ok, reason = valid_card(card)

        if ok:
            valid.append(card)
        elif reason == "unknown_price":
            unknown = True

    if unknown:
        print(
            f"🟡 PRICE UNKNOWN → PENDING "
            f"(retry {UNKNOWN_RETRY}s)",
            flush=True,
        )
        return

    if not valid:
        print("🔴 Nessuna carta idonea", flush=True)

        if reject_offer(offer):
            complete(offer_id)

        return

    if counter_offer(offer, valid):
        if reject_offer(offer):
            complete(offer_id)
        else:
            print(
                "⚠️ Counter creata, "
                "ma originale non rifiutata",
                flush=True,
            )
    else:
        print(
            "🟡 Counter non creata → PENDING",
            flush=True,
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 BOT AVVIATO", flush=True)
    print(f"📦 VERSIONE BOT: {BOT_VERSION}", flush=True)
    print("💰 Pagamento: €0.20 per carta", flush=True)
    print("📊 Range floor: €0.32 - €0.70", flush=True)
    print("🎂 Età: < 28", flush=True)
    print("🏆 COMPETIZIONI: tutte le activeCompetitions", flush=True)
    print("💰 PRICE SOURCE: liveSingleSaleOffers", flush=True)
    print("🎯 MATCH PRICE: player + rarity + season", flush=True)
    print("💶 EUR: eurCents", flush=True)
    print("💵 USD: usdCents → EUR", flush=True)
    print("🚫 WEI: ESCLUSO dal floor FIAT", flush=True)
    print("🛡️ PRICE UNKNOWN: LEAVE PENDING", flush=True)
    print(f"🔁 RETRY: {UNKNOWN_RETRY}s", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)

    if not check_account():
        return

    print(
        "🟢 Schema offer già verificato "
        "(controllo live rimosso per alleggerire)",
        flush=True,
    )

    while True:
        try:
            offers = get_offers()

            print(
                f"📨 Offerte pendenti: {len(offers)}",
                flush=True,
            )

            for offer in offers:
                try:
                    process_offer(offer)
                except Exception as e:
                    print(
                        f"❌ Errore offerta: {e}",
                        flush=True,
                    )

            time.sleep(INTERVAL)

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True,
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
            daemon=True,
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True,
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
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
        "competition_mode": "ALL_ACTIVE_SORARE_COMPETITIONS",
        "price_mode": "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",
        "price_eur_mode": "EUR_DIRECT_OR_USD_CONVERTED",
        "wei_conversion": False,
        "unknown_price_action": "LEAVE_PENDING",
        "unknown_price_retry_seconds": UNKNOWN_RETRY,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
    })


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
