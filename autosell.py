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

# =========================
# CONFIG
# =========================

URL = "https://api.sorare.com/graphql"
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

BOT_STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.json").strip()

KID = os.getenv("KULENOVIC_ID", "").strip()
KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

VERSION = "AUTOSell-3.1-STATE-FIX"

json_lock = threading.RLock()
worker_lock = threading.Lock()
coverage_lock = threading.Lock()

worker_started = False
coverage_cache = set()
coverage_time = 0


# =========================
# UTILS
# =========================

def norm(v):
    return str(v or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def label(c):
    return c.get("name") or c.get("slug") or c.get("assetId") or "Carta"


def eur(c):
    return "N/D" if c is None else f"€{c / 100:.2f}"


# =========================
# STATE
# =========================

def ensure_state():
    path = os.path.abspath(BOT_STATE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not os.path.exists(path):
        save_state([])


def raw_state():
    ensure_state()

    try:
        with open(BOT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Lettura bot_state.json: {e}", flush=True)
        return []


def extract_cards(data):
    """
    Compatibile con:
      1) [...]
      2) {"cards":[...], ...}
      3) {"cards":{"...": {...}}, ...}
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
    with json_lock:
        return extract_cards(raw_state())


def save_state(cards):
    with json_lock:
        tmp = f"{BOT_STATE_PATH}.{uuid.uuid4()}.tmp"

        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cards, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp, BOT_STATE_PATH)
            return True

        except Exception as e:
            print(f"❌ Scrittura state: {e}", flush=True)

            try:
                os.remove(tmp)
            except Exception:
                pass

            return False


def find_card(asset_id):
    aid = norm(asset_id)

    with json_lock:
        cards = state()

        for i, c in enumerate(cards):
            cid = c.get("asset_id") or c.get("assetId")
            if norm(cid) == aid:
                return cards, i

    return cards, None


def update_card(asset_id, status=None, offer_id=None, error=None):
    with json_lock:
        cards, i = find_card(asset_id)

        if i is None:
            print(f"⚠️ Asset non presente nello state: {asset_id}", flush=True)
            return False

        c = cards[i]

        if status:
            c["status"] = status

        if offer_id:
            c["sale_offer_id"] = offer_id

        if error is not None:
            c["last_error"] = error
        elif status in ("SELLING", "SOLD"):
            c["last_error"] = None

        if status == "SELLING":
            c["selling_at"] = now()

        if status == "SOLD":
            c["sold_at"] = now()

        return save_state(cards)


def ready_cards():
    with json_lock:
        return [
            dict(c)
            for c in state()
            if isinstance(c, dict)
            and norm(c.get("status")) == "ready"
            and (c.get("asset_id") or c.get("assetId"))
        ]


# =========================
# SORARE GRAPHQL
# =========================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    h = {
        "Authorization": (
            TOKEN if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def gql(query, variables=None):
    for attempt in range(3):
        try:
            r = requests.post(
                URL,
                json={
                    "query": query,
                    "variables": variables or {}
                },
                headers=headers(),
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                time.sleep(min(attempt + 2, 10))
                continue

            if r.status_code != 200:
                print(f"❌ {r.text[:1500]}", flush=True)
                time.sleep(attempt + 1)
                continue

            data = r.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(data["errors"], ensure_ascii=False)[:2500],
                    flush=True
                )

            return data

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            time.sleep(attempt + 1)

    return None


# =========================
# ACCOUNT
# =========================

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


# =========================
# CARD DETAILS
# =========================

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
    """, {"ids": [asset_id]})

    if not data or data.get("errors"):
        return None

    cards = ((data.get("data") or {}).get("anyCards") or [])

    return cards[0] if len(cards) == 1 else None


# =========================
# COVERAGE
# =========================

def coverage(force=False):
    global coverage_cache, coverage_time

    if (
        not force
        and coverage_cache
        and time.time() - coverage_time < 3600
    ):
        return coverage_cache

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={"User-Agent": "Sorare-AutoSell"}
        )

        print(f"🌐 Coverage HTTP {r.status_code}", flush=True)

        if r.status_code != 200:
            return coverage_cache

        found = {
            norm(x)
            for x in re.findall(
                r'/football/leagues/([^"\'?#<>\s]+)',
                r.text,
                re.I
            )
            if norm(x)
        }

        if found:
            with coverage_lock:
                coverage_cache = found
                coverage_time = time.time()

            print(
                f"🌐 Sorare Coverage aggiornata: {len(found)} competizioni",
                flush=True
            )

        return coverage_cache

    except Exception as e:
        print(f"⚠️ Coverage: {e}", flush=True)
        return coverage_cache


# =========================
# PRICE
# =========================

def usd_to_eur(cents):
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )

        rate = float(r.json()["rates"]["EUR"])
        return round(cents * rate)

    except Exception:
        return None


