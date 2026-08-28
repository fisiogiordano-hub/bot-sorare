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
TIMEOUT = 25
UNKNOWN_PRICE_RETRY = 60
USD_EUR_CACHE_SECONDS = 300

BOT_VERSION = "20.3"

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATE
# ============================================================

processed = set()
unknown_price = {}

state_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

usd_rate = None
usd_rate_time = 0
usd_lock = threading.Lock()


# ============================================================
# HTTP / GRAPHQL
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
            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT,
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
                    flush=True,
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
                        ensure_ascii=False,
                    )[:2000],
                    flush=True,
                )
                return data

            return data

        except requests.RequestException as e:
            print(f"❌ HTTP Sorare: {e}", flush=True)
            time.sleep(attempt + 1)

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
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
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    print(
        "🔐 Stark key account: "
        + ("PRESENTE" if user.get("starkKey") else "NON DISPONIBILE"),
        flush=True,
    )

    return True


# ============================================================
# PENDING OFFERS
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

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    return (
        (
            user.get("pendingTokenOffersReceived")
            or {}
        ).get("nodes")
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
# USD -> EUR
# ============================================================

def usd_eur():
    global usd_rate, usd_rate_time

    now = time.time()

    with usd_lock:
        if (
            usd_rate
            and now - usd_rate_time < USD_EUR_CACHE_SECONDS
        ):
            return usd_rate

        try:
            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={"from": "USD", "to": "EUR"},
                timeout=10,
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
            print(
                f"❌ USD/EUR: {e}",
                flush=True,
            )
            return None


def price_eur_cents(amounts):
    if not isinstance(amounts, dict):
        return None

    # EUR diretto
    try:
        eur = int(amounts.get("eurCents"))
        if eur > 0:
            return eur
    except (TypeError, ValueError):
        pass

    # USD -> EUR
    try:
        usd = float(amounts.get("usdCents"))
    except (TypeError, ValueError):
        usd = 0

    if usd > 0:
        rate = usd_eur()

        if rate:
            return int(round(usd * rate))

    # WEI volutamente escluso
    if amounts.get("wei") is not None:
        print(
            "🚫 WEI escluso dal floor FIAT",
            flush=True,
        )

    return None


# ============================================================
# LIVE SINGLE SALE FLOOR
# ============================================================

def get_live_floor(card):
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

    data = graphql("""
        query LiveSales($playerSlug: String, $first: Int) {
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
        "first": 50,
    })

    if not data or data.get("errors"):
        return None

    connection = (
        ((data.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers")
        or {}
    )

    prices = []

    for offer in connection.get("nodes") or []:
        sender_side = offer.get("senderSide") or {}

        compatible = False

        for c in sender_side.get("anyCards") or []:
            c_player = (
                c.get("anyPlayer") or {}
            ).get("slug")

            try:
                c_season = int(c.get("seasonYear"))
            except (TypeError, ValueError):
                c_season = None

            if (
                str(c_player or "").lower()
                == player_slug
                and
                str(c.get("rarityTyped") or "").lower()
                == rarity
                and
                c_season == season
            ):
                compatible = True
                break

        if not compatible:
            continue

        amounts = (
            offer.get("receiverSide") or {}
        ).get("amounts") or {}

        price = price_eur_cents(amounts)

        if price is not None:
            prices.append(price)

    if not prices:
        print(
            "      ⚠️ Floor FIAT non verificabile",
            flush=True,
        )
        return None

    floor = min(prices)

    print(
        f"      💰 FLOOR LIVE: €{floor / 100:.2f}",
        flush=True,
    )

    return floor


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
        or
        str(card.get("slug") or "").lower() in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def competitions(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub") or {}

    result = []

    for c in club.get("activeCompetitions") or []:
        if isinstance(c, dict) and c.get("slug"):
            result.append(str(c["slug"]))

    return list(dict.fromkeys(result))


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
        print(f"   📄 {name}: ❌ età sconosciuta", flush=True)
        return False, "invalid"

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    print(f"   📄 {name}", flush=True)
    print(f"      🎂 Età: {age}", flush=True)

    if age >= MAX_AGE:
        print("      ❌ Età troppo alta", flush=True)
        return False, "invalid"

    if rarity != "LIMITED":
        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )
        return False, "invalid"

    price = get_live_floor(card)

    if price is None:
        print(
            "      🟡 Prezzo sconosciuto → PENDING",
            flush=True,
        )
        return False, "unknown_price"

    print(
        f"      💰 Floor: €{price / 100:.2f}",
        flush=True,
    )

    if not MIN_PRICE <= price <= MAX_PRICE:
        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )
        return False, "invalid"

    comps = competitions(card)

    if not comps:
        print(
            "      ❌ Nessuna activeCompetition",
            flush=True,
        )
        return False, "invalid"

    print(
        f"      🏆 {', '.join(comps)}",
        flush=True,
    )

    print(
        "      ✅ CARTA VALIDA",
        flush=True,
    )

    return True, "valid"


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:
        print(
            "❌ blockchainId mancante",
            flush=True,
        )
        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: reject simulato",
            flush=True,
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
            "clientMutationId": str(uuid.uuid4()),
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
                flush=True,
            )
        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# SIGN AUTHORIZATIONS
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
const { signAuthorizationRequest } =
    require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function build(a) {
    const r = a.request;

    if (!r)
        throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest" &&
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
        "Authorization non supportata: " +
        r.__typename
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
    receiver = (
        offer.get("sender") or {}
    ).get("slug")

    receiver = str(receiver or "").strip()

    ids = [
        str(c.get("assetId")).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not ids:
        return False

    amount = len(ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: "
        f"{len(ids)} carta/e → €{amount / 100:.2f}",
        flush=True,
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: controproposta simulata",
            flush=True,
        )
        return True

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    prepare_input = {
        "receiveAssetIds": ids,
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
        mutation PrepareOffer(
            $input: prepareOfferInput!
        ) {
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
    """, {"input": prepare_input})

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ prepareOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True,
            )
        return False

    auth = result.get("authorizations") or []

    if not auth:
        print(
            "❌ Nessuna autorizzazione",
            flush=True,
        )
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return False

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    create_input = {
        "approvals": approvals,
        "receiveAssetIds": ids,
        "sendAssetIds": [],
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
        f"💰 €{amount / 100:.2f}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True,
    )

    return True


