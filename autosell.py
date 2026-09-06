import os
import time
import uuid
import json
import hmac
import hashlib
import threading
import requests

from flask import Flask, jsonify


# ============================================================
# AUTOSELL SORARE
# SOLO VENDITA
# NIENTE AUTOBUY
# NIENTE SWAP
# ============================================================

app = Flask(__name__)

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    ""
).strip()

AUD = os.getenv(
    "SORARE_JWT_AUD",
    ""
).strip()

# Chiave privata Sorare/Ethereum esportata dal wallet.
#
# NON INSERIRLA NEL CODICE.
# Deve rimanere esclusivamente nella Environment Variable
# SORARE_STARK_PRIVATE_KEY su Render.
PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# SICUREZZA
# ============================================================

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "true"
    ).lower() == "true"
)


# ============================================================
# PARAMETRI AUTOSELL
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

MIN_LIVE_LISTINGS = 5

LISTING_DURATION = 7 * 24 * 60 * 60

INTERVAL = 15
TIMEOUT = 30

BOT_VERSION = (
    "29.0-AUTOSELL-SOLANA-PYTHON-SIGNER"
)


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

KID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()


# ============================================================
# STATO
# ============================================================

worker_started = False
worker_lock = threading.Lock()


# ============================================================
# UTILITY
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

    rarity = card.get("rarityTyped")
    season = card.get("seasonYear")
    serial = card.get("serialNumber")

    parts = [name]

    if season:
        parts.append(str(season))

    if rarity:
        parts.append(str(rarity))

    if serial:
        parts.append(f"#{serial}")

    return " • ".join(parts)


def format_eur(cents):

    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# AUTH
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
        "User-Agent": (
            f"Sorare-AutoSell/{BOT_VERSION}"
        )
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


