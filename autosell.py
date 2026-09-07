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

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

# ============================================================
# AUTOSELL RULES
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

MIN_LIVE_LISTINGS = 5

RARITY_REQUIRED = "LIMITED"

SELL_PRICE_MODE = os.getenv(
    "SELL_PRICE_MODE",
    "FLOOR"
).upper()

# ============================================================
# COVERAGE
# ============================================================

COVERAGE_CACHE = 3600

coverage_cache = set()
coverage_time = 0
coverage_available = False

coverage_lock = threading.Lock()

# ============================================================
# USD / EUR
# ============================================================

USD_CACHE = 300

usd_rate = None
usd_time = 0

# ============================================================
# JSON
# ============================================================

JSON_PATH = os.getenv(
    "AUTOSSELL_JSON_PATH",
    "autosell_cards.json"
).strip()

json_lock = threading.Lock()

# ============================================================
# BOT
# ============================================================

BOT_VERSION = "AUTOSell-2.4-COVERAGE-DECK-FIX"

worker_lock = threading.Lock()
worker_started = False

# ============================================================
# KULENOVIC PROTECTED
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
# OPERATIONS DECK
# ============================================================

OPERATIONS_DECK_NAME = "✅ OPERAZIONI BOT"

OPERATIONS_DECK_CACHE = 300

operations_deck_cache = set()
operations_deck_time = 0

operations_deck_lock = threading.Lock()


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
    return c.get("name") or c.get("slug") or "Carta"


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

    os.makedirs(folder, exist_ok=True)

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

        if isinstance(data, list):
            return data

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

    os.makedirs(folder, exist_ok=True)

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
            os.fsync(f.fileno())

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
                and norm(c.get("status")) == "ready"
                and str(
                    c.get("asset_id") or ""
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

            if not isinstance(c, dict):
                continue

            current = str(
                c.get("asset_id") or ""
            ).strip()

            if current.lower() != asset_id.lower():
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
            f"⚠️ JSON: asset_id non trovato: {asset_id}",
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
                    c.get("asset_id") or ""
                ).strip().lower()
                == asset_id.lower()
            ):

                return c.get("status") != "SOLD"

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
# SORARE GRAPHQL
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    return {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
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
                f"🌐 Sorare HTTP {r.status_code}",
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
                    min(retry, 15)
                )

                continue

            if r.status_code != 200:

                print(
                    f"❌ Sorare HTTP "
                    f"{r.status_code}: "
                    f"{r.text[:1000]}",
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
                    f"❌ JSON Sorare non valido: {e}",
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
                        ensure_ascii=False
                    )[:4000],
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

    matches = re.findall(
        r'/football/leagues/([^"\'?#<>\s]+)',
        text,
        re.I
    )

    return {
        norm(x)
        for x in matches
        if norm(x)
    }


def load_coverage(force=False):

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
                "User-Agent":
                    f"Sorare-Bot/{BOT_VERSION}"
            }
        )

        print(
            f"🌐 Coverage HTTP {r.status_code}",
            flush=True
        )

        if r.status_code != 200:

            with coverage_lock:
                coverage_available = False

            return cached

        result = extract_coverage_slugs(
            r.text
        )

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
# OPERATIONS DECK
# ============================================================

