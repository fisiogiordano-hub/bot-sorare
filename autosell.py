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

DRY_RUN = (
    os.getenv("DRY_RUN", "false").strip().lower() == "true"
)

INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

# ============================================================
# AUTOSELL RULES
# ============================================================

# Tutti i prezzi interni sono in CENTESIMI EUR.
#
# €0.01  -> 1
# €0.10  -> 10
# €0.32  -> 32
# €0.70  -> 70

MIN_PRICE = 0
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

VERSION = "AUTOSell-17.0-SOLANA-EUR-CENTS-FIX"

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# LOCKS
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

def norm(v):
    return str(v or "").strip().lower()


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
    """
    cents = centesimi EUR.

    1  -> €0.01
    32 -> €0.32
    70 -> €0.70
    """

    if cents is None:
        return "N/D"

    try:
        return f"€{int(cents) / 100:.2f}"
    except Exception:
        return "N/D"


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

        data.setdefault(
            "processed_offers",
            []
        )

        data.setdefault(
            "acquired_cards",
            []
        )

        data.setdefault(
            "pending_autobuys",
            []
        )

        data.setdefault(
            "updated_at",
            int(time.time())
        )

        return data

    except Exception as e:

        print(
            f"❌ Errore lettura state: {e}",
            flush=True
        )

        return default_state()


