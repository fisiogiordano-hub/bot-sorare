import os
import time
import uuid
import json
import subprocess
import threading
import requests

from flask import Flask, jsonify


# ============================================================
# AUTOSELL SORARE
# SOLO VENDITA
# NIENTE AUTOBUY
# NIENTE SWAP
# SOLANA SAFE
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

# Private key Sorare/Ethereum esportata dal wallet Sorare.
#
# NON inserire la chiave direttamente nel codice.
# Configurarla nelle Environment Variables del deployment.
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
    ).lower()
    == "true"
)


# ============================================================
# PARAMETRI AUTOSELL
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

MIN_LIVE_LISTINGS = 5

LISTING_DURATION = (
    7 * 24 * 60 * 60
)

INTERVAL = 15
TIMEOUT = 30

BOT_VERSION = (
    "29.0-AUTOSELL-SOLANA-SAFE"
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
    return str(
        value or ""
    ).strip().lower()


def card_name(card):

    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


def card_label(card):

    name = card_name(card)

    rarity = card.get(
        "rarityTyped"
    )

    season = card.get(
        "seasonYear"
    )

    serial = card.get(
        "serialNumber"
    )

    parts = [name]

    if season:
        parts.append(
            str(season)
        )

    if rarity:
        parts.append(
            str(rarity)
        )

    if serial:
        parts.append(
            f"#{serial}"
        )

    return " • ".join(parts)


def format_eur(cents):

    if cents is None:
        return "N/D"

    return (
        f"€{cents / 100:.2f}"
    )


# ============================================================
# AUTH HTTP
# ============================================================

def headers():

    if not TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN "
            "non configurato"
        )

    token = TOKEN

    if not token.lower().startswith(
        "bearer "
    ):

        token = (
            "Bearer " + token
        )

    result = {

        "Authorization": token,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            f"Sorare-AutoSell/{BOT_VERSION}"
    }

    if AUD:

        result["JWT-AUD"] = AUD

    return result


# ============================================================
# GRAPHQL
# ============================================================

