import os
import time
import uuid
import json
import subprocess
import shutil
import requests

# ============================================================
# AUTOSELL - MODULO INDIPENDENTE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv(
    "AUTOSELL_DRY_RUN", "true"
).lower() == "true"

INTERVAL = int(
    os.getenv("AUTOSELL_INTERVAL", "60")
)

TIMEOUT = 25

MIN_PRICE = 32
MAX_PRICE = 70

LISTING_DAYS = 7

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID", ""
).strip()

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# UTILITY
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


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


def eur(cents):
    return f"€{cents / 100:.2f}"


# ============================================================
# GRAPHQL
# ============================================================

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
        "User-Agent": "Sorare-AutoSell/1.1",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    try:
        r = requests.post(
            URL,
            json=payload,
            headers=headers(),
            timeout=TIMEOUT,
        )

        print(
            f"🌐 Sorare HTTP {r.status_code}",
            flush=True,
        )

        if r.status_code != 200:
            print(
                f"❌ HTTP: {r.text[:500]}",
                flush=True,
            )
            return None

        data = r.json()

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

    except Exception as e:
        print(
            f"❌ GraphQL: {e}",
            flush=True,
        )
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
# CARTE DELLA GALLERY
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
# CARTE SCHIERATE
#
# Sorare fornisce direttamente gli assetId delle carte
# impegnate nelle lineup Football live o upcoming.
#
# True  -> carta schierata
# False -> carta libera
# None  -> impossibile verificare
# ============================================================

def get_lineup_assets():
    data = graphql("""
        query MyLineupCards {
            currentUser {
                blockchainCardsInLineups(
                    sport: FOOTBALL
                )
            }
        }
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if user is None:
        print(
            "❌ AUTOSELL: impossibile verificare "
            "le carte schierate",
            flush=True,
        )
        return None

    assets = user.get(
        "blockchainCardsInLineups"
    )

    if assets is None:
        print(
            "❌ AUTOSELL: stato lineup non disponibile",
            flush=True,
        )
        return None

    return {
        norm(asset)
        for asset in assets
        if asset
    }


def lineup_status(card, lineup_assets):
    asset_id = norm(
        card.get("assetId")
    )

    if not asset_id:
        return None

    if lineup_assets is None:
        return None

    return asset_id in lineup_assets


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

            if (
                norm(
                    (other.get("anyPlayer") or {})
                    .get("slug")
                ) != player_slug
            ):
                continue

            if norm(
                other.get("rarityTyped")
            ) != rarity:
                continue

            if other_season != season:
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

    if not prices:
        return None

    return min(prices)


# ============================================================
# RANGE PREZZO
# ============================================================

def valid_price(floor):
    return (
        floor is not None
        and MIN_PRICE <= floor <= MAX_PRICE
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
# CONTROLLO CARTA
# ============================================================

def check_card(card, lineup_assets):

    name = card_name(card)

    # --------------------------------------------------------
    # KULENOVIC
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
    # CARTA SCHIERATA
    # --------------------------------------------------------

    status = lineup_status(
        card,
        lineup_assets
    )

    if status is None:
        print(
            f"🛡️ {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: impossibile verificare "
            "lo stato della lineup",
            flush=True,
        )

        return None

    if status:
        print(
            f"🏟️ {name} → NON VENDERE",
            flush=True,
        )

        print(
            "   └─ Motivo: CARTA SCHIERATA "
            "IN UNA LINEUP LIVE/UPCOMING",
            flush=True,
        )

        return None

    print(
        f"🟢 {name} → carta libera",
        flush=True,
    )

    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":
        print(
            f"🚫 {name} → esclusa",
            flush=True,
        )

        print(
            f"   └─ Motivo: rarità "
            f"{rarity or 'N/D'}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # GIÀ IN VENDITA
    # --------------------------------------------------------

    if already_listed(card):
        print(
            f"⏳ {name} → già in vendita",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:
        print(
            f"🚫 {name} → esclusa",
            flush=True,
        )

        print(
            "   └─ Motivo: floor live "
            "non disponibile",
            flush=True,
        )

        return None

    print(
        f"💰 {name} → Floor: {eur(floor)}",
        flush=True,
    )

    # --------------------------------------------------------
    # SOTTO MINIMO
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
    # SOPRA MASSIMO
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

    p = subprocess.run(
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

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        p.stdout
    )


# ============================================================
# METTI IN VENDITA
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
    # PROTEZIONE KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):
        print(
            "🛡️ BLOCCO ASSOLUTO KULENOVIC",
            flush=True,
        )
        return False

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    if not valid_price(floor):
        print(
            "🛑 Prezzo fuori range "
            "→ nessuna vendita",
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
            "🟡 DRY_RUN=True "
            "→ vendita simulata",
            flush=True,
        )
        return True

    # --------------------------------------------------------
    # PREPARE OFFER
    #
    # Nessun settlementInfo qui.
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

    except Exception as e:
        print(
            f"❌ Firma vendita: {e}",
            flush=True,
        )
        return False

    # --------------------------------------------------------
    # CREATE SINGLE SALE
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
        f"{card_name(card)}",
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
# CICLO AUTOSELL
# ============================================================

def run_once():

    print(
        "\n==============================",
        flush=True,
    )

    print(
        "🔄 AUTOSELL - CONTROLLO GALLERY",
        flush=True,
    )

    print(
        "==============================",
        flush=True,
    )

    # --------------------------------------------------------
    # VERIFICA LINEUP UNA SOLA VOLTA
    # --------------------------------------------------------

    lineup_assets = get_lineup_assets()

    if lineup_assets is None:
        print(
            "🛑 AUTOSELL BLOCCATO: "
            "impossibile verificare le carte schierate",
            flush=True,
        )
        return

    print(
        f"🏟️ Carte impegnate in lineup "
        f"live/upcoming: {len(lineup_assets)}",
        flush=True,
    )

    # --------------------------------------------------------
    # GALLERY
    # --------------------------------------------------------

    cards = all_cards()

    print(
        f"📦 Carte trovate: {len(cards)}",
        flush=True,
    )

    for card in cards:

        try:

            floor = check_card(
                card,
                lineup_assets
            )

            if floor is None:
                continue

            # ------------------------------------------------
            # ULTIMO BLOCCO DI SICUREZZA
            # ------------------------------------------------

            if lineup_status(
                card,
                lineup_assets
            ):
                print(
                    f"🛡️ BLOCCO FINALE: "
                    f"{card_name(card)} "
                    f"risulta schierata",
                    flush=True,
                )
                continue

            create_listing(
                card,
                floor
            )

        except Exception as e:

            print(
                f"❌ Errore {card_name(card)}: {e}",
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
        f"💰 RANGE: "
        f"{eur(MIN_PRICE)} - "
        f"{eur(MAX_PRICE)}",
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
        "🏟️ CARTE SCHIERATE: BLOCCATE",
        flush=True,
    )

    print(
        "🏆 LIMITED soltanto",
        flush=True,
    )

    print(
        "💰 PREZZO = FLOOR LIVE",
        flush=True,
    )

    print(
        "🛡️ FALLBACK SICURO: "
        "SE LINEUP NON VERIFICABILE → NON VENDERE",
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

        except Exception as e:
            print(
                f"❌ AUTOSELL: {e}",
                flush=True,
            )

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
