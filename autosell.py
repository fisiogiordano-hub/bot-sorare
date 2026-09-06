import os
import time
import uuid
import json
import shutil
import subprocess
import requests

# ============================================================
# AUTOSELL - MODULO INDIPENDENTE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# IMPORTANTE:
# true  = simula soltanto
# false = mette realmente in vendita
DRY_RUN = os.getenv("AUTOSELL_DRY_RUN", "true").lower() == "true"

INTERVAL = int(os.getenv("AUTOSELL_INTERVAL", "60"))
TIMEOUT = 25

# ============================================================
# REGOLE AUTOSELL
# ============================================================

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70

MIN_LIVE_LISTINGS = 5

LISTING_DAYS = 7

# Solo LIMITED
ALLOWED_RARITY = "LIMITED"

# ============================================================
# KULENOVIC - PROTEZIONE ASSOLUTA
# ============================================================

KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def eur(cents):
    return f"€{cents / 100:.2f}"


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


def card_label(card):
    name = card_name(card)
    slug = card.get("slug")

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


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
        "User-Agent": "Sorare-AutoSell/1.0",
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
                f"🌐 Sorare HTTP {response.status_code}",
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

                time.sleep(min(retry, 15))
                continue

            if response.status_code != 200:

                print(
                    f"❌ HTTP {response.status_code}: "
                    f"{response.text[:500]}",
                    flush=True,
                )

                time.sleep(attempt + 1)
                continue

            data = response.json()

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                    )[:3000],
                    flush=True,
                )

            return data

        except Exception as exc:

            print(
                f"❌ GraphQL: {exc}",
                flush=True,
            )

            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def get_account():

    data = graphql("""
        query {
            currentUser {
                slug
                nickname
            }
        }
    """)

    return (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )


# ============================================================
# GALLERY
# ============================================================

