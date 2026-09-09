import os
import time
import uuid
import json
import base64
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

SORARE_URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"
STATE_FILE = "bot_state.json"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv(
    "GITHUB_REPO",
    "fisiogiordano-hub/bot-sorare"
).strip()
GITHUB_BRANCH = os.getenv(
    "GITHUB_BRANCH",
    "main"
).strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SWAP_AUTO_ACCEPT = os.getenv(
    "SWAP_AUTO_ACCEPT", "false"
).lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
MIN_LIVE_LISTINGS = 5

SWAP_MIN = 1.20
SWAP_MAX = 1.25

INTERVAL = 10
TIMEOUT = 25
USD_CACHE = 300
COVERAGE_CACHE = 3600

BOT_VERSION = "23.0-AUTOBUY-PENDING-FIX"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# STATE
# ============================================================

processed = set()

# Carte realmente acquisite dal bot
acquired_cards = {}

# AutoBuy creati ma NON ancora completati
pending_autobuys = {}

state_lock = threading.Lock()
github_lock = threading.Lock()
coverage_lock = threading.Lock()
worker_lock = threading.Lock()

worker_started = False

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0

current_user_slug = None


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def card_name(card):
    return card.get("name") or card.get("slug") or "Carta"


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
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    result = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {}
    }

    for attempt in range(3):
        try:
            r = requests.post(
                SORARE_URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                time.sleep(
                    min(
                        int(
                            r.headers.get(
                                "Retry-After",
                                attempt + 2
                            )
                        ),
                        15
                    )
                )
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            data = r.json()

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
            time.sleep(attempt + 1)

    return None


# ============================================================
# LOCAL / GITHUB STATE
# ============================================================

def normalize_acquired_card(item):
    if not isinstance(item, dict):
        return None

    asset_id = str(
        item.get("assetId")
        or item.get("asset_id")
        or ""
    ).strip()

    if not asset_id:
        return None

    return {
        "assetId": asset_id,
        "slug": item.get("slug"),
        "purchase_price_cents":
            item.get("purchase_price_cents"),
        "status":
            item.get("status") or "da_vendere",
        "source":
            item.get("source") or "unknown",
        "offer_id":
            item.get("offer_id")
    }


def normalize_pending_autobuy(item):
    if not isinstance(item, dict):
        return None

    offer_id = str(
        item.get("offer_id") or ""
    ).strip()

    if not offer_id:
        return None

    cards = item.get("cards") or []

    if not isinstance(cards, list):
        cards = []

    return {
        "offer_id": offer_id,
        "original_offer_id":
            item.get("original_offer_id"),
        "created_at":
            item.get("created_at"),
        "cards": cards,
        "price_per_card":
            item.get(
                "price_per_card",
                PAY_PER_CARD
            ),
        "status":
            item.get("status") or "PENDING"
    }


def build_state():
    with state_lock:
        return {
            "processed_offers":
                sorted(processed),

            "acquired_cards":
                list(
                    acquired_cards.values()
                ),

            "pending_autobuys":
                list(
                    pending_autobuys.values()
                ),

            "updated_at":
                int(time.time())
        }


def load_state_data(data):
    global processed
    global acquired_cards
    global pending_autobuys

    if not isinstance(data, dict):
        return

    ids = data.get("processed_offers") or []

    if isinstance(ids, list):
        processed = {
            norm(x)
            for x in ids
            if x
        }

    cards = data.get("acquired_cards") or []

    if isinstance(cards, list):
        for item in cards:
            card = normalize_acquired_card(item)

            if card:
                acquired_cards[
                    norm(card["assetId"])
                ] = card

    pending = data.get("pending_autobuys") or []

    if isinstance(pending, list):
        for item in pending:
            offer = normalize_pending_autobuy(item)

            if offer:
                pending_autobuys[
                    norm(offer["offer_id"])
                ] = offer


def load_local_state():
    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        # Compatibilità con vecchio file
        if isinstance(data, dict):
            load_state_data(data)
        else:
            print(
                "⚠️ bot_state.json non è un oggetto "
                "→ ignorato",
                flush=True
            )

    except Exception as e:
        print(
            f"⚠️ Lettura {STATE_FILE}: {e}",
            flush=True
        )


def save_local_state():
    tmp = STATE_FILE + ".tmp"

    try:
        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                build_state(),
                f,
                indent=2,
                ensure_ascii=False
            )

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print(
            f"❌ Salvataggio {STATE_FILE}: {e}",
            flush=True
        )
        return False


