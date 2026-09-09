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

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv("BOT_STATE_PATH", "bot_state.json").strip()

KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-3.2-DA-VENDERE-FIX"

state_lock = threading.RLock()
worker_lock = threading.Lock()

worker_started = False

coverage_cache = set()
coverage_timestamp = 0


# ============================================================
# UTILS
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()


# ============================================================
# STATE
# ============================================================

def ensure_state():
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    os.makedirs(directory, exist_ok=True)

    if not os.path.exists(STATE_FILE):
        save_state([])


def load_raw_state():
    ensure_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print(f"❌ Errore lettura state: {e}", flush=True)
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

    # Compatibilità con eventuali altri formati
    cards = data.get("acquired_cards")

    if isinstance(cards, list):
        return cards

    return []


def get_state():
    with state_lock:
        return extract_cards(load_raw_state())


def save_state(cards):
    with state_lock:
        tmp = f"{STATE_FILE}.{uuid.uuid4()}.tmp"

        try:
            with open(tmp, "w", encoding="utf-8") as f:
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
            print(f"❌ Errore scrittura state: {e}", flush=True)

            try:
                os.remove(tmp)
            except Exception:
                pass

            return False


def find_card(asset):
    target = norm(asset)

    cards = get_state()

    for index, card in enumerate(cards):
        if norm(asset_id(card)) == target:
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

        elif status in ("SELLING", "SOLD"):
            card["last_error"] = None

        if status == "SELLING":
            card["selling_at"] = now()

        if status == "SOLD":
            card["sold_at"] = now()

        return save_state(cards)


def sellable_cards():
    """
    IMPORTANTE:
    accetta sia READY sia DA_VENDERE.

    Il tuo autobuy salva infatti:
        "status": "da_vendere"

    mentre il vecchio autosell cercava solamente:
        "ready"

    Questo causava:
        Carte READY: 0
    """

    allowed = {
        "ready",
        "da_vendere"
    }

    with state_lock:
        result = []

        for card in get_state():
            if not isinstance(card, dict):
                continue

            if norm(card.get("status")) not in allowed:
                continue

            if not asset_id(card):
                continue

            result.append(dict(card))

        return result


# ============================================================
# SORARE
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
                time.sleep(2 + attempt)
                continue

            if response.status_code != 200:
                print(
                    f"❌ Sorare: {response.text[:1000]}",
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
                f"❌ GraphQL errore: {e}",
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
# CARD
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
        "ids": [asset]
    })

    if not data or data.get("errors"):
        return None

    cards = (
        ((data.get("data") or {}).get("anyCards"))
        or []
    )

    return cards[0] if cards else None


# ============================================================
# COVERAGE
# ============================================================

def get_coverage(force=False):
    global coverage_cache
    global coverage_timestamp

    if (
        not force
        and coverage_cache
        and time.time() - coverage_timestamp < 3600
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
            coverage_cache = found
            coverage_timestamp = time.time()

            print(
                f"🌐 Coverage: {len(found)} competizioni",
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

        return round(usd_cents * rate)

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


def get_floor(card):
    player = card.get("anyPlayer") or {}

    slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
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
        (
            ((data.get("data") or {}).get("tokens"))
            or {}
        )
        .get("liveSingleSaleOffers", {})
        .get("nodes", [])
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for listed_card in cards:

            try:
                same_season = (
                    int(listed_card.get("seasonYear"))
                    == season
                )
            except Exception:
                continue

            same_player = (
                norm(
                    (listed_card.get("anyPlayer") or {})
                    .get("slug")
                )
                == slug
            )

            same_rarity = (
                norm(listed_card.get("rarityTyped"))
                == rarity
            )

            if not (
                same_player
                and same_rarity
                and same_season
            ):
                continue

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
# VALIDATION
# ============================================================

def is_kulenovic(card):
    wanted = {
        norm(KULENOVIC_SLUG),
        norm(KULENOVIC_ASSET)
    }

    if KULENOVIC_ID:
        wanted.add(norm(KULENOVIC_ID))

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

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor < MIN_PRICE:
        return False, "FLOOR_LOW"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    competitions = {
        norm(item.get("slug"))
        for item in (
            club.get("activeCompetitions") or []
        )
        if isinstance(item, dict)
        and item.get("slug")
    }

    covered = competitions & get_coverage()

    if not covered:
        return False, "COVERAGE"

    return True, floor


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

    return json.loads(process.stdout)


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
            f"{card_name(card)} a {euro(price)}",
            flush=True
        )
        return "DRY-RUN"

    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

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
            "sendAssetIds": [asset],
            "receiveAssetIds": [],
            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },
            "clientMutationId": str(uuid.uuid4())
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
        approvals = sign(authorizations)

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
            "clientMutationId": str(uuid.uuid4())
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
            f"✅ INSERZIONE CREATA: {offer_id}",
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
    # CARD DETAILS
    # --------------------------------------------------------

    card = card_details(asset)

    if not card:
        print(
            "❌ Carta non recuperabile",
            flush=True
        )

        update_card(
            asset,
            "ERROR",
            error="CARD_DETAILS"
        )

        return

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    valid_card, result = validate(card)

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
                "FLOOR SOPRA €0.70",

            "COVERAGE":
                "COMPETIZIONE NON COPERTA"
        }

        print(
            f"🚫 ESCLUSA: "
            f"{card_name(card)} → "
            f"{messages.get(result, result)}",
            flush=True
        )

        # Questi due casi devono poter essere
        # ritentati al ciclo successivo.
        if result in (
            "FLOOR_UNKNOWN",
            "COVERAGE"
        ):
            update_card(
                asset,
                "da_vendere",
                error=result
            )

        else:
            update_card(
                asset,
                "BLOCKED",
                error=result
            )

        return

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    price = result

    print(
        f"✅ Carta valida: {card_name(card)}",
        flush=True
    )

    print(
        f"   └─ Floor: {euro(price)}",
        flush=True
    )

    # --------------------------------------------------------
    # LOCK
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
    # CREATE LISTING
    # --------------------------------------------------------

    offer_id = create_sale(
        card,
        price
    )

    if not offer_id:

        update_card(
            asset,
            "da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "↩️ Carta riportata a DA_VENDERE",
            flush=True
        )

        return

    # --------------------------------------------------------
    # SUCCESS
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
            card
            for card in get_state()
            if norm(card.get("status"))
            == "selling"
        ]

    if not selling:
        print(
            "🔄 Recovery: nessuna carta SELLING.",
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

    get_coverage(force=True)

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

                    asset = asset_id(row)

                    print(
                        f"❌ AutoSell {asset}: {e}",
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

    with state_lock:
        cards = get_state()

    ready = 0
    da_vendere = 0
    selling = 0

    for card in cards:

        status = norm(
            card.get("status")
        )

        if status == "ready":
            ready += 1

        elif status == "da_vendere":
            da_vendere += 1

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
        "storage": STATE_FILE,
        "cards": len(cards),
        "ready": ready,
        "da_vendere": da_vendere,
        "selling": selling,
        "coverage": len(coverage_cache),
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
        cards = get_state()

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