def get_cards(cursor=None):

    data = graphql("""
        query MyCards($cursor: String) {

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
        "cursor": cursor
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
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KULENOVIC_ID:
        wanted.add(norm(KULENOVIC_ID))

    asset_id = norm(card.get("assetId"))
    slug = norm(card.get("slug"))

    return (
        asset_id in wanted
        or slug in wanted
    )


# ============================================================
# CARTA GIÀ IN VENDITA
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
# VERIFICA LINEUP
# ============================================================
#
# PRINCIPIO:
#
# 1. Se Sorare conferma che la carta è schierata:
#       BLOCCA vendita.
#
# 2. Se il controllo lineup non è disponibile:
#       BLOCCA vendita.
#
# Questo evita di vendere accidentalmente una carta
# schierata.
#
# ============================================================

def lineup_status(asset_id):

    if not asset_id:
        return "UNKNOWN"

    # --------------------------------------------------------
    # Tentativo di interrogazione delle lineup del currentUser.
    #
    # La struttura delle API Sorare può cambiare.
    # Se la query non è supportata, graphql() restituirà
    # un errore e consideriamo il risultato UNKNOWN.
    # --------------------------------------------------------

    data = graphql("""
        query CardLineups {

            currentUser {

                football {
                    lineups {
                        nodes {
                            cards {
                                assetId
                            }
                        }
                    }
                }
            }
        }
    """)

    if not data:
        return "UNKNOWN"

    if data.get("errors"):
        return "UNKNOWN"

    current = (
        ((data.get("data") or {})
        .get("currentUser"))
        or {}
    )

    football = current.get("football") or {}

    lineups = football.get("lineups") or {}

    nodes = lineups.get("nodes")

    if nodes is None:
        return "UNKNOWN"

    for lineup in nodes:

        for lineup_card in lineup.get("cards") or []:

            if norm(
                lineup_card.get("assetId")
            ) == norm(asset_id):

                return "STARTED"

    return "NOT_STARTED"


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

            same_player = (
                norm(
                    (other.get("anyPlayer") or {})
                    .get("slug")
                )
                == player_slug
            )

            same_rarity = (
                norm(
                    other.get("rarityTyped")
                )
                == rarity
            )

            same_season = (
                other_season == season
            )

            if not (
                same_player
                and same_rarity
                and same_season
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

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"🚫 {card_label(card)} → "
            f"solo {len(prices)}/{MIN_LIVE_LISTINGS} "
            f"inserzioni live",
            flush=True,
        )

        return None

    return min(prices)


# ============================================================
# VALIDAZIONE FLOOR
# ============================================================

def valid_price(floor):

    return (
        floor is not None
        and MIN_PRICE <= floor <= MAX_PRICE
    )


# ============================================================
# CONTROLLO CARTA
# ============================================================

def check_card(card):

    name = card_label(card)

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

    if rarity != ALLOWED_RARITY:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Motivo: rarità {rarity or 'N/D'}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 3. GIÀ IN VENDITA
    # --------------------------------------------------------

    if already_listed(card):

        print(
            f"⏳ {name} → già in vendita",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 4. LINEUP
    # --------------------------------------------------------

    lineup = lineup_status(
        card.get("assetId")
    )

    if lineup == "STARTED":

        print(
            f"🏟️ {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: CARTA SCHIERATA",
            flush=True,
        )

        return None

    if lineup == "UNKNOWN":

        print(
            f"🛡️ {name} → BLOCCATA",
            flush=True,
        )

        print(
            "   └─ Motivo: controllo lineup "
            "non verificabile",
            flush=True,
        )

        return None

    print(
        f"✅ {name} → carta NON schierata",
        flush=True,
    )

    # --------------------------------------------------------
    # 5. FLOOR
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: floor live non disponibile "
            f"o meno di {MIN_LIVE_LISTINGS} inserzioni",
            flush=True,
        )

        return None

    print(
        f"💰 {name} → Floor: {eur(floor)}",
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
    # 8. OK
    # --------------------------------------------------------

    print(
        f"🟢 {name} → VENDIBILE a {eur(floor)}",
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
# CREATE LISTING
# ============================================================

def create_listing(card, floor):

    asset_id = card.get("assetId")

    if not asset_id:

        print(
            "❌ AssetId mancante",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # BARRIERA KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            "🛡️ BLOCCO ASSOLUTO KULENOVIC",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # BARRIERA PREZZO
    # --------------------------------------------------------

    if not valid_price(floor):

        print(
            "🛑 Prezzo fuori range",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # BARRIERA LINEUP - SECONDO CONTROLLO
    # --------------------------------------------------------

    lineup = lineup_status(asset_id)

    if lineup != "NOT_STARTED":

        print(
            f"🛡️ LISTING BLOCCATO: "
            f"{card_label(card)}",
            flush=True,
        )

        print(
            f"   └─ Stato lineup: {lineup}",
            flush=True,
        )

        return False

    print(
        f"🟢 LISTING: {card_label(card)} "
        f"→ {eur(floor)} "
        f"→ {LISTING_DAYS} giorni",
        flush=True,
    )

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🟡 DRY RUN=True → vendita simulata",
            flush=True,
        )

        return True

    # --------------------------------------------------------
    # PREPARE OFFER
    # --------------------------------------------------------

    prepare_input = {

        "type": "SINGLE_SALE_OFFER",

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
            "❌ Nessuna authorization",
            flush=True,
        )

        return False

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

    # --------------------------------------------------------
    # CREATE SALE
    # --------------------------------------------------------

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

        "clientMutationId": str(
            uuid.uuid4()
        )
    }

    data = graphql("""
        mutation CreateSingleSaleOffer(
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
        f"{card_label(card)}",
        flush=True,
    )

    print(
        f"   ├─ Prezzo: {eur(floor)}",
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
                f"❌ Errore {card_label(card)}: {exc}",
                flush=True,
            )


# ============================================================
# START
# ============================================================

def main():

    print(
        "🤖 AUTOSELL AVVIATO",
        flush=True,
    )

    print(
        "📦 MODULO: INDIPENDENTE",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    print(
        f"💰 RANGE FLOOR: "
        f"{eur(MIN_PRICE)} - "
        f"{eur(MAX_PRICE)}",
        flush=True,
    )

    print(
        f"📊 INSERZIONI LIVE MINIME: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True,
    )

    print(
        f"⏱️ DURATA LISTING: "
        f"{LISTING_DAYS} giorni",
        flush=True,
    )

    print(
        "🔒 KULENOVIC: MAI IN VENDITA",
        flush=True,
    )

    print(
        "🏆 SOLO LIMITED",
        flush=True,
    )

    print(
        "🏟️ CARTA SCHIERATA: MAI IN VENDITA",
        flush=True,
    )

    print(
        "🛡️ CONTROLLO LINEUP NON VERIFICABILE "
        "→ BLOCCO VENDITA",
        flush=True,
    )

    print(
        "💰 PREZZO = FLOOR LIVE",
        flush=True,
    )

    user = get_account()

    if not user:

        print(
            "❌ Account Sorare non verificato",
            flush=True,
        )

        return

    print(
        f"✅ Account: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    while True:

        try:

            run_once()

        except Exception as exc:

            print(
                f"❌ AUTOSELL: {exc}",
                flush=True,
            )

        time.sleep(INTERVAL)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\n🛑 AUTOSELL ARRESTATO",
            flush=True,
        )

    except Exception as exc:

        print(
            f"❌ ERRORE FATALE AUTOSELL: {exc}",
            flush=True,
        )

        raise