def load_operations_deck(force=False):

    global operations_deck_cache
    global operations_deck_time

    now = time.time()

    with operations_deck_lock:

        cached = set(
            operations_deck_cache
        )

        cached_time = (
            operations_deck_time
        )

    if (
        not force
        and cached
        and now - cached_time
        < OPERATIONS_DECK_CACHE
    ):
        return cached

    query = """
        query OperationsDeck($slug: String!) {
            deck(slug: $slug) {
                name
                slug
                tokensCount
                cards(first: 100) {
                    nodes {
                        assetId
                        slug
                    }
                }
            }
        }
    """

    data = graphql(
        query,
        {
            "slug":
                OPERATIONS_DECK_NAME
        }
    )

    deck = (
        ((data or {}).get("data") or {})
        .get("deck")
    )

    if not deck:

        print(
            "⚠️ Operations Deck non disponibile",
            flush=True
        )

        return cached

    nodes = (
        ((deck.get("cards") or {})
        .get("nodes"))
        or []
    )

    result = {
        str(
            c.get("assetId") or ""
        ).strip().lower()

        for c in nodes

        if (
            isinstance(c, dict)
            and str(
                c.get("assetId") or ""
            ).strip()
        )
    }

    if not result:

        print(
            f"⚠️ Deck '{OPERATIONS_DECK_NAME}' "
            "senza carte",
            flush=True
        )

        return cached

    with operations_deck_lock:

        operations_deck_cache = result

        operations_deck_time = time.time()

    print(
        f"📋 Deck '{OPERATIONS_DECK_NAME}': "
        f"{len(result)} carte",
        flush=True
    )

    return set(result)


def sync_operations_deck():

    deck_assets = load_operations_deck()

    if not deck_assets:
        return 0

    added = 0

    with json_lock:

        cards = load_cards()

        existing = {
            str(
                c.get("asset_id") or ""
            ).strip().lower()

            for c in cards

            if isinstance(c, dict)
        }

        for asset_id in sorted(
            deck_assets
        ):

            if asset_id in existing:
                continue

            cards.append({
                "asset_id":
                    asset_id,

                "source":
                    "OPERATIONS_DECK",

                "status":
                    "READY",

                "created_at":
                    now_iso(),

                "sold_at":
                    None,

                "sale_offer_id":
                    None,

                "last_error":
                    None
            })

            existing.add(asset_id)

            added += 1

            print(
                "➕ OPERATIONS BOT → "
                f"JSON READY: {asset_id}",
                flush=True
            )

        if added:
            save_cards(cards)

    if added:

        print(
            "📥 OPERATIONS BOT: aggiunte "
            f"{added} nuove carte al JSON",
            flush=True
        )

    return added


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
        and now - usd_time < USD_CACHE
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

    if not isinstance(amounts, dict):
        return None

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
        "playerSlug":
            player_slug,

        "first":
            50
    })

    if not data:
        return None

    if data.get("errors"):
        return None

    offers = (
        ((((data.get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes"))
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
                        c.get("seasonYear")
                    )
                    == season
                )

            except (
                TypeError,
                ValueError
            ):
                continue

            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                )
                == player_slug

                and norm(
                    c.get("rarityTyped")
                )
                == rarity

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
# VALIDATION
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
        norm(card.get("assetId"))
        in wanted

        or

        norm(card.get("slug"))
        in wanted
    )


def coverage_info(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active = [
        norm(c.get("slug"))

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
        available = coverage_available

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
            "code":
                "KULENOVIC",

            "message":
                "Kulenovic è protetto "
                "e non deve essere venduto"
        }

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != RARITY_REQUIRED:

        return False, {
            "code":
                "RARITY",

            "rarity":
                rarity or "N/D"
        }

    floor = live_floor(card)

    if floor is None:

        return False, {
            "code":
                "PRICE_UNKNOWN",

            "min_live_listings":
                MIN_LIVE_LISTINGS
        }

    if floor < MIN_PRICE:

        return False, {
            "code":
                "PRICE_LOW",

            "floor":
                floor,

            "min_price":
                MIN_PRICE
        }

    if floor > MAX_PRICE:

        return False, {
            "code":
                "PRICE_HIGH",

            "floor":
                floor,

            "max_price":
                MAX_PRICE
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
            "code":
                "COVERAGE",

            "active_competitions":
                active,

            "covered_competitions":
                covered_competitions
        }

    return True, {
        "floor":
            floor,

        "rarity":
            rarity,

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
    ).get("code")

    if code == "KULENOVIC":

        print(
            "   └─ Motivo: KULENOVIC PROTETTO",
            flush=True
        )

    elif code == "RARITY":

        print(
            "   ├─ Motivo: RARITÀ NON VALIDA",
            flush=True
        )

        print(
            f"   └─ Rarità: "
            f"{info.get('rarity')}",
            flush=True
        )

    elif code == "PRICE_UNKNOWN":

        print(
            "   ├─ Motivo: FLOOR LIVE "
            "NON DISPONIBILE",
            flush=True
        )

        print(
            "   └─ Listing richiesti: "
            f"{info.get('min_live_listings')}",
            flush=True
        )

    elif code == "PRICE_LOW":

        print(
            "   ├─ Motivo: FLOOR TROPPO BASSO",
            flush=True
        )

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Minimo: "
            f"{format_eur(info.get('min_price'))}",
            flush=True
        )

    elif code == "PRICE_HIGH":

        print(
            "   ├─ Motivo: FLOOR TROPPO ALTO",
            flush=True
        )

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Massimo: "
            f"{format_eur(info.get('max_price'))}",
            flush=True
        )

    elif code == "COVERAGE_UNAVAILABLE":

        print(
            "   ├─ Motivo: COVERAGE "
            "NON DISPONIBILE",
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
            "   ├─ Motivo: COMPETIZIONE "
            "NON COPERTA",
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
            "   └─ Motivo: verifica fallita",
            flush=True
        )


