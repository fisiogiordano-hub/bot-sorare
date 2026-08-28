import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
import requests

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)

# ------------------------------------------------------------
# PREZZI
# ------------------------------------------------------------
#
# Tutti i prezzi qui sono CENTESIMI EUR.
#
# €0.32 = 32
# €0.70 = 70
#
MIN_PRICE = 32
MAX_PRICE = 70

# €0.20 per carta
PAY_PER_CARD = 20

# Deve essere strettamente minore di 28
MAX_AGE = 28

# Polling offerte
INTERVAL = 10

# Timeout HTTP
TIMEOUT = 30

# Se non troviamo un prezzo fiat verificabile,
# lasciamo l'offerta pending e ritentiamo.
UNKNOWN_PRICE_RETRY = 60

# Cache USD/EUR
USD_EUR_CACHE_SECONDS = 300

BOT_VERSION = "20.0"

# ------------------------------------------------------------
# KULENOVIC
# ------------------------------------------------------------

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


def safe_int(value):

    if value is None:
        return None

    try:
        return int(
            str(value).strip()
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def safe_decimal(value):

    if value is None:
        return None

    try:
        return Decimal(
            str(value).strip()
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):
        return None


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

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                try:
                    wait = int(
                        retry_after
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    wait = attempt * 3

                print(
                    f"⏳ Rate limit Sorare: {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    f"❌ HTTP {response.status_code}: "
                    f"{response.text[:1500]}",
                    flush=True,
                )

                time.sleep(attempt)
                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = response.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )

                return None

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

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
                    "User-Agent": (
                        f"Sorare-Bot/{BOT_VERSION}"
                    ),
                },
            )

            print(
                f"📚 Schema Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    "⚠️ Impossibile scaricare "
                    "lo schema live",
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
                f"⚠️ Errore download schema: "
                f"{error}",
                flush=True,
            )

            return None


