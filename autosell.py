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

BOT_VERSION = "AUTOSell-2.2-COVERAGE-SAFE"

SELL_PRICE_MODE = os.getenv("SELL_PRICE_MODE", "FLOOR").upper()

JSON_PATH = os.getenv(
    "AUTOSSELL_JSON_PATH", "autosell_cards.json"
).strip()

KID = os.getenv("KULENOVIC_ID", "").strip()
KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

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

def norm(value):
    return str(value or "").strip().lower()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def card_name(card):
    return card.get("name") or card.get("slug") or "Carta"


def card_label(card):
    name = card_name(card)
    slug = card.get("slug")
    return f"{name} [{slug}]" if slug and slug != name else name


def format_eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


# ============================================================
# JSON
# ============================================================

def ensure_json_file():
    os.makedirs(os.path.dirname(os.path.abspath(JSON_PATH)), exist_ok=True)

    if not os.path.exists(JSON_PATH):
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)


def load_cards():
    ensure_json_file()

    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            print("⚠️ autosell_cards.json non contiene una lista", flush=True)
            return []

        return data

    except json.JSONDecodeError as e:
        print(f"❌ JSON non valido: {e}", flush=True)
    except Exception as e:
        print(f"❌ Lettura JSON: {e}", flush=True)

    return []


def save_cards(cards):
    os.makedirs(os.path.dirname(os.path.abspath(JSON_PATH)), exist_ok=True)

    temp_path = f"{JSON_PATH}.tmp.{uuid.uuid4()}"

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cards, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, JSON_PATH)
        return True

    except Exception as e:
        print(f"❌ Scrittura JSON: {e}", flush=True)

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        return False


def get_ready_cards():
    with json_lock:
        return [
            card for card in load_cards()
            if (
                isinstance(card, dict)
                and norm(card.get("status")) == "ready"
                and str(card.get("asset_id") or "").strip()
            )
        ]


def update_json_card(
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
            if not isinstance(card, dict):
                continue

            current = str(card.get("asset_id") or "").strip()

            if current.lower() != asset_id.lower():
                continue

            if status is not None:
                card["status"] = status

            if sale_offer_id is not None:
                card["sale_offer_id"] = sale_offer_id

            if last_error is not None:
                card["last_error"] = last_error
            elif status in ("SELLING", "SOLD"):
                card["last_error"] = None

            if status == "SELLING":
                card["selling_at"] = now_iso()

            if status == "SOLD":
                card["sold_at"] = now_iso()

            return save_cards(cards)

        print(f"⚠️ JSON: asset_id non trovato: {asset_id}", flush=True)
        return False


def add_card_to_json(asset_id, source="AUTOBUY"):
    asset_id = str(asset_id or "").strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for card in cards:
            if not isinstance(card, dict):
                continue

            existing = str(card.get("asset_id") or "").strip()

            if existing.lower() == asset_id.lower():
                return card.get("status") != "SOLD"

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
# SORARE
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    token = TOKEN if TOKEN.lower().startswith("bearer ") else f"Bearer {TOKEN}"

    result = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}"
    }

    if AUD:
        result["JWT-AUD"] = AUD

    return result


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {}
    }

    for attempt in range(3):
        try:
            response = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {response.status_code}", flush=True)

            if response.status_code == 429:
                retry = response.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    retry = int(retry)
                except Exception:
                    retry = attempt + 2

                time.sleep(min(retry, 15))
                continue

            if response.status_code != 200:
                print(
                    f"❌ Sorare HTTP {response.status_code}: "
                    f"{response.text[:1000]}",
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            try:
                data = response.json()
            except Exception as e:
                print(f"❌ JSON Sorare non valido: {e}", flush=True)
                time.sleep(attempt + 1)
                continue

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:3000],
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

