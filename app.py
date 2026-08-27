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

# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MIN_PRICE = 30
MAX_PRICE = 80
PAY_PER_CARD = 20
MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 30

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

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

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-Bot/17.0",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


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
            r = requests.post(
                URL,
                json=payload,
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True,
            )

            if r.status_code == 429:
                try:
                    wait = int(
                        r.headers.get(
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

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: "
                    f"{r.text[:1500]}",
                    flush=True,
                )

                time.sleep(attempt)
                continue

            try:
                data = r.json()

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

                for e in data["errors"]:
                    print(
                        json.dumps(
                            e,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                return data

            return data

        except requests.RequestException as e:
            print(
                f"❌ HTTP: {e}",
                flush=True,
            )

            time.sleep(attempt)

        except Exception as e:
            print(
                f"❌ GraphQL: {e}",
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
            r = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Sorare-Bot/17.0",
                },
            )

            print(
                f"📚 Schema Sorare HTTP {r.status_code}",
                flush=True,
            )

            if r.status_code != 200:
                print(
                    "⚠️ Impossibile scaricare "
                    "lo schema live",
                    flush=True,
                )

                return None

            _schema_text = r.text

            print(
                f"✅ Schema live scaricato "
                f"({len(_schema_text)} caratteri)",
                flush=True,
            )

            return _schema_text

        except Exception as e:
            print(
                f"⚠️ Errore download schema: {e}",
                flush=True,
            )

            return None


def get_input_fields(type_name):
    schema = get_live_schema()

    if not schema:
        return set()

    m = re.search(
        r"\binput\s+"
        + re.escape(type_name)
        + r"\s*\{",
        schema,
        re.MULTILINE,
    )

    if not m:
        print(
            f"⚠️ {type_name} non trovato "
            f"nello schema",
            flush=True,
        )

        return set()

    start = m.end()
    depth = 1
    pos = start

    while pos < len(schema) and depth:
        if schema[pos] == "{":
            depth += 1

        elif schema[pos] == "}":
            depth -= 1

        pos += 1

    block = schema[start:pos - 1]
    fields = set()

    for line in block.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        m = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*"
            r"(?:\([^)]*\))?\s*:",
            line,
        )

        if m:
            fields.add(m.group(1))

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

    p = get_input_fields(
        "prepareOfferInput"
    )

    c = get_input_fields(
        "createDirectOfferInput"
    )

    if p:
        print(
            "   prepareOfferInput:",
            flush=True,
        )

        for x in sorted(p):
            print(
                f"      • {x}",
                flush=True,
            )

    if c:
        print(
            "   createDirectOfferInput:",
            flush=True,
        )

        for x in sorted(c):
            print(
                f"      • {x}",
                flush=True,
            )

    return p, c


# ============================================================
# ACCOUNT / OFFERS
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
        (user.get(
            "pendingTokenOffersReceived"
        ) or {}).get("nodes")
        or []
    )


# ============================================================
# HISTORICAL PRICES
# ============================================================

