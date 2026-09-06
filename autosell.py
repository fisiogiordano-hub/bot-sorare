import os
import time
import uuid
import json
import shutil
import subprocess
import requests


# ============================================================
# AUTOSELL SORARE - MODULO INDIPENDENTE
# ============================================================
#
# AVVIO:
#     python autosell.py
#
# REGOLE:
# - KULENOVIC MAI IN VENDITA
# - CARTA SCHIERATA IN COMPETIZIONE = MAI IN VENDITA
# - SE LINEUP NON VERIFICABILE = BLOCCO VENDITA
# - SOLO LIMITED
# - MINIMO 5 INSERZIONI LIVE
# - FLOOR €0.32 - €0.70
# - PREZZO LISTING = FLOOR LIVE
# - CARTE APPENA ACQUISTATE INCLUSE
# - CARTE GIA' IN VENDITA = SKIP
#
# IMPORTANTE:
# NON usa currentUser.mySo5Lineups
#
# Il controllo lineup viene effettuato direttamente sulla carta
# tramite:
#   liveSo5Lineup
#   openedSo5Lineups
#
# ============================================================


# ============================================================
# CONFIGURAZIONE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# ATTENZIONE:
# Su Render imposta:
#
# AUTOSELL_DRY_RUN=false
#
# Per sicurezza il default resta TRUE.
DRY_RUN = os.getenv(
    "AUTOSELL_DRY_RUN",
    "true"
).lower() == "true"

INTERVAL = int(
    os.getenv(
        "AUTOSELL_INTERVAL",
        "60"
    )
)

TIMEOUT = int(
    os.getenv(
        "AUTOSELL_TIMEOUT",
        "30"
    )
)

# ============================================================
# REGOLE AUTOSELL
# ============================================================

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70

MIN_LIVE_LISTINGS = 5

LISTING_DAYS = 7

# ============================================================
# KULENOVIC PROTETTO
# ============================================================

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

KSLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# STATO
# ============================================================

last_cycle = 0


# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(
        value or ""
    ).strip().lower()