def github_headers():
    if not GITHUB_TOKEN:
        return None

    return {
        "Authorization":
            f"Bearer {GITHUB_TOKEN}",
        "Accept":
            "application/vnd.github+json",
        "X-GitHub-Api-Version":
            "2022-11-28",
        "User-Agent":
            f"Sorare-Bot/{BOT_VERSION}"
    }


def github_state_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO}/contents/{STATE_FILE}"
    )


def load_github_state():
    if not GITHUB_TOKEN:
        return

    try:
        r = requests.get(
            github_state_url(),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=TIMEOUT
        )

        if r.status_code == 404:
            print(
                "ℹ️ bot_state.json non presente su GitHub",
                flush=True
            )
            return

        if r.status_code != 200:
            print(
                f"⚠️ GitHub load HTTP {r.status_code}: "
                f"{r.text[:500]}",
                flush=True
            )
            return

        data = r.json()

        content = data.get("content")

        if not content:
            return

        decoded = base64.b64decode(
            content.replace("\n", "")
        ).decode("utf-8")

        state = json.loads(decoded)

        if not isinstance(state, dict):
            print(
                "⚠️ GitHub bot_state.json non valido",
                flush=True
            )
            return

        with state_lock:
            load_state_data(state)

        print(
            f"💾 Stato GitHub caricato: "
            f"{len(processed)} offerte | "
            f"{len(acquired_cards)} carte | "
            f"{len(pending_autobuys)} AutoBuy pending",
            flush=True
        )

    except Exception as e:
        print(
            f"⚠️ GitHub load state: {e}",
            flush=True
        )


def save_github_state():
    if not GITHUB_TOKEN:
        return False

    with github_lock:
        try:
            raw = json.dumps(
                build_state(),
                indent=2,
                ensure_ascii=False
            )

            encoded = base64.b64encode(
                raw.encode("utf-8")
            ).decode("ascii")

            r = requests.get(
                github_state_url(),
                headers=github_headers(),
                params={"ref": GITHUB_BRANCH},
                timeout=TIMEOUT
            )

            sha = None

            if r.status_code == 200:
                sha = r.json().get("sha")

            payload = {
                "message":
                    f"Update bot_state.json "
                    f"{int(time.time())}",
                "content": encoded,
                "branch": GITHUB_BRANCH
            }

            if sha:
                payload["sha"] = sha

            r = requests.put(
                github_state_url(),
                headers=github_headers(),
                json=payload,
                timeout=TIMEOUT
            )

            if r.status_code not in (200, 201):
                print(
                    f"❌ GitHub save HTTP "
                    f"{r.status_code}: "
                    f"{r.text[:1000]}",
                    flush=True
                )
                return False

            print(
                "💾 bot_state.json salvato su GitHub",
                flush=True
            )

            return True

        except Exception as e:
            print(
                f"❌ GitHub save state: {e}",
                flush=True
            )
            return False


def load_state():
    load_local_state()
    load_github_state()

    save_local_state()

    print(
        f"💾 Stato iniziale: "
        f"{len(processed)} offerte processate | "
        f"{len(acquired_cards)} carte acquisite | "
        f"{len(pending_autobuys)} AutoBuy pending",
        flush=True
    )


def persist_state():
    save_local_state()

    if GITHUB_TOKEN:
        save_github_state()


