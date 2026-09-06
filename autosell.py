import os
import time
import uuid
import json
import shutil
import subprocess
import requests


# ============================================================
# AUTOSELL - MODULO COMPLETAMENTE INDIPENDENTE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# ------------------------------------------------------------
# SICUREZZA
# ------------------------------------------------------------

DRY_RUN = os.getenv("AUTOSELL_DRY_RUN", "true").lower() == "true"

INTERVAL = int(
    os.getenv("AUTOSELL_INTERVAL", "60")
)

TIMEOUT = 25

# ------------------------------------------------------------
# PARAMETRI AUTOSELL
# ------------------------------------------------------------

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70

LISTING_DAYS = 7

MIN_LIVE_LISTINGS = 5

# ------------------------------------------------------------
# KULENOVIC
# ------------------------------------------------------------

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

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


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )


def card_label(card):
    name = card_name(card)
    asset = card.get("assetId")

    if asset:
        return f"{name} [{asset}]"

    return name


def eur(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# AUTENTICAZIONE
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


# ============================================================
# GRAPHQL
# ============================================================

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

                wait = int(
                    response.headers.get(
                        "Retry-After",
                        attempt + 2,
                    )
                )

                time.sleep(
                    min(wait, 15)
                )

                continue

            if response.status_code != 200:

                print(
                    f"❌ HTTP: "
                    f"{response.text[:500]}",
                    flush=True,
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
                        ensure_ascii=False,
                    )[:3000],
                    flush=True,
                )

            return data

        except Exception as error:

            print(
                f"❌ GraphQL: {error}",
                flush=True,
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
# TUTTE LE CARTE DELLA GALLERY
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

    cards = (
        user.get("cards")
        or {}
    )

    return (
        cards.get("nodes") or [],
        cards.get("pageInfo") or {},
    )


def all_cards():

    result = []

    cursor = None

    while True:

        cards, page = get_cards(cursor)

        if not cards:
            break

        result.extend(cards)

        if not page.get("hasNextPage"):
            break

        cursor = page.get("endCursor")

        if not cursor:
            break

    return result


# ============================================================
# KULENOVIC - PROTEZIONE ASSOLUTA
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KULENOVIC_ID:
        wanted.add(
            norm(KULENOVIC_ID)
        )

    return (
        norm(card.get("assetId")) in wanted
        or
        norm(card.get("slug")) in wanted
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

        for other in cards:

            other_player = (
                other.get("anyPlayer")
                or {}
            )

            if norm(
                other_player.get("slug")
            ) != player_slug:
                continue

            if norm(
                other.get("rarityTyped")
            ) != rarity:
                continue

            try:
                other_season = int(
                    other.get("seasonYear")
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
                )
                .get("amounts")
                or {}
            )

            try:
                price = int(
                    amounts.get("eurCents")
                )

            except (
                TypeError,
                ValueError
            ):
                price = 0

            if price > 0:
                prices.append(price)

            break

    # --------------------------------------------------------
    # SICUREZZA:
    # meno di 5 inserzioni live = nessun prezzo affidabile
    # --------------------------------------------------------

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"   └─ Solo {len(prices)} "
            f"inserzioni live → floor NON affidabile",
            flush=True,
        )

        return None

    return min(prices)


# ============================================================
# CONTROLLO CARTA SCHIERATA
#
# IMPORTANTE:
# Se il controllo delle lineup non può essere verificato,
# la carta viene BLOCCATA.
#
# Questo evita una vendita accidentale.
# ============================================================

def card_is_in_lineup(card):

    asset_id = norm(
        card.get("assetId")
    )

    slug = norm(
        card.get("slug")
    )

    if not asset_id and not slug:
        return None

    # --------------------------------------------------------
    # Il campo mySo5Lineups è l'area dell'API Sorare usata
    # per recuperare le lineup dell'utente.
    #
    # Non assumiamo una struttura non verificata.
    # Chiediamo solo i dati che possiamo leggere.
    # --------------------------------------------------------

    data = graphql("""
        query MyLineups {

            currentUser {

                mySo5Lineups {

                    nodes {

                        id
                    }
                }
            }
        }
    """)

    # --------------------------------------------------------
    # Se la query non è disponibile / fallisce:
    # BLOCCO DI SICUREZZA.
    # --------------------------------------------------------

    if not data or data.get("errors"):

        print(
            "   └─ ⚠️ Impossibile verificare "
            "le lineup → CARTA BLOCCATA",
            flush=True,
        )

        return None

    user = (
        ((data.get("data") or {})
        .get("currentUser"))
        or {}
    )

    lineups = (
        user.get("mySo5Lineups")
        or {}
    )

    nodes = (
        lineups.get("nodes")
        or []
    )

    # --------------------------------------------------------
    # Se non troviamo lineup:
    # la carta non risulta schierata.
    #
    # Ma se l'API restituisce lineup senza dettagli delle carte,
    # non possiamo determinare la presenza della carta.
    # In quel caso BLOCCHIAMO.
    # --------------------------------------------------------

    if not nodes:
        return False

    # --------------------------------------------------------
    # La query minimale sopra non espone ancora le carte della
    # lineup. Quindi non possiamo dichiarare con certezza che
    # una specifica carta NON sia schierata.
    #
    # BLOCCO DI SICUREZZA.
    # --------------------------------------------------------

    print(
        "   └─ ⚠️ Lineup presenti ma dati carta "
        "non disponibili nella risposta → CARTA BLOCCATA",
        flush=True,
    )

    return None


# ============================================================
# VERIFICA COMPLETA
# ============================================================

