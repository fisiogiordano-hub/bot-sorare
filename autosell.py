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

BOT_VERSION = "AUTOSell-2.5-SOLANA-SIGN-FIX"
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
    name = card_name(c)
    slug = c.get("slug")
    return f"{name} [{slug}]" if slug and slug != name else name


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
            if os.path.exists(temp):
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

        return False


def add_card_to_json(asset_id, source="AUTOBUY"):
    asset_id = str(asset_id or "").strip()

    if not asset_id:
        return False

    with json_lock:
        cards = load_cards()

        for c in cards:
            if (
                isinstance(c, dict)
                and norm(c.get("asset_id")) == norm(asset_id)
            ):
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
# OPERATIONS BOT DECK
# ============================================================

def get_operations_deck_cards():
    asset_ids = []
    after = None

    while True:
        data = graphql(
            """
            query OperationsBotDeck(
                $deckName: String!
                $first: Int!
                $after: String
            ) {
                currentUser {
                    footballUserProfile {
                        deck(name: $deckName) {
                            cards(
                                first: $first
                                after: $after
                            ) {
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
            """,
            {
                "deckName": OPERATIONS_DECK_NAME,
                "first": DECK_PAGE_SIZE,
                "after": after
            }
        )

        if not data or data.get("errors"):
            return []

        profile = (
            ((data.get("data") or {}).get("currentUser") or {})
            .get("footballUserProfile")
        )

        deck = (profile or {}).get("deck")

        if not deck:
            print(
                f"⚠️ Deck non trovato: {OPERATIONS_DECK_NAME}",
                flush=True
            )
            return []

        cards = (
            ((deck.get("cards") or {}).get("nodes"))
            or []
        )

        for c in cards:
            asset_id = str(
                (c or {}).get("assetId") or ""
            ).strip()

            if asset_id:
                asset_ids.append(asset_id)

        page = (
            (deck.get("cards") or {}).get("pageInfo")
            or {}
        )

        if not page.get("hasNextPage"):
            break

        after = page.get("endCursor")

        if not after:
            break

    return list(dict.fromkeys(asset_ids))


def sync_operations_bot_deck():
    asset_ids = get_operations_deck_cards()

    if not asset_ids:
        return 0

    added = 0

    with json_lock:
        cards = load_cards()

    existing = {
        norm(c.get("asset_id"))
        for c in cards
        if isinstance(c, dict)
    }

    for asset_id in asset_ids:
        if norm(asset_id) in existing:
            continue

        if add_card_to_json(
            asset_id,
            source="OPERATIONS_DECK"
        ):
            added += 1
            existing.add(norm(asset_id))

            print(
                f"➕ OPERATIONS BOT → JSON READY: {asset_id}",
                flush=True
            )

    print(
        f"📋 Deck '{OPERATIONS_DECK_NAME}': "
        f"{len(asset_ids)} carte",
        flush=True
    )

    if added:
        print(
            f"📥 OPERATIONS BOT: aggiunte {added} nuove carte",
            flush=True
        )

    return added


# ============================================================
# SORARE
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    return {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
        **({"JWT-AUD": AUD} if AUD else {})
    }


def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {}
    }

    for attempt in range(3):
        try:
            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                retry = r.headers.get(
                    "Retry-After",
                    str(attempt + 2)
                )

                try:
                    retry = int(retry)
                except Exception:
                    retry = attempt + 2

                time.sleep(min(retry, 15))
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:1000]}",
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
                    )[:3000],
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
    global coverage_available

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
            headers={
                "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
            }
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
            f"🌐 Sorare Coverage aggiornata: "
            f"{len(result)} competizioni",
            flush=True
        )

        return result

    except Exception as e:
        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )

        with coverage_lock:
            coverage_available = False

        return old


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


def card_details(asset_ids):
    ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
        )
    )

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
        ((data.get("data") or {}).get("anyCards"))
        or []
    )


# ============================================================
# USD / EUR
# ============================================================

