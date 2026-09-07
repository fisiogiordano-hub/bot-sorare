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

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LIVE_LISTINGS = 5

COVERAGE_CACHE = 3600
USD_CACHE = 300

BOT_VERSION = "AUTOSell-2.3-COVERAGE-63"


# ============================================================
# PREZZO
# ============================================================

SELL_PRICE_MODE = os.getenv(
    "SELL_PRICE_MODE",
    "FLOOR"
).upper()


# ============================================================
# JSON
# ============================================================

JSON_PATH = os.getenv(
    "AUTOSSELL_JSON_PATH",
    "autosell_cards.json"
).strip()

json_lock = threading.Lock()


# ============================================================
# KULENOVIC
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
# WORKER / CACHE
# ============================================================

worker_lock = threading.Lock()
worker_started = False

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

coverage_verified = False


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


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


def card_label(card):
    name = card_name(card)
    slug = card.get("slug")

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


def format_eur(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# JSON DATABASE
# ============================================================

def ensure_json_file():

    directory = os.path.dirname(
        os.path.abspath(JSON_PATH)
    )

    os.makedirs(
        directory,
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

        if isinstance(data, list):
            return data

        print(
            "⚠️ JSON non contiene una lista",
            flush=True
        )

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

    directory = os.path.dirname(
        os.path.abspath(JSON_PATH)
    )

    os.makedirs(
        directory,
        exist_ok=True
    )

    temp_path = (
        JSON_PATH
        + ".tmp."
        + str(uuid.uuid4())
    )

    try:

        with open(
            temp_path,
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
            temp_path,
            JSON_PATH
        )

        return True

    except Exception as e:

        print(
            f"❌ Scrittura JSON: {e}",
            flush=True
        )

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        return False


def get_ready_cards():

    with json_lock:

        cards = load_cards()

        result = []

        for card in cards:

            if not isinstance(card, dict):
                continue

            if norm(card.get("status")) != "ready":
                continue

            asset_id = str(
                card.get("asset_id") or ""
            ).strip()

            if asset_id:
                result.append(card)

        return result


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
        found = False

        for card in cards:

            if not isinstance(card, dict):
                continue

            current = str(
                card.get("asset_id") or ""
            ).strip()

            if current.lower() != asset_id.lower():
                continue

            found = True

            if status is not None:
                card["status"] = status

            if sale_offer_id is not None:
                card["sale_offer_id"] = sale_offer_id

            if last_error is not None:
                card["last_error"] = last_error
            elif status in ("SELLING", "SOLD"):
                card["last_error"] = None

            if status == "SELLING":
                card["selling_at"] = now_iso()

            if status == "SOLD":
                card["sold_at"] = now_iso()

            break

        if not found:

            print(
                f"⚠️ Asset non trovato nel JSON: {asset_id}",
                flush=True
            )

            return False

        return save_cards(cards)


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

        for card in cards:

            if not isinstance(card, dict):
                continue

            existing = str(
                card.get("asset_id") or ""
            ).strip()

            if existing.lower() == asset_id.lower():

                if norm(card.get("status")) == "sold":
                    return False

                return True

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
# HEADERS
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
            f"Sorare-AutoSell/{BOT_VERSION}"
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

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

            response = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True
            )

            if response.status_code == 429:

                retry = response.headers.get(
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

            if response.status_code != 200:

                print(
                    f"❌ Sorare HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:1000]}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            data = response.json()

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

def load_coverage(force=False):

    global coverage_cache
    global coverage_time
    global coverage_verified

    now = time.time()

    with coverage_lock:

        cached = set(coverage_cache)
        cached_time = coverage_time

    if (
        not force
        and cached
        and now - cached_time < COVERAGE_CACHE
    ):
        return cached

    try:

        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-AutoSell/{BOT_VERSION}"
            }
        )

        print(
            f"🌐 Coverage HTTP {r.status_code}",
            flush=True
        )

        if r.status_code != 200:

            print(
                "⚠️ Coverage HTTP non valido",
                flush=True
            )

            return cached

        text = r.text or ""

        result = set()

        # ----------------------------------------------------
        # Metodo 1: URL football/leagues
        # ----------------------------------------------------

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            text,
            re.I
        )

        for item in matches:

            item = norm(item)

            if item:
                result.add(item)

        # ----------------------------------------------------
        # Metodo 2: JSON / slug
        # ----------------------------------------------------

        slug_matches = re.findall(
            r'"slug"\s*:\s*"([^"]+)"',
            text,
            re.I
        )

        for item in slug_matches:

            item = norm(item)

            if (
                item
                and len(item) < 100
                and not item.startswith("http")
            ):
                result.add(item)

        # ----------------------------------------------------
        # Metodo 3: cerca competizioni football
        # ----------------------------------------------------

        football_matches = re.findall(
            r'football[^"\']{0,100}',
            text,
            re.I
        )

        for item in football_matches:

            item = norm(item)

            if item:
                result.add(item)

        # ----------------------------------------------------
        # Se Sorare restituisce la pagina ma il parser
        # non riconosce gli slug, NON consideriamo la
        # coverage come motivo sufficiente per spegnere
        # il worker.
        # ----------------------------------------------------

        with coverage_lock:

            if result:

                coverage_cache = result
                coverage_time = time.time()
                coverage_verified = True

                print(
                    f"🌐 Coverage riconosciuta: "
                    f"{len(result)} elementi",
                    flush=True
                )

            else:

                coverage_time = time.time()

                print(
                    "⚠️ Coverage ricevuta ma parser "
                    "senza slug riconosciuti",
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
        "playerSlug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):
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

        for c in (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        ):

            try:

                c_season = int(
                    c.get("seasonYear")
                )

            except (
                TypeError,
                ValueError
            ):

                continue

            c_player = norm(
                (c.get("anyPlayer") or {})
                .get("slug")
            )

            c_rarity = norm(
                c.get("rarityTyped")
            )

            if (
                c_player == player_slug
                and c_rarity == rarity
                and c_season == season
            ):

                p = price_eur(
                    (
                        offer.get("receiverSide")
                        or {}
                    ).get("amounts") or {}
                )

                if p is not None:
                    prices.append(p)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    if KID:
        wanted.add(norm(KID))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


# ============================================================
# COVERAGE VALIDATION
# ============================================================

def coverage_info(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active = []

    for c in (
        club.get("activeCompetitions")
        or []
    ):

        if isinstance(c, dict):

            slug = norm(
                c.get("slug")
            )

            if slug:
                active.append(slug)

    coverage = load_coverage()

    covered = [
        x for x in active
        if x in coverage
    ]

    # --------------------------------------------------------
    # IMPORTANTE:
    #
    # Se la pagina Coverage di Sorare è raggiungibile ma
    # il parser non trova gli slug, non blocchiamo il bot
    # per questo motivo.
    #
    # La verifica della carta continua comunque con:
    # LIMITED + floor + listing minimi + range prezzo +
    # Kulenovic barrier.
    # --------------------------------------------------------

    if not coverage:

        return True, active, [], "COVERAGE_UNPARSED"

    return (
        bool(covered),
        active,
        covered,
        "COVERAGE_VERIFIED"
    )


# ============================================================
# AUTOSell VALIDATION
# ============================================================

def validate_for_autosell(card):

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        return False, {

            "code": "KULENOVIC",

            "message":
                "Kulenovic è protetto"
        }


    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        return False, {

            "code": "RARITY",

            "message":
                "rarità diversa da LIMITED",

            "rarity":
                rarity or "N/D"
        }


    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:

        return False, {

            "code": "PRICE_UNKNOWN",

            "message":
                "floor live non disponibile "
                "oppure meno di "
                f"{MIN_LIVE_LISTINGS} listing",

            "min_live_listings":
                MIN_LIVE_LISTINGS
        }


    # --------------------------------------------------------
    # FLOOR MIN
    # --------------------------------------------------------

    if floor < MIN_PRICE:

        return False, {

            "code": "PRICE_LOW",

            "message":
                "floor sotto il minimo",

            "floor": floor,

            "min_price": MIN_PRICE
        }


    # --------------------------------------------------------
    # FLOOR MAX
    # --------------------------------------------------------

    if floor > MAX_PRICE:

        return False, {

            "code": "PRICE_HIGH",

            "message":
                "floor sopra il massimo",

            "floor": floor,

            "max_price": MAX_PRICE
        }


    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    covered, active, covered_competitions, mode = (
        coverage_info(card)
    )

    if not covered:

        return False, {

            "code": "COVERAGE",

            "message":
                "nessuna competizione coperta",

            "active_competitions":
                active,

            "covered_competitions":
                covered_competitions
        }


    # --------------------------------------------------------
    # VALIDA
    # --------------------------------------------------------

    return True, {

        "floor": floor,

        "rarity": rarity,

        "active_competitions":
            active,

        "covered_competitions":
            covered_competitions,

        "coverage_mode":
            mode
    }


# ============================================================
# REJECTION LOG
# ============================================================

def print_rejection(card, info):

    print(
        f"🚫 AutoSell ESCLUSA: "
        f"{card_label(card)}",
        flush=True
    )

    if not info:

        print(
            "   └─ Motivo: verifica fallita",
            flush=True
        )

        return

    code = info.get("code")


    if code == "KULENOVIC":

        print(
            "   └─ Motivo: KULENOVIC PROTETTO",
            flush=True
        )

        return


    if code == "RARITY":

        print(
            "   ├─ Motivo: RARITÀ NON VALIDA",
            flush=True
        )

        print(
            f"   └─ Rarità: "
            f"{info.get('rarity')}",
            flush=True
        )

        return


    if code == "PRICE_UNKNOWN":

        print(
            "   ├─ Motivo: FLOOR LIVE "
            "NON DISPONIBILE",
            flush=True
        )

        print(
            f"   └─ Listing richiesti: "
            f"{info.get('min_live_listings')}",
            flush=True
        )

        return


    if code == "PRICE_LOW":

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

        return


    if code == "PRICE_HIGH":

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

        return


    if code == "COVERAGE":

        print(
            "   ├─ Motivo: "
            "COMPETIZIONE NON COPERTA",
            flush=True
        )

        print(
            "   └─ Carta bloccata",
            flush=True
        )

        return


    print(
        f"   └─ Motivo: "
        f"{info.get('message', 'sconosciuto')}",
        flush=True
    )


# ============================================================
# NODE SIGNING
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

function sign(a) {

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
        "Authorization non supportata: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(sign)
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

    return json.loads(
        p.stdout
    )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price_cents):

    asset_id = str(
        card.get("assetId") or ""
    ).strip()

    if not asset_id:

        print(
            "❌ AutoSell: assetId mancante",
            flush=True
        )

        return None


    prepare_input = {

        "type":
            "SINGLE_SALE_OFFER",

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "receiveAmount": {

            "amount":
                str(price_cents),

            "currency":
                "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
    }


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


    # --------------------------------------------------------
    # PREPARE OFFER
    # --------------------------------------------------------

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
        "input":
            prepare_input
    })


    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )


    if not result:

        print(
            "❌ AutoSell: prepareOffer "
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
            "❌ AutoSell prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return None


    authorizations = (
        result.get("authorizations")
        or []
    )


    if not authorizations:

        print(
            "❌ AutoSell: "
            "nessuna authorization",
            flush=True
        )

        return None


    # --------------------------------------------------------
    # SIGN
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

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
        .get("createSingleSaleOffer")
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


    token_offer = (
        result.get("tokenOffer")
        or {}
    )


    offer_id = token_offer.get("id")


    if not offer_id:

        print(
            "❌ AutoSell: "
            "tokenOffer ID mancante",
            flush=True
        )

        return None


    print(
        f"✅ AUTOSELL CREATO: {offer_id}",
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
        f"   └─ Provenienza: {source}",
        flush=True
    )


    # --------------------------------------------------------
    # DETAILS
    # --------------------------------------------------------

    cards = card_details(
        [asset_id]
    )


    if len(cards) != 1:

        print(
            "❌ Carta non recuperabile "
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


    # --------------------------------------------------------
    # HARD ASSET BARRIER
    # --------------------------------------------------------

    returned_asset = str(
        card.get("assetId") or ""
    ).strip()


    if returned_asset.lower() != asset_id.lower():

        print(
            "❌ Asset ID non corrispondente "
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


    # --------------------------------------------------------
    # KULENOVIC HARD BARRIER
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            "🛡️ KULENOVIC RILEVATO "
            "→ MAI VENDERE",
            flush=True
        )

        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error="KULENOVIC"
        )

        return


    # --------------------------------------------------------
    # SOURCE BARRIER
    # --------------------------------------------------------

    source_norm = norm(source)

    allowed_sources = {
        "autobuy",
        "auto-buy",
        "swap",
        "autoswap",
        "bot"
    }

    if source_norm not in allowed_sources:

        print(
            f"🛑 Provenienza non autorizzata: "
            f"{source}",
            flush=True
        )

        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error="INVALID_SOURCE"
        )

        return


    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    valid, info = validate_for_autosell(
        card
    )


    if not valid:

        print_rejection(
            card,
            info
        )

        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error=(
                info or {}
            ).get(
                "code",
                "INVALID"
            )
        )

        return


    floor = info.get("floor")


    print(
        f"✅ Carta valida: "
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
        f"   └─ Coverage mode: "
        f"{info.get('coverage_mode')}",
        flush=True
    )


    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    if SELL_PRICE_MODE != "FLOOR":

        print(
            f"❌ SELL_PRICE_MODE non supportato: "
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


    if sell_price is None:

        print(
            "❌ Prezzo vendita N/D",
            flush=True
        )

        return


    # --------------------------------------------------------
    # FINAL PRICE BARRIER
    # --------------------------------------------------------

    if (
        sell_price < MIN_PRICE
        or sell_price > MAX_PRICE
    ):

        print(
            "🛑 Prezzo finale fuori range "
            "→ BLOCCATO",
            flush=True
        )

        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error=
                "FINAL_PRICE_OUT_OF_RANGE"
        )

        return


    # --------------------------------------------------------
    # MARK SELLING
    # --------------------------------------------------------

    if not update_json_card(
        asset_id,
        status="SELLING",
        last_error=None
    ):

        print(
            "❌ Impossibile aggiornare JSON "
            "→ NON VENDERE",
            flush=True
        )

        return


    # --------------------------------------------------------
    # CREATE SALE
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    if not update_json_card(
        asset_id,
        status="SOLD",
        sale_offer_id=offer_id,
        last_error=None
    ):

        print(
            "⚠️ Vendita creata ma JSON "
            "non aggiornato",
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
        f"📦 VERSIONE: {BOT_VERSION}",
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
        "🛡️ SOURCE: AUTOBUY / SWAP",
        flush=True
    )

    print(
        f"💾 JSON: {JSON_PATH}",
        flush=True
    )


    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------

    try:

        headers()

    except Exception as e:

        print(
            f"❌ Configurazione Sorare: {e}",
            flush=True
        )

        return


    try:

        ensure_json_file()

    except Exception as e:

        print(
            f"❌ Configurazione JSON: {e}",
            flush=True
        )

        return


    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    coverage = load_coverage(
        force=True
    )


    if coverage:

        print(
            f"🏆 Coverage riconosciuta: "
            f"{len(coverage)} elementi",
            flush=True
        )

    else:

        print(
            "⚠️ Coverage non parsata.",
            flush=True
        )

        print(
            "🛡️ AutoSell RESTA ATTIVO.",
            flush=True
        )

        print(
            "   La coverage non verrà usata "
            "come unico motivo per bloccare "
            "le vendite.",
            flush=True
        )


    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():
        return


    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:

        try:

            rows = get_ready_cards()


            print(
                f"🗄️ Carte READY nel JSON: "
                f"{len(rows)}",
                flush=True
            )


            for row in rows:

                try:

                    process_card(row)

                except Exception as e:

                    asset_id = str(
                        row.get("asset_id")
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
            name="autosell-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
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

        verified = coverage_verified

    ready = get_ready_cards()

    return jsonify({

        "status": "online",

        "bot": "autosell",

        "version": BOT_VERSION,

        "dry_run": DRY_RUN,

        "min_price_cents": MIN_PRICE,

        "max_price_cents": MAX_PRICE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "age_parameter":
            "NOT_USED",

        "rarity":
            "LIMITED",

        "coverage":
            "SAFE_OPTIONAL",

        "coverage_verified":
            verified,

        "kulenovic":
            "NEVER_SELL",

        "source":
            "AUTOBUY_OR_SWAP_ONLY",

        "storage":
            "PERSISTENT_JSON",

        "json_path":
            JSON_PATH,

        "ready_cards":
            len(ready),

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

    return jsonify({

        "status": "ok",

        "bot": "autosell",

        "version": BOT_VERSION,

        "worker_started":
            worker_started,

        "coverage_loaded":
            loaded,

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