def check_card(card):

    name = card_name(card)

    print(
        f"\n🔎 CONTROLLO: {name}",
        flush=True,
    )

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            "🔒 KULENOVIC → MAI IN VENDITA",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # LIMITED
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
    # CARTA SCHIERATA
    # --------------------------------------------------------

    lineup_status = card_is_in_lineup(card)

    if lineup_status is True:

        print(
            f"🏟️ {name} → SCHIERATA",
            flush=True,
        )

        print(
            "   └─ Motivo: carta presente "
            "in una lineup → NON VENDERE",
            flush=True,
        )

        return None

    if lineup_status is None:

        print(
            f"🛑 {name} → controllo lineup "
            f"non verificabile",
            flush=True,
        )

        print(
            "   └─ Sicurezza: NON VENDERE",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # FLOOR LIVE
    # --------------------------------------------------------

    floor = live_floor(card)

    if floor is None:

        print(
            f"🚫 {name} → esclusa",
            flush=True,
        )

        print(
            "   └─ Motivo: floor live "
            "non disponibile/insufficiente",
            flush=True,
        )

        return None

    print(
        f"💰 Floor live: {eur(floor)}",
        flush=True,
    )

    # --------------------------------------------------------
    # MINIMO
    # --------------------------------------------------------

    if floor < MIN_PRICE:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Floor {eur(floor)} "
            f"< minimo {eur(MIN_PRICE)}",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # MASSIMO
    # --------------------------------------------------------

    if floor > MAX_PRICE:

        print(
            f"🚫 {name} → NON VENDERE",
            flush=True,
        )

        print(
            f"   └─ Floor {eur(floor)} "
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

def sign_authorizations(
    authorizations
):

    node = (
        shutil.which("node")
        or
        shutil.which("nodejs")
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

function signAuthorization(a) {

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
            signAuthorization
        )
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
            "authorizations":
                authorizations,
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
# CREA LISTING
# ============================================================

def create_listing(
    card,
    floor
):

    asset_id = card.get(
        "assetId"
    )

    if not asset_id:

        print(
            "❌ AssetId mancante",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # SECONDA BARRIERA KULENOVIC
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

    if not (
        MIN_PRICE
        <= floor
        <= MAX_PRICE
    ):

        print(
            "🛑 Prezzo fuori range",
            flush=True,
        )

        return False

    print(
        f"🟢 LISTING {card_name(card)}",
        flush=True,
    )

    print(
        f"   ├─ Prezzo: {eur(floor)}",
        flush=True,
    )

    print(
        f"   └─ Durata: "
        f"{LISTING_DAYS} giorni",
        flush=True,
    )

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🟡 DRY RUN=True "
            "→ nessuna vendita reale",
            flush=True,
        )

        return True

    # --------------------------------------------------------
    # PREPARE OFFER
    #
    # Sorare documenta la creazione di una Single Sale tramite
    # prepareOffer + autorizzazioni + createSingleSaleOffer.
    # --------------------------------------------------------

    prepare_input = {

        "type":
            "SINGLE_SALE_OFFER",

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

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
        "input":
            prepare_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer non disponibile",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

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
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        print(
            "❌ Nessuna authorization",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # FIRMA
    # --------------------------------------------------------

    try:

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as error:

        print(
            f"❌ Firma vendita: {error}",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # CREATE SINGLE SALE
    # --------------------------------------------------------

    create_input = {

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "assetId":
            asset_id,

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
    """, {
        "input":
            create_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get(
            "createSingleSaleOffer"
        )
    )

    if not result:

        print(
            "❌ createSingleSaleOffer "
            "non disponibile",
            flush=True,
        )

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
                ensure_ascii=False,
            ),
            flush=True,
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
            flush=True,
        )

        return False

    print(
        "✅ CARTA MESSA IN VENDITA",
        flush=True,
    )

    print(
        f"   ├─ Carta: "
        f"{card_name(card)}",
        flush=True,
    )

    print(
        f"   ├─ Prezzo: "
        f"{eur(floor)}",
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
        f"   └─ ID: "
        f"{offer.get('id')}",
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
        f"📦 Carte trovate: "
        f"{len(cards)}",
        flush=True,
    )

    if not cards:

        print(
            "⚠️ Nessuna carta trovata",
            flush=True,
        )

        return

    for card in cards:

        try:

            floor = check_card(
                card
            )

            if floor is None:
                continue

            create_listing(
                card,
                floor
            )

        except Exception as error:

            print(
                f"❌ Errore "
                f"{card_name(card)}: "
                f"{error}",
                flush=True,
            )


# ============================================================
# MAIN
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

    # --------------------------------------------------------
    # CONTROLLO CREDENZIALI
    # --------------------------------------------------------

    if not TOKEN:

        print(
            "❌ SORARE_JWT_TOKEN mancante",
            flush=True,
        )

        return

    if not AUD:

        print(
            "⚠️ SORARE_JWT_AUD non configurato",
            flush=True,
        )

    if not STARK:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY mancante",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    account = get_account()

    if not account:

        print(
            "❌ Account Sorare non verificato",
            flush=True,
        )

        return

    print(
        f"✅ Account: "
        f"{account.get('nickname') or account.get('slug')}",
        flush=True,
    )

    # --------------------------------------------------------
    # CICLO INFINITO
    # --------------------------------------------------------

    while True:

        try:

            run_once()

        except Exception as error:

            print(
                f"❌ AUTOSELL: {error}",
                flush=True,
            )

        print(
            f"💤 Prossimo controllo "
            f"tra {INTERVAL} secondi",
            flush=True,
        )

        time.sleep(
            INTERVAL
        )


# ============================================================
# AVVIO AUTONOMO
# ============================================================

if __name__ == "__main__":
    main()
