import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

# Endpoint GraphQL attuale di Sorare
URL = "https://api.sorare.com/federation/graphql"

# Endpoint ufficiale per lo schema live
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)

MIN_PRICE = 30
MAX_PRICE = 80

# 20 centesimi
PAY_PER_CARD = 20

MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 30

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

RECENT_SALES_REQUIRED = 5

processed = set()

state_lock = threading.Lock()

_worker_started = False
_worker_lock = threading.Lock()

_schema_lock = threading.Lock()
_schema_text = None


# ============================================================
# UTILS
# ============================================================

def slug(v):
    v = str(v or "").strip().lower()

    for a, b in [
        ("_", "-"),
        (" ", "-"),
        ("’", ""),
        ("'", ""),
    ]:
        v = v.replace(a, b)

    while "--" in v:
        v = v.replace("--", "-")

    return v


def auth_headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-Bot/17.0",
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for attempt in range(1, 4):

        try:

            response = requests.post(
                URL,
                json=payload,
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True,
            )

            if response.status_code == 429:

                try:
                    wait = int(
                        response.headers.get(
                            "Retry-After",
                            attempt * 3,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    wait = attempt * 3

                print(
                    f"⏳ Rate limit: {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    f"❌ HTTP {response.status_code}: "
                    f"{response.text[:1500]}",
                    flush=True,
                )

                time.sleep(attempt)
                continue

            try:
                data = response.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )

                return None

            if data.get("errors"):

                print(
                    "❌ GraphQL ERROR:",
                    flush=True,
                )

                for error in data["errors"]:

                    print(
                        json.dumps(
                            error,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                return data

            return data

        except requests.RequestException as error:

            print(
                f"❌ HTTP: {error}",
                flush=True,
            )

            time.sleep(attempt)

        except Exception as error:

            print(
                f"❌ GraphQL: {error}",
                flush=True,
            )

            return None

    return None


# ============================================================
# LIVE SCHEMA
# ============================================================

def get_live_schema():

    global _schema_text

    with _schema_lock:

        if _schema_text is not None:
            return _schema_text

        try:

            response = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Sorare-Bot/17.0",
                },
            )

            print(
                f"📚 Schema Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    "⚠️ Impossibile scaricare "
                    "lo schema live",
                    flush=True,
                )

                return None

            _schema_text = response.text

            print(
                f"✅ Schema live scaricato "
                f"({len(_schema_text)} caratteri)",
                flush=True,
            )

            return _schema_text

        except Exception as error:

            print(
                f"⚠️ Errore schema live: {error}",
                flush=True,
            )

            return None


def get_input_fields(type_name):

    schema = get_live_schema()

    if not schema:
        return set()

    marker = f"input {type_name}"

    start_marker = schema.find(marker)

    if start_marker == -1:

        print(
            f"⚠️ {type_name} non trovato "
            f"nello schema",
            flush=True,
        )

        return set()

    brace_start = schema.find(
        "{",
        start_marker,
    )

    if brace_start == -1:
        return set()

    depth = 1
    pos = brace_start + 1

    while pos < len(schema) and depth:

        if schema[pos] == "{":
            depth += 1

        elif schema[pos] == "}":
            depth -= 1

        pos += 1

    block = schema[
        brace_start + 1:
        pos - 1
    ]

    fields = set()

    for line in block.splitlines():

        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if line.startswith('"""'):
            continue

        parts = line.split(":", 1)

        if len(parts) != 2:
            continue

        field = parts[0].strip()

        if field and field[0].isalpha():

            # Rimuove eventuali argomenti
            if "(" in field:
                field = field.split(
                    "(",
                    1,
                )[0].strip()

            fields.add(field)

    print(
        f"🔍 Campi {type_name}: "
        f"{', '.join(sorted(fields))}",
        flush=True,
    )

    return fields


def inspect_live_schema():

    print(
        "🔎 CONTROLLO SCHEMA LIVE",
        flush=True,
    )

    prepare_fields = get_input_fields(
        "prepareOfferInput"
    )

    create_fields = get_input_fields(
        "createDirectOfferInput"
    )

    return (
        prepare_fields,
        create_fields,
    )


# ============================================================
# ACCOUNT
# ============================================================

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
            flush=True,
        )

        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    return True