def save_document(data):

    tmp = (
        f"{STATE_FILE}."
        f"{uuid.uuid4().hex}.tmp"
    )

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

        os.replace(
            tmp,
            STATE_FILE
        )

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
    error=None
):

    wanted = norm(asset)

    with state_lock:

        data = load_document()

        cards = data.get(
            "acquired_cards",
            []
        )

        for card in cards:

            if not isinstance(card, dict):
                continue

            if norm(
                asset_id(card)
            ) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = offer_id

            card["last_error"] = error

            if norm(status) == "selling":

                card["selling_at"] = (
                    card.get("selling_at")
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


def sellable_cards():

    result = []

    for card in get_cards():

        if not isinstance(card, dict):
            continue

        if norm(
            card.get("status")
        ) not in {
            "da_vendere",
            "ready"
        }:
            continue

        if asset_id(card):
            result.append(dict(card))

    return result


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
        "User-Agent": (
            f"Sorare-AutoSell/{VERSION}"
        )
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
                f"🌐 Sorare HTTP "
                f"{response.status_code}",
                flush=True
            )

            if response.status_code == 429:

                time.sleep(
                    2 + attempt * 2
                )

                continue

            if response.status_code != 200:

                print(
                    f"❌ Sorare: "
                    f"{response.text[:1500]}",
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
    """, {
        "ids": [asset]
    })

    cards = (
        ((data or {}).get("data") or {})
        .get("anyCards")
        or []
    )

    return cards[0] if cards else None


# ============================================================
# ACTIVE PUBLIC OFFER PRE-CHECK
# ============================================================

def find_active_public_offer(asset):

    wanted = norm(asset)

    data = gql("""
        query ActivePublicOffers($first: Int) {

            tokens {

                liveSingleSaleOffers(
                    first: $first
                ) {

                    nodes {

                        id

                        senderSide {

                            anyCards {
                                assetId
                            }
                        }
                    }
                }
            }
        }
    """, {
        "first": 100
    })

    if not data:

        print(
            "⚠️ Pre-check offerte: "
            "nessuna risposta",
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

        if not isinstance(
            offer,
            dict
        ):
            continue

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for c in cards:

            if not isinstance(c, dict):
                continue

            if norm(
                c.get("assetId")
            ) == wanted:

                print(
                    "🟢 PRE-CHECK → "
                    "CARTA GIÀ IN VENDITA",
                    flush=True
                )

                print(
                    f"🟢 OFFER → "
                    f"{offer.get('id')}",
                    flush=True
                )

                return offer.get("id")

    print(
        "🔵 PRE-CHECK → "
        "nessuna offerta pubblica attiva",
        flush=True
    )

    return None


# ============================================================
# USD -> EUR
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

        return round(
            usd_cents * rate
        )

    except Exception:

        return None


def amount_to_eur(amounts):

    if not isinstance(
        amounts,
        dict
    ):
        return None

    try:

        value = int(
            amounts.get("eurCents")
            or 0
        )

        if value > 0:
            return value

    except Exception:
        pass

    try:

        value = int(
            amounts.get("usdCents")
            or 0
        )

        if value > 0:
            return usd_to_eur(value)

    except Exception:
        pass

    return None


# ============================================================
# FLOOR
# ============================================================

def get_floor(card):

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

    except Exception:

        return None

    if not player_slug or not rarity:
        return None

    data = gql("""
        query LiveOffers(
            $slug: String,
            $first: Int
        ) {

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

        sender = (
            offer.get("senderSide")
            or {}
        )

        for listed in (
            sender.get("anyCards")
            or []
        ):

            if not isinstance(
                listed,
                dict
            ):
                continue

            listed_player = (
                listed.get("anyPlayer")
                or {}
            )

            try:

                same_season = (
                    int(
                        listed.get(
                            "seasonYear"
                        )
                    )
                    == season
                )

            except Exception:

                continue

            if not same_season:
                continue

            if norm(
                listed_player.get("slug")
            ) != player_slug:
                continue

            if norm(
                listed.get("rarityTyped")
            ) != rarity:
                continue

            price = amount_to_eur(
                (
                    offer.get(
                        "receiverSide"
                    )
                    or {}
                ).get("amounts")
            )

            if price is not None:
                prices.append(price)

            break

    print(
        f"📊 Listing trovate: "
        f"{len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    if len(prices) < MIN_LISTINGS:
        return None

    floor = min(prices)

    print(
        f"✅ FLOOR → {eur(floor)}",
        flush=True
    )

    return floor


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

                ... on
                SolanaTokenTransferAuthorizationRequest {

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


def prepare_sale(
    asset,
    price,
    quiet=False
):
    """
    price = CENTESIMI EUR.

    Esempi:

        price=1
        => €0.01

        price=44
        => €0.44

        price=70
        => €0.70

    IMPORTANTE:
    Sorare vuole il valore intero in centesimi,
    non "0.44".
    """

    price = int(price)

    if price < 1:
        print(
            "❌ Prezzo prepareOffer non valido: "
            f"{price}",
            flush=True
        )
        return None

    input_data = {

        "sendAssetIds": [
            asset
        ],

        "receiveAssetIds": [],

        "settlementCurrencies": [
            "EUR"
        ],

        # ====================================================
        # CORREZIONE FONDAMENTALE
        # ====================================================
        #
        # NON:
        #
        # "0.44"
        #
        # MA:
        #
        # "44"
        #
        # perché 44 = €0.44.
        #
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },

        "clientMutationId": new_id()
    }

    if not quiet:

        print(
            "📦 prepareOffer → "
            f"{eur(price)} EUR "
            f"(amount={price})",
            flush=True
        )

    data = gql(
        PREPARE_QUERY,
        {
            "input": input_data
        }
    )

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

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

        return None

    authorizations = (
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        return None

    if not quiet:

        print(
            "✅ Authorization ricevute: "
            f"{len(authorizations)}",
            flush=True
        )

    return authorizations


# ============================================================
# DINAMIC MINIMUM SORARE PRICE
# ============================================================

def find_sorare_minimum_price(
    asset,
    max_price=MAX_PRICE
):
    """
    Trova il minimo tecnico accettato da Sorare.

    Tutti i valori sono CENTESIMI.

    Esempio:

        1  = €0.01
        32 = €0.32
        70 = €0.70

    La funzione NON crea una vendita.
    Chiama solamente prepareOffer.
    """

    print(
        "🔎 Ricerca MINIMO TECNICO SORARE...",
        flush=True
    )

    low = 1
    high = int(max_price)

    # --------------------------------------------------------
    # TEST LIMITE SUPERIORE
    # --------------------------------------------------------

    upper = prepare_sale(
        asset,
        high,
        quiet=True
    )

    if upper is None:

        print(
            f"🚫 Sorare non accetta "
            f"nemmeno {eur(high)}",
            flush=True
        )

        return None

    print(
        f"🟢 {eur(high)} accettato da Sorare",
        flush=True
    )

    # --------------------------------------------------------
    # BINARY SEARCH
    # --------------------------------------------------------

    while low < high:

        mid = (
            low + high
        ) // 2

        result = prepare_sale(
            asset,
            mid,
            quiet=True
        )

        if result is not None:

            print(
                f"🟢 {eur(mid)} "
                f"→ ACCETTATO",
                flush=True
            )

            high = mid

        else:

            print(
                f"🔴 {eur(mid)} "
                f"→ RIFIUTATO",
                flush=True
            )

            low = mid + 1

    # --------------------------------------------------------
    # VERIFICA FINALE
    # --------------------------------------------------------

    final = prepare_sale(
        asset,
        low,
        quiet=True
    )

    if final is None:

        print(
            "❌ Impossibile confermare "
            "il minimo tecnico Sorare",
            flush=True
        )

        return None

    print(
        "💶 MINIMO TECNICO SORARE → "
        f"{eur(low)} "
        f"(amount={low})",
        flush=True
    )

    return low


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):

    return (
        norm(
            card.get("slug")
        )
        ==
        norm(
            KULENOVIC_SLUG
        )
        or
        norm(
            card.get("assetId")
        )
        ==
        norm(
            KULENOVIC_ASSET
        )
    )


def validate(card):

    if is_kulenovic(card):

        return False, "KULENOVIC"

    if (
        norm(
            card.get("rarityTyped")
        ).upper()
        != "LIMITED"
    ):

        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:

        return False, "FLOOR_UNKNOWN"

    if floor > MAX_PRICE:

        return False, "FLOOR_HIGH"

    return True, floor


# ============================================================
# BASE58
# ============================================================

def base58_decode(value):

    value = str(value).strip()

    if not value:

        raise ValueError(
            "Base58 vuoto"
        )

    number = 0

    for char in value:

        index = SOLANA_ALPHABET.find(
            char
        )

        if index < 0:

            raise ValueError(
                f"Carattere Base58 "
                f"non valido: {char}"
            )

        number = (
            number * 58
            + index
        )

    raw = (
        b""
        if number == 0
        else number.to_bytes(
            max(
                1,
                (
                    number.bit_length()
                    + 7
                ) // 8
            ),
            "big"
        )
    )

    zeros = 0

    for char in value:

        if char != "1":
            break

        zeros += 1

    return (
        b"\x00" * zeros
        + raw
    )


# ============================================================
# SOLANA KEY CHECK
# ============================================================

def solana_key_info():

    if not SOLANA:

        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY "
            "mancante"
        )

    value = SOLANA.strip()

    try:

        decoded = base58_decode(
            value
        )

        if len(decoded) in {
            32,
            64
        }:

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
            c in
            "0123456789abcdefABCDEF"
            for c in hex_value
        )
    ):

        decoded = bytes.fromhex(
            hex_value
        )

        if len(decoded) in {
            32,
            64
        }:

            return {
                "format": "hex",
                "bytes": decoded
            }

    raise RuntimeError(
        "SORARE_SOLANA_PRIVATE_KEY "
        "non valida. Atteso Base58 "
        "o HEX da 32/64 byte."
    )


