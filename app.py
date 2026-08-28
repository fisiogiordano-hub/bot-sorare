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
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28

INTERVAL = 10
TIMEOUT = 30
UNKNOWN_PRICE_RETRY = 60
USD_EUR_CACHE_SECONDS = 300

BOT_VERSION = "20.2"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

processed = set()
unknown_price_offers = {}

_worker_started = False
_worker_lock = threading.Lock()

_schema_text = None
_schema_lock = threading.Lock()

_usd_cache = None
_usd_cache_time = 0
_usd_lock = threading.Lock()

state_lock = threading.Lock()


# =========================
# UTILS
# =========================

def slug(v):
    v = str(v or "").strip().lower()
    v = v.replace("_", "-").replace(" ", "-")
    v = v.replace("’", "").replace("'", "")
    return re.sub(r"-+", "-", v)


def auth_headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    token = TOKEN if TOKEN.lower().startswith("bearer ") else f"Bearer {TOKEN}"

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# =========================
# GRAPHQL
# =========================

def graphql(query, variables=None):
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(1, 4):
        try:
            r = requests.post(
                URL,
                json=payload,
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                try:
                    wait = int(r.headers.get("Retry-After", attempt * 3))
                except (TypeError, ValueError):
                    wait = attempt * 3
                print(f"⏳ Rate limit: {wait}s", flush=True)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                print(f"❌ HTTP {r.status_code}: {r.text[:1000]}", flush=True)
                time.sleep(attempt)
                continue

            try:
                data = r.json()
            except ValueError:
                print("❌ JSON Sorare non valido", flush=True)
                return None

            if data.get("errors"):
                print("❌ GraphQL ERROR:", flush=True)
                for e in data["errors"]:
                    print(json.dumps(e, ensure_ascii=False), flush=True)
                return data

            return data

        except requests.RequestException as e:
            print(f"❌ HTTP Sorare: {e}", flush=True)
            time.sleep(attempt)
        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            return None

    return None


# =========================
# SCHEMA
# =========================

def get_live_schema():
    global _schema_text

    with _schema_lock:
        if _schema_text is not None:
            return _schema_text

        print("📚 Controllo schema Sorare corrente...", flush=True)

        try:
            r = requests.get(
                SCHEMA_URL,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
                },
                timeout=TIMEOUT,
            )

            print(f"📚 Schema Sorare HTTP {r.status_code}", flush=True)

            if r.status_code != 200:
                return None

            _schema_text = r.text
            print(f"✅ Schema live scaricato ({len(r.text)} caratteri)", flush=True)
            return _schema_text

        except Exception as e:
            print(f"❌ Errore schema: {e}", flush=True)
            return None


def get_input_fields(type_name):
    schema = get_live_schema()
    if not schema:
        return set()

    m = re.search(
        r"\binput\s+" + re.escape(type_name) + r"\s*\{",
        schema,
        re.MULTILINE,
    )

    if not m:
        print(f"⚠️ {type_name} non trovato nello schema", flush=True)
        return set()

    start = m.end()
    depth = 1
    pos = start

    while pos < len(schema) and depth:
        if schema[pos] == "{":
            depth += 1
        elif schema[pos] == "}":
            depth -= 1
        pos += 1

    fields = set()

    for line in schema[start:pos - 1].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        m = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:",
            line,
        )

        if m:
            fields.add(m.group(1))

    print(
        f"🔍 Campi {type_name}: {', '.join(sorted(fields))}",
        flush=True,
    )
    return fields