# ============================================================
# OFFERS
# ============================================================

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
                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }

                        receiverSide {
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
        (
            user.get(
                "pendingTokenOffersReceived"
            )
            or {}
        )
        .get("nodes")
        or []
    )


# ============================================================
# CURRENT MARKET FLOOR
# ============================================================

def money_to_cents(value):

    if value is None:
        return None

    try:

        number = float(value)

        if number <= 0:
            return None

        return int(
            round(number * 100)
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def current_market_floor(card):

    """
    Prezzo minimo ATTUALMENTE in vendita.

    Controlliamo:

    1. lowestPriceCard
       = stesso giocatore + stessa rarità
         + stessa stagione

    2. lowestPriceCardAnySeason
       = stesso giocatore + stessa rarità
         senza limitazione di stagione

    Viene utilizzato il minimo tra i dati LIVE
    effettivamente disponibili.

    NON utilizziamo publicMinPrices come sostituto
    di una vendita LIVE.
    """

    values = []

    for source_name in (
        "lowestPriceCard",
        "lowestPriceCardAnySeason",
    ):

        source = (
            card.get(source_name)
            or {}
        )

        live = (
            source.get(
                "liveSingleSaleOffer"
            )
            or {}
        )

        receiver = (
            live.get(
                "receiverSide"
            )
            or {}
        )

        amounts = (
            receiver.get(
                "amounts"
            )
            or {}
        )

        # Schema attuale:
        # MonetaryAmount.eur
        eur = amounts.get("eur")

        cents = money_to_cents(eur)

        if cents is not None:
            values.append(cents)

    if not values:
        return None

    return min(values)


# ============================================================
# RECENT SALES / TOKEN PRICES
# ============================================================

def extract_token_price_cents(item):

    if not isinstance(item, dict):
        return None

    amounts = (
        item.get("amounts")
        or {}
    )

    # Campo attuale principale
    eur = amounts.get("eur")

    cents = money_to_cents(eur)

    if cents is not None:
        return cents

    # Fallback compatibilità
    amount_in_fiat = (
        item.get("amountInFiat")
        or {}
    )

    eur = amount_in_fiat.get("eur")

    cents = money_to_cents(eur)

    if cents is not None:
        return cents

    return None


def get_recent_sales_for_player(
    player_slug,
    rarity,
):

    """
    RECUPERA LE ULTIME 5 VENDITE/PREZZI PUBBLICI
    DEL GIOCATORE + RARITÀ.

    Sorare espone tokens.tokenPrices.

    Parametri:

        playerSlug
        rarity
        collection = FOOTBALL

    IMPORTANTE:

    collection = FOOTBALL NON significa
    "una singola stagione".

    Il filtro è sullo sport/collection FOOTBALL.

    Quindi lo storico NON viene limitato
    alla stagione della carta ricevuta.

    La funzione richiede almeno 5 risultati
    con prezzo EUR valido.
    """

    player_slug = str(
        player_slug or ""
    ).strip()

    rarity = str(
        rarity or ""
    ).strip().upper()

    if not player_slug:

        print(
            "      ❌ playerSlug mancante "
            "per storico vendite",
            flush=True,
        )

        return None

    if not rarity:

        print(
            "      ❌ rarity mancante "
            "per storico vendite",
            flush=True,
        )

        return None

    print(
        "      📊 Richiesta ultime 5 "
        "vendite/prezzi pubblici",
        flush=True,
    )

    print(
        f"         playerSlug = {player_slug}",
        flush=True,
    )

    print(
        f"         rarity = {rarity}",
        flush=True,
    )

    print(
        "         collection = FOOTBALL",
        flush=True,
    )

    data = graphql("""
        query RecentTokenPrices(
            $playerSlug: String!
            $rarity: Rarity!
            $collection: Collection!
        ) {
            tokens {
                tokenPrices(
                    playerSlug: $playerSlug
                    rarity: $rarity
                    collection: $collection
                ) {
                    id
                    date

                    amounts {
                        eur
                        wei
                    }

                    deal {
                        __typename

                        ... on TokenAuction {
                            id
                        }

                        ... on TokenPrimaryOffer {
                            id
                        }

                        ... on TokenOffer {
                            id
                        }
                    }
                }
            }
        }
    """, {
        "playerSlug": player_slug,
        "rarity": rarity,
        "collection": "FOOTBALL",
    })

    if not data:

        print(
            "      ❌ Nessuna risposta "
            "da tokenPrices",
            flush=True,
        )

        return None

    if data.get("errors"):

        print(
            "      ❌ tokenPrices "
            "GRAPHQL ERROR",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return None

    tokens = (
        (data.get("data") or {})
        .get("tokens")
        or {}
    )

    prices = (
        tokens.get(
            "tokenPrices"
        )
        or []
    )

    if not prices:

        print(
            "      ❌ tokenPrices ha "
            "restituito 0 risultati",
            flush=True,
        )

        return None

    print(
        f"      📊 Risultati ricevuti: "
        f"{len(prices)}",
        flush=True,
    )

    valid = []

    for item in prices:

        price = extract_token_price_cents(
            item
        )

        if price is None:
            continue

        valid.append({
            "price": price,
            "date": item.get("date"),
            "id": item.get("id"),
            "deal": (
                item.get("deal")
                or {}
            ),
        })

    if len(valid) < RECENT_SALES_REQUIRED:

        print(
            f"      ❌ Prezzi validi: "
            f"{len(valid)} / "
            f"{RECENT_SALES_REQUIRED}",
            flush=True,
        )

        return None

    # Sorare restituisce gli ultimi prezzi.
    # Riordiniamo comunque per data decrescente
    # se la data è disponibile.

    def sort_key(item):

        return str(
            item.get("date")
            or ""
        )

    valid.sort(
        key=sort_key,
        reverse=True,
    )

    recent = valid[
        :RECENT_SALES_REQUIRED
    ]

    print(
        "      ✅ Ultime 5 vendite/prezzi "
        "recuperate",
        flush=True,
    )

    return recent


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_ids):

    asset_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in asset_ids
            if value
        )
    )

    if not asset_ids:
        return []

    data = graphql("""
        query Cards(
            $assetIds: [String!]
        ) {
            anyCards(
                assetIds: $assetIds
            ) {
                assetId
                slug
                name
                rarityTyped

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

                lowestPriceCard {
                    liveSingleSaleOffer {
                        receiverSide {
                            amounts {
                                eur
                            }
                        }
                    }

                    publicMinPrices {
                        eur
                    }
                }

                lowestPriceCardAnySeason {
                    liveSingleSaleOffer {
                        receiverSide {
                            amounts {
                                eur
                            }
                        }
                    }

                    publicMinPrices {
                        eur
                    }
                }
            }
        }
    """, {
        "assetIds": asset_ids
    })

    if not data:
        return []

    if data.get("errors"):

        print(
            "❌ Errore GraphQL "
            "card_details:",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return []

    cards = (
        ((data.get("data") or {})
         .get("anyCards"))
        or []
    )

    return cards


# ============================================================
# FLOOR DEFINITIVO
# ============================================================

def card_price(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = str(
        player.get("slug")
        or card.get("slug")
        or ""
    ).strip()

    rarity = str(
        card.get("rarityTyped")
        or ""
    ).upper()

    if not player_slug:

        print(
            "      ❌ Player slug mancante",
            flush=True,
        )

        return None

    if not rarity:

        print(
            "      ❌ Rarità mancante",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # 1. MINIMO ATTUALE
    # --------------------------------------------------------

    current_floor = (
        current_market_floor(card)
    )

    if current_floor is None:

        print(
            "      ❌ Manca il prezzo minimo "
            "attualmente in vendita",
            flush=True,
        )

        print(
            "      ❌ CARTA RIFIUTATA",
            flush=True,
        )

        return None

    print(
        f"      🛒 Minimo attuale: "
        f"€{current_floor / 100:.2f}",
        flush=True,
    )

    # --------------------------------------------------------
    # 2. ULTIME 5 VENDITE
    # --------------------------------------------------------

    recent_sales = (
        get_recent_sales_for_player(
            player_slug,
            rarity,
        )
    )

    # --------------------------------------------------------
    # ENTRAMBI OBBLIGATORI
    # --------------------------------------------------------

    if recent_sales is None:

        print(
            "      ❌ Storico ultime 5 "
            "non verificabile",
            flush=True,
        )

        print(
            "      ❌ CARTA RIFIUTATA",
            flush=True,
        )

        return None

    if len(recent_sales) < (
        RECENT_SALES_REQUIRED
    ):

        print(
            f"      ❌ Solo "
            f"{len(recent_sales)} "
            f"vendite disponibili",
            flush=True,
        )

        print(
            "      ❌ CARTA RIFIUTATA",
            flush=True,
        )

        return None

    # --------------------------------------------------------
    # PRENDIAMO ESATTAMENTE LE ULTIME 5
    # --------------------------------------------------------

    recent_sales = recent_sales[
        :RECENT_SALES_REQUIRED
    ]

    sale_prices = []

    for sale in recent_sales:

        price = sale.get(
            "price"
        )

        if price is None:

            print(
                "      ❌ Una delle ultime "
                "5 vendite non ha prezzo EUR",
                flush=True,
            )

            print(
                "      ❌ CARTA RIFIUTATA",
                flush=True,
            )

            return None

        sale_prices.append(
            price
        )

    # --------------------------------------------------------
    # MINIMO ULTIME 5
    # --------------------------------------------------------

    sales_floor = min(
        sale_prices
    )

    print(
        "      📉 Ultime 5 vendite:",
        flush=True,
    )

    for index, price in enumerate(
        sale_prices,
        start=1,
    ):

        print(
            f"         {index}) "
            f"€{price / 100:.2f}",
            flush=True,
        )

    print(
        f"      📉 Minimo ultime 5: "
        f"€{sales_floor / 100:.2f}",
        flush=True,
    )

    # --------------------------------------------------------
    # FLOOR DEFINITIVO
    # --------------------------------------------------------

    floor = min(
        current_floor,
        sales_floor,
    )

    print(
        f"      💰 FLOOR DEFINITIVO: "
        f"€{floor / 100:.2f}",
        flush=True,
    )

    return floor


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(
            KID.lower()
        )

    return (
        str(
            card.get("assetId")
            or ""
        ).lower()
        in wanted

        or

        str(
            card.get("slug")
            or ""
        ).lower()
        in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def get_competitions(card):

    club = (
        card.get("anyPlayer")
        or {}
    ).get("activeClub")

    if not isinstance(
        club,
        dict,
    ):
        return []

    result = []

    for competition in (
        club.get(
            "activeCompetitions"
        )
        or []
    ):

        if isinstance(
            competition,
            dict,
        ):

            value = slug(
                competition.get(
                    "slug"
                )
            )

            if value:
                result.append(
                    value
                )

    return list(
        dict.fromkeys(result)
    )


def check_competition(card):

    club = (
        card.get("anyPlayer")
        or {}
    ).get("activeClub")

    if not isinstance(
        club,
        dict,
    ):

        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )

        return False

    name = (
        club.get("name")
        or club.get("slug")
        or "Sconosciuta"
    )

    competitions = (
        get_competitions(card)
    )

    print(
        f"      🏟️ Squadra: {name}",
        flush=True,
    )

    if not competitions:

        print(
            "      ❌ Nessuna "
            "activeCompetition",
            flush=True,
        )

        return False

    print(
        "      🏆 Competizioni Sorare:",
        flush=True,
    )

    for competition in competitions:

        print(
            f"         🆕 {competition}",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


# ============================================================
# VALID CARD
# ============================================================

def valid_card(card):

    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped")
        or ""
    ).upper()

    player = (
        card.get("anyPlayer")
        or {}
    )

    try:

        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError,
    ):

        print(
            f"   📄 {name}",
            flush=True,
        )

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return False

    print(
        f"   📄 {name}",
        flush=True,
    )

    print(
        f"      🎂 Età: {age} anni",
        flush=True,
    )

    if age >= MAX_AGE:

        print(
            f"      ❌ Età troppo alta "
            f"(limite: < {MAX_AGE})",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # FLOOR RIGIDO
    # --------------------------------------------------------

    price = card_price(card)

    if price is None:

        print(
            "      ❌ Floor non verificabile "
            "con entrambi i dati",
            flush=True,
        )

        return False

    print(
        f"      💰 Floor €{price / 100:.2f}",
        flush=True,
    )

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # COMPETIZIONE
    # --------------------------------------------------------

    if not check_competition(card):
        return False

    competitions = (
        get_competitions(card)
    )

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return True


# ============================================================
# REJECT OFFER
# ============================================================

def reject_offer(offer):

    blockchain_id = str(
        offer.get("blockchainId")
        or ""
    ).strip()

    if not blockchain_id:

        print(
            "❌ blockchainId mancante",
            flush=True,
        )

        return False

    if DRY_RUN:

        print(
            "🟡 DRY RUN: rifiuto simulato",
            flush=True,
        )

        return True

    data = graphql("""
        mutation Reject(
            $input: rejectOfferInput!
        ) {
            rejectOffer(
                input: $input
            ) {
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
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            ),
        }
    })

    if not data:

        print(
            "❌ Nessuna risposta rejectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ rejectOffer "
            "GRAPHQL ERROR",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("rejectOffer")
    )

    if not result:

        print(
            "❌ Risposta rejectOffer vuota",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ Reject: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# SIGN
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

    script = r'''
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function build(a) {

    const r = a.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest" &&
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
        "Authorization non supportata: " +
        r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(build)
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
# PREPARE INPUT
# ============================================================

def build_prepare_offer_input(
    asset_ids,
    receiver,
    amount,
):

    fields = get_input_fields(
        "prepareOfferInput"
    )

    if not fields:

        raise RuntimeError(
            "Impossibile leggere "
            "prepareOfferInput "
            "dallo schema live"
        )

    result = {}

    if "type" in fields:

        result["type"] = (
            "DIRECT_OFFER"
        )

    if "sendAssetIds" in fields:

        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:

        result["receiveAssetIds"] = (
            asset_ids
        )

    if "sendAmount" in fields:

        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:

        result["receiverSlug"] = (
            receiver
        )

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = ["EUR"]

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

    return result


# ============================================================
# CREATE INPUT
# ============================================================

def build_create_direct_offer_input(
    asset_ids,
    receiver,
    amount,
    approvals,
):

    fields = get_input_fields(
        "createDirectOfferInput"
    )

    if not fields:

        raise RuntimeError(
            "Impossibile leggere "
            "createDirectOfferInput "
            "dallo schema live"
        )

    result = {}

    if "approvals" in fields:

        result["approvals"] = approvals

    if "dealId" in fields:

        result["dealId"] = str(
            uuid.uuid4()
        )

    if "sendAssetIds" in fields:

        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:

        result["receiveAssetIds"] = (
            asset_ids
        )

    if "sendAmount" in fields:

        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:

        result["receiverSlug"] = (
            receiver
        )

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = ["EUR"]

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

    return result


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(
    offer,
    cards,
):

    sender = (
        offer.get("sender")
        or {}
    )

    receiver = str(
        sender.get("slug")
        or ""
    ).strip()

    asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if card.get("assetId")
    ]

    if not receiver:

        print(
            "❌ receiverSlug mancante",
            flush=True,
        )

        return False

    if not asset_ids:

        print(
            "❌ Nessuna carta da ricevere",
            flush=True,
        )

        return False

    amount = (
        len(asset_ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 Controproposta: "
        f"{len(asset_ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True,
    )

    print(
        f"👤 Receiver: {receiver}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True,
        )

        return True

    if not STARK:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    try:

        prepare_input = (
            build_prepare_offer_input(
                asset_ids,
                receiver,
                amount,
            )
        )

    except Exception as error:

        print(
            f"❌ Costruzione "
            f"prepareOfferInput: {error}",
            flush=True,
        )

        return False

    print(
        "📦 prepareOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            prepare_input,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
        "input": prepare_input
    })

    if not data:

        print(
            "❌ Nessuna risposta "
            "da prepareOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ prepareOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ prepareOffer: "
                f"{error.get('message', 'Errore')}",
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
            "❌ Nessuna autorizzazione "
            "restituita",
            flush=True,
        )

        return False

    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )

    # --------------------------------------------------------
    # SIGN
    # --------------------------------------------------------

    try:

        approvals = (
            sign_authorizations(
                authorizations
            )
        )

    except Exception as error:

        print(
            f"❌ Firma: {error}",
            flush=True,
        )

        return False

    print(
        f"✍️ Autorizzazioni firmate: "
        f"{len(approvals)}",
        flush=True,
    )

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    try:

        create_input = (
            build_create_direct_offer_input(
                asset_ids,
                receiver,
                amount,
                approvals,
            )
        )

    except Exception as error:

        print(
            f"❌ Costruzione "
            f"createDirectOfferInput: {error}",
            flush=True,
        )

        return False

    debug = dict(
        create_input
    )

    if "approvals" in debug:

        debug["approvals"] = (
            f"{len(approvals)} "
            f"authorization(s)"
        )

    print(
        "📦 createDirectOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            debug,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(
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
        "input": create_input
    })

    if not data:

        print(
            "❌ Nessuna risposta "
            "da createDirectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ createDirectOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("createDirectOffer")
    )

    if not result:

        print(
            "❌ createDirectOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ createDirectOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get(
        "id"
    )

    if not offer_id:

        print(
            "❌ Nessuna offerta creata "
            "da Sorare",
            flush=True,
        )

        return False

    print(
        "=" * 40,
        flush=True,
    )

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{offer_id}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True,
    )

    print(
        "=" * 40,
        flush=True,
    )

    return True


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):

    offer_id = str(
        offer.get("id")
        or ""
    ).strip()

    if not offer_id:
        return

    with state_lock:

        if offer_id in processed:
            return

    print(
        "\n" + "=" * 40,
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )

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

    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if not any(
        is_kulenovic(card)
        for card in receiver_cards
    ):

        print(
            "⏭️ Kulenovic non presente: "
            "ignoro",
            flush=True,
        )

        with state_lock:
            processed.add(offer_id)

        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    # --------------------------------------------------------
    # CARTE OFFERTE
    # --------------------------------------------------------

    ids = [
        card.get("assetId")
        for card in sender_cards
        if card.get("assetId")
    ]

    if not ids:

        print(
            "❌ Nessuna carta offerta",
            flush=True,
        )

        with state_lock:
            processed.add(offer_id)

        return

    cards = card_details(
        ids
    )

    if len(cards) != len(ids):

        print(
            "❌ Impossibile verificare "
            "tutte le carte",
            flush=True,
        )

        # NON segniamo come processed:
        # verrà riprovata.
        return

    print(
        f"🔎 Controllo "
        f"{len(cards)} carta/e",
        flush=True,
    )

    valid_cards = []

    for card in cards:

        try:

            if valid_card(card):

                valid_cards.append(
                    card
                )

        except Exception as error:

            print(
                f"❌ Errore controllo "
                f"carta: {error}",
                flush=True,
            )

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/"
        f"{len(cards)}",
        flush=True,
    )

    # --------------------------------------------------------
    # NESSUNA CARTA VALIDA
    # --------------------------------------------------------

    if not valid_cards:

        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        if reject_offer(
            offer
        ):

            with state_lock:
                processed.add(
                    offer_id
                )

        return

    # --------------------------------------------------------
    # ALCUNE CARTE ESCLUSE
    # --------------------------------------------------------

    rejected = (
        len(cards)
        - len(valid_cards)
    )

    if rejected:

        print(
            f"⚠️ {rejected} carta/e "
            f"esclusa/e",
            flush=True,
        )

    # --------------------------------------------------------
    # CONTROPROPOSTA
    # --------------------------------------------------------

    if counter_offer(
        offer,
        valid_cards,
    ):

        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

        # Rifiutiamo l'offerta originale
        # SOLO dopo aver creato la controproposta.

        if reject_offer(
            offer
        ):

            with state_lock:
                processed.add(
                    offer_id
                )

        else:

            print(
                "⚠️ Controproposta creata, "
                "ma impossibile rifiutare "
                "l'offerta originale.",
                flush=True,
            )

    else:

        print(
            "🔴 Controproposta "
            "NON creata.",
            flush=True,
        )

        print(
            "🟡 Offerta originale "
            "lasciata IN SOSPESO "
            "per nuovo tentativo.",
            flush=True,
        )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 BOT AVVIATO",
        flush=True,
    )

    print(
        "📦 VERSIONE BOT: 17.0 "
        "STRICT FLOOR + TOKEN PRICES",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} "
        f"per carta",
        flush=True,
    )

    print(
        f"📊 Range floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True,
    )

    print(
        f"🎂 Età massima: "
        f"meno di {MAX_AGE} anni",
        flush=True,
    )

    print(
        "🏆 COMPETIZIONI: TUTTE le "
        "activeCompetitions Sorare",
        flush=True,
    )

    print(
        "💰 FLOOR: minimo attuale + "
        "minimo ultime 5",
        flush=True,
    )

    print(
        "🚫 FLOOR: ENTRAMBI obbligatori",
        flush=True,
    )

    print(
        "🌍 STORICO: giocatore + rarità "
        "su collection FOOTBALL, "
        "senza filtro stagione",
        flush=True,
    )

    print(
        "📊 STORICO: tokenPrices "
        "Sorare",
        flush=True,
    )

    print(
        "🔧 API: federation/graphql",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if not check_account():

        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return

    inspect_live_schema()

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True,
            )

            for offer in offers:

                try:

                    process_offer(
                        offer
                    )

                except Exception as error:

                    print(
                        f"❌ Errore offerta: "
                        f"{error}",
                        flush=True,
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as error:

            print(
                f"❌ Worker: {error}",
                flush=True,
            )

            time.sleep(
                INTERVAL
            )


def start_worker():

    global _worker_started

    with _worker_lock:

        if _worker_started:
            return

        _worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True,
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": "17.0",

        "dry_run": DRY_RUN,

        "pay_per_card_cents":
            PAY_PER_CARD,

        "interval_seconds":
            INTERVAL,

        "max_age":
            MAX_AGE,

        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",

        "floor_mode":
            "MIN_CURRENT_LISTING_AND_MIN_LAST_5_SALES",

        "recent_sales_required":
            RECENT_SALES_REQUIRED,

        "require_both_floor_sources":
            True,

        "history_scope":
            "PLAYER_RARITY_FOOTBALL_ALL_SEASONS",

        "history_query":
            "tokens.tokenPrices",

        "prepare_mode":
            "LIVE_SCHEMA_AWARE",

        "settlement_currency":
            "EUR",

        "graphql_endpoint":
            URL,
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "17.0",

        "floor_mode":
            "STRICT_CURRENT_PLUS_LAST_5",

        "require_both":
            True,

        "history_query":
            "tokens.tokenPrices",
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000",
            )
        ),
    )
