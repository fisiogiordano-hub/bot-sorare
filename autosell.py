import os
import json
import time
import uuid
import threading
import subprocess
import requests

from flask import Flask, jsonify


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

SORARE_URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()

# StarkEx / Mangopay
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# Solana
SOLANA_KEY = os.getenv(
    "SORARE_SOLANA_PRIVATE_KEY",
    ""
).strip()

DRY_RUN = (
    os.getenv("DRY_RUN", "false").strip().lower()
    == "true"
)

INTERVAL = int(
    os.getenv("INTERVAL", "30")
)

TIMEOUT = int(
    os.getenv("TIMEOUT", "25")
)

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

VERSION = "AUTOSell-10.0-SOLANA-BASE58-FIX"

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4"
    "ac587bd3d40c7136796b6c0ed10ba0a6"
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

    return f"€{cents / 100:.2f}"


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

        data = load_document()

        cards = data.get(
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

        found = False

        for card in cards:

            if not isinstance(card, dict):
                continue

            if norm(
                asset_id(card)
            ) != wanted:
                continue

            found = True

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = (
                    offer_id
                )

            card["last_error"] = error

            if status == "SELLING":
                card["selling_at"] = now()

            break

        if not found:

            print(
                f"⚠️ Carta non presente nello state: "
                f"{asset}",
                flush=True
            )

            return False

        data["acquired_cards"] = cards
        data["updated_at"] = int(time.time())

        return save_document(data)


def sellable_cards():

    result = []

    for card in get_cards():

        if not isinstance(card, dict):
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
                    "❌ Sorare:",
                    response.text[:1500],
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            try:
                data = response.json()
            except Exception:
                print(
                    "❌ Risposta Sorare non JSON",
                    flush=True
                )
                return None

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

    print(
        "🔑 Solana private key: "
        + (
            "PRESENTE"
            if SOLANA_KEY
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
                listed_player.get(
                    "slug"
                )
            ) != player_slug:
                continue

            if norm(
                listed.get(
                    "rarityTyped"
                )
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

    return min(prices)


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

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor < MIN_PRICE:
        return False, "FLOOR_LOW"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, floor


# ============================================================
# SOLANA SIGNER
# ============================================================

def sign_authorizations(authorizations):

    node = (
        shutil_which("node")
        or shutil_which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not SOLANA_KEY:
        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY mancante"
        )

    js = r'''
const fs = require("fs");
const crypto = require("crypto");

const bs58 = require("bs58");
const nacl = require("tweetnacl");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);


function decodeBase58(value) {

    const result = bs58.decode(value);

    return Buffer.from(result);
}


function decodePrivateKey(value) {

    let key = String(value || "").trim();

    /*
     * --------------------------------------------------------
     * JSON ARRAY
     * --------------------------------------------------------
     */

    if (
        key.startsWith("[") &&
        key.endsWith("]")
    ) {

        let parsed;

        try {
            parsed = JSON.parse(key);
        } catch (e) {
            throw new Error(
                "SORARE_SOLANA_PRIVATE_KEY: JSON non valido"
            );
        }

        if (
            !Array.isArray(parsed) ||
            parsed.some(
                x =>
                    !Number.isInteger(x) ||
                    x < 0 ||
                    x > 255
            )
        ) {
            throw new Error(
                "SORARE_SOLANA_PRIVATE_KEY: array non valido"
            );
        }

        return Buffer.from(parsed);
    }


    /*
     * --------------------------------------------------------
     * HEX
     * --------------------------------------------------------
     */

    const hex = key.replace(/^0x/i, "");

    if (
        hex.length > 0 &&
        hex.length % 2 === 0 &&
        /^[0-9a-f]+$/i.test(hex)
    ) {

        return Buffer.from(
            hex,
            "hex"
        );
    }


    /*
     * --------------------------------------------------------
     * BASE58
     * --------------------------------------------------------
     */

    try {

        return decodeBase58(key);

    } catch (e) {

        throw new Error(
            "SORARE_SOLANA_PRIVATE_KEY: " +
            "formato non riconosciuto"
        );
    }
}


function makeKeyPair(raw) {

    /*
     * Solana secret key standard:
     * 64 bytes = seed/private 32 + public 32
     */

    if (raw.length === 64) {

        const seed = raw.subarray(
            0,
            32
        );

        return nacl.sign.keyPair.fromSeed(
            new Uint8Array(seed)
        );
    }


    /*
     * Seed Ed25519 puro
     */

    if (raw.length === 32) {

        return nacl.sign.keyPair.fromSeed(
            new Uint8Array(raw)
        );
    }


    throw new Error(
        "La Solana key decodificata contiene " +
        raw.length +
        " byte. Attesi 32 o 64 byte."
    );
}


function base58Encode(bytes) {

    return bs58.encode(
        Buffer.from(bytes)
    );
}


function signSolana(auth) {

    const req = auth.request;

    const raw = decodePrivateKey(
        input.privateKey
    );

    const keyPair = makeKeyPair(raw);

    const derivedAddress =
        base58Encode(
            keyPair.publicKey
        );


    /*
     * --------------------------------------------------------
     * SAFETY CHECK
     * --------------------------------------------------------
     */

    if (
        derivedAddress !==
        req.senderAddress
    ) {

        throw new Error(
            "Solana senderAddress non corrisponde " +
            "alla SORARE_SOLANA_PRIVATE_KEY. " +
            "Derivato=" +
            derivedAddress +
            " richiesto=" +
            req.senderAddress
        );
    }


    /*
     * --------------------------------------------------------
     * SORARE MESSAGE
     * --------------------------------------------------------
     *
     * NON aggiungere assetId.
     * NON aggiungere senderAddress.
     */

    const message = [
        "TRANSFER",
        req.transferProxyProgramAddress,
        req.merkleTreeAddress,
        String(req.leafIndex),
        String(req.nonce),
        String(req.expirationTimestamp),
        req.receiverAddress,
        "0x",
        req.originator
    ].join(":");


    /*
     * SHA-256
     */

    const hash = crypto
        .createHash("sha256")
        .update(
            Buffer.from(
                message,
                "utf8"
            )
        )
        .digest();


    /*
     * Ed25519
     */

    const signature =
        nacl.sign.detached(
            new Uint8Array(hash),
            keyPair.secretKey
        );


    /*
     * Sorare vuole la signature Base58
     */

    return {
        fingerprint:
            auth.fingerprint,

        solanaTokenTransferApproval: {
            signature:
                base58Encode(signature),

            nonce:
                req.nonce,

            expirationTimestamp:
                req.expirationTimestamp
        }
    };
}


function signStark(auth) {

    /*
     * Per le authorization Stark/Mangopay
     * usiamo @sorare/crypto.
     */

    const {
        signAuthorizationRequest
    } = require("@sorare/crypto");

    const req = auth.request;

    const signature =
        signAuthorizationRequest(
            input.starkPrivateKey,
            req
        );


    if (
        req.__typename ===
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
        req.__typename ===
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
        req.__typename ===
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
        "Authorization non supportata: " +
        req.__typename
    );
}


async function main() {

    const approvals = [];


    for (
        const auth of input.authorizations
    ) {

        const type =
            auth.request.__typename;

        console.error(
            "🔐 Authorization → " +
            type
        );


        if (
            type ===
            "SolanaTokenTransferAuthorizationRequest"
        ) {

            approvals.push(
                signSolana(auth)
            );

        } else {

            approvals.push(
                signStark(auth)
            );
        }
    }


    process.stdout.write(
        JSON.stringify(approvals)
    );
}


main().catch(error => {

    console.error(
        error.stack || error
    );

    process.exit(1);
});
'''

    process = subprocess.run(
        [
            node,
            "-e",
            js
        ],

        input=json.dumps({
            "privateKey": SOLANA_KEY,
            "starkPrivateKey": STARK,
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


def shutil_which(name):

    import shutil

    return shutil.which(name)


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


def prepare_offer(asset, price):

    # --------------------------------------------------------
    # PRIMA PROVA
    # Il tuo endpoint ha già dimostrato di rifiutare "type".
    # Quindi la variante compatibile è quella senza type.
    # --------------------------------------------------------

    input_without_type = {

        "sendAssetIds": [
            asset
        ],

        "receiveAssetIds": [],

        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },

        "clientMutationId": new_id()
    }


    data = gql(
        PREPARE_QUERY,
        {
            "input":
                input_without_type
        }
    )


    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )


    if result:

        errors = (
            result.get("errors")
            or []
        )

        authorizations = (
            result.get("authorizations")
            or []
        )

        if authorizations:

            print(
                f"✅ Authorization ricevute: "
                f"{len(authorizations)}",
                flush=True
            )

            return authorizations


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


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price):

    asset = asset_id(card)

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


    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    authorizations = prepare_offer(
        asset,
        price
    )


    if not authorizations:

        print(
            "❌ prepareOffer: "
            "nessun risultato",
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
            f"❌ Firma: {e}",
            flush=True
        )

        return None


    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

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

        "receiveAmount": {
            "amount":
                str(price),

            "currency":
                "EUR"
        },

        "clientMutationId":
            new_id()
    }


    data = gql(
        create_query,
        {
            "input":
                create_input
        }
    )


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

        return None


    errors = (
        result.get("errors")
        or []
    )


    if errors:

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return None


    offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = offer.get("id")


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


    # --------------------------------------------------------
    # LOCK
    # --------------------------------------------------------

    if not update_card(
        asset,
        status="SELLING",
        error=None
    ):

        print(
            "❌ Impossibile impostare SELLING",
            flush=True
        )

        return


    # --------------------------------------------------------
    # CREATE SALE
    # --------------------------------------------------------

    offer_id = create_sale(
        details,
        price
    )


    if not offer_id:

        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "🔁 Carta rimessa in DA_VENDERE",
            flush=True
        )

        return


    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

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
        f"💾 STORAGE: {STATE_FILE}",
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
