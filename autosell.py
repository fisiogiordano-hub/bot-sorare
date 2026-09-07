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
MIN_LIVE_LISTINGS = 5

COVERAGE_CACHE = 3600
USD_CACHE = 300

BOT_VERSION = "AUTOSell-2.1-PERSISTENT"


# ============================================================
# PREZZO
# ============================================================

SELL_PRICE_MODE = os.getenv(
    "SELL_PRICE_MODE",
    "FLOOR"
).upper()


# ============================================================
# DATABASE JSON
# ============================================================

JSON_PATH = os.getenv(
    "AUTOSSELL_JSON_PATH",
    "autosell_cards.json"
).strip()

json_lock = threading.Lock()


# ============================================================
# KULENOVIC PROTETTO
# ============================================================

KID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# WORKER / CACHE
# ============================================================

worker_lock = threading.Lock()
worker_started = False

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

usd_rate = None
usd_time = 0


# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def now_iso():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def format_eur(cents):
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
    slug = card.get("slug")

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


# ============================================================
# JSON
# ============================================================

def ensure_json_file():
    directory = os.path.dirname(
        os.path.abspath(JSON_PATH)
    )

    os.makedirs(
        directory,
        exist_ok=True
    )

    if not os.path.exists(JSON_PATH):
        with open(
            JSON_PATH,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                [],
                f,
                ensure_ascii=False,
                indent=2
            )


def load_cards():
    ensure_json_file()

    try:
        with open(
            JSON_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data if isinstance(data, list) else []

    except Exception as e:
        print(
            f"❌ Lettura JSON: {e}",
            flush=True
        )
        return []


def save_cards(cards):
    temp = f"{JSON_PATH}.tmp.{uuid.uuid4()}"

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

        os.replace(temp, JSON_PATH)
        return True

    except Exception as e:
        print(
            f"❌ Scrittura JSON: {e}",
            flush=True
        )

        try:
            if os.path.exists(temp):
                os.remove(temp)
        except Exception:
            pass

        return False


def get_ready_cards():
    with json_lock:
        cards = load_cards()

    return [
        c for c in cards
        if isinstance(c, dict)
        and norm(c.get("status")) == "READY"
        and str(c.get("asset_id") or "").strip()
    ]


def update_card(
    asset_id,
    status=None,
    sale_offer_id=None,
    last_error=None
):
    asset_id = str(asset_id or "").strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for card in cards:

            current = str(
                card.get("asset_id") or ""
            ).strip()

            if current.lower() != asset_id.lower():
                continue

            if status is not None:
                card["status"] = status

            if sale_offer_id is not None:
                card["sale_offer_id"] = sale_offer_id

            if last_error is not None:
                card["last_error"] = last_error
            elif status == "SELLING":
                card["last_error"] = None

            if status == "SELLING":
                card["selling_at"] = now_iso()

            return save_cards(cards)

    print(
        f"⚠️ Asset non trovato nel JSON: {asset_id}",
        flush=True
    )

    return False


def add_card_to_json(
    asset_id,
    source="AUTOBUY"
):
    asset_id = str(asset_id or "").strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for card in cards:
            if (
                str(card.get("asset_id") or "")
                .strip()
                .lower()
                == asset_id.lower()
            ):
                return True

        cards.append({
            "asset_id": asset_id,
            "source": source,
            "status": "READY",
            "created_at": now_iso(),
            "selling_at": None,
            "sold_at": None,
            "sale_offer_id": None,
            "last_error": None
        })

        return save_cards(cards)


# ============================================================
# SORARE HEADERS
# ============================================================

def sorare_headers():
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
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}"
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
        "variables": variables or {}
    }

    for attempt in range(3):

        try:
            r = requests.post(
                SORARE_URL,
                json=payload,
                headers=sorare_headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                time.sleep(
                    min(attempt + 2, 15)
                )
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            data = r.json()

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
# COVERAGE
# ============================================================

def load_coverage(force=False):

    global coverage_cache
    global coverage_time

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and now - coverage_time < COVERAGE_CACHE
        ):
            return set(coverage_cache)

        cached = set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-AutoSell/{BOT_VERSION}"
            }
        )

        if r.status_code != 200:
            return cached

        matches = re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',
            r.text,
            re.I
        )

        result = {
            norm(x)
            for x in matches
            if norm(x)
        }

        if not result:
            return cached

        with coverage_lock:
            coverage_cache = result
            coverage_time = time.time()

        print(
            f"🌐 Coverage aggiornata: "
            f"{len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:
        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )
        return cached


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