def get_input_fields(type_name):

    schema = get_live_schema()

    if not schema:
        return set()

    match = re.search(
        r"\binput\s+"
        + re.escape(type_name)
        + r"\s*\{",
        schema,
        re.MULTILINE,
    )

    if not match:

        print(
            f"⚠️ {type_name} non trovato "
            "nello schema",
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

    block = schema[
        start:position - 1
    ]

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

            fields.add(
                field_match.group(1)
            )

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

    if prepare_fields:

        print(
            "   prepareOfferInput:",
            flush=True,
        )

        for field in sorted(
            prepare_fields
        ):

            print(
                f"      • {field}",
                flush=True,
            )

    if create_fields:

        print(
            "   createDirectOfferInput:",
            flush=True,
        )

        for field in sorted(
            create_fields
        ):

            print(
                f"      • {field}",
                flush=True,
            )

    required_prepare = {
        "receiveAssetIds",
        "sendAssetIds",
        "sendAmount",
        "receiverSlug",
        "settlementCurrencies",
    }

    required_create = {
        "approvals",
        "receiveAssetIds",
        "sendAssetIds",
        "sendAmount",
        "receiverSlug",
    }

    if not required_prepare.issubset(
        prepare_fields
    ):

        print(
            "⚠️ SCHEMA prepareOfferInput "
            "INATTESO",
            flush=True,
        )

    if not required_create.issubset(
        create_fields
    ):

        print(
            "⚠️ SCHEMA createDirectOfferInput "
            "INATTESO",
            flush=True,
        )

    if (
        required_prepare.issubset(
            prepare_fields
        )
        and
        required_create.issubset(
            create_fields
        )
    ):

        print(
            "✅ SCHEMA OFFER CONFERMATO",
            flush=True,
        )

        print(
            "🟢 Schema corrente compatibile.",
            flush=True,
        )

    return (
        prepare_fields,
        create_fields,
    )


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

    nickname = (
        user.get("nickname")
        or user.get("slug")
        or "Sconosciuto"
    )

    print(
        f"✅ Sorare: {nickname}",
        flush=True,
    )

    if user.get("starkKey"):

        print(
            "🔐 Stark key account: PRESENTE",
            flush=True,
        )

    else:

        print(
            "⚠️ Stark key account: ASSENTE",
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

    connection = (
        user.get(
            "pendingTokenOffersReceived"
        )
        or {}
    )

    return (
        connection.get("nodes")
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
        query Cards(
            $assetIds: [String!]!
        ) {
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
        ((data.get("data") or {})
         .get("anyCards"))
        or []
    )


# ============================================================
# USD -> EUR
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
                f"💱 USD/EUR HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    "❌ Cambio USD/EUR "
                    "non disponibile",
                    flush=True,
                )

                return None

            data = response.json()

            rate = (
                (data.get("rates") or {})
                .get("EUR")
            )

            rate = safe_decimal(rate)

            if rate is None or rate <= 0:

                print(
                    "❌ Cambio USD/EUR "
                    "non valido",
                    flush=True,
                )

                return None

            _usd_eur_cache = rate
            _usd_eur_cache_time = now

            print(
                f"💱 1 USD = "
                f"{rate:.6f} EUR",
                flush=True,
            )

            return rate

        except Exception as error:

            print(
                f"❌ Errore cambio USD/EUR: "
                f"{error}",
                flush=True,
            )

            return None


def usd_cents_to_eur_cents(
    usd_cents
):

    value = safe_decimal(
        usd_cents
    )

    if value is None or value <= 0:
        return None

    rate = get_usd_eur_rate()

    if rate is None:
        return None

    eur_cents = (
        value * rate
    ).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )

    eur_cents = int(
        eur_cents
    )

    if eur_cents <= 0:
        return None

    print(
        f"💵 {usd_cents} USD cents "
        f"→ {eur_cents} EUR cents",
        flush=True,
    )

    return eur_cents


# ============================================================
# PRICE EXTRACTION
# ============================================================

def extract_fiat_price_eur_cents(
    amounts
):
    """
    REGOLA FONDAMENTALE.

    Per il floor FIAT utilizziamo:

    1. eurCents
    2. usdCents -> USD/EUR

    NON utilizziamo:

    - wei
    - referenceCurrency=WEI
    - referenceCurrency=ETH
    - conversioni crypto arbitrarie

    Motivo:
    il bot deve confrontare il floor con il range
    EUR €0.32 - €0.70 e la controproposta viene
    costruita in EUR.

    Quindi un prezzo crypto non verificabile come
    prezzo fiat viene classificato UNKNOWN_PRICE.
    """

    if not isinstance(
        amounts,
        dict,
    ):

        return None

    eur_cents = amounts.get(
        "eurCents"
    )

    usd_cents = amounts.get(
        "usdCents"
    )

    reference_currency = str(
        amounts.get(
            "referenceCurrency"
        )
        or ""
    ).strip().upper()

    wei = amounts.get(
        "wei"
    )

    # ========================================================
    # 1. EUR DIRETTO
    # ========================================================

    eur_value = safe_int(
        eur_cents
    )

    if (
        eur_value is not None
        and
        eur_value > 0
    ):

        print(
            f"💶 Prezzo EUR diretto: "
            f"€{eur_value / 100:.2f}",
            flush=True,
        )

        return eur_value

    # ========================================================
    # 2. USD CENTS -> EUR
    # ========================================================

    usd_value = safe_int(
        usd_cents
    )

    if (
        usd_value is not None
        and
        usd_value > 0
    ):

        print(
            f"💵 Prezzo USD cents: "
            f"{usd_value}",
            flush=True,
        )

        converted = (
            usd_cents_to_eur_cents(
                usd_value
            )
        )

        if converted is not None:

            print(
                f"✅ USD → EUR: "
                f"€{converted / 100:.2f}",
                flush=True,
            )

            return converted

        print(
            "⚠️ USD presente ma "
            "conversione impossibile",
            flush=True,
        )

    # ========================================================
    # 3. CRYPTO — MAI USARE PER IL FLOOR FIAT
    # ========================================================

    if wei is not None:

        print(
            "🚫 WEI IGNORATO PER IL FLOOR FIAT",
            flush=True,
        )

        print(
            f"   referenceCurrency="
            f"'{reference_currency}'",
            flush=True,
        )

        print(
            "   🛑 Nessuna conversione "
            "wei → EUR effettuata.",
            flush=True,
        )

    # ========================================================
    # 4. UNKNOWN
    # ========================================================

    return None


# ============================================================
# CARD MATCH
# ============================================================

def card_matches_target(
    card,
    player_slug,
    rarity,
    season,
):

    if not isinstance(
        card,
        dict,
    ):

        return False

    card_player = (
        card.get("anyPlayer")
        or {}
    )

    card_player_slug = slug(
        card_player.get("slug")
    )

    card_rarity = str(
        card.get("rarityTyped")
        or ""
    ).strip().lower()

    card_season = safe_int(
        card.get("seasonYear")
    )

    return (
        card_player_slug
        == player_slug
        and
        card_rarity
        == rarity
        and
        card_season
        == season
    )


# ============================================================
# LIVE SINGLE SALE FLOOR
# ============================================================

def get_live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = slug(
        player.get("slug")
    )

    rarity = str(
        card.get("rarityTyped")
        or ""
    ).strip().lower()

    season = safe_int(
        card.get("seasonYear")
    )

    print(
        "      🔎 FLOOR LIVE SINGLE SALE",
        flush=True,
    )

    print(
        f"         playerSlug: "
        f"{player_slug}",
        flush=True,
    )

    print(
        f"         rarità: "
        f"{rarity}",
        flush=True,
    )

    print(
        f"         stagione: "
        f"{season}",
        flush=True,
    )

    if not player_slug:

        print(
            "      ❌ playerSlug mancante",
            flush=True,
        )

        return None

    if not rarity:

        print(
            "      ❌ rarità mancante",
            flush=True,
        )

        return None

    if season is None:

        print(
            "      ❌ stagione mancante",
            flush=True,
        )

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

            print(
                "      ❌ Nessuna risposta "
                "LIVE SINGLE SALE",
                flush=True,
            )

            return None

        if data.get("errors"):

            print(
                "      ❌ GraphQL LIVE "
                "SINGLE SALE:",
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

        tokens = (
            (data.get("data") or {})
            .get("tokens")
            or {}
        )

        connection = (
            tokens.get(
                "liveSingleSaleOffers"
            )
            or {}
        )

        nodes = (
            connection.get("nodes")
            or []
        )

        print(
            f"         📦 pagina {page}: "
            f"{len(nodes)} offerte",
            flush=True,
        )

        for offer in nodes:

            offer_id = str(
                offer.get("id")
                or ""
            )

            sender_side = (
                offer.get(
                    "senderSide"
                )
                or {}
            )

            offer_cards = (
                sender_side.get(
                    "anyCards"
                )
                or []
            )

            # ------------------------------------------------
            # SINGLE SALE = ESATTAMENTE UNA CARTA
            # ------------------------------------------------

            if len(offer_cards) != 1:

                print(
                    f"         ⏭️ {offer_id}: "
                    f"ignorata, "
                    f"{len(offer_cards)} carte",
                    flush=True,
                )

                continue

            offer_card = (
                offer_cards[0]
            )

            # ------------------------------------------------
            # MATCH ESATTO
            # ------------------------------------------------

            if not card_matches_target(
                offer_card,
                player_slug,
                rarity,
                season,
            ):

                continue

            # ------------------------------------------------
            # IL PREZZO È QUELLO CHE IL BUYER PAGA
            # ------------------------------------------------

            receiver_side = (
                offer.get(
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

            # ------------------------------------------------
            # LOG DIAGNOSTICO
            # ------------------------------------------------

            print(
                f"         🎯 MATCH: "
                f"{offer_id}",
                flush=True,
            )

            print(
                "            amounts:",
                flush=True,
            )

            print(
                json.dumps(
                    amounts,
                    ensure_ascii=False,
                ),
                flush=True,
            )

            # ------------------------------------------------
            # PREZZO FIAT
            # ------------------------------------------------

            price = (
                extract_fiat_price_eur_cents(
                    amounts
                )
            )

            if price is None:

                print(
                    "         ⚠️ MATCH "
                    "SENZA PREZZO FIAT "
                    "VERIFICABILE",
                    flush=True,
                )

                continue

            # ------------------------------------------------
            # RANGE DI SICUREZZA
            # ------------------------------------------------
            #
            # Non eliminiamo prezzi fuori range:
            # un floor può essere €1.50.
            #
            # Li raccogliamo comunque perché stiamo
            # cercando il vero minimo.
            # ------------------------------------------------

            prices.append(
                (
                    price,
                    offer_id,
                )
            )

            print(
                f"            💰 "
                f"Prezzo verificato: "
                f"€{price / 100:.2f}",
                flush=True,
            )

        # ----------------------------------------------------
        # PAGINATION
        # ----------------------------------------------------

        page_info = (
            connection.get(
                "pageInfo"
            )
            or {}
        )

        has_next = bool(
            page_info.get(
                "hasNextPage"
            )
        )

        next_cursor = (
            page_info.get(
                "endCursor"
            )
        )

        if not has_next:
            break

        if not next_cursor:
            break

        if next_cursor == cursor:
            break

        cursor = next_cursor

    # ========================================================
    # NESSUN PREZZO
    # ========================================================

    if not prices:

        print(
            "      ❌ Nessun prezzo FIAT "
            "verificabile trovato",
            flush=True,
        )

        return None

    # ========================================================
    # MINIMO REALE
    # ========================================================

    prices.sort(
        key=lambda item: item[0]
    )

    floor = prices[0][0]
    offer_id = prices[0][1]

    print(
        "=" * 40,
        flush=True,
    )

    print(
        f"      💰 FLOOR FIAT VERIFICATO: "
        f"€{floor / 100:.2f}",
        flush=True,
    )

    print(
        f"         🆔 {offer_id}",
        flush=True,
    )

    print(
        f"         📊 offerte compatibili: "
        f"{len(prices)}",
        flush=True,
    )

    print(
        "=" * 40,
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
        wanted.add(
            KID.lower()
        )

    asset_id = str(
        card.get("assetId")
        or ""
    ).lower()

    card_slug = str(
        card.get("slug")
        or ""
    ).lower()

    return (
        asset_id in wanted
        or
        card_slug in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def get_competitions(card):

    club = (
        card.get("anyPlayer")
        or {}
    ).get("activeClub")

    if not isinstance(
        club,
        dict,
    ):

        return []

    result = []

    for competition in (
        club.get(
            "activeCompetitions"
        )
        or []
    ):

        if not isinstance(
            competition,
            dict,
        ):

            continue

        value = slug(
            competition.get(
                "slug"
            )
        )

        if value:
            result.append(value)

    return list(
        dict.fromkeys(
            result
        )
    )


def check_competition(card):

    club = (
        card.get("anyPlayer")
        or {}
    ).get("activeClub")

    if not isinstance(
        club,
        dict,
    ):

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

    competitions = get_competitions(
        card
    )

    print(
        f"      🏟️ Squadra: {name}",
        flush=True,
    )

    if not competitions:

        print(
            "      ❌ Nessuna "
            "activeCompetition",
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
        card.get("rarityTyped")
        or ""
    ).upper()

    player = (
        card.get("anyPlayer")
        or {}
    )

    age = safe_int(
        player.get("age")
    )

    print(
        f"   📄 {name}",
        flush=True,
    )

    if age is None:

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return (
            False,
            "invalid",
        )

    print(
        f"      🎂 Età: {age}",
        flush=True,
    )

    # --------------------------------------------------------
    # AGE
    # --------------------------------------------------------

    if age >= MAX_AGE:

        print(
            f"      ❌ Età troppo alta "
            f"(limite: < {MAX_AGE})",
            flush=True,
        )

        return (
            False,
            "invalid",
        )

    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )

        return (
            False,
            "invalid",
        )

    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    price = get_live_floor(
        card
    )

    if price is None:

        print(
            "      ⚠️ FLOOR FIAT NON TROVATO",
            flush=True,
        )

        print(
            "      🟡 Carta NON classificata "
            "come non idonea",
            flush=True,
        )

        print(
            "      🟡 Offerta lasciata "
            "IN SOSPESO",
            flush=True,
        )

        return (
            False,
            "unknown_price",
        )

    print(
        f"      💰 Floor: "
        f"€{price / 100:.2f}",
        flush=True,
    )

    # --------------------------------------------------------
    # PRICE RANGE
    # --------------------------------------------------------

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )

        return (
            False,
            "invalid",
        )

    # --------------------------------------------------------
    # COMPETITION
    # --------------------------------------------------------

    if not check_competition(
        card
    ):

        return (
            False,
            "invalid",
        )

    competitions = get_competitions(
        card
    )

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return (
        True,
        "valid",
    )


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):

    blockchain_id = str(
        offer.get(
            "blockchainId"
        )
        or ""
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
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            ),
        }
    })

    if not data:

        print(
            "❌ Nessuna risposta rejectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ rejectOffer GRAPHQL ERROR",
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
        .get("rejectOffer")
    )

    if not result:

        print(
            "❌ Risposta rejectOffer vuota",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

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
# SIGN AUTHORIZATIONS
# ============================================================

def sign_authorizations(
    authorizations
):

    node = (
        shutil.which("node")
        or
        shutil.which("nodejs")
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
            "Output firma Node.js "
            "non valido"
        )


# ============================================================
# PREPARE INPUT
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
            "Impossibile leggere "
            "prepareOfferInput"
        )

    result = {}

    # --------------------------------------------------------
    # IMPORTANTISSIMO:
    #
    # Stiamo facendo una DIRECT OFFER:
    #
    # noi riceviamo le carte
    # noi inviamo EUR
    # --------------------------------------------------------

    if "receiveAssetIds" in fields:

        result[
            "receiveAssetIds"
        ] = asset_ids

    if "sendAssetIds" in fields:

        result[
            "sendAssetIds"
        ] = []

    if "sendAmount" in fields:

        result[
            "sendAmount"
        ] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:

        result[
            "receiverSlug"
        ] = receiver

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = [
            "EUR"
        ]

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(
            uuid.uuid4()
        )

    return result


