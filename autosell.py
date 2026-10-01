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

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MAX_PRICE = 70
MIN_PRICE = 30
MIN_LISTINGS = 5

TECHNICAL_START_ETH = 0.0002
TECHNICAL_STEP_ETH = 0.0001

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

VERSION = "AUTOSell-15-AUTOBUY-SWAP"

ALLOWED_SOURCES = {
    "autobuy",
    "swap"
}

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

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
        "acquired_cards": [],
        "processed_offers": [],
        "pending_autobuys": [],
        "updated_at": int(time.time())
    }


def load_state():
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        save_state(default_state())

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_state()

        for key in default_state():
            data.setdefault(
                key,
                default_state()[key]
            )

        return data

    except Exception as e:
        print(
            f"❌ State: {e}",
            flush=True
        )

        return default_state()


def save_state(data):
    tmp = (
        STATE_FILE
        + "."
        + uuid.uuid4().hex
        + ".tmp"
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

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print(
            f"❌ Save state: {e}",
            flush=True
        )

        try:
            os.remove(tmp)
        except Exception:
            pass

        return False


def get_cards():
    with state_lock:
        cards = load_state().get(
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
    price=None,
    end_date=None,
    error=None
):
    wanted = norm(asset)

    with state_lock:
        data = load_state()

        for card in data["acquired_cards"]:

            if not isinstance(card, dict):
                continue

            if norm(asset_id(card)) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id is not None:
                card["sale_offer_id"] = offer_id

            if price is not None:
                card["sale_price_eur_cents"] = int(price)

            if end_date is not None:
                card["sale_end_date"] = end_date

            card["last_error"] = error
            card["updated_at"] = now()

            data["updated_at"] = int(time.time())

            return save_state(data)

    return False


def sellable_cards():
    result = []

    for card in get_cards():

        if not isinstance(card, dict):
            continue

        if norm(card.get("source")) not in ALLOWED_SOURCES:
            continue

        if norm(card.get("status")) not in {
            "da_vendere",
            "ready",
            "selling"
        }:
            continue

        if asset_id(card):
            result.append(dict(card))

    return result


# ============================================================
# GRAPHQL
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
                time.sleep(2 + attempt * 2)
                continue

            if response.status_code != 200:
                time.sleep(attempt + 1)
                continue

            data = response.json()

            if data.get("errors"):
                print(
                    "⚠️ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:1500],
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
        return False

    print(
        "✅ Sorare: "
        + str(
            user.get("nickname")
            or user.get("slug")
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
# ACTIVE OFFER
# ============================================================

def active_offer(asset):

    data = gql("""
        query ActiveOffer($assetId: String!) {
            anyCards(assetIds: [$assetId]) {
                assetId

                liveSingleSaleOffer {
                    id
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
    """, {
        "assetId": asset
    })

    if not data:
        return None

    if data.get("errors"):
        return None

    cards = (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )

    for card in cards:

        if norm(card.get("assetId")) != norm(asset):
            continue

        offer = card.get(
            "liveSingleSaleOffer"
        )

        if not offer:
            return None

        return offer

    return None


# ============================================================
# EXPIRATION / RECOVERY
# ============================================================

def parse_timestamp(value):

    if not value:
        return None

    text = str(value).strip()

    try:
        return time.mktime(
            time.strptime(
                text[:19],
                "%Y-%m-%dT%H:%M:%S"
            )
        )

    except Exception:
        return None


def expired_saved_sale(card):

    end_date = card.get(
        "sale_end_date"
    )

    if not end_date:
        return False

    timestamp = parse_timestamp(
        end_date
    )

    if timestamp is None:
        return False

    return time.time() >= timestamp


def recovery():

    for card in get_cards():

        if not isinstance(card, dict):
            continue

        if norm(card.get("source")) not in ALLOWED_SOURCES:
            continue

        if norm(card.get("status")) != "selling":
            continue

        asset = asset_id(card)

        if not asset:
            continue

        offer = active_offer(asset)

        # Offerta ancora attiva.
        if offer:

            offer_id = offer.get("id")

            end_date = offer.get("endDate")

            update_card(
                asset,
                status="selling",
                offer_id=offer_id,
                end_date=end_date
            )

            continue

        # Nessuna offerta attiva.
        # Ripubblica SOLO se abbiamo una scadenza
        # salvata e sappiamo che è terminata.
        if expired_saved_sale(card):

            old_price = card.get(
                "sale_price_eur_cents"
            )

            if old_price:

                print(
                    "🔄 EXPIRED: SAME PREVIOUS PRICE "
                    f"→ {eur(old_price)}",
                    flush=True
                )

                update_card(
                    asset,
                    status="da_vendere",
                    error=None
                )

        else:

            # Nessuna offerta e nessuna scadenza
            # affidabile: non toccare la carta.
            print(
                f"⚠️ Nessuna offerta attiva → "
                f"{asset} | nessuna scadenza certa",
                flush=True
            )


# ============================================================
# EUR
# ============================================================

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
        usd = int(
            amounts.get("usdCents") or 0
        )

        if usd <= 0:
            return None

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

        return max(
            1,
            round(usd * rate)
        )

    except Exception:
        return None


# ============================================================
# FLOOR
# ============================================================

def get_floor(card):

    player = card.get(
        "anyPlayer"
    ) or {}

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
        query Floor(
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

        sender = offer.get(
            "senderSide"
        ) or {}

        for listed in (
            sender.get("anyCards") or []
        ):

            player = listed.get(
                "anyPlayer"
            ) or {}

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
                player.get("slug")
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
                    ) or {}
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
        norm(card.get("slug"))
        == norm(KULENOVIC_SLUG)
        or
        norm(card.get("assetId"))
        == norm(KULENOVIC_ASSET)
    )


def validate(card):

    source = norm(
        card.get("source")
    )

    print(
        f"🏷️ SOURCE → {source}",
        flush=True
    )

    if source not in ALLOWED_SOURCES:
        return False, "SOURCE"

    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(
        card.get("rarityTyped")
    ).upper() != "LIMITED":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    price = max(
        floor,
        MIN_PRICE
    )

    return True, price


# ============================================================
# ETH
# ============================================================

def get_eth_eur():

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

        return float(
            response.json()["ethereum"]["eur"]
        )

    except Exception as e:

        print(
            f"❌ ETH/EUR: {e}",
            flush=True
        )

        return None


def eth_to_cents(
    eth,
    rate
):

    try:
        return max(
            1,
            round(
                float(eth)
                * float(rate)
                * 100
            )
        )
    except Exception:
        return None


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


def prepare_sale(
    asset,
    price
):

    data = gql(
        PREPARE_QUERY,
        {
            "input": {
                "sendAssetIds": [asset],
                "receiveAssetIds": [],
                "settlementCurrencies": ["EUR"],
                "receiveAmount": {
                    "amount": str(price),
                    "currency": "EUR"
                },
                "clientMutationId": new_id()
            }
        }
    )

    result = (
        ((data or {}).get("data") or {})
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

    return (
        result.get("authorizations")
        or []
    )


# ============================================================
# SIGN
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

    js = r'''
const crypto = require("node:crypto");
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

function base58Decode(value) {
    let number = 0n;

    for (const char of String(value).trim()) {
        const index = ALPHABET.indexOf(char);

        if (index < 0) {
            throw new Error(
                "Base58 non valido"
            );
        }

        number =
            number * 58n
            + BigInt(index);
    }

    let bytes = Buffer.alloc(0);

    if (number > 0n) {
        let hex = number.toString(16);

        if (hex.length % 2) {
            hex = "0" + hex;
        }

        bytes = Buffer.from(hex, "hex");
    }

    let zeros = 0;

    for (
        const char of String(value).trim()
    ) {
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

function base58Encode(data) {
    const bytes = Buffer.from(data);
    let number = 0n;

    for (const byte of bytes) {
        number =
            number * 256n
            + BigInt(byte);
    }

    let result = "";

    while (number > 0n) {
        const index =
            Number(number % 58n);

        result =
            ALPHABET[index]
            + result;

        number /= 58n;
    }

    let zeros = 0;

    for (const byte of bytes) {
        if (byte !== 0) break;
        zeros++;
    }

    return (
        "1".repeat(zeros)
        + result
    );
}

function parsePrivateKey(value) {
    const clean =
        String(value || "").trim();

    try {
        const decoded =
            base58Decode(clean);

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
        hex.length % 2 === 0 &&
        /^[0-9a-fA-F]+$/.test(hex)
    ) {
        const decoded =
            new Uint8Array(
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
        "Chiave Solana non valida"
    );
}

async function solanaSigner(privateKey) {
    let bytes =
        parsePrivateKey(privateKey);

    if (bytes.length === 64) {
        bytes = bytes.slice(0, 32);
    }

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            bytes
        );

    return createSignerFromKeyPair(
        keyPair
    );
}

async function signSolana(auth) {
    const req = auth.request;

    const signer =
        await solanaSigner(
            input.solanaPrivateKey
        );

    if (
        signer.address !==
        req.senderAddress
    ) {
        throw new Error(
            "Solana senderAddress non corrisponde"
        );
    }

    const message = [
        "TRANSFER",
        req.transferProxyProgramAddress,
        req.merkleTreeAddress,
        String(req.leafIndex),
        req.nonce,
        String(req.expirationTimestamp),
        req.receiverAddress,
        "0x",
        req.originator
    ].join(":");

    const hash =
        crypto
        .createHash("sha256")
        .update(
            Buffer.from(message, "utf8")
        )
        .digest();

    const signable =
        createSignableMessage(
            new Uint8Array(hash)
        );

    const signatures =
        await signer.signMessages([
            signable
        ]);

    const signature =
        signatures[0][signer.address];

    return {
        fingerprint: auth.fingerprint,

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
        "Authorization non supportata: "
        + req.__typename
    );
}

async function main() {
    const approvals = [];

    for (const auth of input.authorizations) {
        const type =
            (auth.request || {}).__typename;

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
        JSON.stringify(approvals)
    );
}

main().catch((error) => {
    console.error(
        error.stack || String(error)
    );

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
        return json.loads(
            process.stdout
        )
    except Exception:
        raise RuntimeError(
            "Output firma non valido"
        )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale_once(
    card,
    price
):

    asset = asset_id(card)

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → "
            f"{label(card)} → {eur(price)}",
            flush=True
        )

        return {
            "id": "DRY-RUN",
            "endDate": None
        }, None

    authorizations = prepare_sale(
        asset,
        price
    )

    if not authorizations:
        return None, "PREPARE_FAILED"

    try:
        approvals = sign_authorizations(
            authorizations
        )
    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True
        )
        return None, str(e)

    data = gql("""
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
    """, {
        "input": {
            "approvals": approvals,
            "dealId": new_id(),
            "assetId": asset,
            "settlementCurrencies": "EUR",
            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },
            "clientMutationId": new_id()
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        return None, "CREATE_FAILED"

    errors = result.get("errors") or []

    if errors:

        error_text = " ".join(
            str(e.get("message", ""))
            for e in errors
            if isinstance(e, dict)
        )

        if (
            "active public offer already exists"
            in norm(error_text)
        ):
            existing = active_offer(asset)

            if existing:
                return existing, None

            return None, "ALREADY_LISTED"

        return None, error_text

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:
        return None, "OFFER_ID_MISSING"

    return token_offer, None


# ============================================================
# SALE WITH PRICE RETRY
# ============================================================

def technical_error(text):

    text = norm(text)

    return (
        "price must be greater than" in text
        or
        "price must be at least" in text
        or
        "minimum price" in text
        or
        "min price" in text
    )


def create_sale(
    card,
    price
):

    print(
        f"🎯 PREZZO → {eur(price)}",
        flush=True
    )

    result, error = create_sale_once(
        card,
        price
    )

    if result:
        return result, price

    if not technical_error(error or ""):
        return None, None

    rate = get_eth_eur()

    if not rate:
        return None, None

    eth = TECHNICAL_START_ETH

    for _ in range(1000):

        technical_price = eth_to_cents(
            eth,
            rate
        )

        if technical_price is None:
            return None, None

        # Mai scendere sotto €0,30.
        technical_price = max(
            technical_price,
            MIN_PRICE
        )

        print(
            f"🔁 TECNICO → "
            f"{eth:.4f} ETH ≈ "
            f"{eur(technical_price)}",
            flush=True
        )

        result, error = create_sale_once(
            card,
            technical_price
        )

        if result:
            return result, technical_price

        if not technical_error(error or ""):
            return None, None

        eth = round(
            eth + TECHNICAL_STEP_ETH,
            4
        )

    return None, None


# ============================================================
# PROCESS
# ============================================================

def process(card):

    asset = asset_id(card)

    print(
        f"\n💰 AUTOSELL → {asset}",
        flush=True
    )

    source = norm(
        card.get("source")
    )

    print(
        f"🏷️ SOURCE → {source}",
        flush=True
    )

    # --------------------------------------------------------
    # SOURCE
    # --------------------------------------------------------

    if source not in ALLOWED_SOURCES:

        print(
            "🚫 SOURCE NON CONSENTITA",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error="SOURCE_NOT_ALLOWED"
        )

        return

    # --------------------------------------------------------
    # SE GIÀ IN VENDITA
    # --------------------------------------------------------

    current = active_offer(asset)

    if current:

        print(
            "🟢 GIÀ IN VENDITA → "
            f"{current.get('id')}",
            flush=True
        )

        price = amount_to_eur(
            (
                current.get(
                    "receiverSide"
                ) or {}
            ).get("amounts")
        )

        update_card(
            asset,
            status="selling",
            offer_id=current.get("id"),
            price=price,
            end_date=current.get("endDate"),
            error=None
        )

        return

    # --------------------------------------------------------
    # RECUPERA CARTA
    # --------------------------------------------------------

    details = card_details(asset)

    if not details:

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

    # --------------------------------------------------------
    # VALIDAZIONE
    # --------------------------------------------------------

    ok, result = validate(details)

    if not ok:

        print(
            f"🚫 ESCLUSA → {result}",
            flush=True
        )

        update_card(
            asset,
            status=(
                "BLOCKED"
                if result in {
                    "KULENOVIC",
                    "RARITY",
                    "SOURCE"
                }
                else "da_vendere"
            ),
            error=result
        )

        return

    price = result

    print(
        f"✅ CARTA VALIDA → "
        f"floor/prezzo {eur(price)}",
        flush=True
    )

    if price == MIN_PRICE:
        print(
            "🛡️ MINIMO €0.30 APPLICATO",
            flush=True
        )

    # --------------------------------------------------------
    # CREA VENDITA
    # --------------------------------------------------------

    update_card(
        asset,
        status="selling",
        price=price,
        error=None
    )

    result, used_price = create_sale(
        details,
        price
    )

    if not result:

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

    offer_id = result.get("id")
    end_date = result.get("endDate")

    if offer_id == "DRY-RUN":
        print(
            "🟡 DRY RUN",
            flush=True
        )

    else:
        print(
            f"🎉 VENDITA CREATA → "
            f"{offer_id} | "
            f"{eur(used_price)}",
            flush=True
        )

    update_card(
        asset,
        status="selling",
        offer_id=offer_id,
        price=used_price,
        end_date=end_date,
        error=None
    )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        f"🤖 AUTOSELL {VERSION}",
        flush=True
    )

    print(
        "🎯 SOURCE: AUTOBUY + SWAP",
        flush=True
    )

    print(
        "💰 FLOOR MAX: €0.70",
        flush=True
    )

    print(
        "💰 MIN PRICE: €0.30",
        flush=True
    )

    print(
        f"📊 LISTING MINIME: {MIN_LISTINGS}",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI VENDUTO",
        flush=True
    )

    print(
        "💶 SETTLEMENT: EUR",
        flush=True
    )

    print(
        "🔄 EXPIRED: SAME PREVIOUS PRICE",
        flush=True
    )

    try:
        auth_headers()
        load_state()

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

            for card in cards:

                try:
                    process(card)

                except Exception as e:

                    asset = asset_id(card)

                    print(
                        f"❌ {asset}: {e}",
                        flush=True
                    )

                    if asset:
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
        "sources": [
            "autobuy",
            "swap"
        ],
        "floor_max": "€0.70",
        "min_price": "€0.30",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "kulenovic": "NEVER_SELL",
        "settlement": "EUR",
        "expired": "SAME_PREVIOUS_PRICE",
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
            if norm(c.get("status"))
            == "selling"
        ),
        "worker": worker_started
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
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
