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

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()

# Private key Sorare/Ethereum già usata dal bot.
STARK = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()

# IMPORTANTE:
# La Solana key NON è una seconda private key obbligatoria.
# Sorare documenta che viene derivata dalla stessa private key
# Sorare/Ethereum usando SLIP-0010 + m/44'/501'/0'/0'
SOLANA_PRIVATE_KEY = os.getenv(
    "SORARE_SOLANA_PRIVATE_KEY",
    STARK
).strip()

KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = os.getenv(
    "DRY_RUN",
    "false"
).lower() == "true"

SWAP_AUTO_ACCEPT = os.getenv(
    "SWAP_AUTO_ACCEPT",
    "false"
).lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 25
MIN_LIVE_LISTINGS = 5

SWAP_MIN = 1.20
SWAP_MAX = 1.25

USD_CACHE = 300
COVERAGE_CACHE = 3600

BOT_VERSION = "39.0-SOLANA-SLIP10"

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

processed = set()

state_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()


def norm(v):
    return str(v or "").strip().lower()


def card_name(c):
    return c.get("name") or c.get("slug") or "Carta"


def card_label(c):
    name = card_name(c)
    slug = c.get("slug")

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


def mark_done(offer_id):
    with state_lock:
        processed.add(offer_id)


def should_process(offer_id):
    with state_lock:
        return offer_id not in processed


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
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):

    payload = {
        "query": query,
        "variables": variables or {}
    }

    for attempt in range(3):

        try:

            r = requests.post(
                URL,
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


def get_offers():

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

    return (
        user
        .get("pendingTokenOffersReceived", {})
        .get("nodes")
        or []
    )


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

    except (TypeError, ValueError):
        pass

    try:

        usd = float(
            amounts.get("usdCents")
        )

    except (TypeError, ValueError):

        usd = 0

    if usd > 0:

        rate = usd_eur()

        if rate:
            return int(
                round(usd * rate)
            )

    return None


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

    except (TypeError, ValueError):

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

        for c in (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        ):

            try:

                c_season = int(
                    c.get("seasonYear")
                )

            except (TypeError, ValueError):

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


def is_kulenovic(card):

    wanted = {
        KSLUG.lower(),
        KASSET.lower()
    }

    if KID:
        wanted.add(KID.lower())

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


def coverage_info(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active = [
        norm(c.get("slug"))
        for c in (
            club.get("activeCompetitions")
            or []
        )
        if (
            isinstance(c, dict)
            and c.get("slug")
        )
    ]

    coverage = load_coverage()

    if not active:
        return False, [], []

    covered = [
        x
        for x in active
        if x in coverage
    ]

    return (
        bool(covered),
        active,
        covered
    )


def validate_card(card):

    player = card.get("anyPlayer") or {}

    try:

        age = int(
            player.get("age")
        )

    except (TypeError, ValueError):

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

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        return False, {
            "code": "RARITY",
            "message":
                "rarità diversa da LIMITED",
            "rarity":
                rarity or "NON DISPONIBILE"
        }

    floor = live_floor(card)

    if floor is None:

        return False, {
            "code": "PRICE_UNKNOWN",
            "message":
                "prezzo live non disponibile "
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
        "active_competitions":
            active,
        "covered_competitions":
            covered_competitions
    }


def format_eur(cents):

    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


def print_card_rejection(
    card,
    info,
    context="AUTOBUY"
):

    print(
        f"🚫 {context} - Carta esclusa: "
        f"{card_label(card)}",
        flush=True
    )

    if not info:

        print(
            "   └─ Motivo: "
            "verifica non riuscita",
            flush=True
        )

        return

    code = info.get("code")

    if code == "AGE":

        print(
            "   ├─ Motivo: ETÀ FUORI LIMITE",
            flush=True
        )

        print(
            f"   ├─ Età carta: "
            f"{info.get('age')}",
            flush=True
        )

        print(
            f"   └─ Limite: < "
            f"{info.get('max_age')}",
            flush=True
        )

        return

    if code == "AGE_UNKNOWN":

        print(
            "   └─ Motivo: "
            "ETÀ NON DISPONIBILE",
            flush=True
        )

        return

    if code == "RARITY":

        print(
            "   ├─ Motivo: "
            "RARITÀ NON VALIDA",
            flush=True
        )

        print(
            f"   └─ Rarità trovata: "
            f"{info.get('rarity')}",
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
            f"   └─ Inserzioni live richieste: "
            f"{info.get('min_live_listings')}",
            flush=True
        )

        return

    if code == "PRICE_LOW":

        print(
            "   ├─ Motivo: "
            "FLOOR SOTTO IL MINIMO",
            flush=True
        )

        print(
            f"   ├─ Floor live: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Range consentito: "
            f"{format_eur(info.get('min_price'))} - "
            f"{format_eur(info.get('max_price'))}",
            flush=True
        )

        return

    if code == "PRICE_HIGH":

        print(
            "   ├─ Motivo: "
            "FLOOR SOPRA IL MASSIMO",
            flush=True
        )

        print(
            f"   ├─ Floor live: "
            f"{format_eur(info.get('floor'))}",
            flush=True
        )

        print(
            f"   └─ Range consentito: "
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
            info.get("active_competitions")
            or []
        )

        covered = (
            info.get("covered_competitions")
            or []
        )

        print(
            f"   ├─ Competizioni attive: "
            f"{', '.join(active) if active else 'nessuna'}",
            flush=True
        )

        print(
            f"   └─ Competizioni coperte: "
            f"{', '.join(covered) if covered else 'nessuna'}",
            flush=True
        )

        return

    print(
        f"   └─ Motivo: "
        f"{info.get('message', 'sconosciuto')}",
        flush=True
    )


def reject_offer(offer):

    blockchain_id = norm(
        offer.get("blockchainId")
    )

    if not blockchain_id:

        print(
            "❌ Impossibile rifiutare: "
            "blockchainId mancante",
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
            "blockchainId":
                blockchain_id,

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
        "✅ Offerta rifiutata",
        flush=True
    )

    return True


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

    if not SOLANA_PRIVATE_KEY:

        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY / "
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    # Questo script usa:
    #
    # @sorare/crypto
    # @solana/kit
    # micro-key-producer
    #
    # Per StarkEx:
    #     STARK private key
    #
    # Per Solana:
    #     stessa private key Sorare/Ethereum
    #     -> SLIP-0010
    #     -> m/44'/501'/0'/0'
    #     -> Ed25519
    #
    # NON viene usata @sorare/crypto per Solana.

    script = r'''
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const {
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair,
    createSignableMessage,
    getBase58Decoder
} = require("@solana/kit");

const {
    HDKey
} = require("micro-key-producer/slip10.js");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);


const SOLANA_DERIVATION_PATH =
    "m/44'/501'/0'/0'";


/*
 * ==========================================================
 * SOLANA KEYPAIR
 * ==========================================================
 *
 * Sorare:
 *
 * Ethereum/Sorare private key
 *        ↓
 * SLIP-0010
 *        ↓
 * m/44'/501'/0'/0'
 *        ↓
 * Ed25519 keypair
 */

async function deriveSolanaSigner(
    ethereumPrivateKey
) {

    if (!ethereumPrivateKey) {
        throw new Error(
            "Private key Solana mancante"
        );
    }

    const clean =
        ethereumPrivateKey
            .replace(/^0x/, "")
            .trim();

    if (!/^[0-9a-fA-F]{64}$/.test(clean)) {

        throw new Error(
            "La private key Sorare deve essere "
            + "32 byte esadecimali"
        );
    }

    const seed =
        Buffer.from(
            clean,
            "hex"
        );

    const derived =
        HDKey
            .fromMasterSeed(seed)
            .derive(
                SOLANA_DERIVATION_PATH
            );

    const derivedPrivateKeyBytes =
        derived.privateKey;

    if (!derivedPrivateKeyBytes) {

        throw new Error(
            "Derivazione SLIP-0010 fallita"
        );
    }

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            derivedPrivateKeyBytes
        );

    return createSignerFromKeyPair(
        keyPair
    );
}


/*
 * ==========================================================
 * STARKEX
 * ==========================================================
 */

function signStark(a) {

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

        r.amount = BigInt(
            r.amount
        );
    }

    const signature =
        signAuthorizationRequest(
            input.starkPrivateKey,
            r
        );

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            starkexTransferApproval: {

                nonce:
                    r.nonce,

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

            fingerprint:
                a.fingerprint,

            starkexLimitOrderApproval: {

                nonce:
                    r.nonce,

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

            fingerprint:
                a.fingerprint,

            mangopayWalletTransferApproval: {

                nonce:
                    r.nonce,

                signature
            }
        };
    }

    throw new Error(
        "Authorization Stark non supportata: "
        + r.__typename
    );
}


/*
 * ==========================================================
 * SOLANA TOKEN TRANSFER
 * ==========================================================
 *
 * IMPORTANTE:
 *
 * NON usiamo:
 *
 *     signAuthorizationRequest(...)
 *
 * per Solana.
 *
 * La procedura ufficiale Sorare è:
 *
 * 1. costruire il messaggio esatto
 * 2. UTF-8
 * 3. SHA-256
 * 4. Ed25519 sull'hash
 * 5. Base58
 */

async function signSolana(a) {

    const r = a.request;

    if (!r) {

        throw new Error(
            "Solana AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename !==
        "SolanaTokenTransferAuthorizationRequest"
    ) {

        throw new Error(
            "Tipo Solana inatteso: "
            + r.__typename
        );
    }

    console.log(
        "🔐 Solana authorization ricevuta"
    );

    console.log(
        "📨 senderAddress: "
        + (r.senderAddress || "N/D")
    );

    console.log(
        "📤 receiverAddress: "
        + (r.receiverAddress || "N/D")
    );

    console.log(
        "🌳 merkleTreeAddress: "
        + (r.merkleTreeAddress || "N/D")
    );

    console.log(
        "🔢 leafIndex: "
        + (r.leafIndex ?? "N/D")
    );

    console.log(
        "🔢 nonce: "
        + (r.nonce ?? "N/D")
    );

    console.log(
        "⏳ expirationTimestamp: "
        + (r.expirationTimestamp ?? "N/D")
    );

    console.log(
        "🔑 transferProxyProgramAddress: "
        + (
            r.transferProxyProgramAddress
            || "N/D"
        )
    );

    console.log(
        "👤 originator: "
        + (r.originator || "N/D")
    );


    if (!r.transferProxyProgramAddress) {

        throw new Error(
            "transferProxyProgramAddress mancante"
        );
    }

    if (!r.merkleTreeAddress) {

        throw new Error(
            "merkleTreeAddress mancante"
        );
    }

    if (
        r.leafIndex === null
        ||
        r.leafIndex === undefined
    ) {

        throw new Error(
            "leafIndex mancante"
        );
    }

    if (
        r.nonce === null
        ||
        r.nonce === undefined
    ) {

        throw new Error(
            "nonce mancante"
        );
    }

    if (
        r.expirationTimestamp === null
        ||
        r.expirationTimestamp === undefined
    ) {

        throw new Error(
            "expirationTimestamp mancante"
        );
    }

    if (!r.receiverAddress) {

        throw new Error(
            "receiverAddress mancante"
        );
    }

    if (!r.originator) {

        throw new Error(
            "originator mancante"
        );
    }


    /*
     * Questo è ESATTAMENTE il formato
     * richiesto da Sorare.
     *
     * assetId NON viene incluso.
     *
     * senderAddress NON viene incluso.
     *
     * '0x' è letterale.
     */

    const message = [

        "TRANSFER",

        r.transferProxyProgramAddress,

        r.merkleTreeAddress,

        r.leafIndex.toString(),

        r.nonce,

        r.expirationTimestamp.toString(),

        r.receiverAddress,

        "0x",

        r.originator

    ].join(":");


    console.log(
        "📝 Solana message costruito"
    );


    const textEncoder =
        new TextEncoder();

    const messageBytes =
        textEncoder.encode(
            message
        );


    /*
     * SHA-256 DEL MESSAGGIO
     *
     * Non si firma direttamente
     * la stringa.
     */

    const messageHash =
        await crypto.subtle.digest(
            "SHA-256",
            messageBytes
        );


    /*
     * La firma Ed25519 viene fatta
     * sull'hash SHA-256.
     */

    const signer =
        await deriveSolanaSigner(
            input.solanaPrivateKey
        );


    /*
     * senderAddress deve coincidere
     * con la keypair derivata.
     *
     * Questo controllo evita di inviare
     * firme sicuramente errate.
     */

    if (
        signer.address !==
        r.senderAddress
    ) {

        throw new Error(
            "DERIVAZIONE SOLANA ERRATA: "
            + "derived="
            + signer.address
            + " request="
            + r.senderAddress
        );
    }

    console.log(
        "✅ Solana sender verificato: "
        + signer.address
    );


    const signableMessage =
        createSignableMessage(
            new Uint8Array(
                messageHash
            )
        );


    const signatures =
        await signer.signMessages([
            signableMessage
        ]);


    const signature =
        getBase58Decoder().decode(
            signatures[
                signer.address
            ]
        );


    if (!signature) {

        throw new Error(
            "Firma Solana vuota"
        );
    }


    console.log(
        "✅ Firma Solana generata"
    );


    /*
     * NON modificare nonce
     * o expirationTimestamp.
     *
     * Devono essere quelli restituiti
     * dalla authorization.
     */

    return {

        fingerprint:
            a.fingerprint,

        solanaTokenTransferApproval: {

            signature,

            nonce:
                r.nonce,

            expirationTimestamp:
                r.expirationTimestamp
        }
    };
}


/*
 * ==========================================================
 * MAIN
 * ==========================================================
 */

async function main() {

    const result = [];

    if (
        !input.authorizations
        ||
        !Array.isArray(
            input.authorizations
        )
    ) {

        throw new Error(
            "authorizations mancanti"
        );
    }


    for (
        const a
        of input.authorizations
    ) {

        const type =
            a &&
            a.request &&
            a.request.__typename;


        console.log(
            "🔐 Authorization: "
            + type
        );


        if (
            type ===
            "SolanaTokenTransferAuthorizationRequest"
        ) {

            result.push(
                await signSolana(a)
            );

            continue;
        }


        if (
            type ===
            "StarkexTransferAuthorizationRequest"
            ||
            type ===
            "StarkexLimitOrderAuthorizationRequest"
            ||
            type ===
            "MangopayWalletTransferAuthorizationRequest"
        ) {

            result.push(
                signStark(a)
            );

            continue;
        }


        throw new Error(
            "Authorization non supportata: "
            + type
        );
    }


    process.stdout.write(
        JSON.stringify(result)
    );
}


main().catch(
    err => {

        console.error(err);

        process.exit(1);
    }
);
'''

    p = subprocess.run(

        [node, "-e", script],

        input=json.dumps({

            "starkPrivateKey":
                STARK,

            "solanaPrivateKey":
                SOLANA_PRIVATE_KEY,

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


    try:

        return json.loads(
            p.stdout
        )

    except Exception:

        raise RuntimeError(
            "Output firma non valido: "
            + p.stdout[-2000:]
        )


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
        return False

    amount = (
        len(ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 Controproposta: "
        f"{len(ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True
        )

        return True

    inp = {

        "receiveAssetIds":
            ids,

        "sendAssetIds":
            [],

        "sendAmount": {

            "amount":
                str(amount),

            "currency":
                "EUR"
        },

        "receiverSlug":
            receiver,

        "settlementCurrencies":
            ["EUR"],

        "clientMutationId":
            str(uuid.uuid4())
    }


    data = graphql("""
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
    """, {
        "input": inp
    })


    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )


    if not result:
        return False


    errors = result.get("errors") or []


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


    auth = (
        result.get("authorizations")
        or []
    )


    if not auth:

        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )

        return False


    try:

        approvals = sign_authorizations(
            auth
        )

    except Exception as e:

        print(
            f"❌ Firma: {e}",
            flush=True
        )

        return False


    create = dict(inp)

    create["approvals"] = approvals

    create["dealId"] = str(
        uuid.uuid4()
    )


    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {

                tokenOffer {
                    id
                    blockchainId
                    status
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": create
    })


    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )


    if not result:
        return False


    errors = result.get("errors") or []


    if errors:

        print(
            "❌ createDirectOffer errors:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )

        return False


    token_offer = (
        result.get("tokenOffer")
        or {}
    )


    if not token_offer.get("id"):
        return False


    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{token_offer['id']}",
        flush=True
    )

    return True


def process_autobuy(offer):

    offer_id = norm(
        offer.get("id")
    )

    if (
        not offer_id
        or not should_process(offer_id)
    ):
        return


    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )


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

        print(
            "🔴 AUTOBUY: "
            "nessuna carta ricevuta",
            flush=True
        )

        mark_done(offer_id)

        return


    print(
        f"\n📨 AUTOBUY {offer_id}",
        flush=True
    )


    details = card_details(ids)


    if len(details) != len(ids):

        print(
            "❌ AUTOBUY: impossibile verificare "
            "tutte le carte → RIFIUTO",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

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
                f"{format_eur(info.get('floor'))}",
                flush=True
            )

            valid.append(card)

        else:

            print_card_rejection(
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
            mark_done(offer_id)

        return


    if counter_offer(
        offer,
        valid
    ):

        if reject_offer(offer):
            mark_done(offer_id)


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

        print(
            "❌ Accept: "
            "exchangeRateId non disponibile",
            flush=True
        )

        return None, None


    settlement = {

        "currency":
            "WEI",

        "paymentMethod":
            "WALLET",

        "exchangeRateId":
            rate
    }


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
    """, {
        "input": {

            "offerId":
                offer_id,

            "settlementInfo":
                settlement
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
            "❌ prepareAcceptOffer errors:",
            json.dumps(
                errors,
                ensure_ascii=False
            )[:3000],
            flush=True
        )

        return None, None


    return (
        result.get("authorizations")
        or [],
        rate
    )


def accept_offer(offer):

    offer_id = norm(
        offer.get("id")
    )


    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "ACCEPT simulato",
            flush=True
        )

        return True


    auth, rate = prepare_accept(
        offer_id
    )


    if not auth:
        return False


    try:

        approvals = sign_authorizations(
            auth
        )

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

            "approvals":
                approvals,

            "offerId":
                offer_id,

            "settlementInfo": {

                "currency":
                    "WEI",

                "paymentMethod":
                    "WALLET",

                "exchangeRateId":
                    rate
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
            "❌ acceptOffer errors:",
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


    give = card_details(
        give_ids
    )


    receive = card_details(
        receive_ids
    )


    if (
        len(give) != len(give_ids)
        or
        len(receive) != len(receive_ids)
    ):

        print(
            "❌ SWAP: impossibile verificare "
            "tutte le carte → RIFIUTO",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return


    if any(
        is_kulenovic(c)
        for c in give
    ):

        print(
            "🔒 SWAP - Carta ceduta: KULENOVIC",
            flush=True
        )

        print(
            "   └─ Motivo: "
            "KULENOVIC NON È CEDIBILE",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return


    if any(
        is_kulenovic(c)
        for c in receiver_cards
    ):

        print(
            "🛡️ SWAP - KULENOVIC "
            "rilevato negli ID originali",
            flush=True
        )

        print(
            "   └─ Motivo: "
            "KULENOVIC NON È CEDIBILE",
            flush=True
        )

        print(
            "🔴 SWAP RIFIUTATO",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return


    total_given = 0


    for card in give:

        floor = live_floor(card)


        if floor is None:

            print_card_rejection(
                card,
                {
                    "code":
                        "PRICE_UNKNOWN",

                    "message":
                        "prezzo live "
                        "non disponibile",

                    "min_live_listings":
                        MIN_LIVE_LISTINGS
                },
                "SWAP - CARTA CEDUTA"
            )

            print(
                "🔴 SWAP RIFIUTATO",
                flush=True
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return


        print(
            f"📤 SWAP - Carta ceduta: "
            f"{card_label(card)}",
            flush=True
        )


        print(
            f"   └─ Floor live: "
            f"{format_eur(floor)}",
            flush=True
        )


        total_given += floor


    total_received = 0


    for card in receive:

        ok, info = validate_card(card)


        if not ok:

            print_card_rejection(
                card,
                info,
                "SWAP - CARTA RICEVUTA"
            )


            print(
                "🔴 SWAP RIFIUTATO",
                flush=True
            )


            if reject_offer(offer):
                mark_done(offer_id)

            return


        floor = info.get("floor")


        print(
            f"📥 SWAP - Carta ricevuta: "
            f"{card_label(card)}",
            flush=True
        )


        print(
            f"   └─ Floor live: "
            f"{format_eur(floor)}",
            flush=True
        )


        total_received += floor


    cash = price_eur(
        (offer.get("senderSide") or {})
        .get("amounts")
        or {}
    ) or 0


    total_received += cash


    if total_given <= 0:

        print(
            "🔴 SWAP RIFIUTATO: "
            "valore ceduto non valido",
            flush=True
        )

        mark_done(offer_id)

        return


    minimum = int(
        round(
            total_given * SWAP_MIN
        )
    )


    maximum = int(
        round(
            total_given * SWAP_MAX
        )
    )


    print(
        f"📤 Totale ceduto: "
        f"{format_eur(total_given)}",
        flush=True
    )


    print(
        f"📥 Totale ricevuto: "
        f"{format_eur(total_received)}",
        flush=True
    )


    print(
        f"💶 Cash già offerto: "
        f"{format_eur(cash)}",
        flush=True
    )


    print(
        f"🎯 Range richiesto: "
        f"{format_eur(minimum)} - "
        f"{format_eur(maximum)}",
        flush=True
    )


    if total_received < minimum:

        premium = (
            total_received
            / total_given
            - 1
        ) * 100


        print(
            "🔴 SWAP RIFIUTATO: "
            "valore ricevuto sotto +20%",
            flush=True
        )


        print(
            f"   ├─ Premium reale: "
            f"{premium:.2f}%",
            flush=True
        )


        print(
            f"   └─ Minimo richiesto: "
            f"+{(SWAP_MIN - 1) * 100:.0f}%",
            flush=True
        )


        if reject_offer(offer):
            mark_done(offer_id)

        return


    if total_received > maximum:

        premium = (
            total_received
            / total_given
            - 1
        ) * 100


        print(
            "🔴 SWAP RIFIUTATO: "
            "valore ricevuto sopra +25%",
            flush=True
        )


        print(
            f"   ├─ Premium reale: "
            f"{premium:.2f}%",
            flush=True
        )


        print(
            f"   └─ Massimo consentito: "
            f"+{(SWAP_MAX - 1) * 100:.0f}%",
            flush=True
        )


        if reject_offer(offer):
            mark_done(offer_id)

        return


    premium = (
        total_received
        / total_given
        - 1
    ) * 100


    print(
        f"✅ SWAP APPROVABILE: "
        f"+{premium:.2f}%",
        flush=True
    )


    if not SWAP_AUTO_ACCEPT:

        print(
            "🛑 SWAP_AUTO_ACCEPT=False "
            "→ nessuna azione",
            flush=True
        )

        mark_done(offer_id)

        return


    if accept_offer(offer):
        mark_done(offer_id)


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
        f"🔄 SWAP_AUTO_ACCEPT="
        f"{SWAP_AUTO_ACCEPT}",
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
        "💶 SWAP CASH: "
        "solo cash già offerto dal manager",
        flush=True
    )


    print(
        "🚫 SWAP NON aggiunge cash",
        flush=True
    )


    print(
        "💰 PRICE SOURCE: "
        "liveSingleSaleOffers",
        flush=True
    )


    print(
        "🎯 MATCH PRICE: "
        "player + rarity + season",
        flush=True
    )


    print(
        "💶 EUR: eurCents | "
        "USD: usdCents → EUR",
        flush=True
    )


    print(
        "🚫 WEI: escluso",
        flush=True
    )


    print(
        "🔒 KULENOVIC: MAI CEDIBILE",
        flush=True
    )


    print(
        "🎯 KULENOVIC RICHIESTO "
        "→ SEMPRE AUTOBUY",
        flush=True
    )


    print(
        "🛡️ DOPPIA BARRIERA "
        "KULENOVIC ATTIVA",
        flush=True
    )


    print(
        "📝 LOG MOTIVI ESCLUSIONE: ATTIVO",
        flush=True
    )


    print(
        "🔐 SOLANA AUTH: "
        "SLIP-0010 + Ed25519",
        flush=True
    )


    print(
        "🧬 SOLANA PATH: "
        "m/44'/501'/0'/0'",
        flush=True
    )


    coverage = load_coverage(
        force=True
    )


    if not coverage:

        print(
            "❌ Coverage non disponibile "
            "→ bot fermato",
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

            offers = get_offers()


            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True
            )


            for offer in offers:

                try:

                    process_offer(
                        offer
                    )

                except Exception as e:

                    print(
                        f"❌ Errore offerta: "
                        f"{e}",
                        flush=True
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


@app.get("/")
def home():

    with coverage_lock:

        covered = set(
            coverage_cache
        )


    return jsonify({

        "status":
            "online",

        "bot":
            "sorare",

        "version":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "swap_auto_accept":
            SWAP_AUTO_ACCEPT,

        "pay_per_card_cents":
            PAY_PER_CARD,

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "max_age":
            MAX_AGE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "swap_min_multiplier":
            SWAP_MIN,

        "swap_max_multiplier":
            SWAP_MAX,

        "swap_cash_mode":
            "MANAGER_OFFERED_CASH_ONLY",

        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

        "unknown_price_action":
            "REJECT_OR_EXCLUDE",

        "kulenovic":
            "NEVER_CEDIBLE",

        "kulenovic_requested":
            "ALWAYS_AUTOBUY",

        "rejection_reason_logging":
            "ENABLED",

        "solana_auth":
            "ENABLED",

        "solana_derivation":
            "SLIP-0010",

        "solana_derivation_path":
            "m/44'/501'/0'/0'",

        "covered_competitions_count":
            len(covered),

        "covered_competitions":
            sorted(covered),

        "coverage_source":
            COVERAGE_URL
    })


@app.get("/health")
def health():

    with coverage_lock:

        loaded = bool(
            coverage_cache
        )


    return jsonify({

        "status":
            "ok",

        "bot":
            "running",

        "version":
            BOT_VERSION,

        "worker_started":
            worker_started,

        "coverage_loaded":
            loaded,

        "dry_run":
            DRY_RUN,

        "swap_auto_accept":
            SWAP_AUTO_ACCEPT
    })


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
