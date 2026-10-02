import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify

# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

SORARE_URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
SOLANA = os.getenv("SORARE_SOLANA_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

# ============================================================
# VENDITA
# ============================================================

MAX_PRICE_CENTS = 70
MIN_SELL_PRICE_CENTS = 30
MIN_LISTINGS = 5

TECHNICAL_START_ETH = 0.0002
TECHNICAL_STEP_ETH = 0.0001

# 7 giorni esatti
SALE_DURATION_SECONDS = 7 * 24 * 60 * 60

# ============================================================
# SOURCE
# ============================================================

COVERAGE_ENABLED = False
SOURCE_LABEL = "AUTOBUY / SWAP"

ALLOWED_SOURCES = {"AUTOBUY", "SWAP"}

# ============================================================
# STATE
# ============================================================

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

VERSION = "AUTOSell-15.0-EUR-7DAYS-RENEWAL"

# ============================================================
# PROTEZIONE KULENOVIC
# ============================================================

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# THREAD
# ============================================================

state_lock = threading.RLock()
worker_lock = threading.Lock()
worker_started = False

# ============================================================
# SOLANA BASE58
# ============================================================

SOLANA_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def now():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def new_id():
    return str(uuid.uuid4())


def asset_id(card):
    return str(
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()


def label(card):
    return (
        card.get("name")
        or card.get("slug")
        or asset_id(card)
        or "Carta"
    )


def eur(cents):
    if cents is None:
        return "N/D"

    try:
        return f"€{int(cents) / 100:.2f}"
    except Exception:
        return "N/D"


def parse_timestamp(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    value = str(value).strip()

    if not value:
        return None

    try:
        numeric = float(value)

        if numeric > 10_000_000_000:
            numeric /= 1000

        return numeric

    except Exception:
        pass

    try:
        from datetime import datetime

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        return datetime.fromisoformat(value).timestamp()

    except Exception:
        return None


def is_expired(end_date):
    timestamp = parse_timestamp(end_date)

    return (
        timestamp is not None
        and time.time() >= timestamp
    )


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "processed_offers": [],
        "acquired_cards": [],
        "pending_autobuys": [],
        "updated_at": int(time.time())
    }


def ensure_state():
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        save_document(default_state())


def load_document():
    ensure_state()

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_state()

        for key, default in (
            ("processed_offers", []),
            ("acquired_cards", []),
            ("pending_autobuys", []),
            ("updated_at", int(time.time()))
        ):
            data.setdefault(key, default)

        return data

    except Exception as e:
        print(
            f"❌ Errore lettura state: {e}",
            flush=True
        )
        return default_state()


def save_document(data):
    tmp = f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"

    try:
        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print(
            f"❌ Errore scrittura state: {e}",
            flush=True
        )

        try:
            os.remove(tmp)
        except Exception:
            pass

        return False


def get_cards():
    with state_lock:
        cards = load_document().get(
            "acquired_cards",
            []
        )

        return (
            cards
            if isinstance(cards, list)
            else []
        )


def update_card(
    asset,
    status=None,
    offer_id=None,
    error=None,
    sale_price_cents=None,
    sale_offer_end_date=None
):
    wanted = norm(asset)

    with state_lock:
        data = load_document()
        cards = data.get("acquired_cards", [])

        for card in cards:
            if not isinstance(card, dict):
                continue

            if norm(asset_id(card)) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = offer_id

            if sale_price_cents is not None:
                card["sale_price_cents"] = int(
                    sale_price_cents
                )

            if sale_offer_end_date is not None:
                card["sale_offer_end_date"] = (
                    sale_offer_end_date
                )

            card["last_error"] = error

            if norm(status) == "selling":
                card["selling_at"] = (
                    card.get("selling_at")
                    or now()
                )

            if norm(status) == "not_owned":
                card["not_owned_at"] = (
                    card.get("not_owned_at")
                    or now()
                )

            data["acquired_cards"] = cards
            data["updated_at"] = int(time.time())

            return save_document(data)

        print(
            f"⚠️ Carta non presente nello state: {asset}",
            flush=True
        )

        return False


def update_card_source(asset, source):
    wanted = norm(asset)

    with state_lock:
        data = load_document()
        cards = data.get("acquired_cards", [])

        for card in cards:
            if (
                isinstance(card, dict)
                and norm(asset_id(card)) == wanted
            ):
                card["source"] = source
                data["updated_at"] = int(time.time())
                return save_document(data)

    return False


def sellable_cards():
    return [
        dict(card)
        for card in get_cards()
        if (
            isinstance(card, dict)
            and norm(card.get("status"))
            in {"da_vendere", "ready"}
            and asset_id(card)
        )
    ]


def selling_cards():
    return [
        dict(card)
        for card in get_cards()
        if (
            isinstance(card, dict)
            and norm(card.get("status")) == "selling"
            and asset_id(card)
        )
    ]


# ============================================================
# ERRORI
# ============================================================

def is_not_owned_error(error_text):
    text = norm(error_text)

    return (
        "is not owned by" in text
        and "on solana" in text
    )


def is_technical_price_error(error_text):
    text = norm(error_text)

    return any(
        phrase in text
        for phrase in (
            "price must be greater than",
            "price must be at least",
            "minimum price",
            "min price"
        )
    )


# ============================================================
# SORARE GRAPHQL
# ============================================================

def auth_headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    headers = {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


def gql(query, variables=None):
    for attempt in range(3):
        try:
            response = requests.post(
                SORARE_URL,
                headers=auth_headers(),
                json={
                    "query": query,
                    "variables": variables or {}
                },
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True
            )

            if response.status_code == 429:
                try:
                    delay = float(
                        response.headers.get(
                            "Retry-After"
                        )
                    )
                except Exception:
                    delay = 2 + attempt * 2

                delay = max(1, min(delay, 60))

                print(
                    f"⏳ HTTP 429 → attendo {delay:.1f}s",
                    flush=True
                )

                time.sleep(delay)
                continue

            if response.status_code != 200:
                print(
                    f"❌ Sorare: {response.text[:1500]}",
                    flush=True
                )
                time.sleep(attempt + 1)
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
            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    data = gql("""
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
        + str(
            user.get("nickname")
            or user.get("slug")
        ),
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

    print(
        "🔑 Solana private key: "
        + (
            "PRESENTE"
            if SOLANA
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    return True


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset):
    data = gql("""
        query Cards($ids: [String!]!) {
            anyCards(assetIds: $ids) {
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
                    }
                }
            }
        }
    """, {"ids": [asset]})

    cards = (
        ((data or {}).get("data") or {})
        .get("anyCards")
        or []
    )

    return cards[0] if cards else None


# ============================================================
# ACTIVE PUBLIC OFFER
# ============================================================

def find_active_public_offer(asset):
    wanted = norm(asset)

    data = gql("""
        query ActivePublicOffer($assetId: String!) {
            anyCards(assetIds: [$assetId]) {
                assetId

                liveSingleSaleOffer {
                    id
                    startDate
                    endDate

                    receiverSide {
                        amounts {
                            eurCents
                            usdCents
                        }
                    }
                }
            }
        }
    """, {"assetId": asset})

    if data:
        errors = data.get("errors") or []

        if not errors:
            cards = (
                ((data.get("data") or {})
                .get("anyCards"))
                or []
            )

            for card in cards:
                if (
                    not isinstance(card, dict)
                    or norm(card.get("assetId")) != wanted
                ):
                    continue

                offer = (
                    card.get("liveSingleSaleOffer")
                    or {}
                )

                offer_id = offer.get("id")

                if offer_id:
                    price = amount_to_eur(
                        (
                            offer.get("receiverSide")
                            or {}
                        ).get("amounts")
                    )

                    print(
                        "🟢 PRE-CHECK → CARTA GIÀ IN VENDITA",
                        flush=True
                    )

                    print(
                        f"🟢 OFFER → {offer_id}",
                        flush=True
                    )

                    return {
                        "id": offer_id,
                        "endDate": offer.get("endDate"),
                        "price": price
                    }

        else:
            print(
                "⚠️ Pre-check mirato non disponibile:",
                json.dumps(
                    errors,
                    ensure_ascii=False
                )[:1500],
                flush=True
            )

    data = gql("""
        query ActivePublicOffers($first: Int) {
            tokens {
                liveSingleSaleOffers(first: $first) {
                    nodes {
                        id
                        startDate
                        endDate

                        senderSide {
                            anyCards {
                                assetId
                            }
                        }

                        receiverSide {
                            amounts {
                                eurCents
                                usdCents
                            }
                        }
                    }
                }
            }
        }
    """, {"first": 100})

    if not data:
        print(
            "⚠️ Pre-check offerte: nessuna risposta",
            flush=True
        )
        return None

    errors = data.get("errors") or []

    if errors:
        print(
            "⚠️ Pre-check fallback:",
            json.dumps(
                errors,
                ensure_ascii=False
            )[:1500],
            flush=True
        )
        return None

    nodes = (
        (((data.get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes")
        or []
    )

    for offer in nodes:
        if not isinstance(offer, dict):
            continue

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for listed_card in cards:
            if (
                isinstance(listed_card, dict)
                and norm(listed_card.get("assetId")) == wanted
            ):
                offer_id = offer.get("id")

                price = amount_to_eur(
                    (
                        offer.get("receiverSide")
                        or {}
                    ).get("amounts")
                )

                print(
                    "🟢 PRE-CHECK → CARTA GIÀ IN VENDITA",
                    flush=True
                )

                print(
                    f"🟢 OFFER → {offer_id}",
                    flush=True
                )

                return {
                    "id": offer_id,
                    "endDate": offer.get("endDate"),
                    "price": price
                }

    print(
        "🔵 PRE-CHECK → nessuna offerta pubblica attiva",
        flush=True
    )

    return None


# ============================================================
# EUR / FLOOR
# ============================================================

def usd_to_eur(usd_cents):
    try:
        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR"
            },
            timeout=10
        )

        rate = float(
            response.json()["rates"]["EUR"]
        )

        return round(usd_cents * rate)

    except Exception:
        return None


def amount_to_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        value = int(
            amounts.get("eurCents") or 0
        )

        if value > 0:
            return value

    except Exception:
        pass

    try:
        value = int(
            amounts.get("usdCents") or 0
        )

        if value > 0:
            return usd_to_eur(value)

    except Exception:
        pass

    return None


def get_floor(card):
    player = card.get("anyPlayer") or {}
    player_slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    if not player_slug or not rarity:
        return None

    data = gql("""
        query LiveOffers($slug: String, $first: Int) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug: $slug
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
                            }
                        }
                    }
                }
            }
        }
    """, {
        "slug": player_slug,
        "first": 50
    })

    nodes = (
        (((data or {}).get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in nodes:
        sender = offer.get("senderSide") or {}

        for listed in sender.get("anyCards") or []:
            listed_player = (
                listed.get("anyPlayer")
                or {}
            )

            try:
                same_season = (
                    int(listed.get("seasonYear"))
                    == season
                )
            except Exception:
                continue

            if not same_season:
                continue

            if norm(listed_player.get("slug")) != player_slug:
                continue

            if norm(listed.get("rarityTyped")) != rarity:
                continue

            price = amount_to_eur(
                (
                    offer.get("receiverSide")
                    or {}
                ).get("amounts")
            )

            if price is not None:
                prices.append(price)

            break

    print(
        f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return (
        min(prices)
        if len(prices) >= MIN_LISTINGS
        else None
    )


# ============================================================
# ETH / EUR
# ============================================================

def get_eth_eur_rate():
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "ethereum",
                "vs_currencies": "eur"
            },
            timeout=10
        )

        response.raise_for_status()

        rate = float(
            response.json()["ethereum"]["eur"]
        )

        if rate <= 0:
            raise ValueError(
                "Cambio ETH/EUR non valido"
            )

        print(
            f"💱 ETH/EUR → €{rate:.2f}",
            flush=True
        )

        return rate

    except Exception as e:
        print(
            f"❌ Impossibile ottenere ETH/EUR: {e}",
            flush=True
        )
        return None


def eth_to_eur_cents(eth_amount, eth_eur_rate):
    try:
        return max(
            1,
            round(
                float(eth_amount)
                * float(eth_eur_rate)
                * 100
            )
        )
    except Exception:
        return None


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):
    return (
        norm(card.get("slug")) == norm(KULENOVIC_SLUG)
        or norm(card.get("assetId")) == norm(KULENOVIC_ASSET)
    )


def is_sealed(card):
    rarity = norm(card.get("rarityTyped"))
    name = norm(card.get("name"))
    slug = norm(card.get("slug"))

    return (
        rarity == "sealed"
        or "sealed" in name
        or "sealed" in slug
    )


def validate(card):
    if is_kulenovic(card):
        return False, "KULENOVIC"

    if is_sealed(card):
        return False, "SEALED"

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor > MAX_PRICE_CENTS:
        return False, "FLOOR_HIGH"

    return True, max(
        floor,
        MIN_SELL_PRICE_CENTS
    )


# ============================================================
# BASE58
# ============================================================

def base58_decode(value):
    value = str(value).strip()

    if not value:
        raise ValueError("Base58 vuoto")

    number = 0

    for char in value:
        index = SOLANA_ALPHABET.find(char)

        if index < 0:
            raise ValueError(
                f"Carattere Base58 non valido: {char}"
            )

        number = number * 58 + index

    raw = (
        b""
        if number == 0
        else number.to_bytes(
            max(
                1,
                (number.bit_length() + 7) // 8
            ),
            "big"
        )
    )

    zeros = 0

    for char in value:
        if char != "1":
            break
        zeros += 1

    return b"\x00" * zeros + raw


# ============================================================
# SOLANA KEY CHECK
# ============================================================

def solana_key_info():
    if not SOLANA:
        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY mancante"
        )

    value = SOLANA.strip()

    try:
        decoded = base58_decode(value)

        if len(decoded) in {32, 64}:
            return {
                "format": "base58",
                "bytes": decoded
            }
    except Exception:
        pass

    hex_value = (
        value[2:]
        if value.startswith("0x")
        else value
    )

    if (
        len(hex_value) % 2 == 0
        and all(
            c in "0123456789abcdefABCDEF"
            for c in hex_value
        )
    ):
        decoded = bytes.fromhex(hex_value)

        if len(decoded) in {32, 64}:
            return {
                "format": "hex",
                "bytes": decoded
            }

    raise RuntimeError(
        "SORARE_SOLANA_PRIVATE_KEY non valida. "
        "Atteso Base58 o HEX da 32/64 byte."
    )


# ============================================================
# SIGN AUTHORIZATIONS
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

    types = [
        (
            a.get("request") or {}
        ).get("__typename")
        for a in authorizations
    ]

    requires_stark = any(
        t != "SolanaTokenTransferAuthorizationRequest"
        for t in types
    )

    requires_solana = any(
        t == "SolanaTokenTransferAuthorizationRequest"
        for t in types
    )

    if requires_stark and not STARK:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY mancante "
            "per authorization Stark/Mangopay"
        )

    if requires_solana and not SOLANA:
        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY mancante"
        )

    js = r'''
const crypto = require("crypto");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const {
    createSignableMessage,
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair
} = require("@solana/kit");

const input = JSON.parse(
    require("fs").readFileSync(0, "utf8")
);

const ALPHABET =
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
    "abcdefghijkmnopqrstuvwxyz";

function b58decode(value) {
    let num = 0n;

    for (const char of String(value).trim()) {
        const i = ALPHABET.indexOf(char);

        if (i < 0) {
            throw new Error("Base58 non valido");
        }

        num = num * 58n + BigInt(i);
    }

    let bytes;

    if (num === 0n) {
        bytes = Buffer.alloc(0);
    } else {
        let hex = num.toString(16);

        if (hex.length % 2) {
            hex = "0" + hex;
        }

        bytes = Buffer.from(hex, "hex");
    }

    let zeros = 0;

    for (const char of String(value).trim()) {
        if (char !== "1") break;
        zeros++;
    }

    return new Uint8Array(
        Buffer.concat([
            Buffer.alloc(zeros),
            bytes
        ])
    );
}

function b58encode(data) {
    const bytes = Buffer.from(data);
    let num = 0n;

    for (const byte of bytes) {
        num = num * 256n + BigInt(byte);
    }

    let result = "";

    while (num > 0n) {
        const r = Number(num % 58n);
        result = ALPHABET[r] + result;
        num /= 58n;
    }

    let zeros = 0;

    for (const byte of bytes) {
        if (byte !== 0) break;
        zeros++;
    }

    return "1".repeat(zeros) + result;
}

function parsePrivateKey(value) {
    const clean = String(value || "").trim();

    try {
        const decoded = b58decode(clean);

        if (
            decoded.length === 32 ||
            decoded.length === 64
        ) {
            return decoded;
        }
    } catch (_) {}

    let hex = clean;

    if (hex.startsWith("0x")) {
        hex = hex.slice(2);
    }

    if (
        /^[0-9a-fA-F]+$/.test(hex) &&
        hex.length % 2 === 0
    ) {
        const decoded = new Uint8Array(
            Buffer.from(hex, "hex")
        );

        if (
            decoded.length === 32 ||
            decoded.length === 64
        ) {
            return decoded;
        }
    }

    throw new Error(
        "Chiave Solana non riconosciuta"
    );
}

async function createSolanaSigner(privateKey) {
    let keyBytes = parsePrivateKey(privateKey);

    if (keyBytes.length === 64) {
        keyBytes = keyBytes.slice(0, 32);
    }

    if (keyBytes.length !== 32) {
        throw new Error(
            "Private key Solana non valida: " +
            keyBytes.length +
            " byte"
        );
    }

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            keyBytes
        );

    return createSignerFromKeyPair(keyPair);
}

async function signSolana(auth) {
    const req = auth.request;

    const signer =
        await createSolanaSigner(
            input.solanaPrivateKey
        );

    console.error(
        "🔑 Solana signer → " +
        signer.address
    );

    console.error(
        "🎯 senderAddress → " +
        req.senderAddress
    );

    if (signer.address !== req.senderAddress) {
        throw new Error(
            "La chiave Solana NON corrisponde " +
            "al senderAddress. Derivato=" +
            signer.address +
            " richiesto=" +
            req.senderAddress
        );
    }

    const message = [
        "TRANSFER",
        req.transferProxyProgramAddress,
        req.merkleTreeAddress,
        req.leafIndex.toString(),
        req.nonce,
        req.expirationTimestamp.toString(),
        req.receiverAddress,
        "0x",
        req.originator
    ].join(":");

    const hash = crypto
        .createHash("sha256")
        .update(Buffer.from(message, "utf8"))
        .digest();

    const signable =
        createSignableMessage(
            new Uint8Array(hash)
        );

    const signatures =
        await signer.signMessages([
            signable
        ]);

    const signatureBytes =
        signatures[0][signer.address];

    return {
        fingerprint: auth.fingerprint,

        solanaTokenTransferApproval: {
            signature: b58encode(signatureBytes),
            nonce: req.nonce,
            expirationTimestamp:
                req.expirationTimestamp
        }
    };
}

function signStark(auth) {
    const req = auth.request;

    const signature =
        signAuthorizationRequest(
            input.starkPrivateKey,
            req
        );

    const base = {
        fingerprint: auth.fingerprint
    };

    if (
        req.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {
        return {
            ...base,

            starkexTransferApproval: {
                nonce: req.nonce,
                expirationTimestamp:
                    req.expirationTimestamp,
                signature
            }
        };
    }

    if (
        req.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {
        return {
            ...base,

            starkexLimitOrderApproval: {
                nonce: req.nonce,
                expirationTimestamp:
                    req.expirationTimestamp,
                signature
            }
        };
    }

    if (
        req.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {
        return {
            ...base,

            mangopayWalletTransferApproval: {
                nonce: req.nonce,
                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: " +
        req.__typename
    );
}

async function main() {
    const approvals = [];

    for (const auth of input.authorizations) {
        const type =
            (auth.request || {}).__typename;

        console.error(
            "🔐 Authorization → " + type
        );

        approvals.push(
            type ===
            "SolanaTokenTransferAuthorizationRequest"
                ? await signSolana(auth)
                : signStark(auth)
        );
    }

    process.stdout.write(
        JSON.stringify(approvals)
    );
}

main().catch(error => {
    console.error(error.stack || error);
    process.exit(1);
});
'''

    process = subprocess.run(
        [node, "-e", js],
        input=json.dumps({
            "starkPrivateKey": STARK,
            "solanaPrivateKey": SOLANA,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if process.stderr:
        print(
            process.stderr.strip(),
            flush=True
        )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Firma fallita"
        )

    try:
        return json.loads(process.stdout)
    except Exception:
        raise RuntimeError(
            "Output firma non valido: "
            + process.stdout[:1000]
        )


# ============================================================
# PREPARE OFFER
# ============================================================

PREPARE_QUERY = """
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
            }
        }

        errors {
            message
        }
    }
}
"""


def prepare_sale(asset, price):
    input_data = {
        "sendAssetIds": [asset],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    print(
        f"📦 prepareOffer → EUR {eur(price)}",
        flush=True
    )

    data = gql(
        PREPARE_QUERY,
        {"input": input_data}
    )

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

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:
        print(
            "❌ prepareOffer: nessuna authorization",
            flush=True
        )
        return None

    print(
        f"✅ Authorization ricevute: {len(authorizations)}",
        flush=True
    )

    return authorizations


# ============================================================
# CREATE SINGLE SALE
# ============================================================

def create_sale_once(card, price):
    asset = asset_id(card)

    if not asset:
        return None, None, None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → {label(card)} → {eur(price)}",
            flush=True
        )
        return "DRY-RUN", None, None

    authorizations = prepare_sale(
        asset,
        price
    )

    if not authorizations:
        return None, "PREPARE_FAILED", None

    try:
        approvals = sign_authorizations(
            authorizations
        )
    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True
        )
        return None, str(e), None

    create_query = """
    mutation CreateSale(
        $input: createSingleSaleOfferInput!
    ) {
        createSingleSaleOffer(
            input: $input
        ) {
            tokenOffer {
                id
                startDate
                endDate
            }

            errors {
                message
            }
        }
    }
    """

    create_input = {
        "approvals": approvals,
        "dealId": new_id(),
        "assetId": asset,
        "settlementCurrencies": "EUR",
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },

        # DURATA MASSIMA: 7 GIORNI
        "duration": SALE_DURATION_SECONDS,

        "clientMutationId": new_id()
    }

    print(
        f"📤 createSingleSaleOffer → EUR {eur(price)} "
        f"| durata=7 giorni",
        flush=True
    )

    data = gql(
        create_query,
        {"input": create_input}
    )

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print(
            "❌ createSingleSaleOffer: nessun risultato",
            flush=True
        )
        return None, "CREATE_NO_RESULT", None

    errors = result.get("errors") or []

    if errors:
        error_text = " ".join(
            str(e.get("message", ""))
            for e in errors
            if isinstance(e, dict)
        )

        if is_not_owned_error(error_text):
            print(
                "🚫 NOT_OWNED → "
                "Sorare segnala che la carta "
                "non è posseduta su Solana",
                flush=True
            )
            return None, "NOT_OWNED", None

        if "active public offer already exists" in norm(
            error_text
        ):
            print(
                "🟢 CARTA GIÀ IN VENDITA SU SORARE",
                flush=True
            )

            existing = find_active_public_offer(
                asset
            )

            if existing:
                return (
                    existing.get("id"),
                    "ALREADY-LISTED",
                    existing.get("endDate")
                )

            return (
                "ALREADY-LISTED",
                "ALREADY-LISTED",
                None
            )

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return None, error_text, None

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")
    end_date = token_offer.get("endDate")

    if not offer_id:
        print(
            "❌ Vendita non creata: offer ID assente",
            flush=True
        )
        return None, "OFFER_ID_MISSING", None

    print(
        f"✅ INSERZIONE CREATA → {offer_id}",
        flush=True
    )

    print(
        f"⏰ SCADENZA → {end_date}",
        flush=True
    )

    return offer_id, None, end_date


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(
    card,
    price,
    allow_technical_retry=True
):
    asset = asset_id(card)

    if not asset:
        return None, None, None, None

    print(
        f"🎯 TENTATIVO → {eur(price)}",
        flush=True
    )

    offer_id, error, end_date = create_sale_once(
        card,
        price
    )

    if offer_id:
        return (
            offer_id,
            price,
            end_date,
            None
        )

    if error == "NOT_OWNED":
        return None, None, "NOT_OWNED", None

    if not allow_technical_retry:
        print(
            "⚠️ RINNOVO → nessun cambio prezzo consentito",
            flush=True
        )
        return None, None, error, None

    if not is_technical_price_error(error or ""):
        print(
            "❌ Vendita fallita per errore "
            "non legato al minimo tecnico.",
            flush=True
        )
        return None, None, error, None

    print(
        "⚠️ PREZZO SOTTO IL MINIMO TECNICO SORARE",
        flush=True
    )

    eth_eur_rate = get_eth_eur_rate()

    if eth_eur_rate is None:
        return None, None, "ETH_EUR_UNAVAILABLE", None

    technical_eth = TECHNICAL_START_ETH

    for _ in range(1000):
        technical_price = eth_to_eur_cents(
            technical_eth,
            eth_eur_rate
        )

        if technical_price is None:
            return None, None, "ETH_EUR_CONVERSION", None

        technical_price = max(
            technical_price,
            MIN_SELL_PRICE_CENTS
        )

        print(
            f"🔁 TENTATIVO MINIMO TECNICO → "
            f"{technical_eth:.4f} ETH ≈ "
            f"{eur(technical_price)}",
            flush=True
        )

        offer_id, error, end_date = create_sale_once(
            card,
            technical_price
        )

        if offer_id:
            print(
                f"✅ MINIMO TECNICO TROVATO → "
                f"{technical_eth:.4f} ETH ≈ "
                f"{eur(technical_price)}",
                flush=True
            )

            return (
                offer_id,
                technical_price,
                end_date,
                None
            )

        if error == "NOT_OWNED":
            return None, None, "NOT_OWNED", None

        if not is_technical_price_error(error or ""):
            print(
                "❌ Tentativo tecnico fallito per "
                "errore non legato al prezzo.",
                flush=True
            )
            return None, None, error, None

        technical_eth = round(
            technical_eth + TECHNICAL_STEP_ETH,
            4
        )

    print(
        "❌ Raggiunto limite massimo "
        "dei tentativi tecnici.",
        flush=True
    )

    return None, None, "TECHNICAL_RETRY_LIMIT", None


# ============================================================
# PROCESS CARD
# ============================================================

def process(card):
    asset = asset_id(card)

    print(
        f"\n💰 AUTOSELL CHECK → {asset}",
        flush=True
    )

    if norm(card.get("status")) == "not_owned":
        print(
            "🚫 NOT_OWNED → nessun nuovo tentativo",
            flush=True
        )
        return

    existing_offer = find_active_public_offer(asset)

    if existing_offer:
        print(
            "🟢 CARTA GIÀ IN VENDITA",
            flush=True
        )

        update_card(
            asset,
            status="SELLING",
            offer_id=existing_offer.get("id"),
            sale_price_cents=existing_offer.get("price"),
            sale_offer_end_date=existing_offer.get("endDate"),
            error=None
        )
        return

    details = card_details(asset)

    if not details:
        print(
            "❌ Carta non recuperabile",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error="CARD_DETAILS"
        )
        return

    print(
        f"🃏 {details.get('name') or details.get('slug')} "
        f"{details.get('seasonYear')} • "
        f"{norm(details.get('rarityTyped'))}",
        flush=True
    )

    ok, result = validate(details)

    if not ok:
        messages = {
            "KULENOVIC": "KULENOVIC PROTETTO",
            "SEALED": "CARTA SEALED",
            "RARITY": "RARITÀ NON LIMITED",
            "FLOOR_UNKNOWN": "FLOOR NON DISPONIBILE",
            "FLOOR_HIGH": "FLOOR SOPRA €0.70"
        }

        print(
            f"🚫 ESCLUSA → {messages.get(result, result)}",
            flush=True
        )

        update_card(
            asset,
            status=(
                "BLOCKED"
                if result in {
                    "KULENOVIC",
                    "SEALED",
                    "RARITY"
                }
                else "da_vendere"
            ),
            error=result
        )
        return

    price = int(result)

    print(
        f"✅ CARTA VALIDA → prezzo {eur(price)}",
        flush=True
    )

    if not update_card(
        asset,
        status="SELLING",
        sale_price_cents=price,
        error=None
    ):
        print(
            "❌ Impossibile impostare SELLING",
            flush=True
        )
        return

    (
        offer_id,
        used_price,
        result_error,
        result_end_date
    ) = create_sale(
        details,
        price,
        allow_technical_retry=True
    )

    if result_error == "NOT_OWNED":
        update_card(
            asset,
            status="NOT_OWNED",
            error="SORARE_NOT_OWNED_ON_SOLANA"
        )

        print(
            "🚫 STATO → NOT_OWNED",
            flush=True
        )
        return

    if not offer_id:
        update_card(
            asset,
            status="da_vendere",
            error=(
                str(result_error)
                if result_error
                else "CREATE_SALE_FAILED"
            )
        )

        print(
            "🔁 Carta rimessa in DA_VENDERE",
            flush=True
        )
        return

    # IMPORTANTE:
    # salviamo la vera endDate restituita da Sorare.
    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        sale_price_cents=(
            used_price
            if used_price is not None
            else price
        ),
        sale_offer_end_date=result_end_date,
        error=None
    )

    if offer_id == "ALREADY-LISTED":
        print(
            "🟢 CARTA GIÀ IN VENDITA",
            flush=True
        )

    elif offer_id == "DRY-RUN":
        print(
            "🟡 DRY RUN COMPLETATO",
            flush=True
        )

    else:
        print(
            f"🎉 AUTOSELL COMPLETATO → "
            f"{label(details)} | "
            f"offer={offer_id} | "
            f"price={eur(used_price)} | "
            f"end={result_end_date}",
            flush=True
        )

    print(
        "🟢 STATO → SELLING",
        flush=True
    )


# ============================================================
# RENEWAL
# ============================================================

def renew_expired_sales():
    cards = selling_cards()

    if not cards:
        return

    print(
        f"♻️ Renewal check → "
        f"{len(cards)} carte SELLING",
        flush=True
    )

    for card in cards:
        asset = asset_id(card)

        if not asset:
            continue

        if norm(card.get("status")) == "not_owned":
            continue

        # ----------------------------------------------------
        # CONTROLLO OFFERTA ATTIVA
        # ----------------------------------------------------

        existing = find_active_public_offer(asset)

        if existing:
            update_card(
                asset,
                status="SELLING",
                offer_id=existing.get("id"),
                sale_price_cents=existing.get("price"),
                sale_offer_end_date=existing.get("endDate"),
                error=None
            )
            continue

        # ----------------------------------------------------
        # NESSUNA OFFERTA ATTIVA
        # ----------------------------------------------------

        end_date = card.get(
            "sale_offer_end_date"
        )

        previous_price = card.get(
            "sale_price_cents"
        )

        if not end_date:
            print(
                f"ℹ️ Renewal → {asset}: "
                f"scadenza non presente nello state",
                flush=True
            )
            continue

        if not is_expired(end_date):
            continue

        if previous_price is None:
            print(
                f"⚠️ Renewal → {asset}: "
                f"prezzo precedente non disponibile",
                flush=True
            )
            continue

        try:
            previous_price = int(previous_price)
        except Exception:
            print(
                f"⚠️ Renewal → {asset}: "
                f"prezzo non valido",
                flush=True
            )
            continue

        print(
            f"♻️ OFFERTA SCADUTA → {asset}",
            flush=True
        )

        print(
            f"♻️ RINNOVO ALLO STESSO PREZZO → "
            f"{eur(previous_price)}",
            flush=True
        )

        details = card_details(asset)

        if not details:
            print(
                "❌ Renewal → "
                "card details non disponibili",
                flush=True
            )
            continue

        # Nessun validate():
        # il rinnovo usa esattamente previous_price.
        (
            offer_id,
            used_price,
            result_error,
            result_end_date
        ) = create_sale(
            details,
            previous_price,
            allow_technical_retry=False
        )

        if result_error == "NOT_OWNED":
            update_card(
                asset,
                status="NOT_OWNED",
                error="SORARE_NOT_OWNED_ON_SOLANA"
            )

            print(
                "🚫 Renewal → NOT_OWNED",
                flush=True
            )
            continue

        if offer_id == "ALREADY-LISTED":
            existing = find_active_public_offer(asset)

            if existing:
                update_card(
                    asset,
                    status="SELLING",
                    offer_id=existing.get("id"),
                    sale_price_cents=(
                        existing.get("price")
                        or previous_price
                    ),
                    sale_offer_end_date=existing.get("endDate"),
                    error=None
                )

            continue

        if not offer_id:
            print(
                f"⚠️ Renewal fallito → "
                f"{asset} | {result_error}",
                flush=True
            )

            update_card(
                asset,
                status="SELLING",
                error=(
                    str(result_error)
                    if result_error
                    else "RENEWAL_FAILED"
                )
            )
            continue

        # ----------------------------------------------------
        # RINNOVO RIUSCITO
        # ----------------------------------------------------

        update_card(
            asset,
            status="SELLING",
            offer_id=offer_id,
            sale_price_cents=previous_price,
            sale_offer_end_date=result_end_date,
            error=None
        )

        print(
            f"♻️ RINNOVO COMPLETATO → "
            f"{asset} | "
            f"offer={offer_id} | "
            f"price={eur(previous_price)} | "
            f"end={result_end_date}",
            flush=True
        )


# ============================================================
# RECOVERY
# ============================================================

def recovery():
    selling = selling_cards()

    if not selling:
        print(
            "🔄 Recovery: nessuna carta SELLING.",
            flush=True
        )
        return

    print(
        f"🔄 Recovery: {len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:
        print(
            f"   └─ {asset_id(card)} "
            f"| offer={card.get('sale_offer_id')} "
            f"| price={eur(card.get('sale_price_cents'))} "
            f"| end={card.get('sale_offer_end_date')}",
            flush=True
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print(f"📦 VERSIONE: {VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)
    print("💰 FLOOR MAX: €0.70", flush=True)
    print("💰 PREZZO MINIMO: €0.30", flush=True)
    print(
        "🔁 MINIMO TECNICO: da 0.0002 ETH, +0.0001 ETH",
        flush=True
    )
    print(f"📊 LISTING MINIME: {MIN_LISTINGS}", flush=True)
    print("⏱️ DURATA VENDITA: 7 GIORNI", flush=True)
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("🔒 SEALED: MAI VENDUTE", flush=True)
    print("🃏 RARITÀ: SOLO LIMITED", flush=True)
    print("🛡️ COVERAGE: DISABILITATA", flush=True)
    print(f"🛡️ SOURCE: {SOURCE_LABEL}", flush=True)
    print("💶 SETTLEMENT: EUR", flush=True)
    print(
        "🟢 PRE-CHECK: GIÀ IN VENDITA → NON RIPROVARE",
        flush=True
    )
    print(
        "♻️ RENEWAL: TUTTE LE SELLING → STESSO PREZZO",
        flush=True
    )
    print(
        "⏱️ RENEWAL: NUOVI 7 GIORNI",
        flush=True
    )
    print(
        "🚫 NOT_OWNED: BLOCCO PERMANENTE",
        flush=True
    )
    print(
        "🛡️ DUPLICATE ERROR: TRATTATO COME SELLING",
        flush=True
    )
    print(f"💾 STORAGE: {STATE_FILE}", flush=True)

    try:
        auth_headers()
        ensure_state()
    except Exception as e:
        print(
            f"❌ Configurazione: {e}",
            flush=True
        )
        return

    if not check_account():
        return

    recovery()

    while True:
        try:
            # Prima tutti i rinnovi.
            renew_expired_sales()

            # Poi nuove vendite.
            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: {len(cards)}",
                flush=True
            )

            for card in cards:
                try:
                    process(card)

                except Exception as e:
                    asset = asset_id(card)

                    print(
                        f"❌ AutoSell {asset}: {e}",
                        flush=True
                    )

                    if not asset:
                        continue

                    current = [
                        c
                        for c in get_cards()
                        if (
                            norm(asset_id(c))
                            == norm(asset)
                        )
                    ]

                    if (
                        current
                        and norm(
                            current[0].get("status")
                        ) == "not_owned"
                    ):
                        continue

                    update_card(
                        asset,
                        status="da_vendere",
                        error=str(e)
                    )

            time.sleep(INTERVAL)

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True
            )
            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    cards = get_cards()

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "floor_max": "€0.70",
        "min_sell_price": "€0.30",
        "technical_min_eth": "0.0002",
        "technical_step_eth": "0.0001",
        "sale_duration_days": 7,
        "sale_duration_seconds": SALE_DURATION_SECONDS,
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "sealed": "NEVER_SELL",
        "age": "NOT_USED",
        "kulenovic": "NEVER_SELL",
        "coverage": "DISABLED",
        "source": SOURCE_LABEL,
        "settlement": "EUR",
        "already_listed": "SKIP",
        "renewal": "PREVIOUS_PRICE",
        "renewal_duration_days": 7,
        "not_owned": "PERMANENT_BLOCK",
        "duplicate_offer": "SELLING",
        "storage": STATE_FILE,
        "cards": len(cards),

        "da_vendere": sum(
            1
            for c in cards
            if norm(c.get("status"))
            in {"da_vendere", "ready"}
        ),

        "selling": sum(
            1
            for c in cards
            if norm(c.get("status")) == "selling"
        ),

        "not_owned": sum(
            1
            for c in cards
            if norm(c.get("status")) == "not_owned"
        ),

        "worker": worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": VERSION,
        "worker": worker_started,
        "dry_run": DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():
    cards = get_cards()

    return jsonify({
        "count": len(cards),
        "cards": cards
    })


# ============================================================
# MAIN
# ============================================================

def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        ),
        debug=False
    )