def eur(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


def card_label(card):
    name = card_name(card)

    rarity = (
        card.get("rarityTyped")
        or ""
    )

    season = (
        card.get("seasonYear")
        or ""
    )

    serial = (
        card.get("serialNumber")
        or ""
    )

    parts = [name]

    if season:
        parts.append(str(season))

    if rarity:
        parts.append(str(rarity).title())

    if serial:
        parts.append(
            f"{serial}/1000"
        )

    return " ".join(
        str(x)
        for x in parts
        if x
    )


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    if KULENOVIC_ID:
        wanted.add(
            norm(KULENOVIC_ID)
        )

    asset_id = norm(
        card.get("assetId")
    )

    slug = norm(
        card.get("slug")
    )

    return (
        asset_id in wanted
        or slug in wanted
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

    if not token.lower().startswith(
        "bearer "
    ):
        token = (
            "Bearer "
            + token
        )

    result = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Sorare-AutoSell/2.0"
        )
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


def graphql(
    query,
    variables=None
):

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
                f"🌐 Sorare HTTP "
                f"{response.status_code}",
                flush=True
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    delay = int(
                        retry_after
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    delay = attempt + 2

                delay = min(
                    delay,
                    30
                )

                print(
                    f"⏳ Rate limit → "
                    f"attendo {delay}s",
                    flush=True
                )

                time.sleep(delay)

                continue

            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    "❌ HTTP:",
                    response.text[:1000],
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = response.json()

            except Exception as e:

                print(
                    f"❌ JSON non valido: {e}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

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

            return data

        except Exception as e:

            print(
                f"❌ GraphQL request: {e}",
                flush=True
            )

            time.sleep(
                attempt + 1
            )

    return None


# ============================================================
# ACCOUNT
# ============================================================

def get_account():

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

    return user


# ============================================================
# CARTE GALLERY
# ============================================================

def get_cards(
    cursor=None
):

    data = graphql("""
        query AutoSellCards(
            $cursor: String
        ) {

            currentUser {

                cards(
                    after: $cursor
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

                        myMintedSingleSaleOffer {

                            id
                            startDate
                            endDate

                        }

                        # =================================================
                        # NUOVO CONTROLLO LINEUP
                        # =================================================
                        #
                        # NON usiamo:
                        # currentUser.mySo5Lineups
                        #
                        # Usiamo direttamente i campi della carta.
                        #

                        liveSo5Lineup {

                            id
                            hasLiveGames

                            so5Fixture {
                                slug
                            }

                        }

                        openedSo5Lineups {

                            id
                            hasLiveGames

                            so5Fixture {
                                slug
                            }

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

    if not data:
        return [], {}

    if data.get("errors"):
        return [], {}

    user = (
        ((data.get("data") or {})
        .get("currentUser"))
        or {}
    )

    cards = (
        user.get("cards")
        or {}
    )

    return (
        cards.get("nodes")
        or [],
        cards.get("pageInfo")
        or {}
    )


def all_cards():

    result = []

    cursor = None

    while True:

        cards, page = get_cards(
            cursor
        )

        if not cards and not page:
            break

        result.extend(
            cards
        )

        if not page.get(
            "hasNextPage"
        ):
            break

        cursor = page.get(
            "endCursor"
        )

        if not cursor:
            break

    return result


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
        query AutoSellLiveSales(
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

    tokens = (
        ((data.get("data") or {})
        .get("tokens"))
        or {}
    )

    sales = (
        tokens.get(
            "liveSingleSaleOffers"
        )
        or {}
    )

    offers = (
        sales.get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender_side = (
            offer.get("senderSide")
            or {}
        )

        cards = (
            sender_side.get(
                "anyCards"
            )
            or []
        )

        matched = False

        for other in cards:

            other_player = (
                other.get("anyPlayer")
                or {}
            )

            other_slug = norm(
                other_player.get(
                    "slug"
                )
            )

            if (
                other_slug
                != player_slug
            ):
                continue

            if norm(
                other.get(
                    "rarityTyped"
                )
            ) != rarity:
                continue

            try:

                other_season = int(
                    other.get(
                        "seasonYear"
                    )
                )

            except (
                TypeError,
                ValueError
            ):

                continue

            if other_season != season:
                continue

            amounts = (
                (
                    offer.get(
                        "receiverSide"
                    )
                    or {}
                ).get(
                    "amounts"
                )
                or {}
            )

            # ------------------------------------------------
            # SOLO EUR
            # ------------------------------------------------

            try:

                cents = int(
                    amounts.get(
                        "eurCents"
                    )
                )

            except (
                TypeError,
                ValueError
            ):

                cents = 0

            if cents > 0:
                prices.append(
                    cents
                )

            matched = True

            break

        if matched:
            continue

    # --------------------------------------------------------
    # ALMENO 5 INSERZIONI
    # --------------------------------------------------------

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"   └─ Floor non valido: "
            f"{len(prices)}/"
            f"{MIN_LIVE_LISTINGS} "
            f"inserzioni live",
            flush=True
        )

        return None

    return min(
        prices
    )


# ============================================================
# CONTROLLO LINEUP
# ============================================================

def check_lineup(card):

    """
    Ritorna:

        True
            carta schierata

        False
            carta non schierata

        None
            impossibile verificare

    IMPORTANTE:

    NON usa currentUser.mySo5Lineups.

    Usa direttamente:

        card.liveSo5Lineup
        card.openedSo5Lineups

    """

    try:

        live = card.get(
            "liveSo5Lineup"
        )

        opened = (
            card.get(
                "openedSo5Lineups"
            )
            or []
        )

    except Exception as e:

        print(
            f"   └─ ⚠️ Errore "
            f"lettura lineup: {e}",
            flush=True
        )

        return None

    # --------------------------------------------------------
    # LIVE LINEUP
    # --------------------------------------------------------

    if live:

        lineup_id = (
            live.get("id")
        )

        print(
            "   ├─ 🏟️ LIVE LINEUP: "
            f"PRESENTE"
            + (
                f" [{lineup_id}]"
                if lineup_id
                else ""
            ),
            flush=True
        )

        return True

    # --------------------------------------------------------
    # OPENED LINEUPS
    # --------------------------------------------------------

    if opened:

        valid_lineups = [
            x
            for x in opened
            if isinstance(
                x,
                dict
            )
            and x.get("id")
        ]

        if valid_lineups:

            print(
                "   ├─ 🏟️ LINEUP "
                f"SCHIERATA: "
                f"{len(valid_lineups)}",
                flush=True
            )

            return True

    # --------------------------------------------------------
    # NESSUNA LINEUP
    # --------------------------------------------------------

    print(
        "   ├─ 🏟️ Lineup: "
        "NESSUNA",
        flush=True
    )

    return False


# ============================================================
# CARTA GIA' IN VENDITA
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
# VERIFICA CARTA
# ============================================================

def check_card(card):

    label = card_label(
        card
    )

    print(
        f"\n🔎 CONTROLLO: {label}",
        flush=True
    )

    # ========================================================
    # KULENOVIC
    # ========================================================

    if is_kulenovic(card):

        print(
            "🔒 KULENOVIC → "
            "MAI IN VENDITA",
            flush=True
        )

        print(
            "   └─ Protezione assoluta",
            flush=True
        )

        return None

    # ========================================================
    # RARITY
    # ========================================================

    rarity = norm(
        card.get(
            "rarityTyped"
        )
    ).upper()

    if rarity != "LIMITED":

        print(
            f"🚫 {label} → esclusa",
            flush=True
        )

        print(
            f"   └─ Motivo: "
            f"rarità {rarity or 'N/D'}",
            flush=True
        )

        return None

    # ========================================================
    # GIA' IN VENDITA
    # ========================================================

    if already_listed(card):

        print(
            f"⏳ {label} → "
            "già in vendita",
            flush=True
        )

        return None

    # ========================================================
    # LINEUP
    # ========================================================

    lineup = check_lineup(
        card
    )

    # --------------------------------------------------------
    # SCHIERATA
    # --------------------------------------------------------

    if lineup is True:

        print(
            f"🛑 {label} → "
            "NON VENDERE",
            flush=True
        )

        print(
            "   └─ Motivo: "
            "carta schierata "
            "in competizione",
            flush=True
        )

        return None

    # --------------------------------------------------------
    # NON VERIFICABILE
    # --------------------------------------------------------

    if lineup is None:

        print(
            f"🛑 {label} → "
            "controllo lineup "
            "non verificabile",
            flush=True
        )

        print(
            "   └─ Sicurezza: "
            "NON VENDERE",
            flush=True
        )

        return None

    # ========================================================
    # FLOOR
    # ========================================================

    print(
        f"💰 {label} → "
        "ricerca floor live...",
        flush=True
    )

    floor = live_floor(
        card
    )

    if floor is None:

        print(
            f"🚫 {label} → "
            "esclusa",
            flush=True
        )

        print(
            "   └─ Motivo: "
            "floor live non disponibile "
            "o meno di "
            f"{MIN_LIVE_LISTINGS} "
            "inserzioni",
            flush=True
        )

        return None

    print(
        f"   └─ Floor live: "
        f"{eur(floor)}",
        flush=True
    )

    # ========================================================
    # FLOOR SOTTO MINIMO
    # ========================================================

    if floor < MIN_PRICE:

        print(
            f"🚫 {label} → "
            "NON VENDERE",
            flush=True
        )

        print(
            f"   └─ Motivo: "
            f"floor {eur(floor)} "
            f"< minimo "
            f"{eur(MIN_PRICE)}",
            flush=True
        )

        return None

    # ========================================================
    # FLOOR SOPRA MASSIMO
    # ========================================================

    if floor > MAX_PRICE:

        print(
            f"🚫 {label} → "
            "NON VENDERE",
            flush=True
        )

        print(
            f"   └─ Motivo: "
            f"floor {eur(floor)} "
            f"> massimo "
            f"{eur(MAX_PRICE)}",
            flush=True
        )

        return None

    # ========================================================
    # VENDIBILE
    # ========================================================

    print(
        f"✅ {label} → "
        f"VENDIBILE A {eur(floor)}",
        flush=True
    )

    return floor


# ============================================================
# PREZZO VALIDO
# ============================================================

def valid_price(
    floor
):

    return (
        floor is not None
        and MIN_PRICE <= floor <= MAX_PRICE
    )


# ============================================================
# FIRMA AUTHORIZATIONS
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
        r.amount = BigInt(
            r.amount
        );
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
        "Authorization non supportata: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(
            sign
        )
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
# CREA LISTING
# ============================================================

def create_listing(
    card,
    floor
):

    label = card_label(
        card
    )

    asset_id = card.get(
        "assetId"
    )

    # ========================================================
    # BARRIERA 1 - ASSET
    # ========================================================

    if not asset_id:

        print(
            f"❌ {label}: "
            "assetId mancante",
            flush=True
        )

        return False

    # ========================================================
    # BARRIERA 2 - KULENOVIC
    # ========================================================

    if is_kulenovic(card):

        print(
            "🛡️ BLOCCO ASSOLUTO "
            "KULENOVIC",
            flush=True
        )

        return False

    # ========================================================
    # BARRIERA 3 - PREZZO
    # ========================================================

    if not valid_price(
        floor
    ):

        print(
            "🛑 Prezzo fuori range "
            "→ nessuna vendita",
            flush=True
        )

        return False

    # ========================================================
    # BARRIERA 4 - LINEUP
    # ========================================================

    lineup = check_lineup(
        card
    )

    if lineup is not False:

        print(
            "🛑 Listing bloccato: "
            "lineup presente o "
            "non verificabile",
            flush=True
        )

        return False

    # ========================================================
    # LISTING
    # ========================================================

    print(
        f"🟢 LISTING: "
        f"{label} → "
        f"{eur(floor)} "
        f"→ {LISTING_DAYS} giorni",
        flush=True
    )

    # ========================================================
    # DRY RUN
    # ========================================================

    if DRY_RUN:

        print(
            "🟡 DRY RUN=True "
            "→ vendita simulata",
            flush=True
        )

        return True

    # ========================================================
    # PREPARE OFFER
    # ========================================================

    prepare_input = {

        "type":
            "SINGLE_SALE_OFFER",

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {

            "amount":
                str(floor),

            "currency":
                "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation PrepareAutoSell(
            $input: prepareOfferInput!
        ) {

            prepareOffer(
                input: $input
            ) {

                authorizations {

                    fingerprint

                    request {

                        __typename

                        ... on
                        StarkexTransferAuthorizationRequest {

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

                        ... on
                        StarkexLimitOrderAuthorizationRequest {

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

                        ... on
                        MangopayWalletTransferAuthorizationRequest {

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
        "input":
            prepare_input
    })

    if not data:

        return False

    result = (
        ((data.get("data") or {})
        .get("prepareOffer"))
    )

    if not result:

        return False

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

        return False

    authorizations = (
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        print(
            "❌ Nessuna authorization",
            flush=True
        )

        return False

    # ========================================================
    # FIRMA
    # ========================================================

    try:

        approvals = (
            sign_authorizations(
                authorizations
            )
        )

    except Exception as e:

        print(
            f"❌ Firma vendita: {e}",
            flush=True
        )

        return False

    # ========================================================
    # CREATE SALE
    # ========================================================

    create_input = {

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "assetId":
            asset_id,

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {

            "amount":
                str(floor),

            "currency":
                "EUR"
        },

        "clientMutationId":
            str(uuid.uuid4())
    }

    data = graphql("""
        mutation CreateAutoSell(
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
        "input":
            create_input
    })

    if not data:

        return False

    result = (
        ((data.get("data") or {})
        .get(
            "createSingleSaleOffer"
        ))
    )

    if not result:

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

    offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    if not offer.get("id"):

        print(
            "❌ Listing non creata",
            flush=True
        )

        return False

    print(
        "✅ CARTA MESSA "
        "IN VENDITA",
        flush=True
    )

    print(
        f"   ├─ Carta: {label}",
        flush=True
    )

    print(
        f"   ├─ Prezzo: "
        f"{eur(floor)}",
        flush=True
    )

    print(
        f"   ├─ Inizio: "
        f"{offer.get('startDate', 'N/D')}",
        flush=True
    )

    print(
        f"   ├─ Fine: "
        f"{offer.get('endDate', 'N/D')}",
        flush=True
    )

    print(
        f"   └─ ID: "
        f"{offer.get('id')}",
        flush=True
    )

    return True


# ============================================================
# CICLO
# ============================================================

def run_once():

    global last_cycle

    last_cycle = time.time()

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

    cards = all_cards()

    if not cards:

        print(
            "⚠️ Nessuna carta ricevuta "
            "oppure errore API",
            flush=True
        )

        return

    print(
        f"📦 Carte trovate: "
        f"{len(cards)}",
        flush=True
    )

    sold = 0
    skipped = 0

    for card in cards:

        try:

            floor = check_card(
                card
            )

            if floor is None:

                skipped += 1

                continue

            if create_listing(
                card,
                floor
            ):

                sold += 1

            # ------------------------------------------------
            # Piccola pausa per evitare raffiche API
            # ------------------------------------------------

            time.sleep(0.5)

        except Exception as e:

            print(
                f"❌ Errore "
                f"{card_label(card)}: "
                f"{e}",
                flush=True
            )

            skipped += 1

    print(
        "\n📊 RISULTATO CICLO",
        flush=True
    )

    print(
        f"   ├─ Carte: "
        f"{len(cards)}",
        flush=True
    )

    print(
        f"   ├─ Listing creati: "
        f"{sold}",
        flush=True
    )

    print(
        f"   └─ Carte escluse: "
        f"{skipped}",
        flush=True
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "🤖 AUTOSELL AVVIATO",
        flush=True
    )

    print(
        "📦 MODULO: INDIPENDENTE",
        flush=True
    )

    print(
        "📦 VERSIONE: 3.0-LINEUP-FIX",
        flush=True
    )

    print(
        f"🧪 DRY_RUN="
        f"{DRY_RUN}",
        flush=True
    )

    print(
        f"💰 RANGE FLOOR: "
        f"{eur(MIN_PRICE)} - "
        f"{eur(MAX_PRICE)}",
        flush=True
    )

    print(
        f"📊 INSERZIONI LIVE "
        f"MINIME: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        f"⏱️ DURATA LISTING: "
        f"{LISTING_DAYS} giorni",
        flush=True
    )

    print(
        "🔒 KULENOVIC: "
        "MAI IN VENDITA",
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
        "🛡️ LINEUP NON VERIFICABILE: "
        "BLOCCO VENDITA",
        flush=True
    )

    print(
        "💰 PREZZO = FLOOR LIVE",
        flush=True
    )

    print(
        "🆕 CARTE APPENA ACQUISTATE: "
        "INCLUSE",
        flush=True
    )

    print(
        "📡 LINEUP API: "
        "liveSo5Lineup + openedSo5Lineups",
        flush=True
    )

    print(
        "🚫 currentUser.mySo5Lineups: "
        "NON UTILIZZATO",
        flush=True
    )

    print(
        "================================",
        flush=True
    )

    # ========================================================
    # TOKEN
    # ========================================================

    if not TOKEN:

        print(
            "❌ SORARE_JWT_TOKEN "
            "non configurato",
            flush=True
        )

        return

    # ========================================================
    # ACCOUNT
    # ========================================================

    try:

        user = get_account()

    except Exception as e:

        print(
            f"❌ Account: {e}",
            flush=True
        )

        return

    if not user:

        print(
            "❌ Account Sorare "
            "non verificato",
            flush=True
        )

        return

    print(
        f"✅ Account: "
        f"{user.get('nickname') "
        "or user.get('slug')}",
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

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            run_once()

        except KeyboardInterrupt:

            print(
                "\n🛑 AUTOSELL "
                "ARRESTATO",
                flush=True
            )

            break

        except Exception as e:

            print(
                f"❌ AUTOSELL: {e}",
                flush=True
            )

        print(
            f"\n⏳ Prossimo controllo "
            f"tra {INTERVAL}s...",
            flush=True
        )

        time.sleep(
            INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
