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
PRIVATE_KEY = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

PORT = int(os.getenv("PORT", "10000"))

VERSION = "AUTOSell-9.0-SOLANA"

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

def empty_state():
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
        save_state(empty_state())


def load_state():
    ensure_state()

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return empty_state()

        data.setdefault("processed_offers", [])
        data.setdefault("acquired_cards", [])
        data.setdefault("pending_autobuys", [])
        data.setdefault("updated_at", int(time.time()))

        return data

    except Exception as e:
        print(
            f"❌ State read: {e}",
            flush=True
        )

        return empty_state()


def save_state(data):
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
            f"❌ State write: {e}",
            flush=True
        )

        try:
            os.remove(tmp)
        except Exception:
            pass

        return False


def get_cards():
    with state_lock:
        data = load_state()

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

        data = load_state()

        cards = data.get(
            "acquired_cards",
            []
        )

        for card in cards:

            if not isinstance(card, dict):
                continue

            if norm(asset_id(card)) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = offer_id

            if error is not None:
                card["last_error"] = error
            else:
                card["last_error"] = None

            if status == "SELLING":
                card["selling_at"] = now()

            data["acquired_cards"] = cards
            data["updated_at"] = int(time.time())

            return save_state(data)

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

        status = norm(
            card.get("status")
        )

        if status not in {
            "da_vendere",
            "ready"
        }:
            continue

        asset = asset_id(card)

        if not asset:
            continue

        result.append(dict(card))

    return result


# ============================================================
# SORARE
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

                wait = 2 + attempt * 2

                print(
                    f"⏳ Rate limit → "
                    f"attendo {wait}s",
                    flush=True
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    f"❌ Sorare: "
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

    return True


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset):

    data = gql("""
        query Card($ids: [String!]!) {
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
# PRICE
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
            response.json()
            ["rates"]
            ["EUR"]
        )

        return round(
            usd_cents * rate
        )

    except Exception as e:

        print(
            f"⚠️ USD/EUR: {e}",
            flush=True
        )

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
        (
            (
                (
                    data or {}
                ).get("data")
                or {}
            ).get("tokens")
            or {}
        ).get("liveSingleSaleOffers")
        or {}
    ).get("nodes") or []

    prices = []

    for offer in nodes:

        sender = (
            offer.get("senderSide")
            or {}
        )

        cards = (
            sender.get("anyCards")
            or []
        )

        for listed in cards:

            listed_player = (
                listed.get("anyPlayer")
                or {}
            )

            try:

                listed_season = int(
                    listed.get("seasonYear")
                )

            except Exception:
                continue

            if listed_season != season:
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
                    offer.get("receiverSide")
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
        norm(card.get("slug"))
        == norm(KULENOVIC_SLUG)
        or
        norm(card.get("assetId"))
        == norm(KULENOVIC_ASSET)
    )


def validate(card):

    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(
        card.get("rarityTyped")
    ).upper() != "LIMITED":

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
# NODE / SIGNING
# ============================================================

def node_binary():

    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    return node


def sign_authorizations(authorizations):

    node = node_binary()

    if not PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY mancante"
        )

    js = r"""
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const {
    createSignableMessage,
    getBase58Decoder,
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair
} = require("@solana/kit");

const {
    HDKey
} = require("micro-key-producer/slip10.js");


const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);


const SOLANA_PATH =
    "m/44'/501'/0'/0'";


async function deriveSolanaSigner(
    ethereumPrivateKey
) {

    const clean =
        ethereumPrivateKey
            .replace(/^0x/, "")
            .trim();

    if (!/^[0-9a-fA-F]{64}$/.test(clean)) {
        throw new Error(
            "SORARE_STARK_PRIVATE_KEY non valida: " +
            "servono 32 byte esadecimali"
        );
    }

    const seed = Buffer.from(
        clean,
        "hex"
    );

    const derived =
        HDKey
            .fromMasterSeed(seed)
            .derive(SOLANA_PATH);

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            derived.privateKey
        );

    return createSignerFromKeyPair(
        keyPair
    );
}


