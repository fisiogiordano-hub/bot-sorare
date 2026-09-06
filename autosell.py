import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
import requests


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

KID = os.getenv("KULENOVIC_ID", "").strip()

# SICUREZZA:
# se la variabile non esiste, il bot parte in DRY RUN.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Prezzi
MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70

# Inserzioni live minime necessarie per avere un floor affidabile
MIN_LIVE_LISTINGS = 5

# Età massima
MAX_AGE = 28

# Durata vendita
LISTING_DURATION = 7 * 24 * 60 * 60  # 7 giorni

# Intervallo tra i cicli
INTERVAL = 30

# Timeout HTTP
TIMEOUT = 25

# Cache cambio USD/EUR
USD_CACHE = 300

# Cache coverage
COVERAGE_CACHE = 3600

BOT_VERSION = "23.0-AUTOSELL-LINEUP-FIX"

# ============================================================
# KULENOVIC
# ============================================================

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# GLOBAL STATE
# ============================================================

processed = set()
state_lock = threading.Lock()

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

lineup_cache = set()
lineup_cache_time = 0
lineup_lock = threading.Lock()


# ============================================================
# FLASK
# ============================================================

try:
    from flask import Flask, jsonify

    app = Flask(__name__)

except Exception:
    app = None


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


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


def mark_done(offer_id):
    with state_lock:
        processed.add(offer_id)


def should_process(offer_id):
    with state_lock:
        return offer_id not in processed


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
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
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

                retry_after = response.headers.get(
                    "Retry-After",
                    attempt + 2
                )

                try:
                    wait = min(
                        int(retry_after),
                        15
                    )
                except Exception:
                    wait = attempt + 2

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    f"❌ HTTP: {response.text[:1000]}",
                    flush=True
                )

                time.sleep(attempt + 1)
                continue

            try:
                data = response.json()
            except Exception:
                print(
                    "❌ Risposta JSON non valida",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

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

        except Exception as exc:

            print(
                f"❌ GraphQL: {exc}",
                flush=True
            )

            time.sleep(attempt + 1)

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

        print(
            "❌ Account Sorare non verificato",
            flush=True
        )

        return False

    print(
        f"✅ Account: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key: "
        + (
            "PRESENTE"
            if user.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    return True


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):

    global coverage_cache
    global coverage_time

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

        response = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-AutoSell/{BOT_VERSION}"
            }
        )

        if response.status_code != 200:
            return cached

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            response.text,
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

    except Exception as exc:

        print(
            f"⚠️ Coverage: {exc}",
            flush=True
        )

        return cached


# ============================================================
# LINEUP CHECK
# ============================================================

def load_lineup_cards(force=False):
    """
    NUOVO CONTROLLO LINEUP.

    NON usa più:

        currentUser.mySo5Lineups

    Lo schema attuale Sorare espone:

        currentUser.blockchainCardsInLineups(
            sport: FOOTBALL
        )

    La query viene fatta UNA SOLA VOLTA per ciclo.
    """

    global lineup_cache
    global lineup_cache_time

    now = time.time()

    with lineup_lock:

        cached = set(lineup_cache)
        cached_time = lineup_cache_time

    if (
        not force
        and now - cached_time < 30
    ):
        return cached, True

    data = graphql("""
        query LineupCards {
            currentUser {
                blockchainCardsInLineups(
                    sport: FOOTBALL
                )
            }
        }
    """)

    if not data:
        return cached, False

    if data.get("errors"):
        return cached, False

    user = (
        ((data.get("data") or {}).get("currentUser"))
        or {}
    )

    values = user.get(
        "blockchainCardsInLineups"
    )

    if values is None:
        return cached, False

    result = {
        norm(x)
        for x in values
        if norm(x)
    }

    with lineup_lock:

        lineup_cache = result
        lineup_cache_time = time.time()

    print(
        f"🛡️ Carte presenti in lineup: "
        f"{len(result)}",
        flush=True
    )

    return result, True


