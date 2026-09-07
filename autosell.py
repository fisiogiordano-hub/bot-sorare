import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
import base64
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

SORARE_URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"
STATE_FILE = "bot_state.json"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv(
    "GITHUB_REPO", "fisiogiordano-hub/bot-sorare"
).strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SWAP_AUTO_ACCEPT = os.getenv(
    "SWAP_AUTO_ACCEPT", "false"
).lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 25
MIN_LIVE_LISTINGS = 5

SWAP_MIN = 1.20
SWAP_MAX = 1.25

USD_CACHE = 300
COVERAGE_CACHE = 3600

BOT_VERSION = "22.7-COVERAGE-FIX"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

processed = set()
coverage_cache = set()

state_lock = threading.Lock()
coverage_lock = threading.Lock()
github_lock = threading.Lock()
worker_lock = threading.Lock()

coverage_time = 0
usd_rate = None
usd_time = 0
worker_started = False


# ============================================================
# UTILS
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def card_name(c):
    return c.get("name") or c.get("slug") or "Carta"


def card_label(c):
    name = card_name(c)
    slug = c.get("slug")
    return f"{name} [{slug}]" if slug and slug != name else name


def format_eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


# ============================================================
# STATE
# ============================================================

def load_local_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {norm(x) for x in data.get("processed_offers", []) if x}
    except Exception:
        return set()


def save_local_state():
    try:
        with state_lock:
            data = {
                "processed_offers": sorted(processed),
                "updated_at": int(time.time())
            }

        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print(f"❌ Stato locale: {e}", flush=True)
        return False


def github_headers():
    if not GITHUB_TOKEN:
        return None

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
    }


def github_url():
    return (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/contents/{STATE_FILE}"
    )


def load_github_state():
    if not GITHUB_TOKEN:
        return set()

    try:
        r = requests.get(
            github_url(),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            return set()

        content = r.json().get("content", "")
        raw = base64.b64decode(
            content.replace("\n", "")
        ).decode()

        data = json.loads(raw)
        ids = data.get("processed_offers", [])

        print(
            f"💾 GitHub: {len(ids)} offerte caricate",
            flush=True
        )

        return {norm(x) for x in ids if x}

    except Exception as e:
        print(f"⚠️ GitHub load: {e}", flush=True)
        return set()


def save_github_state():
    if not GITHUB_TOKEN:
        return False

    with github_lock:
        try:
            with state_lock:
                data = {
                    "processed_offers": sorted(processed),
                    "updated_at": int(time.time())
                }

            raw = json.dumps(
                data, indent=2, ensure_ascii=False
            )

            encoded = base64.b64encode(
                raw.encode()
            ).decode()

            r = requests.get(
                github_url(),
                headers=github_headers(),
                params={"ref": GITHUB_BRANCH},
                timeout=TIMEOUT
            )

            payload = {
                "message": f"Update {STATE_FILE}",
                "content": encoded,
                "branch": GITHUB_BRANCH
            }

            if r.status_code == 200:
                payload["sha"] = r.json().get("sha")

            r = requests.put(
                github_url(),
                headers=github_headers(),
                json=payload,
                timeout=TIMEOUT
            )

            return r.status_code in (200, 201)

        except Exception as e:
            print(f"⚠️ GitHub save: {e}", flush=True)
            return False


def load_state():
    global processed

    processed = (
        load_local_state()
        | load_github_state()
    )

    save_local_state()

    print(
        f"💾 Stato: {len(processed)} offerte processate",
        flush=True
    )


def mark_done(offer_id):
    offer_id = norm(offer_id)

    if not offer_id:
        return

    with state_lock:
        processed.add(offer_id)

    save_local_state()
    save_github_state()


def should_process(offer_id):
    with state_lock:
        return norm(offer_id) not in processed


# ============================================================
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    token = TOKEN
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


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
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                time.sleep(
                    min(
                        int(r.headers.get(
                            "Retry-After", attempt + 2
                        )),
                        15
                    )
                )
                continue

            if r.status_code != 200:
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
            print(f"❌ GraphQL: {e}", flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# COVERAGE
# ============================================================

def extract_football_coverage(html):
    """
    Estrae gli slug delle sole competizioni Football.
    Il vecchio parser dipendeva esclusivamente da un particolare
    formato HTML. Sorare può cambiare il markup.

    Primo metodo:
        /football/leagues/<slug>

    Secondo metodo:
        cerca URL/link Football presenti nel markup.

    Terzo metodo:
        cerca stringhe JSON contenenti il percorso Football.
    """

    result = set()

    # Metodo 1 - URL normali
    patterns = [
        r'https?://(?:www\.)?sorare\.com/football/leagues/([^"\'?#<>\s]+)',
        r'["\'](?:https?:)?//(?:www\.)?sorare\.com/football/leagues/([^"\'?#<>\s]+)',
        r'["\']/football/leagues/([^"\'?#<>\s]+)'
    ]

    for pattern in patterns:
        for slug in re.findall(pattern, html, re.I):
            slug = norm(slug)
            if slug:
                result.add(slug)

    # Metodo 2 - eventuali slug inseriti nei dati JSON
    for match in re.findall(
        r'football[/\\]+leagues[/\\]+([a-z0-9_-]+)',
        html,
        re.I
    ):
        result.add(norm(match))

    return result


def load_coverage(force=False):
    global coverage_cache, coverage_time

    now = time.time()

    with coverage_lock:
        if (
            not force
            and coverage_cache
            and now - coverage_time < COVERAGE_CACHE
        ):
            return set(coverage_cache)

        old = set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
                "Accept": "text/html,application/xhtml+xml"
            }
        )

        print(
            f"🌐 Coverage HTTP {r.status_code}",
            flush=True
        )

        if r.status_code != 200:
            return old

        result = extract_football_coverage(r.text)

        if not result:
            print(
                "⚠️ Coverage HTTP 200 ma parser vuoto",
                flush=True
            )
            return old

        with coverage_lock:
            coverage_cache = result
            coverage_time = time.time()

        print(
            f"🏆 Coverage Football: {len(result)} competizioni",
            flush=True
        )

        return set(result)

    except Exception as e:
        print(f"⚠️ Coverage: {e}", flush=True)
        return old


