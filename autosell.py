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

# Stark key:
# usata SOLO per authorization StarkEx / Mangopay
STARK = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()

# Solana key:
# usata SOLO per SolanaTokenTransferAuthorizationRequest
SOLANA = os.getenv(
    "SORARE_SOLANA_PRIVATE_KEY",
    ""
).strip()

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "false"
    ).strip().lower()
    == "true"
)

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

# ============================================================
# PREZZI
#
# Sorare EUR:
# 32 = €0.32
# 70 = €0.70
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

VERSION = (
    "AUTOSell-12.0-SOLANA-EUR-SETTLEMENT-FIX"
)

# ============================================================
# KULENOVIC PROTETTO
# ============================================================

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# LOCK
# ============================================================

state_lock = threading.RLock()

worker_lock = threading.Lock()

worker_started = False


# ============================================================
# UTILS
# ============================================================

def norm(value):

    return str(
        value or ""
    ).strip().lower()


def now():

    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def new_id():

    return str(
        uuid.uuid4()
    )


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

    return (
        f"€{cents / 100:.2f}"
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

    path = os.path.abspath(
        STATE_FILE
    )

    directory = os.path.dirname(
        path
    )

    if directory:
        os.makedirs(
            directory,
            exist_ok=True
        )

    if not os.path.exists(path):

        save_document(
            default_state()
        )


def load_document():

    ensure_state()

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(
            data,
            dict
        ):

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

            os.fsync(
                f.fileno()
            )

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

        data = load_document()

        cards = data.get(
            "acquired_cards",
            []
        )

        return (
            cards
            if isinstance(
                cards,
                list
            )
            else []
        )


def update_card(
    asset,
    status=None,
    offer_id=None,
    error=None
):

    wanted = norm(
        asset
    )

    with state_lock:

        data = load_document()

        cards = data.get(
            "acquired_cards",
            []
        )

        found = False

        for card in cards:

            if not isinstance(
                card,
                dict
            ):
                continue

            if (
                norm(
                    asset_id(card)
                )
                != wanted
            ):
                continue

            found = True

            if status is not None:

                card["status"] = status

            if offer_id:

                card[
                    "sale_offer_id"
                ] = offer_id

            if error is not None:

                card[
                    "last_error"
                ] = error

            else:

                card[
                    "last_error"
                ] = None

            if status == "SELLING":

                card[
                    "selling_at"
                ] = now()

            break

        if not found:

            print(
                f"⚠️ Carta non presente "
                f"nello state: {asset}",
                flush=True
            )

            return False

        data[
            "acquired_cards"
        ] = cards

        data[
            "updated_at"
        ] = int(time.time())

        return save_document(
            data
        )


def sellable_cards():

    result = []

    for card in get_cards():

        if not isinstance(
            card,
            dict
        ):
            continue

        status = norm(
            card.get("status")
        )

        if status not in {
            "da_vendere",
            "ready"
        }:
            continue

        if not asset_id(card):
            continue

        result.append(
            dict(card)
        )

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
            if TOKEN.lower().startswith(
                "bearer "
            )
            else f"Bearer {TOKEN}"
        ),

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:

        headers[
            "JWT-AUD"
        ] = AUD

    return headers


