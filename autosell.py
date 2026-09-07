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
# CONFIGURAZIONE
# ============================================================

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv(
    "DRY_RUN",
    "false"
).lower() == "true"

INTERVAL = int(
    os.getenv(
        "INTERVAL",
        "30"
    )
)

TIMEOUT = int(
    os.getenv(
        "TIMEOUT",
        "25"
    )
)

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LIVE_LISTINGS = 5

COVERAGE_CACHE = 3600
USD_CACHE = 300

BOT_VERSION = "AUTOSell-2.4-PREPARE-OFFER-FIX"

SELL_PRICE_MODE = os.getenv(
    "SELL_PRICE_MODE",
    "FLOOR"
).upper()

JSON_PATH = os.getenv(
    "AUTOSSELL_JSON_PATH",
    "autosell_cards.json"
).strip()


# ============================================================
# SETTLEMENT CURRENCIES
#
# FIX PRINCIPALE PREPARE OFFER:
#
# Sorare richiede settlementCurrencies nel prepareOffer
# e anche nel createSingleSaleOffer.
#
# Default: EUR
#
# Esempi:
#
# SETTLEMENT_CURRENCIES=EUR
#
# oppure:
#
# SETTLEMENT_CURRENCIES=EUR,WEI
# ============================================================

SETTLEMENT_CURRENCIES_RAW = os.getenv(
    "SETTLEMENT_CURRENCIES",
    "EUR"
).strip()


def parse_settlement_currencies():
    values = []

    for value in SETTLEMENT_CURRENCIES_RAW.split(","):
        value = value.strip().upper()

        if value and value not in values:
            values.append(value)

    return values


SETTLEMENT_CURRENCIES = parse_settlement_currencies()


# ============================================================
# KULENOVIC PROTETTO
# ============================================================

KID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# DECK SORARE
# ============================================================

OPERATIONS_DECK_NAME = "✅ OPERAZIONI BOT"

DECK_PAGE_SIZE = 100


# ============================================================
# LOCK / STATO
# ============================================================

json_lock = threading.Lock()
worker_lock = threading.Lock()
coverage_lock = threading.Lock()

worker_started = False

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0
coverage_available = False


# ============================================================
# UTILITY
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now_iso():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def card_name(c):
    return (
        c.get("name")
        or c.get("slug")
        or "Carta"
    )


def card_label(c):
    name = card_name(c)

    slug = c.get("slug")

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