# ============================================================
# ACCOUNT / OFFERTE
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
        "🔐 Stark key: "
        + ("PRESENTE" if user.get("starkKey") else "ASSENTE"),
        flush=True
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
                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }

                        receiverSide {
                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

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

    return (
        (((data or {}).get("data") or {})
         .get("currentUser") or {})
        .get("pendingTokenOffersReceived", {})
        .get("nodes")
        or []
    )


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
                    age

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
        (data.get("data") or {}).get("anyCards") or []
    )


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

        rate = float(
            (r.json().get("rates") or {}).get("EUR")
        )

        if rate > 0:
            usd_rate = rate
            usd_time = time.time()
            return rate

    except Exception as e:
        print(f"⚠️ USD/EUR: {e}", flush=True)

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

    if usd > 0:
        rate = usd_eur()
        if rate:
            return int(round(usd * rate))

    return None


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
                                anyPlayer { slug }
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
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        ):
            try:
                c_season = int(c.get("seasonYear"))
            except Exception:
                continue

            if (
                norm((c.get("anyPlayer") or {}).get("slug")) == slug
                and norm(c.get("rarityTyped")) == rarity
                and c_season == season
            ):
                p = price_eur(
                    (offer.get("receiverSide") or {})
                    .get("amounts") or {}
                )

                if p is not None:
                    prices.append(p)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
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


def coverage_info(card):
    player = card.get("anyPlayer") or {}
    club = player.get("activeClub") or {}

    active = [
        norm(x.get("slug"))
        for x in club.get("activeCompetitions") or []
        if isinstance(x, dict) and x.get("slug")
    ]

    coverage = load_coverage()

    covered = [x for x in active if x in coverage]

    return bool(covered), active, covered