def usd_eur():
    global usd_rate
    global usd_time

    now = time.time()

    if usd_rate and now - usd_time < USD_CACHE:
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
            (r.json().get("rates") or {}).get("EUR")
        )

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = now

        return rate

    except Exception:
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


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):
    player = card.get("anyPlayer") or {}

    player_slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
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
        (
            (
                (data.get("data") or {})
                .get("tokens")
                or {}
            )
            .get("liveSingleSaleOffers")
            or {}
        )
        .get("nodes")
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
                same_season = (
                    int(c.get("seasonYear"))
                    == season
                )
            except Exception:
                continue

            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) == player_slug
                and norm(c.get("rarityTyped")) == rarity
                and same_season
            ):
                price = price_eur(
                    (
                        offer.get("receiverSide")
                        or {}
                    ).get("amounts") or {}
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
    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
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
        return False, {
            "code": "KULENOVIC"
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

    covered, active, covered_competitions, error = (
        coverage_info(card)
    )

    if error:
        return False, {
            "code": "COVERAGE_UNAVAILABLE",
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
        "rarity": rarity,
        "active_competitions": active,
        "covered_competitions": covered_competitions
    }


def print_rejection(card, info):
    code = (info or {}).get("code")

    print(
        f"🚫 AutoSell - ESCLUSA: {card_label(card)}",
        flush=True
    )

    print(
        f"   └─ Motivo: {code}",
        flush=True
    )


# ============================================================
# SIGNING
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
const crypto = require("crypto");

const { signAuthorizationRequest } = require("@sorare/crypto");
const {
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair,
  createSignableMessage,
  getBase58Decoder,
} = require("@solana/kit");
const { HDKey } = require("micro-key-producer/slip10.js");

const input = JSON.parse(
  fs.readFileSync(0, "utf8")
);

const privateKey = input.privateKey;
const authorizations = input.authorizations || [];

const SOLANA_PATH = "m/44'/501'/0'/0'";

async function deriveSolanaSigner() {
  const seed = Buffer.from(
    privateKey.replace(/^0x/, ""),
    "hex"
  );

  const derived = HDKey
    .fromMasterSeed(seed)
    .derive(SOLANA_PATH);

  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );

  return createSignerFromKeyPair(keyPair);
}

async function signSolana(a, r) {
  const signer = await deriveSolanaSigner();

  if (!r.senderAddress) {
    throw new Error(
      "senderAddress mancante nella Solana authorization"
    );
  }

  if (signer.address !== r.senderAddress) {
    throw new Error(
      "Solana senderAddress non corrisponde alla chiave derivata: " +
      signer.address +
      " != " +
      r.senderAddress
    );
  }

  const message = [
    "TRANSFER",
    r.transferProxyProgramAddress,
    r.merkleTreeAddress,
    r.leafIndex.toString(),
    r.nonce,
    r.expirationTimestamp.toString(),
    r.receiverAddress,
    "0x",
    r.originator,
  ].join(":");

  const hash = crypto
    .createHash("sha256")
    .update(
      Buffer.from(
        message,
        "utf8"
      )
    )
    .digest();

  const signableMessage =
    createSignableMessage(
      new Uint8Array(hash)
    );

  const [signatures] =
    await signer.signMessages([
      signableMessage
    ]);

  const signature =
    getBase58Decoder().decode(
      signatures[signer.address]
    );

  return {
    fingerprint: a.fingerprint,

    solanaTokenTransferApproval: {
      signature,
      nonce: r.nonce,
      expirationTimestamp:
        r.expirationTimestamp
    }
  };
}

async function buildApproval(a) {
  const r = a.request;

  if (!r || !r.__typename) {
    throw new Error(
      "AuthorizationRequest mancante"
    );
  }

  if (
    r.__typename ===
    "SolanaTokenTransferAuthorizationRequest"
  ) {
    return await signSolana(a, r);
  }

  if (
    r.__typename ===
    "StarkexTransferAuthorizationRequest"
  ) {
    const copy = { ...r };

    if (copy.amount != null) {
      copy.amount = BigInt(copy.amount);
    }

    const signature =
      signAuthorizationRequest(
        privateKey,
        copy
      );

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
    const copy = { ...r };

    if (copy.amountSell != null) {
      copy.amountSell = BigInt(copy.amountSell);
    }

    if (copy.amountBuy != null) {
      copy.amountBuy = BigInt(copy.amountBuy);
    }

    const signature =
      signAuthorizationRequest(
        privateKey,
        copy
      );

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
        signature: r.operationHash
      }
    };
  }

  throw new Error(
    "Authorization non supportata: " +
    r.__typename
  );
}