def extract_coverage(text):
    """
    Estrae gli slug delle competizioni football dalla pagina
    Coverage usando più formati possibili.
    """

    if not text:
        return set()

    patterns = [
        r'/football/leagues/([a-zA-Z0-9_-]+)',
        r'football/leagues/([a-zA-Z0-9_-]+)',
        r'football_leagues/([a-zA-Z0-9_-]+)',
        r'footballLeague["\']?\s*:\s*["\']([^"\']+)',
        r'footballLeagueSlug["\']?\s*:\s*["\']([^"\']+)',
        r'"slug"\s*:\s*"([a-zA-Z0-9_-]+)"'
    ]

    result = set()

    for pattern in patterns:
        for value in re.findall(pattern, text, re.I):
            value = norm(value)

            if not value:
                continue

            if (
                "football" in value
                or pattern.endswith("([a-zA-Z0-9_-]+)')")
            ):
                result.add(value)

    # Rimuove valori chiaramente non appartenenti a competizioni
    invalid = {
        "football",
        "leagues",
        "league",
        "coverage",
        "undefined",
        "null",
        "true",
        "false"
    }

    result -= invalid

    return result


def load_coverage(force=False):
    global coverage_cache, coverage_time, coverage_available

    now = time.time()

    with coverage_lock:
        cached = set(coverage_cache)
        cached_time = coverage_time

    if (
        not force
        and cached
        and now - cached_time < COVERAGE_CACHE
    ):
        return cached

    try:
        response = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/json"
                )
            }
        )

        print(
            f"🌐 Coverage HTTP {response.status_code}",
            flush=True
        )

        if response.status_code != 200:
            print(
                "⚠️ Coverage temporaneamente non disponibile",
                flush=True
            )

            with coverage_lock:
                coverage_available = False

            return cached

        text = response.text or ""

        result = extract_coverage(text)

        if not result:
            print(
                "⚠️ Coverage ricevuta ma nessuna competizione riconosciuta",
                flush=True
            )

            with coverage_lock:
                coverage_available = False

            return cached

        with coverage_lock:
            coverage_cache = result
            coverage_time = time.time()
            coverage_available = True

        print(
            f"🌐 Coverage Football: {len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:
        print(f"⚠️ Coverage: {e}", flush=True)

        with coverage_lock:
            coverage_available = False

        return cached


# ============================================================
# ACCOUNT / CARD
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
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: {user.get('nickname') or user.get('slug')}",
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
    """, {"assetIds": ids})

    if not data or data.get("errors"):
        return []

    return (
        ((data.get("data") or {}).get("anyCards"))
        or []
    )


# ============================================================
# USD / EUR
# ============================================================

def usd_eur():
    global usd_rate, usd_time

    now = time.time()

    if usd_rate and now - usd_time < USD_CACHE:
        return usd_rate

    try:
        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )

        if response.status_code != 200:
            return None

        rate = float(
            (response.json().get("rates") or {}).get("EUR")
        )

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = now

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

    except (TypeError, ValueError):
        pass

    try:
        usd = float(amounts.get("usdCents"))
    except (TypeError, ValueError):
        usd = 0

    if usd > 0:
        rate = usd_eur()

        if rate:
            return int(round(usd * rate))

    return None


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):
    player = card.get("anyPlayer") or {}
    player_slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except (TypeError, ValueError):
        return None

    if not player_slug or not rarity:
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
    """, {
        "playerSlug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):
        return None

    offers = (
        ((((data.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes"))
        or []
    )

    prices = []

    for offer in offers:
        for c in (
            (offer.get("senderSide") or {}).get("anyCards") or []
        ):
            try:
                c_season = int(c.get("seasonYear"))
            except (TypeError, ValueError):
                continue

            c_player = norm(
                (c.get("anyPlayer") or {}).get("slug")
            )

            c_rarity = norm(c.get("rarityTyped"))

            if (
                c_player == player_slug
                and c_rarity == rarity
                and c_season == season
            ):
                price = price_eur(
                    (offer.get("receiverSide") or {}).get("amounts") or {}
                )

                if price is not None:
                    prices.append(price)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        print(
            f"⚠️ Floor {player_slug}: "
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
        (card.get("anyPlayer") or {}).get("activeClub")
        or {}
    )

    active = [
        norm(c.get("slug"))
        for c in (club.get("activeCompetitions") or [])
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
        return False, {
            "code": "KULENOVIC",
            "message": "Kulenovic è protetto e non deve essere venduto"
        }

    rarity = norm(card.get("rarityTyped")).upper()

    if rarity != "LIMITED":
        return False, {
            "code": "RARITY",
            "message": "rarità diversa da LIMITED",
            "rarity": rarity or "N/D"
        }

    floor = live_floor(card)

    if floor is None:
        return False, {
            "code": "PRICE_UNKNOWN",
            "message": (
                "floor live non disponibile oppure meno di "
                f"{MIN_LIVE_LISTINGS} listing"
            ),
            "min_live_listings": MIN_LIVE_LISTINGS
        }

    if floor < MIN_PRICE:
        return False, {
            "code": "PRICE_LOW",
            "message": "floor sotto il minimo",
            "floor": floor,
            "min_price": MIN_PRICE
        }

    if floor > MAX_PRICE:
        return False, {
            "code": "PRICE_HIGH",
            "message": "floor sopra il massimo",
            "floor": floor,
            "max_price": MAX_PRICE
        }

    covered, active, covered_competitions, coverage_error = coverage_info(card)

    if coverage_error == "COVERAGE_UNAVAILABLE":
        return False, {
            "code": "COVERAGE_UNAVAILABLE",
            "message": "coverage Sorare non disponibile",
            "active_competitions": active,
            "covered_competitions": []
        }

    if not covered:
        return False, {
            "code": "COVERAGE",
            "message": "nessuna competizione coperta",
            "active_competitions": active,
            "covered_competitions": covered_competitions
        }

    return True, {
        "floor": floor,
        "rarity": rarity,
        "active_competitions": active,
        "covered_competitions": covered_competitions
    }


# ============================================================
# LOG ESCLUSIONI
# ============================================================

def print_rejection(card, info):
    print(
        f"🚫 AutoSell - ESCLUSA: {card_label(card)}",
        flush=True
    )

    if not info:
        print("   └─ Motivo: verifica fallita", flush=True)
        return

    code = info.get("code")

    messages = {
        "KULENOVIC": [
            "   └─ Motivo: KULENOVIC PROTETTO"
        ],
        "RARITY": [
            "   ├─ Motivo: RARITÀ NON VALIDA",
            f"   └─ Rarità: {info.get('rarity')}"
        ],
        "PRICE_UNKNOWN": [
            "   ├─ Motivo: FLOOR LIVE NON DISPONIBILE",
            f"   └─ Listing richiesti: {info.get('min_live_listings')}"
        ],
        "PRICE_LOW": [
            "   ├─ Motivo: FLOOR TROPPO BASSO",
            f"   ├─ Floor: {format_eur(info.get('floor'))}",
            f"   └─ Minimo: {format_eur(info.get('min_price'))}"
        ],
        "PRICE_HIGH": [
            "   ├─ Motivo: FLOOR TROPPO ALTO",
            f"   ├─ Floor: {format_eur(info.get('floor'))}",
            f"   └─ Massimo: {format_eur(info.get('max_price'))}"
        ],
        "COVERAGE_UNAVAILABLE": [
            "   ├─ Motivo: COVERAGE NON DISPONIBILE",
            "   └─ Nessuna vendita eseguita; il bot riproverà."
        ]
    }

    if code in messages:
        for line in messages[code]:
            print(line, flush=True)
        return

    if code == "COVERAGE":
        active = info.get("active_competitions") or []
        covered = info.get("covered_competitions") or []

        print(
            "   ├─ Motivo: COMPETIZIONE NON COPERTA",
            flush=True
        )
        print(
            "   ├─ Attive: " + (
                ", ".join(active) if active else "nessuna"
            ),
            flush=True
        )
        print(
            "   └─ Coperte: " + (
                ", ".join(covered) if covered else "nessuna"
            ),
            flush=True
        )
        return

    print(
        f"   └─ Motivo: {info.get('message', 'sconosciuto')}",
        flush=True
    )


# ============================================================
# NODE SIGNING
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

    if (!r) {
        throw new Error("AuthorizationRequest mancante");
    }

    if (
        r.__typename === "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(input.privateKey, r);

    if (r.__typename === "StarkexTransferAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename === "StarkexLimitOrderAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename === "MangopayWalletTransferAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: r.nonce,
                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: " + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(input.authorizations.map(sign))
);
'''

    process = subprocess.run(
        [node, "-e", script],
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
            process.stderr.strip() or "Firma fallita"
        )

    return json.loads(process.stdout)


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price_cents):
    asset_id = str(card.get("assetId") or "").strip()

    if not asset_id:
        print("❌ AutoSell: assetId mancante", flush=True)
        return None

    if DRY_RUN:
        print("🟡 DRY RUN: vendita simulata", flush=True)
        print(f"   ├─ Carta: {card_label(card)}", flush=True)
        print(f"   ├─ Asset: {asset_id}", flush=True)
        print(f"   └─ Prezzo: {format_eur(price_cents)}", flush=True)
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
        mutation PrepareOffer($input: prepareOfferInput!) {
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

    try:
        approvals = sign_authorizations(authorizations)
    except Exception as e:
        print(f"❌ AutoSell firma: {e}", flush=True)
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

    print(
        f"✅ AUTOSELL CREATO: {offer_id}",
        flush=True
    )

    return offer_id


# ============================================================
# PROCESS CARD
# ============================================================

def process_card(row):
    asset_id = str(row.get("asset_id") or "").strip()
    source = row.get("source") or "UNKNOWN"

    if not asset_id:
        return

    print("\n💰 AUTOSELL CHECK", flush=True)
    print(f"   ├─ Asset: {asset_id}", flush=True)
    print(f"   ├─ Provenienza: {source}", flush=True)
    print("   └─ Età: NON UTILIZZATA", flush=True)

    cards = card_details([asset_id])

    if len(cards) != 1:
        print(
            "❌ AutoSell: impossibile recuperare la carta "
            "→ NON VENDERE",
            flush=True
        )

        update_json_card(
            asset_id,
            status="ERROR",
            last_error="CARD_DETAILS_UNAVAILABLE"
        )
        return

    card = cards[0]

    returned_asset = str(card.get("assetId") or "").strip()

    if returned_asset.lower() != asset_id.lower():
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
            status=(
                "READY"
                if code == "COVERAGE_UNAVAILABLE"
                else "BLOCKED"
            ),
            last_error=code
        )
        return

    floor = info.get("floor")

    print(
        f"✅ AutoSell - Carta valida: {card_label(card)}",
        flush=True
    )

    print(
        f"   ├─ Rarità: {info.get('rarity')}",
        flush=True
    )

    print(
        f"   ├─ Floor: {format_eur(floor)}",
        flush=True
    )

    print(
        "   └─ Competizioni coperte: "
        f"{', '.join(info.get('covered_competitions') or [])}",
        flush=True
    )

    if SELL_PRICE_MODE == "FLOOR":
        sell_price = floor
    else:
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

    if sell_price is None:
        print(
            "❌ AutoSell: prezzo vendita N/D",
            flush=True
        )
        return

    if not MIN_PRICE <= sell_price <= MAX_PRICE:
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
            "❌ AutoSell: impossibile aggiornare il JSON "
            "→ NON VENDERE",
            flush=True
        )
        return

    offer_id = create_sale(card, sell_price)

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
    print(f"   ├─ Prezzo: {format_eur(sell_price)}", flush=True)
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
    print("🛡️ SOURCE: AUTOBUY / SWAP", flush=True)
    print(f"💾 JSON: {JSON_PATH}", flush=True)

    try:
        headers()
        ensure_json_file()
    except Exception as e:
        print(f"❌ Configurazione: {e}", flush=True)
        return

    coverage = load_coverage(force=True)

    if coverage:
        print(
            f"🏆 Competizioni coperte: {len(coverage)}",
            flush=True
        )
    else:
        print("⚠️ Coverage non disponibile.", flush=True)
        print(
            "🛡️ AutoSell resta attivo ma NON venderà carte "
            "finché la coverage non sarà verificata.",
            flush=True
        )

    if not check_account():
        return

    while True:
        try:
            load_coverage()

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
            print(
                f"❌ AutoSell worker: {e}",
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


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