# ============================================================
# CREATE INPUT
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
            "Impossibile leggere "
            "createDirectOfferInput"
        )

    result = {}

    if "approvals" in fields:

        result[
            "approvals"
        ] = approvals

    if "dealId" in fields:

        result[
            "dealId"
        ] = str(
            uuid.uuid4()
        )

    if "sendAssetIds" in fields:

        result[
            "sendAssetIds"
        ] = []

    if "receiveAssetIds" in fields:

        result[
            "receiveAssetIds"
        ] = asset_ids

    if "sendAmount" in fields:

        result[
            "sendAmount"
        ] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:

        result[
            "receiverSlug"
        ] = receiver

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(
            uuid.uuid4()
        )

    return result


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(
    offer,
    cards,
):

    sender = (
        offer.get("sender")
        or {}
    )

    receiver = str(
        sender.get("slug")
        or ""
    ).strip()

    asset_ids = [
        str(
            card.get("assetId")
        ).strip()
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

    amount = (
        len(asset_ids)
        * PAY_PER_CARD
    )

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
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True,
        )

        return True

    if not STARK:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    # ========================================================
    # PREPARE
    # ========================================================

    try:

        prepare_input = (
            build_prepare_offer_input(
                asset_ids,
                receiver,
                amount,
            )
        )

    except Exception as error:

        print(
            f"❌ Costruzione "
            f"prepareOfferInput: "
            f"{error}",
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
            prepareOffer(
                input: $input
            ) {
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
            "❌ Nessuna risposta "
            "da prepareOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ prepareOffer "
            "GRAPHQL GLOBAL ERROR:",
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

        print(
            "❌ prepareOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ prepareOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    authorizations = (
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        print(
            "❌ Nessuna autorizzazione "
            "restituita",
            flush=True,
        )

        return False

    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )

    # ========================================================
    # SIGN
    # ========================================================

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

    # ========================================================
    # CREATE
    # ========================================================

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
            f"createDirectOfferInput: "
            f"{error}",
            flush=True,
        )

        return False

    debug = dict(
        create_input
    )

    if "approvals" in debug:

        debug["approvals"] = (
            f"{len(approvals)} "
            f"authorization(s)"
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
        "input": create_input,
    })

    if not data:

        print(
            "❌ Nessuna risposta "
            "da createDirectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ createDirectOffer "
            "GRAPHQL GLOBAL ERROR:",
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

        print(
            "❌ createDirectOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ createDirectOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    token_offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    offer_id = token_offer.get(
        "id"
    )

    if not offer_id:

        print(
            "❌ Nessuna offerta creata "
            "da Sorare",
            flush=True,
        )

        return False

    print(
        "=" * 40,
        flush=True,
    )

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{offer_id}",
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

    print(
        "=" * 40,
        flush=True,
    )

    return True


# ============================================================
# UNKNOWN PRICE RETRY
# ============================================================

def should_retry_unknown(
    offer_id
):

    now = time.time()

    with state_lock:

        last_attempt = (
            unknown_price_offers.get(
                offer_id
            )
        )

        if last_attempt is None:

            unknown_price_offers[
                offer_id
            ] = now

            return True

        if (
            now - last_attempt
            >= UNKNOWN_PRICE_RETRY
        ):

            unknown_price_offers[
                offer_id
            ] = now

            return True

        return False


def mark_completed(
    offer_id
):

    with state_lock:

        processed.add(
            offer_id
        )

        unknown_price_offers.pop(
            offer_id,
            None,
        )


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(
    offer
):

    offer_id = str(
        offer.get("id")
        or ""
    ).strip()

    if not offer_id:
        return

    # --------------------------------------------------------
    # GIÀ COMPLETATA
    # --------------------------------------------------------

    with state_lock:

        if offer_id in processed:
            return

    print(
        "\n" + "=" * 40,
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )

    # --------------------------------------------------------
    # RETRY
    # --------------------------------------------------------

    if not should_retry_unknown(
        offer_id
    ):

        print(
            "⏳ Floor ancora sconosciuto: "
            "attendo prossimo tentativo",
            flush=True,
        )

        return

    sender_cards = (
        (
            offer.get(
                "senderSide"
            )
            or {}
        ).get(
            "anyCards"
        )
        or []
    )

    receiver_cards = (
        (
            offer.get(
                "receiverSide"
            )
            or {}
        ).get(
            "anyCards"
        )
        or []
    )

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if not any(
        is_kulenovic(card)
        for card in receiver_cards
    ):

        print(
            "⏭️ Kulenovic non presente: "
            "ignoro",
            flush=True,
        )

        mark_completed(
            offer_id
        )

        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    # --------------------------------------------------------
    # CARTE OFFERTE
    # --------------------------------------------------------

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

        mark_completed(
            offer_id
        )

        return

    # --------------------------------------------------------
    # DETTAGLI
    # --------------------------------------------------------

    cards = card_details(
        ids
    )

    if len(cards) != len(ids):

        print(
            "❌ Impossibile verificare "
            "tutte le carte",
            flush=True,
        )

        return

    print(
        f"🔎 Controllo {len(cards)} carta/e",
        flush=True,
    )

    valid_cards = []

    has_unknown_price = False
    invalid_cards = 0

    # --------------------------------------------------------
    # VALIDAZIONE
    # --------------------------------------------------------

    for card in cards:

        try:

            valid, reason = valid_card(
                card
            )

            if valid:

                valid_cards.append(
                    card
                )

            elif reason == "unknown_price":

                has_unknown_price = True

            else:

                invalid_cards += 1

        except Exception as error:

            print(
                f"❌ Errore controllo carta: "
                f"{error}",
                flush=True,
            )

            return

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/{len(cards)}",
        flush=True,
    )

    # ========================================================
    # PRICE UNKNOWN
    # ========================================================

    if has_unknown_price:

        print(
            "⚠️ ALMENO UNA CARTA "
            "NON HA UN FLOOR FIAT "
            "VERIFICABILE.",
            flush=True,
        )

        print(
            "🟡 NESSUNA AZIONE AUTOMATICA.",
            flush=True,
        )

        print(
            "🟡 OFFERTA LASCIATA "
            "IN SOSPESO.",
            flush=True,
        )

        print(
            "🛑 Niente rifiuto.",
            flush=True,
        )

        print(
            "🛑 Niente controproposta.",
            flush=True,
        )

        print(
            "🔁 Verrà ritentata "
            f"tra circa "
            f"{UNKNOWN_PRICE_RETRY}s.",
            flush=True,
        )

        return

    # ========================================================
    # NESSUNA CARTA VALIDA
    # ========================================================

    if not valid_cards:

        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        if reject_offer(
            offer
        ):

            mark_completed(
                offer_id
            )

        return

    # ========================================================
    # CARTA VALIDA
    # ========================================================

    rejected = (
        len(cards)
        - len(valid_cards)
    )

    if rejected:

        print(
            f"⚠️ {rejected} carta/e "
            "esclusa/e",
            flush=True,
        )

    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    if counter_offer(
        offer,
        valid_cards,
    ):

        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

        # ----------------------------------------------------
        # Dopo aver creato la controproposta,
        # rifiutiamo l'originale.
        # ----------------------------------------------------

        if reject_offer(
            offer
        ):

            mark_completed(
                offer_id
            )

        else:

            print(
                "⚠️ Controproposta creata "
                "ma impossibile rifiutare "
                "l'offerta originale.",
                flush=True,
            )

    else:

        print(
            "🔴 Controproposta NON creata.",
            flush=True,
        )

        print(
            "🟡 Offerta originale "
            "lasciata IN SOSPESO.",
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
        f"📦 VERSIONE BOT: "
        f"{BOT_VERSION}",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} "
        f"per carta",
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
        "🏆 COMPETIZIONI: "
        "tutte le activeCompetitions",
        flush=True,
    )

    print(
        "💰 PRICE SOURCE: "
        "liveSingleSaleOffers",
        flush=True,
    )

    print(
        "🎯 MATCH PRICE: "
        "player + rarity + season",
        flush=True,
    )

    print(
        "💶 EUR: eurCents",
        flush=True,
    )

    print(
        "💵 USD: usdCents → EUR",
        flush=True,
    )

    print(
        "🚫 WEI: ESCLUSO dal floor FIAT",
        flush=True,
    )

    print(
        "🚫 referenceCurrency=WEI "
        "NON viene interpretato come ETH",
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
        "🛡️ PRICE UNKNOWN: "
        "LEAVE PENDING",
        flush=True,
    )

    print(
        f"🔁 RETRY: "
        f"{UNKNOWN_PRICE_RETRY}s",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():

        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # SCHEMA
    # --------------------------------------------------------

    inspect_live_schema()

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True,
            )

            for offer in offers:

                try:

                    process_offer(
                        offer
                    )

                except Exception as error:

                    print(
                        f"❌ Errore offerta: "
                        f"{error}",
                        flush=True,
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as error:

            print(
                f"❌ Worker: {error}",
                flush=True,
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# START WORKER
# ============================================================

def start_worker():

    global _worker_started

    with _worker_lock:

        if _worker_started:
            return

        _worker_started = True

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

        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",

        "prepare_mode":
            "LIVE_SCHEMA_AWARE",

        "settlement_currency":
            "EUR",

        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

        "price_fiat_priority": [
            "eurCents",
            "usdCents"
        ],

        "usd_conversion":
            True,

        "usd_conversion_mode":
            "LIVE_USD_EUR_RATE_WITH_CACHE",

        "usd_eur_cache_seconds":
            USD_EUR_CACHE_SECONDS,

        "wei_conversion":
            False,

        "wei_used_for_fiat_floor":
            False,

        "reference_currency_wei":
            "IGNORED",

        "latest_english_auction_fallback":
            False,

        "public_min_prices":
            False,

        "lowest_price_card":
            False,

        "token_prices":
            False,

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
# MAIN
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000",
            )
        ),
    )