def inspect_live_schema():
    print("🔎 CONTROLLO SCHEMA LIVE", flush=True)

    prepare = get_input_fields("prepareOfferInput")
    create = get_input_fields("createDirectOfferInput")

    if not prepare or not create:
        return False

    required_prepare = {
        "receiveAssetIds",
        "receiverSlug",
        "sendAssetIds",
        "sendAmount",
    }

    required_create = {
        "approvals",
        "receiveAssetIds",
        "receiverSlug",
        "sendAssetIds",
        "sendAmount",
    }

    mp = required_prepare - prepare
    mc = required_create - create

    if mp:
        print(f"❌ prepareOfferInput mancanti: {', '.join(sorted(mp))}", flush=True)
        return False

    if mc:
        print(f"❌ createDirectOfferInput mancanti: {', '.join(sorted(mc))}", flush=True)
        return False

    print("   prepareOfferInput:", flush=True)
    for x in sorted(prepare):
        print(f"      • {x}", flush=True)

    print("   createDirectOfferInput:", flush=True)
    for x in sorted(create):
        print(f"      • {x}", flush=True)

    print("✅ SCHEMA OFFER CONFERMATO", flush=True)
    print("🟢 Schema corrente compatibile.", flush=True)
    return True


# =========================
# ACCOUNT / OFFERS
# =========================

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

    print(f"✅ Sorare: {user.get('nickname') or user.get('slug')}", flush=True)
    print(
        f"🔐 Stark key account: {'PRESENTE' if user.get('starkKey') else 'NON DISPONIBILE'}",
        flush=True,
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
                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }
                        receiverSide {
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

    user = ((data or {}).get("data") or {}).get("currentUser") or {}

    return (
        user.get("pendingTokenOffersReceived", {}).get("nodes")
        or []
    )


# =========================
# CARD DETAILS
# =========================

def card_details(asset_ids):
    ids = list(dict.fromkeys(str(x).strip() for x in asset_ids if x))
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
        if data and data.get("errors"):
            for e in data["errors"]:
                print(json.dumps(e, ensure_ascii=False), flush=True)
        return []

    return ((data.get("data") or {}).get("anyCards")) or []


# =========================
# USD / EUR
# =========================

def get_usd_eur_rate():
    global _usd_cache, _usd_cache_time

    now = time.time()

    with _usd_lock:
        if _usd_cache and now - _usd_cache_time < USD_EUR_CACHE_SECONDS:
            return _usd_cache

        try:
            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={"from": "USD", "to": "EUR"},
                timeout=TIMEOUT,
            )

            print(f"💱 USD/EUR HTTP {r.status_code}", flush=True)

            if r.status_code != 200:
                return None

            rate = float((r.json().get("rates") or {}).get("EUR"))

            if rate <= 0:
                return None

            _usd_cache = rate
            _usd_cache_time = now

            print(f"💱 1 USD = {rate:.6f} EUR", flush=True)
            return rate

        except Exception as e:
            print(f"❌ USD/EUR: {e}", flush=True)
            return None


def usd_to_eur_cents(usd):
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return None

    if usd <= 0:
        return None

    rate = get_usd_eur_rate()
    if rate is None:
        return None

    eur = int(round(usd * rate))

    if eur <= 0:
        return None

    print(f"💵 {usd:.0f} USD cents → {eur} EUR cents", flush=True)
    return eur


# =========================
# PRICE
# =========================

def extract_price(amounts):
    if not isinstance(amounts, dict):
        return None

    eur = amounts.get("eurCents")

    if eur is not None:
        try:
            eur = int(str(eur).strip())
            if eur > 0:
                print(f"💶 Prezzo EUR: €{eur / 100:.2f}", flush=True)
                return eur
        except (TypeError, ValueError):
            pass

    usd = amounts.get("usdCents")

    if usd is not None:
        print(f"💵 Prezzo USD cents: {usd}", flush=True)

        eur = usd_to_eur_cents(usd)

        if eur is not None:
            print(f"✅ USD → EUR: €{eur / 100:.2f}", flush=True)
            return eur

    if amounts.get("wei") is not None:
        print(
            f"🚫 WEI escluso "
            f"(referenceCurrency={amounts.get('referenceCurrency')})",
            flush=True,
        )

    print("🛑 Prezzo FIAT non verificabile", flush=True)
    return None


# =========================
# LIVE FLOOR
# =========================