def gql(
    query,
    variables=None
):

    for attempt in range(3):

        try:

            response = requests.post(
                SORARE_URL,

                headers=auth_headers(),

                json={
                    "query": query,
                    "variables":
                        variables or {}
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
                    "❌ Sorare:",
                    response.text[:1000],
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            data = response.json()

            if data.get(
                "errors"
            ):

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
            "❌ Account Sorare "
            "non verificato",
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

            anyCards(
                assetIds: $ids
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

    return (
        cards[0]
        if cards
        else None
    )


# ============================================================
# EUR
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
            response.json()[
                "rates"
            ]["EUR"]
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
            amounts.get(
                "eurCents"
            ) or 0
        )

        if value > 0:

            return value

    except Exception:
        pass

    try:

        value = int(
            amounts.get(
                "usdCents"
            ) or 0
        )

        if value > 0:

            return usd_to_eur(
                value
            )

    except Exception:
        pass

    return None


# ============================================================
# FLOOR
# ============================================================

def get_floor(card):

    player = (
        card.get(
            "anyPlayer"
        )
        or {}
    )

    player_slug = norm(
        player.get("slug")
    )

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    )

    try:

        season = int(
            card.get(
                "seasonYear"
            )
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
            offer.get(
                "senderSide"
            )
            or {}
        )

        for listed in (
            sender.get(
                "anyCards"
            )
            or []
        ):

            listed_player = (
                listed.get(
                    "anyPlayer"
                )
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

            if (
                norm(
                    listed_player.get(
                        "slug"
                    )
                )
                != player_slug
            ):

                continue

            if (
                norm(
                    listed.get(
                        "rarityTyped"
                    )
                )
                != rarity
            ):

                continue

            price = amount_to_eur(
                (
                    offer.get(
                        "receiverSide"
                    )
                    or {}
                ).get(
                    "amounts"
                )
            )

            if price is not None:

                prices.append(
                    price
                )

            break

    print(
        f"📊 Listing trovate: "
        f"{len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    if (
        len(prices)
        < MIN_LISTINGS
    ):

        return None

    return min(
        prices
    )


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):

    return (
        norm(
            card.get("slug")
        )
        == norm(
            KULENOVIC_SLUG
        )
        or
        norm(
            card.get("assetId")
        )
        == norm(
            KULENOVIC_ASSET
        )
    )


def validate(card):

    if is_kulenovic(card):

        return False, "KULENOVIC"

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        return False, "RARITY"

    floor = get_floor(
        card
    )

    if floor is None:

        return False, "FLOOR_UNKNOWN"

    if floor < MIN_PRICE:

        return False, "FLOOR_LOW"

    if floor > MAX_PRICE:

        return False, "FLOOR_HIGH"

    return True, floor


# ============================================================
# SOLANA BASE58
# ============================================================

SOLANA_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


def base58_decode(value):

    value = str(
        value
    ).strip()

    if not value:

        raise ValueError(
            "Base58 vuoto"
        )

    number = 0

    for char in value:

        index = (
            SOLANA_ALPHABET.find(
                char
            )
        )

        if index < 0:

            raise ValueError(
                "Carattere Base58 "
                f"non valido: {char}"
            )

        number = (
            number * 58
        ) + index

    raw = number.to_bytes(
        max(
            1,
            (
                number.bit_length()
                + 7
            ) // 8
        ),
        "big"
    )

    leading_zeroes = 0

    for char in value:

        if char != "1":

            break

        leading_zeroes += 1

    return (
        b"\x00" * leading_zeroes
    ) + raw.lstrip(
        b"\x00"
    )


def base58_encode(data):

    if not data:

        return ""

    data = bytes(
        data
    )

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
            SOLANA_ALPHABET[
                remainder
            ]
        )

    leading_zeroes = 0

    for byte in data:

        if byte != 0:

            break

        leading_zeroes += 1

    return (
        "1" * leading_zeroes
        + "".join(
            reversed(chars)
        )
    )


# ============================================================
# SOLANA KEY NORMALIZATION
# ============================================================

def solana_key_info():

    if not SOLANA:

        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY "
            "mancante"
        )

    value = SOLANA.strip()

    # --------------------------------------------------------
    # Base58
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # HEX
    # --------------------------------------------------------

    hex_value = value

    if hex_value.startswith(
        "0x"
    ):

        hex_value = (
            hex_value[2:]
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
        "da 32/64 byte oppure HEX "
        "da 32/64 byte."
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

    needs_stark = any(
        (
            a.get("request")
            or {}
        ).get("__typename")
        not in {
            "SolanaTokenTransferAuthorizationRequest"
        }
        for a in authorizations
    )

    needs_solana = any(
        (
            a.get("request")
            or {}
        ).get("__typename")
        ==
        "SolanaTokenTransferAuthorizationRequest"
        for a in authorizations
    )

    if needs_stark and not STARK:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "mancante per authorization "
            "Stark/Mangopay"
        )

    if needs_solana and not SOLANA:

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


// ==========================================================
// BASE58
// ==========================================================

const ALPHABET =
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
    "abcdefghijkmnopqrstuvwxyz";


function base58Decode(value) {

    let num = 0n;

    for (const char of value) {

        const index =
            ALPHABET.indexOf(char);

        if (index < 0) {

            throw new Error(
                "Carattere Base58 non valido: "
                + char
            );
        }

        num =
            num * 58n
            + BigInt(index);
    }

    let hex =
        num.toString(16);

    if (hex.length % 2) {

        hex =
            "0" + hex;
    }

    let bytes =
        hex === "0"
            ? Buffer.alloc(0)
            : Buffer.from(
                hex,
                "hex"
            );

    let zeros = 0;

    for (
        const char of value
    ) {

        if (char !== "1") {

            break;
        }

        zeros++;
    }

    if (zeros > 0) {

        bytes = Buffer.concat([
            Buffer.alloc(zeros),
            bytes
        ]);
    }

    return new Uint8Array(
        bytes
    );
}