# ============================================================
# GRAPHQL
# ============================================================

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
                f"🌐 Sorare HTTP "
                f"{response.status_code}",
                flush=True
            )

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    wait = int(retry_after)
                except ValueError:
                    wait = attempt + 2

                wait = min(wait, 15)

                print(
                    f"⏳ Rate limit → "
                    f"attendo {wait}s",
                    flush=True
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    "❌ HTTP: "
                    + response.text[:1500],
                    flush=True
                )

                time.sleep(attempt + 1)
                continue

            try:

                data = response.json()

            except Exception:

                print(
                    "❌ Risposta non JSON",
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
                f"❌ GraphQL exception: {exc}",
                flush=True
            )

            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql("""
        query CurrentUser {
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
# GALLERY
# ============================================================

def get_gallery():

    all_cards = []

    after = None
    page = 0

    sealed_count = 0
    total_gallery_count = 0

    while True:

        page += 1

        data = graphql("""
            query MyCards(
                $first: Int
                $after: String
            ) {
                currentUser {
                    cards(
                        first: $first
                        after: $after
                        ownedByMe: true
                        sport: FOOTBALL
                    ) {
                        nodes {
                            assetId
                            slug
                            name
                            rarityTyped
                            seasonYear
                            serialNumber
                            sealed

                            anyPlayer {
                                slug
                                displayName
                            }

                            liveSingleSaleOffer {
                                id
                                status
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
            "first": 50,
            "after": after
        })

        if not data:

            print(
                "❌ Gallery: risposta assente",
                flush=True
            )

            return None

        if data.get("errors"):

            print(
                "❌ Gallery: GraphQL error",
                flush=True
            )

            return None

        user = (
            ((data.get("data") or {})
            .get("currentUser"))
            or {}
        )

        cards_data = (
            user.get("cards")
            or {}
        )

        nodes = (
            cards_data.get("nodes")
            or []
        )

        print(
            f"📄 Gallery pagina {page}: "
            f"{len(nodes)} carte",
            flush=True
        )

        total_gallery_count += len(nodes)

        for card in nodes:

            if card.get("sealed") is True:

                sealed_count += 1

                print(
                    f"🔒 {card_label(card)} "
                    f"→ IN CASSAFORTE, esclusa",
                    flush=True
                )

                continue

            all_cards.append(card)

        page_info = (
            cards_data.get("pageInfo")
            or {}
        )

        if not page_info.get("hasNextPage"):
            break

        after = page_info.get("endCursor")

        if not after:
            break

        time.sleep(0.1)

    print(
        f"📦 Carte gallery totali: "
        f"{total_gallery_count}",
        flush=True
    )

    print(
        f"🔒 Carte in Cassaforte escluse: "
        f"{sealed_count}",
        flush=True
    )

    print(
        f"📦 Carte NON sealed disponibili: "
        f"{len(all_cards)}",
        flush=True
    )

    limited_cards = []

    for card in all_cards:

        if norm(
            card.get("rarityTyped")
        ) == "limited":

            limited_cards.append(card)

    print(
        f"🏆 LIMITED DA ANALIZZARE: "
        f"{len(limited_cards)}",
        flush=True
    )

    return limited_cards


# ============================================================
# LINEUP
# ============================================================

def get_lineup_asset_ids():

    data = graphql("""
        query CardsInLineups {
            currentUser {
                blockchainCardsInLineups(
                    sport: FOOTBALL
                )
            }
        }
    """)

    if not data:

        print(
            "🛡️ LINEUP: risposta assente",
            flush=True
        )

        return None

    if data.get("errors"):

        print(
            "🛡️ LINEUP: GraphQL error",
            flush=True
        )

        return None

    user = (
        ((data.get("data") or {})
        .get("currentUser"))
        or {}
    )

    values = user.get(
        "blockchainCardsInLineups"
    )

    if values is None:

        print(
            "🛡️ LINEUP: campo assente",
            flush=True
        )

        return None

    if not isinstance(values, list):

        print(
            "🛡️ LINEUP: formato inatteso",
            flush=True
        )

        return None

    return {
        norm(value)
        for value in values
        if value
    }


# ============================================================
# IDENTIFICATORI CARTA
# ============================================================

def card_lineup_identifiers(card):

    identifiers = set()

    asset_id = norm(
        card.get("assetId")
    )

    if asset_id:
        identifiers.add(asset_id)

    slug = norm(
        card.get("slug")
    )

    if slug:
        identifiers.add(slug)

    return identifiers


def card_in_lineup(card, lineup_ids):

    if lineup_ids is None:
        return None

    identifiers = card_lineup_identifiers(
        card
    )

    if not identifiers:
        return None

    return bool(
        identifiers.intersection(
            lineup_ids
        )
    )


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

    card_identifiers = (
        card_lineup_identifiers(card)
    )

    return bool(
        card_identifiers.intersection(
            wanted
        )
    )


# ============================================================
# USD -> EUR
# ============================================================

usd_rate = None
usd_time = 0

USD_CACHE = 300


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

        data = response.json()

        rate = float(
            (data.get("rates") or {})
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


# ============================================================
# PREZZO
# ============================================================

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
# FLOOR LIVE
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

    if not player_slug:
        return None

    if rarity != "limited":
        return None

    data = graphql("""
        query LiveSales(
            $playerSlug: String
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

        sender_side = (
            offer.get("senderSide")
            or {}
        )

        cards = (
            sender_side.get("anyCards")
            or []
        )

        for market_card in cards:

            market_player = (
                market_card.get("anyPlayer")
                or {}
            )

            market_slug = norm(
                market_player.get("slug")
            )

            market_rarity = norm(
                market_card.get("rarityTyped")
            )

            try:

                market_season = int(
                    market_card.get("seasonYear")
                )

            except (
                TypeError,
                ValueError
            ):

                continue

            if not (
                market_slug == player_slug
                and market_rarity == rarity
                and market_season == season
            ):
                continue

            receiver_side = (
                offer.get("receiverSide")
                or {}
            )

            amounts = (
                receiver_side.get("amounts")
                or {}
            )

            price = price_eur(
                amounts
            )

            if price is not None:
                prices.append(price)

            break

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"   └─ ⚠️ Solo "
            f"{len(prices)}/"
            f"{MIN_LIVE_LISTINGS} "
            f"inserzioni live",
            flush=True
        )

        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
# ============================================================

def validate_card(card, lineup_ids):

    if card.get("sealed") is True:

        return False, {
            "code": "VAULT",
            "message": (
                "carta presente in Cassaforte"
            )
        }

    if is_kulenovic(card):

        return False, {
            "code": "KULENOVIC",
            "message": (
                "KULENOVIC MAI IN VENDITA"
            )
        }

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        return False, {
            "code": "RARITY",
            "message": (
                "rarità diversa da LIMITED"
            ),
            "rarity": rarity or "N/D"
        }

    in_lineup = card_in_lineup(
        card,
        lineup_ids
    )

    if in_lineup is None:

        return False, {
            "code": "LINEUP_UNKNOWN",
            "message": (
                "impossibile verificare "
                "la lineup"
            )
        }

    if in_lineup:

        return False, {
            "code": "LINEUP",
            "message": (
                "carta presente in lineup"
            )
        }

    floor = live_floor(card)

    if floor is None:

        return False, {
            "code": "PRICE_UNKNOWN",
            "message": (
                "meno di "
                f"{MIN_LIVE_LISTINGS} "
                "inserzioni live valide"
            )
        }

    if floor < MIN_PRICE:

        return False, {
            "code": "PRICE_LOW",
            "message": (
                "floor sotto il minimo"
            ),
            "floor": floor
        }

    if floor > MAX_PRICE:

        return False, {
            "code": "PRICE_HIGH",
            "message": (
                "floor sopra il massimo"
            ),
            "floor": floor
        }

    return True, {
        "rarity": rarity,
        "floor": floor
    }


# ============================================================
# LOG ESCLUSIONE
# ============================================================

def print_rejection(card, info):

    label = card_label(card)

    print(
        f"🚫 {label} → esclusa",
        flush=True
    )

    if not info:

        print(
            "   └─ Motivo: verifica fallita",
            flush=True
        )

        return

    code = info.get("code")

    if code == "VAULT":

        print(
            "   └─ Motivo: CARTA IN CASSAFORTE",
            flush=True
        )

        print(
            "      Sicurezza: NON VENDERE",
            flush=True
        )

    elif code == "KULENOVIC":

        print(
            "   └─ Motivo: "
            "KULENOVIC MAI IN VENDITA",
            flush=True
        )

    elif code == "RARITY":

        print(
            f"   └─ Motivo: rarità "
            f"{info.get('rarity')}",
            flush=True
        )

    elif code == "LINEUP":

        print(
            "   └─ Motivo: CARTA IN LINEUP",
            flush=True
        )

        print(
            "      Sicurezza: NON VENDERE",
            flush=True
        )

    elif code == "LINEUP_UNKNOWN":

        print(
            "   └─ Motivo: controllo lineup "
            "non verificabile",
            flush=True
        )

        print(
            "      Sicurezza: NON VENDERE",
            flush=True
        )

    elif code == "PRICE_UNKNOWN":

        print(
            "   └─ Motivo: prezzo live "
            "non verificabile",
            flush=True
        )

    elif code == "PRICE_LOW":

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Minimo: "
            f"{format_eur(MIN_PRICE)}",
            flush=True
        )

    elif code == "PRICE_HIGH":

        print(
            f"   ├─ Floor: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Massimo: "
            f"{format_eur(MAX_PRICE)}",
            flush=True
        )

    else:

        print(
            f"   └─ Motivo: "
            f"{info.get('message', 'sconosciuto')}",
            flush=True
        )


# ============================================================
# BASE58
# ============================================================

BASE58_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)


def base58_encode(data):

    if not data:
        return ""

    number = int.from_bytes(
        data,
        "big"
    )

    chars = []

    while number > 0:

        number, remainder = divmod(
            number,
            58
        )

        chars.append(
            BASE58_ALPHABET[remainder]
        )

    leading_zeroes = 0

    for byte in data:

        if byte == 0:
            leading_zeroes += 1
        else:
            break

    return (
        "1" * leading_zeroes
        + "".join(reversed(chars))
    )


# ============================================================
# ED25519
#
# Implementazione minima Ed25519.
#
# Viene usata esclusivamente per firmare il digest SHA-256
# richiesto dalla Solana authorization di Sorare.
# ============================================================

ED25519_Q = (
    2 ** 255 - 19
)

ED25519_L = (
    2 ** 252
    + 27742317777372353535851937790883648493
)

ED25519_D = (
    -121665
    * pow(
        121666,
        ED25519_Q - 2,
        ED25519_Q
    )
) % ED25519_Q

ED25519_I = pow(
    2,
    (ED25519_Q - 1) // 4,
    ED25519_Q
)


def ed25519_xrecover(y):

    xx = (
        (y * y - 1)
        * pow(
            ED25519_D * y * y + 1,
            ED25519_Q - 2,
            ED25519_Q
        )
    ) % ED25519_Q

    x = pow(
        xx,
        (ED25519_Q + 3) // 8,
        ED25519_Q
    )

    if (
        x * x - xx
    ) % ED25519_Q != 0:

        x = (
            x * ED25519_I
        ) % ED25519_Q

    if x & 1:
        x = ED25519_Q - x

    return x


ED25519_BY = (
    4
    * pow(
        5,
        ED25519_Q - 2,
        ED25519_Q
    )
) % ED25519_Q

ED25519_BX = ed25519_xrecover(
    ED25519_BY
)

ED25519_B = (
    ED25519_BX,
    ED25519_BY
)


def ed25519_add(P, Q):

    x1, y1 = P
    x2, y2 = Q

    denominator_x = (
        1
        + ED25519_D * x1 * x2 * y1 * y2
    )

    denominator_y = (
        1
        - ED25519_D * x1 * x2 * y1 * y2
    )

    x3 = (
        (x1 * y2 + x2 * y1)
        * pow(
            denominator_x,
            ED25519_Q - 2,
            ED25519_Q
        )
    ) % ED25519_Q

    y3 = (
        (y1 * y2 + x1 * x2)
        * pow(
            denominator_y,
            ED25519_Q - 2,
            ED25519_Q
        )
    ) % ED25519_Q

    return x3, y3


def ed25519_scalarmult(P, e):

    if e == 0:
        return (
            0,
            1
        )

    if e == 1:
        return P

    Q = ed25519_scalarmult(
        P,
        e // 2
    )

    Q = ed25519_add(
        Q,
        Q
    )

    if e & 1:
        Q = ed25519_add(
            Q,
            P
        )

    return Q


def ed25519_encodepoint(P):

    x, y = P

    value = (
        y
        | ((x & 1) << 255)
    )

    return value.to_bytes(
        32,
        "little"
    )


def ed25519_clamp_scalar(h):

    scalar = bytearray(
        h[:32]
    )

    scalar[0] &= 248
    scalar[31] &= 63
    scalar[31] |= 64

    return int.from_bytes(
        scalar,
        "little"
    )


def ed25519_public_key(seed):

    if len(seed) != 32:
        raise ValueError(
            "Seed Ed25519 deve essere di 32 byte"
        )

    h = hashlib.sha512(seed).digest()

    a = ed25519_clamp_scalar(h)

    A = ed25519_scalarmult(
        ED25519_B,
        a
    )

    return ed25519_encodepoint(A)


def ed25519_sign(seed, message):

    if len(seed) != 32:
        raise ValueError(
            "Seed Ed25519 deve essere di 32 byte"
        )

    h = hashlib.sha512(seed).digest()

    a = ed25519_clamp_scalar(h)

    prefix = h[32:]

    public_key = ed25519_encodepoint(
        ed25519_scalarmult(
            ED25519_B,
            a
        )
    )

    r_digest = hashlib.sha512(
        prefix + message
    ).digest()

    r = (
        int.from_bytes(
            r_digest,
            "little"
        )
        % ED25519_L
    )

    R = ed25519_encodepoint(
        ed25519_scalarmult(
            ED25519_B,
            r
        )
    )

    k_digest = hashlib.sha512(
        R
        + public_key
        + message
    ).digest()

    k = (
        int.from_bytes(
            k_digest,
            "little"
        )
        % ED25519_L
    )

    S = (
        r
        + k * a
    ) % ED25519_L

    return (
        R
        + S.to_bytes(
            32,
            "little"
        )
    )


# ============================================================
# SLIP-0010 ED25519
# ============================================================

def slip10_master_key(seed):

    I = hmac.new(
        b"ed25519 seed",
        seed,
        hashlib.sha512
    ).digest()

    return (
        I[:32],
        I[32:]
    )


def slip10_child(
    key,
    chain_code,
    index
):

    # Ed25519 SLIP-0010 usa esclusivamente
    # derivazione hardened.

    data = (
        b"\x00"
        + key
        + index.to_bytes(
            4,
            "big"
        )
    )

    I = hmac.new(
        chain_code,
        data,
        hashlib.sha512
    ).digest()

    return (
        I[:32],
        I[32:]
    )


def derive_solana_seed_from_sorare_key(
    ethereum_private_key
):

    private_key = (
        ethereum_private_key
        .strip()
        .lower()
    )

    if private_key.startswith("0x"):
        private_key = private_key[2:]

    if len(private_key) != 64:

        raise ValueError(
            "SORARE_STARK_PRIVATE_KEY "
            "deve contenere 32 byte / 64 caratteri hex"
        )

    try:

        master_seed = bytes.fromhex(
            private_key
        )

    except ValueError:

        raise ValueError(
            "SORARE_STARK_PRIVATE_KEY "
            "non è una chiave hex valida"
        )

    key, chain = slip10_master_key(
        master_seed
    )

    # m/44'/501'/0'/0'
    path = [
        44,
        501,
        0,
        0
    ]

    for component in path:

        hardened_index = (
            component
            | 0x80000000
        )

        key, chain = slip10_child(
            key,
            chain,
            hardened_index
        )

    return key


# ============================================================
# SOLANA ADDRESS
# ============================================================

def solana_address_from_seed(seed):

    public_key = ed25519_public_key(
        seed
    )

    return base58_encode(
        public_key
    )


# ============================================================
# FIRMA SOLANA AUTHORIZATION
#
# Sorare:
#
# TRANSFER:
# transferProxyProgramAddress:
# merkleTreeAddress:
# leafIndex:
# nonce:
# expirationTimestamp:
# receiverAddress:
# 0x:
# originator
#
# poi UTF-8 -> SHA256 -> Ed25519 -> Base58
# ============================================================

def sign_solana_authorization(
    authorization
):

    request = (
        authorization.get("request")
        or {}
    )

    typename = request.get(
        "__typename"
    )

    if typename != (
        "SolanaTokenTransferAuthorizationRequest"
    ):

        raise RuntimeError(
            "Authorization non Solana: "
            + str(typename)
        )

    if not PRIVATE_KEY:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    required_fields = [
        "assetId",
        "leafIndex",
        "merkleTreeAddress",
        "originator",
        "receiverAddress",
        "senderAddress",
        "expirationTimestamp",
        "nonce",
        "transferProxyProgramAddress"
    ]

    missing = [
        field
        for field in required_fields
        if request.get(field) is None
    ]

    if missing:

        raise RuntimeError(
            "Authorization Solana incompleta. "
            "Campi mancanti: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # DERIVAZIONE CHIAVE
    # --------------------------------------------------------

    solana_seed = (
        derive_solana_seed_from_sorare_key(
            PRIVATE_KEY
        )
    )

    derived_address = (
        solana_address_from_seed(
            solana_seed
        )
    )

    expected_address = str(
        request.get("senderAddress")
        or ""
    ).strip()

    print(
        "🟣 SOLANA ADDRESS:",
        flush=True
    )

    print(
        f"   ├─ Derivata: {derived_address}",
        flush=True
    )

    print(
        f"   └─ Sorare:   {expected_address}",
        flush=True
    )

    if derived_address != expected_address:

        raise RuntimeError(
            "Solana senderAddress mismatch. "
            "La chiave privata configurata non "
            "corrisponde al senderAddress restituito "
            "da Sorare."
        )

    # --------------------------------------------------------
    # MESSAGE UFFICIALE SORARE
    # --------------------------------------------------------

    message = ":".join([
        "TRANSFER",
        str(
            request[
                "transferProxyProgramAddress"
            ]
        ),
        str(
            request[
                "merkleTreeAddress"
            ]
        ),
        str(
            request[
                "leafIndex"
            ]
        ),
        str(
            request[
                "nonce"
            ]
        ),
        str(
            request[
                "expirationTimestamp"
            ]
        ),
        str(
            request[
                "receiverAddress"
            ]
        ),
        "0x",
        str(
            request[
                "originator"
            ]
        )
    ])

    message_bytes = message.encode(
        "utf-8"
    )

    # --------------------------------------------------------
    # SHA-256
    # --------------------------------------------------------

    message_hash = hashlib.sha256(
        message_bytes
    ).digest()

    # --------------------------------------------------------
    # ED25519
    # --------------------------------------------------------

    signature_bytes = ed25519_sign(
        solana_seed,
        message_hash
    )

    # --------------------------------------------------------
    # BASE58
    # --------------------------------------------------------

    signature = base58_encode(
        signature_bytes
    )

    # --------------------------------------------------------
    # APPROVAL
    # --------------------------------------------------------

    approval = {

        "fingerprint":
            authorization.get(
                "fingerprint"
            ),

        "solanaTokenTransferApproval": {

            "signature":
                signature,

            "nonce":
                request["nonce"],

            "expirationTimestamp":
                request[
                    "expirationTimestamp"
                ]
        }
    }

    return approval


# ============================================================
# FIRMA AUTHORIZATIONS
# ============================================================

def sign_authorizations(
    authorizations
):

    approvals = []

    for index, authorization in enumerate(
        authorizations
    ):

        request = (
            authorization.get("request")
            or {}
        )

        typename = request.get(
            "__typename"
        )

        print(
            f"🔐 Authorization {index}: "
            f"{typename}",
            flush=True
        )

        if typename == (
            "SolanaTokenTransferAuthorizationRequest"
        ):

            approval = (
                sign_solana_authorization(
                    authorization
                )
            )

            approvals.append(
                approval
            )

            print(
                "   └─ ✅ Solana authorization "
                "firmata",
                flush=True
            )

        else:

            raise RuntimeError(
                "Authorization non supportata "
                "dal modulo AutoSell: "
                + str(typename)
            )

    return approvals


# ============================================================
# PREPARE SALE
# ============================================================

def prepare_sale(card, price):

    asset_id = str(
        card.get("assetId")
        or ""
    ).strip()

    if not asset_id:

        print(
            "❌ AssetId mancante",
            flush=True
        )

        return None

    # ========================================================
    # IMPORTANTE
    #
    # Il tuo endpoint ha risposto:
    #
    # Field is not defined on prepareOfferInput
    #
    # per il campo "type".
    #
    # Quindi NON lo inviamo.
    #
    # ========================================================

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

                        ... on SolanaBankTransferAuthorizationRequest {
                            amount
                            nonce
                            expirationTimestamp
                            receiverAddress
                            senderAddress
                        }

                        ... on EthereumBankTransferAuthorizationRequest {
                            amount
                            nonce
                            expirationTimestamp
                            receiverAddress
                            senderAddress
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

            "sendAssetIds": [
                asset_id
            ],

            "receiveAssetIds": [],

            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },

            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer: nessun risultato",
            flush=True
        )

        return None

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        print(
            "❌ prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
