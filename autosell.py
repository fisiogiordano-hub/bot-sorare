import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

SORARE_URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70
MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

# Kulenovic NON deve mai essere venduto
KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-4.0-NO-COVERAGE"

# ============================================================
# LOCK
# ============================================================

state_lock = threading.RLock()
worker_lock = threading.Lock()

worker_started = False


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def now():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def label(card):
    return (
        card.get("name")
        or card.get("slug")
        or card.get("assetId")
        or card.get("asset_id")
        or "Carta"
    )


def eur(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# STATE
# ============================================================

def ensure_state():
    directory = os.path.dirname(
        os.path.abspath(STATE_FILE)
    )

    os.makedirs(directory, exist_ok=True)

    if not os.path.exists(STATE_FILE):
        save_state([])


def load_state():
    ensure_state()

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

    except Exception as e:
        print(
            f"❌ Errore lettura state: {e}",
            flush=True
        )
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        cards = data.get("cards")

        if isinstance(cards, list):
            return cards

        if isinstance(cards, dict):
            return list(cards.values())

    return []


def state():
    with state_lock:
        return load_state()


def save_state(cards):
    with state_lock:
        tmp = f"{STATE_FILE}.{uuid.uuid4()}.tmp"

        try:
            with open(
                tmp,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    cards,
                    f,
                    ensure_ascii=False,
                    indent=2
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp, STATE_FILE)
            return True

        except Exception as e:
            print(
                f"❌ Errore scrittura state: {e}",
                flush=True
            )

            try:
                os.remove(tmp)
            except Exception:
                pass

            return False


def get_asset_id(card):
    return str(
        card.get("asset_id")
        or card.get("assetId")
        or ""
    ).strip()


def find_card(asset_id):
    wanted = norm(asset_id)

    cards = load_state()

    for index, card in enumerate(cards):
        if norm(get_asset_id(card)) == wanted:
            return cards, index

    return cards, None


def update_card(
    asset_id,
    status=None,
    offer_id=None,
    error=None
):
    with state_lock:
        cards, index = find_card(asset_id)

        if index is None:
            print(
                f"⚠️ Carta non presente nello state: {asset_id}",
                flush=True
            )
            return False

        card = cards[index]

        if status is not None:
            card["status"] = status

        if offer_id is not None:
            card["sale_offer_id"] = offer_id

        if error is not None:
            card["last_error"] = error
        elif status in ("READY", "SELLING", "SOLD"):
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = now()

        if status == "SOLD":
            card["sold_at"] = now()

        return save_state(cards)


def sellable_cards():
    with state_lock:
        cards = load_state()

        result = []

        for card in cards:
            if not isinstance(card, dict):
                continue

            status = norm(card.get("status"))

            # Supporta sia READY sia DA_VENDERE
            if status not in (
                "ready",
                "da_vendere"
            ):
                continue

            if not get_asset_id(card):
                continue

            result.append(dict(card))

        return result


# ============================================================
# SORARE GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    authorization = TOKEN

    if not authorization.lower().startswith("bearer "):
        authorization = f"Bearer {authorization}"

    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


def gql(query, variables=None):
    for attempt in range(3):
        try:
            response = requests.post(
                SORARE_URL,
                json={
                    "query": query,
                    "variables": variables or {}
                },
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True
            )

            if response.status_code == 429:
                time.sleep(2 + attempt)
                continue

            if response.status_code != 200:
                print(
                    f"❌ Sorare: "
                    f"{response.text[:1000]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            data = response.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:2000],
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


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    data = gql("""
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


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_id):
    data = gql("""
        query Cards($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                slug
                name
                rarityTyped
                seasonYear
                anyPlayer {
                    slug
                    displayName
                }
            }
        }
    """, {
        "ids": [asset_id]
    })

    if not data or data.get("errors"):
        return None

    cards = (
        ((data.get("data") or {})
        .get("anyCards")) or []
    )

    return cards[0] if cards else None


# ============================================================
# PRICE
# ============================================================

def offer_price(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        value = int(
            amounts.get("eurCents") or 0
        )

        if value > 0:
            return value

    except Exception:
        pass

    return None


def get_floor(card):
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
    except Exception:
        return None

    if not player_slug or not rarity:
        return None

    data = gql("""
        query Live($slug: String, $first: Int) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug: $slug
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
        "slug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):
        return None

    offers = (
        (((data.get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:
        sender = offer.get("senderSide") or {}

        cards = sender.get("anyCards") or []

        for listed_card in cards:
            listed_player = (
                listed_card.get("anyPlayer")
                or {}
            )

            try:
                listed_season = int(
                    listed_card.get("seasonYear")
                )
            except Exception:
                continue

            if (
                norm(listed_player.get("slug"))
                == player_slug
                and norm(
                    listed_card.get("rarityTyped")
                ) == rarity
                and listed_season == season
            ):
                amounts = (
                    offer.get("receiverSide") or {}
                ).get("amounts") or {}

                price = offer_price(amounts)

                if price is not None:
                    prices.append(price)

                break

    if len(prices) < MIN_LISTINGS:
        print(
            f"⚠️ Floor {player_slug}: "
            f"{len(prices)}/{MIN_LISTINGS} listing",
            flush=True
        )
        return None

    floor_price = min(prices)

    print(
        f"📉 FLOOR: {eur(floor_price)} "
        f"({len(prices)} listing)",
        flush=True
    )

    return floor_price


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):
    return (
        norm(card.get("slug"))
        == norm(KULENOVIC_SLUG)
        or
        norm(card.get("assetId"))
        == norm(KULENOVIC_ASSET)
    )


def validate_card(card):
    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(
        card.get("rarityTyped")
    ).upper() != "LIMITED":
        return False, "RARITY"

    floor_price = get_floor(card)

    if floor_price is None:
        return False, "FLOOR_UNKNOWN"

    if floor_price < MIN_PRICE:
        return False, "FLOOR_LOW"

    if floor_price > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, floor_price


# ============================================================
# SIGN
# ============================================================

def sign(authorizations):
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
            "SORARE_STARK_PRIVATE_KEY mancante"
        )

    javascript = r'''
const fs = require("fs");
const { signAuthorizationRequest } =
    require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function signOne(auth) {
    const request = auth.request;

    if (!request) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        request.__typename ===
        "StarkexTransferAuthorizationRequest"
        && request.amount != null
    ) {
        request.amount = BigInt(request.amount);
    }

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            request
        );

    if (
        request.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {
        return {
            fingerprint: auth.fingerprint,
            starkexTransferApproval: {
                nonce: request.nonce,
                expirationTimestamp:
                    request.expirationTimestamp,
                signature
            }
        };
    }

    if (
        request.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {
        return {
            fingerprint: auth.fingerprint,
            starkexLimitOrderApproval: {
                nonce: request.nonce,
                expirationTimestamp:
                    request.expirationTimestamp,
                signature
            }
        };
    }

    if (
        request.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {
        return {
            fingerprint: auth.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: request.nonce,
                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: "
        + request.__typename
    );
}

const result =
    input.authorizations.map(signOne);

process.stdout.write(
    JSON.stringify(result)
);
'''

    process = subprocess.run(
        [node, "-e", javascript],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
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
# CREATE SALE
# ============================================================

def create_sale(card, price):
    asset_id = get_asset_id(card)

    if not asset_id:
        return None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → "
            f"{label(card)} a {eur(price)}",
            flush=True
        )
        return "DRY-RUN"

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    data = gql("""
        mutation Prepare(
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
        "input": {
            "type": "SINGLE_SALE_OFFER",
            "sendAssetIds": [asset_id],
            "receiveAssetIds": [],
            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },
            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print(
            "❌ prepareOffer: nessun risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

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
        result.get("authorizations") or []
    )

    if not authorizations:
        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )
        return None

    # --------------------------------------------------------
    # SIGN
    # --------------------------------------------------------

    try:
        approvals = sign(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True
        )
        return None

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    data = gql("""
        mutation Create(
            $input: createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(input: $input) {
                tokenOffer {
                    id
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": {
            "approvals": approvals,
            "dealId": str(uuid.uuid4()),
            "assetId": asset_id,
            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },
            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print(
            "❌ createSingleSaleOffer: "
            "nessun risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return None

    offer_id = (
        result.get("tokenOffer") or {}
    ).get("id")

    if not offer_id:
        print(
            "❌ Inserzione non creata",
            flush=True
        )
        return None

    print(
        f"✅ INSERZIONE CREATA: {offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):
    asset_id = get_asset_id(row)

    if not asset_id:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset_id}",
        flush=True
    )

    # --------------------------------------------------------
    # Recupera dati aggiornati da Sorare
    # --------------------------------------------------------

    card = card_details(asset_id)

    if not card:
        print(
            "❌ Carta non recuperabile → "
            "rimane DA VENDERE",
            flush=True
        )

        update_card(
            asset_id,
            "DA_VENDERE",
            error="CARD_DETAILS"
        )
        return

    print(
        f"🃏 {label(card)}",
        flush=True
    )

    # --------------------------------------------------------
    # Validazione
    # --------------------------------------------------------

    valid_card, result = validate_card(
        card
    )

    if not valid_card:

        messages = {
            "KULENOVIC":
                "KULENOVIC PROTETTO",

            "RARITY":
                "RARITÀ NON LIMITED",

            "FLOOR_UNKNOWN":
                "FLOOR NON DISPONIBILE",

            "FLOOR_LOW":
                "FLOOR SOTTO €0.32",

            "FLOOR_HIGH":
                "FLOOR SOPRA €0.70"
        }

        print(
            f"🚫 ESCLUSA: {label(card)} → "
            f"{messages.get(result, result)}",
            flush=True
        )

        # Solo se il problema è temporaneo,
        # lasciamo la carta nuovamente disponibile.
        if result == "FLOOR_UNKNOWN":
            update_card(
                asset_id,
                "DA_VENDERE",
                error=result
            )
        else:
            update_card(
                asset_id,
                "BLOCKED",
                error=result
            )

        return

    price = result

    print(
        f"✅ CARTA VALIDA → "
        f"{label(card)} | Floor {eur(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # LOCK
    # --------------------------------------------------------

    if not update_card(
        asset_id,
        "SELLING"
    ):
        print(
            "❌ Impossibile impostare SELLING",
            flush=True
        )
        return

    # --------------------------------------------------------
    # CREATE SALE
    # --------------------------------------------------------

    offer_id = create_sale(
        card,
        price
    )

    if not offer_id:

        print(
            "❌ INSERZIONE FALLITA → "
            "carta nuovamente DA_VENDERE",
            flush=True
        )

        update_card(
            asset_id,
            "DA_VENDERE",
            error="CREATE_SALE_FAILED"
        )

        return

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    update_card(
        asset_id,
        "SELLING",
        offer_id=offer_id
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(card)} | "
        f"{eur(price)} | "
        f"{offer_id}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():
    with state_lock:
        cards = load_state()

        selling = [
            c for c in cards
            if norm(c.get("status"))
            == "selling"
        ]

    if not selling:
        print(
            "🔄 Recovery: "
            "nessuna carta SELLING.",
            flush=True
        )
        return

    print(
        f"🛡️ Recovery: "
        f"{len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:
        print(
            f"   └─ {get_asset_id(card)} "
            f"| offer="
            f"{card.get('sale_offer_id')}",
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
        f"📦 VERSIONE: {VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        "💰 RANGE: €0.32 - €0.70",
        flush=True
    )

    print(
        f"📊 LISTING MINIME: {MIN_LISTINGS}",
        flush=True
    )

    print(
        "🎂 ETÀ: NON UTILIZZATA",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI VENDUTO",
        flush=True
    )

    print(
        "🛡️ COVERAGE: DISABILITATA",
        flush=True
    )

    print(
        "🛡️ SOURCE: AUTOBUY / SWAP",
        flush=True
    )

    print(
        f"💾 STORAGE: {STATE_FILE}",
        flush=True
    )

    try:
        headers()
        ensure_state()

    except Exception as e:
        print(
            f"❌ Configurazione: {e}",
            flush=True
        )
        return

    if not check_account():
        return

    recovery()

    while True:
        try:
            rows = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: "
                f"{len(rows)}",
                flush=True
            )

            for row in rows:
                try:
                    process_card(row)

                except Exception as e:
                    asset_id = get_asset_id(row)

                    print(
                        f"❌ AutoSell "
                        f"{asset_id}: {e}",
                        flush=True
                    )

                    if asset_id:
                        update_card(
                            asset_id,
                            "DA_VENDERE",
                            error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        thread = threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        )

        thread.start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


@app.get("/")
def home():
    with state_lock:
        cards = load_state()

    ready = 0
    selling = 0

    for card in cards:
        status = norm(
            card.get("status")
        )

        if status in (
            "ready",
            "da_vendere"
        ):
            ready += 1

        elif status == "selling":
            selling += 1

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "range": "€0.32-€0.70",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "age": "NOT_USED",
        "kulenovic": "NEVER_SELL",
        "coverage": "DISABLED",
        "storage": STATE_FILE,
        "cards": len(cards),
        "ready": ready,
        "selling": selling,
        "worker": worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": VERSION,
        "worker": worker_started,
        "dry_run": DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():
    with state_lock:
        data = load_state()

    return jsonify({
        "count": len(data),
        "cards": data
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
        )
    )