function base58Encode(bytes) {

    const data =
        Buffer.from(bytes);

    let num = 0n;

    for (
        const byte of data
    ) {

        num =
            num * 256n
            + BigInt(byte);
    }

    let result = "";

    while (num > 0n) {

        const remainder =
            Number(
                num % 58n
            );

        result =
            ALPHABET[remainder]
            + result;

        num =
            num / 58n;
    }

    let zeros = 0;

    for (
        const byte of data
    ) {

        if (byte !== 0) {

            break;
        }

        zeros++;
    }

    return (
        "1".repeat(zeros)
        + result
    );
}


// ==========================================================
// KEY PARSING
// ==========================================================

function parsePrivateKey(value) {

    const clean =
        String(value || "")
            .trim();

    // ------------------------------------------------------
    // Base58
    // ------------------------------------------------------

    try {

        const decoded =
            base58Decode(clean);

        if (
            decoded.length === 32
            || decoded.length === 64
        ) {

            return decoded;
        }

    } catch (_) {
        // prova HEX
    }

    // ------------------------------------------------------
    // HEX
    // ------------------------------------------------------

    let hex = clean;

    if (
        hex.startsWith("0x")
    ) {

        hex =
            hex.slice(2);
    }

    if (
        /^[0-9a-fA-F]+$/.test(hex)
        && hex.length % 2 === 0
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
            || decoded.length === 64
        ) {

            return decoded;
        }
    }

    throw new Error(
        "Chiave Solana non riconosciuta."
    );
}


// ==========================================================
// SOLANA SIGNER
// ==========================================================