def mark_done(offer_id):
    key = norm(offer_id)

    if not key:
        return

    with state_lock:
        processed.add(key)

    persist_state()


# ============================================================
# ACQUIRED CARDS
# ============================================================

def persist_acquired_card(
    card,
    purchase_price_cents,
    source,
    offer_id
):
    asset_id = str(
        card.get("assetId") or ""
    ).strip()

    if not asset_id:
        return

    key = norm(asset_id)

    item = {
        "assetId": asset_id,
        "slug": card.get("slug"),
        "purchase_price_cents":
            purchase_price_cents,
        "status":
            "da_vendere",
        "source":
            source,
        "offer_id":
            offer_id
    }

    with state_lock:
        existing = acquired_cards.get(key)

        if existing:
            existing.update({
                k: v
                for k, v in item.items()
                if v is not None
            })
        else:
            acquired_cards[key] = item

    print(
        f"💾 CARTA ACQUISITA: "
        f"{card_label(card)} | "
        f"source={source}",
        flush=True
    )

    persist_state()


# ============================================================
# PENDING AUTOBUY
# ============================================================

def add_pending_autobuy(
    offer_id,
    original_offer_id,
    cards
):
    item = {
        "offer_id": offer_id,
        "original_offer_id":
            original_offer_id,
        "created_at":
            int(time.time()),
        "cards": [
            {
                "assetId":
                    c.get("assetId"),
                "slug":
                    c.get("slug"),
                "name":
                    c.get("name")
            }
            for c in cards
        ],
        "price_per_card":
            PAY_PER_CARD,
        "status":
            "PENDING"
    }

    with state_lock:
        pending_autobuys[
            norm(offer_id)
        ] = item

    persist_state()

    print(
        f"💾 AutoBuy pending salvato: "
        f"{offer_id}",
        flush=True
    )


def remove_pending_autobuy(offer_id):
    with state_lock:
        pending_autobuys.pop(
            norm(offer_id),
            None
        )

    persist_state()


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    global current_user_slug

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

    current_user_slug = user.get("slug")

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or current_user_slug}",
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
# OFFERTE RICEVUTE
# ============================================================

def get_received_offers():
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

    connection = (
        user.get(
            "pendingTokenOffersReceived"
        )
        or {}
    )

    return connection.get("nodes") or []


# ============================================================
# OFFERTE AUTOBUY INVIATE
# ============================================================