def get_last_5_prices(player_slug, rarity):
    """
    Recupera le ultime 5 quotazioni pubbliche
    di Sorare per:

        - stesso giocatore
        - stessa rarità
        - collection FOOTBALL

    IMPORTANTE:

    tokenPrices non usa la stagione della carta.
    Il filtro è player + rarity + collection sportiva.

    Quindi per un calciatore Limited vengono
    considerate le quotazioni Limited Football
    del giocatore attraverso le diverse stagioni.
    """

    player_slug = str(
        player_slug or ""
    ).strip()

    rarity = str(
        rarity or ""
    ).strip().upper()

    if not player_slug:
        print(
            "      ⚠️ Storico: playerSlug mancante",
            flush=True,
        )

        return []

    if rarity != "LIMITED":
        print(
            f"      ⚠️ Storico: rarità "
            f"{rarity} non gestita",
            flush=True,
        )

        return []

    query = """
        query TokenPrices(
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

                    amountInFiat {
                        eur
                    }

                    amounts {
                        eur
                    }
                }
            }
        }
    """

    data = graphql(
        query,
        {
            "playerSlug": player_slug,
            "rarity": rarity,
            "collection": "FOOTBALL",
        },
    )

    if not data:
        print(
            "      ⚠️ Storico prezzi: "
            "nessuna risposta",
            flush=True,
        )

        return []

    if data.get("errors"):
        print(
            "      ⚠️ Storico prezzi: "
            "errore GraphQL",
            flush=True,
        )

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return []

    tokens = (
        (data.get("data") or {})
        .get("tokens")
        or {}
    )

    prices = tokens.get("tokenPrices") or []

    if not isinstance(prices, list):
        return []

    result = []

    for item in prices[:5]:
        if not isinstance(item, dict):
            continue

        value = None

        # ----------------------------------------------------
        # PRIMA SCELTA: amountInFiat.eur
        # ----------------------------------------------------

        try:
            eur = (
                (item.get("amountInFiat") or {})
                .get("eur")
            )

            if eur is not None:
                value = float(eur)

        except (
            TypeError,
            ValueError,
            AttributeError,
        ):
            value = None

        # ----------------------------------------------------
        # SE NON DISPONIBILE: amounts.eur
        # ----------------------------------------------------

        if value is None:
            try:
                eur = (
                    (item.get("amounts") or {})
                    .get("eur")
                )

                if eur is not None:
                    value = float(eur)

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                value = None

        if value is None:
            continue

        if value <= 0:
            continue

        cents = int(
            round(value * 100)
        )

        if cents > 0:
            result.append({
                "cents": cents,
                "date": item.get("date"),
                "id": item.get("id"),
            })

    # --------------------------------------------------------
    # ORDINA DAL PIÙ RECENTE AL PIÙ VECCHIO
    # --------------------------------------------------------

    # Sorare normalmente restituisce già le ultime 5.
    # Non alteriamo l'ordine ricevuto.

    print(
        f"      📈 Ultime quotazioni "
        f"pubbliche trovate: {len(result)}",
        flush=True,
    )

    for index, item in enumerate(
        result,
        start=1,
    ):
        date = item.get("date") or "-"

        print(
            f"         {index}. "
            f"€{item['cents'] / 100:.2f} "
            f"({date})",
            flush=True,
        )

    return result


# ============================================================
# CARDS
# ============================================================

