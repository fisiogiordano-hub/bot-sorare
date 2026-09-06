import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import requests


# ============================================================
# AUTOSELL - MODULO INDIPENDENTE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("AUTOSELL_DRY_RUN", "false").lower() == "true"

# Controllo ogni minuto
INTERVAL = int(os.getenv("AUTOSELL_INTERVAL", "60"))

TIMEOUT = 25

# ============================================================
# REGOLE AUTOSELL
# ============================================================

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70

LISTING_DAYS = 7
LISTING_SECONDS = 7 * 24 * 60 * 60

MIN_LIVE_LISTINGS = 5

# ============================================================
# KULENOVIC - MAI VENDERE
# ============================================================

KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# LOCK
# ============================================================

started = False
start_lock = threading.Lock()


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


def eur(cents):
    return f"€{cents / 100:.2f}"


def is_kulenovic(card):
    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KULENOVIC_ID:
        wanted.add(norm(KULENOVIC_ID))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


# ============================================================
# HTTP / GRAPHQL
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
        "User-Agent": "Sorare-AutoSell/2.0",
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for attempt in range(3):

        try:
            response = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 AutoSell Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code == 429:

                retry = response.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    retry = int(retry)
                except ValueError:
                    retry = attempt + 2

                retry = min(retry, 30)

                print(
                    f"⏳ AutoSell rate limit → "
                    f"attendo {retry}s",
                    flush=True,
                )

                time.sleep(retry)
                continue

            if response.status_code != 200:

                print(
                    f"❌ AutoSell HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:500]}",
                    flush=True,
                )

                time.sleep(attempt + 1)
                continue

            data = response.json()

            if data.get("errors"):

                print(
                    "❌ AutoSell GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                    )[:3000],
                    flush=True,
                )

            return data

        except Exception as exc:

            print(
                f"❌ AutoSell GraphQL: {exc}",
                flush=True,
            )

            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql("""
        query AutoSellAccount {
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
        or {}
    )

    if not user:
        print(
            "❌ AutoSell: account non verificato",
            flush=True,
        )
        return False

    print(
        f"✅ AutoSell account: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    print(
        "🔐 AutoSell Stark key: "
        + (
            "PRESENTE"
            if user.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True,
    )

    return True


# ============================================================
# GALLERY
# ============================================================

def get_cards(cursor=None):

    data = graphql("""
        query AutoSellCards($cursor: String) {
            currentUser {
                cards(after: $cursor) {
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
                            age
                        }

                        # Carta effettivamente utilizzata
                        # in una lineup So5 attiva/aperta
                        liveSo5Lineup {
                            id
                        }

                        openedSo5Lineups {
                            id
                        }

                        myMintedSingleSaleOffer {
                            id
                            startDate
                            endDate
                        }
                    }

                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        }
    """, {
        "cursor": cursor,
    })

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    cards = user.get("cards") or {}

    return (
        cards.get("nodes") or [],
        cards.get("pageInfo") or {},
    )


def all_cards():

    result = []
    cursor = None

    while True:

        cards, page = get_cards(cursor)

        result.extend(cards)

        if not page.get("hasNextPage"):
            break

        cursor = page.get("endCursor")

        if not cursor:
            break

    return result


# ============================================================
# CARTA SCHIERATA
# ============================================================

def is_in_lineup(card):

    live = card.get("liveSo5Lineup")

    if live and live.get("id"):
        return True, "LIVE_SO5_LINEUP"

    opened = card.get("openedSo5Lineups") or []

    if opened:
        for lineup in opened:

            if isinstance(lineup, dict) and lineup.get("id"):
                return True, "OPENED_SO5_LINEUP"

    return False, None


# ============================================================
# GIÀ IN VENDITA
# ============================================================

def already_listed(card):

    offer = card.get(
        "myMintedSingleSaleOffer"
    )

    return bool(
        offer
        and offer.get("id")
    )


# ============================================================
# FLOOR LIVE
# ============================================================

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
        query AutoSellLiveSales(
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
                            }
                        }
                    }
                }
            }
        }
    """, {
        "playerSlug": player_slug,
        "first": 50,
    })

    if not data or data.get("errors"):
        return None

    offers = (
        (
            ((data.get("data") or {})
            .get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        ).get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for other in cards:

            try:
                other_season = int(
                    other.get("seasonYear")
                )
            except (TypeError, ValueError):
                continue

            other_player = norm(
                (other.get("anyPlayer") or {})
                .get("slug")
            )

            other_rarity = norm(
                other.get("rarityTyped")
            )

            if (
                other_player != player_slug
                or other_rarity != rarity
                or other_season != season
            ):
                continue

            amounts = (
                (offer.get("receiverSide") or {})
                .get("amounts")
                or {}
            )

            try:
                price = int(
                    amounts.get("eurCents")
                )
            except (TypeError, ValueError):
                price = 0

            if price > 0:
                prices.append(price)

            break

    # ========================================================
    # SICUREZZA:
    # devono esserci almeno 5 inserzioni live
    # ========================================================

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"⚠️ {card_name(card)} → solo "
            f"{len(prices)} inserzioni live "
            f"(richieste {MIN_LIVE_LISTINGS})",
            flush=True,
        )

        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
# ============================================================

def check_card(card):

    name = card_name(card)

    # --------------------------------------------------------
    # 1. KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            f"🔒 {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: KULENOVIC PROTETTO",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 2. RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Motivo: rarità "
            f"{rarity or 'N/D'}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 3. CARTA SCHIERATA
    # --------------------------------------------------------

    in_lineup, lineup_type = is_in_lineup(card)

    if in_lineup:

        print(
            f"🏆 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Motivo: carta schierata "
            f"({lineup_type})",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 4. GIÀ IN VENDITA
    # --------------------------------------------------------

    if already_listed(card):

        print(
            f"⏳ {name} → già in vendita",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 5. FLOOR LIVE
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: floor live "
            "non disponibile",
            flush=True,
        )

        return None

    print(
        f"💰 {name} → Floor live: "
        f"{eur(floor)}",
        flush=True,
    )

    # --------------------------------------------------------
    # 6. FLOOR < €0.32
    # --------------------------------------------------------

    if floor < MIN_PRICE:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Motivo: floor {eur(floor)} "
            f"< minimo {eur(MIN_PRICE)}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 7. FLOOR > €0.70
    # --------------------------------------------------------

    if floor > MAX_PRICE:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Motivo: floor {eur(floor)} "
            f"> massimo {eur(MAX_PRICE)}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # OK
    # --------------------------------------------------------

    print(
        f"✅ {name} → VENDIBILE "
        f"a {eur(floor)}",
        flush=True,
    )

    return floor


# ============================================================
# FIRMA AUTORIZZAZIONI
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
        && r.amount != null
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
            script,
        ],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
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
# CREA INSERZIONE
# ============================================================

def create_listing(card, floor):

    asset_id = card.get("assetId")

    if not asset_id:

        print(
            "❌ AssetId mancante",
            flush=True,
        )

        return False

    # Doppia protezione Kulenovic
    if is_kulenovic(card):

        print(
            "🛡️ BLOCCO ASSOLUTO KULENOVIC",
            flush=True,
        )

        return False

    # Doppia protezione prezzo
    if floor < MIN_PRICE or floor > MAX_PRICE:

        print(
            "🛑 Prezzo fuori range → "
            "nessuna vendita",
            flush=True,
        )

        return False

    print(
        f"🟢 LISTING: {card_name(card)} "
        f"→ {eur(floor)} "
        f"→ {LISTING_DAYS} giorni",
        flush=True,
    )

    if DRY_RUN:

        print(
            "🟡 AUTOSELL_DRY_RUN=True "
            "→ vendita simulata",
            flush=True,
        )

        return True

    # ========================================================
    # PREPARE OFFER
    #
    # IMPORTANTE:
    # prepareOfferInput attuale NON contiene "type".
    # ========================================================

    prepare_input = {

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {
            "amount": str(floor),
            "currency": "EUR"
        },

        "clientMutationId": str(
            uuid.uuid4()
        )
    }

    data = graphql("""
        mutation AutoSellPrepareOffer(
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
                    }
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": prepare_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer → nessun risultato",
            flush=True,
        )

        return False

    errors = result.get("errors") or []

    if errors:

        print(
            "❌ prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False,
            ),
            flush=True,
        )

        return False

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:

        print(
            "❌ prepareOffer → "
            "nessuna authorization",
            flush=True,
        )

        return False

    # ========================================================
    # FIRMA
    # ========================================================

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as exc:

        print(
            f"❌ Firma vendita: {exc}",
            flush=True,
        )

        return False

    # ========================================================
    # CREATE SINGLE SALE
    #
    # duration = 604800 secondi = 7 giorni
    # ========================================================

    create_input = {

        "approvals": approvals,

        "dealId": str(
            uuid.uuid4()
        ),

        "assetId": asset_id,

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {
            "amount": str(floor),
            "currency": "EUR"
        },

        "duration": LISTING_SECONDS,

        "clientMutationId": str(
            uuid.uuid4()
        )
    }

    data = graphql("""
        mutation AutoSellCreateListing(
            $input: createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(input: $input) {

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
        "input": create_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:

        print(
            "❌ createSingleSaleOffer "
            "→ nessun risultato",
            flush=True,
        )

        return False

    errors = result.get("errors") or []

    if errors:

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False,
            ),
            flush=True,
        )

        return False

    offer = (
        result.get("tokenOffer")
        or {}
    )

    if not offer.get("id"):

        print(
            "❌ Listing non creata",
            flush=True,
        )

        return False

    print(
        f"✅ CARTA MESSA IN VENDITA: "
        f"{card_name(card)}",
        flush=True,
    )

    print(
        f"   ├─ Prezzo: {eur(floor)}",
        flush=True,
    )

    print(
        f"   ├─ Durata: {LISTING_DAYS} giorni",
        flush=True,
    )

    print(
        f"   ├─ Inizio: "
        f"{offer.get('startDate', 'N/D')}",
        flush=True,
    )

    print(
        f"   ├─ Fine: "
        f"{offer.get('endDate', 'N/D')}",
        flush=True,
    )

    print(
        f"   └─ ID: {offer.get('id')}",
        flush=True,
    )

    return True


# ============================================================
# CICLO
# ============================================================

def run_once():

    print(
        "\n================================",
        flush=True,
    )

    print(
        "🔄 AUTOSELL - CONTROLLO GALLERY",
        flush=True,
    )

    print(
        "================================",
        flush=True,
    )

    cards = all_cards()

    print(
        f"📦 Carte trovate: {len(cards)}",
        flush=True,
    )

    for card in cards:

        try:

            floor = check_card(card)

            if floor is None:
                continue

            create_listing(
                card,
                floor
            )

        except Exception as exc:

            print(
                f"❌ AutoSell errore "
                f"{card_name(card)}: {exc}",
                flush=True,
            )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 AUTOSELL AVVIATO",
        flush=True,
    )

    print(
        "📦 MODULO: INDIPENDENTE",
        flush=True,
    )

    print(
        f"🧪 AUTOSELL_DRY_RUN={DRY_RUN}",
        flush=True,
    )

    print(
        "🏆 RARITÀ: LIMITED",
        flush=True,
    )

    print(
        f"💰 FLOOR: {eur(MIN_PRICE)} - "
        f"{eur(MAX_PRICE)}",
        flush=True,
    )

    print(
        "💰 PREZZO = FLOOR LIVE",
        flush=True,
    )

    print(
        f"📊 INSERZIONI LIVE MINIME: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True,
    )

    print(
        f"⏱️ DURATA: {LISTING_DAYS} giorni",
        flush=True,
    )

    print(
        "🏆 CARTA SCHIERATA → NON VENDERE",
        flush=True,
    )

    print(
        "🔒 KULENOVIC → MAI VENDERE",
        flush=True,
    )

    print(
        "🆕 CARTE APPENA ACQUISTATE → "
        "INCLUSE",
        flush=True,
    )

    print(
        "🔄 INSERZIONE SCADUTA → "
        "RIMESSA IN VENDITA",
        flush=True,
    )

    if not check_account():
        return

    # Primo controllo immediato
    try:
        run_once()
    except Exception as exc:
        print(
            f"❌ AutoSell primo controllo: {exc}",
            flush=True,
        )

    while True:

        time.sleep(INTERVAL)

        try:
            run_once()

        except Exception as exc:

            print(
                f"❌ AutoSell worker: {exc}",
                flush=True,
            )


# ============================================================
# START PUBBLICO
# ============================================================

def start():

    global started

    with start_lock:

        if started:
            print(
                "ℹ️ AutoSell già avviato",
                flush=True,
            )
            return

        started = True

        threading.Thread(
            target=worker,
            name="autosell-worker",
            daemon=True,
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True,
        )
