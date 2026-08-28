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

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)

# ============================================================
# STRATEGIA
# ============================================================

# Prezzi in CENTESIMI di EUR
MIN_PRICE = 32
MAX_PRICE = 70

# Pagamento controproposta:
# €0,20 per carta
PAY_PER_CARD = 20

# Età strettamente inferiore a 28
MAX_AGE = 28

INTERVAL = 10
TIMEOUT = 30

# Se il prezzo non può essere determinato
# con una valuta supportata, l'offerta resta pending.
UNKNOWN_PRICE_RETRY = 60

# Cache cambi fiat
USD_EUR_CACHE_SECONDS = 300
GBP_EUR_CACHE_SECONDS = 300

# Cache cambio ETH/EUR Sorare
ETH_EUR_CACHE_SECONDS = 300

BOT_VERSION = "20.0"


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

_gbp_eur_lock = threading.Lock()
_gbp_eur_cache = None
_gbp_eur_cache_time = 0

_eth_eur_lock = threading.Lock()
_eth_eur_cache = None
_eth_eur_cache_time = 0


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

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                try:
                    wait = int(
                        response.headers.get(
                            "Retry-After",
                            attempt * 3,
                        )
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

        try:

            print(
                "📚 Controllo schema Sorare corrente...",
                flush=True,
            )

            response = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
                },
            )

            print(
                f"📚 Schema Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    "❌ Impossibile scaricare "
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
                f"❌ Errore download schema: {error}",
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
        "dealId",
        "receiveAssetIds",
        "receiverSlug",
        "sendAmount",
        "sendAssetIds",
    }

    missing_prepare = (
        required_prepare
        - prepare_fields
    )

    missing_create = (
        required_create
        - create_fields
    )

    if missing_prepare:

        print(
            "🛑 PREPARE INCOMPATIBILE.",
            flush=True,
        )

        print(
            "   Mancano:",
            ", ".join(
                sorted(missing_prepare)
            ),
            flush=True,
        )

        return False

    if missing_create:

        print(
            "🛑 CREATE INCOMPATIBILE.",
            flush=True,
        )

        print(
            "   Mancano:",
            ", ".join(
                sorted(missing_create)
            ),
            flush=True,
        )

        return False

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
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    stark_key = user.get("starkKey")

    print(
        "🔐 Stark key account: "
        + (
            "PRESENTE"
            if stark_key
            else "ASSENTE"
        ),
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
# FIAT RATES
# ============================================================

def get_fiat_eur_rate(
    currency,
    cache_seconds,
    cache_lock,
    cache_value_name,
):
    """
    Recupera:
        1 USD -> EUR
    oppure:
        1 GBP -> EUR

    Usa Frankfurter.
    """

    global _usd_eur_cache
    global _usd_eur_cache_time
    global _gbp_eur_cache
    global _gbp_eur_cache_time

    currency = currency.upper()

    now = time.time()

    with cache_lock:

        if currency == "USD":

            if (
                _usd_eur_cache is not None
                and
                now - _usd_eur_cache_time
                < cache_seconds
            ):

                return _usd_eur_cache

        elif currency == "GBP":

            if (
                _gbp_eur_cache is not None
                and
                now - _gbp_eur_cache_time
                < cache_seconds
            ):

                return _gbp_eur_cache

        else:
            return None

        try:

            response = requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from": currency,
                    "to": "EUR",
                },
                timeout=TIMEOUT,
            )

            print(
                f"💱 {currency}/EUR HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    f"❌ Cambio {currency}/EUR "
                    "non disponibile",
                    flush=True,
                )

                return None

            data = response.json()

            rate = (
                (data.get("rates") or {})
                .get("EUR")
            )

            rate = float(rate)

            if rate <= 0:
                raise ValueError(
                    f"Cambio {currency}/EUR non valido"
                )

            if currency == "USD":

                _usd_eur_cache = rate
                _usd_eur_cache_time = now

            elif currency == "GBP":

                _gbp_eur_cache = rate
                _gbp_eur_cache_time = now

            print(
                f"💱 1 {currency} = "
                f"{rate:.6f} EUR",
                flush=True,
            )

            return rate

        except Exception as error:

            print(
                f"❌ Errore cambio "
                f"{currency}/EUR: {error}",
                flush=True,
            )

            return None


