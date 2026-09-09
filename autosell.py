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

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70
MIN_LISTINGS = 5

STATE_PATH = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

# ------------------------------------------------------------
# Kulenovic: MAI vendere
# ------------------------------------------------------------

KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-5.0-NO-COVERAGE"

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


def timestamp():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def card_name(card):
    return (
        card.get("name")
        or card.get("slug")
        or card.get("assetId")
        or card.get("asset_id")
        or "Carta"
    )


def euro(cents):
    if cents is None:
        return "N/D"

    return f"€{cents / 100:.2f}"


def asset_id(card):
    return str(
        card.get("asset_id")
        or card.get("assetId")
        or ""
    ).strip()


# ============================================================
# STATE
# ============================================================

def ensure_state():
    path = os.path.abspath(STATE_PATH)

    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        save_state([])


def read_state():
    ensure_state()

    try:
        with open(
            STATE_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            f"❌ Errore lettura state: {e}",
            flush=True
        )
        return []


def extract_cards(data):
    """
    Supporta:

    [
        {...}
    ]

    oppure:

    {
        "cards": [...]
    }

    oppure:

    {
        "cards": {
            "id": {...}
        }
    }
    """

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    cards = data.get("cards")

    if isinstance(cards, list):
        return cards

    if isinstance(cards, dict):
        return list(cards.values())

    return []


def state():
    with state_lock:
        return extract_cards(read_state())