def card_details(asset_ids):

    ids = list(dict.fromkeys(
        str(x).strip()
        for x in asset_ids
        if x
    ))

    if not ids:
        return []

    data = graphql("""
        query Cards($assetIds: [String!]!) {
            anyCards(assetIds: $assetIds) {
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
        "assetIds": ids
    })

    if not data or data.get("errors"):
        return []

    return (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )


# ============================================================
# USD -> EUR
# ============================================================

def usd_eur():

    global usd_rate
    global usd_time

    if (
        usd_rate
        and time.time() - usd_time < USD_CACHE
    ):
        return usd_rate

    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR"
            },
            timeout=10
        )

        if r.status_code != 200:
            return None

        rate = float(
            r.json()["rates"]["EUR"]
        )

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = time.time()

        return rate

    except Exception:
        return None


def price_eur(amounts):

    if not isinstance(amounts, dict):
        return None

    try:
        eur = int(
            amounts.get("eurCents")
        )
        if eur > 0:
            return eur
    except (TypeError, ValueError):
        pass

    try:
        usd = float(
            amounts.get("usdCents")
        )
    except (TypeError, ValueError):
        usd = 0

    if usd <= 0:
        return None

    rate = usd_eur()

    if not rate:
        return None

    return int(round(usd * rate))


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):

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
    except (TypeError, ValueError):
        return None

    if not player_slug or not rarity:
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
                                referenceCurrency
                                wei
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

    if not data or data.get("errors"):
        return None

    offers = (
        ((((data.get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes"))
        or []
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards")
            or []
        )

        for c in cards:

            try:
                c_season = int(
                    c.get("seasonYear")
                )
            except (TypeError, ValueError):
                continue

            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) != player_slug
            ):
                continue

            if (
                norm(c.get("rarityTyped"))
                != rarity
            ):
                continue

            if c_season != season:
                continue

            amount = (
                (offer.get("receiverSide") or {})
                .get("amounts")
                or {}
            )

            price = price_eur(amount)

            if price is not None:
                prices.append(price)

            break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    if KID:
        wanted.add(norm(KID))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


# ============================================================
# COVERAGE CARD
# ============================================================

def has_coverage(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    active = {
        norm(c.get("slug"))
        for c in (
            club.get("activeCompetitions")
            or []
        )
        if isinstance(c, dict)
        and c.get("slug")
    }

    coverage = load_coverage()

    covered = active & coverage

    return bool(covered), active, covered


# ============================================================
# VALIDAZIONE
# ============================================================

def validate_card(card):

    if is_kulenovic(card):
        return False, {
            "code": "KULENOVIC",
            "message": "Kulenovic protetto"
        }

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":
        return False, {
            "code": "RARITY",
            "rarity": rarity or "N/D"
        }

    floor = live_floor(card)

    if floor is None:
        return False, {
            "code": "PRICE_UNKNOWN"
        }

    if floor < MIN_PRICE:
        return False, {
            "code": "PRICE_LOW",
            "floor": floor
        }

    if floor > MAX_PRICE:
        return False, {
            "code": "PRICE_HIGH",
            "floor": floor
        }

    covered, active, competitions = has_coverage(card)

    if not covered:
        return False, {
            "code": "COVERAGE",
            "active": sorted(active),
            "covered": sorted(competitions)
        }

    return True, {
        "floor": floor,
        "rarity": rarity,
        "covered": sorted(competitions)
    }


# ============================================================
# LOG ESCLUSIONI
# ============================================================

def rejection(card, info):

    code = info.get("code")

    print(
        f"🚫 AutoSell ESCLUSA: "
        f"{card_label(card)}",
        flush=True
    )

    messages = {
        "KULENOVIC":
            "KULENOVIC PROTETTO",
        "RARITY":
            f"Rarità non valida: "
            f"{info.get('rarity')}",
        "PRICE_UNKNOWN":
            f"Floor non disponibile "
            f"oppure meno di {MIN_LIVE_LISTINGS} listing",
        "PRICE_LOW":
            f"Floor troppo basso: "
            f"{format_eur(info.get('floor'))}",
        "PRICE_HIGH":
            f"Floor troppo alto: "
            f"{format_eur(info.get('floor'))}",
        "COVERAGE":
            "Nessuna competizione coperta"
    }

    print(
        f"   └─ {messages.get(code, code)}",
        flush=True
    )


# ============================================================
# FIRMA SORARE
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
        && r.amount != null
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
        "Authorization non supportata: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(sign)
    )
);
'''

    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        result.stdout
    )


# ============================================================
# CREA VENDITA
# ============================================================

def create_sale(card, price_cents):

    asset_id = str(
        card.get("assetId") or ""
    ).strip()

    if not asset_id:
        return None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → "
            f"{card_label(card)} "
            f"a {format_eur(price_cents)}",
            flush=True
        )
        return "DRY-RUN"

    prepare_input = {
        "type": "SINGLE_SALE_OFFER",
        "sendAssetIds": [asset_id],
        "receiveAssetIds": [],
        "receiveAmount": {
            "amount": str(price_cents),
            "currency": "EUR"
        },
        "clientMutationId": str(uuid.uuid4())
    }

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
        "input": prepare_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print(
            "❌ prepareOffer senza risultato",
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
            "❌ Nessuna authorization",
            flush=True
        )
        return None

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

    create_input = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "assetId": asset_id,
        "receiveAmount": {
            "amount": str(price_cents),
            "currency": "EUR"
        },
        "clientMutationId": str(uuid.uuid4())
    }

    data = graphql("""
        mutation CreateSingleSaleOffer(
            $input: createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(input: $input) {
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
        "input": create_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print(
            "❌ createSingleSaleOffer "
            "senza risultato",
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

    offer = result.get("tokenOffer") or {}
    offer_id = offer.get("id")

    if not offer_id:
        print(
            "❌ ID offerta mancante",
            flush=True
        )
        return None

    print(
        f"✅ AUTOSELL CREATO: {offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESSA CARTA
# ============================================================

def process_card(row):

    asset_id = str(
        row.get("asset_id") or ""
    ).strip()

    if not asset_id:
        return

    print(
        "\n💰 AUTOSELL CHECK",
        flush=True
    )

    print(
        f"   ├─ Asset: {asset_id}",
        flush=True
    )

    print(
        f"   └─ Provenienza: "
        f"{row.get('source', 'UNKNOWN')}",
        flush=True
    )

    cards = card_details([asset_id])

    if len(cards) != 1:
        print(
            "❌ Carta non recuperabile "
            "→ NON VENDERE",
            flush=True
        )

        update_card(
            asset_id,
            status="ERROR",
            last_error="CARD_DETAILS_UNAVAILABLE"
        )
        return

    card = cards[0]

    if (
        str(card.get("assetId") or "")
        .strip()
        .lower()
        != asset_id.lower()
    ):
        print(
            "❌ Asset ID non corrispondente "
            "→ BLOCCATO",
            flush=True
        )

        update_card(
            asset_id,
            status="ERROR",
            last_error="ASSET_ID_MISMATCH"
        )
        return

    valid, info = validate_card(card)

    if not valid:
        rejection(card, info)

        update_card(
            asset_id,
            status="BLOCKED",
            last_error=info.get(
                "code",
                "INVALID"
            )
        )
        return

    floor = info["floor"]

    print(
        f"✅ Carta valida: {card_label(card)}",
        flush=True
    )

    print(
        f"   ├─ Rarità: {info['rarity']}",
        flush=True
    )

    print(
        f"   ├─ Floor: {format_eur(floor)}",
        flush=True
    )

    print(
        f"   └─ Prezzo vendita: "
        f"{format_eur(floor)}",
        flush=True
    )

    if SELL_PRICE_MODE != "FLOOR":
        print(
            f"❌ SELL_PRICE_MODE non supportato: "
            f"{SELL_PRICE_MODE}",
            flush=True
        )

        update_card(
            asset_id,
            status="ERROR",
            last_error="INVALID_SELL_PRICE_MODE"
        )
        return

    sell_price = floor

    if not (
        MIN_PRICE
        <= sell_price
        <= MAX_PRICE
    ):
        update_card(
            asset_id,
            status="BLOCKED",
            last_error="FINAL_PRICE_OUT_OF_RANGE"
        )
        return

    # IMPORTANTE:
    # segniamo SELLING prima della chiamata a Sorare.
    # Se il processo si interrompe dopo la creazione
    # dell'offerta, non rischiamo di ripeterla al ciclo
    # successivo.

    if not update_card(
        asset_id,
        status="SELLING"
    ):
        print(
            "❌ Impossibile aggiornare JSON "
            "→ NON VENDERE",
            flush=True
        )
        return

    offer_id = create_sale(
        card,
        sell_price
    )

    if not offer_id:
        update_card(
            asset_id,
            status="READY",
            last_error="CREATE_SALE_FAILED"
        )
        return

    # NON è SOLD.
    # La carta è semplicemente in vendita.

    update_card(
        asset_id,
        status="SELLING",
        sale_offer_id=offer_id
    )

    print(
        "🎉 AUTOSELL COMPLETATO",
        flush=True
    )

    print(
        f"   ├─ Carta: {card_label(card)}",
        flush=True
    )

    print(
        f"   ├─ Prezzo: {format_eur(sell_price)}",
        flush=True
    )

    print(
        f"   └─ Offer ID: {offer_id}",
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
        f"📦 VERSIONE: {BOT_VERSION}",
        flush=True
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        f"💰 RANGE: "
        f"{format_eur(MIN_PRICE)} - "
        f"{format_eur(MAX_PRICE)}",
        flush=True
    )

    print(
        f"📊 LISTING MINIME: "
        f"{MIN_LIVE_LISTINGS}",
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
        f"💾 JSON: {JSON_PATH}",
        flush=True
    )

    try:
        sorare_headers()
        ensure_json_file()
    except Exception as e:
        print(
            f"❌ Configurazione: {e}",
            flush=True
        )
        return

    coverage = load_coverage(force=True)

    if not coverage:
        print(
            "❌ Coverage non disponibile "
            "→ AutoSell fermato",
            flush=True
        )
        return

    print(
        f"🏆 Competizioni coperte: "
        f"{len(coverage)}",
        flush=True
    )

    if not check_account():
        return

    while True:

        try:
            rows = get_ready_cards()

            print(
                f"🗄️ Carte READY: {len(rows)}",
                flush=True
            )

            for row in rows:
                try:
                    process_card(row)
                except Exception as e:

                    asset_id = str(
                        row.get("asset_id") or ""
                    ).strip()

                    print(
                        f"❌ Errore AutoSell "
                        f"{asset_id}: {e}",
                        flush=True
                    )

                    if asset_id:
                        update_card(
                            asset_id,
                            status="ERROR",
                            last_error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:

            print(
                f"❌ Worker AutoSell: {e}",
                flush=True
            )

            time.sleep(INTERVAL)


# ============================================================
# START
# ============================================================

def start_worker():

    global worker_started

    with worker_lock:

        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            name="autosell-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    with coverage_lock:
        coverage = sorted(coverage_cache)

    ready = get_ready_cards()

    with json_lock:
        cards = load_cards()

    selling = sum(
        1 for c in cards
        if isinstance(c, dict)
        and norm(c.get("status")) == "SELLING"
    )

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": BOT_VERSION,
        "dry_run": DRY_RUN,

        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "min_live_listings": MIN_LIVE_LISTINGS,

        "rarity": "LIMITED",
        "age_parameter": "NOT_USED",
        "coverage_required": True,

        "kulenovic": "NEVER_SELL",

        "source": "AUTOBUY_OR_SWAP_ONLY",

        "storage": "PERSISTENT_JSON",
        "json_path": JSON_PATH,

        "ready_cards": len(ready),
        "selling_cards": selling,

        "sell_price_mode":
            SELL_PRICE_MODE,

        "covered_competitions_count":
            len(coverage),

        "worker_started":
            worker_started
    })


@app.get("/health")
def health():

    with coverage_lock:
        loaded = bool(coverage_cache)

    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": BOT_VERSION,
        "worker_started": worker_started,
        "coverage_loaded": loaded,
        "dry_run": DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():

    with json_lock:
        cards = load_cards()

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
        )
    )