def format_eur(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# JSON
# ============================================================

def ensure_json_file():
    folder = os.path.dirname(
        os.path.abspath(JSON_PATH)
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    if not os.path.exists(JSON_PATH):
        with open(
            JSON_PATH,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                [],
                f,
                ensure_ascii=False,
                indent=2
            )


def load_cards():
    ensure_json_file()

    try:
        with open(
            JSON_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data if isinstance(
            data,
            list
        ) else []

    except json.JSONDecodeError as e:
        print(
            f"❌ JSON non valido: {e}",
            flush=True
        )

    except Exception as e:
        print(
            f"❌ Lettura JSON: {e}",
            flush=True
        )

    return []


def save_cards(cards):
    folder = os.path.dirname(
        os.path.abspath(JSON_PATH)
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    temp = (
        f"{JSON_PATH}.tmp."
        f"{uuid.uuid4()}"
    )

    try:
        with open(
            temp,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                cards,
                f,
                ensure_ascii=False,
                indent=2
            )

            f.flush()
            os.fsync(
                f.fileno()
            )

        os.replace(
            temp,
            JSON_PATH
        )

        return True

    except Exception as e:
        print(
            f"❌ Scrittura JSON: {e}",
            flush=True
        )

        try:
            if os.path.exists(temp):
                os.remove(temp)
        except Exception:
            pass

        return False


def get_ready_cards():
    with json_lock:
        return [
            c
            for c in load_cards()
            if (
                isinstance(c, dict)
                and norm(
                    c.get("status")
                ) == "ready"
                and str(
                    c.get("asset_id")
                    or ""
                ).strip()
            )
        ]


def update_json_card(
    asset_id,
    status=None,
    sale_offer_id=None,
    last_error=None
):
    asset_id = str(
        asset_id or ""
    ).strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for c in cards:

            if not isinstance(
                c,
                dict
            ):
                continue

            current_asset = str(
                c.get("asset_id")
                or ""
            ).strip()

            if current_asset.lower() != asset_id.lower():
                continue

            if status is not None:
                c["status"] = status

            if sale_offer_id is not None:
                c["sale_offer_id"] = sale_offer_id

            if last_error is not None:
                c["last_error"] = last_error

            elif status in (
                "SELLING",
                "SOLD"
            ):
                c["last_error"] = None

            if status == "SELLING":
                c["selling_at"] = now_iso()

            elif status == "SOLD":
                c["sold_at"] = now_iso()

            return save_cards(cards)

        print(
            "⚠️ JSON: asset_id non trovato: "
            f"{asset_id}",
            flush=True
        )

        return False


def add_card_to_json(
    asset_id,
    source="AUTOBUY"
):
    asset_id = str(
        asset_id or ""
    ).strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for c in cards:

            if (
                isinstance(c, dict)
                and str(
                    c.get("asset_id")
                    or ""
                ).strip().lower()
                == asset_id.lower()
            ):
                return (
                    c.get("status")
                    != "SOLD"
                )

        cards.append({
            "asset_id": asset_id,
            "source": source,
            "status": "READY",
            "created_at": now_iso(),
            "sold_at": None,
            "sale_offer_id": None,
            "last_error": None
        })

        return save_cards(cards)


# ============================================================
# DECK OPERAZIONI BOT
# ============================================================

def get_operations_deck_cards():
    """
    Legge le carte contenute nel deck:

        ✅ OPERAZIONI BOT

    Non vende direttamente.

    Restituisce assetId.
    """

    asset_ids = []
    after = None

    while True:

        data = graphql(
            """
            query OperationsBotDeck(
                $deckName: String!
                $first: Int!
                $after: String
            ) {
                currentUser {
                    footballUserProfile {
                        deck(name: $deckName) {
                            id
                            name
                            slug
                            tokensCount

                            cards(
                                first: $first
                                after: $after
                            ) {
                                nodes {
                                    assetId
                                    slug
                                    name
                                    rarityTyped
                                    seasonYear
                                }

                                pageInfo {
                                    hasNextPage
                                    endCursor
                                }
                            }
                        }
                    }
                }
            }
            """,
            {
                "deckName": OPERATIONS_DECK_NAME,
                "first": DECK_PAGE_SIZE,
                "after": after
            }
        )

        if not data:
            print(
                "⚠️ Deck OPERAZIONI BOT: "
                "nessuna risposta",
                flush=True
            )

            return []

        if data.get("errors"):
            print(
                "⚠️ Deck OPERAZIONI BOT - GraphQL:",
                json.dumps(
                    data["errors"],
                    ensure_ascii=False
                )[:3000],
                flush=True
            )

            return []

        current_user = (
            (data.get("data") or {})
            .get("currentUser")
        )

        if not current_user:
            print(
                "⚠️ Deck OPERAZIONI BOT: "
                "currentUser non disponibile",
                flush=True
            )

            return []

        profile = current_user.get(
            "footballUserProfile"
        )

        if not profile:
            print(
                "⚠️ Deck OPERAZIONI BOT: "
                "profilo Football non disponibile",
                flush=True
            )

            return []

        deck = profile.get("deck")

        if not deck:
            print(
                f"⚠️ Deck non trovato: "
                f"{OPERATIONS_DECK_NAME}",
                flush=True
            )

            return []

        cards_data = (
            (deck.get("cards") or {})
            .get("nodes")
            or []
        )

        for card in cards_data:

            if not isinstance(
                card,
                dict
            ):
                continue

            asset_id = str(
                card.get("assetId")
                or ""
            ).strip()

            if asset_id:
                asset_ids.append(
                    asset_id
                )

        page_info = (
            (deck.get("cards") or {})
            .get("pageInfo")
            or {}
        )

        has_next = bool(
            page_info.get(
                "hasNextPage"
            )
        )

        next_cursor = page_info.get(
            "endCursor"
        )

        if not has_next:
            break

        if not next_cursor:
            print(
                "⚠️ Deck OPERAZIONI BOT: "
                "paginazione senza cursor",
                flush=True
            )

            break

        after = next_cursor

    asset_ids = list(
        dict.fromkeys(
            asset_ids
        )
    )

    print(
        f"📋 Deck "
        f"'{OPERATIONS_DECK_NAME}': "
        f"{len(asset_ids)} carte",
        flush=True
    )

    return asset_ids


def sync_operations_bot_deck():
    asset_ids = get_operations_deck_cards()

    if not asset_ids:
        return 0

    added = 0

    for asset_id in asset_ids:

        with json_lock:
            cards = load_cards()

            exists = False

            for c in cards:

                if (
                    isinstance(c, dict)
                    and str(
                        c.get("asset_id")
                        or ""
                    ).strip().lower()
                    == asset_id.lower()
                ):
                    exists = True
                    break

        if exists:
            continue

        if add_card_to_json(
            asset_id,
            source="OPERATIONS_DECK"
        ):
            added += 1

            print(
                "➕ OPERATIONS BOT → "
                f"JSON READY: {asset_id}",
                flush=True
            )

    if added:
        print(
            "📥 OPERATIONS BOT: "
            f"aggiunte {added} nuove carte al JSON",
            flush=True
        )

    return added


# ============================================================
# SORARE
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    return {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith(
                "bearer "
            )
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            f"Sorare-AutoSell/{BOT_VERSION}"
        ),
        **(
            {"JWT-AUD": AUD}
            if AUD
            else {}
        )
    }


def graphql(
    query,
    variables=None
):
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
                f"🌐 Sorare HTTP "
                f"{r.status_code}",
                flush=True
            )

            if r.status_code == 429:

                retry = r.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    retry = int(retry)
                except Exception:
                    retry = attempt + 2

                time.sleep(
                    min(
                        retry,
                        15
                    )
                )

                continue

            if r.status_code != 200:

                print(
                    f"❌ Sorare HTTP "
                    f"{r.status_code}: "
                    f"{r.text[:2000]}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            try:
                data = r.json()

            except Exception as e:

                print(
                    f"❌ JSON Sorare non valido: "
                    f"{e}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                        indent=2
                    )[:5000],
                    flush=True
                )

            return data

        except Exception as e:

            print(
                f"❌ GraphQL: {e}",
                flush=True
            )

            time.sleep(
                attempt + 1
            )

    return None


# ============================================================
# COVERAGE
# ============================================================

def extract_coverage_slugs(text):
    if not text:
        return set()

    text = text.replace(
        "\\/",
        "/"
    )

    text = text.replace(
        "\\u002F",
        "/"
    )

    text = text.replace(
        "\\u002f",
        "/"
    )

    result = set()

    for pattern in (
        r"/football/leagues/([a-zA-Z0-9_-]+)",
        r"football/leagues/([a-zA-Z0-9_-]+)"
    ):

        result.update(
            norm(x)
            for x in re.findall(
                pattern,
                text,
                re.I
            )
            if norm(x)
        )

    return result


def load_coverage(
    force=False
):
    global coverage_cache
    global coverage_time
    global coverage_available

    now = time.time()

    with coverage_lock:
        cached = set(
            coverage_cache
        )

        cached_time = coverage_time

        cached_available = (
            coverage_available
        )

    if (
        not force
        and cached
        and cached_available
        and now - cached_time
        < COVERAGE_CACHE
    ):
        return cached

    try:

        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent": (
                    f"Sorare-Bot/"
                    f"{BOT_VERSION}"
                )
            }
        )

        if r.status_code != 200:

            with coverage_lock:
                coverage_available = False

            return cached

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            r.text,
            re.I
        )

        result = {
            norm(x)
            for x in matches
            if norm(x)
        }

        if not result:

            with coverage_lock:
                coverage_available = False

            return cached

        with coverage_lock:

            coverage_cache = result
            coverage_time = time.time()
            coverage_available = True

        print(
            "🌐 Sorare Coverage aggiornata: "
            f"{len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:

        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )

        with coverage_lock:
            coverage_available = False

        return cached


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql(
        """
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
        """
    )

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
        "✅ Sorare: "
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

    data = graphql(
        """
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
        """,
        {
            "assetIds": ids
        }
    )

    if not data:
        return []

    if data.get("errors"):
        return []

    return (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )


# ============================================================
# USD / EUR
# ============================================================

def usd_eur():

    global usd_rate
    global usd_time

    now = time.time()

    if (
        usd_rate
        and now - usd_time
        < USD_CACHE
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
        usd_time = now

        return rate

    except Exception as e:

        print(
            f"❌ USD/EUR: {e}",
            flush=True
        )

        return None


def price_eur(amounts):

    if not isinstance(
        amounts,
        dict
    ):
        return None

    try:

        eur = int(
            amounts.get(
                "eurCents"
            )
        )

        if eur > 0:
            return eur

    except (
        TypeError,
        ValueError
    ):
        pass

    try:

        usd = float(
            amounts.get(
                "usdCents"
            )
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
                round(
                    usd * rate
                )
            )

    return None


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = norm(
        player.get("slug")
    )

    rarity = norm(
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
        return None

    if not player_slug or not rarity:
        return None

    data = graphql(
        """
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
        """,
        {
            "playerSlug": player_slug,
            "first": 50
        }
    )

    if not data:
        return None

    if data.get("errors"):
        return None

    offers = (
        (
            (
                (data.get("data") or {})
                .get("tokens")
                or {}
            )
            .get("liveSingleSaleOffers")
            or {}
        )
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for c in cards:

            try:

                season_ok = (
                    int(
                        c.get(
                            "seasonYear"
                        )
                    )
                    == season
                )

            except (
                TypeError,
                ValueError
            ):
                continue

            player_ok = (
                norm(
                    (
                        c.get(
                            "anyPlayer"
                        )
                        or {}
                    ).get("slug")
                )
                == player_slug
            )

            rarity_ok = (
                norm(
                    c.get(
                        "rarityTyped"
                    )
                )
                == rarity
            )

            if (
                player_ok
                and rarity_ok
                and season_ok
            ):

                price = price_eur(
                    (
                        offer.get(
                            "receiverSide"
                        )
                        or {}
                    ).get(
                        "amounts"
                    )
                    or {}
                )

                if price is not None:
                    prices.append(price)

                break

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"⚠️ Floor {player_slug}: "
            f"{len(prices)}/"
            f"{MIN_LIVE_LISTINGS} listing",
            flush=True
        )

        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    if KID:
        wanted.add(
            norm(KID)
        )

    return (
        norm(
            card.get(
                "assetId"
            )
        ) in wanted
        or
        norm(
            card.get(
                "slug"
            )
        ) in wanted
    )


def coverage_info(card):

    club = (
        (
            card.get(
                "anyPlayer"
            )
            or {}
        )
        .get(
            "activeClub"
        )
        or {}
    )

    active = [
        norm(
            c.get("slug")
        )
        for c in (
            club.get(
                "activeCompetitions"
            )
            or []
        )
        if (
            isinstance(c, dict)
            and c.get("slug")
        )
    ]

    coverage = load_coverage()

    with coverage_lock:
        available = (
            coverage_available
        )

    if (
        not available
        or not coverage
    ):
        return (
            None,
            active,
            [],
            "COVERAGE_UNAVAILABLE"
        )

    covered = [
        x
        for x in active
        if x in coverage
    ]

    return (
        bool(covered),
        active,
        covered,
        None
    )


def validate_for_autosell(card):

    if is_kulenovic(card):

        return False, {
            "code": "KULENOVIC",
            "message": (
                "Kulenovic è protetto "
                "e non deve essere venduto"
            )
        }

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        return False, {
            "code": "RARITY",
            "rarity": (
                rarity
                or "N/D"
            )
        }

    floor = live_floor(card)

    if floor is None:

        return False, {
            "code": "PRICE_UNKNOWN",
            "min_live_listings":
                MIN_LIVE_LISTINGS
        }

    if floor < MIN_PRICE:

        return False, {
            "code": "PRICE_LOW",
            "floor": floor,
            "min_price": MIN_PRICE
        }

    if floor > MAX_PRICE:

        return False, {
            "code": "PRICE_HIGH",
            "floor": floor,
            "max_price": MAX_PRICE
        }

    (
        covered,
        active,
        covered_competitions,
        error
    ) = coverage_info(card)

    if error:

        return False, {
            "code":
                "COVERAGE_UNAVAILABLE",
            "active_competitions":
                active,
            "covered_competitions":
                []
        }

    if not covered:

        return False, {
            "code": "COVERAGE",
            "active_competitions":
                active,
            "covered_competitions":
                covered_competitions
        }

    return True, {
        "floor": floor,
        "rarity": rarity,
        "active_competitions":
            active,
        "covered_competitions":
            covered_competitions
    }


def print_rejection(
    card,
    info
):

    print(
        "🚫 AutoSell - ESCLUSA: "
        f"{card_label(card)}",
        flush=True
    )

    code = (
        info or {}
    ).get(
        "code"
    )

    if code == "KULENOVIC":

        print(
            "   └─ Motivo: "
            "KULENOVIC PROTETTO",
            flush=True
        )

    elif code == "RARITY":

        print(
            "   ├─ Motivo: "
            "RARITÀ NON VALIDA",
            flush=True
        )

        print(
            f"   └─ Rarità: "
            f"{info.get('rarity')}",
            flush=True
        )

    elif code == "PRICE_UNKNOWN":

        print(
            "   ├─ Motivo: "
            "FLOOR LIVE NON DISPONIBILE",
            flush=True
        )

        print(
            "   └─ Listing richiesti: "
            f"{info.get('min_live_listings')}",
            flush=True
        )

    elif code == "PRICE_LOW":

        print(
            "   ├─ Motivo: "
            "FLOOR TROPPO BASSO",
            flush=True
        )

        print(
            "   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            "   └─ Minimo: "
            f"{format_eur(info.get('min_price'))}",
            flush=True
        )

    elif code == "PRICE_HIGH":

        print(
            "   ├─ Motivo: "
            "FLOOR TROPPO ALTO",
            flush=True
        )

        print(
            "   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            "   └─ Massimo: "
            f"{format_eur(info.get('max_price'))}",
            flush=True
        )

    elif code == "COVERAGE_UNAVAILABLE":

        print(
            "   ├─ Motivo: "
            "COVERAGE NON DISPONIBILE",
            flush=True
        )

        print(
            "   └─ Nessuna vendita; "
            "il bot riproverà.",
            flush=True
        )

    elif code == "COVERAGE":

        active = (
            info.get(
                "active_competitions"
            )
            or []
        )

        covered = (
            info.get(
                "covered_competitions"
            )
            or []
        )

        print(
            "   ├─ Motivo: "
            "COMPETIZIONE NON COPERTA",
            flush=True
        )

        print(
            "   ├─ Attive: "
            + (
                ", ".join(active)
                if active
                else "nessuna"
            ),
            flush=True
        )

        print(
            "   └─ Coperte: "
            + (
                ", ".join(covered)
                if covered
                else "nessuna"
            ),
            flush=True
        )

    else:

        print(
            "   └─ Motivo: "
            "verifica fallita",
            flush=True
        )


# ============================================================
# NODE SIGNING
#
# Allineato al buildApproval ufficiale Sorare:
#
# fingerprint
# +
# authorizationRequest
#
# Nessuna modifica manuale dell'amount.
# ============================================================

def sign_authorizations(
    authorizations
):

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

    if not isinstance(
        authorizations,
        list
    ):
        raise RuntimeError(
            "Authorizations non valide"
        )

    script = r'''
const fs = require("fs");
const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function buildApproval(
    privateKey,
    authorization
) {
    if (!authorization) {
        throw new Error(
            "Authorization mancante"
        );
    }

    const fingerprint =
        authorization.fingerprint;

    const request =
        authorization.request;

    if (!fingerprint) {
        throw new Error(
            "Authorization fingerprint mancante"
        );
    }

    if (!request) {
        throw new Error(
            "Authorization request mancante"
        );
    }

    const typename =
        request.__typename;

    if (!typename) {
        throw new Error(
            "Authorization __typename mancante"
        );
    }

    /*
     * IMPORTANTE:
     *
     * Passiamo alla libreria Sorare
     * ESATTAMENTE la request ricevuta
     * da prepareOffer.
     *
     * Non modifichiamo amount.
     */
    const signature =
        signAuthorizationRequest(
            privateKey,
            request
        );

    if (
        typename ===
        "StarkexTransferAuthorizationRequest"
    ) {
        return {
            fingerprint,
            starkexTransferApproval: {
                nonce: request.nonce,
                expirationTimestamp:
                    request.expirationTimestamp,
                signature
            }
        };
    }

    if (
        typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {
        return {
            fingerprint,
            starkexLimitOrderApproval: {
                nonce: request.nonce,
                expirationTimestamp:
                    request.expirationTimestamp,
                signature
            }
        };
    }

    if (
        typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {
        return {
            fingerprint,
            mangopayWalletTransferApproval: {
                nonce: request.nonce,
                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + typename
    );
}

const approvals =
    input.authorizations.map(
        a => buildApproval(
            input.privateKey,
            a
        )
    );

process.stdout.write(
    JSON.stringify(approvals)
);
'''

    process = subprocess.run(
        [
            node,
            "-e",
            script
        ],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations":
                authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
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

    except Exception as e:

        raise RuntimeError(
            "Output firma Node non valido: "
            f"{e}; "
            f"stdout={process.stdout[:2000]}"
        )


# ============================================================
# CREATE SALE
#
# QUI È LA CORREZIONE PRINCIPALE.
#
# prepareOffer:
#
# settlementCurrencies
#
# createSingleSaleOffer:
#
# settlementCurrencies
#
# come nell'esempio ufficiale Sorare.
# ============================================================

def create_sale(
    card,
    price_cents
):

    asset_id = str(
        card.get(
            "assetId"
        )
        or ""
    ).strip()

    if not asset_id:

        print(
            "❌ AutoSell: "
            "assetId mancante",
            flush=True
        )

        return None

    if not SETTLEMENT_CURRENCIES:

        print(
            "❌ AutoSell: "
            "SETTLEMENT_CURRENCIES vuoto",
            flush=True
        )

        return None

    print(
        "💳 Settlement currencies: "
        + ", ".join(
            SETTLEMENT_CURRENCIES
        ),
        flush=True
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "vendita simulata",
            flush=True
        )

        print(
            f"   ├─ Carta: "
            f"{card_label(card)}",
            flush=True
        )

        print(
            f"   ├─ Asset: "
            f"{asset_id}",
            flush=True
        )

        print(
            f"   ├─ Prezzo: "
            f"{format_eur(price_cents)}",
            flush=True
        )

        print(
            "   └─ Settlement: "
            + ", ".join(
                SETTLEMENT_CURRENCIES
            ),
            flush=True
        )

        return "DRY-RUN"

    # ========================================================
    # PREPARE OFFER
    # ========================================================

    prepare_input = {
        "type": "SINGLE_SALE_OFFER",

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        # ====================================================
        # FIX:
        # PRESENTE ANCHE NEL PREPARE OFFER
        # ====================================================

        "settlementCurrencies":
            SETTLEMENT_CURRENCIES,

        "receiveAmount": {
            "amount": str(
                price_cents
            ),
            "currency": "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
    }

    print(
        "🛠️ prepareOffer...",
        flush=True
    )

    print(
        "   ├─ type: "
        "SINGLE_SALE_OFFER",
        flush=True
    )

    print(
        "   ├─ assetId: "
        f"{asset_id}",
        flush=True
    )

    print(
        "   ├─ settlementCurrencies: "
        + ", ".join(
            SETTLEMENT_CURRENCIES
        ),
        flush=True
    )

    print(
        "   └─ receiveAmount: "
        f"€{price_cents / 100:.2f}",
        flush=True
    )

    data = graphql(
        """
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
        """,
        {
            "input": prepare_input
        }
    )

    if not data:

        print(
            "❌ AutoSell: "
            "prepareOffer nessuna risposta",
            flush=True
        )

        return None

    result = (
        ((data.get("data") or {})
        .get("prepareOffer"))
    )

    if not result:

        print(
            "❌ AutoSell: "
            "prepareOffer senza risultato",
            flush=True
        )

        if data.get("errors"):

            print(
                "❌ GraphQL:",
                json.dumps(
                    data["errors"],
                    ensure_ascii=False,
                    indent=2
                ),
                flush=True
            )

        return None

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        print(
            "❌ AutoSell "
            "prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False,
                indent=2
            ),
            flush=True
        )

        return None

    authorizations = (
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        print(
            "❌ AutoSell: "
            "prepareOffer ha restituito "
            "zero authorization",
            flush=True
        )

        return None

    print(
        "🔐 Authorization ricevute: "
        f"{len(authorizations)}",
        flush=True
    )

    # ========================================================
    # DEBUG SICURO DELLE AUTHORIZATION
    #
    # NON stampa la private key.
    # ========================================================

    for index, authorization in enumerate(
        authorizations,
        start=1
    ):

        request = (
            authorization.get(
                "request"
            )
            or {}
        )

        print(
            f"   ├─ Authorization #{index}",
            flush=True
        )

        print(
            "   │  ├─ fingerprint: "
            + str(
                authorization.get(
                    "fingerprint"
                )
            ),
            flush=True
        )

        print(
            "   │  └─ type: "
            + str(
                request.get(
                    "__typename"
                )
            ),
            flush=True
        )

    # ========================================================
    # FIRMA
    # ========================================================

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:

        print(
            f"❌ AutoSell firma: {e}",
            flush=True
        )

        return None

    if not approvals:

        print(
            "❌ AutoSell: "
            "nessun approval generato",
            flush=True
        )

        return None

    print(
        "✅ Authorization firmate",
        flush=True
    )

    # ========================================================
    # CREATE SINGLE SALE OFFER
    # ========================================================

    create_input = {
        "approvals": approvals,

        "dealId": str(
            uuid.uuid4()
        ),

        "assetId": asset_id,

        # ====================================================
        # FIX:
        # PRESENTE ANCHE QUI
        # ====================================================

        "settlementCurrencies":
            SETTLEMENT_CURRENCIES,

        "receiveAmount": {
            "amount": str(
                price_cents
            ),
            "currency": "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
    }

    print(
        "📤 createSingleSaleOffer...",
        flush=True
    )

    data = graphql(
        """
        mutation CreateSingleSaleOffer(
            $input: createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(
                input: $input
            ) {
                tokenOffer {
                    id
                    status
                    startDate
                    endDate
                }

                errors {
                    message
                }
            }
        }
        """,
        {
            "input": create_input
        }
    )

    if not data:

        print(
            "❌ AutoSell: "
            "createSingleSaleOffer "
            "nessuna risposta",
            flush=True
        )

        return None

    result = (
        ((data.get("data") or {})
        .get(
            "createSingleSaleOffer"
        ))
    )

    if not result:

        print(
            "❌ AutoSell: "
            "createSingleSaleOffer "
            "senza risultato",
            flush=True
        )

        if data.get("errors"):

            print(
                "❌ GraphQL:",
                json.dumps(
                    data["errors"],
                    ensure_ascii=False,
                    indent=2
                ),
                flush=True
            )

        return None

    errors = (
        result.get(
            "errors"
        )
        or []
    )

    if errors:

        print(
            "❌ AutoSell "
            "createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False,
                indent=2
            ),
            flush=True
        )

        return None

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
            "❌ AutoSell: "
            "tokenOffer ID mancante",
            flush=True
        )

        return None

    print(
        "✅ AUTOSELL CREATO: "
        f"{offer_id}",
        flush=True
    )

    print(
        "   ├─ Status: "
        f"{token_offer.get('status')}",
        flush=True
    )

    print(
        "   ├─ Prezzo: "
        f"{format_eur(price_cents)}",
        flush=True
    )

    print(
        "   └─ Settlement: "
        + ", ".join(
            SETTLEMENT_CURRENCIES
        ),
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):

    asset_id = str(
        row.get(
            "asset_id"
        )
        or ""
    ).strip()

    source = (
        row.get(
            "source"
        )
        or "UNKNOWN"
    )

    if not asset_id:
        return

    print(
        "\n💰 AUTOSELL CHECK",
        flush=True
    )

    print(
        f"   ├─ Asset: "
        f"{asset_id}",
        flush=True
    )

    print(
        f"   ├─ Provenienza: "
        f"{source}",
        flush=True
    )

    print(
        "   └─ Età: "
        "NON UTILIZZATA",
        flush=True
    )

    cards = card_details(
        [asset_id]
    )

    if len(cards) != 1:

        print(
            "❌ AutoSell: "
            "impossibile recuperare "
            "la carta → NON VENDERE",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=(
                "CARD_DETAILS_UNAVAILABLE"
            )
        )

        return

    card = cards[0]

    if norm(
        card.get(
            "assetId"
        )
    ) != norm(asset_id):

        print(
            "❌ AutoSell: "
            "assetId non corrispondente "
            "→ BLOCCATO",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=(
                "ASSET_ID_MISMATCH"
            )
        )

        return

    valid, info = (
        validate_for_autosell(
            card
        )
    )

    if not valid:

        print_rejection(
            card,
            info
        )

        code = (
            info or {}
        ).get(
            "code",
            "INVALID"
        )

        update_json_card(
            asset_id,
            status=(
                "READY"
                if code
                == "COVERAGE_UNAVAILABLE"
                else "BLOCKED"
            ),
            last_error=code
        )

        return

    floor = info.get(
        "floor"
    )

    print(
        "✅ AutoSell - "
        "Carta valida: "
        f"{card_label(card)}",
        flush=True
    )

    print(
        "   ├─ Rarità: "
        f"{info.get('rarity')}",
        flush=True
    )

    print(
        "   ├─ Floor: "
        f"{format_eur(floor)}",
        flush=True
    )

    print(
        "   └─ Competizioni coperte: "
        + ", ".join(
            info.get(
                "covered_competitions"
            )
            or []
        ),
        flush=True
    )

    if SELL_PRICE_MODE != "FLOOR":

        print(
            "❌ SELL_PRICE_MODE "
            "non supportato: "
            f"{SELL_PRICE_MODE}",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=(
                "INVALID_SELL_PRICE_MODE"
            )
        )

        return

    sell_price = floor

    if (
        sell_price is None
        or not (
            MIN_PRICE
            <= sell_price
            <= MAX_PRICE
        )
    ):

        print(
            "🛑 AutoSell: "
            "prezzo finale fuori "
            "dal range → BLOCCATO",
            flush=True
        )

        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error=(
                "FINAL_PRICE_OUT_OF_RANGE"
            )
        )

        return

    # ========================================================
    # BLOCCO PRIMA DELLA VENDITA
    # ========================================================

    if not update_json_card(
        asset_id,
        status="SELLING",
        last_error=None
    ):

        print(
            "❌ AutoSell: "
            "impossibile aggiornare "
            "il JSON → NON VENDERE",
            flush=True
        )

        return

    offer_id = create_sale(
        card,
        sell_price
    )

    if not offer_id:

        update_json_card(
            asset_id,
            status="READY",
            last_error=(
                "CREATE_SALE_FAILED"
            )
        )

        return

    if not update_json_card(
        asset_id,
        status="SOLD",
        sale_offer_id=offer_id,
        last_error=None
    ):

        print(
            "⚠️ ATTENZIONE: "
            "vendita creata ma "
            "JSON non aggiornato",
            flush=True
        )

        return

    print(
        "🎉 AUTOSELL COMPLETATO",
        flush=True
    )

    print(
        "   ├─ Carta: "
        f"{card_label(card)}",
        flush=True
    )

    print(
        "   ├─ Prezzo: "
        f"{format_eur(sell_price)}",
        flush=True
    )

    print(
        "   └─ Offer ID: "
        f"{offer_id}",
        flush=True
    )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 AUTOSELL AVVIATO",
        flush=True
    )

    print(
        f"📦 VERSIONE: "
        f"{BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        "💰 RANGE: "
        f"{format_eur(MIN_PRICE)} - "
        f"{format_eur(MAX_PRICE)}",
        flush=True
    )

    print(
        "📊 LISTING MINIME: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        "🎂 ETÀ: NON UTILIZZATA",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI VENDUTO",
        flush=True
    )

    print(
        "🛡️ SOURCE: "
        "AUTOBUY / SWAP",
        flush=True
    )

    print(
        "📋 DECK AGGIUNTIVO: "
        f"{OPERATIONS_DECK_NAME}",
        flush=True
    )

    print(
        "💳 SETTLEMENT: "
        + ", ".join(
            SETTLEMENT_CURRENCIES
        ),
        flush=True
    )

    print(
        f"💾 JSON: {JSON_PATH}",
        flush=True
    )

    try:

        headers()
        ensure_json_file()

    except Exception as e:

        print(
            f"❌ Configurazione: {e}",
            flush=True
        )

        return

    # ========================================================
    # CONTROLLO SETTLEMENT
    # ========================================================

    if not SETTLEMENT_CURRENCIES:

        print(
            "❌ SETTLEMENT_CURRENCIES "
            "non configurato.",
            flush=True
        )

        return

    # ========================================================
    # COVERAGE
    # ========================================================

    coverage = load_coverage(
        force=True
    )

    if coverage:

        print(
            "🏆 COMPETIZIONI FOOTBALL: "
            f"{len(coverage)}",
            flush=True
        )

    else:

        print(
            "⚠️ Coverage non disponibile.",
            flush=True
        )

        print(
            "🛡️ AutoSell resta attivo ma "
            "NON venderà carte finché "
            "la coverage non sarà verificata.",
            flush=True
        )

    # ========================================================
    # ACCOUNT
    # ========================================================

    if not check_account():
        return

    # ========================================================
    # PRIMA SYNC DECK
    # ========================================================

    try:

        sync_operations_bot_deck()

    except Exception as e:

        print(
            "⚠️ Sync deck "
            "OPERAZIONI BOT: "
            f"{e}",
            flush=True
        )

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            load_coverage()

            # ------------------------------------------------
            # SYNC DECK
            # ------------------------------------------------

            try:

                sync_operations_bot_deck()

            except Exception as e:

                print(
                    "⚠️ Sync deck "
                    "OPERAZIONI BOT: "
                    f"{e}",
                    flush=True
                )

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            rows = get_ready_cards()

            print(
                "🗄️ Carte READY nel JSON: "
                f"{len(rows)}",
                flush=True
            )

            for row in rows:

                try:

                    process_card(
                        row
                    )

                except Exception as e:

                    asset_id = str(
                        row.get(
                            "asset_id"
                        )
                        or ""
                    )

                    print(
                        "❌ AutoSell errore "
                        f"{asset_id}: {e}",
                        flush=True
                    )

                    if asset_id:

                        update_json_card(
                            asset_id,
                            status="ERROR",
                            last_error=str(e)
                        )

            time.sleep(
                INTERVAL
            )

        except Exception as e:

            print(
                f"❌ AutoSell worker: {e}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# FLASK
# ============================================================

def start_worker():

    global worker_started

    with worker_lock:

        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            name="autosell-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


@app.get("/")
def home():

    with coverage_lock:

        covered = set(
            coverage_cache
        )

        coverage_ok = (
            coverage_available
        )

    return jsonify({

        "status": "online",

        "bot": "autosell",

        "version":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "age_parameter":
            "NOT_USED",

        "rarity":
            "LIMITED",

        "coverage":
            "REQUIRED",

        "coverage_available":
            coverage_ok,

        "kulenovic":
            "NEVER_SELL",

        "source":
            "AUTOBUY_OR_SWAP_ONLY",

        "additional_source":
            "SORARE_DECK",

        "operations_deck":
            OPERATIONS_DECK_NAME,

        "storage":
            "PERSISTENT_JSON",

        "json_path":
            JSON_PATH,

        "ready_cards":
            len(
                get_ready_cards()
            ),

        "sell_price_mode":
            SELL_PRICE_MODE,

        "settlement_currencies":
            SETTLEMENT_CURRENCIES,

        "covered_competitions_count":
            len(covered),

        "covered_competitions":
            sorted(covered),

        "worker_started":
            worker_started
    })


@app.get("/health")
def health():

    with coverage_lock:

        loaded = bool(
            coverage_cache
        )

        coverage_ok = (
            coverage_available
        )

    return jsonify({

        "status": "ok",

        "bot": "autosell",

        "version":
            BOT_VERSION,

        "worker_started":
            worker_started,

        "coverage_loaded":
            loaded,

        "coverage_available":
            coverage_ok,

        "dry_run":
            DRY_RUN,

        "settlement_currencies":
            SETTLEMENT_CURRENCIES
    })


@app.get("/cards")
def cards_endpoint():

    with json_lock:

        cards = load_cards()

    return jsonify({

        "count":
            len(cards),

        "cards":
            cards
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
                "10000"
            )
        )
    )