# ============================================================
# AUTHORIZATION SIGNING
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

    script = r'''
const crypto = require("crypto");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    require("fs").readFileSync(
        0,
        "utf8"
    )
);


async function signSolanaRequest(request) {

    let kit;

    try {

        kit = require("@solana/kit");

    } catch (e) {

        throw new Error(
            "@solana/kit non installato"
        );
    }

    let HDKey;

    try {

        HDKey =
            require(
                "micro-key-producer/slip10.js"
            ).HDKey;

    } catch (e) {

        throw new Error(
            "micro-key-producer non installato"
        );
    }

    const {
        createKeyPairFromPrivateKeyBytes,
        createSignerFromKeyPair,
        createSignableMessage,
        getBase58Decoder
    } = kit;


    const seed = Buffer.from(
        input.privateKey
            .replace(/^0x/, ""),
        "hex"
    );


    const derived =
        HDKey
            .fromMasterSeed(seed)
            .derive(
                "m/44'/501'/0'/0'"
            );


    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            derived.privateKey
        );


    const signer =
        createSignerFromKeyPair(
            keyPair
        );


    const {
        leafIndex,
        merkleTreeAddress,
        originator,
        receiverAddress,
        senderAddress,
        expirationTimestamp,
        nonce,
        transferProxyProgramAddress
    } = request;


    if (
        signer.address !== senderAddress
    ) {

        throw new Error(
            "Solana signer mismatch: " +
            signer.address +
            " != " +
            senderAddress
        );
    }


    const message = [
        "TRANSFER",
        transferProxyProgramAddress,
        merkleTreeAddress,
        leafIndex.toString(),
        nonce.toString(),
        expirationTimestamp.toString(),
        receiverAddress,
        "0x",
        originator
    ].join(":");


    const bytes =
        new TextEncoder().encode(
            message
        );


    const hash =
        await crypto.webcrypto.subtle.digest(
            "SHA-256",
            bytes
        );


    const signable =
        createSignableMessage(
            new Uint8Array(hash)
        );


    const [
        signatures
    ] = await signer.signMessages([
        signable
    ]);


    return getBase58Decoder().decode(
        signatures[
            signer.address
        ]
    );
}


async function signOne(a) {

    const r = a.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }


    /*
     * STARKEX TRANSFER
     */

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        const signature =
            signAuthorizationRequest(
                input.privateKey,
                r
            );


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


    /*
     * STARKEX LIMIT ORDER
     */

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {

        const signature =
            signAuthorizationRequest(
                input.privateKey,
                r
            );


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


    /*
     * MANGOPAY WALLET
     */

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {

        const signature =
            signAuthorizationRequest(
                input.privateKey,
                r
            );


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


    /*
     * SOLANA TOKEN TRANSFER
     *
     * Used by migrated cards.
     */

    if (
        r.__typename ===
        "SolanaTokenTransferAuthorizationRequest"
        ||
        r.__typename ===
        "StarkexV6TokenTransferAuthorizationRequest"
    ) {

        const signature =
            await signSolanaRequest(
                r
            );


        return {
            fingerprint:
                a.fingerprint,

            solanaTokenTransferApproval: {
                nonce:
                    r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }


    throw new Error(
        "Authorization non supportata: " +
        r.__typename
    );
}


(async () => {

    const result = [];

    for (
        const authorization
        of input.authorizations
    ) {

        result.push(
            await signOne(
                authorization
            )
        );
    }

    process.stdout.write(
        JSON.stringify(result)
    );

})().catch(err => {

    console.error(
        err.stack ||
        err.message ||
        String(err)
    );

    process.exit(1);
});
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

    try:

        return json.loads(
            p.stdout
        )

    except Exception as e:

        raise RuntimeError(
            "Output firma Node non valido: "
            f"{e}"
        )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(
    card,
    price_cents
):

    asset_id = str(
        card.get("assetId") or ""
    ).strip()

    if not asset_id:

        print(
            "❌ AutoSell: assetId mancante",
            flush=True
        )

        return None


    if DRY_RUN:

        print(
            "🟡 DRY RUN: vendita simulata",
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
            f"   └─ Prezzo: "
            f"{format_eur(price_cents)}",
            flush=True
        )

        return "DRY-RUN"


    # ========================================================
    # IMPORTANT:
    #
    # prepareOfferInput ATTUALE NON HA PIÙ "type".
    #
    # Sorare schema attuale:
    #
    # clientMutationId
    # receiveAmount
    # receiveAssetIds
    # receiverSlug
    # sendAmount
    # sendAssetIds
    # settlementCurrencies
    #
    # ========================================================

    prepare_input = {

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {
            "amount":
                str(price_cents),

            "currency":
                "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
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


                        ... on SolanaTokenTransferAuthorizationRequest {

                            assetId

                            leafIndex

                            merkleTreeAddress

                            originator

                            receiverAddress

                            senderAddress

                            expirationTimestamp

                            nonce

                            transferProxyProgramAddress
                        }


                        ... on StarkexV6TokenTransferAuthorizationRequest {

                            assetId

                            leafIndex

                            merkleTreeAddress

                            originator

                            receiverAddress

                            senderAddress

                            expirationTimestamp

                            nonce

                            transferProxyProgramAddress
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
            "❌ AutoSell: "
            "prepareOffer senza risultato",
            flush=True
        )

        return None


    errors = (
        result.get("errors")
        or []
    )


    if errors:

        print(
            "❌ AutoSell prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
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
            "nessuna authorization",
            flush=True
        )

        return None


    print(
        "🔐 Authorization ricevute: "
        f"{len(authorizations)}",
        flush=True
    )


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


    # ========================================================
    # CREATE SINGLE SALE OFFER
    # ========================================================

    create_input = {

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "assetId":
            asset_id,

        "receiveAmount": {
            "amount":
                str(price_cents),

            "currency":
                "EUR"
        },

        "settlementCurrencies": [
            "EUR"
        ],

        "clientMutationId":
            str(uuid.uuid4())
    }


    data = graphql("""
        mutation CreateSingleSaleOffer(
            $input: createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(
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
            create_input
    })


    result = (
        ((data or {}).get("data") or {})
        .get(
            "createSingleSaleOffer"
        )
    )


    if not result:

        print(
            "❌ AutoSell: "
            "createSingleSaleOffer "
            "senza risultato",
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
            "createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return None


    offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )


    offer_id = offer.get("id")


    if not offer_id:

        print(
            "❌ AutoSell: "
            "tokenOffer ID mancante",
            flush=True
        )

        return None


    print(
        f"✅ AUTOSELL CREATO: "
        f"{offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):

    asset_id = str(
        row.get("asset_id") or ""
    ).strip()

    source = (
        row.get("source")
        or "UNKNOWN"
    )


    if not asset_id:
        return


    print(
        "\n💰 AUTOSELL CHECK",
        flush=True
    )

    print(
        f"   ├─ Asset: {asset_id}",
        flush=True
    )

    print(
        f"   ├─ Provenienza: {source}",
        flush=True
    )

    print(
        "   └─ Età: NON UTILIZZATA",
        flush=True
    )


    cards = card_details([
        asset_id
    ])


    if len(cards) != 1:

        print(
            "❌ AutoSell: impossibile "
            "recuperare la carta "
            "→ NON VENDERE",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=
                "CARD_DETAILS_UNAVAILABLE"
        )

        return


    card = cards[0]


    if norm(
        card.get("assetId")
    ) != norm(asset_id):

        print(
            "❌ AutoSell: assetId "
            "non corrispondente "
            "→ BLOCCATO",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=
                "ASSET_ID_MISMATCH"
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
        "✅ AutoSell - Carta valida: "
        f"{card_label(card)}",
        flush=True
    )

    print(
        f"   ├─ Rarità: "
        f"{info.get('rarity')}",
        flush=True
    )

    print(
        f"   ├─ Floor: "
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
            f"❌ SELL_PRICE_MODE "
            f"non supportato: "
            f"{SELL_PRICE_MODE}",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error=
                "INVALID_SELL_PRICE_MODE"
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
            last_error=
                "FINAL_PRICE_OUT_OF_RANGE"
        )

        return


    # ========================================================
    # BLOCCO ATOMICO LOGICO:
    # prima SELLING, poi API
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
            last_error=
                "CREATE_SALE_FAILED"
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
        f"   ├─ Carta: "
        f"{card_label(card)}",
        flush=True
    )

    print(
        f"   ├─ Prezzo: "
        f"{format_eur(sell_price)}",
        flush=True
    )

    print(
        f"   └─ Offer ID: "
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
        f"💰 RANGE: "
        f"{format_eur(MIN_PRICE)} - "
        f"{format_eur(MAX_PRICE)}",
        flush=True
    )

    print(
        f"📊 LISTING MINIME: "
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
        "🛡️ SOURCE: AUTOBUY / SWAP "
        "+ OPERATIONS DECK",
        flush=True
    )

    print(
        f"📋 DECK: "
        f"{OPERATIONS_DECK_NAME}",
        flush=True
    )

    print(
        f"💾 JSON: "
        f"{JSON_PATH}",
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
            "🛡️ AutoSell resta attivo "
            "ma NON venderà carte "
            "finché la Coverage "
            "non sarà verificata.",
            flush=True
        )


    # ========================================================
    # ACCOUNT
    # ========================================================

    if not check_account():
        return


    # ========================================================
    # OPERATIONS DECK
    # ========================================================

    sync_operations_deck()


    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            load_coverage()

            sync_operations_deck()

            rows = get_ready_cards()


            print(
                f"🗄️ Carte READY nel JSON: "
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
                        f"❌ AutoSell errore "
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


    with operations_deck_lock:

        deck_cards = set(
            operations_deck_cache
        )


    return jsonify({

        "status":
            "online",

        "bot":
            "autosell",

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
            RARITY_REQUIRED,

        "coverage":
            "REQUIRED",

        "coverage_available":
            coverage_ok,

        "kulenovic":
            "NEVER_SELL",

        "source":
            "AUTOBUY_OR_SWAP_PLUS_OPERATIONS_DECK",

        "operations_deck":
            OPERATIONS_DECK_NAME,

        "operations_deck_cards":
            len(deck_cards),

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


    with operations_deck_lock:

        deck_loaded = bool(
            operations_deck_cache
        )


    return jsonify({

        "status":
            "ok",

        "bot":
            "autosell",

        "version":
            BOT_VERSION,

        "worker_started":
            worker_started,

        "coverage_loaded":
            loaded,

        "coverage_available":
            coverage_ok,

        "operations_deck_loaded":
            deck_loaded,

        "operations_deck":
            OPERATIONS_DECK_NAME,

        "dry_run":
            DRY_RUN
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