async function signSolana(auth) {

    const req = auth.request;

    const signer =
        await deriveSolanaSigner(
            input.privateKey
        );

    if (
        signer.address !==
        req.senderAddress
    ) {

        throw new Error(
            "Solana senderAddress non corrisponde " +
            "alla chiave derivata. " +
            "Derivata=" + signer.address +
            " richiesta=" + req.senderAddress
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


    const messageBytes =
        new TextEncoder().encode(
            message
        );


    const messageHash =
        await crypto.subtle.digest(
            "SHA-256",
            messageBytes
        );


    const signableMessage =
        createSignableMessage(
            new Uint8Array(messageHash)
        );


    const [signatures] =
        await signer.signMessages([
            signableMessage
        ]);


    const signature =
        getBase58Decoder().decode(
            signatures[signer.address]
        );


    return {
        fingerprint: auth.fingerprint,

        solanaTokenTransferApproval: {
            signature,
            nonce: req.nonce,
            expirationTimestamp:
                req.expirationTimestamp
        }
    };
}


function normalizeStarkRequest(req) {

    if (
        req.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        if (
            req.amount !== undefined &&
            req.amount !== null
        ) {
            req.amount = BigInt(
                req.amount
            );
        }
    }

    return req;
}


function signStark(auth) {

    const req =
        normalizeStarkRequest(
            auth.request
        );

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            req
        );


    if (
        req.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {
            fingerprint: auth.fingerprint,

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
            fingerprint: auth.fingerprint,

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
            fingerprint: auth.fingerprint,

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

    for (
        const auth of input.authorizations
    ) {

        if (
            !auth ||
            !auth.request
        ) {
            throw new Error(
                "AuthorizationRequest vuota"
            );
        }

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


main().catch(error => {

    console.error(
        error.stack || error
    );

    process.exit(1);
});
"""

    process = subprocess.run(
        [node, "-e", js],

        input=json.dumps({
            "privateKey": PRIVATE_KEY,
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

    except Exception as e:

        raise RuntimeError(
            "Output firma non valido: "
            + str(e)
        )


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


    # ========================================================
    # PREPARE
    #
    # IMPORTANT:
    # NON aggiungere settlementCurrencies.
    # ========================================================

    prepare_query = """
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


    prepare_input = {
        "type": "SINGLE_SALE_OFFER",

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
        prepare_query,
        {
            "input": prepare_input
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
        result.get("authorizations")
        or []
    )


    if not authorizations:

        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )

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
    # IMPORTANT:
    # NON aggiungere settlementCurrencies.
    # ========================================================

    create_query = """
        mutation CreateSingleSaleOffer(
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

        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },

        "clientMutationId": new_id()
    }


    data = gql(
        create_query,
        {
            "input": create_input
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


    token_offer = (
        result.get("tokenOffer")
        or {}
    )


    offer_id = token_offer.get("id")


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
# PROCESS
# ============================================================

def process(card):

    asset = asset_id(card)

    if not asset:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset}",
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
        f"🃏 {label(details)}",
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
            "🚫 ESCLUSA → "
            + messages.get(
                result,
                result
            ),
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
            "❌ Impossibile impostare SELLING",
            flush=True
        )

        return


    # ========================================================
    # CREATE SALE
    # ========================================================

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
            + asset_id(card)
            + " | offer="
            + str(
                card.get(
                    "sale_offer_id"
                )
            ),
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
        node_binary()

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

                    asset = asset_id(card)

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
        "status": "online",
        "bot": "autosell",
        "version": VERSION,

        "dry_run": DRY_RUN,

        "range": "€0.32-€0.70",

        "min_live_listings":
            MIN_LISTINGS,

        "rarity": "LIMITED",

        "age": "NOT_USED",

        "kulenovic": "NEVER_SELL",

        "coverage": "DISABLED",

        "storage": STATE_FILE,

        "cards": len(cards),

        "da_vendere": sum(
            1
            for c in cards
            if norm(
                c.get("status")
            ) in {
                "da_vendere",
                "ready"
            }
        ),

        "selling": sum(
            1
            for c in cards
            if norm(
                c.get("status")
            ) == "selling"
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
# START WORKER
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


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