# ============================================================
# SIGN AUTHORIZATIONS
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

    types = [

        (
            a.get("request") or {}
        ).get("__typename")

        for a in authorizations
    ]

    requires_stark = any(
        t not in {
            "SolanaTokenTransferAuthorizationRequest"
        }
        for t in types
    )

    requires_solana = any(
        t ==
        "SolanaTokenTransferAuthorizationRequest"
        for t in types
    )

    if requires_stark and not STARK:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "mancante"
        )

    if requires_solana and not SOLANA:

        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY "
            "mancante"
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
    require("fs").readFileSync(
        0,
        "utf8"
    )
);

const ALPHABET =
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
    "abcdefghijkmnopqrstuvwxyz";


function b58decode(value) {

    let num = 0n;

    for (
        const char of
        String(value).trim()
    ) {

        const i =
            ALPHABET.indexOf(char);

        if (i < 0) {

            throw new Error(
                "Base58 non valido"
            );
        }

        num =
            num * 58n
            + BigInt(i);
    }

    let bytes;

    if (num === 0n) {

        bytes = Buffer.alloc(0);

    } else {

        let hex =
            num.toString(16);

        if (
            hex.length % 2
        ) {

            hex =
                "0" + hex;
        }

        bytes =
            Buffer.from(
                hex,
                "hex"
            );
    }

    let zeros = 0;

    for (
        const char of
        String(value).trim()
    ) {

        if (char !== "1")
            break;

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

    const bytes =
        Buffer.from(data);

    let num = 0n;

    for (
        const byte of bytes
    ) {

        num =
            num * 256n
            + BigInt(byte);
    }

    let result = "";

    while (num > 0n) {

        const r =
            Number(
                num % 58n
            );

        result =
            ALPHABET[r]
            + result;

        num /=
            58n;
    }

    let zeros = 0;

    for (
        const byte of bytes
    ) {

        if (byte !== 0)
            break;

        zeros++;
    }

    return (
        "1".repeat(zeros)
        + result
    );
}


function parsePrivateKey(
    value
) {

    const clean =
        String(value || "")
        .trim();

    try {

        const decoded =
            b58decode(clean);

        if (
            decoded.length === 32
            ||
            decoded.length === 64
        ) {

            return decoded;
        }

    } catch (_) {}

    let hex = clean;

    if (
        hex.startsWith("0x")
    ) {

        hex =
            hex.slice(2);
    }

    if (
        /^[0-9a-fA-F]+$/.test(hex)
        &&
        hex.length % 2 === 0
    ) {

        const decoded =
            new Uint8Array(
                Buffer.from(
                    hex,
                    "hex"
                )
            );

        if (
            decoded.length === 32
            ||
            decoded.length === 64
        ) {

            return decoded;
        }
    }

    throw new Error(
        "Chiave Solana "
        + "non riconosciuta"
    );
}


async function createSolanaSigner(
    privateKey
) {

    let keyBytes =
        parsePrivateKey(
            privateKey
        );

    if (
        keyBytes.length === 64
    ) {

        keyBytes =
            keyBytes.slice(
                0,
                32
            );
    }

    if (
        keyBytes.length !== 32
    ) {

        throw new Error(
            "Private key Solana "
            + "non valida: "
            + keyBytes.length
            + " byte"
        );
    }

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            keyBytes
        );

    return createSignerFromKeyPair(
        keyPair
    );
}