(async () => {
  const result = [];

  for (const a of authorizations) {
    result.push(
      await buildApproval(a)
    );
  }

  process.stdout.write(
    JSON.stringify(result)
  );
})().catch(err => {
  console.error(err);
  process.exit(1);
});
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
            p.stderr.strip()
            or "Firma fallita"
        )

    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError(
            "Output signer non valido"
        )


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price_cents):
    asset_id = str(
        card.get("assetId") or ""
    ).strip()

    if not asset_id:
        return None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN: {card_label(card)} "
            f"{format_eur(price_cents)}",
            flush=True
        )
        return "DRY-RUN"

    prepare_input = {
        # IMPORTANTE:
        # "type" NON viene inviato.
        # Il tuo endpoint lo rifiuta su prepareOfferInput.
        "sendAssetIds": [asset_id],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price_cents),
            "currency": "EUR"
        },
        "clientMutationId": str(uuid.uuid4())
    }

    print(
        "💳 Settlement currencies: EUR",
        flush=True
    )

    print(
        "🛠️ prepareOffer...",
        flush=True
    )

    print(
        f"   ├─ assetId: {asset_id}",
        flush=True
    )

    print(
        f"   └─ receiveAmount: "
        f"{format_eur(price_cents)}",
        flush=True
    )

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

                        ... on SolanaTokenTransferAuthorizationRequest {
                            assetId
                            leafIndex
                            merkleTreeAddress
                            originator
                            receiverAddress
                            senderAddress
                            expirationTimestamp
                            nonce
                            transferProxyProgramAddress
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
            "❌ AutoSell: prepareOffer senza risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ AutoSell prepareOffer:",
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

    print(
        f"🔐 Authorization ricevute: "
        f"{len(authorizations)}",
        flush=True
    )

    if not authorizations:
        print(
            "❌ AutoSell: nessuna authorization",
            flush=True
        )
        return None

    # Mostriamo il tipo ricevuto per capire
    # immediatamente quale signer viene usato.
    for a in authorizations:
        r = a.get("request") or {}

        print(
            f"🔑 Authorization type: "
            f"{r.get('__typename')}",
            flush=True
        )

    try:
        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ AutoSell firma: {e}",
            flush=True
        )
        return None

    print(
        f"✍️ Approval create: "
        f"{len(approvals)}",
        flush=True
    )

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
        "input": {
            "approvals": approvals,
            "dealId": str(uuid.uuid4()),
            "assetId": asset_id,
            "receiveAmount": {
                "amount": str(price_cents),
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
            "❌ AutoSell: createSingleSaleOffer "
            "senza risultato",
            flush=True
        )
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ AutoSell createSingleSaleOffer:",
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
    asset_id = str(
        row.get("asset_id") or ""
    ).strip()

    source = row.get("source") or "UNKNOWN"

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
        f"   ├─ Provenienza: {source}",
        flush=True
    )

    print(
        "   └─ Età: NON UTILIZZATA",
        flush=True
    )

    cards = card_details([asset_id])

    if len(cards) != 1:
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="CARD_DETAILS_UNAVAILABLE"
        )
        return

    card = cards[0]

    if norm(card.get("assetId")) != norm(asset_id):
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="ASSET_ID_MISMATCH"
        )
        return

    valid, info = validate_for_autosell(card)

    if not valid:
        print_rejection(card, info)

        code = (
            info or {}
        ).get("code", "INVALID")

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
        f"✅ AutoSell - Carta valida: "
        f"{card_label(card)}",
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
        + ", ".join(
            info.get("covered_competitions") or []
        ),
        flush=True
    )

    if SELL_PRICE_MODE != "FLOOR":
        update_json_card(
            asset_id,
            status="ERROR",
            last_error="INVALID_SELL_PRICE_MODE"
        )
        return

    sell_price = floor

    if (
        sell_price is None
        or not MIN_PRICE <= sell_price <= MAX_PRICE
    ):
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
            "❌ AutoSell: JSON non aggiornato "
            "→ NON VENDERE",
            flush=True
        )
        return

    offer_id = create_sale(
        card,
        sell_price
    )

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
            "⚠️ Vendita creata ma JSON non aggiornato",
            flush=True
        )
        return

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
        f"📊 LISTING MINIME: {MIN_LIVE_LISTINGS}",
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
        f"📋 DECK AGGIUNTIVO: "
        f"{OPERATIONS_DECK_NAME}",
        flush=True
    )

    print(
        f"💾 JSON: {JSON_PATH}",
        flush=True
    )

    try:
        headers()
        ensure_json_file()
    except Exception as e:
        print(
            f"❌ Configurazione: {e}",
            flush=True
        )
        return

    coverage = load_coverage(force=True)

    if coverage:
        print(
            f"🏆 COMPETIZIONI FOOTBALL: "
            f"{len(coverage)}",
            flush=True
        )
    else:
        print(
            "⚠️ Coverage non disponibile.",
            flush=True
        )

    if not check_account():
        return

    try:
        sync_operations_bot_deck()
    except Exception as e:
        print(
            f"⚠️ Sync deck: {e}",
            flush=True
        )

    while True:
        try:
            load_coverage()

            try:
                sync_operations_bot_deck()
            except Exception as e:
                print(
                    f"⚠️ Sync deck: {e}",
                    flush=True
                )

            rows = get_ready_cards()

            print(
                f"🗄️ Carte READY nel JSON: {len(rows)}",
                flush=True
            )

            for row in rows:
                try:
                    process_card(row)
                except Exception as e:
                    asset_id = str(
                        row.get("asset_id") or ""
                    )

                    print(
                        f"❌ AutoSell errore "
                        f"{asset_id}: {e}",
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

        print(
            "✅ Thread AutoSell avviato.",
            flush=True
        )


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