def card_details(asset_ids):
    asset_ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
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
                    displayName
                    slug
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
                                eurCents
                            }
                        }
                    }

                    publicMinPrices {
                        eurCents
                    }
                }

                lowestPriceCardAnySeason {
                    liveSingleSaleOffer {
                        receiverSide {
                            amounts {
                                eurCents
                            }
                        }
                    }

                    publicMinPrices {
                        eurCents
                    }
                }
            }
        }
    """, {
        "assetIds": asset_ids,
    })

    if not data:
        return []

    if data.get("errors"):
        print(
            "❌ Errore GraphQL "
            "card_details:",
            flush=True,
        )

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
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
# CURRENT MARKET PRICE
# ============================================================

def current_market_prices(card):
    """
    Recupera tutti i prezzi correnti che
    il vecchio bot già utilizzava.

    Vengono considerati:

        lowestPriceCard
        lowestPriceCardAnySeason

    e dentro ciascuno:

        liveSingleSaleOffer
        publicMinPrices
    """

    values = []

    sources = []

    for name in (
        "lowestPriceCard",
        "lowestPriceCardAnySeason",
    ):
        source = card.get(name) or {}

        # ----------------------------------------------------
        # LIVE SINGLE SALE OFFER
        # ----------------------------------------------------

        try:
            live = (
                (source.get(
                    "liveSingleSaleOffer"
                ) or {})
                .get("receiverSide", {})
                .get("amounts", {})
                .get("eurCents")
            )

            if live is not None:
                value = int(live)

                if value > 0:
                    values.append(value)

                    sources.append({
                        "type": "liveSingleSaleOffer",
                        "scope": name,
                        "cents": value,
                    })

        except (
            TypeError,
            ValueError,
            AttributeError,
        ):
            pass

        # ----------------------------------------------------
        # PUBLIC MIN PRICES
        # ----------------------------------------------------

        public = (
            source.get("publicMinPrices")
            or []
        )

        if isinstance(public, dict):
            public = [public]

        for item in public:
            try:
                value = int(
                    item.get("eurCents")
                )

                if value > 0:
                    values.append(value)

                    sources.append({
                        "type": "publicMinPrices",
                        "scope": name,
                        "cents": value,
                    })

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                pass

    return values, sources


# ============================================================
# FLOOR
# ============================================================

def card_price(card):
    """
    NUOVA LOGICA FLOOR

    Il floor viene calcolato come:

        minimo assoluto tra:

        1) prezzi attuali di mercato
           restituiti da:
              lowestPriceCard
              lowestPriceCardAnySeason

        2) prezzi delle ultime 5 quotazioni
           pubbliche del giocatore Limited
           Football.

    NON viene usata la stagione della carta
    per lo storico.

    Se lo storico non è disponibile,
    viene utilizzato il vecchio metodo
    del prezzo corrente.
    """

    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    player = card.get("anyPlayer") or {}

    player_slug = str(
        player.get("slug") or ""
    ).strip()

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    # ========================================================
    # 1. PREZZI ATTUALI
    # ========================================================

    current_values, current_sources = (
        current_market_prices(card)
    )

    # ========================================================
    # DEBUG PREZZI ATTUALI
    # ========================================================

    print(
        f"      🔎 PREZZI ATTUALI: {name}",
        flush=True,
    )

    if current_sources:
        for source in current_sources:
            print(
                f"         • "
                f"{source['scope']} / "
                f"{source['type']}: "
                f"€{source['cents'] / 100:.2f}",
                flush=True,
            )

    else:
        print(
            "         • Nessun prezzo corrente",
            flush=True,
        )

    # ========================================================
    # 2. ULTIME 5 QUOTAZIONI
    # ========================================================

    historical = []

    if player_slug and rarity == "LIMITED":
        historical = get_last_5_prices(
            player_slug,
            rarity,
        )

    else:
        print(
            "      ⚠️ Storico non richiesto: "
            "playerSlug o rarity non validi",
            flush=True,
        )

    historical_values = [
        item["cents"]
        for item in historical
        if item.get("cents")
    ]

    # ========================================================
    # 3. CREA UNICA LISTA DI TUTTI I PREZZI
    # ========================================================

    all_values = []

    all_values.extend(
        current_values
    )

    all_values.extend(
        historical_values
    )

    # ========================================================
    # 4. SE NESSUN PREZZO
    # ========================================================

    if not all_values:
        print(
            f"      🔍 DEBUG PREZZO: {name}",
            flush=True,
        )

        print(
            json.dumps(
                {
                    "assetId":
                        card.get("assetId"),
                    "slug":
                        card.get("slug"),
                    "playerSlug":
                        player_slug,
                    "rarity":
                        rarity,
                    "lowestPriceCard":
                        card.get(
                            "lowestPriceCard"
                        ),
                    "lowestPriceCardAnySeason":
                        card.get(
                            "lowestPriceCardAnySeason"
                        ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        return None

    # ========================================================
    # 5. MINIMO ASSOLUTO
    # ========================================================

    floor = min(all_values)

    # ========================================================
    # 6. DEBUG COMPLETO DEL CALCOLO
    # ========================================================

    print(
        "      🧮 CALCOLO FLOOR:",
        flush=True,
    )

    if current_values:
        print(
            "         Prezzi attuali:",
            flush=True,
        )

        for value in current_values:
            print(
                f"            €{value / 100:.2f}",
                flush=True,
            )

    if historical_values:
        print(
            "         Ultime 5 quotazioni:",
            flush=True,
        )

        for value in historical_values:
            print(
                f"            €{value / 100:.2f}",
                flush=True,
            )

    print(
        f"         👉 FLOOR FINALE: "
        f"€{floor / 100:.2f}",
        flush=True,
    )

    # --------------------------------------------------------
    # INDICA DA DOVE ARRIVA IL FLOOR
    # --------------------------------------------------------

    if historical_values and floor == min(
        historical_values
    ):
        print(
            "         📈 Floor determinato "
            "dallo storico delle ultime 5",
            flush=True,
        )

    elif current_values and floor == min(
        current_values
    ):
        print(
            "         🛒 Floor determinato "
            "dal prezzo corrente",
            flush=True,
        )

    return floor


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
            card.get("assetId") or ""
        ).lower()
        in wanted
        or
        str(
            card.get("slug") or ""
        ).lower()
        in wanted
    )


# ============================================================
# COMPETITIONS / VALIDATION
# ============================================================

def get_competitions(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        return []

    result = []

    for c in (
        club.get("activeCompetitions")
        or []
    ):
        if isinstance(c, dict):
            value = slug(
                c.get("slug")
            )

            if value:
                result.append(value)

    return list(
        dict.fromkeys(result)
    )


def check_competition(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
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

    competitions = get_competitions(card)

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

    for c in competitions:
        print(
            f"         🆕 {c} ({c})",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


def valid_card(card):
    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    player = (
        card.get("anyPlayer") or {}
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

    # ========================================================
    # FLOOR
    # ========================================================

    price = card_price(card)

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

    if price is None:
        print(
            "      ❌ Prezzo non disponibile",
            flush=True,
        )

        return False

    print(
        f"      💰 FLOOR: "
        f"€{price / 100:.2f}",
        flush=True,
    )

    if not MIN_PRICE <= price <= MAX_PRICE:
        print(
            f"      ❌ Floor fuori range "
            f"(€{MIN_PRICE / 100:.2f} - "
            f"€{MAX_PRICE / 100:.2f})",
            flush=True,
        )

        return False

    if rarity != "LIMITED":
        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )

        return False

    if not check_competition(card):
        return False

    competitions = get_competitions(card)

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return True


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:
        print(
            "❌ blockchainId mancante",
            flush=True,
        )

        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: "
            "rifiuto simulato",
            flush=True,
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
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            ),
        }
    })

    result = (
        (data or {}).get("data") or {}
    ).get("rejectOffer")

    if not result:
        print(
            "❌ Risposta rejectOffer vuota",
            flush=True,
        )

        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(
                f"❌ Reject: "
                f"{e.get('message', 'Errore')}",
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

def sign_authorizations(authorizations):
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

    if (!r)
        throw new Error(
            "AuthorizationRequest mancante"
        );

    if (
        r.__typename ===
            "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    )
        r.amount = BigInt(r.amount);

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            r
        );

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    )
        return {
            fingerprint: a.fingerprint,

            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp:
                    r.expirationTimestamp,
                signature
            }
        };

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    )
        return {
            fingerprint: a.fingerprint,

            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp:
                    r.expirationTimestamp,
                signature
            }
        };

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    )
        return {
            fingerprint: a.fingerprint,

            mangopayWalletTransferApproval: {
                nonce: r.nonce,
                signature
            }
        };

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

    p = subprocess.run(
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

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        p.stdout
    )


# ============================================================
# BUILD INPUTS
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
        result["type"] = "DIRECT_OFFER"

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
        result["receiverSlug"] = receiver

    if "settlementCurrencies" in fields:
        result[
            "settlementCurrencies"
        ] = ["EUR"]

    if "clientMutationId" in fields:
        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

    return result


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
        result[
            "receiveAssetIds"
        ] = asset_ids

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

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

def counter_offer(offer, cards):
    sender = (
        offer.get("sender") or {}
    )

    receiver = str(
        sender.get("slug") or ""
    ).strip()

    asset_ids = [
        str(c.get("assetId")).strip()
        for c in cards
        if c.get("assetId")
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

    except Exception as e:
        print(
            f"❌ Costruzione "
            f"prepareOfferInput: {e}",
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
            prepareOffer(input: $input) {
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
        "input": prepare_input,
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

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
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

    errors = result.get(
        "errors"
    ) or []

    if errors:
        for e in errors:
            print(
                f"❌ prepareOffer: "
                f"{e.get('message', 'Errore')}",
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
        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ Firma: {e}",
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

    except Exception as e:
        print(
            f"❌ Costruzione "
            f"createDirectOfferInput: {e}",
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
        "input": create_input,
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

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
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

    errors = result.get(
        "errors"
    ) or []

    if errors:
        for e in errors:
            print(
                f"❌ createDirectOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True,
            )

        return False

    token_offer = (
        result.get(
            "tokenOffer"
        )
        or {}
    )

    offer_id = token_offer.get(
        "id"
    )

    if not offer_id:
        print(
            "❌ Nessuna offerta "
            "creata da Sorare",
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
        offer.get("id") or ""
    ).strip()

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

        processed.add(
            offer_id
        )

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

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        print(
            "⏭️ Kulenovic non presente: "
            "ignoro",
            flush=True,
        )

        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not ids:
        print(
            "❌ Nessuna carta offerta",
            flush=True,
        )

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

        except Exception as e:
            print(
                f"❌ Errore controllo "
                f"carta: {e}",
                flush=True,
            )

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/"
        f"{len(cards)}",
        flush=True,
    )

    if not valid_cards:
        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        reject_offer(
            offer
        )

        return

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

    if counter_offer(
        offer,
        valid_cards,
    ):
        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

        if not reject_offer(
            offer
        ):
            print(
                "⚠️ Impossibile rifiutare "
                "l'offerta originale",
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
        "📦 VERSIONE BOT: 17.0",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} "
        f"per carta",
        flush=True,
    )

    print(
        f"📊 Range FLOOR: "
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
        "💰 FLOOR: prezzo corrente "
        "+ ultime 5 quotazioni pubbliche",
        flush=True,
    )

    print(
        "🌍 STORICO: tutte le stagioni "
        "del giocatore",
        flush=True,
    )

    print(
        "⚽ COLLECTION STORICO: FOOTBALL",
        flush=True,
    )

    print(
        "📉 FLOOR FINALE: "
        "minimo assoluto",
        flush=True,
    )

    print(
        "🔧 PREPARE: schema LIVE + "
        "settlementCurrencies EUR",
        flush=True,
    )

    print(
        "🔧 CREATE: createDirectOffer",
        flush=True,
    )

    print(
        "🚫 PREPARE: nessun "
        "exchangeRateId forzato",
        flush=True,
    )

    print(
        "🚫 PREPARE: type inviato solo "
        "se presente nello schema LIVE",
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

                except Exception as e:
                    print(
                        f"❌ Errore offerta: "
                        f"{e}",
                        flush=True,
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as e:
            print(
                f"❌ Worker: {e}",
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
        "prepare_mode":
            "LIVE_SCHEMA_AWARE",
        "settlement_currency":
            "EUR",
        "exchange_rate_mode":
            "NOT_FORCED_IN_PREPARE",
        "floor_mode":
            "CURRENT_MIN_PLUS_LAST_5_PUBLIC_PRICES",
        "historical_sales":
            "LAST_5_PUBLIC_PRICES",
        "historical_collection":
            "FOOTBALL",
        "historical_seasons":
            "ALL_SEASONS",
        "floor_selection":
            "ABSOLUTE_MINIMUM",
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "17.0",
    })


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
