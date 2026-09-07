import os, time, uuid, json, shutil, subprocess, threading, re
import requests
from flask import Flask, jsonify

app = Flask(__name__)

URL = "https://api.sorare.com/graphql"
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

BOT_VERSION = "AUTOSell-2.4-PREPARE-FIX"
SELL_PRICE_MODE = os.getenv("SELL_PRICE_MODE", "FLOOR").upper()
JSON_PATH = os.getenv("AUTOSSELL_JSON_PATH", "autosell_cards.json").strip()

KID = os.getenv("KULENOVIC_ID", "").strip()
KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

OPERATIONS_DECK_NAME = "✅ OPERAZIONI BOT"
DECK_PAGE_SIZE = 100

json_lock = threading.Lock()
worker_lock = threading.Lock()
coverage_lock = threading.Lock()

worker_started = False
usd_rate = None
usd_time = 0
coverage_cache = set()
coverage_time = 0
coverage_available = False


# ============================================================
# UTILITY
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def card_name(c):
    return c.get("name") or c.get("slug") or "Carta"


def card_label(c):
    n = card_name(c)
    s = c.get("slug")
    return f"{n} [{s}]" if s and s != n else n


def format_eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


# ============================================================
# JSON
# ============================================================

def ensure_json_file():
    folder = os.path.dirname(os.path.abspath(JSON_PATH))
    os.makedirs(folder, exist_ok=True)

    if not os.path.exists(JSON_PATH):
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)


def load_cards():
    ensure_json_file()

    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"❌ JSON: {e}", flush=True)
        return []


def save_cards(cards):
    folder = os.path.dirname(os.path.abspath(JSON_PATH))
    os.makedirs(folder, exist_ok=True)
    temp = f"{JSON_PATH}.tmp.{uuid.uuid4()}"

    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(cards, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp, JSON_PATH)
        return True

    except Exception as e:
        print(f"❌ Scrittura JSON: {e}", flush=True)
        try:
            os.remove(temp)
        except Exception:
            pass
        return False


def get_ready_cards():
    with json_lock:
        return [
            c for c in load_cards()
            if isinstance(c, dict)
            and norm(c.get("status")) == "ready"
            and str(c.get("asset_id") or "").strip()
        ]


def update_json_card(asset_id, status=None,
                     sale_offer_id=None, last_error=None):

    asset_id = str(asset_id or "").strip()
    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for c in cards:
            if not isinstance(c, dict):
                continue

            if norm(c.get("asset_id")) != norm(asset_id):
                continue

            if status is not None:
                c["status"] = status

            if sale_offer_id is not None:
                c["sale_offer_id"] = sale_offer_id

            if last_error is not None:
                c["last_error"] = last_error
            elif status in ("SELLING", "SOLD"):
                c["last_error"] = None

            if status == "SELLING":
                c["selling_at"] = now_iso()
            elif status == "SOLD":
                c["sold_at"] = now_iso()

            return save_cards(cards)

    print(f"⚠️ JSON: asset_id non trovato: {asset_id}", flush=True)
    return False


def add_card_to_json(asset_id, source="AUTOBUY"):
    asset_id = str(asset_id or "").strip()
    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for c in cards:
            if isinstance(c, dict) and norm(c.get("asset_id")) == norm(asset_id):
                return c.get("status") != "SOLD"

        cards.append({
            "asset_id": asset_id,
            "source": source,
            "status": "READY",
            "created_at": now_iso(),
            "sold_at": None,
            "sale_offer_id": None,
            "last_error": None
        })

        return save_cards(cards)


# ============================================================
# OPERATIONS DECK
# ============================================================