def validate_card(card):
    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except Exception:
        return False, {
            "code": "AGE_UNKNOWN",
            "message": "età non disponibile"
        }

    if age >= MAX_AGE:
        return False, {
            "code": "AGE",
            "age": age,
            "max_age": MAX_AGE
        }

    rarity = norm(card.get("rarityTyped")).upper()

    if rarity != "LIMITED":
        return False, {
            "code": "RARITY",
            "rarity": rarity
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
            "floor": floor
        }

    if floor > MAX_PRICE:
        return False, {
            "code": "PRICE_HIGH",
            "floor": floor
        }

    covered, active, covered_competitions = coverage_info(card)

    if not covered:
        return False, {
            "code": "COVERAGE",
            "active_competitions": active,
            "covered_competitions": covered_competitions
        }

    return True, {
        "floor": floor,
        "age": age,
        "rarity": rarity,
        "active_competitions": active,
        "covered_competitions": covered_competitions
    }


def print_rejection(card, info, context):
    print(
        f"🚫 {context}: {card_label(card)}",
        flush=True
    )

    if not info:
        return

    code = info.get("code")

    if code == "AGE":
        print(
            f"   └─ Età {info.get('age')} "
            f">= limite {info.get('max_age')}",
            flush=True
        )

    elif code == "AGE_UNKNOWN":
        print("   └─ Età non disponibile", flush=True)

    elif code == "RARITY":
        print(
            f"   └─ Rarità: {info.get('rarity')}",
            flush=True
        )

    elif code == "PRICE_UNKNOWN":
        print(
            f"   └─ Meno di {MIN_LIVE_LISTINGS} listing live",
            flush=True
        )

    elif code == "PRICE_LOW":
        print(
            f"   └─ Floor {format_eur(info.get('floor'))} "
            f"< €{MIN_PRICE / 100:.2f}",
            flush=True
        )

    elif code == "PRICE_HIGH":
        print(
            f"   └─ Floor {format_eur(info.get('floor'))} "
            f"> €{MAX_PRICE / 100:.2f}",
            flush=True
        )

    elif code == "COVERAGE":
        print(
            f"   └─ Competizioni attive: "
            f"{', '.join(info.get('active_competitions') or [])}",
            flush=True
        )


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    bid = norm(offer.get("blockchainId"))

    if not bid:
        return False

    if DRY_RUN:
        print("🟡 DRY RUN: reject", flush=True)
        return True

    data = graphql("""
        mutation Reject($input: rejectOfferInput!) {
            rejectOffer(input: $input) {
                tokenOffer { id status }
                errors { message }
            }
        }
    """, {
        "input": {
            "blockchainId": bid,
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result or result.get("errors"):
        return False

    print("✅ Offerta rifiutata", flush=True)
    return True


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
const { signAuthorizationRequest } =
require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function sign(a) {
    const r = a.request;

    if (!r)
        throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        && r.amount != null
    ) r.amount = BigInt(r.amount);

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
                expirationTimestamp: r.expirationTimestamp,
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
                expirationTimestamp: r.expirationTimestamp,
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

    if p.returncode:
        raise RuntimeError(
            p.stderr.strip() or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# AUTOBUY
# ============================================================

def counter_offer(offer, cards):
    receiver = norm(
        (offer.get("sender") or {}).get("slug")
    )

    ids = [
        str(c["assetId"])
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not ids:
        return False

    amount = len(ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: {len(ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True
    )

    if DRY_RUN:
        print("🟡 DRY RUN: controproposta", flush=True)
        return True

    inp = {
        "receiveAssetIds": ids,
        "sendAssetIds": [],
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR"
        },
        "receiverSlug": receiver,
        "settlementCurrencies": ["EUR"],
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
                errors { message }
            }
        }
    """, {"input": inp})

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result or result.get("errors"):
        return False

    auth = result.get("authorizations") or []

    if not auth:
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return False

    create = dict(inp)
    create["approvals"] = approvals
    create["dealId"] = str(uuid.uuid4())

    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {
                tokenOffer {
                    id
                    blockchainId
                    status
                }
                errors { message }
            }
        }
    """, {"input": create})

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result or result.get("errors"):
        return False

    if not (result.get("tokenOffer") or {}).get("id"):
        return False

    print("✅ CONTROPROPOSTA INVIATA", flush=True)
    return True


def process_autobuy(offer):
    offer_id = norm(offer.get("id"))

    if not offer_id or not should_process(offer_id):
        return

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    if not any(is_kulenovic(c) for c in receiver_cards):
        return

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not ids:
        mark_done(offer_id)
        return

    print(f"\n📨 AUTOBUY {offer_id}", flush=True)

    details = card_details(ids)

    if len(details) != len(ids):
        if reject_offer(offer):
            mark_done(offer_id)
        return

    valid = []

    for card in details:
        ok, info = validate_card(card)

        if ok:
            print(
                f"✅ AUTOBUY: {card_label(card)} "
                f"→ {format_eur(info['floor'])}",
                flush=True
            )
            valid.append(card)
        else:
            print_rejection(card, info, "AUTOBUY")

    if not valid:
        if reject_offer(offer):
            mark_done(offer_id)
        return

    if counter_offer(offer, valid):
        if reject_offer(offer):
            mark_done(offer_id)


# ============================================================
# SWAP
# ============================================================

def get_exchange_rate_id():
    data = graphql("""
        query {
            config {
                exchangeRate { id }
            }
        }
    """)

    return (
        (((data or {}).get("data") or {})
         .get("config") or {})
        .get("exchangeRate", {})
        .get("id")
    )


def prepare_accept(offer_id):
    rate = get_exchange_rate_id()

    if not rate:
        return None, None

    data = graphql("""
        mutation PrepareAcceptOffer(
            $input: prepareAcceptOfferInput!
        ) {
            prepareAcceptOffer(input: $input) {
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
                errors { message }
            }
        }
    """, {
        "input": {
            "offerId": offer_id,
            "settlementInfo": {
                "currency": "WEI",
                "paymentMethod": "WALLET",
                "exchangeRateId": rate
            }
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareAcceptOffer")
    )

    if not result or result.get("errors"):
        return None, None

    return result.get("authorizations") or [], rate


def accept_offer(offer):
    offer_id = norm(offer.get("id"))

    if DRY_RUN:
        print("🟡 DRY RUN: ACCEPT", flush=True)
        return True

    auth, rate = prepare_accept(offer_id)

    if not auth:
        return False

    try:
        approvals = sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma ACCEPT: {e}", flush=True)
        return False

    data = graphql("""
        mutation AcceptOffer($input: acceptOfferInput!) {
            acceptOffer(input: $input) {
                tokenOffer { id status }
                errors { message }
            }
        }
    """, {
        "input": {
            "approvals": approvals,
            "offerId": offer_id,
            "settlementInfo": {
                "currency": "WEI",
                "paymentMethod": "WALLET",
                "exchangeRateId": rate
            },
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("acceptOffer")
    )

    if not result or result.get("errors"):
        return False

    print("✅ SWAP ACCETTATO", flush=True)
    return True


def process_swap(offer):
    offer_id = norm(offer.get("id"))

    if not offer_id or not should_process(offer_id):
        return

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    if not sender_cards or not receiver_cards:
        return

    give_ids = [
        c.get("assetId")
        for c in receiver_cards
        if c.get("assetId")
    ]

    receive_ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:
        mark_done(offer_id)
        return

    print(f"\n🔄 SWAP {offer_id}", flush=True)

    give = card_details(give_ids)
    receive = card_details(receive_ids)

    if len(give) != len(give_ids) or len(receive) != len(receive_ids):
        if reject_offer(offer):
            mark_done(offer_id)
        return

    if any(is_kulenovic(c) for c in give):
        print(
            "🔒 KULENOVIC NON È CEDIBILE",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    total_given = 0

    for card in give:
        floor = live_floor(card)

        if floor is None:
            print_rejection(
                card,
                {"code": "PRICE_UNKNOWN"},
                "SWAP CEDUTA"
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return

        print(
            f"📤 CEDUTA {card_label(card)} "
            f"{format_eur(floor)}",
            flush=True
        )

        total_given += floor

    total_received = 0

    for card in receive:
        ok, info = validate_card(card)

        if not ok:
            print_rejection(
                card,
                info,
                "SWAP RICEVUTA"
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return

        floor = info["floor"]

        print(
            f"📥 RICEVUTA {card_label(card)} "
            f"{format_eur(floor)}",
            flush=True
        )

        total_received += floor

    cash = price_eur(
        (offer.get("senderSide") or {})
        .get("amounts") or {}
    ) or 0

    total_received += cash

    if total_given <= 0:
        mark_done(offer_id)
        return

    minimum = int(round(total_given * SWAP_MIN))
    maximum = int(round(total_given * SWAP_MAX))

    premium = (
        total_received / total_given - 1
    ) * 100

    print(
        f"📤 Ceduto: {format_eur(total_given)}",
        flush=True
    )

    print(
        f"📥 Ricevuto: {format_eur(total_received)}",
        flush=True
    )

    print(
        f"🎯 Range: {format_eur(minimum)} - "
        f"{format_eur(maximum)}",
        flush=True
    )

    if total_received < minimum:
        print(
            f"🔴 SWAP RIFIUTATO: +{premium:.2f}%",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    if total_received > maximum:
        print(
            f"🔴 SWAP RIFIUTATO: +{premium:.2f}%",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    print(
        f"✅ SWAP APPROVABILE: +{premium:.2f}%",
        flush=True
    )

    if not SWAP_AUTO_ACCEPT:
        print(
            "🛑 SWAP_AUTO_ACCEPT=False",
            flush=True
        )
        mark_done(offer_id)
        return

    if accept_offer(offer):
        mark_done(offer_id)


# ============================================================
# DISPATCH
# ============================================================

def process_offer(offer):
    receiver = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    sender = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    if any(is_kulenovic(c) for c in receiver):
        process_autobuy(offer)
    elif sender and receiver:
        process_swap(offer)


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 BOT AVVIATO", flush=True)
    print(f"📦 VERSIONE: {BOT_VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)

    print(
        f"💰 AUTOBUY: €{PAY_PER_CARD / 100:.2f}/carta",
        flush=True
    )

    print(
        f"📊 FLOOR: €{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(f"🎂 ETÀ: < {MAX_AGE}", flush=True)
    print(f"📊 LISTING MINIME: {MIN_LIVE_LISTINGS}", flush=True)
    print("🔄 SWAP: +20% / +25%", flush=True)
    print("🔒 KULENOVIC: MAI CEDIBILE", flush=True)
    print("🎯 KULENOVIC RICHIESTO → AUTOBUY", flush=True)

    load_state()

    coverage = load_coverage(force=True)

    if not coverage:
        print(
            "❌ COVERAGE NON DISPONIBILE → BOT FERMO",
            flush=True
        )
        return

    print(
        f"🏆 COMPETIZIONI FOOTBALL: {len(coverage)}",
        flush=True
    )

    if not check_account():
        return

    while True:
        try:
            offers = get_offers()

            print(
                f"📨 OFFERTE PENDENTI: {len(offers)}",
                flush=True
            )

            for offer in offers:
                try:
                    process_offer(offer)
                except Exception as e:
                    print(
                        f"❌ Errore offerta: {e}",
                        flush=True
                    )

            time.sleep(INTERVAL)

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True
            )
            time.sleep(INTERVAL)


def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            daemon=True,
            name="sorare-worker"
        ).start()

        print("✅ Thread Sorare avviato", flush=True)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    with coverage_lock:
        covered = set(coverage_cache)

    with state_lock:
        count = len(processed)

    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": BOT_VERSION,
        "dry_run": DRY_RUN,
        "swap_auto_accept": SWAP_AUTO_ACCEPT,
        "pay_per_card_cents": PAY_PER_CARD,
        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "max_age": MAX_AGE,
        "min_live_listings": MIN_LIVE_LISTINGS,
        "swap_min_multiplier": SWAP_MIN,
        "swap_max_multiplier": SWAP_MAX,
        "kulenovic": "NEVER_CEDIBLE",
        "kulenovic_requested": "ALWAYS_AUTOBUY",
        "processed_offers": count,
        "covered_competitions_count": len(covered),
        "coverage_source": COVERAGE_URL
    })


@app.get("/health")
def health():
    with coverage_lock:
        loaded = bool(coverage_cache)

    with state_lock:
        count = len(processed)

    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
        "worker_started": worker_started,
        "coverage_loaded": loaded,
        "covered_competitions_count": len(coverage_cache),
        "processed_offers": count,
        "dry_run": DRY_RUN,
        "swap_auto_accept": SWAP_AUTO_ACCEPT
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
