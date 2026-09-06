import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify


# ============================================================
# AUTOSELL SORARE - MODULO INDIPENDENTE
# LIMITED ONLY - NESSUN LIMITE DI ETÀ
# ============================================================

app = Flask(__name__)

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()


# ============================================================
# SICUREZZA
# ============================================================

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


# ============================================================
# PARAMETRI AUTOSELL
# ============================================================

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70

MIN_LIVE_LISTINGS = 5

LISTING_DURATION = 7 * 24 * 60 * 60   # 7 giorni

INTERVAL = 15
TIMEOUT = 30

BOT_VERSION = "26.0-AUTOSELL-LIMITED-NO-AGE"


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

KID = os.getenv("KULENOVIC_ID", "").strip()


# ============================================================
# STATO
# ============================================================

worker_started = False
worker_lock = threading.Lock()

processed = set()
processed_lock = threading.Lock()


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
                f"🌐 Sorare HTTP {response.status_code}",
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
                    f"⏳ Rate limit → attendo {wait}s",
                    flush=True
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    f"❌ HTTP: "
                    f"{response.text[:1000]}",
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

    cursor = None
    page = 0

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
            "after": cursor
        })

        if not data or data.get("errors"):
            return None

        user = (
            ((data.get("data") or {}).get("currentUser"))
            or {}
        )

        cards_data = (
            user.get("cards")
            or {}
        )

        cards = (
            cards_data.get("nodes")
            or []
        )

        if cards:

            print(
                f"📄 Gallery pagina {page}: "
                f"{len(cards)} carte",
                flush=True
            )

            all_cards.extend(cards)

        page_info = (
            cards_data.get("pageInfo")
            or {}
        )

        if not page_info.get("hasNextPage"):
            break

        next_cursor = page_info.get(
            "endCursor"
        )

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    # ========================================================
    # FILTRO RARITÀ PRIMA DEL CONTROLLO DELLE CARTE
    # ========================================================

    limited_cards = [
        card
        for card in all_cards
        if norm(
            card.get("rarityTyped")
        ) == "limited"
    ]

    print(
        f"📦 Carte gallery totali: "
        f"{len(all_cards)}",
        flush=True
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
        return None

    if data.get("errors"):
        return None

    user = (
        ((data.get("data") or {}).get("currentUser"))
        or {}
    )

    values = user.get(
        "blockchainCardsInLineups"
    )

    if values is None:
        return None

    return {
        norm(x)
        for x in values
        if x
    }


def card_in_lineup(card, lineup_ids):

    if lineup_ids is None:
        return None

    asset_id = norm(
        card.get("assetId")
    )

    if not asset_id:
        return None

    return asset_id in lineup_ids


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

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
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

    # EUR
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

    # USD
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

    # WEI volutamente ignorato
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

    if not rarity:
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
                market_slug == player_slug
                and market_rarity == rarity
                and market_season == season
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

def validate_card(
    card,
    lineup_ids
):

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        return False, {
            "code": "KULENOVIC",
            "message": (
                "KULENOVIC MAI IN VENDITA"
            )
        }

    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    # Sicurezza aggiuntiva:
    # anche se la funzione viene chiamata
    # accidentalmente con una Common,
    # NON viene mai venduta.

    if rarity != "LIMITED":

        return False, {
            "code": "RARITY",
            "message": (
                "rarità diversa da LIMITED"
            ),
            "rarity": rarity or "N/D"
        }

    # --------------------------------------------------------
    # NESSUN CONTROLLO ETÀ
    # --------------------------------------------------------
    #
    # QUALSIASI ETÀ È CONSENTITA.
    #
    # --------------------------------------------------------

    # --------------------------------------------------------
    # LINEUP
    # --------------------------------------------------------

    in_lineup = card_in_lineup(
        card,
        lineup_ids
    )

    if in_lineup is None:

        return False, {
            "code": "LINEUP_UNKNOWN",
            "message": (
                "impossibile verificare "
                "le lineup"
            )
        }

    if in_lineup:

        return False, {
            "code": "LINEUP",
            "message": (
                "carta presente in una "
                "lineup live/upcoming"
            )
        }

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

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

        print(
            "   └─ Motivo: verifica fallita",
            flush=True
        )

        return

    code = info.get("code")

    if code == "KULENOVIC":

        print(
            "   └─ Motivo: KULENOVIC "
            "MAI IN VENDITA",
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
# FIRMA AUTORIZZAZIONI
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

function sign(a) {

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

                signature
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

                signature
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

                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(sign)
    )
);
'''

    process = subprocess.run(
        [
            node,
            "-e",
            script
        ],
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
# PREPARA VENDITA
# ============================================================

def prepare_sale(
    card,
    price
):

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
                    }
                }

                errors {
                    message
                }
            }
        }
    """, {

        "input": {

            "receiveAssetIds": [],

            "sendAssetIds": [
                asset_id
            ],

            "receiveAmount": {

                "amount": str(price),
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
            "❌ Nessuna authorization",
            flush=True
        )

        return None

    return authorizations


# ============================================================
# CREA LISTING
# ============================================================

def create_sale(
    card,
    price,
    approvals
):

    asset_id = str(
        card.get("assetId")
        or ""
    ).strip()

    deal_id = str(
        uuid.uuid4()
    )

    data = graphql("""
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
    """, {

        "input": {

            "approvals":
                approvals,

            "assetId":
                asset_id,

            "dealId":
                deal_id,

            "duration":
                LISTING_DURATION,

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
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        return False

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

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    if not token_offer.get("id"):

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

    label = card_label(card)

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

    label = card_label(card)

    print(
        f"\n🔎 CONTROLLO: {label}",
        flush=True
    )

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
            f"⏳ {label} → già in vendita",
            flush=True
        )

        return

    # --------------------------------------------------------
    # VALIDAZIONE
    # --------------------------------------------------------

    valid, info = validate_card(
        card,
        lineup_ids
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
        "📦 MODULO: INDIPENDENTE",
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
        "🎂 LIMITE ETÀ: NESSUNO",
        flush=True
    )

    print(
        "🏟️ CARTA SCHIERATA: "
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

            # ------------------------------------------------
            # GALLERY
            # ------------------------------------------------

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

            # ------------------------------------------------
            # LINEUP
            # ------------------------------------------------

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
                    "NESSUNA CARTA VERRÀ VENDUTA",
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

            # ------------------------------------------------
            # PROCESSA SOLO LIMITED
            # ------------------------------------------------

            for card in cards:

                try:

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

                # Piccola pausa per non
                # martellare l'API

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

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "age_limit":
            "NONE",

        "listing_duration_seconds":
            LISTING_DURATION,

        "listing_duration_days":
            7,

        "kulenovic":
            "NEVER_SELL",

        "rarity":
            "LIMITED_ONLY",

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

        "age_limit":
            "NONE"
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
