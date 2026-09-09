import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import requests

from flask import Flask, jsonify


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

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

# ------------------------------------------------------------
# KULENOVIC PROTETTO
# ------------------------------------------------------------

KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-7.0-FIX-PREPARE"


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


def asset_id(card):
    return str(
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()


def label(card):
    return (
        card.get("name")
        or card.get("slug")
        or card.get("assetId")
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
    path = os.path.abspath(STATE_FILE)

    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        save_state([])


def raw_state():
    ensure_state()

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            f"❌ Errore lettura state: {e}",
            flush=True
        )
        return {}


def extract_cards(data):
    # Il tuo JSON reale contiene:
    # {
    #   "processed_offers": [],
    #   "acquired_cards": [],
    #   "pending_autobuys": []
    # }

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    cards = data.get("acquired_cards")

    if isinstance(cards, list):
        return cards

    cards = data.get("cards")

    if isinstance(cards, list):
        return cards

    if isinstance(cards, dict):
        return list(cards.values())

    return []


def state():
    with state_lock:
        return extract_cards(raw_state())


def save_state(cards):
    """
    IMPORTANTE:
    preserva la struttura reale del bot_state.json.
    """

    with state_lock:

        data = raw_state()

        if not isinstance(data, dict):
            data = {}

        data["acquired_cards"] = cards
        data["updated_at"] = int(time.time())

        tmp = (
            f"{STATE_FILE}."
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
                STATE_FILE
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


def find_card(asset):
    wanted = norm(asset)

    cards = state()

    for i, card in enumerate(cards):

        if norm(asset_id(card)) == wanted:
            return cards, i

    return cards, None


def update_card(
    asset,
    status=None,
    offer_id=None,
    error=None
):
    with state_lock:

        cards, index = find_card(asset)

        if index is None:

            print(
                f"⚠️ Carta non presente: {asset}",
                flush=True
            )

            return False

        card = cards[index]

        if status is not None:
            card["status"] = status

        if offer_id:
            card["sale_offer_id"] = offer_id

        if error is not None:
            card["last_error"] = error

        elif status == "SELLING":
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = now()

        return save_state(cards)


def sellable_cards():
    result = []

    for card in state():

        if not isinstance(card, dict):
            continue

        status = norm(
            card.get("status")
        )

        if status not in {
            "da_vendere",
            "ready"
        }:
            continue

        if not asset_id(card):
            continue

        result.append(dict(card))

    return result


# ============================================================
# SORARE GRAPHQL
# ============================================================

def auth_headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    headers = {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-AutoSell"
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
                headers=auth_headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {response.status_code}",
                flush=True
            )

            if response.status_code == 429:

                time.sleep(
                    2 + attempt * 2
                )

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
                    )[:2500],
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
        query Card($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                slug
                name
                rarityTyped
                seasonYear

                anyPlayer {
                    slug
                    displayName

                    activeClub {
                        slug
                        name
                    }
                }
            }
        }
    """, {
        "ids": [asset]
    })

    cards = (
        ((data or {}).get("data") or {})
        .get("anyCards")
        or []
    )

    return cards[0] if cards else None


# ============================================================
# FLOOR
# ============================================================

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
        query LiveOffers(
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

    offers = (
        (((data or {}).get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in offers:

        sender = offer.get("senderSide") or {}

        listed_cards = (
            sender.get("anyCards") or []
        )

        for listed in listed_cards:

            listed_player = (
                listed.get("anyPlayer") or {}
            )

            if norm(
                listed_player.get("slug")
            ) != player_slug:
                continue

            if norm(
                listed.get("rarityTyped")
            ) != rarity:
                continue

            try:

                if int(
                    listed.get("seasonYear")
                ) != season:
                    continue

            except Exception:
                continue

            amounts = (
                offer.get("receiverSide") or {}
            ).get("amounts") or {}

            try:

                value = int(
                    amounts.get("eurCents") or 0
                )

                if value > 0:
                    prices.append(value)

            except Exception:
                pass

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
# VALIDAZIONE
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
        or norm(card.get("slug")) in wanted
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
# SIGNATURE
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
            "SORARE_STARK_PRIVATE_KEY mancante"
        )

    js = r"""
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

  const type = request.__typename;

  if (
    type !==
      "StarkexTransferAuthorizationRequest" &&
    type !==
      "StarkexLimitOrderAuthorizationRequest" &&
    type !==
      "MangopayWalletTransferAuthorizationRequest"
  ) {
    throw new Error(
      "Authorization non Stark/Mangopay: " + type
    );
  }

  if (
    type ===
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
    type ===
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
    type ===
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

  return {
    fingerprint: auth.fingerprint,

    mangopayWalletTransferApproval: {
      nonce: request.nonce,
      signature
    }
  };
}

