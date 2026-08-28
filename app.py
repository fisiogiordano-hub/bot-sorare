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
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

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
TIMEOUT = 30
UNKNOWN_PRICE_RETRY = 60
USD_EUR_CACHE_SECONDS = 300

BOT_VERSION = "20.1"

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# STATE
# ============================================================

processed = set()
unknown_price_offers = {}

state_lock = threading.Lock()

_worker_started = False
_worker_lock = threading.Lock()

_schema_lock = threading.Lock()
_schema_text = None

_usd_eur_lock = threading.Lock()
_usd_eur_cache = None
_usd_eur_cache_time = 0


# ============================================================
# UTILS
# ============================================================

def slug(value):
    value = str(value or "").strip().lower()

    for old, new in [
        ("_", "-"),
        (" ", "-"),
        ("’", ""),
        ("'", ""),
    ]:
        value = value.replace(old, new)

    while "--" in value:
        value = value.replace("--", "-")

    return value


def auth_headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for attempt in range(1, 4):
        try:
            response = requests.post(
                URL,
                json=payload,
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True,
            )

            if response.status_code == 429:
                try:
                    wait = int(
                        response.headers.get(
                            "Retry-After",
                            attempt * 3,
                        )
                    )
                except (TypeError, ValueError):
                    wait = attempt * 3

                print(
                    f"⏳ Rate limit Sorare: {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:
                print(
                    f"❌ HTTP {response.status_code}: "
                    f"{response.text[:1000]}",
                    flush=True,
                )
                time.sleep(attempt)
                continue

            try:
                data = response.json()
            except ValueError:
                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )
                return None

            if data.get("errors"):
                print(
                    "❌ GraphQL ERROR:",
                    flush=True,
                )

                for error in data["errors"]:
                    print(
                        json.dumps(
                            error,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                return data

            return data

        except requests.RequestException as error:
            print(
                f"❌ HTTP Sorare: {error}",
                flush=True,
            )
            time.sleep(attempt)

        except Exception as error:
            print(
                f"❌ GraphQL: {error}",
                flush=True,
            )
            return None

    return None


# ============================================================
# LIVE SCHEMA
# ============================================================

def get_live_schema():
    global _schema_text

    with _schema_lock:
        if _schema_text is not None:
            return _schema_text

        print(
            "📚 Controllo schema Sorare corrente...",
            flush=True,
        )

        try:
            response = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
                },
            )

            print(
                f"📚 Schema Sorare HTTP {response.status_code}",
                flush=True,
            )

            if response.status_code != 200:
                print(
                    "❌ Impossibile scaricare lo schema live",
                    flush=True,
                )
                return None

            _schema_text = response.text

            print(
                f"✅ Schema live scaricato "
                f"({len(_schema_text)} caratteri)",
                flush=True,
            )

            return _schema_text

        except Exception as error:
            print(
                f"❌ Errore schema live: {error}",
                flush=True,
            )
            return None


def get_input_fields(type_name):
    schema = get_live_schema()

    if not schema:
        return set()

    match = re.search(
        r"\binput\s+" +
        re.escape(type_name) +
        r"\s*\{",
        schema,
        re.MULTILINE,
    )

    if not match:
        print(
            f"⚠️ {type_name} non trovato nello schema",
            flush=True,
        )
        return set()

    start = match.end()
    depth = 1
    position = start

    while position < len(schema) and depth:
        if schema[position] == "{":
            depth += 1
        elif schema[position] == "}":
            depth -= 1
        position += 1

    block = schema[start:position - 1]
    fields = set()

    for line in block.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        field_match = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*"
            r"(?:\([^)]*\))?\s*:",
            line,
        )

        if field_match:
            fields.add(field_match.group(1))

    print(
        f"🔍 Campi {type_name}: "
        f"{', '.join(sorted(fields))}",
        flush=True,
    )

    return fields