def save_state(cards):
    with state_lock:

        tmp = (
            f"{STATE_PATH}."
            f"{uuid.uuid4().hex}.tmp"
        )

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

            os.replace(
                tmp,
                STATE_PATH
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

    for index, card in enumerate(cards):

        current = (
            card.get("asset_id")
            or card.get("assetId")
        )

        if norm(current) == wanted:
            return cards, index

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
                f"⚠️ Carta non presente nello state: {asset}",
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

        elif status in (
            "SELLING",
            "SOLD"
        ):
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = timestamp()

        if status == "SOLD":
            card["sold_at"] = timestamp()

        return save_state(cards)


# ============================================================
# CARTE DA VENDERE
# ============================================================

def sellable_cards():
    """
    IMPORTANTE:

    Il vecchio bot usa:
        status = "da_vendere"

    Alcune versioni usavano:
        status = "ready"

    Autosell accetta entrambi.

    Non consideriamo vendibili:
        SELLING
        SOLD
        BLOCKED
        ERROR
    """

    allowed = {
        "da_vendere",
        "ready"
    }

    result = []

    with state_lock:

        for card in state():

            if not isinstance(card, dict):
                continue

            status = norm(
                card.get("status")
            )

            aid = asset_id(card)

            if not aid:
                continue

            if status not in allowed:
                continue

            result.append(dict(card))

    return result


# ============================================================
# HTTP / GRAPHQL
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    authorization = TOKEN

    if not authorization.lower().startswith(
        "bearer "
    ):
        authorization = (
            f"Bearer {authorization}"
        )

    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            f"Sorare-AutoSell/{VERSION}"
        )
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
                f"🌐 Sorare HTTP "
                f"{response.status_code}",
                flush=True
            )

            if response.status_code == 429:

                time.sleep(
                    min(attempt + 2, 10)
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

def get_card(asset):

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

    if not data:
        return None

    if data.get("errors"):
        return None

    cards = (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )

    if not cards:
        return None

    return cards[0]


# ============================================================
# PRICE
# ============================================================

def amount_to_eur(amounts):

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

    slug = norm(
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

    if not slug or not rarity:
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
                            }
                        }
                    }
                }
            }
        }
    """, {
        "slug": slug,
        "first": 50
    })

    if not data:
        return None

    if data.get("errors"):
        return None

    offers = (
        (((data.get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:

        sender = (
            offer.get("senderSide")
            or {}
        )

        cards = (
            sender.get("anyCards")
            or []
        )

        for listed in cards:

            listed_player = (
                listed.get("anyPlayer")
                or {}
            )

            try:
                same_season = (
                    int(
                        listed.get(
                            "seasonYear"
                        )
                    ) == season
                )
            except Exception:
                continue

            same_card = (
                norm(
                    listed_player.get("slug")
                ) == slug
                and
                norm(
                    listed.get(
                        "rarityTyped"
                    )
                ) == rarity
                and
                same_season
            )

            if not same_card:
                continue

            amount = (
                offer.get("receiverSide")
                or {}
            ).get("amounts")

            price = amount_to_eur(
                amount
            )

            if price is not None:
                prices.append(price)

            break

    if len(prices) < MIN_LISTINGS:

        print(
            f"⚠️ Floor {slug}: "
            f"{len(prices)}/{MIN_LISTINGS} listing",
            flush=True
        )

        return None

    return min(prices)


# ============================================================
# KULENOVIC
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


# ============================================================
# VALIDAZIONE
# ============================================================

def validate(card):

    if is_kulenovic(card):
        return False, "KULENOVIC"

    rarity = norm(
        card.get("rarityTyped")
    )

    if rarity != "limited":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor < MIN_PRICE:
        return False, "FLOOR_LOW"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, floor


# ============================================================
# FIRMA
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
        "Authorization non supportata: " +
        request.__typename
    );
}

const result =
    input.authorizations.map(signOne);

process.stdout.write(
    JSON.stringify(result)
);
'''

    process = subprocess.run(
        [
            node,
            "-e",
            javascript
        ],
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
# CREAZIONE INSERZIONE
# ============================================================

def create_sale(card, price):

    asset = asset_id(card)

    if not asset:
        return None

    if DRY_RUN:

        print(
            f"🟡 DRY RUN → "
            f"{card_name(card)} "
            f"a {euro(price)}",
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
            $input:
            createSingleSaleOfferInput!
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

    if offer_id:

        print(
            f"✅ INSERZIONE CREATA: "
            f"{offer_id}",
            flush=True
        )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):

    asset = asset_id(row)

    if not asset:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset}",
        flush=True
    )

    # --------------------------------------------------------
    # Recupera dati reali Sorare
    # --------------------------------------------------------

    card = get_card(asset)

    if not card:

        print(
            "❌ Carta non recuperabile",
            flush=True
        )

        # NON la blocchiamo definitivamente.
        # Rimane da_vendere e verrà ritentata.
        update_card(
            asset,
            "da_vendere",
            error="CARD_DETAILS"
        )

        return

    # --------------------------------------------------------
    # Protezione Kulenovic
    # --------------------------------------------------------

    if is_kulenovic(card):

        print(
            f"🔒 KULENOVIC → "
            f"{card_name(card)} "
            f"NON VENDUTO",
            flush=True
        )

        update_card(
            asset,
            "BLOCKED",
            error="KULENOVIC"
        )

        return

    # --------------------------------------------------------
    # Validazione
    # --------------------------------------------------------

    ok, result = validate(card)

    if not ok:

        reasons = {
            "RARITY":
                "RARITÀ NON LIMITED",

            "FLOOR_UNKNOWN":
                "FLOOR NON DISPONIBILE",

            "FLOOR_LOW":
                "FLOOR SOTTO €0.32",

            "FLOOR_HIGH":
                "FLOOR SOPRA €0.70",

            "KULENOVIC":
                "KULENOVIC PROTETTO"
        }

        reason = reasons.get(
            result,
            result
        )

        print(
            f"🚫 ESCLUSA: "
            f"{card_name(card)} "
            f"→ {reason}",
            flush=True
        )

        # ----------------------------------------------------
        # NON perdiamo la carta.
        #
        # Floor sconosciuto/basso/alto:
        # resta da_vendere e verrà riprovata.
        # ----------------------------------------------------

        update_card(
            asset,
            "da_vendere",
            error=result
        )

        return

    price = result

    print(
        f"✅ CARTA VALIDA: "
        f"{card_name(card)}",
        flush=True
    )

    print(
        f"   └─ Floor: {euro(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # Lock
    # --------------------------------------------------------

    if not update_card(
        asset,
        "SELLING"
    ):

        print(
            "❌ Impossibile impostare "
            "SELLING → STOP",
            flush=True
        )

        return

    # --------------------------------------------------------
    # Crea vendita
    # --------------------------------------------------------

    offer_id = create_sale(
        card,
        price
    )

    if not offer_id:

        print(
            "❌ INSERZIONE NON CREATA "
            "→ carta nuovamente da_vendere",
            flush=True
        )

        update_card(
            asset,
            "da_vendere",
            error="CREATE_SALE_FAILED"
        )

        return

    # --------------------------------------------------------
    # Inserzione creata
    # --------------------------------------------------------

    update_card(
        asset,
        "SELLING",
        offer_id=offer_id
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{card_name(card)} | "
        f"{euro(price)} | "
        f"{offer_id}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():

    with state_lock:

        selling = [
            c for c in state()
            if norm(
                c.get("status")
            ) == "selling"
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
            f"   └─ "
            f"{asset_id(card)} "
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
        f"📊 LISTING MINIME: "
        f"{MIN_LISTINGS}",
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
        f"💾 STORAGE: {STATE_PATH}",
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

                    process_card(card)

                except Exception as e:

                    aid = asset_id(card)

                    print(
                        f"❌ AutoSell "
                        f"{aid}: {e}",
                        flush=True
                    )

                    # Errore temporaneo:
                    # non perdiamo la carta.
                    if aid:
                        update_card(
                            aid,
                            "da_vendere",
                            error=str(e)
                        )

                # Piccola pausa tra le carte
                time.sleep(1)

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

    with state_lock:
        cards = state()

    selling = sum(
        1
        for c in cards
        if norm(
            c.get("status")
        ) == "selling"
    )

    sellable = sum(
        1
        for c in cards
        if norm(
            c.get("status")
        ) in {
            "da_vendere",
            "ready"
        }
    )

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "range": "€0.32-€0.70",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "age": "NOT_USED",
        "coverage": False,
        "kulenovic": "NEVER_SELL",
        "storage": STATE_PATH,
        "cards": len(cards),
        "da_vendere": sellable,
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
        data = state()

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
        ),
        debug=False
    )