# ============================================================
# RETRY
# ============================================================

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


# ============================================================
# PROCESS
# ============================================================

def process_offer(offer):
    offer_id = str(
        offer.get("id") or ""
    ).strip()

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

    if not retry_unknown(offer_id):
        return

    receiver_cards = (
        offer.get("receiverSide") or {}
    ).get("anyCards") or []

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        completed(offer_id)
        return

    print(
        f"\n📨 OFFERTA {offer_id}",
        flush=True,
    )

    sender_cards = (
        offer.get("senderSide") or {}
    ).get("anyCards") or []

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
            flush=True,
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
                flush=True,
            )
            return

    if unknown:
        print(
            "🟡 Prezzo non verificabile → "
            "offerta lasciata PENDING",
            flush=True,
        )
        return

    if not valid:
        print(
            "🔴 Nessuna carta valida → rifiuto",
            flush=True,
        )

        if reject_offer(offer):
            completed(offer_id)

        return

    if counter_offer(offer, valid):
        if reject_offer(offer):
            completed(offer_id)
        else:
            print(
                "⚠️ Controproposta creata ma "
                "originale non rifiutata",
                flush=True,
            )
    else:
        print(
            "🟡 Controproposta fallita → "
            "originale PENDING",
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
    print(
        "🏆 COMPETIZIONI: tutte le activeCompetitions",
        flush=True,
    )
    print(
        "💰 PRICE SOURCE: liveSingleSaleOffers",
        flush=True,
    )
    print(
        "🎯 MATCH PRICE: player + rarity + season",
        flush=True,
    )
    print("💶 EUR: eurCents", flush=True)
    print("💵 USD: usdCents → EUR", flush=True)
    print("🚫 WEI: ESCLUSO dal floor FIAT", flush=True)
    print(
        "🚫 referenceCurrency=WEI NON viene "
        "interpretato come ETH",
        flush=True,
    )
    print("🚫 Conversione wei → EUR: OFF", flush=True)
    print("🚫 latestEnglishAuction: OFF", flush=True)
    print("🚫 publicMinPrices: OFF", flush=True)
    print("🚫 lowestPriceCard: OFF", flush=True)
    print("🚫 tokenPrices: OFF", flush=True)
    print("🛡️ PRICE UNKNOWN: LEAVE PENDING", flush=True)
    print("🔁 RETRY: 60s", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)

    if not check_account():
        return

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
# START WORKER
# ============================================================

def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        t = threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        )

        t.start()

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
        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",
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
        "unknown_price_action": "LEAVE_PENDING",
        "unknown_price_retry_seconds":
            UNKNOWN_PRICE_RETRY,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
    })


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
        ),
    )