def inspect_live_schema():
    print(
        "🔎 CONTROLLO SCHEMA LIVE",
        flush=True,
    )

    prepare_fields = get_input_fields(
        "prepareOfferInput"
    )

    create_fields = get_input_fields(
        "createDirectOfferInput"
    )

    if not prepare_fields or not create_fields:
        return False

    required_prepare = {
        "receiveAssetIds",
        "receiverSlug",
        "sendAssetIds",
        "sendAmount",
    }

    required_create = {
        "approvals",
        "receiveAssetIds",
        "receiverSlug",
        "sendAssetIds",
        "sendAmount",
    }

    missing_prepare = required_prepare - prepare_fields
    missing_create = required_create - create_fields

    if missing_prepare:
        print(
            "❌ Campi mancanti prepareOfferInput: "
            f"{', '.join(sorted(missing_prepare))}",
            flush=True,
        )
        return False

    if missing_create:
        print(
            "❌ Campi mancanti createDirectOfferInput: "
            f"{', '.join(sorted(missing_create))}",
            flush=True,
        )
        return False

    print("   prepareOfferInput:", flush=True)

    for field in sorted(prepare_fields):
        print(f"      • {field}", flush=True)

    print("   createDirectOfferInput:", flush=True)

    for field in sorted(create_fields):
        print(f"      • {field}", flush=True)

    print(
        "✅ SCHEMA OFFER CONFERMATO",
        flush=True,
    )

    print(
        "🟢 Schema corrente compatibile.",
        flush=True,
    )

    return True


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
            flush=True,
        )
        return False

    print(
        "✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    if user.get("starkKey"):
        print(
            "🔐 Stark key account: PRESENTE",
            flush=True,
        )
    else:
        print(
            "⚠️ Stark key account: NON DISPONIBILE",
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

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    return (
        (
            user.get(
                "pendingTokenOffersReceived"
            )
            or {}
        ).get("nodes")
        or []
    )


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_ids):
    asset_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in asset_ids
            if value
        )
    )

    if not asset_ids:
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
    """, {
        "assetIds": asset_ids,
    })

    if not data:
        print(
            "❌ Nessuna risposta da anyCards",
            flush=True,
        )
        return []

    if data.get("errors"):
        print(
            "❌ Errore GraphQL card_details:",
            flush=True,
        )

        for error in data["errors"]:
            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return []

    return (
        ((data.get("data") or {}).get("anyCards"))
        or []
    )


# ============================================================
# USD / EUR
# ============================================================

def get_usd_eur_rate():
    global _usd_eur_cache
    global _usd_eur_cache_time

    now = time.time()

    with _usd_eur_lock:
        if (
            _usd_eur_cache is not None
            and
            now - _usd_eur_cache_time
            < USD_EUR_CACHE_SECONDS
        ):
            return _usd_eur_cache

        try:
            response = requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from": "USD",
                    "to": "EUR",
                },
                timeout=TIMEOUT,
            )

            print(
                f"💱 USD/EUR HTTP {response.status_code}",
                flush=True,
            )

            if response.status_code != 200:
                return None

            rate = float(
                (
                    response.json().get("rates")
                    or {}
                ).get("EUR")
            )

            if rate <= 0:
                return None

            _usd_eur_cache = rate
            _usd_eur_cache_time = now

            print(
                f"💱 1 USD = {rate:.6f} EUR",
                flush=True,
            )

            return rate

        except Exception as error:
            print(
                f"❌ Errore cambio USD/EUR: {error}",
                flush=True,
            )
            return None


def usd_cents_to_eur_cents(usd_cents):
    try:
        usd_cents = float(
            str(usd_cents).strip()
        )
    except (TypeError, ValueError):
        return None

    if usd_cents <= 0:
        return None

    rate = get_usd_eur_rate()

    if rate is None:
        return None

    eur_cents = int(round(usd_cents * rate))

    if eur_cents <= 0:
        return None

    print(
        f"💵 {usd_cents:.0f} USD cents "
        f"→ {eur_cents} EUR cents",
        flush=True,
    )

    return eur_cents


# ============================================================
# PRICE
# ============================================================

def extract_offer_eur_cents(amounts):
    if not isinstance(amounts, dict):
        return None

    eur = amounts.get("eurCents")

    if eur is not None:
        try:
            eur = int(str(eur).strip())

            if eur > 0:
                print(
                    f"💶 Prezzo EUR: €{eur / 100:.2f}",
                    flush=True,
                )
                return eur

        except (TypeError, ValueError):
            pass

    usd = amounts.get("usdCents")

    if usd is not None:
        print(
            f"💵 Prezzo USD cents: {usd}",
            flush=True,
        )

        converted = usd_cents_to_eur_cents(usd)

        if converted is not None:
            print(
                f"✅ USD → EUR: €{converted / 100:.2f}",
                flush=True,
            )
            return converted

    if amounts.get("wei") is not None:
        print(
            "🚫 WEI ESCLUSO dal floor FIAT",
            flush=True,
        )

        print(
            "🚫 referenceCurrency="
            f"{amounts.get('referenceCurrency')}",
            flush=True,
        )

    print(
        "🛑 Prezzo FIAT non verificabile",
        flush=True,
    )

    return None


# ============================================================
# LIVE FLOOR
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
        season = None

    print(
        "      🔎 FLOOR LIVE SINGLE SALE",
        flush=True,
    )

    print(
        f"         playerSlug: {player_slug}",
        flush=True,
    )

    print(
        f"         rarità: {rarity}",
        flush=True,
    )

    print(
        f"         stagione: {season}",
        flush=True,
    )

    if not player_slug or not rarity or season is None:
        return None

    cursor = None
    page = 0
    prices = []

    while True:
        page += 1

        data = graphql("""
            query LiveSingleSaleOffers(
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
                                amounts {
                                    eurCents
                                    usdCents
                                    referenceCurrency
                                    wei
                                }

                                anyCards {
                                    assetId
                                    slug
                                    name
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

        if not data:
            return None

        if data.get("errors"):
            print(
                "      ❌ GraphQL LIVE SINGLE SALE:",
                flush=True,
            )

            for error in data["errors"]:
                print(
                    json.dumps(
                        error,
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            return None

        connection = (
            (
                (
                    (data.get("data") or {})
                    .get("tokens")
                    or {}
                ).get("liveSingleSaleOffers")
                or {}
            )
        )

        nodes = connection.get("nodes") or []

        print(
            f"         📦 pagina {page}: "
            f"{len(nodes)} offerte",
            flush=True,
        )

        for offer in nodes:
            sender_side = (
                offer.get("senderSide") or {}
            )

            offer_cards = (
                sender_side.get("anyCards") or []
            )

            compatible = False

            for offer_card in offer_cards:
                offer_rarity = str(
                    offer_card.get("rarityTyped") or ""
                ).strip().lower()

                try:
                    offer_season = int(
                        offer_card.get("seasonYear")
                    )
                except (TypeError, ValueError):
                    offer_season = None

                offer_player = (
                    offer_card.get("anyPlayer") or {}
                )

                offer_player_slug = str(
                    offer_player.get("slug") or ""
                ).strip().lower()

                if (
                    offer_player_slug
                    and offer_player_slug != player_slug
                ):
                    continue

                if offer_rarity != rarity:
                    continue

                if offer_season != season:
                    continue

                compatible = True
                break

            if not compatible:
                continue

            receiver_side = (
                offer.get("receiverSide") or {}
            )

            amounts = (
                receiver_side.get("amounts") or {}
            )

            price = extract_offer_eur_cents(
                amounts
            )

            if price is None:
                print(
                    "         ⚠️ Offerta compatibile "
                    "ma prezzo non convertibile",
                    flush=True,
                )
                continue

            prices.append(
                (
                    price,
                    offer.get("id"),
                )
            )

        page_info = connection.get("pageInfo") or {}

        if not page_info.get("hasNextPage"):
            break

        next_cursor = page_info.get("endCursor")

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    if not prices:
        print(
            "      ❌ Nessuna LIVE SINGLE SALE "
            "compatibile con prezzo FIAT disponibile",
            flush=True,
        )
        return None

    prices.sort(key=lambda item: item[0])

    floor, offer_id = prices[0]

    print(
        f"      💰 FLOOR LIVE: €{floor / 100:.2f}",
        flush=True,
    )

    print(
        f"         🆔 offerta floor: {offer_id}",
        flush=True,
    )

    print(
        f"         📊 compatibili: {len(prices)}",
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

def get_competitions(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        return []

    result = []

    for competition in (
        club.get("activeCompetitions") or []
    ):
        if not isinstance(competition, dict):
            continue

        value = slug(
            competition.get("slug")
        )

        if value:
            result.append(value)

    return list(dict.fromkeys(result))


def check_competition(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )
        return False

    name = (
        club.get("name")
        or club.get("slug")
        or "Sconosciuta"
    )

    competitions = get_competitions(card)

    print(
        f"      🏟️ Squadra: {name}",
        flush=True,
    )

    if not competitions:
        print(
            "      ❌ Nessuna activeCompetition",
            flush=True,
        )
        return False

    print(
        "      🏆 activeCompetitions:",
        flush=True,
    )

    for competition in competitions:
        print(
            f"         • {competition}",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


# ============================================================
# CARD VALIDATION
# ============================================================

def valid_card(card):
    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except (TypeError, ValueError):
        print(
            f"   📄 {name}",
            flush=True,
        )
        print(
            "      ❌ Età non disponibile",
            flush=True,
        )
        return False, "invalid"

    print(
        f"   📄 {name}",
        flush=True,
    )

    print(
        f"      🎂 Età: {age}",
        flush=True,
    )

    if age >= MAX_AGE:
        print(
            f"      ❌ Età troppo alta "
            f"(richiesto < {MAX_AGE})",
            flush=True,
        )
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
            "      ⚠️ FLOOR NON VERIFICABILE",
            flush=True,
        )
        print(
            "      🟡 Offerta LASCIATA IN SOSPESO",
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

    if not check_competition(card):
        return False, "invalid"

    competitions = get_competitions(card)

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
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
            "🟡 DRY RUN: rifiuto simulato",
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
        (data or {}).get("data") or {}
    ).get("rejectOffer")

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                f"❌ Reject: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )
        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# SIGN
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
            "SORARE_STARK_PRIVATE_KEY non configurata"
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

    process = subprocess.run(
        [
            node,
            "-e",
            script,
        ],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Firma fallita"
        )

    try:
        return json.loads(
            process.stdout
        )
    except json.JSONDecodeError:
        raise RuntimeError(
            "Risposta firma non valida"
        )


# ============================================================
# PREPARE
# ============================================================

def build_prepare_offer_input(
    asset_ids,
    receiver,
    amount,
):
    fields = get_input_fields(
        "prepareOfferInput"
    )

    if not fields:
        raise RuntimeError(
            "prepareOfferInput non disponibile"
        )

    result = {}

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = asset_ids

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "settlementCurrencies" in fields:
        result["settlementCurrencies"] = ["EUR"]

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(uuid.uuid4())

    return result


# ============================================================
# CREATE
# ============================================================

def build_create_direct_offer_input(
    asset_ids,
    receiver,
    amount,
    approvals,
):
    fields = get_input_fields(
        "createDirectOfferInput"
    )

    if not fields:
        raise RuntimeError(
            "createDirectOfferInput non disponibile"
        )

    result = {}

    if "approvals" in fields:
        result["approvals"] = approvals

    if "dealId" in fields:
        result["dealId"] = str(uuid.uuid4())

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = asset_ids

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(uuid.uuid4())

    return result


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(offer, cards):
    sender = offer.get("sender") or {}

    receiver = str(
        sender.get("slug") or ""
    ).strip()

    asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if card.get("assetId")
    ]

    if not receiver:
        print(
            "❌ receiverSlug mancante",
            flush=True,
        )
        return False

    if not asset_ids:
        print(
            "❌ Nessuna carta da ricevere",
            flush=True,
        )
        return False

    amount = len(asset_ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: "
        f"{len(asset_ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True,
    )

    print(
        f"👤 Receiver: {receiver}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: controproposta simulata",
            flush=True,
        )
        return True

    try:
        prepare_input = build_prepare_offer_input(
            asset_ids,
            receiver,
            amount,
        )
    except Exception as error:
        print(
            f"❌ Costruzione prepareOfferInput: {error}",
            flush=True,
        )
        return False

    print(
        "📦 prepareOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            prepare_input,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
    """, {
        "input": prepare_input,
    })

    if not data:
        print(
            "❌ Nessuna risposta da prepareOffer",
            flush=True,
        )
        return False

    if data.get("errors"):
        print(
            "❌ prepareOffer GRAPHQL ERROR:",
            flush=True,
        )

        for error in data["errors"]:
            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                f"❌ prepareOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )
        return False

    authorizations = (
        result.get("authorizations") or []
    )

    if not authorizations:
        print(
            "❌ Nessuna autorizzazione restituita",
            flush=True,
        )
        return False

    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )

    try:
        approvals = sign_authorizations(
            authorizations
        )
    except Exception as error:
        print(
            f"❌ Firma: {error}",
            flush=True,
        )
        return False

    print(
        f"✍️ Autorizzazioni firmate: "
        f"{len(approvals)}",
        flush=True,
    )

    try:
        create_input = (
            build_create_direct_offer_input(
                asset_ids,
                receiver,
                amount,
                approvals,
            )
        )
    except Exception as error:
        print(
            f"❌ Costruzione "
            f"createDirectOfferInput: {error}",
            flush=True,
        )
        return False

    debug = dict(create_input)

    if "approvals" in debug:
        debug["approvals"] = (
            f"{len(approvals)} authorization(s)"
        )

    print(
        "📦 createDirectOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            debug,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
    """, {
        "input": create_input,
    })

    if not data:
        return False

    if data.get("errors"):
        print(
            "❌ createDirectOffer GRAPHQL ERROR:",
            flush=True,
        )

        for error in data["errors"]:
            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                f"❌ createDirectOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )
        return False

    token_offer = (
        result.get("tokenOffer") or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:
        print(
            "❌ Nessuna offerta creata da Sorare",
            flush=True,
        )
        return False

    print("=" * 40, flush=True)

    print(
        f"✅ CONTROPROPOSTA INVIATA: {offer_id}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True,
    )

    print("=" * 40, flush=True)

    return True


# ============================================================
# UNKNOWN PRICE
# ============================================================

def should_retry_unknown(offer_id):
    now = time.time()

    with state_lock:
        last_attempt = unknown_price_offers.get(
            offer_id
        )

        if last_attempt is None:
            unknown_price_offers[offer_id] = now
            return True

        if now - last_attempt >= UNKNOWN_PRICE_RETRY:
            unknown_price_offers[offer_id] = now
            return True

        return False


def mark_completed(offer_id):
    with state_lock:
        processed.add(offer_id)
        unknown_price_offers.pop(
            offer_id,
            None,
        )


# ============================================================
# PROCESS OFFER
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

    print("\n" + "=" * 40, flush=True)

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )

    if not should_retry_unknown(offer_id):
        print(
            "⏳ Floor ancora sconosciuto: attendo",
            flush=True,
        )
        return

    sender_cards = (
        (
            offer.get("senderSide") or {}
        ).get("anyCards") or []
    )

    receiver_cards = (
        (
            offer.get("receiverSide") or {}
        ).get("anyCards") or []
    )

    if not any(
        is_kulenovic(card)
        for card in receiver_cards
    ):
        print(
            "⏭️ Kulenovic non presente: ignoro",
            flush=True,
        )

        mark_completed(offer_id)
        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    ids = [
        card.get("assetId")
        for card in sender_cards
        if card.get("assetId")
    ]

    if not ids:
        print(
            "❌ Nessuna carta offerta",
            flush=True,
        )

        mark_completed(offer_id)
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "❌ Impossibile verificare tutte le carte",
            flush=True,
        )
        return

    print(
        f"🔎 Controllo {len(cards)} carta/e",
        flush=True,
    )

    valid_cards = []
    has_unknown_price = False

    for card in cards:
        try:
            valid, reason = valid_card(card)

            if valid:
                valid_cards.append(card)

            elif reason == "unknown_price":
                has_unknown_price = True

        except Exception as error:
            print(
                f"❌ Errore controllo carta: {error}",
                flush=True,
            )
            return

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/{len(cards)}",
        flush=True,
    )

    if has_unknown_price:
        print(
            "⚠️ ALMENO UNA CARTA "
            "HA PREZZO NON VERIFICABILE",
            flush=True,
        )

        print(
            "🟡 OFFERTA LASCIATA IN SOSPESO",
            flush=True,
        )

        print(
            f"🔁 Retry tra {UNKNOWN_PRICE_RETRY}s",
            flush=True,
        )

        return

    if not valid_cards:
        print(
            "❌ Nessuna carta idonea",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta",
            flush=True,
        )

        if reject_offer(offer):
            mark_completed(offer_id)

        return

    rejected = len(cards) - len(valid_cards)

    if rejected:
        print(
            f"⚠️ {rejected} carta/e esclusa/e",
            flush=True,
        )

    if counter_offer(
        offer,
        valid_cards,
    ):
        print(
            "🟢 Controproposta completata",
            flush=True,
        )

        if reject_offer(offer):
            mark_completed(offer_id)
        else:
            print(
                "⚠️ Controproposta creata "
                "ma offerta originale non rifiutata",
                flush=True,
            )

    else:
        print(
            "🔴 Controproposta NON creata",
            flush=True,
        )

        print(
            "🟡 Offerta originale "
            "lasciata IN SOSPESO",
            flush=True,
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print(
        "🤖 BOT AVVIATO",
        flush=True,
    )

    print(
        f"📦 VERSIONE BOT: {BOT_VERSION}",
        flush=True,
    )

    print(
        f"💰 Pagamento: €{PAY_PER_CARD / 100:.2f} per carta",
        flush=True,
    )

    print(
        f"📊 Range floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True,
    )

    print(
        f"🎂 Età: < {MAX_AGE}",
        flush=True,
    )

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
    print(
        "🚫 WEI: ESCLUSO dal floor FIAT",
        flush=True,
    )
    print(
        "🚫 referenceCurrency=WEI NON viene "
        "interpretato come ETH",
        flush=True,
    )
    print(
        "🚫 Conversione wei → EUR: OFF",
        flush=True,
    )
    print(
        "🚫 latestEnglishAuction: OFF",
        flush=True,
    )
    print(
        "🚫 publicMinPrices: OFF",
        flush=True,
    )
    print(
        "🚫 lowestPriceCard: OFF",
        flush=True,
    )
    print(
        "🚫 tokenPrices: OFF",
        flush=True,
    )
    print(
        "🛡️ PRICE UNKNOWN: LEAVE PENDING",
        flush=True,
    )

    print(
        f"🔁 RETRY: {UNKNOWN_PRICE_RETRY}s",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if not check_account():
        print(
            "❌ Account non valido. Worker fermato.",
            flush=True,
        )
        return

    if not inspect_live_schema():
        print(
            "❌ Schema incompatibile. Worker fermato.",
            flush=True,
        )
        return

    while True:
        try:
            offers = get_offers()

            print(
                f"📨 Offerte pendenti: {len(offers)}",
                flush=True,
            )

            # IMPORTANTE:
            # se non ci sono offerte non facciamo altro lavoro
            if not offers:
                time.sleep(INTERVAL)
                continue

            for offer in offers:
                try:
                    process_offer(offer)
                except Exception as error:
                    print(
                        f"❌ Errore offerta: {error}",
                        flush=True,
                    )

            time.sleep(INTERVAL)

        except Exception as error:
            print(
                f"❌ Worker: {error}",
                flush=True,
            )

            time.sleep(INTERVAL)


# ============================================================
# START WORKER
# ============================================================

def start_worker():
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        _worker_started = True

        thread = threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        )

        thread.start()

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
        "prepare_mode":
            "LIVE_SCHEMA_AWARE",
        "settlement_currency": "EUR",
        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",
        "price_eur_mode":
            "EUR_DIRECT_OR_USD_CONVERTED",
        "usd_conversion": True,
        "usd_conversion_mode":
            "USD_CENTS_TO_EUR",
        "usd_eur_cache_seconds":
            USD_EUR_CACHE_SECONDS,
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
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
    })


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
        ),
    )