def get_sent_pending_offers():
    data = graphql("""
        query {
            currentUser {
                pendingTokenOffersSent(first: 50) {
                    nodes {
                        id
                        status
                        type
                        sender {
                            ... on User {
                                slug
                            }
                        }
                        receiver {
                            ... on User {
                                slug
                            }
                        }
                        senderSide {
                            anyCards {
                                assetId
                                slug
                            }
                        }
                        receiverSide {
                            anyCards {
                                assetId
                                slug
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
        (user.get(
            "pendingTokenOffersSent"
        ) or {})
        .get("nodes")
        or []
    )


def get_offer_by_id(offer_id):
    data = graphql("""
        query OfferById($id: String!) {
            offer(id: $id) {
                id
                status
                type
                createdAt
                acceptedAt
                cancelledAt
                transactionDate

                sender {
                    ... on User {
                        slug
                    }
                }

                receiver {
                    ... on User {
                        slug
                    }
                }

                userBuyer {
                    slug
                }

                userSeller {
                    slug
                }

                actualReceiver {
                    ... on User {
                        slug
                    }
                }

                senderSide {
                    anyCards {
                        assetId
                        slug
                        name
                    }
                }

                receiverSide {
                    anyCards {
                        assetId
                        slug
                        name
                    }
                }
            }
        }
    """, {
        "id": offer_id
    })

    return (
        ((data or {}).get("data") or {})
        .get("offer")
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
                user {
                    slug
                }
                tokenOwner {
                    user {
                        slug
                    }
                }
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
        "assetIds": ids
    })

    if not data or data.get("errors"):
        return []

    return (
        (data.get("data") or {})
        .get("anyCards")
        or []
    )


def card_owned_by_me(card):
    if not current_user_slug:
        return False

    wanted = norm(current_user_slug)

    user = card.get("user") or {}

    if norm(user.get("slug")) == wanted:
        return True

    owner = card.get("tokenOwner") or {}
    owner_user = owner.get("user") or {}

    if norm(owner_user.get("slug")) == wanted:
        return True

    return False


# ============================================================
# PRICES
# ============================================================

def usd_eur():
    global usd_rate
    global usd_time

    now = time.time()

    if usd_rate and now - usd_time < USD_CACHE:
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

    except Exception:
        pass

    try:
        usd = float(
            amounts.get("usdCents")
        )
    except Exception:
        usd = 0

    if usd <= 0:
        return None

    rate = usd_eur()

    if not rate:
        return None

    return int(round(usd * rate))


def live_floor(card):
    player = card.get("anyPlayer") or {}

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
    except Exception:
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
        (((data.get("data") or {})
          .get("tokens") or {})
         .get("liveSingleSaleOffers") or {})
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
                c_season = int(
                    c.get("seasonYear")
                )
            except Exception:
                continue

            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) == player_slug
                and norm(
                    c.get("rarityTyped")
                ) == rarity
                and c_season == season
            ):
                p = price_eur(
                    (offer.get("receiverSide") or {})
                    .get("amounts")
                    or {}
                )

                if p is not None:
                    prices.append(p)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):
    global coverage_cache
    global coverage_time

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and now - coverage_time
            < COVERAGE_CACHE
        ):
            return set(coverage_cache)

        cached = set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-Bot/{BOT_VERSION}"
            }
        )

        if r.status_code != 200:
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
        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )
        return cached


# ============================================================
# VALIDAZIONE
# ============================================================

def is_kulenovic(card):
    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    kid = os.getenv(
        "KULENOVIC_ID",
        ""
    ).strip()

    if kid:
        wanted.add(norm(kid))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


def coverage_info(card):
    player = card.get("anyPlayer") or {}
    club = player.get("activeClub") or {}

    active = [
        norm(c.get("slug"))
        for c in (
            club.get("activeCompetitions")
            or []
        )
        if isinstance(c, dict)
        and c.get("slug")
    ]

    coverage = load_coverage()

    covered = [
        x for x in active
        if x in coverage
    ]

    return bool(covered), active, covered


def validate_card(card):
    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except Exception:
        return False, {
            "code": "AGE_UNKNOWN"
        }

    if age >= MAX_AGE:
        return False, {
            "code": "AGE",
            "age": age
        }

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":
        return False, {
            "code": "RARITY",
            "rarity": rarity
        }

    floor = live_floor(card)

    if floor is None:
        return False, {
            "code": "PRICE_UNKNOWN"
        }

    if floor < MIN_PRICE:
        return False, {
            "code": "PRICE_LOW",
            "floor": floor
        }

    if floor > MAX_PRICE:
        return False, {
            "code": "PRICE_HIGH",
            "floor": floor
        }

    covered, active, covered_competitions = (
        coverage_info(card)
    )

    if not covered:
        return False, {
            "code": "COVERAGE",
            "active": active,
            "covered": covered_competitions
        }

    return True, {
        "floor": floor,
        "age": age,
        "rarity": rarity,
        "covered": covered_competitions
    }


def print_rejection(card, info, context):
    code = info.get("code") if info else "UNKNOWN"

    messages = {
        "AGE":
            f"Età {info.get('age')} >= {MAX_AGE}",
        "AGE_UNKNOWN":
            "Età non disponibile",
        "RARITY":
            f"Rarità {info.get('rarity')}",
        "PRICE_UNKNOWN":
            "Floor live non disponibile "
            f"o meno di {MIN_LIVE_LISTINGS} inserzioni",
        "PRICE_LOW":
            f"Floor {format_eur(info.get('floor'))} "
            f"< {format_eur(MIN_PRICE)}",
        "PRICE_HIGH":
            f"Floor {format_eur(info.get('floor'))} "
            f"> {format_eur(MAX_PRICE)}",
        "COVERAGE":
            "Nessuna competizione coperta"
    }

    print(
        f"🚫 {context}: "
        f"{card_label(card)} → "
        f"{messages.get(code, code)}",
        flush=True
    )


# ============================================================
# REJECT OFFER
# ============================================================

def reject_offer(offer):
    blockchain_id = norm(
        offer.get("blockchainId")
    )

    if not blockchain_id:
        print(
            "❌ Reject: blockchainId mancante",
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
        mutation Reject(
            $input: rejectOfferInput!
        ) {
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
            "clientMutationId":
                str(uuid.uuid4())
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
        print(
            "❌ Reject errors:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True
    )

    return True


# ============================================================
# FIRMA
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
const { signAuthorizationRequest } =
require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function sign(a) {
    const r = a.request;

    if (!r)
        throw new Error(
            "AuthorizationRequest mancante"
        );

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
        input.authorizations.map(sign)
    )
);
'''

    p = subprocess.run(
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

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# CREATE AUTOBUY COUNTER-OFFER
# ============================================================

def counter_offer(offer, cards):
    receiver = norm(
        (offer.get("sender") or {})
        .get("slug")
    )

    ids = [
        str(c["assetId"]).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not ids:
        return None

    amount = (
        len(ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 AUTOBUY: creo controproposta "
        f"{len(ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True
    )

    if DRY_RUN:
        fake_id = (
            "DRYRUN:"
            + str(uuid.uuid4())
        )

        print(
            f"🟡 DRY RUN: {fake_id}",
            flush=True
        )

        return fake_id

    prepare = graphql("""
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
                    }
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": {
            "receiveAssetIds": ids,
            "sendAssetIds": [],
            "sendAmount": {
                "amount": str(amount),
                "currency": "EUR"
            },
            "receiverSlug": receiver,
            "settlementCurrencies": ["EUR"],
            "clientMutationId":
                str(uuid.uuid4())
        }
    })

    result = (
        ((prepare or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return None

    auth = result.get("authorizations") or []

    if not auth:
        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )
        return None

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(
            f"❌ Firma AutoBuy: {e}",
            flush=True
        )
        return None

    create = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {
                tokenOffer {
                    id
                    blockchainId
                    status
                    type
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": {
            "receiveAssetIds": ids,
            "sendAssetIds": [],
            "sendAmount": {
                "amount": str(amount),
                "currency": "EUR"
            },
            "receiverSlug": receiver,
            "settlementCurrencies": ["EUR"],
            "clientMutationId":
                str(uuid.uuid4()),
            "approvals": approvals,
            "dealId":
                str(uuid.uuid4())
        }
    })

    result = (
        ((create or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ createDirectOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return None

    token_offer = result.get("tokenOffer") or {}
    new_id = token_offer.get("id")

    if not new_id:
        print(
            "❌ createDirectOffer: "
            "ID mancante",
            flush=True
        )
        return None

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{new_id}",
        flush=True
    )

    return new_id


# ============================================================
# AUTOBUY
# ============================================================

def process_autobuy(offer):
    original_id = norm(
        offer.get("id")
    )

    if (
        not original_id
        or not should_process(original_id)
    ):
        return

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    # AutoBuy solo se l'offerta richiede Kulenovic
    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        return

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
        if reject_offer(offer):
            mark_done(original_id)
        return

    print(
        f"\n📨 AUTOBUY {original_id}",
        flush=True
    )

    details = card_details(ids)

    if len(details) != len(ids):
        print(
            "❌ AUTOBUY: "
            "impossibile verificare tutte le carte",
            flush=True
        )

        if reject_offer(offer):
            mark_done(original_id)

        return

    valid = []

    for card in details:
        ok, info = validate_card(card)

        if ok:
            print(
                f"✅ AUTOBUY - Carta valida: "
                f"{card_label(card)}",
                flush=True
            )

            print(
                f"   └─ Floor live: "
                f"{format_eur(info['floor'])}",
                flush=True
            )

            valid.append(card)

        else:
            print_rejection(
                card,
                info,
                "AUTOBUY"
            )

    if not valid:
        print(
            "🔴 AUTOBUY: "
            "nessuna carta valida → RIFIUTO",
            flush=True
        )

        if reject_offer(offer):
            mark_done(original_id)

        return

    new_offer_id = counter_offer(
        offer,
        valid
    )

    if not new_offer_id:
        print(
            "❌ AUTOBUY: "
            "controproposta non creata",
            flush=True
        )
        return

    # ========================================================
    # IMPORTANTE:
    #
    # NON registriamo ancora le carte come acquisite.
    #
    # Registriamo invece la SingleBuyOffer da monitorare.
    # ========================================================

    add_pending_autobuy(
        offer_id=new_offer_id,
        original_offer_id=original_id,
        cards=valid
    )

    # Ora possiamo chiudere l'offerta originale
    if reject_offer(offer):
        mark_done(original_id)

    print(
        f"⏳ AUTOBUY IN ATTESA: "
        f"{new_offer_id}",
        flush=True
    )


# ============================================================
# CONTROLLO AUTOBUY PENDING
# ============================================================

def check_pending_autobuys():
    with state_lock:
        pending = list(
            pending_autobuys.values()
        )

    if not pending:
        return

    print(
        f"🔎 Controllo {len(pending)} "
        f"AutoBuy pending...",
        flush=True
    )

    for item in pending:
        offer_id = item.get("offer_id")

        if not offer_id:
            continue

        try:
            offer = get_offer_by_id(
                offer_id
            )

            if not offer:
                print(
                    f"⚠️ AutoBuy {offer_id}: "
                    f"offerta non trovata",
                    flush=True
                )
                continue

            status = norm(
                offer.get("status")
            ).upper()

            print(
                f"📦 AUTOBUY {offer_id} "
                f"→ {status}",
                flush=True
            )

            # =================================================
            # RIFIUTATA / CANCELLATA / TERMINATA
            # =================================================

            if status in {
                "CANCELLED",
                "REJECTED",
                "ENDED",
                "SETTLEMENT_FAILED"
            }:
                print(
                    f"❌ AUTOBUY concluso senza acquisto: "
                    f"{offer_id} → {status}",
                    flush=True
                )

                remove_pending_autobuy(
                    offer_id
                )
                continue

            # =================================================
            # ACCETTATA
            #
            # Non registriamo ancora alla cieca.
            # Verifichiamo che le carte siano effettivamente
            # di proprietà del nostro account.
            # =================================================

            if status not in {
                "ACCEPTED",
                "SETTLEMENT_PUBLISHED"
            }:
                continue

            cards = item.get("cards") or []

            ids = [
                c.get("assetId")
                for c in cards
                if c.get("assetId")
            ]

            if not ids:
                remove_pending_autobuy(
                    offer_id
                )
                continue

            details = card_details(ids)

            if len(details) != len(ids):
                print(
                    f"⏳ AUTOBUY {offer_id}: "
                    f"carta non ancora verificabile",
                    flush=True
                )
                continue

            all_owned = all(
                card_owned_by_me(c)
                for c in details
            )

            if not all_owned:
                print(
                    f"⏳ AUTOBUY {offer_id}: "
                    f"offerta {status}, "
                    f"ma carta non ancora risultata "
                    f"di proprietà",
                    flush=True
                )
                continue

            # =================================================
            # QUI la carta è realmente nostra.
            # =================================================

            for card in details:
                persist_acquired_card(
                    card=card,
                    purchase_price_cents=
                        PAY_PER_CARD,
                    source="autobuy",
                    offer_id=offer_id
                )

            print(
                f"🎉 AUTOBUY COMPLETATO: "
                f"{offer_id}",
                flush=True
            )

            remove_pending_autobuy(
                offer_id
            )

        except Exception as e:
            print(
                f"❌ Controllo AutoBuy "
                f"{offer_id}: {e}",
                flush=True
            )


# ============================================================
# SWAP
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
        (((data or {}).get("data") or {})
         .get("config") or {})
        .get("exchangeRate", {})
        .get("id")
    )


def prepare_accept(offer_id):
    rate = get_exchange_rate_id()

    if not rate:
        return None, None

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
            "settlementInfo": {
                "currency": "WEI",
                "paymentMethod": "WALLET",
                "exchangeRateId": rate
            }
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareAcceptOffer")
    )

    if not result:
        return None, None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareAcceptOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return None, None

    return (
        result.get("authorizations") or [],
        rate
    )


def accept_offer(offer):
    offer_id = norm(
        offer.get("id")
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: ACCEPT simulato",
            flush=True
        )
        return True

    auth, rate = prepare_accept(
        offer_id
    )

    if not auth:
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(
            f"❌ Firma ACCEPT: {e}",
            flush=True
        )
        return False

    data = graphql("""
        mutation AcceptOffer(
            $input: acceptOfferInput!
        ) {
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
            "settlementInfo": {
                "currency": "WEI",
                "paymentMethod": "WALLET",
                "exchangeRateId": rate
            },
            "clientMutationId":
                str(uuid.uuid4())
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
        print(
            "❌ acceptOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return False

    print(
        "✅ SWAP ACCETTATO",
        flush=True
    )

    return True


def process_swap(offer):
    offer_id = norm(
        offer.get("id")
    )

    if (
        not offer_id
        or not should_process(offer_id)
    ):
        return

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

    give_ids = [
        c.get("assetId")
        for c in receiver_cards
        if c.get("assetId")
    ]

    receive_ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:
        mark_done(offer_id)
        return

    print(
        f"\n🔄 SWAP {offer_id}",
        flush=True
    )

    give = card_details(give_ids)
    receive = card_details(receive_ids)

    if (
        len(give) != len(give_ids)
        or len(receive) != len(receive_ids)
    ):
        if reject_offer(offer):
            mark_done(offer_id)
        return

    # Kulenovic mai cedibile
    if any(
        is_kulenovic(c)
        for c in give
    ):
        print(
            "🔒 SWAP RIFIUTATO: "
            "KULENOVIC NON È CEDIBILE",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    total_given = 0

    for card in give:
        floor = live_floor(card)

        if floor is None:
            print_rejection(
                card,
                {"code": "PRICE_UNKNOWN"},
                "SWAP - CARTA CEDUTA"
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return

        total_given += floor

        print(
            f"📤 SWAP ceduta: "
            f"{card_label(card)} → "
            f"{format_eur(floor)}",
            flush=True
        )

    total_received = 0

    for card in receive:
        ok, info = validate_card(card)

        if not ok:
            print_rejection(
                card,
                info,
                "SWAP - CARTA RICEVUTA"
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return

        total_received += info["floor"]

        print(
            f"📥 SWAP ricevuta: "
            f"{card_label(card)} → "
            f"{format_eur(info['floor'])}",
            flush=True
        )

    cash = price_eur(
        (offer.get("senderSide") or {})
        .get("amounts")
        or {}
    ) or 0

    total_received += cash

    minimum = int(
        round(total_given * SWAP_MIN)
    )

    maximum = int(
        round(total_given * SWAP_MAX)
    )

    print(
        f"📤 Ceduto: "
        f"{format_eur(total_given)}",
        flush=True
    )

    print(
        f"📥 Ricevuto: "
        f"{format_eur(total_received)}",
        flush=True
    )

    print(
        f"🎯 Range: "
        f"{format_eur(minimum)} - "
        f"{format_eur(maximum)}",
        flush=True
    )

    if total_received < minimum:
        if reject_offer(offer):
            mark_done(offer_id)
        return

    if total_received > maximum:
        if reject_offer(offer):
            mark_done(offer_id)
        return

    if not SWAP_AUTO_ACCEPT:
        print(
            "🛑 SWAP_AUTO_ACCEPT=False",
            flush=True
        )
        mark_done(offer_id)
        return

    if accept_offer(offer):
        for card in receive:
            floor = live_floor(card)

            persist_acquired_card(
                card=card,
                purchase_price_cents=floor,
                source="swap",
                offer_id=offer_id
            )

        mark_done(offer_id)


# ============================================================
# DISPATCH
# ============================================================

def process_offer(offer):
    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    if any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        process_autobuy(offer)
        return

    if sender_cards and receiver_cards:
        process_swap(offer)


# ============================================================
# WORKER
# ============================================================

def worker():
    print(
        "🤖 BOT AVVIATO",
        flush=True
    )

    print(
        f"📦 VERSIONE BOT: "
        f"{BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        f"💰 AutoBuy: "
        f"€{PAY_PER_CARD / 100:.2f}/carta",
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
        f"📊 Inserzioni minime: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        "🔄 SWAP: +20% / +25%",
        flush=True
    )

    print(
        "💶 SWAP CASH: solo cash già offerto",
        flush=True
    )

    print(
        "🚫 SWAP NON aggiunge cash",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI CEDIBILE",
        flush=True
    )

    print(
        "🎯 KULENOVIC RICHIESTO → SEMPRE AUTOBUY",
        flush=True
    )

    print(
        "💾 STATE: bot_state.json + GitHub",
        flush=True
    )

    load_state()

    coverage = load_coverage(force=True)

    if not coverage:
        print(
            "❌ Coverage non disponibile → bot fermato",
            flush=True
        )
        return

    print(
        f"🏆 Competizioni Football coperte: "
        f"{len(coverage)}",
        flush=True
    )

    if not check_account():
        return

    while True:
        try:
            # ------------------------------------------------
            # 1. CONTROLLA GLI AUTOBUY GIÀ CREATI
            # ------------------------------------------------

            check_pending_autobuys()

            # ------------------------------------------------
            # 2. LEGGE LE OFFERTE RICEVUTE
            # ------------------------------------------------

            offers = get_received_offers()

            print(
                f"📨 Offerte ricevute pendenti: "
                f"{len(offers)}",
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


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    with coverage_lock:
        covered = len(coverage_cache)

    with state_lock:
        processed_count = len(processed)
        acquired_count = len(acquired_cards)
        pending_count = len(pending_autobuys)

    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": BOT_VERSION,

        "dry_run": DRY_RUN,

        "autobuy": {
            "price_cents": PAY_PER_CARD,
            "min_floor_cents": MIN_PRICE,
            "max_floor_cents": MAX_PRICE,
            "max_age": MAX_AGE,
            "min_live_listings":
                MIN_LIVE_LISTINGS
        },

        "swap": {
            "auto_accept":
                SWAP_AUTO_ACCEPT,
            "min_multiplier":
                SWAP_MIN,
            "max_multiplier":
                SWAP_MAX
        },

        "kulenovic":
            "NEVER_CEDIBLE",

        "processed_offers":
            processed_count,

        "acquired_cards":
            acquired_count,

        "pending_autobuys":
            pending_count,

        "github_state":
            bool(GITHUB_TOKEN),

        "covered_competitions":
            covered
    })


@app.get("/health")
def health():
    with state_lock:
        return jsonify({
            "status": "ok",
            "bot": "running",
            "version": BOT_VERSION,
            "worker_started":
                worker_started,
            "processed_offers":
                len(processed),
            "acquired_cards":
                len(acquired_cards),
            "pending_autobuys":
                len(pending_autobuys),
            "dry_run":
                DRY_RUN,
            "swap_auto_accept":
                SWAP_AUTO_ACCEPT
        })


# ============================================================
# START
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