def get_live_floor(card):
    player = card.get("anyPlayer") or {}

    player_slug = str(player.get("slug") or "").strip().lower()
    rarity = str(card.get("rarityTyped") or "").strip().lower()

    try:
        season = int(card.get("seasonYear"))
    except (TypeError, ValueError):
        season = None

    print("      🔎 FLOOR LIVE SINGLE SALE", flush=True)
    print(f"         playerSlug: {player_slug}", flush=True)
    print(f"         rarità: {rarity}", flush=True)
    print(f"         stagione: {season}", flush=True)

    if not player_slug or not rarity or season is None:
        return None

    cursor = None
    prices = []
    page = 0

    while True:
        page += 1

        data = graphql("""
            query LiveSingleSaleOffers(
                $playerSlug: String
                $first: Int
                $after: String
            ) {
                tokens {
                    liveSingleSaleOffers(
                        playerSlug: $playerSlug
                        first: $first
                        after: $after
                    ) {
                        nodes {
                            id
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
                                    name
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
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
        """, {
            "playerSlug": player_slug,
            "first": 50,
            "after": cursor,
        })

        if not data or data.get("errors"):
            return None

        connection = (
            ((data.get("data") or {}).get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        )

        nodes = connection.get("nodes") or []

        print(f"         📦 pagina {page}: {len(nodes)} offerte", flush=True)

        for offer in nodes:
            sender = offer.get("senderSide") or {}
            compatible = False

            for c in sender.get("anyCards") or []:
                op = (c.get("anyPlayer") or {}).get("slug")
                orarity = str(c.get("rarityTyped") or "").strip().lower()

                try:
                    oseason = int(c.get("seasonYear"))
                except (TypeError, ValueError):
                    oseason = None

                if (
                    (not op or str(op).lower() == player_slug)
                    and orarity == rarity
                    and oseason == season
                ):
                    compatible = True
                    break

            if not compatible:
                continue

            amounts = (
                offer.get("receiverSide") or {}
            ).get("amounts") or {}

            price = extract_price(amounts)

            if price is not None:
                prices.append((price, offer.get("id")))
            else:
                print(
                    f"         ⚠️ Offerta compatibile ma prezzo non convertibile: "
                    f"{offer.get('id')}",
                    flush=True,
                )

        info = connection.get("pageInfo") or {}

        if not info.get("hasNextPage"):
            break

        nxt = info.get("endCursor")

        if not nxt or nxt == cursor:
            break

        cursor = nxt

    if not prices:
        print("      ❌ Nessun prezzo FIAT compatibile", flush=True)
        return None

    prices.sort(key=lambda x: x[0])

    floor, offer_id = prices[0]

    print(f"      💰 FLOOR LIVE: €{floor / 100:.2f}", flush=True)
    print(f"         🆔 {offer_id}", flush=True)
    print(f"         📊 compatibili: {len(prices)}", flush=True)

    return floor


# =========================
# VALIDATION
# =========================

def is_kulenovic(card):
    wanted = {KSLUG.lower(), KASSET.lower()}

    if KID:
        wanted.add(KID.lower())

    return (
        str(card.get("assetId") or "").lower() in wanted
        or str(card.get("slug") or "").lower() in wanted
    )


def get_competitions(card):
    club = (card.get("anyPlayer") or {}).get("activeClub") or {}
    return list(dict.fromkeys(
        slug(x.get("slug"))
        for x in club.get("activeCompetitions") or []
        if isinstance(x, dict) and slug(x.get("slug"))
    ))


def check_competition(card):
    club = (card.get("anyPlayer") or {}).get("activeClub")

    if not isinstance(club, dict):
        print("      ❌ Nessuna squadra", flush=True)
        return False

    competitions = get_competitions(card)

    print(
        f"      🏟️ Squadra: {club.get('name') or club.get('slug') or 'Sconosciuta'}",
        flush=True,
    )

    if not competitions:
        print("      ❌ Nessuna activeCompetition", flush=True)
        return False

    print("      🏆 activeCompetitions:", flush=True)
    for x in competitions:
        print(f"         • {x}", flush=True)

    return True


def valid_card(card):
    name = card.get("name") or card.get("slug") or "Carta"
    rarity = str(card.get("rarityTyped") or "").upper()
    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except (TypeError, ValueError):
        print(f"   📄 {name}\n      ❌ Età non disponibile", flush=True)
        return False, "invalid"

    print(f"   📄 {name}", flush=True)
    print(f"      🎂 Età: {age}", flush=True)

    if age >= MAX_AGE:
        print(f"      ❌ Età troppo alta (richiesto < {MAX_AGE})", flush=True)
        return False, "invalid"

    if rarity != "LIMITED":
        print(f"      ❌ Rarità: {rarity}", flush=True)
        return False, "invalid"

    price = get_live_floor(card)

    if price is None:
        print("      ⚠️ FLOOR NON VERIFICABILE", flush=True)
        print("      🟡 OFFERTA LASCIATA IN SOSPESO", flush=True)
        return False, "unknown_price"

    print(f"      💰 Floor: €{price / 100:.2f}", flush=True)

    if not MIN_PRICE <= price <= MAX_PRICE:
        print("      ❌ Prezzo fuori range", flush=True)
        return False, "invalid"

    if not check_competition(card):
        return False, "invalid"

    competitions = get_competitions(card)

    print(
        f"      ✅ VALIDATA | {age} anni | €{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return True, "valid"


# =========================
# REJECT
# =========================

def reject_offer(offer):
    blockchain_id = str(offer.get("blockchainId") or "").strip()

    if not blockchain_id:
        print("❌ blockchainId mancante", flush=True)
        return False

    if DRY_RUN:
        print("🟡 DRY RUN: rifiuto simulato", flush=True)
        return True

    data = graphql("""
        mutation Reject($input: rejectOfferInput!) {
            rejectOffer(input: $input) {
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
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(uuid.uuid4()),
        }
    })

    result = ((data or {}).get("data") or {}).get("rejectOffer")

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for e in errors:
            print(f"❌ Reject: {e.get('message', 'Errore')}", flush=True)
        return False

    print("✅ Offerta originale rifiutata", flush=True)
    return True


# =========================
# SIGN
# =========================

def sign_authorizations(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    if not STARK:
        raise RuntimeError("SORARE_STARK_PRIVATE_KEY non configurata")

    script = r'''
const fs = require("fs");
const { signAuthorizationRequest } = require("@sorare/crypto");

const input = JSON.parse(fs.readFileSync(0, "utf8"));

function build(a) {
    const r = a.request;

    if (!r) throw new Error("AuthorizationRequest mancante");

    if (
        r.__typename === "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(
        input.privateKey,
        r
    );

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

    throw new Error("Authorization non supportata: " + r.__typename);
}

process.stdout.write(
    JSON.stringify(input.authorizations.map(build))
);
'''

    p = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "Firma fallita")

    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Risposta firma non valida")


# =========================
# OFFER INPUT
# =========================

def build_prepare(asset_ids, receiver, amount):
    fields = get_input_fields("prepareOfferInput")
    if not fields:
        raise RuntimeError("prepareOfferInput non disponibile")

    result = {}

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = asset_ids

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "settlementCurrencies" in fields:
        result["settlementCurrencies"] = ["EUR"]

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(uuid.uuid4())

    return result


def build_create(asset_ids, receiver, amount, approvals):
    fields = get_input_fields("createDirectOfferInput")
    if not fields:
        raise RuntimeError("createDirectOfferInput non disponibile")

    result = {}

    if "approvals" in fields:
        result["approvals"] = approvals

    if "dealId" in fields:
        result["dealId"] = str(uuid.uuid4())

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = asset_ids

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(uuid.uuid4())

    return result


# =========================
# COUNTER OFFER
# =========================

def counter_offer(offer, cards):
    sender = offer.get("sender") or {}
    receiver = str(sender.get("slug") or "").strip()

    asset_ids = [
        str(c.get("assetId")).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver or not asset_ids:
        print("❌ Dati controproposta mancanti", flush=True)
        return False

    amount = len(asset_ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: {len(asset_ids)} carta/e → €{amount / 100:.2f}",
        flush=True,
    )

    if DRY_RUN:
        print("🟡 DRY RUN: controproposta simulata", flush=True)
        return True

    try:
        prepare_input = build_prepare(
            asset_ids,
            receiver,
            amount,
        )
    except Exception as e:
        print(f"❌ prepare input: {e}", flush=True)
        return False

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

    if not data or data.get("errors"):
        return False

    result = ((data.get("data") or {}).get("prepareOffer"))

    if not result or result.get("errors"):
        for e in (result or {}).get("errors") or []:
            print(f"❌ prepareOffer: {e.get('message', 'Errore')}", flush=True)
        return False

    authorizations = result.get("authorizations") or []

    if not authorizations:
        print("❌ Nessuna autorizzazione", flush=True)
        return False

    try:
        approvals = sign_authorizations(authorizations)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return False

    try:
        create_input = build_create(
            asset_ids,
            receiver,
            amount,
            approvals,
        )
    except Exception as e:
        print(f"❌ create input: {e}", flush=True)
        return False

    data = graphql("""
        mutation CreateDirectOffer($input: createDirectOfferInput!) {
            createDirectOffer(input: $input) {
                tokenOffer {
                    id
                    blockchainId
                    status
                }
                errors {
                    message
                }
            }
        }
    """, {"input": create_input})

    if not data or data.get("errors"):
        return False

    result = ((data.get("data") or {}).get("createDirectOffer"))

    if not result or result.get("errors"):
        for e in (result or {}).get("errors") or []:
            print(f"❌ createDirectOffer: {e.get('message', 'Errore')}", flush=True)
        return False

    token_offer = result.get("tokenOffer") or {}

    if not token_offer.get("id"):
        print("❌ Nessuna offerta creata", flush=True)
        return False

    print("=" * 40, flush=True)
    print(f"✅ CONTROPROPOSTA INVIATA: {token_offer['id']}", flush=True)
    print(f"💰 €{amount / 100:.2f} ({len(asset_ids)} × €0,20)", flush=True)
    print("🎯 Kulenovic NON ceduto", flush=True)
    print("=" * 40, flush=True)

    return True


# =========================
# STATE
# =========================

def should_retry_unknown(offer_id):
    now = time.time()

    with state_lock:
        last = unknown_price_offers.get(offer_id)

        if last is None or now - last >= UNKNOWN_PRICE_RETRY:
            unknown_price_offers[offer_id] = now
            return True

        return False


def mark_completed(offer_id):
    with state_lock:
        processed.add(offer_id)
        unknown_price_offers.pop(offer_id, None)


# =========================
# PROCESS
# =========================

def process_offer(offer):
    offer_id = str(offer.get("id") or "").strip()

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

    print("\n" + "=" * 40, flush=True)
    print(f"📨 OFFERTA {offer_id}", flush=True)

    if not should_retry_unknown(offer_id):
        print("⏳ Floor sconosciuto: attendo", flush=True)
        return

    sender_cards = (
        (offer.get("senderSide") or {}).get("anyCards") or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {}).get("anyCards") or []
    )

    if not any(is_kulenovic(c) for c in receiver_cards):
        mark_completed(offer_id)
        return

    print("🎯 Kulenovic trovato", flush=True)

    ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not ids:
        mark_completed(offer_id)
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print("❌ Impossibile verificare tutte le carte", flush=True)
        return

    print(f"🔎 Controllo {len(cards)} carta/e", flush=True)

    valid_cards = []
    unknown = False

    for card in cards:
        try:
            valid, reason = valid_card(card)

            if valid:
                valid_cards.append(card)
            elif reason == "unknown_price":
                unknown = True

        except Exception as e:
            print(f"❌ Errore controllo carta: {e}", flush=True)
            return

    print(
        f"📊 Carte valide: {len(valid_cards)}/{len(cards)}",
        flush=True,
    )

    if unknown:
        print("⚠️ PREZZO NON VERIFICABILE", flush=True)
        print("🟡 OFFERTA LASCIATA IN SOSPESO", flush=True)
        return

    if not valid_cards:
        print("❌ Nessuna carta idonea", flush=True)

        if reject_offer(offer):
            mark_completed(offer_id)

        return

    if len(valid_cards) != len(cards):
        print(
            f"⚠️ {len(cards) - len(valid_cards)} carta/e esclusa/e",
            flush=True,
        )

    if counter_offer(offer, valid_cards):
        print("🟢 Controproposta completata", flush=True)

        if reject_offer(offer):
            mark_completed(offer_id)
        else:
            print(
                "⚠️ Controproposta creata ma originale non rifiutata",
                flush=True,
            )
    else:
        print("🔴 Controproposta NON creata", flush=True)
        print("🟡 Offerta originale IN SOSPESO", flush=True)


# =========================
# WORKER
# =========================

def worker():
    print("🤖 BOT AVVIATO", flush=True)
    print(f"📦 VERSIONE BOT: {BOT_VERSION}", flush=True)
    print(f"💰 Pagamento: €{PAY_PER_CARD / 100:.2f} per carta", flush=True)
    print(
        f"📊 Range floor: €{MIN_PRICE / 100:.2f} - €{MAX_PRICE / 100:.2f}",
        flush=True,
    )
    print(f"🎂 Età: < {MAX_AGE}", flush=True)
    print("🏆 COMPETIZIONI: tutte le activeCompetitions", flush=True)
    print("💰 PRICE SOURCE: liveSingleSaleOffers", flush=True)
    print("🎯 MATCH PRICE: player + rarity + season", flush=True)
    print("💶 EUR: eurCents", flush=True)
    print("💵 USD: usdCents → EUR", flush=True)
    print("🚫 WEI: ESCLUSO dal floor FIAT", flush=True)
    print("🚫 referenceCurrency=WEI NON viene interpretato come ETH", flush=True)
    print("🚫 Conversione wei → EUR: OFF", flush=True)
    print("🚫 latestEnglishAuction: OFF", flush=True)
    print("🚫 publicMinPrices: OFF", flush=True)
    print("🚫 lowestPriceCard: OFF", flush=True)
    print("🚫 tokenPrices: OFF", flush=True)
    print("🛡️ PRICE UNKNOWN: LEAVE PENDING", flush=True)
    print(f"🔁 RETRY: {UNKNOWN_PRICE_RETRY}s", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)

    if not check_account():
        return

    if not inspect_live_schema():
        return

    while True:
        try:
            offers = get_offers()
            print(f"📨 Offerte pendenti: {len(offers)}", flush=True)

            for offer in offers:
                try:
                    process_offer(offer)
                except Exception as e:
                    print(f"❌ Errore offerta: {e}", flush=True)

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ Worker: {e}", flush=True)
            time.sleep(INTERVAL)


def start_worker():
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        _worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        ).start()

        print("✅ Thread Sorare avviato.", flush=True)


# =========================
# FLASK
# =========================

@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": BOT_VERSION,
        "dry_run": DRY_RUN,
        "pay_per_card_cents": PAY_PER_CARD,
        "interval_seconds": INTERVAL,
        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "max_age": MAX_AGE,
        "competition_mode": "ALL_ACTIVE_SORARE_COMPETITIONS",
        "prepare_mode": "LIVE_SCHEMA_AWARE",
        "settlement_currency": "EUR",
        "price_mode": "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",
        "price_eur_mode": "EUR_DIRECT_OR_USD_CONVERTED",
        "usd_conversion": True,
        "usd_conversion_mode": "USD_CENTS_TO_EUR",
        "usd_eur_cache_seconds": USD_EUR_CACHE_SECONDS,
        "wei_conversion": False,
        "wei_excluded_from_fiat_floor": True,
        "latest_english_auction_fallback": False,
        "public_min_prices": False,
        "lowest_price_card": False,
        "token_prices": False,
        "unknown_price_action": "LEAVE_PENDING",
        "unknown_price_retry_seconds": UNKNOWN_PRICE_RETRY,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": BOT_VERSION,
    })


if __name__ == "__main__":
    start_worker()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