process.stdout.write(
  JSON.stringify(
    input.authorizations.map(signOne)
  )
);
"""

    payload = {
        "privateKey": STARK,
        "authorizations": authorizations
    }

    process = subprocess.run(
        [node, "-e", js],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if process.returncode != 0:

        raise RuntimeError(
            process.stderr.strip()
            or "Firma fallita"
        )

    try:
        return json.loads(
            process.stdout
        )
    except Exception:
        raise RuntimeError(
            "Output firma non valido"
        )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price):

    asset = asset_id(card)

    if not asset:
        return None

    if DRY_RUN:

        print(
            f"🟡 DRY RUN → "
            f"{label(card)} → {eur(price)}",
            flush=True
        )

        return "DRY-RUN"

    # --------------------------------------------------------
    # PREPARE OFFER
    # --------------------------------------------------------
    #
    # NOTA IMPORTANTE:
    #
    # NON inseriamo:
    #
    #     type: "SINGLE_SALE_OFFER"
    #
    # perché il tuo endpoint attuale ha risposto:
    #
    # Field is not defined on prepareOfferInput
    #
    # --------------------------------------------------------

    prepare_query = """
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
    """

    prepare_input = {
        "sendAssetIds": [asset],
        "receiveAssetIds": [],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": str(uuid.uuid4())
    }

    data = gql(
        prepare_query,
        {"input": prepare_input}
    )

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
            )[:2500],
            flush=True
        )

        return None

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:

        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )

        return None

    # --------------------------------------------------------
    # DEBUG AUTHORIZATION TYPE
    # --------------------------------------------------------

    for auth in authorizations:

        request = (
            auth.get("request")
            or {}
        )

        print(
            "🔐 Authorization → "
            f"{request.get('__typename')}",
            flush=True
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
            flush=True
        )

        return None

    # --------------------------------------------------------
    # CREATE SINGLE SALE
    # --------------------------------------------------------

    create_query = """
        mutation CreateSingleSale(
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
    """

    create_input = {
        "approvals": approvals,

        "dealId": str(
            uuid.uuid4()
        ),

        "assetId": asset,

        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },

        "clientMutationId": str(
            uuid.uuid4()
        )
    }

    data = gql(
        create_query,
        {"input": create_input}
    )

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
            )[:2500],
            flush=True
        )

        return None

    offer_id = (
        (result.get("tokenOffer") or {})
        .get("id")
    )

    if not offer_id:

        print(
            "❌ Vendita non creata: "
            "offer ID assente",
            flush=True
        )

        return None

    print(
        f"✅ INSERZIONE CREATA → "
        f"{offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process(card):

    asset = asset_id(card)

    if not asset:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset}",
        flush=True
    )

    details = card_details(asset)

    if not details:

        print(
            "❌ Carta non recuperabile",
            flush=True
        )

        update_card(
            asset,
            "da_vendere",
            error="CARD_DETAILS"
        )

        return

    print(
        f"🃏 {label(details)}",
        flush=True
    )

    ok, result = validate(details)

    if not ok:

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
            f"🚫 ESCLUSA → "
            f"{messages.get(result, result)}",
            flush=True
        )

        if result in {
            "KULENOVIC",
            "RARITY"
        }:

            update_card(
                asset,
                "BLOCKED",
                error=result
            )

        else:

            update_card(
                asset,
                "da_vendere",
                error=result
            )

        return

    price = result

    print(
        f"✅ CARTA VALIDA → "
        f"floor {eur(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # BLOCCO PRE-VENDITA
    # --------------------------------------------------------

    if not update_card(
        asset,
        "SELLING"
    ):

        print(
            "❌ Impossibile impostare SELLING",
            flush=True
        )

        return

    # --------------------------------------------------------
    # CREA INSERZIONE
    # --------------------------------------------------------

    offer_id = create_sale(
        details,
        price
    )

    if not offer_id:

        update_card(
            asset,
            "da_vendere",
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
        "SELLING",
        offer_id=offer_id,
        error=None
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(details)} | "
        f"{eur(price)} | "
        f"{offer_id}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():

    selling = []

    for card in state():

        if norm(
            card.get("status")
        ) == "selling":

            selling.append(card)

    if not selling:

        print(
            "🔄 Recovery: "
            "nessuna carta SELLING.",
            flush=True
        )

        return

    print(
        f"🔄 Recovery: "
        f"{len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:

        print(
            f"   └─ {asset_id(card)} "
            f"| offer={card.get('sale_offer_id')}",
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

        auth_headers()
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

                    asset = asset_id(card)

                    print(
                        f"❌ AutoSell "
                        f"{asset}: {e}",
                        flush=True
                    )

                    if asset:

                        update_card(
                            asset,
                            "da_vendere",
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

    cards = state()

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
        "source": "AUTOBUY / SWAP",

        "storage": STATE_FILE,

        "cards": len(cards),

        "da_vendere": len([
            c for c in cards
            if norm(c.get("status"))
            in {
                "da_vendere",
                "ready"
            }
        ]),

        "selling": len([
            c for c in cards
            if norm(c.get("status"))
            == "selling"
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

    cards = state()

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