def get_operations_deck_cards():
    asset_ids = []
    after = None

    while True:
        data = graphql("""
            query OperationsBotDeck(
                $deckName: String!
                $first: Int!
                $after: String
            ) {
                currentUser {
                    footballUserProfile {
                        deck(name: $deckName) {
                            name
                            cards(first: $first, after: $after) {
                                nodes {
                                    assetId
                                }
                                pageInfo {
                                    hasNextPage
                                    endCursor
                                }
                            }
                        }
                    }
                }
            }
        """, {
            "deckName": OPERATIONS_DECK_NAME,
            "first": DECK_PAGE_SIZE,
            "after": after
        })

        if not data or data.get("errors"):
            print("⚠️ Deck OPERAZIONI BOT non disponibile", flush=True)
            return []

        profile = ((data.get("data") or {}).get("currentUser") or {}).get(
            "footballUserProfile"
        )

        deck = (profile or {}).get("deck")

        if not deck:
            print(f"⚠️ Deck non trovato: {OPERATIONS_DECK_NAME}", flush=True)
            return []

        cards = ((deck.get("cards") or {}).get("nodes")) or []

        asset_ids.extend(
            str(c.get("assetId")).strip()
            for c in cards
            if isinstance(c, dict) and c.get("assetId")
        )

        page = (deck.get("cards") or {}).get("pageInfo") or {}

        if not page.get("hasNextPage"):
            break

        after = page.get("endCursor")
        if not after:
            break

    asset_ids = list(dict.fromkeys(asset_ids))

    print(
        f"📋 Deck '{OPERATIONS_DECK_NAME}': {len(asset_ids)} carte",
        flush=True
    )

    return asset_ids


def sync_operations_bot_deck():
    added = 0

    for asset_id in get_operations_deck_cards():
        with json_lock:
            exists = any(
                isinstance(c, dict)
                and norm(c.get("asset_id")) == norm(asset_id)
                for c in load_cards()
            )

        if not exists and add_card_to_json(
            asset_id, "OPERATIONS_DECK"
        ):
            added += 1
            print(
                f"➕ OPERATIONS BOT → JSON READY: {asset_id}",
                flush=True
            )

    if added:
        print(
            f"📥 OPERATIONS BOT: aggiunte {added} nuove carte",
            flush=True
        )

    return added