def usd_cents_to_eur_cents(
    usd_cents
):

    try:

        usd_cents = int(
            str(usd_cents).strip()
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if usd_cents <= 0:
        return None

    rate = get_fiat_eur_rate(
        "USD",
        USD_EUR_CACHE_SECONDS,
        _usd_eur_lock,
        "_usd_eur_cache",
    )

    if rate is None:
        return None

    eur_cents = int(
        round(
            usd_cents * rate
        )
    )

    if eur_cents <= 0:
        return None

    print(
        f"💵 {usd_cents} USD cents "
        f"→ {eur_cents} EUR cents",
        flush=True,
    )

    return eur_cents


def gbp_cents_to_eur_cents(
    gbp_cents
):

    try:

        gbp_cents = int(
            str(gbp_cents).strip()
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if gbp_cents <= 0:
        return None

    rate = get_fiat_eur_rate(
        "GBP",
        GBP_EUR_CACHE_SECONDS,
        _gbp_eur_lock,
        "_gbp_eur_cache",
    )

    if rate is None:
        return None

    eur_cents = int(
        round(
            gbp_cents * rate
        )
    )

    if eur_cents <= 0:
        return None

    print(
        f"💷 {gbp_cents} GBP cents "
        f"→ {eur_cents} EUR cents",
        flush=True,
    )

    return eur_cents


# ============================================================
# ETH / EUR
# ============================================================

def get_eth_eur_cents_rate():

    global _eth_eur_cache
    global _eth_eur_cache_time

    now = time.time()

    with _eth_eur_lock:

        if (
            _eth_eur_cache is not None
            and
            now - _eth_eur_cache_time
            < ETH_EUR_CACHE_SECONDS
        ):

            return _eth_eur_cache

        data = graphql("""
            query ExchangeRate {
                config {
                    exchangeRate {
                        id
                        time

                        ethRates {
                            eurCents
                        }
                    }
                }
            }
        """)

        if not data:

            print(
                "❌ Impossibile leggere "
                "il cambio ETH/EUR",
                flush=True,
            )

            return None

        if data.get("errors"):

            print(
                "❌ Errore cambio ETH/EUR:",
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

        exchange_rate = (
            (
                (data.get("data") or {})
                .get("config")
                or {}
            )
            .get("exchangeRate")
            or {}
        )

        eth_rates = (
            exchange_rate.get(
                "ethRates"
            )
            or {}
        )

        value = eth_rates.get(
            "eurCents"
        )

        try:

            value = int(value)

        except (
            TypeError,
            ValueError,
        ):
            value = None

        if not value or value <= 0:

            print(
                "❌ Cambio ETH/EUR non valido",
                flush=True,
            )

            return None

        _eth_eur_cache = value
        _eth_eur_cache_time = now

        print(
            f"💱 Sorare ETH/EUR: "
            f"{value} EUR cents / ETH",
            flush=True,
        )

        return value


def wei_to_eur_cents(wei):

    if wei is None:
        return None

    try:

        wei = int(
            str(wei).strip()
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if wei <= 0:
        return None

    eur_cents_per_eth = (
        get_eth_eur_cents_rate()
    )

    if not eur_cents_per_eth:
        return None

    # 1 ETH = 10^18 wei
    #
    # eur_cents_per_eth:
    # EUR cents per 1 ETH
    #
    # quindi:
    #
    # wei * eur_cents_per_eth / 10^18

    eur_cents = (
        wei
        * eur_cents_per_eth
    ) // 10**18

    if eur_cents <= 0:
        return None

    return int(eur_cents)


# ============================================================
# AUTHORITATIVE PRICE EXTRACTION
# ============================================================

def extract_offer_eur_cents(
    amounts
):
    """
    IMPORTANTISSIMO:

    MonetaryAmount Sorare contiene più rappresentazioni
    dello stesso prezzo:

        eurCents
        gbpCents
        usdCents
        wei
        referenceCurrency

    La valuta autorevole è referenceCurrency.

    NON scegliamo semplicemente il primo campo valorizzato.

    Regole:

        referenceCurrency = EUR
            -> eurCents

        referenceCurrency = USD
            -> usdCents -> EUR

        referenceCurrency = GBP
            -> gbpCents -> EUR

        referenceCurrency = WEI
            -> wei -> ETH/EUR

        referenceCurrency = LAMPORT
            -> NON supportato per questo bot
            -> UNKNOWN_PRICE

    Questo evita di mischiare il prezzo originale con
    conversioni secondarie presenti nello stesso MonetaryAmount.
    """

    if not isinstance(
        amounts,
        dict,
    ):
        return None

    reference_currency = str(
        amounts.get(
            "referenceCurrency"
        )
        or ""
    ).strip().upper()

    eur_cents = amounts.get(
        "eurCents"
    )

    usd_cents = amounts.get(
        "usdCents"
    )

    gbp_cents = amounts.get(
        "gbpCents"
    )

    wei = amounts.get(
        "wei"
    )

    print(
        "         💱 AMOUNTS:",
        flush=True,
    )

    print(
        json.dumps(
            amounts,
            ensure_ascii=False,
        ),
        flush=True,
    )

    print(
        f"         🎯 referenceCurrency="
        f"{reference_currency}",
        flush=True,
    )

    # ========================================================
    # EUR
    # ========================================================

    if reference_currency == "EUR":

        try:

            value = int(
                str(eur_cents).strip()
            )

        except (
            TypeError,
            ValueError,
        ):

            print(
                "         ❌ EUR reference ma "
                "eurCents non valido",
                flush=True,
            )

            return None

        if value <= 0:

            print(
                "         ❌ eurCents <= 0",
                flush=True,
            )

            return None

        print(
            f"         💶 PREZZO AUTOREVOLE EUR: "
            f"€{value / 100:.2f}",
            flush=True,
        )

        return value

    # ========================================================
    # USD
    # ========================================================

    if reference_currency == "USD":

        try:

            value = int(
                str(usd_cents).strip()
            )

        except (
            TypeError,
            ValueError,
        ):

            print(
                "         ❌ USD reference ma "
                "usdCents non valido",
                flush=True,
            )

            return None

        if value <= 0:

            print(
                "         ❌ usdCents <= 0",
                flush=True,
            )

            return None

        print(
            f"         💵 PREZZO AUTOREVOLE USD: "
            f"{value} cents",
            flush=True,
        )

        converted = (
            usd_cents_to_eur_cents(
                value
            )
        )

        if converted is None:

            print(
                "         🛑 USD → EUR impossibile",
                flush=True,
            )

            return None

        print(
            f"         ✅ PREZZO USATO: "
            f"€{converted / 100:.2f}",
            flush=True,
        )

        return converted

    # ========================================================
    # GBP
    # ========================================================

    if reference_currency == "GBP":

        try:

            value = int(
                str(gbp_cents).strip()
            )

        except (
            TypeError,
            ValueError,
        ):

            print(
                "         ❌ GBP reference ma "
                "gbpCents non valido",
                flush=True,
            )

            return None

        if value <= 0:

            print(
                "         ❌ gbpCents <= 0",
                flush=True,
            )

            return None

        print(
            f"         💷 PREZZO AUTOREVOLE GBP: "
            f"{value} cents",
            flush=True,
        )

        converted = (
            gbp_cents_to_eur_cents(
                value
            )
        )

        if converted is None:

            print(
                "         🛑 GBP → EUR impossibile",
                flush=True,
            )

            return None

        print(
            f"         ✅ PREZZO USATO: "
            f"€{converted / 100:.2f}",
            flush=True,
        )

        return converted

    # ========================================================
    # WEI
    # ========================================================

    if reference_currency == "WEI":

        print(
            "         💎 PREZZO AUTOREVOLE: WEI",
            flush=True,
        )

        if wei is None:

            print(
                "         ❌ referenceCurrency=WEI "
                "ma wei è NULL",
                flush=True,
            )

            return None

        try:

            wei_int = int(
                str(wei).strip()
            )

        except (
            TypeError,
            ValueError,
        ):

            print(
                "         ❌ wei non valido",
                flush=True,
            )

            return None

        if wei_int <= 0:

            print(
                "         ❌ wei <= 0",
                flush=True,
            )

            return None

        print(
            f"         💎 wei={wei_int}",
            flush=True,
        )

        converted = wei_to_eur_cents(
            wei_int
        )

        if converted is None:

            print(
                "         🛑 WEI → EUR impossibile",
                flush=True,
            )

            return None

        print(
            f"         ✅ PREZZO USATO: "
            f"€{converted / 100:.2f}",
            flush=True,
        )

        return converted

    # ========================================================
    # LAMPORT
    # ========================================================

    if reference_currency == "LAMPORT":

        print(
            "         ⚠️ referenceCurrency=LAMPORT",
            flush=True,
        )

        print(
            "         🛑 Conversione LAMPORT "
            "non implementata.",
            flush=True,
        )

        return None

    # ========================================================
    # UNKNOWN
    # ========================================================

    print(
        "         🛑 referenceCurrency "
        f"non supportata: '{reference_currency}'",
        flush=True,
    )

    print(
        "         🟡 Prezzo classificato "
        "come UNKNOWN_PRICE.",
        flush=True,
    )

    return None


# ============================================================
# LIVE SINGLE SALE FLOOR
# ============================================================

def get_live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = str(
        player.get("slug")
        or ""
    ).strip().lower()

    rarity = str(
        card.get("rarityTyped")
        or ""
    ).strip().upper()

    season = card.get(
        "seasonYear"
    )

    try:

        season = int(season)

    except (
        TypeError,
        ValueError,
    ):
        season = None

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
                                    gbpCents
                                    usdCents
                                    wei
                                    referenceCurrency
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
                                    gbpCents
                                    usdCents
                                    wei
                                    referenceCurrency
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
                "da liveSingleSaleOffers",
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

            offer_id = offer.get(
                "id"
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
            # MATCH ESATTO
            # ------------------------------------------------

            compatible = False

            matched_card = None

            for offer_card in offer_cards:

                offer_rarity = str(
                    offer_card.get(
                        "rarityTyped"
                    )
                    or ""
                ).strip().upper()

                offer_season = (
                    offer_card.get(
                        "seasonYear"
                    )
                )

                try:

                    offer_season = int(
                        offer_season
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    offer_season = None

                offer_player = (
                    offer_card.get(
                        "anyPlayer"
                    )
                    or {}
                )

                offer_player_slug = str(
                    offer_player.get(
                        "slug"
                    )
                    or ""
                ).strip().lower()

                # Player
                if (
                    offer_player_slug
                    != player_slug
                ):
                    continue

                # Rarity
                if (
                    offer_rarity
                    != rarity
                ):
                    continue

                # Season
                if (
                    offer_season
                    != season
                ):
                    continue

                compatible = True
                matched_card = offer_card

                break

            if not compatible:
                continue

            print(
                "\n         ────────────────────",
                flush=True,
            )

            print(
                f"         🧾 OFFER: {offer_id}",
                flush=True,
            )

            print(
                "         🎯 MATCH ESATTO:",
                flush=True,
            )

            print(
                f"            player={player_slug}",
                flush=True,
            )

            print(
                f"            rarity={rarity}",
                flush=True,
            )

            print(
                f"            season={season}",
                flush=True,
            )

            if matched_card:

                print(
                    f"            card="
                    f"{matched_card.get('slug')}",
                    flush=True,
                )

            # ------------------------------------------------
            # IMPORTO DAL RECEIVER SIDE
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

            price = (
                extract_offer_eur_cents(
                    amounts
                )
            )

            if price is None:

                print(
                    "         ⚠️ OFFER COMPATIBILE "
                    "MA PREZZO UNKNOWN",
                    flush=True,
                )

                print(
                    "         🟡 NON entra nel floor.",
                    flush=True,
                )

                continue

            print(
                f"         💰 PREZZO VALIDO: "
                f"€{price / 100:.2f}",
                flush=True,
            )

            prices.append(
                (
                    price,
                    offer_id,
                )
            )

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
    # FLOOR
    # ========================================================

    if not prices:

        print(
            "      ❌ NESSUNA LIVE SINGLE SALE "
            "COMPATIBILE CON PREZZO VERIFICABILE",
            flush=True,
        )

        return None

    prices.sort(
        key=lambda item: item[0]
    )

    floor = prices[0][0]
    offer_id = prices[0][1]

    print(
        "\n      ========================================",
        flush=True,
    )

    print(
        f"      💰 FLOOR LIVE ESATTO: "
        f"€{floor / 100:.2f}",
        flush=True,
    )

    print(
        f"         🆔 offerta floor: "
        f"{offer_id}",
        flush=True,
    )

    print(
        f"         📊 offerte compatibili "
        f"con prezzo verificabile: "
        f"{len(prices)}",
        flush=True,
    )

    # --------------------------------------------------------
    # TOP 10 PREZZI
    # --------------------------------------------------------

    print(
        "      📊 PREZZI ORDINATI:",
        flush=True,
    )

    for index, (
        price,
        item_offer_id,
    ) in enumerate(
        prices[:10],
        start=1,
    ):

        print(
            f"         {index}. "
            f"€{price / 100:.2f} "
            f"({item_offer_id})",
            flush=True,
        )

    print(
        "      ========================================",
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
        dict.fromkeys(result)
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
        card.get("rarityTyped")
        or ""
    ).strip().upper()

    player = (
        card.get("anyPlayer")
        or {}
    )

    try:

        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError,
    ):

        print(
            f"   📄 {name}",
            flush=True,
        )

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return (
            False,
            "invalid",
        )

    print(
        f"   📄 {name}",
        flush=True,
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
            f"(richiesto < {MAX_AGE})",
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
            "      ⚠️ FLOOR UNKNOWN",
            flush=True,
        )

        print(
            "      🟡 Carta NON dichiarata "
            "non idonea.",
            flush=True,
        )

        print(
            "      🟡 Offerta lasciata PENDING.",
            flush=True,
        )

        return (
            False,
            "unknown_price",
        )

    print(
        f"      💰 Floor verificato: "
        f"€{price / 100:.2f}",
        flush=True,
    )

    # --------------------------------------------------------
    # RANGE
    # --------------------------------------------------------

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            f"      ❌ Floor fuori range: "
            f"€{price / 100:.2f}",
            flush=True,
        )

        print(
            f"      ❌ Range richiesto: "
            f"€{MIN_PRICE / 100:.2f} - "
            f"€{MAX_PRICE / 100:.2f}",
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
        f"€{price / 100:.2f}",
        flush=True,
    )

    print(
        f"      🏆 {', '.join(competitions)}",
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

    result = (
        (data or {}).get("data") or {}
    ).get("rejectOffer")

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

        r.amount = BigInt(
            r.amount
        );
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

    except json.JSONDecodeError as error:

        raise RuntimeError(
            "Output firma JSON non valido: "
            f"{error}"
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
            "prepareOfferInput "
            "dallo schema live"
        )

    result = {}

    # --------------------------------------------------------
    # CARD DA RICEVERE
    # --------------------------------------------------------

    if "receiveAssetIds" in fields:

        result[
            "receiveAssetIds"
        ] = asset_ids

    # --------------------------------------------------------
    # NESSUNA CARTA DA CEDERE
    # --------------------------------------------------------

    if "sendAssetIds" in fields:

        result[
            "sendAssetIds"
        ] = []

    # --------------------------------------------------------
    # PAGAMENTO CHE RICEVERÀ IL MITTENTE
    # --------------------------------------------------------

    if "sendAmount" in fields:

        result[
            "sendAmount"
        ] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    # --------------------------------------------------------
    # DESTINATARIO
    # --------------------------------------------------------

    if "receiverSlug" in fields:

        result[
            "receiverSlug"
        ] = receiver

    # --------------------------------------------------------
    # VALUTA ACCETTATA
    # --------------------------------------------------------

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = [
            "EUR"
        ]

    # --------------------------------------------------------
    # CLIENT MUTATION ID
    # --------------------------------------------------------

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

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
            "createDirectOfferInput "
            "dallo schema live"
        )

    result = {}

    # --------------------------------------------------------
    # APPROVALS
    # --------------------------------------------------------

    if "approvals" in fields:

        result[
            "approvals"
        ] = approvals

    # --------------------------------------------------------
    # DEAL ID
    # --------------------------------------------------------

    if "dealId" in fields:

        result[
            "dealId"
        ] = str(uuid.uuid4())

    # --------------------------------------------------------
    # CARD DA CEDERE
    # --------------------------------------------------------

    if "sendAssetIds" in fields:

        result[
            "sendAssetIds"
        ] = []

    # --------------------------------------------------------
    # CARD DA RICEVERE
    # --------------------------------------------------------

    if "receiveAssetIds" in fields:

        result[
            "receiveAssetIds"
        ] = asset_ids

    # --------------------------------------------------------
    # PAGAMENTO
    # --------------------------------------------------------

    if "sendAmount" in fields:

        result[
            "sendAmount"
        ] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    # --------------------------------------------------------
    # RECEIVER
    # --------------------------------------------------------

    if "receiverSlug" in fields:

        result[
            "receiverSlug"
        ] = receiver

    # --------------------------------------------------------
    # CLIENT MUTATION ID
    # --------------------------------------------------------

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

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

    amount = (
        len(asset_ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 CONTROPROPOSTA:",
        flush=True,
    )

    print(
        f"   {len(asset_ids)} carta/e",
        flush=True,
    )

    print(
        f"   €{amount / 100:.2f}",
        flush=True,
    )

    print(
        f"   👤 Receiver: {receiver}",
        flush=True,
    )

    print(
        "   🎯 Kulenovic NON viene ceduto",
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
            f"prepareOfferInput: {error}",
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
        "=" * 45,
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
        "=" * 45,
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
        "\n" + "=" * 45,
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
            "⏳ Floor UNKNOWN già controllato: "
            "attendo retry",
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
            "⏭️ Kulenovic non presente: ignoro",
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
    # UNKNOWN PRICE
    # ========================================================

    if has_unknown_price:

        print(
            "⚠️ ALMENO UNA CARTA "
            "HA PREZZO UNKNOWN.",
            flush=True,
        )

        print(
            "🟡 NESSUNA AZIONE AUTOMATICA.",
            flush=True,
        )

        print(
            "🟡 OFFERTA LASCIATA PENDING.",
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
            f"🔁 Retry tra circa "
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
            f"esclusa/e.",
            flush=True,
        )

    # ========================================================
    # COUNTER OFFER
    # ========================================================

    if counter_offer(
        offer,
        valid_cards,
    ):

        print(
            "🟢 Controproposta "
            "creata.",
            flush=True,
        )

        # ----------------------------------------------------
        # SOLO DOPO LA CREAZIONE RIUSCITA
        # ----------------------------------------------------
        #
        # rifiutiamo l'offerta originale.

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
            "lasciata PENDING.",
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
        "💱 PRICE CURRENCY: "
        "referenceCurrency autorevole",
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
        "💷 GBP: gbpCents → EUR",
        flush=True,
    )

    print(
        "💎 WEI: wei → ETH/EUR "
        "SOLO se referenceCurrency=WEI",
        flush=True,
    )

    print(
        "🪙 LAMPORT: NON supportato",
        flush=True,
    )

    print(
        f"💱 USD/EUR CACHE: "
        f"{USD_EUR_CACHE_SECONDS}s",
        flush=True,
    )

    print(
        f"💱 GBP/EUR CACHE: "
        f"{GBP_EUR_CACHE_SECONDS}s",
        flush=True,
    )

    print(
        f"💱 ETH/EUR CACHE: "
        f"{ETH_EUR_CACHE_SECONDS}s",
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
        f"🔁 RETRY: "
        f"{UNKNOWN_PRICE_RETRY}s",
        flush=True,
    )

    print(
        "🧪 DRY_RUN="
        + str(DRY_RUN),
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

    if not inspect_live_schema():

        print(
            "🛑 Schema incompatibile.",
            flush=True,
        )

        print(
            "🛑 Worker fermato per sicurezza.",
            flush=True,
        )

        return

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

        "create_mode":
            "LIVE_SCHEMA_AWARE",

        "settlement_currency":
            "EUR",

        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

        "price_authority":
            "referenceCurrency",

        "price_eur_mode":
            "EUR_DIRECT_OR_USD_OR_GBP_OR_EXPLICIT_WEI",

        "usd_conversion":
            True,

        "gbp_conversion":
            True,

        "wei_conversion":
            True,

        "wei_requires_reference_currency":
            "WEI",

        "lamport_conversion":
            False,

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
