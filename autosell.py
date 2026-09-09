import os
import re
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
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70
MIN_LISTINGS = 5

STATE_FILE = os.getenv(
    "BOT_STATE_PATH",
    "bot_state.json"
).strip()

# ============================================================
# KULENOVIC - MAI VENDUTO
# ============================================================

KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-3.2-DA-VENDERE-FIX"

# ============================================================
# LOCK / CACHE
# ============================================================

state_lock = threading.RLock()
worker_lock = threading.Lock()
coverage_lock = threading.Lock()

worker_started = False

coverage_cache = set()
coverage_time = 0


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
            f"❌ Lettura state: {e}",
            flush=True
        )
        return []


def extract_cards(data):
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


def get_state():
    with state_lock:
        return extract_cards(raw_state())


def save_state(cards):
    with state_lock:
        temp = f"{STATE_FILE}.{uuid.uuid4()}.tmp"

        try:
            with open(
                temp,
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

            os.replace(temp, STATE_FILE)

            return True

        except Exception as e:
            print(
                f"❌ Scrittura state: {e}",
                flush=True
            )

            try:
                os.remove(temp)
            except Exception:
                pass

            return False


def find_card(asset_id):
    aid = norm(asset_id)

    cards = get_state()

    for index, card in enumerate(cards):

        cid = (
            card.get("asset_id")
            or card.get("assetId")
        )

        if norm(cid) == aid:
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
                f"⚠️ Asset non presente nello state: {asset_id}",
                flush=True
            )
            return False

        card = cards[index]

        if status:
            card["status"] = status

        if offer_id:
            card["sale_offer_id"] = offer_id

        if error is not None:
            card["last_error"] = error

        elif status in ("SELLING", "SOLD"):
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = now()

        if status == "SOLD":
            card["sold_at"] = now()

        return save_state(cards)


# ============================================================
# CARTE DA VENDERE
# ============================================================

def sellable_cards():
    """
    IMPORTANTISSIMO:

    Il bot accetta sia:
      - ready
      - da_vendere

    Il tuo autobuy salva le carte come:
      status = "da_vendere"

    Quindi non devono essere ignorate.
    """

    result = []

    with state_lock:

        for card in get_state():

            if not isinstance(card, dict):
                continue

            asset_id = (
                card.get("asset_id")
                or card.get("assetId")
            )

            if not asset_id:
                continue

            status = norm(card.get("status"))

            if status in (
                "ready",
                "da_vendere"
            ):
                result.append(dict(card))

    return result


# ============================================================
# GRAPHQL
# ============================================================

