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

BOT_STATE_PATH = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-7.0-FIX-ACQUIRED-CARDS"

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


def get_asset(card):
    return str(
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()


def card_label(card):
    return (
        card.get("name")
        or card.get("slug")
        or get_asset(card)
        or "Carta"
    )


def euro(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


# ============================================================
# STATE
# ============================================================

def ensure_state():
    path = os.path.abspath(BOT_STATE_PATH)
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        data = {
            "processed_offers": [],
            "acquired_cards": [],
            "pending_autobuys": [],
            "updated_at": int(time.time())
        }

        save_raw_state(data)


def load_state():
    ensure_state()

    try:
        with open(
            BOT_STATE_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            print(
                "❌ bot_state.json non è un oggetto JSON",
                flush=True
            )
            return {
                "processed_offers": [],
                "acquired_cards": [],
                "pending_autobuys": []
            }

        if not isinstance(
            data.get("acquired_cards"),
            list
        ):
            data["acquired_cards"] = []

        return data

    except Exception as e:
        print(
            f"❌ Errore lettura bot_state.json: {e}",
            flush=True
        )

        return {
            "processed_offers": [],
            "acquired_cards": [],
            "pending_autobuys": []
        }


def save_raw_state(data):
    tmp = (
        f"{BOT_STATE_PATH}."
        f"{uuid.uuid4().hex}.tmp"
    )

    try:
        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(
            tmp,
            BOT_STATE_PATH
        )

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


def acquired_cards():
    with state_lock:
        data = load_state()

        return [
            dict(card)
            for card in data.get(
                "acquired_cards",
                []
            )
            if isinstance(card, dict)
        ]


def find_card(asset):
    wanted = norm(asset)

    with state_lock:
        data = load_state()

        cards = data["acquired_cards"]

        for index, card in enumerate(cards):

            if norm(get_asset(card)) == wanted:
                return data, index

    return None, None


def update_card(
    asset,
    status=None,
    offer_id=None,
    error=None
):
    with state_lock:

        data, index = find_card(asset)

        if data is None or index is None:

            print(
                f"⚠️ Carta non trovata nello state: {asset}",
                flush=True
            )

            return False

        card = data["acquired_cards"][index]

        if status is not None:
            card["status"] = status

        if offer_id:
            card["sale_offer_id"] = offer_id

        if error is not None:
            card["last_error"] = error
        elif status in {
            "SELLING",
            "da_vendere"
        }:
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = now()

        data["updated_at"] = int(time.time())

        return save_raw_state(data)


def sellable_cards():
    """
    Il bot AutoBuy salva le carte con:

        status = da_vendere

    Accettiamo anche READY per compatibilità
    con eventuali vecchi record.
    """

    result = []

    for card in acquired_cards():

        status = norm(
            card.get("status")
        )

        if status not in {
            "da_vendere",
            "ready"
        }:
            continue

        if not get_asset(card):
            continue

        result.append(card)

    return result


# ============================================================
# SORARE API
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    result = {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


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

                time.sleep(
                    min(2 + attempt * 2, 8)
                )

                continue

            if response.status_code != 200:

                print(
                    f"❌ Sorare: "
                    f"{response.text[:1200]}",
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

def card_details(asset):

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
        "ids": [asset]
    })

    if not data or data.get("errors"):
        return None

    cards = (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )

    return cards[0] if cards else None


# ============================================================
# PRICE
# ============================================================

def usd_to_eur(usd_cents):

    try:

        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR"
            },
            timeout=10
        )

        rate = float(
            response.json()["rates"]["EUR"]
        )

        return round(
            usd_cents * rate
        )

    except Exception as e:

        print(
            f"⚠️ Cambio USD/EUR: {e}",
            flush=True
        )

        return None


def offer_price(amounts):

    if not isinstance(amounts, dict):
        return None

    try:

        eur_cents = int(
            amounts.get("eurCents") or 0
        )

        if eur_cents > 0:
            return eur_cents

    except Exception:
        pass

    try:

        usd_cents = float(
            amounts.get("usdCents") or 0
        )

        if usd_cents > 0:
            return usd_to_eur(
                usd_cents
            )

    except Exception:
        pass

    return None


def floor(card):

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
        query Live(
            $slug: String,
            $first: Int
        ) {
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
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender = (
            offer.get("senderSide")
            or {}
        )

        listed_cards = (
            sender.get("anyCards")
            or []
        )

        for listed in listed_cards:

            listed_player = (
                listed.get("anyPlayer")
                or {}
            )

            try:
                listed_season = int(
                    listed.get("seasonYear")
                )
            except Exception:
                continue

            same = (
                norm(
                    listed_player.get("slug")
                ) == player_slug
                and
                norm(
                    listed.get("rarityTyped")
                ) == rarity
                and
                listed_season == season
            )

            if not same:
                continue

            amounts = (
                offer.get("receiverSide")
                or {}
            ).get("amounts")

            price = offer_price(amounts)

            if price is not None:
                prices.append(price)

            break

    print(
        f"📊 Listing trovate: "
        f"{len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    if len(prices) < MIN_LISTINGS:
        return None

    return min(prices)


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KULENOVIC_SLUG),
        norm(KULENOVIC_ASSET)
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


def validate(card):

    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(
        card.get("rarityTyped")
    ).upper() != "LIMITED":
        return False, "RARITY"

    price = floor(card)

    if price is None:
        return False, "FLOOR_UNKNOWN"

    if price < MIN_PRICE:
        return False, "FLOOR_LOW"

    if price > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, price


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

    js = r'''
const fs = require("fs");
const {
    signAuthorizationRequest
} = require("@sorare/crypto");

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
        &&
        request.amount != null
    ) {
        request.amount = BigInt(
            request.amount
        );
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

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(signOne)
    )
);
'''

    process = subprocess.run(
        [node, "-e", js],
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

    asset = get_asset(card)

    if not asset:
        return None

    if DRY_RUN:

        print(
            f"🟡 DRY RUN → "
            f"{card_label(card)} → "
            f"{euro(price)}",
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
            "sendAssetIds": [asset],
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
        result.get("authorizations")
        or []
    )

    if not authorizations:

        print(
            "❌ Nessuna authorization",
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
            createSingleSaleOffer(
                input: $input
            ) {

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
            "assetId": asset,
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
        (result.get("tokenOffer") or {})
        .get("id")
    )

    if not offer_id:

        print(
            "❌ Inserzione non creata: "
            "offer ID assente",
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

def process(card):

    asset = get_asset(card)

    if not asset:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset}",
        flush=True
    )

    details = card_details(asset)

    if not details:

        print(
            "❌ Carta non recuperabile → RITENTO",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error="CARD_DETAILS"
        )

        return

    print(
        f"🃏 {card_label(details)}",
        flush=True
    )

    valid_card, result = validate(
        details
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
            f"🚫 ESCLUSA: "
            f"{messages.get(result, result)}",
            flush=True
        )

        if result in {
            "KULENOVIC",
            "RARITY"
        }:

            update_card(
                asset,
                status="BLOCKED",
                error=result
            )

        else:

            update_card(
                asset,
                status="da_vendere",
                error=result
            )

        return

    price = result

    print(
        f"✅ CARTA VALIDA → "
        f"floor {euro(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # LOCK
    # --------------------------------------------------------

    if not update_card(
        asset,
        status="SELLING"
    ):

        print(
            "❌ Impossibile impostare SELLING",
            flush=True
        )

        return

    # --------------------------------------------------------
    # SALE
    # --------------------------------------------------------

    offer_id = create_sale(
        details,
        price
    )

    if not offer_id:

        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "🔁 Carta rimessa in DA_VENDERE",
            flush=True
        )

        return

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        error=None
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{card_label(details)} | "
        f"{euro(price)} | "
        f"{offer_id}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():

    selling = [
        card
        for card in acquired_cards()
        if norm(card.get("status")) == "selling"
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
            f"   └─ {get_asset(card)} "
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
        f"💾 STORAGE: {BOT_STATE_PATH}",
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

            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: "
                f"{len(cards)}",
                flush=True
            )

            for card in cards:

                try:
                    process(card)

                except Exception as e:

                    asset = get_asset(card)

                    print(
                        f"❌ AutoSell "
                        f"{asset}: {e}",
                        flush=True
                    )

                    if asset:

                        update_card(
                            asset,
                            status="da_vendere",
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

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


@app.get("/")
def home():

    cards = acquired_cards()

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
        "storage": BOT_STATE_PATH,
        "cards": len(cards),
        "da_vendere": len([
            c for c in cards
            if norm(c.get("status"))
            in {"da_vendere", "ready"}
        ]),
        "selling": len([
            c for c in cards
            if norm(c.get("status")) == "selling"
        ]),
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

    cards = acquired_cards()

    return jsonify({
        "count": len(cards),
        "cards": cards
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