# ============================================================
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    return {
        "Authorization": (
            TOKEN if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
        **({"JWT-AUD": AUD} if AUD else {})
    }


def graphql(query, variables=None):
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(3):
        try:
            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                try:
                    wait = int(r.headers.get("Retry-After", attempt + 2))
                except Exception:
                    wait = attempt + 2

                time.sleep(min(wait, 15))
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: {r.text[:1000]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            data = r.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(data["errors"], ensure_ascii=False)[:3000],
                    flush=True
                )

            return data

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):
    global coverage_cache, coverage_time, coverage_available

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and coverage_available
            and now - coverage_time < COVERAGE_CACHE
        ):
            return set(coverage_cache)

        old = set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={"User-Agent": f"Sorare-Bot/{BOT_VERSION}"}
        )

        if r.status_code != 200:
            with coverage_lock:
                coverage_available = False
            return old

        result = {
            norm(x)
            for x in re.findall(
                r'/football/leagues/([^"\'?#<>\s]+)',
                r.text,
                re.I
            )
            if norm(x)
        }

        if not result:
            with coverage_lock:
                coverage_available = False
            return old

        with coverage_lock:
            coverage_cache = result
            coverage_time = time.time()
            coverage_available = True

        print(
            f"🌐 Sorare Coverage aggiornata: {len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:
        print(f"⚠️ Coverage: {e}", flush=True)

        with coverage_lock:
            coverage_available = False

        return old


# ============================================================
# ACCOUNT / CARTE
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

    user = ((data or {}).get("data") or {}).get("currentUser")

    if not user:
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: {user.get('nickname') or user.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key account: "
        + ("PRESENTE" if user.get("starkKey") else "NON DISPONIBILE"),
        flush=True
    )

    return True


def card_details(asset_ids):
    ids = list(dict.fromkeys(
        str(x).strip() for x in asset_ids if x
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
    """, {"assetIds": ids})

    if not data or data.get("errors"):
        return []

    return ((data.get("data") or {}).get("anyCards")) or []


# ============================================================
# PREZZI
# ============================================================

def usd_eur():
    global usd_rate, usd_time

    if usd_rate and time.time() - usd_time < USD_CACHE:
        return usd_rate

    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )

        if r.status_code != 200:
            return None

        rate = float((r.json().get("rates") or {}).get("EUR"))

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = time.time()
        return rate

    except Exception as e:
        print(f"❌ USD/EUR: {e}", flush=True)
        return None


def price_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        eur = int(amounts.get("eurCents"))
        if eur > 0:
            return eur
    except Exception:
        pass

    try:
        usd = float(amounts.get("usdCents"))
    except Exception:
        usd = 0

    rate = usd_eur() if usd > 0 else None

    return int(round(usd * rate)) if rate else None


def live_floor(card):
    player = card.get("anyPlayer") or {}
    slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    if not slug or not rarity:
        return None

    data = graphql("""
        query LiveSales($playerSlug: String, $first: Int) {
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
    """, {"playerSlug": slug, "first": 50})

    if not data or data.get("errors"):
        return None

    offers = (
        (((data.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:
        for c in (
            (offer.get("senderSide") or {}).get("anyCards") or []
        ):
            try:
                same_season = int(c.get("seasonYear")) == season
            except Exception:
                continue

            same_card = (
                norm((c.get("anyPlayer") or {}).get("slug")) == slug
                and norm(c.get("rarityTyped")) == rarity
                and same_season
            )

            if same_card:
                p = price_eur(
                    (offer.get("receiverSide") or {}).get("amounts") or {}
                )
                if p is not None:
                    prices.append(p)
                break

    if len(prices) < MIN_LIVE_LISTINGS:
        print(
            f"⚠️ Floor {slug}: "
            f"{len(prices)}/{MIN_LIVE_LISTINGS} listing",
            flush=True
        )
        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
# ============================================================

def is_kulenovic(card):
    wanted = {norm(KSLUG), norm(KASSET)}
    if KID:
        wanted.add(norm(KID))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


def coverage_info(card):
    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub") or {}
    )

    active = [
        norm(c.get("slug"))
        for c in club.get("activeCompetitions") or []
        if isinstance(c, dict) and c.get("slug")
    ]

    coverage = load_coverage()

    with coverage_lock:
        available = coverage_available

    if not available or not coverage:
        return None, active, [], "COVERAGE_UNAVAILABLE"

    covered = [x for x in active if x in coverage]
    return bool(covered), active, covered, None


def validate_for_autosell(card):
    if is_kulenovic(card):
        return False, {"code": "KULENOVIC"}

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        return False, {
            "code": "RARITY",
            "rarity": norm(card.get("rarityTyped")).upper() or "N/D"
        }

    floor = live_floor(card)

    if floor is None:
        return False, {
            "code": "PRICE_UNKNOWN",
            "min_live_listings": MIN_LIVE_LISTINGS
        }

    if floor < MIN_PRICE:
        return False, {
            "code": "PRICE_LOW",
            "floor": floor,
            "min_price": MIN_PRICE
        }

    if floor > MAX_PRICE:
        return False, {
            "code": "PRICE_HIGH",
            "floor": floor,
            "max_price": MAX_PRICE
        }

    covered, active, covered_competitions, error = coverage_info(card)

    if error:
        return False, {
            "code": error,
            "active_competitions": active
        }

    if not covered:
        return False, {
            "code": "COVERAGE",
            "active_competitions": active,
            "covered_competitions": covered_competitions
        }

    return True, {
        "floor": floor,
        "rarity": "LIMITED",
        "active_competitions": active,
        "covered_competitions": covered_competitions
    }


def print_rejection(card, info):
    code = (info or {}).get("code")

    print(
        f"🚫 AutoSell - ESCLUSA: {card_label(card)}",
        flush=True
    )

    messages = {
        "KULENOVIC": "KULENOVIC PROTETTO",
        "RARITY": "RARITÀ NON VALIDA",
        "PRICE_UNKNOWN": "FLOOR LIVE NON DISPONIBILE",
        "PRICE_LOW": "FLOOR TROPPO BASSO",
        "PRICE_HIGH": "FLOOR TROPPO ALTO",
        "COVERAGE_UNAVAILABLE": "COVERAGE NON DISPONIBILE",
        "COVERAGE": "COMPETIZIONE NON COPERTA"
    }

    print(
        f"   └─ Motivo: {messages.get(code, 'VERIFICA FALLITA')}",
        flush=True
    )

    if code in ("PRICE_LOW", "PRICE_HIGH"):
        print(
            f"   ├─ Floor: {format_eur(info.get('floor'))}",
            flush=True
        )

    if code == "COVERAGE":
        print(
            "   ├─ Attive: "
            + ", ".join(info.get("active_competitions") or [])
            or "nessuna",
            flush=True
        )
        print(
            "   └─ Coperte: "
            + ", ".join(info.get("covered_competitions") or [])
            or "nessuna",
            flush=True
        )


# ============================================================
# FIRMA
# ============================================================

def sign_authorizations(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    if not STARK:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    script = r'''
const fs = require("fs");
const { signAuthorizationRequest } = require("@sorare/crypto");

const input = JSON.parse(fs.readFileSync(0, "utf8"));

function sign(a) {
    const r = a.request;
    if (!r) throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename === "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) r.amount = BigInt(r.amount);

    const signature = signAuthorizationRequest(
        input.privateKey,
        r
    );

    if (r.__typename === "StarkexTransferAuthorizationRequest")
        return {
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };

    if (r.__typename === "StarkexLimitOrderAuthorizationRequest")
        return {
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };

    if (r.__typename === "MangopayWalletTransferAuthorizationRequest")
        return {
            fingerprint: a.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: r.nonce,
                signature
            }
        };

    throw new Error(
        "Authorization non supportata: " + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(input.authorizations.map(sign))
);
'''

    p = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip() or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price_cents):
    asset_id = str(card.get("assetId") or "").strip()

    if not asset_id:
        print("❌ AutoSell: assetId mancante", flush=True)
        return None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN: {card_label(card)} → "
            f"{format_eur(price_cents)}",
            flush=True
        )
        return "DRY-RUN"

    print("💳 Settlement currencies: EUR", flush=True)
    print("🛠️ prepareOffer...", flush=True)
    print(f"   ├─ assetId: {asset_id}", flush=True)
    print(
        f"   └─ receiveAmount: {format_eur(price_cents)}",
        flush=True
    )

    # ========================================================
    # CORREZIONE PRINCIPALE
    #
    # NON inviare più:
    #     type: "SINGLE_SALE_OFFER"
    #
    # L'API che sta rispondendo al bot rifiuta quel campo
    # su prepareOfferInput.
    # ========================================================

    prepare_input = {
        "sendAssetIds": [asset_id],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
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
    """, {"input": prepare_input})

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print(
            "❌ AutoSell: prepareOffer senza risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ AutoSell prepareOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    authorizations = result.get("authorizations") or []

    if not authorizations:
        print(
            "❌ AutoSell: nessuna authorization",
            flush=True
        )
        return None

    print(
        f"🔐 Authorization ricevute: {len(authorizations)}",
        flush=True
    )

    try:
        approvals = sign_authorizations(authorizations)
    except Exception as e:
        print(f"❌ AutoSell firma: {e}", flush=True)
        return None

    create_input = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "assetId": asset_id,
        "settlementCurrencies": ["EUR"],
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
    """, {"input": create_input})

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print(
            "❌ AutoSell: createSingleSaleOffer senza risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ AutoSell createSingleSaleOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    offer_id = (result.get("tokenOffer") or {}).get("id")

    if not offer_id:
        print(
            "❌ AutoSell: tokenOffer ID mancante",
            flush=True
        )
        return None

    print(f"✅ AUTOSELL CREATO: {offer_id}", flush=True)
    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):
    asset_id = str(row.get("asset_id") or "").strip()

    if not asset_id:
        return

    print("\n💰 AUTOSELL CHECK", flush=True)
    print(f"   ├─ Asset: {asset_id}", flush=True)
    print(
        f"   ├─ Provenienza: {row.get('source') or 'UNKNOWN'}",
        flush=True
    )
    print("   └─ Età: NON UTILIZZATA", flush=True)

    cards = card_details([asset_id])

    if len(cards) != 1:
        print(
            "❌ AutoSell: impossibile recuperare la carta → NON VENDERE",
            flush=True
        )
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="CARD_DETAILS_UNAVAILABLE"
        )
        return

    card = cards[0]

    if norm(card.get("assetId")) != norm(asset_id):
        print(
            "❌ AutoSell: assetId non corrispondente → BLOCCATO",
            flush=True
        )
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="ASSET_ID_MISMATCH"
        )
        return

    valid, info = validate_for_autosell(card)

    if not valid:
        print_rejection(card, info)

        code = (info or {}).get("code", "INVALID")

        update_json_card(
            asset_id,
            status="READY" if code == "COVERAGE_UNAVAILABLE" else "BLOCKED",
            last_error=code
        )
        return

    floor = info["floor"]

    print(
        f"✅ AutoSell - Carta valida: {card_label(card)}",
        flush=True
    )
    print(f"   ├─ Rarità: LIMITED", flush=True)
    print(f"   ├─ Floor: {format_eur(floor)}", flush=True)
    print(
        "   └─ Competizioni coperte: "
        + ", ".join(info["covered_competitions"]),
        flush=True
    )

    if SELL_PRICE_MODE != "FLOOR":
        print(
            f"❌ SELL_PRICE_MODE non supportato: {SELL_PRICE_MODE}",
            flush=True
        )
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="INVALID_SELL_PRICE_MODE"
        )
        return

    if not MIN_PRICE <= floor <= MAX_PRICE:
        print(
            "🛑 AutoSell: prezzo finale fuori dal range → BLOCCATO",
            flush=True
        )
        update_json_card(
            asset_id,
            status="BLOCKED",
            last_error="FINAL_PRICE_OUT_OF_RANGE"
        )
        return

    if not update_json_card(
        asset_id,
        status="SELLING",
        last_error=None
    ):
        print(
            "❌ AutoSell: impossibile aggiornare JSON → NON VENDERE",
            flush=True
        )
        return

    offer_id = create_sale(card, floor)

    if not offer_id:
        update_json_card(
            asset_id,
            status="READY",
            last_error="CREATE_SALE_FAILED"
        )
        return

    if not update_json_card(
        asset_id,
        status="SOLD",
        sale_offer_id=offer_id,
        last_error=None
    ):
        print(
            "⚠️ ATTENZIONE: vendita creata ma JSON non aggiornato",
            flush=True
        )
        return

    print("🎉 AUTOSELL COMPLETATO", flush=True)
    print(f"   ├─ Carta: {card_label(card)}", flush=True)
    print(f"   ├─ Prezzo: {format_eur(floor)}", flush=True)
    print(f"   └─ Offer ID: {offer_id}", flush=True)


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print(f"📦 VERSIONE: {BOT_VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)
    print(
        f"💰 RANGE: {format_eur(MIN_PRICE)} - {format_eur(MAX_PRICE)}",
        flush=True
    )
    print(
        f"📊 LISTING MINIME: {MIN_LIVE_LISTINGS}",
        flush=True
    )
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("📋 DECK AGGIUNTIVO: " + OPERATIONS_DECK_NAME, flush=True)
    print(f"💾 JSON: {JSON_PATH}", flush=True)

    try:
        headers()
        ensure_json_file()
    except Exception as e:
        print(f"❌ Configurazione: {e}", flush=True)
        return

    if load_coverage(force=True):
        print(
            f"🏆 COMPETIZIONI FOOTBALL: {len(coverage_cache)}",
            flush=True
        )
    else:
        print(
            "⚠️ Coverage non disponibile: nessuna vendita.",
            flush=True
        )

    if not check_account():
        return

    try:
        sync_operations_bot_deck()
    except Exception as e:
        print(f"⚠️ Sync deck: {e}", flush=True)

    while True:
        try:
            load_coverage()

            try:
                sync_operations_bot_deck()
            except Exception as e:
                print(f"⚠️ Sync deck: {e}", flush=True)

            rows = get_ready_cards()

            print(
                f"🗄️ Carte READY nel JSON: {len(rows)}",
                flush=True
            )

            for row in rows:
                try:
                    process_card(row)
                except Exception as e:
                    asset_id = str(row.get("asset_id") or "")
                    print(
                        f"❌ AutoSell errore {asset_id}: {e}",
                        flush=True
                    )
                    if asset_id:
                        update_json_card(
                            asset_id,
                            status="ERROR",
                            last_error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ AutoSell worker: {e}", flush=True)
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
            name="autosell-worker",
            daemon=True
        ).start()

        print("✅ Thread AutoSell avviato.", flush=True)


@app.get("/")
def home():
    with coverage_lock:
        covered = set(coverage_cache)
        coverage_ok = coverage_available

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": BOT_VERSION,
        "dry_run": DRY_RUN,
        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "min_live_listings": MIN_LIVE_LISTINGS,
        "age_parameter": "NOT_USED",
        "rarity": "LIMITED",
        "coverage": "REQUIRED",
        "coverage_available": coverage_ok,
        "kulenovic": "NEVER_SELL",
        "source": "AUTOBUY_OR_SWAP_ONLY",
        "additional_source": "SORARE_DECK",
        "operations_deck": OPERATIONS_DECK_NAME,
        "storage": "PERSISTENT_JSON",
        "json_path": JSON_PATH,
        "ready_cards": len(get_ready_cards()),
        "sell_price_mode": SELL_PRICE_MODE,
        "covered_competitions_count": len(covered),
        "covered_competitions": sorted(covered),
        "worker_started": worker_started
    })


@app.get("/health")
def health():
    with coverage_lock:
        loaded = bool(coverage_cache)
        coverage_ok = coverage_available

    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": BOT_VERSION,
        "worker_started": worker_started,
        "coverage_loaded": loaded,
        "coverage_available": coverage_ok,
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


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