def headers():
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
                time.sleep(
                    min(attempt + 2, 10)
                )
                continue

            if response.status_code != 200:
                print(
                    f"❌ {response.text[:1500]}",
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
        (data or {})
        .get("data", {})
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

                    activeClub {
                        slug
                        name

                        activeCompetitions {
                            slug
                        }
                    }
                }
            }
        }
    """, {
        "ids": [asset_id]
    })

    if not data or data.get("errors"):
        return None

    cards = (
        (data.get("data") or {})
        .get("anyCards")
        or []
    )

    return cards[0] if cards else None


# ============================================================
# COVERAGE
# ============================================================

def coverage(force=False):

    global coverage_cache
    global coverage_time

    if (
        not force
        and coverage_cache
        and time.time() - coverage_time < 3600
    ):
        return coverage_cache

    try:
        response = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent": "Sorare-AutoSell"
            }
        )

        print(
            f"🌐 Coverage HTTP {response.status_code}",
            flush=True
        )

        if response.status_code != 200:
            return coverage_cache

        found = {
            norm(x)
            for x in re.findall(
                r'/football/leagues/([^"\'?#<>\s]+)',
                response.text,
                re.I
            )
            if norm(x)
        }

        if found:

            with coverage_lock:
                coverage_cache = found
                coverage_time = time.time()

            print(
                f"🌐 Sorare Coverage aggiornata: "
                f"{len(found)} competizioni",
                flush=True
            )

        return coverage_cache

    except Exception as e:
        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )
        return coverage_cache


# ============================================================
# USD -> EUR
# ============================================================

def usd_to_eur(cents):

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

        return round(cents * rate)

    except Exception:
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
            return usd_to_eur(usd_cents)

    except Exception:
        pass

    return None


# ============================================================
# FLOOR
# ============================================================

def floor(card):

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
        "slug": slug,
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

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for listed in cards:

            try:
                same_season = (
                    int(listed.get("seasonYear"))
                    == season
                )
            except Exception:
                continue

            same_player = (
                norm(
                    (listed.get("anyPlayer") or {})
                    .get("slug")
                )
                == slug
            )

            same_rarity = (
                norm(
                    listed.get("rarityTyped")
                )
                == rarity
            )

            if (
                same_player
                and same_rarity
                and same_season
            ):

                price = offer_price(
                    (offer.get("receiverSide") or {})
                    .get("amounts")
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

    if (
        norm(card.get("rarityTyped")).upper()
        != "LIMITED"
    ):
        return False, "RARITY"

    price = floor(card)

    if price is None:
        return False, "FLOOR_UNKNOWN"

    if price < MIN_PRICE:
        return False, "FLOOR_LOW"

    if price > MAX_PRICE:
        return False, "FLOOR_HIGH"

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active_competitions = {
        norm(x.get("slug"))
        for x in (
            club.get("activeCompetitions")
            or []
        )
        if isinstance(x, dict)
        and x.get("slug")
    }

    covered = (
        active_competitions
        & coverage()
    )

    if not covered:
        return False, "COVERAGE"

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
const { signAuthorizationRequest } = require("@sorare/crypto");

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

    asset_id = str(
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()

    if not asset_id:
        return None

    # ----------------------------------------
    # DRY RUN
    # ----------------------------------------

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → "
            f"{label(card)} a {eur(price)}",
            flush=True
        )
        return "DRY-RUN"

    # ----------------------------------------
    # PREPARE
    # ----------------------------------------

    data = gql("""
        mutation Prepare($input: prepareOfferInput!) {
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
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        (data or {})
        .get("data", {})
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

    # ----------------------------------------
    # SIGN
    # ----------------------------------------

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

    # ----------------------------------------
    # CREATE
    # ----------------------------------------

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
            "assetId": asset_id,
            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        (data or {})
        .get("data", {})
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
            f"✅ INSERZIONE CREATA: {offer_id}",
            flush=True
        )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process(card_row):

    asset_id = str(
        card_row.get("asset_id")
        or card_row.get("assetId")
        or ""
    ).strip()

    if not asset_id:
        return

    print(
        f"\n💰 AUTOSELL CHECK → {asset_id}",
        flush=True
    )

    # ----------------------------------------
    # RECUPERA CARTA
    # ----------------------------------------

    card = card_details(asset_id)

    if not card:
        print(
            "❌ Carta non recuperabile → BLOCCATA",
            flush=True
        )

        update_card(
            asset_id,
            "ERROR",
            error="CARD_DETAILS"
        )

        return

    # ----------------------------------------
    # VALIDAZIONE
    # ----------------------------------------

    ok, result = validate(card)

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
                "FLOOR SOPRA €0.70",

            "COVERAGE":
                "COMPETIZIONE NON COPERTA"
        }

        print(
            f"🚫 ESCLUSA: {label(card)} → "
            f"{messages.get(result, result)}",
            flush=True
        )

        # Questi due casi possono cambiare
        # e quindi vengono ritentati.
        if result in (
            "FLOOR_UNKNOWN",
            "COVERAGE"
        ):
            update_card(
                asset_id,
                "da_vendere",
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
        f"✅ Carta valida: {label(card)}",
        flush=True
    )

    print(
        f"   └─ Floor: {eur(price)}",
        flush=True
    )

    # ----------------------------------------
    # LOCK
    # ----------------------------------------

    if not update_card(
        asset_id,
        "SELLING"
    ):
        print(
            "❌ Impossibile impostare SELLING → STOP",
            flush=True
        )
        return

    # ----------------------------------------
    # CREA INSERZIONE
    # ----------------------------------------

    offer_id = create_sale(
        card,
        price
    )

    # ----------------------------------------
    # FALLIMENTO
    # ----------------------------------------

    if not offer_id:

        update_card(
            asset_id,
            "da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "↩️ Carta riportata a DA_VENDERE",
            flush=True
        )

        return

    # ----------------------------------------
    # SUCCESSO
    # ----------------------------------------

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

        selling = [
            card
            for card in get_state()
            if norm(card.get("status"))
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
        f"{len(selling)} carte già SELLING",
        flush=True
    )

    for card in selling:

        print(
            f"   └─ "
            f"{card.get('asset_id') or card.get('assetId')} "
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

    coverage(force=True)

    if not check_account():
        return

    recovery()

    while True:

        try:

            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE/READY: "
                f"{len(cards)}",
                flush=True
            )

            for card in cards:

                try:
                    process(card)

                except Exception as e:

                    asset_id = (
                        card.get("asset_id")
                        or card.get("assetId")
                    )

                    print(
                        f"❌ AutoSell "
                        f"{asset_id}: {e}",
                        flush=True
                    )

                    if asset_id:

                        update_card(
                            asset_id,
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

    with state_lock:
        cards = get_state()

    with coverage_lock:
        cov = sorted(
            coverage_cache
        )

    sellable = [
        c
        for c in cards
        if norm(c.get("status"))
        in ("ready", "da_vendere")
    ]

    selling = [
        c
        for c in cards
        if norm(c.get("status"))
        == "selling"
    ]

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
        "storage": STATE_FILE,
        "cards": len(cards),
        "sellable": len(sellable),
        "selling": len(selling),
        "coverage": len(cov),
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

    data = get_state()

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