def card_is_in_lineup(card, lineup_cards):
    """
    Confronta diversi identificativi disponibili
    per evitare problemi di formato.
    """

    candidates = set()

    for key in (
        "assetId",
        "ethereumId",
        "slug"
    ):

        value = norm(card.get(key))

        if value:
            candidates.add(value)

    return bool(
        candidates.intersection(lineup_cards)
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
        query Cards(
            $assetIds: [String!]!
        ) {
            anyCards(
                assetIds: $assetIds
            ) {
                assetId
                ethereumId
                slug
                name
                rarityTyped
                seasonYear
                serialNumber
                supply

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

                liveSingleSaleOffer {
                    id
                    status
                }

                liveSo5Lineup {
                    id
                }

                openedSo5Lineups {
                    id
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
# GALLERY
# ============================================================

def get_gallery():

    all_cards = []

    after = None

    while True:

        data = graphql("""
            query Gallery(
                $after: String
            ) {
                currentUser {

                    cards(
                        first: 100
                        after: $after
                        ownedByMe: true
                        sport: FOOTBALL
                    ) {
                        nodes {
                            assetId
                            ethereumId
                            slug
                            name
                            rarityTyped
                            seasonYear
                            serialNumber
                            supply

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

                            liveSingleSaleOffer {
                                id
                                status
                            }

                            liveSo5Lineup {
                                id
                            }

                            openedSo5Lineups {
                                id
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
            "after": after
        })

        if not data:
            break

        if data.get("errors"):
            print(
                "❌ Gallery GraphQL error",
                flush=True
            )
            break

        user = (
            ((data.get("data") or {})
             .get("currentUser"))
            or {}
        )

        connection = (
            user.get("cards")
            or {}
        )

        nodes = connection.get("nodes") or []

        all_cards.extend(nodes)

        page_info = (
            connection.get("pageInfo")
            or {}
        )

        if not page_info.get("hasNextPage"):
            break

        new_cursor = page_info.get(
            "endCursor"
        )

        if not new_cursor or new_cursor == after:
            break

        after = new_cursor

        # protezione
        if len(all_cards) > 5000:
            break

    return all_cards


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

        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR"
            },
            timeout=10
        )

        if response.status_code != 200:
            return None

        rate = float(
            (response.json().get("rates") or {})
            .get("EUR")
        )

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = now

        return rate

    except Exception as exc:

        print(
            f"❌ USD/EUR: {exc}",
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

    if not data:
        return None

    if data.get("errors"):
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

        sender_side = (
            offer.get("senderSide")
            or {}
        )

        cards = (
            sender_side.get("anyCards")
            or []
        )

        for listed_card in cards:

            listed_player = norm(
                (
                    listed_card
                    .get("anyPlayer")
                    or {}
                ).get("slug")
            )

            listed_rarity = norm(
                listed_card.get(
                    "rarityTyped"
                )
            )

            try:

                listed_season = int(
                    listed_card.get(
                        "seasonYear"
                    )
                )

            except (
                TypeError,
                ValueError
            ):
                continue

            if (
                listed_player == player_slug
                and listed_rarity == rarity
                and listed_season == season
            ):

                amount = price_eur(
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

                if amount is not None:
                    prices.append(amount)

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
        wanted.add(
            norm(KID)
        )

    for key in (
        "assetId",
        "ethereumId",
        "slug"
    ):

        value = norm(
            card.get(key)
        )

        if value in wanted:
            return True

    return False


# ============================================================
# COVERAGE CHECK
# ============================================================

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
        if isinstance(c, dict)
        and c.get("slug")
    ]

    coverage = load_coverage()

    if not active:
        return False, [], []

    covered = [
        x for x in active
        if x in coverage
    ]

    return (
        bool(covered),
        active,
        covered
    )


# ============================================================
# CARD VALIDATION
# ============================================================

def validate_card(card):

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        return False, {
            "code": "KULENOVIC",
            "message":
                "KULENOVIC MAI IN VENDITA"
        }

    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        return False, {
            "code": "RARITY",
            "message":
                "rarità diversa da LIMITED",
            "rarity":
                rarity or "NON DISPONIBILE"
        }

    # --------------------------------------------------------
    # AGE
    # --------------------------------------------------------

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
        ValueError
    ):

        return False, {
            "code": "AGE_UNKNOWN",
            "message":
                "età non disponibile"
        }

    if age >= MAX_AGE:

        return False, {
            "code": "AGE",
            "message":
                "età fuori limite",
            "age": age,
            "max_age": MAX_AGE
        }

    # --------------------------------------------------------
    # LIVE SALE ALREADY ACTIVE
    # --------------------------------------------------------

    existing_sale = (
        card.get(
            "liveSingleSaleOffer"
        )
    )

    if existing_sale:

        return False, {
            "code": "ALREADY_LISTED",
            "message":
                "carta già in vendita"
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
                f"o meno di {MIN_LIVE_LISTINGS} "
                "inserzioni",
            "min_live_listings":
                MIN_LIVE_LISTINGS
        }

    if floor < MIN_PRICE:

        return False, {
            "code": "PRICE_LOW",
            "message":
                "floor sotto il minimo",
            "floor": floor,
            "min_price": MIN_PRICE,
            "max_price": MAX_PRICE
        }

    if floor > MAX_PRICE:

        return False, {
            "code": "PRICE_HIGH",
            "message":
                "floor sopra il massimo",
            "floor": floor,
            "min_price": MIN_PRICE,
            "max_price": MAX_PRICE
        }

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    covered, active, covered_competitions = (
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

    return True, {
        "floor": floor,
        "age": age,
        "rarity": rarity,
        "active_competitions": active,
        "covered_competitions":
            covered_competitions
    }


# ============================================================
# REJECTION LOG
# ============================================================

def print_rejection(
    card,
    info
):

    label = card_label(card)

    print(
        f"🚫 {label} → esclusa",
        flush=True
    )

    if not info:
        return

    code = info.get("code")

    if code == "KULENOVIC":

        print(
            "   └─ Motivo: "
            "🔒 KULENOVIC MAI IN VENDITA",
            flush=True
        )

        return

    if code == "RARITY":

        print(
            "   └─ Motivo: "
            f"rarità {info.get('rarity')}",
            flush=True
        )

        return

    if code == "AGE":

        print(
            "   ├─ Motivo: ETÀ FUORI LIMITE",
            flush=True
        )

        print(
            f"   └─ Età: {info.get('age')} "
            f"→ limite < {info.get('max_age')}",
            flush=True
        )

        return

    if code == "AGE_UNKNOWN":

        print(
            "   └─ Motivo: ETÀ NON DISPONIBILE",
            flush=True
        )

        return

    if code == "ALREADY_LISTED":

        print(
            "   └─ Motivo: GIÀ IN VENDITA",
            flush=True
        )

        return

    if code == "PRICE_UNKNOWN":

        print(
            "   ├─ Motivo: "
            "PREZZO LIVE NON DISPONIBILE",
            flush=True
        )

        print(
            f"   └─ Inserzioni minime: "
            f"{info.get('min_live_listings')}",
            flush=True
        )

        return

    if code == "PRICE_LOW":

        print(
            "   ├─ Motivo: FLOOR SOTTO IL MINIMO",
            flush=True
        )

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Range: "
            f"{format_eur(info.get('min_price'))} - "
            f"{format_eur(info.get('max_price'))}",
            flush=True
        )

        return

    if code == "PRICE_HIGH":

        print(
            "   ├─ Motivo: FLOOR SOPRA IL MASSIMO",
            flush=True
        )

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Range: "
            f"{format_eur(info.get('min_price'))} - "
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

        active = (
            info.get(
                "active_competitions"
            )
            or []
        )

        print(
            "   └─ Competizioni: "
            + (
                ", ".join(active)
                if active
                else "nessuna"
            ),
            flush=True
        )

        return

    print(
        f"   └─ Motivo: "
        f"{info.get('message', 'sconosciuto')}",
        flush=True
    )


# ============================================================
# SIGNING
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

function signAuthorization(a) {

    const r = a.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

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
                signature: signature
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
                signature: signature
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
                signature: signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + r.__typename
    );
}

const output =
    input.authorizations.map(
        signAuthorization
    );

process.stdout.write(
    JSON.stringify(output)
);
'''

    process = subprocess.run(
        [node, "-e", script],
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

    return json.loads(
        process.stdout
    )


# ============================================================
# SELL CARD
# ============================================================

def sell_card(card, floor):

    asset_id = str(
        card.get("assetId")
        or ""
    ).strip()

    if not asset_id:
        print(
            "❌ Vendita: assetId mancante",
            flush=True
        )
        return False

    label = card_label(card)

    print(
        f"💰 VENDITA: {label}",
        flush=True
    )

    print(
        f"   ├─ Asset ID: {asset_id}",
        flush=True
    )

    print(
        f"   ├─ Prezzo: {format_eur(floor)}",
        flush=True
    )

    print(
        "   └─ Durata: 7 giorni",
        flush=True
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN → vendita simulata",
            flush=True
        )

        return True

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    data = graphql("""
        mutation PrepareSale(
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
    """, {
        "input": {

            "receiveAssetIds": [
                asset_id
            ],

            "sendAssetIds": [],

            "receiveAmount": {
                "amount": str(floor),
                "currency": "EUR"
            },

            "settlementCurrencies": [
                "EUR"
            ],

            "clientMutationId":
                str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer: "
            "nessun risultato",
            flush=True
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        print(
            "❌ prepareOffer errors:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
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
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )

        return False

    # --------------------------------------------------------
    # SIGN
    # --------------------------------------------------------

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as exc:

        print(
            f"❌ Firma vendita: {exc}",
            flush=True
        )

        return False

    # --------------------------------------------------------
    # CREATE SALE
    # --------------------------------------------------------

    create_input = {

        "assetId": asset_id,

        "receiveAmount": {
            "amount": str(floor),
            "currency": "EUR"
        },

        "settlementCurrencies": [
            "EUR"
        ],

        "duration":
            LISTING_DURATION,

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation CreateSale(
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
        "input": create_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:

        print(
            "❌ createSingleSaleOffer: "
            "nessun risultato",
            flush=True
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        print(
            "❌ createSingleSaleOffer errors:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return False

    token_offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    if not token_offer.get("id"):

        print(
            "❌ Vendita: tokenOffer "
            "non creato",
            flush=True
        )

        return False

    print(
        "✅ VENDITA CREATA",
        flush=True
    )

    print(
        f"   └─ Offer ID: "
        f"{token_offer.get('id')}",
        flush=True
    )

    return True


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(
    card,
    lineup_cards,
    lineup_ok
):

    label = card_label(card)

    print(
        f"\n🔎 CONTROLLO: {label}",
        flush=True
    )

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            f"🔒 {label} → BLOCCATA",
            flush=True
        )

        print(
            "   └─ Motivo: KULENOVIC "
            "MAI IN VENDITA",
            flush=True
        )

        return

    # --------------------------------------------------------
    # COMMON / RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        print(
            f"🚫 {label} → esclusa",
            flush=True
        )

        print(
            f"   └─ Motivo: rarità "
            f"{rarity or 'N/D'}",
            flush=True
        )

        return

    # --------------------------------------------------------
    # ALREADY LISTED
    # --------------------------------------------------------

    if card.get(
        "liveSingleSaleOffer"
    ):

        print(
            f"⏳ {label} → già in vendita",
            flush=True
        )

        return

    # --------------------------------------------------------
    # LINEUP
    # --------------------------------------------------------

    if not lineup_ok:

        print(
            f"🛑 {label} → "
            "controllo lineup non verificabile",
            flush=True
        )

        print(
            "   └─ Sicurezza: NON VENDERE",
            flush=True
        )

        return

    if card_is_in_lineup(
        card,
        lineup_cards
    ):

        print(
            f"🛑 {label} → "
            "CARTA IN LINEUP",
            flush=True
        )

        print(
            "   └─ Sicurezza: NON VENDERE",
            flush=True
        )

        return

    print(
        "✅ Lineup: carta non impegnata",
        flush=True
    )

    # --------------------------------------------------------
    # AGE
    # --------------------------------------------------------

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
        ValueError
    ):

        print(
            f"🛑 {label} → "
            "età non verificabile",
            flush=True
        )

        print(
            "   └─ Sicurezza: NON VENDERE",
            flush=True
        )

        return

    if age >= MAX_AGE:

        print(
            f"🚫 {label} → "
            f"età {age}",
            flush=True
        )

        print(
            f"   └─ Limite: < {MAX_AGE}",
            flush=True
        )

        return

    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:

        print(
            f"🚫 {label} → "
            "floor non verificabile",
            flush=True
        )

        print(
            f"   └─ Servono almeno "
            f"{MIN_LIVE_LISTINGS} "
            "inserzioni live",
            flush=True
        )

        return

    print(
        f"💰 Floor live: "
        f"{format_eur(floor)}",
        flush=True
    )

    if floor < MIN_PRICE:

        print(
            f"🚫 {label} → "
            "floor sotto €0.32",
            flush=True
        )

        return

    if floor > MAX_PRICE:

        print(
            f"🚫 {label} → "
            "floor sopra €0.70",
            flush=True
        )

        return

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    covered, active, covered_competitions = (
        coverage_info(card)
    )

    if not covered:

        print(
            f"🚫 {label} → "
            "nessuna competizione coperta",
            flush=True
        )

        return

    print(
        "🏆 Coverage OK",
        flush=True
    )

    # --------------------------------------------------------
    # FINAL SAFETY CHECK
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            "🛑 BLOCCO FINALE KULENOVIC",
            flush=True
        )

        return

    if card.get(
        "liveSingleSaleOffer"
    ):

        print(
            "🛑 BLOCCO FINALE: "
            "carta già in vendita",
            flush=True
        )

        return

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    print(
        f"🟢 {label} → "
        f"VENDIBILE A {format_eur(floor)}",
        flush=True
    )

    sell_card(
        card,
        floor
    )


# ============================================================
# MAIN AUTOSELL CYCLE
# ============================================================

def run_cycle():

    print(
        "\n================================",
        flush=True
    )

    print(
        "🔄 AUTOSELL - "
        "CONTROLLO GALLERY",
        flush=True
    )

    print(
        "================================",
        flush=True
    )

    # --------------------------------------------------------
    # LINEUP: UNA SOLA QUERY
    # --------------------------------------------------------

    lineup_cards, lineup_ok = (
        load_lineup_cards(
            force=True
        )
    )

    if not lineup_ok:

        print(
            "🛑 CONTROLLO LINEUP "
            "NON VERIFICABILE",
            flush=True
        )

        print(
            "   └─ BLOCCO VENDITE "
            "PER SICUREZZA",
            flush=True
        )

    # --------------------------------------------------------
    # GALLERY
    # --------------------------------------------------------

    gallery = get_gallery()

    print(
        f"📦 Carte in gallery: "
        f"{len(gallery)}",
        flush=True
    )

    if not gallery:

        print(
            "⚠️ Gallery vuota "
            "o non disponibile",
            flush=True
        )

        return

    # --------------------------------------------------------
    # PROCESS
    # --------------------------------------------------------

    for card in gallery:

        try:

            process_card(
                card,
                lineup_cards,
                lineup_ok
            )

        except Exception as exc:

            print(
                f"❌ Errore carta "
                f"{card_label(card)}: "
                f"{exc}",
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
        f"📦 MODULO: INDIPENDENTE",
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
        "💰 RANGE FLOOR: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"📊 INSERZIONI LIVE MINIME: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        "⏱️ DURATA LISTING: 7 giorni",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI IN VENDITA",
        flush=True
    )

    print(
        "🏆 SOLO LIMITED",
        flush=True
    )

    print(
        "🏟️ CARTA IN LINEUP: "
        "MAI IN VENDITA",
        flush=True
    )

    print(
        "🛡️ CONTROLLO LINEUP: "
        "blockchainCardsInLineups",
        flush=True
    )

    print(
        "🛡️ LINEUP NON VERIFICABILE: "
        "BLOCCO VENDITA",
        flush=True
    )

    print(
        "💰 PREZZO = FLOOR LIVE",
        flush=True
    )

    print(
        "🌐 PRICE MATCH: "
        "player + rarity + season",
        flush=True
    )

    print(
        "================================",
        flush=True
    )

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    coverage = load_coverage(
        force=True
    )

    if not coverage:

        print(
            "❌ Coverage non disponibile",
            flush=True
        )

        print(
            "🛑 AutoSell fermato",
            flush=True
        )

        return

    print(
        f"🏆 Competizioni coperte: "
        f"{len(coverage)}",
        flush=True
    )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():

        print(
            "🛑 Account non verificato",
            flush=True
        )

        return

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:

        try:

            run_cycle()

            print(
                f"\n⏳ Prossimo controllo "
                f"tra {INTERVAL} secondi...",
                flush=True
            )

            time.sleep(
                INTERVAL
            )

        except KeyboardInterrupt:

            print(
                "🛑 AutoSell arrestato",
                flush=True
            )

            break

        except Exception as exc:

            print(
                f"❌ Worker AutoSell: "
                f"{exc}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# FLASK HEALTH ENDPOINT
# ============================================================

if app:

    @app.get("/")
    def home():

        with coverage_lock:
            covered_count = len(
                coverage_cache
            )

        with lineup_lock:
            lineup_count = len(
                lineup_cache
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

            "listing_duration_seconds":
                LISTING_DURATION,

            "listing_duration_days":
                7,

            "max_age":
                MAX_AGE,

            "kulenovic":
                "NEVER_SELL",

            "rarity":
                "LIMITED_ONLY",

            "lineup_check":
                "blockchainCardsInLineups",

            "lineup_cards_cached":
                lineup_count,

            "coverage_count":
                covered_count,

            "price_mode":
                "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

            "unknown_price_action":
                "BLOCK_SELL",

            "unknown_lineup_action":
                "BLOCK_SELL"
        })


    @app.get("/health")
    def health():

        with lineup_lock:
            lineup_loaded = (
                lineup_cache_time > 0
            )

        with coverage_lock:
            coverage_loaded = bool(
                coverage_cache
            )

        return jsonify({

            "status": "ok",

            "bot":
                "autosell",

            "version":
                BOT_VERSION,

            "dry_run":
                DRY_RUN,

            "coverage_loaded":
                coverage_loaded,

            "lineup_check_loaded":
                lineup_loaded
        })


# ============================================================
# START
# ============================================================

def start_worker():

    thread = threading.Thread(
        target=worker,
        name="autosell-worker",
        daemon=True
    )

    thread.start()

    print(
        "✅ Thread AutoSell avviato.",
        flush=True
    )


if __name__ == "__main__":

    start_worker()

    if app:

        port = int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

        app.run(
            host="0.0.0.0",
            port=port
        )

    else:

        # fallback
        while True:
            time.sleep(3600)