async function signSolana(
    auth
) {

    const req =
        auth.request;

    const signer =
        await createSolanaSigner(
            input.solanaPrivateKey
        );

    console.error(
        "🔑 Solana signer → "
        + signer.address
    );

    console.error(
        "🎯 senderAddress → "
        + req.senderAddress
    );

    if (
        signer.address
        !==
        req.senderAddress
    ) {

        throw new Error(
            "La chiave Solana "
            + "NON corrisponde "
            + "al senderAddress. "
            + "Derivato="
            + signer.address
            + " richiesto="
            + req.senderAddress
        );
    }

    // ========================================================
    // MESSAGGIO SOLANA UFFICIALE SORARE
    // ========================================================

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

    console.error(
        "📝 Solana message → "
        + message
    );

    // ========================================================
    // SHA-256 DEL MESSAGGIO
    // ========================================================

    const hash =
        crypto
        .createHash("sha256")
        .update(
            Buffer.from(
                message,
                "utf8"
            )
        )
        .digest();

    // ========================================================
    // ED25519 SIGN DEL HASH
    // ========================================================

    const signable =
        createSignableMessage(
            new Uint8Array(hash)
        );

    const signatures =
        await signer.signMessages([
            signable
        ]);

    const signatureBytes =
        signatures[0][
            signer.address
        ];

    const signature =
        b58encode(
            signatureBytes
        );

    console.error(
        "✍️ Solana signature generata"
    );

    return {

        fingerprint:
            auth.fingerprint,

        solanaTokenTransferApproval: {

            signature,

            nonce:
                req.nonce,

            expirationTimestamp:
                req.expirationTimestamp
        }
    };
}