async function createSolanaSigner(
    privateKey
) {

    let keyBytes =
        parsePrivateKey(
            privateKey
        );

    /*
     * 64 byte:
     *
     * [0..31]  = seed/private key
     * [32..63] = public key
     */

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
            "Private key Solana non valida: "
            + keyBytes.length
            + " byte."
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


// ==========================================================
// SOLANA AUTHORIZATION
// ==========================================================

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
        !== req.senderAddress
    ) {

        throw new Error(
            "La Solana private key NON "
            + "corrisponde al senderAddress. "
            + "Derivato="
            + signer.address
            + " richiesto="
            + req.senderAddress
        );
    }

    /*
     * Messaggio Sorare per
     * SolanaTokenTransferAuthorizationRequest.
     */

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
        "📝 Solana message costruito"
    );

    const messageBytes =
        Buffer.from(
            message,
            "utf8"
        );

    const hash =
        crypto
            .createHash("sha256")
            .update(messageBytes)
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
        signatures[0][
            signer.address
        ];

    const signature =
        base58Encode(
            signatureBytes
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


// ==========================================================
// STARK / MANGOPAY
// ==========================================================

function signStark(
    auth
) {

    const req =
        auth.request;

    const signature =
        signAuthorizationRequest(
            input.starkPrivateKey,
            req
        );

    if (
        req.__typename
        ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                auth.fingerprint,

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
        req.__typename
        ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {

        return {

            fingerprint:
                auth.fingerprint,

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
        req.__typename
        ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                auth.fingerprint,

            mangopayWalletTransferApproval: {

                nonce:
                    req.nonce,

                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + req.__typename
    );
}


// ==========================================================
// MAIN
// ==========================================================

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
            type
            ===
            "SolanaTokenTransferAuthorizationRequest"
        ) {

            approvals.push(
                await signSolana(
                    auth
                )
            );

        } else {

            approvals.push(
                signStark(
                    auth
                )
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

        [
            node,
            "-e",
            js
        ],

        input=json.dumps({

            "starkPrivateKey":
                STARK,

            "solanaPrivateKey":
                SOLANA,

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
# PREPARE OFFER
#
# IMPORTANTE:
#
# NON usiamo più:
#
#     "type": "SINGLE_SALE_OFFER"
#
# perché il tuo schema attuale restituisce:
#
#     Field is not defined on prepareOfferInput
#
# settlementCurrencies viene invece mantenuto come "EUR".
# ============================================================

PREPARE_QUERY = """
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
    price
):

    # ========================================================
    # CORREZIONE PRINCIPALE
    #
    # NIENTE "type".
    #
    # settlementCurrencies = "EUR"
    #
    # Non ["EUR"].
    # ========================================================

    prepare_input = {

        "sendAssetIds": [
            asset
        ],

        "receiveAssetIds": [],

        "settlementCurrencies":
            "EUR",

        "receiveAmount": {

            "amount":
                str(price),

            "currency":
                "EUR"
        },

        "clientMutationId":
            new_id()
    }

    print(
        "📦 prepareOffer → "
        "settlementCurrencies=EUR",
        flush=True
    )

    data = gql(
        PREPARE_QUERY,
        {
            "input":
                prepare_input
        }
    )

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

        return None

    errors = (
        result.get(
            "errors"
        )
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

        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )

        return None

    print(
        f"✅ Authorization ricevute: "
        f"{len(authorizations)}",
        flush=True
    )

    return authorizations


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(
    card,
    price
):

    asset = asset_id(
        card
    )

    if not asset:

        return None

    if DRY_RUN:

        print(
            f"🟡 DRY RUN → "
            f"{label(card)} → "
            f"{eur(price)}",
            flush=True
        )

        return "DRY-RUN"

    # ========================================================
    # PREPARE
    # ========================================================

    authorizations = prepare_sale(
        asset,
        price
    )

    if not authorizations:

        return None

    # ========================================================
    # SIGN
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
    # CREATE
    #
    # IMPORTANTE:
    #
    # settlementCurrencies = "EUR"
    #
    # perché il tuo endpoint sta chiedendo
    # esplicitamente Settlement currencies.
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

    create_input = {

        "approvals":
            approvals,

        "dealId":
            new_id(),

        "assetId":
            asset,

        "settlementCurrencies":
            "EUR",

        "receiveAmount": {

            "amount":
                str(price),

            "currency":
                "EUR"
        },

        "clientMutationId":
            new_id()
    }

    print(
        "📤 createSingleSaleOffer → "
        "settlementCurrencies=EUR",
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
        result.get(
            "errors"
        )
        or []
    )

    if errors:

        error_text = " | ".join(
            str(
                e.get("message")
                or ""
            )
            for e in errors
            if isinstance(
                e,
                dict
            )
        )

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        # ====================================================
        # CASO IMPORTANTISSIMO
        #
        # La carta è già in vendita.
        #
        # NON deve tornare DA_VENDERE.
        #
        # Altrimenti il worker la riprova ogni ciclo.
        # ====================================================

        if (
            "an active public offer already exists"
            in error_text.lower()
        ):

            print(
                "🟢 CARTA GIÀ IN VENDITA "
                "SU SORARE",
                flush=True
            )

            return {
                "already_selling":
                    True,

                "error":
                    error_text
            }

        return None

    offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    offer_id = offer.get(
        "id"
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

    asset = asset_id(
        card
    )

    print(
        f"\n💰 AUTOSELL CHECK → "
        f"{asset}",
        flush=True
    )

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

            "FLOOR_LOW":
                "FLOOR SOTTO €0.32",

            "FLOOR_HIGH":
                "FLOOR SOPRA €0.70"
        }

        print(
            f"🚫 ESCLUSA → "
            f"{messages.get(result, result)}",
            flush=True
        )

        if result in {
            "KULENOVIC",
            "RARITY"
        }:

            update_card(
                asset,
                status="BLOCKED",
                error=result
            )

        else:

            update_card(
                asset,
                status="da_vendere",
                error=result
            )

        return

    price = result

    print(
        f"✅ CARTA VALIDA → "
        f"floor {eur(price)}",
        flush=True
    )

    # ========================================================
    # LOCK
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
    # CREATE
    # ========================================================

    offer_result = create_sale(
        details,
        price
    )

    # ========================================================
    # CARTA GIÀ IN VENDITA
    # ========================================================

    if (
        isinstance(
            offer_result,
            dict
        )
        and
        offer_result.get(
            "already_selling"
        )
    ):

        error_text = (
            offer_result.get(
                "error"
            )
            or "ACTIVE_PUBLIC_OFFER"
        )

        update_card(
            asset,
            status="SELLING",
            error=(
                "ALREADY_SELLING: "
                + error_text
            )
        )

        print(
            "🟢 STATO → SELLING "
            "(offerta già presente)",
            flush=True
        )

        return

    # ========================================================
    # CREAZIONE FALLITA
    # ========================================================

    if not offer_result:

        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "🔁 Carta rimessa in "
            "DA_VENDERE",
            flush=True
        )

        return

    # ========================================================
    # SUCCESS
    # ========================================================

    offer_id = offer_result

    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        error=None
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(details)} | "
        f"{eur(price)} | "
        f"{offer_id}",
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
            "   └─ "
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
        f"📦 VERSIONE: {VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        "💰 RANGE: €0.32 - €0.70",
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
        "🟢 GIÀ IN VENDITA: "
        "NON RIPROVARE",
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

                    process(
                        card
                    )

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
            "€0.32-€0.70",

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

        "already_selling":
            "SKIP",

        "storage":
            STATE_FILE,

        "cards":
            len(cards),

        "da_vendere":
            sum(
                1
                for c in cards
                if norm(
                    c.get("status")
                )
                in {
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
                )
                == "selling"
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

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False
    )