def graphql(
    query,
    variables=None
):

    payload = {

        "query": query,

        "variables":
            variables or {}
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

                retry_after = (
                    response.headers.get(
                        "Retry-After",
                        str(attempt + 2)
                    )
                )

                try:

                    wait = int(
                        retry_after
                    )

                except ValueError:

                    wait = (
                        attempt + 2
                    )

                wait = min(
                    wait,
                    15
                )

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
                    + response.text[:1000],
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
                    "❌ Risposta non JSON",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

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
                f"❌ GraphQL exception: "
                f"{exc}",
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

    data = graphql(
        """
        query CurrentUser {

            currentUser {

                slug

                nickname

                starkKey
            }
        }
        """
    )

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

        data = graphql(
            """
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
            """,
            {
                "first": 50,
                "after": after
            }
        )

        if not data:

            print(
                "❌ Gallery: "
                "risposta assente",
                flush=True
            )

            return None

        if data.get("errors"):

            print(
                "❌ Gallery: "
                "GraphQL error",
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
            f"📄 Gallery pagina "
            f"{page}: {len(nodes)} carte",
            flush=True
        )

        total_gallery_count += (
            len(nodes)
        )

        for card in nodes:

            if card.get(
                "sealed"
            ) is True:

                sealed_count += 1

                print(
                    f"🔒 {card_label(card)} "
                    f"→ IN CASSAFORTE, esclusa",
                    flush=True
                )

                continue

            all_cards.append(card)

        page_info = (
            cards_data.get(
                "pageInfo"
            )
            or {}
        )

        if not page_info.get(
            "hasNextPage"
        ):

            break

        after = page_info.get(
            "endCursor"
        )

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

        rarity = norm(
            card.get(
                "rarityTyped"
            )
        )

        if rarity == "limited":

            limited_cards.append(
                card
            )

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

    data = graphql(
        """
        query CardsInLineups {

            currentUser {

                blockchainCardsInLineups(
                    sport: FOOTBALL
                )
            }
        }
        """
    )

    if not data:

        print(
            "🛡️ LINEUP: "
            "risposta assente",
            flush=True
        )

        return None

    if data.get("errors"):

        print(
            "🛡️ LINEUP: "
            "GraphQL error",
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
            "🛡️ LINEUP: "
            "campo assente",
            flush=True
        )

        return None

    if not isinstance(
        values,
        list
    ):

        print(
            "🛡️ LINEUP: "
            "formato inatteso",
            flush=True
        )

        return None

    return {
        norm(value)
        for value in values
        if value
    }


# ============================================================
# IDENTIFICATORI
# ============================================================

def card_lineup_identifiers(
    card
):

    identifiers = set()

    asset_id = norm(
        card.get("assetId")
    )

    if asset_id:

        identifiers.add(
            asset_id
        )

    slug = norm(
        card.get("slug")
    )

    if slug:

        identifiers.add(
            slug
        )

    return identifiers


def card_in_lineup(
    card,
    lineup_ids
):

    if lineup_ids is None:

        return None

    identifiers = (
        card_lineup_identifiers(
            card
        )
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

        wanted.add(
            norm(KID)
        )

    identifiers = (
        card_lineup_identifiers(
            card
        )
    )

    return bool(
        identifiers.intersection(
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
# PREZZO EUR
# ============================================================

def price_eur(amounts):

    if not isinstance(
        amounts,
        dict
    ):

        return None

    try:

        eur = int(
            amounts.get(
                "eurCents"
            )
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
            amounts.get(
                "usdCents"
            )
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
                round(
                    usd * rate
                )
            )

    return None


# ============================================================
# FLOOR LIVE
# ============================================================

def live_floor(card):

    player = (
        card.get(
            "anyPlayer"
        )
        or {}
    )

    player_slug = norm(
        player.get(
            "slug"
        )
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

    except (
        TypeError,
        ValueError
    ):

        return None

    if not player_slug:

        return None

    if rarity != "limited":

        return None

    data = graphql(
        """
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
        """,
        {
            "playerSlug":
                player_slug,

            "first": 50
        }
    )

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
            .get(
                "liveSingleSaleOffers"
            )
            or {}
        )
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender_side = (
            offer.get(
                "senderSide"
            )
            or {}
        )

        cards = (
            sender_side.get(
                "anyCards"
            )
            or []
        )

        for market_card in cards:

            market_player = (
                market_card.get(
                    "anyPlayer"
                )
                or {}
            )

            market_slug = norm(
                market_player.get(
                    "slug"
                )
            )

            market_rarity = norm(
                market_card.get(
                    "rarityTyped"
                )
            )

            try:

                market_season = int(
                    market_card.get(
                        "seasonYear"
                    )
                )

            except (
                TypeError,
                ValueError
            ):

                continue

            if (
                market_slug
                == player_slug
                and market_rarity
                == rarity
                and market_season
                == season
            ):

                receiver_side = (
                    offer.get(
                        "receiverSide"
                    )
                    or {}
                )

                amounts = (
                    receiver_side.get(
                        "amounts"
                    )
                    or {}
                )

                price = price_eur(
                    amounts
                )

                if price is not None:

                    prices.append(
                        price
                    )

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

def validate_card(
    card,
    lineup_ids
):

    if card.get(
        "sealed"
    ) is True:

        return False, {

            "code":
                "VAULT",

            "message":
                "carta presente in Cassaforte"
        }

    if is_kulenovic(card):

        return False, {

            "code":
                "KULENOVIC",

            "message":
                "KULENOVIC MAI IN VENDITA"
        }

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        return False, {

            "code":
                "RARITY",

            "message":
                "rarità diversa da LIMITED",

            "rarity":
                rarity or "N/D"
        }

    in_lineup = card_in_lineup(
        card,
        lineup_ids
    )

    if in_lineup is None:

        return False, {

            "code":
                "LINEUP_UNKNOWN",

            "message":
                "impossibile verificare la lineup"
        }

    if in_lineup:

        return False, {

            "code":
                "LINEUP",

            "message":
                "carta presente in lineup"
        }

    floor = live_floor(
        card
    )

    if floor is None:

        return False, {

            "code":
                "PRICE_UNKNOWN",

            "message":
                "floor non verificabile"
        }

    if floor < MIN_PRICE:

        return False, {

            "code":
                "PRICE_LOW",

            "message":
                "floor sotto il minimo",

            "floor":
                floor
        }

    if floor > MAX_PRICE:

        return False, {

            "code":
                "PRICE_HIGH",

            "message":
                "floor sopra il massimo",

            "floor":
                floor
        }

    return True, {

        "rarity":
            rarity,

        "floor":
            floor
    }


# ============================================================
# LOG ESCLUSIONE
# ============================================================

def print_rejection(
    card,
    info
):

    label = card_label(
        card
    )

    print(
        f"🚫 {label} → esclusa",
        flush=True
    )

    if not info:

        print(
            "   └─ Motivo: "
            "verifica fallita",
            flush=True
        )

        return

    code = info.get(
        "code"
    )

    if code == "VAULT":

        print(
            "   └─ Motivo: "
            "CARTA IN CASSAFORTE",
            flush=True
        )

        print(
            "      Sicurezza: "
            "NON VENDERE",
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
            f"   └─ Motivo: "
            f"rarità {info.get('rarity')}",
            flush=True
        )

    elif code == "LINEUP":

        print(
            "   └─ Motivo: "
            "CARTA IN LINEUP",
            flush=True
        )

        print(
            "      Sicurezza: "
            "NON VENDERE",
            flush=True
        )

    elif code == "LINEUP_UNKNOWN":

        print(
            "   └─ Motivo: "
            "controllo lineup "
            "non verificabile",
            flush=True
        )

        print(
            "      Sicurezza: "
            "NON VENDERE",
            flush=True
        )

    elif code == "PRICE_UNKNOWN":

        print(
            "   └─ Motivo: "
            "prezzo live "
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
# NODE
# ============================================================

def shutil_which(
    name
):

    paths = os.getenv(
        "PATH",
        ""
    ).split(
        os.pathsep
    )

    for path in paths:

        candidate = os.path.join(
            path,
            name
        )

        if (
            os.path.isfile(candidate)
            and os.access(
                candidate,
                os.X_OK
            )
        ):

            return candidate

    return None


# ============================================================
# FIRMA SOLANA
#
# IMPORTANTE:
#
# NON usare:
#
# @sorare/crypto.signAuthorizationRequest
#
# per Solana.
#
# Sorare richiede:
#
# 1. derivazione SLIP-0010
# 2. m/44'/501'/0'/0'
# 3. messaggio TRANSFER
# 4. SHA-256
# 5. Ed25519
# 6. Base58
#
# ============================================================

def sign_solana_authorization(
    authorization
):

    node = shutil_which(
        "node"
    )

    if not node:

        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not PRIVATE_KEY:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    request = (
        authorization.get(
            "request"
        )
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

    # --------------------------------------------------------
    # CONTROLLO CAMPI OBBLIGATORI
    # --------------------------------------------------------

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
    # DEBUG
    # --------------------------------------------------------

    print(
        "🔐 FIRMA SOLANA",
        flush=True
    )

    print(
        "   ├─ senderAddress: "
        + str(
            request.get(
                "senderAddress"
            )
        ),
        flush=True
    )

    print(
        "   ├─ receiverAddress: "
        + str(
            request.get(
                "receiverAddress"
            )
        ),
        flush=True
    )

    print(
        "   ├─ merkleTreeAddress: "
        + str(
            request.get(
                "merkleTreeAddress"
            )
        ),
        flush=True
    )

    print(
        "   ├─ leafIndex: "
        + str(
            request.get(
                "leafIndex"
            )
        ),
        flush=True
    )

    print(
        "   ├─ nonce: "
        + str(
            request.get(
                "nonce"
            )
        ),
        flush=True
    )

    print(
        "   └─ expirationTimestamp: "
        + str(
            request.get(
                "expirationTimestamp"
            )
        ),
        flush=True
    )

    # --------------------------------------------------------
    # SCRIPT NODE
    # --------------------------------------------------------

    script = r'''
const crypto = require("crypto");

const {
    createSignableMessage,
    getBase58Decoder,
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair,
} = require("@solana/kit");

const {
    HDKey
} = require("micro-key-producer/slip10.js");


const SOLANA_DERIVATION_PATH =
    "m/44'/501'/0'/0'";


async function deriveSolanaSigner(
    ethereumPrivateKey
) {

    const cleanKey =
        ethereumPrivateKey
            .replace(/^0x/, "")
            .trim();

    if (!/^[0-9a-fA-F]{64}$/.test(cleanKey)) {

        throw new Error(
            "SORARE_STARK_PRIVATE_KEY " +
            "deve contenere 32 byte esadecimali"
        );
    }

    const seed = Buffer.from(
        cleanKey,
        "hex"
    );

    const result =
        HDKey
            .fromMasterSeed(seed)
            .derive(
                SOLANA_DERIVATION_PATH
            );

    const derivedPrivateKeyBytes =
        result.privateKey;

    if (
        !derivedPrivateKeyBytes ||
        derivedPrivateKeyBytes.length !== 32
    ) {

        throw new Error(
            "Derivazione Solana non valida"
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


async function main() {

    const input = JSON.parse(
        require("fs")
            .readFileSync(
                0,
                "utf8"
            )
    );

    const auth =
        input.authorization;

    const r =
        auth.request;

    const signer =
        await deriveSolanaSigner(
            input.privateKey
        );

    // --------------------------------------------------------
    // CONTROLLO CRITICO
    // --------------------------------------------------------

    if (
        signer.address !==
        r.senderAddress
    ) {

        throw new Error(
            "SOLANA SENDER MISMATCH. " +
            "Chiave derivata: " +
            signer.address +
            " | senderAddress Sorare: " +
            r.senderAddress
        );
    }

    // --------------------------------------------------------
    // MESSAGGIO ESATTO SORARE
    //
    // assetId NON entra nel messaggio.
    //
    // senderAddress NON entra nel messaggio.
    //
    // '0x' è letterale.
    // --------------------------------------------------------

    const message = [

        "TRANSFER",

        r.transferProxyProgramAddress,

        r.merkleTreeAddress,

        r.leafIndex.toString(),

        r.nonce.toString(),

        r.expirationTimestamp.toString(),

        r.receiverAddress,

        "0x",

        r.originator,

    ].join(":");


    // --------------------------------------------------------
    // UTF-8
    // --------------------------------------------------------

    const messageBytes =
        new TextEncoder().encode(
            message
        );


    // --------------------------------------------------------
    // SHA-256
    // --------------------------------------------------------

    const messageHash =
        await crypto.webcrypto.subtle.digest(
            "SHA-256",
            messageBytes
        );


    // --------------------------------------------------------
    // SIGNABLE MESSAGE
    // --------------------------------------------------------

    const signableMessage =
        createSignableMessage(
            new Uint8Array(
                messageHash
            )
        );


    // --------------------------------------------------------
    // ED25519
    // --------------------------------------------------------

    const [
        signatures
    ] =
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


    // --------------------------------------------------------
    // APPROVAL SORARE
    // --------------------------------------------------------

    const approval = {

        fingerprint:
            auth.fingerprint,

        solanaTokenTransferApproval: {

            signature:

                signature,

            nonce:

                r.nonce,

            expirationTimestamp:

                r.expirationTimestamp
        }
    };


    process.stdout.write(
        JSON.stringify(
            approval
        )
    );
}


main().catch(
    error => {

        console.error(
            error.stack ||
            error.message ||
            String(error)
        );

        process.exit(1);
    }
);
'''

    process = subprocess.run(

        [
            node,
            "-e",
            script
        ],

        input=json.dumps({

            "privateKey":
                PRIVATE_KEY,

            "authorization":
                authorization
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
            or "Firma Solana fallita"
        )

    output = (
        process.stdout
        .strip()
    )

    if not output:

        raise RuntimeError(
            "Node non ha restituito "
            "la firma"
        )

    try:

        approval = json.loads(
            output
        )

    except Exception:

        raise RuntimeError(
            "Output firma Solana "
            "non valido: "
            + output[:2000]
        )

    if not approval.get(
        "fingerprint"
    ):

        raise RuntimeError(
            "Approval senza fingerprint"
        )

    solana_approval = (
        approval.get(
            "solanaTokenTransferApproval"
        )
        or {}
    )

    if not solana_approval.get(
        "signature"
    ):

        raise RuntimeError(
            "Approval senza signature"
        )

    print(
        "   └─ ✅ Firma Solana generata",
        flush=True
    )

    return approval


# ============================================================
# SIGN AUTHORITIES
# ============================================================

def sign_authorizations(
    authorizations
):

    approvals = []

    for index, authorization in enumerate(
        authorizations
    ):

        request = (
            authorization.get(
                "request"
            )
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

def prepare_sale(
    card,
    price
):

    asset_id = str(
        card.get(
            "assetId"
        )
        or ""
    ).strip()

    if not asset_id:

        print(
            "❌ AssetId mancante",
            flush=True
        )

        return None

    data = graphql(
        """
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
        """,
        {
            "input": {

                "type":
                    "SINGLE_SALE_OFFER",

                "sendAssetIds": [
                    asset_id
                ],

                "receiveAssetIds": [],

                "receiveAmount": {

                    "amount":
                        str(price),

                    "currency":
                        "EUR"
                },

                "settlementCurrencies": [
                    "EUR"
                ],

                "clientMutationId":
                    str(uuid.uuid4())
            }
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

    print(
        "🔍 DEBUG AUTHORIZATIONS:",
        flush=True
    )

    print(
        f"   └─ Totale authorization: "
        f"{len(authorizations)}",
        flush=True
    )

    for i, auth in enumerate(
        authorizations
    ):

        request = (
            auth.get(
                "request"
            )
            or {}
        )

        print(
            f"   AUTH {i}:",
            flush=True
        )

        print(
            f"   ├─ fingerprint: "
            f"{auth.get('fingerprint')}",
            flush=True
        )

        print(
            f"   ├─ __typename: "
            f"{request.get('__typename')}",
            flush=True
        )

        print(
            f"   └─ request fields: "
            f"{list(request.keys())}",
            flush=True
        )

    if not authorizations:

        print(
            "❌ Nessuna authorization",
            flush=True
        )

        return None

    return authorizations


# ============================================================
# CREATE LISTING
# ============================================================

def create_sale(
    card,
    price,
    approvals
):

    asset_id = str(
        card.get(
            "assetId"
        )
        or ""
    ).strip()

    deal_id = str(
        uuid.uuid4()
    )

    data = graphql(
        """
        mutation CreateSingleSale(
            $input: createSingleSaleOfferInput!
        ) {

            createSingleSaleOffer(
                input: $input
            ) {

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
        """,
        {
            "input": {

                "approvals":
                    approvals,

                "dealId":
                    deal_id,

                "assetId":
                    asset_id,

                "receiveAmount": {

                    "amount":
                        str(price),

                    "currency":
                        "EUR"
                },

                "settlementCurrencies": [
                    "EUR"
                ],

                "clientMutationId":
                    str(uuid.uuid4())
            }
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

        return False

    errors = (
        result.get(
            "errors"
        )
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

        return False

    token_offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    if not token_offer.get(
        "id"
    ):

        print(
            "❌ Listing non creato",
            flush=True
        )

        return False

    print(
        f"✅ LISTING CREATO: "
        f"{token_offer.get('id')}",
        flush=True
    )

    print(
        f"   └─ Prezzo: "
        f"{format_eur(price)}",
        flush=True
    )

    return True


# ============================================================
# VENDITA
# ============================================================

def sell_card(
    card,
    price
):

    label = card_label(
        card
    )

    print(
        f"💰 AUTOSELL: {label}",
        flush=True
    )

    print(
        f"   └─ Floor live: "
        f"{format_eur(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # PROTEZIONE CASSAFORTE
    # --------------------------------------------------------

    if card.get(
        "sealed"
    ) is True:

        print(
            "🔒 CARTA IN CASSAFORTE → "
            "VENDITA BLOCCATA",
            flush=True
        )

        return False

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🟡 DRY_RUN=True → "
            "vendita SIMULATA",
            flush=True
        )

        return True

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    authorizations = prepare_sale(
        card,
        price
    )

    if not authorizations:

        return False

    # --------------------------------------------------------
    # FIRMA
    # --------------------------------------------------------

    try:

        approvals = (
            sign_authorizations(
                authorizations
            )
        )

    except Exception as exc:

        print(
            f"❌ Firma vendita: "
            f"{exc}",
            flush=True
        )

        return False

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    return create_sale(
        card,
        price,
        approvals
    )


# ============================================================
# PROCESSA CARTA
# ============================================================

def process_card(
    card,
    lineup_ids
):

    label = card_label(
        card
    )

    print(
        f"\n🔎 CONTROLLO: {label}",
        flush=True
    )

    # --------------------------------------------------------
    # VAULT
    # --------------------------------------------------------

    if card.get(
        "sealed"
    ) is True:

        print(
            f"🔒 {label} → "
            f"IN CASSAFORTE, SKIP",
            flush=True
        )

        return

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(
        card
    ):

        print(
            f"🛡️ {label} → "
            f"KULENOVIC, MAI IN VENDITA",
            flush=True
        )

        return

    # --------------------------------------------------------
    # GIÀ IN VENDITA
    # --------------------------------------------------------

    existing_offer = (
        card.get(
            "liveSingleSaleOffer"
        )
    )

    if existing_offer:

        print(
            f"⏳ {label} → "
            f"già in vendita",
            flush=True
        )

        return

    # --------------------------------------------------------
    # VALIDAZIONE
    # --------------------------------------------------------

    valid, info = (
        validate_card(
            card,
            lineup_ids
        )
    )

    if not valid:

        print_rejection(
            card,
            info
        )

        return

    # --------------------------------------------------------
    # VALIDATO
    # --------------------------------------------------------

    price = info.get(
        "floor"
    )

    print(
        f"✅ {label} → VENDIBILE",
        flush=True
    )

    print(
        f"   ├─ Rarità: "
        f"{info.get('rarity')}",
        flush=True
    )

    print(
        f"   └─ Floor live: "
        f"{format_eur(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    if sell_card(
        card,
        price
    ):

        print(
            f"🟢 {label} → "
            f"AUTOSELL COMPLETATO",
            flush=True
        )

    else:

        print(
            f"🔴 {label} → "
            f"AUTOSELL FALLITO",
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
        "📦 MODULO: SOLO AUTOSELL",
        flush=True
    )

    print(
        "🚫 AUTOBUY: DISABILITATO",
        flush=True
    )

    print(
        "🚫 SWAP: DISABILITATO",
        flush=True
    )

    print(
        f"📦 VERSIONE: "
        f"{BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN="
        f"{DRY_RUN}",
        flush=True
    )

    print(
        f"💰 RANGE FLOOR: "
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
        "🏟️ CARTA SCHIERATA: "
        "MAI IN VENDITA",
        flush=True
    )

    print(
        "🔒 CARTE IN CASSAFORTE: "
        "ESCLUSE",
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
        "🟣 SOLANA AUTH: ED25519",
        flush=True
    )

    print(
        "🔑 DERIVAZIONE SOLANA: "
        "SLIP-0010 "
        "m/44'/501'/0'/0'",
        flush=True
    )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():

        print(
            "🛑 AutoSell fermato",
            flush=True
        )

        return

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:

        try:

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

            cards = get_gallery()

            if cards is None:

                print(
                    "❌ Impossibile leggere "
                    "la gallery",
                    flush=True
                )

                time.sleep(
                    INTERVAL
                )

                continue

            lineup_ids = (
                get_lineup_asset_ids()
            )

            if lineup_ids is None:

                print(
                    "❌ Impossibile verificare "
                    "le lineup",
                    flush=True
                )

                print(
                    "🛡️ SICUREZZA: "
                    "NESSUNA CARTA "
                    "VERRÀ VENDUTA",
                    flush=True
                )

                time.sleep(
                    INTERVAL
                )

                continue

            print(
                f"🛡️ Carte in lineup "
                f"live/upcoming: "
                f"{len(lineup_ids)}",
                flush=True
            )

            for card in cards:

                try:

                    if card.get(
                        "sealed"
                    ) is True:

                        print(
                            f"🔒 "
                            f"{card_label(card)} "
                            f"→ IN CASSAFORTE, "
                            f"SKIP SICUREZZA",
                            flush=True
                        )

                        continue

                    if norm(
                        card.get(
                            "rarityTyped"
                        )
                    ) != "limited":

                        continue

                    process_card(
                        card,
                        lineup_ids
                    )

                except Exception as exc:

                    print(
                        f"❌ Errore carta "
                        f"{card_label(card)}: "
                        f"{exc}",
                        flush=True
                    )

                time.sleep(
                    0.25
                )

            print(
                f"😴 Prossimo controllo "
                f"tra {INTERVAL}s",
                flush=True
            )

            time.sleep(
                INTERVAL
            )

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

            name="autosell-worker",

            daemon=True

        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


# ============================================================
# HTTP
# ============================================================

@app.get("/")
def home():

    return jsonify({

        "status":
            "online",

        "bot":
            "sorare-autosell",

        "version":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "autosell":
            True,

        "autobuy":
            False,

        "swap":
            False,

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "age_filter":
            "DISABLED",

        "listing_duration_seconds":
            LISTING_DURATION,

        "listing_duration_days":
            7,

        "kulenovic":
            "NEVER_SELL",

        "rarity":
            "LIMITED_ONLY",

        "vault_cards":
            "EXCLUDED",

        "vault_field":
            "sealed",

        "lineup_check":
            "blockchainCardsInLineups",

        "lineup_unknown_action":
            "BLOCK_SELL",

        "price_source":
            "liveSingleSaleOffers",

        "price_match":
            "PLAYER_RARITY_SEASON",

        "price_currency":
            "EUR",

        "solana_authorization":
            "ED25519",

        "solana_derivation":
            "m/44'/501'/0'/0'",

        "solana_signature_encoding":
            "BASE58",

        "worker_started":
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
            BOT_VERSION,

        "worker_started":
            worker_started,

        "dry_run":
            DRY_RUN,

        "autosell":
            True,

        "autobuy":
            False,

        "swap":
            False,

        "rarity":
            "LIMITED_ONLY",

        "vault_cards":
            "EXCLUDED",

        "age_filter":
            "DISABLED",

        "lineup_unknown_action":
            "BLOCK_SELL",

        "solana_authorization":
            "ED25519"
    })


# ============================================================
# MAIN
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
        ),

        debug=False
    )