def offer_price(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        x = int(amounts.get("eurCents") or 0)
        if x > 0:
            return x
    except Exception:
        pass

    try:
        x = float(amounts.get("usdCents") or 0)
        if x > 0:
            return usd_to_eur(x)
    except Exception:
        pass

    return None


def floor(card):
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
    """, {"slug": slug, "first": 50})

    if not data or data.get("errors"):
        return None

    offers = (
        (((data.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:
        for c in ((offer.get("senderSide") or {}).get("anyCards") or []):

            try:
                same_season = int(c.get("seasonYear")) == season
            except Exception:
                continue

            if (
                norm((c.get("anyPlayer") or {}).get("slug")) == slug
                and norm(c.get("rarityTyped")) == rarity
                and same_season
            ):
                p = offer_price(
                    (offer.get("receiverSide") or {}).get("amounts")
                )

                if p is not None:
                    prices.append(p)

                break

    if len(prices) < MIN_LISTINGS:
        print(
            f"⚠️ Floor {slug}: {len(prices)}/{MIN_LISTINGS} listing",
            flush=True
        )
        return None

    return min(prices)


# =========================
# VALIDATION
# =========================

def is_kulenovic(card):
    wanted = {norm(KSLUG), norm(KASSET)}

    if KID:
        wanted.add(norm(KID))

    return (
        norm(card.get("assetId")) in wanted
        or norm(card.get("slug")) in wanted
    )


def valid(card):
    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        return False, "RARITY"

    f = floor(card)

    if f is None:
        return False, "FLOOR_UNKNOWN"

    if f < MIN_PRICE:
        return False, "FLOOR_LOW"

    if f > MAX_PRICE:
        return False, "FLOOR_HIGH"

    club = ((card.get("anyPlayer") or {}).get("activeClub") or {})

    active = {
        norm(x.get("slug"))
        for x in club.get("activeCompetitions", [])
        if isinstance(x, dict) and x.get("slug")
    }

    covered = active & coverage()

    if not covered:
        return False, "COVERAGE"

    return True, f


# =========================
# SIGN
# =========================

def sign(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    if not STARK:
        raise RuntimeError("SORARE_STARK_PRIVATE_KEY mancante")

    js = r'''
const fs = require("fs");
const { signAuthorizationRequest } = require("@sorare/crypto");

const x = JSON.parse(fs.readFileSync(0, "utf8"));

function one(a) {
    const r = a.request;

    if (!r) throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        && r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(
        x.privateKey,
        r
    );

    if (r.__typename ===
        "StarkexTransferAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename ===
        "StarkexLimitOrderAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename ===
        "MangopayWalletTransferAuthorizationRequest") {
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
    JSON.stringify(x.authorizations.map(one))
);
'''

    p = subprocess.run(
        [node, "-e", js],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "Firma fallita")

    return json.loads(p.stdout)


# =========================
# CREATE LISTING
# =========================

def create_sale(card, price):
    asset_id = str(card.get("assetId") or "").strip()

    if not asset_id:
        return None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → {label(card)} a {eur(price)}",
            flush=True
        )
        return "DRY-RUN"

    # 1. PREPARE
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

    result = ((data or {}).get("data") or {}).get("prepareOffer")

    if not result:
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    auth = result.get("authorizations") or []

    if not auth:
        print("❌ Nessuna authorization", flush=True)
        return None

    # 2. SIGN
    try:
        approvals = sign(auth)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return None

    # 3. CREATE
    data = gql("""
        mutation Create($input: createSingleSaleOfferInput!) {
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
            "clientMutationId": str(uuid.uuid4())
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ createSingleSaleOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    offer_id = (result.get("tokenOffer") or {}).get("id")

    if offer_id:
        print(f"✅ INSERZIONE CREATA: {offer_id}", flush=True)

    return offer_id


# =========================
# PROCESS
# =========================

def process(row):
    asset_id = str(
        row.get("asset_id") or row.get("assetId") or ""
    ).strip()

    if not asset_id:
        return

    print(f"\n💰 AUTOSELL CHECK → {asset_id}", flush=True)

    card = card_details(asset_id)

    if not card:
        print("❌ Carta non recuperabile → BLOCCATA", flush=True)
        update_card(asset_id, "ERROR", error="CARD_DETAILS")
        return

    ok, result = valid(card)

    if not ok:
        messages = {
            "KULENOVIC": "KULENOVIC PROTETTO",
            "RARITY": "RARITÀ NON LIMITED",
            "FLOOR_UNKNOWN": "FLOOR NON DISPONIBILE",
            "FLOOR_LOW": "FLOOR SOTTO €0.32",
            "FLOOR_HIGH": "FLOOR SOPRA €0.70",
            "COVERAGE": "COMPETIZIONE NON COPERTA"
        }

        print(
            f"🚫 ESCLUSA: {label(card)} → {messages.get(result, result)}",
            flush=True
        )

        # Solo coverage/floor sconosciuto possono essere riprovati.
        if result in ("FLOOR_UNKNOWN", "COVERAGE"):
            update_card(asset_id, "READY", error=result)
        else:
            update_card(asset_id, "BLOCKED", error=result)

        return

    price = result

    print(f"✅ Carta valida: {label(card)}", flush=True)
    print(f"   └─ Floor: {eur(price)}", flush=True)

    # LOCK prima della vendita.
    if not update_card(asset_id, "SELLING"):
        print("❌ Impossibile impostare SELLING → STOP", flush=True)
        return

    offer_id = create_sale(card, price)

    if not offer_id:
        update_card(
            asset_id,
            "READY",
            error="CREATE_SALE_FAILED"
        )
        return

    # IMPORTANTE:
    # l'inserzione è stata creata, ma la carta NON è ancora venduta.
    update_card(
        asset_id,
        "SELLING",
        offer_id=offer_id
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → {label(card)} | "
        f"{eur(price)} | {offer_id}",
        flush=True
    )


# =========================
# RECOVERY
# =========================

def recovery():
    with json_lock:
        rows = [
            c for c in state()
            if norm(c.get("status")) == "selling"
        ]

    if rows:
        print(
            f"🛡️ Recovery: {len(rows)} carte già SELLING",
            flush=True
        )

        for c in rows:
            print(
                f"   └─ {c.get('asset_id') or c.get('assetId')} "
                f"| offer={c.get('sale_offer_id')}",
                flush=True
            )
    else:
        print("🔄 Recovery: nessuna carta SELLING.", flush=True)


# =========================
# WORKER
# =========================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print(f"📦 VERSIONE: {VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)
    print("💰 RANGE: €0.32 - €0.70", flush=True)
    print(f"📊 LISTING MINIME: {MIN_LISTINGS}", flush=True)
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("🛡️ SOURCE: AUTOBUY / SWAP", flush=True)
    print(f"💾 STORAGE: {BOT_STATE_PATH}", flush=True)

    try:
        headers()
        ensure_state()
    except Exception as e:
        print(f"❌ Configurazione: {e}", flush=True)
        return

    coverage(force=True)

    if not check_account():
        return

    recovery()

    while True:
        try:
            rows = ready_cards()

            print(
                f"🗄️ Carte READY: {len(rows)}",
                flush=True
            )

            for row in rows:
                try:
                    process(row)
                except Exception as e:
                    asset_id = row.get("asset_id") or row.get("assetId")
                    print(
                        f"❌ AutoSell {asset_id}: {e}",
                        flush=True
                    )

                    if asset_id:
                        update_card(
                            asset_id,
                            "ERROR",
                            error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ Worker: {e}", flush=True)
            time.sleep(INTERVAL)


# =========================
# FLASK
# =========================

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

        print("✅ Thread AutoSell avviato.", flush=True)


@app.get("/")
def home():
    with json_lock:
        cards = state()

    with coverage_lock:
        cov = sorted(coverage_cache)

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
        "storage": BOT_STATE_PATH,
        "cards": len(cards),
        "ready": len([
            c for c in cards
            if norm(c.get("status")) == "ready"
        ]),
        "selling": len([
            c for c in cards
            if norm(c.get("status")) == "selling"
        ]),
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
def cards():
    with json_lock:
        data = state()

    return jsonify({
        "count": len(data),
        "cards": data
    })


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