function signStark(auth) {

    const req =
        auth.request;

    const signature =
        signAuthorizationRequest(
            input.starkPrivateKey,
            req
        );

    const base = {

        fingerprint:
            auth.fingerprint
    };

    if (
        req.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {

            ...base,

            starkexTransferApproval: {

                nonce:
                    req.nonce,

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

                nonce:
                    req.nonce,

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

                nonce:
                    req.nonce,

                signature
            }
        };
    }

    throw new Error(
        "Authorization "
        + "non supportata: "
        + req.__typename
    );
}


async function main() {

    const approvals = [];

    for (
        const auth
        of input.authorizations
    ) {

        const type =
            (
                auth.request
                || {}
            ).__typename;

        console.error(
            "🔐 Authorization → "
            + type
        );

        if (
            type ===
            "SolanaTokenTransferAuthorizationRequest"
        ) {

            approvals.push(
                await signSolana(auth)
            );

        } else {

            approvals.push(
                signStark(auth)
            );
        }
    }

    process.stdout.write(
        JSON.stringify(
            approvals
        )
    );
}


main().catch(
    error => {

        console.error(
            error.stack || error
        );

        process.exit(1);
    }
);
'''

    process = subprocess.run(
        [node, "-e", js],

        input=json.dumps({

            "starkPrivateKey": STARK,

            "solanaPrivateKey": SOLANA,

            "authorizations":
                authorizations
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

        return json.loads(
            process.stdout
        )

    except Exception:

        raise RuntimeError(
            "Output firma non valido: "
            + process.stdout[:1000]
        )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(
    card,
    price
):

    asset = asset_id(card)

    if not asset:
        return None

    sale_price = int(price)

    if sale_price < MIN_PRICE:

        print(
            "🚫 Prezzo sotto MIN_PRICE → "
            f"{eur(MIN_PRICE)}",
            flush=True
        )

        return None

    if sale_price > MAX_PRICE:

        print(
            "🚫 Prezzo oltre il massimo → "
            f"{eur(MAX_PRICE)}",
            flush=True
        )

        return None

    if DRY_RUN:

        print(
            f"🟡 DRY RUN → "
            f"{label(card)} → "
            f"{eur(sale_price)} "
            f"(amount={sale_price})",
            flush=True
        )

        return "DRY-RUN"

    # ========================================================
    # DUPLICATE CHECK
    # ========================================================

    existing_offer = (
        find_active_public_offer(
            asset
        )
    )

    if existing_offer:

        print(
            "🟢 CARTA GIÀ IN VENDITA "
            "SU SORARE",
            flush=True
        )

        return existing_offer

    # ========================================================
    # PREPARE FINALE
    # ========================================================

    authorizations = prepare_sale(
        asset,
        sale_price
    )

    if not authorizations:

        print(
            "⚠️ Prezzo non accettato "
            "da Sorare durante prepareOffer.",
            flush=True
        )

        return None

    # ========================================================
    # FIRMA
    # ========================================================

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:

        print(
            f"❌ Firma: {e}",
            flush=True
        )

        return None

    # ========================================================
    # CREATE SINGLE SALE OFFER
    # ========================================================

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

    # ========================================================
    # CORREZIONE FONDAMENTALE:
    #
    # amount = CENTESIMI
    #
    # €0.44 -> "44"
    # €0.65 -> "65"
    # €0.70 -> "70"
    #
    # NON:
    #
    # €0.44 -> "0.44"
    # ========================================================

    create_input = {

        "approvals":
            approvals,

        "dealId":
            new_id(),

        "assetId":
            asset,

        "receiveAmount": {

            "amount":
                str(sale_price),

            "currency":
                "EUR"
        },

        "clientMutationId":
            new_id()
    }

    print(
        "📤 createSingleSaleOffer → "
        f"{eur(sale_price)} EUR "
        f"(amount={sale_price})",
        flush=True
    )

    data = gql(
        create_query,
        {
            "input":
                create_input
        }
    )

    result = (
        ((data or {}).get("data") or {})
        .get(
            "createSingleSaleOffer"
        )
    )

    if not result:

        print(
            "❌ createSingleSaleOffer: "
            "nessun risultato",
            flush=True
        )

        return None

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        error_text = " ".join(

            str(
                e.get(
                    "message",
                    ""
                )
            )

            for e in errors

            if isinstance(
                e,
                dict
            )
        )

        if (
            "active public offer already exists"
            in norm(error_text)
        ):

            print(
                "🟢 CARTA GIÀ IN VENDITA",
                flush=True
            )

            existing_offer = (
                find_active_public_offer(
                    asset
                )
            )

            return (
                existing_offer
                or "ALREADY-LISTED"
            )

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return None

    offer_id = (
        (result.get(
            "tokenOffer"
        ) or {})
        .get("id")
    )

    if not offer_id:

        print(
            "❌ Vendita non creata: "
            "offer ID assente",
            flush=True
        )

        return None

    print(
        f"✅ INSERZIONE CREATA → "
        f"{offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process(card):

    asset = asset_id(card)

    print(
        f"\n💰 AUTOSELL CHECK → "
        f"{asset}",
        flush=True
    )

    # ========================================================
    # GIÀ IN VENDITA
    # ========================================================

    existing_offer = (
        find_active_public_offer(
            asset
        )
    )

    if existing_offer:

        print(
            "🟢 GIÀ IN VENDITA → "
            "NON MODIFICARE",
            flush=True
        )

        update_card(
            asset,
            status="SELLING",
            offer_id=existing_offer,
            error=None
        )

        return

    # ========================================================
    # DETTAGLI
    # ========================================================

    details = card_details(
        asset
    )

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
        f"🃏 "
        f"{details.get('name') or details.get('slug')} "
        f"{details.get('seasonYear')} • "
        f"{norm(details.get('rarityTyped'))}",
        flush=True
    )

    # ========================================================
    # VALIDAZIONE
    # ========================================================

    ok, result = validate(
        details
    )

    if not ok:

        messages = {

            "KULENOVIC":
                "KULENOVIC PROTETTO",

            "RARITY":
                "RARITÀ NON LIMITED",

            "FLOOR_UNKNOWN":
                "FLOOR NON DISPONIBILE",

            "FLOOR_HIGH":
                "FLOOR SOPRA €0.70"
        }

        print(
            f"🚫 ESCLUSA → "
            f"{messages.get(result, result)}",
            flush=True
        )

        update_card(
            asset,

            status=(
                "BLOCKED"
                if result in {
                    "KULENOVIC",
                    "RARITY"
                }
                else "da_vendere"
            ),

            error=result
        )

        return

    floor = int(result)

    print(
        f"✅ FLOOR → {eur(floor)}",
        flush=True
    )

    # ========================================================
    # DETERMINAZIONE PREZZO
    # ========================================================

    sale_price = floor

    # --------------------------------------------------------
    # SOTTO €0.32:
    # CERCA IL MINIMO TECNICO DI SORARE
    # --------------------------------------------------------

    if floor < 32:

        print(
            "💶 FLOOR SOTTO €0.32 → "
            "RICERCA MINIMO TECNICO SORARE",
            flush=True
        )

        sale_price = (
            find_sorare_minimum_price(
                asset,
                MAX_PRICE
            )
        )

    # ========================================================
    # FALLIMENTO
    # ========================================================

    if sale_price is None:

        print(
            "🚫 MINIMO TECNICO SORARE "
            "NON DETERMINABILE ENTRO €0.70",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error="SORARE_MINIMUM_NOT_FOUND"
        )

        return

    sale_price = int(
        sale_price
    )

    # ========================================================
    # LIMITE MASSIMO
    # ========================================================

    if sale_price > MAX_PRICE:

        print(
            "🚫 PREZZO MINIMO SORARE "
            "OLTRE IL LIMITE → "
            f"{eur(sale_price)}",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error="SORARE_MINIMUM_ABOVE_MAX"
        )

        return

    # ========================================================
    # PREZZO FINALE
    # ========================================================

    print(
        "💰 PREZZO FINALE → "
        f"{eur(sale_price)} "
        f"(amount={sale_price})",
        flush=True
    )

    # ========================================================
    # SELLING SOLO QUANDO STIAMO PER CREARE
    # ========================================================

    if not update_card(
        asset,
        status="SELLING",
        error=None
    ):

        print(
            "❌ Impossibile impostare "
            "SELLING",
            flush=True
        )

        return

    # ========================================================
    # CREAZIONE
    # ========================================================

    offer_id = create_sale(
        details,
        sale_price
    )

    if not offer_id:

        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "🔁 Carta rimessa "
            "in DA_VENDERE",
            flush=True
        )

        return

    # ========================================================
    # SUCCESS
    # ========================================================

    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        error=None
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(details)} | "
        f"{eur(sale_price)} | "
        f"{offer_id}",
        flush=True
    )

    print(
        "🟢 STATO → SELLING",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():

    selling = [

        card

        for card in get_cards()

        if norm(
            card.get("status")
        ) == "selling"
    ]

    if not selling:

        print(
            "🔄 Recovery: "
            "nessuna carta SELLING.",
            flush=True
        )

        return

    print(
        f"🔄 Recovery: "
        f"{len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:

        print(
            f"   └─ "
            f"{asset_id(card)} "
            f"| offer="
            f"{card.get('sale_offer_id')}",
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
        f"{VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        "💰 RANGE FLOOR: "
        "€0.00 - €0.70",
        flush=True
    )

    print(
        "📊 NESSUN FLOOR MINIMO",
        flush=True
    )

    print(
        f"📊 LISTING MINIME: "
        f"{MIN_LISTINGS}",
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
        "🛡️ COVERAGE: DISABILITATA",
        flush=True
    )

    print(
        "🛡️ SOURCE: AUTOBUY / SWAP",
        flush=True
    )

    print(
        "💶 SETTLEMENT: EUR",
        flush=True
    )

    print(
        "💶 UNITÀ PREZZO: CENTESIMI EUR",
        flush=True
    )

    print(
        "💶 MINIMO SORARE: "
        "RICERCA DINAMICA",
        flush=True
    )

    print(
        "🟢 GIÀ IN VENDITA → "
        "NON MODIFICARE",
        flush=True
    )

    print(
        "🔎 FLOOR SOTTO €0.32 → "
        "CERCA MINIMO TECNICO",
        flush=True
    )

    print(
        "🛡️ PRE-CHECK DUPLICATI ATTIVO",
        flush=True
    )

    print(
        f"💾 STORAGE: "
        f"{STATE_FILE}",
        flush=True
    )

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

            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: "
                f"{len(cards)}",
                flush=True
            )

            for card in cards:

                try:

                    process(card)

                except Exception as e:

                    asset = asset_id(
                        card
                    )

                    print(
                        f"❌ AutoSell "
                        f"{asset}: {e}",
                        flush=True
                    )

                    if asset:

                        update_card(
                            asset,
                            status="da_vendere",
                            error=str(e)
                        )

            time.sleep(
                INTERVAL
            )

        except Exception as e:

            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    cards = get_cards()

    return jsonify({

        "status":
            "online",

        "bot":
            "autosell",

        "version":
            VERSION,

        "dry_run":
            DRY_RUN,

        "range":
            "€0.00-€0.70",

        "price_unit":
            "EUR_CENTS",

        "min_floor":
            "NONE",

        "max_floor":
            "€0.70",

        "min_live_listings":
            MIN_LISTINGS,

        "rarity":
            "LIMITED",

        "age":
            "NOT_USED",

        "kulenovic":
            "NEVER_SELL",

        "coverage":
            "DISABLED",

        "settlement":
            "EUR",

        "already_listed":
            "SKIP",

        "technical_minimum":
            "DYNAMIC_PREPARE_OFFER",

        "cards":
            len(cards),

        "da_vendere":
            sum(
                1
                for c in cards
                if norm(
                    c.get("status")
                ) in {
                    "da_vendere",
                    "ready"
                }
            ),

        "selling":
            sum(
                1
                for c in cards
                if norm(
                    c.get("status")
                ) == "selling"
            ),

        "worker":
            worker_started
    })


@app.get("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot":
            "autosell",

        "version":
            VERSION,

        "worker":
            worker_started,

        "dry_run":
            DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():

    cards = get_cards()

    return jsonify({

        "count":
            len(cards),

        "cards":
            cards
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
