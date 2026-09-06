import os
import time
import uuid
import json
import subprocess
import threading
from typing import Optional, Dict, Tuple

import requests
from flask import Flask, jsonify


app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
API_KEY = os.getenv("SORARE_API_KEY", "").strip()
PRIVATE_KEY = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = False

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LIVE_LISTINGS = 5

REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.5"))
TIMEOUT = 30
PORT = int(os.getenv("PORT", "10000"))

BOT_VERSION = "36.0-EUR-CENTS-SOLANA-PREPARE-SCHEMA-FIX"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4"
    "ac587bd3d40c7136796b6c0ed10ba0a6"
)
KID = os.getenv("KULENOVIC_ID", "").strip()

worker_started = False
worker_lock = threading.Lock()
last_scan = 0

floor_cache: Dict[
    Tuple[str, int, str],
    Tuple[float, Optional[int]]
] = {}

FLOOR_CACHE_SECONDS = 60

usd_rate = None
usd_time = 0

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
})


# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def eur(cents):
    try:
        return f"€{int(cents) / 100:.2f}"
    except Exception:
        return "N/D"


def label(card):
    parts = [
        card.get("name") or card.get("slug") or "Carta"
    ]

    for key in ("seasonYear", "rarityTyped"):
        if card.get(key):
            parts.append(str(card[key]))

    if card.get("serialNumber"):
        parts.append(f"#{card['serialNumber']}")

    return " • ".join(parts)


def sleep_request_delay():
    if REQUEST_DELAY > 0:
        time.sleep(REQUEST_DELAY)


# ============================================================
# HEADERS / GRAPHQL
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    result = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
    }

    if AUD:
        result["JWT-AUD"] = AUD

    if API_KEY:
        result["APIKEY"] = API_KEY

    return result


def graphql(query, variables=None, operation_name=None):

    for attempt in range(5):

        try:

            sleep_request_delay()

            payload = {
                "query": query,
                "variables": variables or {},
            }

            if operation_name:
                payload["operationName"] = operation_name

            response = session.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 HTTP {response.status_code}",
                flush=True,
            )

            if response.status_code == 429:

                try:
                    wait = int(
                        response.headers.get(
                            "Retry-After",
                            3 * (attempt + 1)
                        )
                    )
                except Exception:
                    wait = 3 * (attempt + 1)

                wait = min(max(wait, 2), 30)

                print(
                    f"⏳ Rate limit → {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:

                print(
                    "❌ HTTP:",
                    response.text[:2000],
                    flush=True,
                )

                time.sleep(min(attempt + 1, 10))
                continue

            try:
                data = response.json()
            except Exception:

                print(
                    "❌ Risposta non JSON",
                    flush=True,
                )

                time.sleep(min(attempt + 1, 10))
                continue

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:4000],
                    flush=True,
                )

            return data

        except Exception as exc:

            print(
                f"❌ GraphQL exception: {exc}",
                flush=True,
            )

            time.sleep(min(attempt + 1, 10))

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql(
        """
        query CurrentUser {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
        """,
        operation_name="CurrentUser",
    )

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ Account Sorare non verificato",
            flush=True,
        )

        return False

    print(
        "✅ Account: "
        + str(user.get("nickname") or user.get("slug")),
        flush=True,
    )

    return True


# ============================================================
# SOLANA KEY CHECK
# ============================================================

def node():

    configured = os.getenv("NODE_BINARY", "").strip()

    if configured:
        if (
            os.path.isfile(configured)
            and os.access(configured, os.X_OK)
        ):
            return configured

    for directory in os.getenv("PATH", "").split(os.pathsep):

        if not directory:
            continue

        executable = os.path.join(directory, "node")

        if (
            os.path.isfile(executable)
            and os.access(executable, os.X_OK)
        ):
            return executable

    return None


def derive_solana_address():

    executable = node()

    if not executable:
        raise RuntimeError("Node.js non disponibile")

    if not PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    script = r"""
const fs = require("fs");

const {
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
} = require("@solana/kit");

const {
  HDKey
} = require("micro-key-producer/slip10.js");

async function main() {

  const key =
    String(process.argv[1])
      .trim()
      .replace(/^0x/i, "");

  if (!/^[0-9a-fA-F]+$/.test(key))
    throw new Error("Private key non valida");

  if (key.length % 2 !== 0)
    throw new Error("Private key HEX con lunghezza dispari");

  const seed = Buffer.from(key, "hex");

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive("m/44'/501'/0'/0'");

  if (!derived.privateKey)
    throw new Error("Derivazione Solana fallita");

  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );

  const signer =
    await createSignerFromKeyPair(keyPair);

  process.stdout.write(signer.address);
}

main().catch(error => {
  console.error(error.message || error);
  process.exit(1);
});
"""

    process = subprocess.run(
        [
            executable,
            "-e",
            script,
            PRIVATE_KEY,
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Derivazione Solana fallita"
        )

    return process.stdout.strip()


def check_solana_key():

    try:

        address = derive_solana_address()

        print(
            f"🔑 Derived Solana address: {address}",
            flush=True,
        )

        print(
            "ℹ️ Verifica senderAddress effettuata "
            "durante prepareOffer",
            flush=True,
        )

        return address

    except Exception as exc:

        print(
            f"❌ Solana key check: {exc}",
            flush=True,
        )

        return None


# ============================================================
# GALLERY
# ============================================================

def get_gallery():

    cards = []
    after = None
    page_number = 0
    total = 0
    sealed = 0

    while True:

        page_number += 1

        data = graphql(
            """
            query Gallery($first:Int,$after:String) {
                currentUser {
                    cards(
                        first:$first,
                        after:$after,
                        ownedByMe:true,
                        sport:FOOTBALL
                    ) {
                        nodes {
                            assetId
                            slug
                            name
                            rarityTyped
                            seasonYear
                            serialNumber
                            sealed
                            anyPlayer {
                                slug
                                displayName
                            }
                            liveSingleSaleOffer {
                                id
                                status
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
            """,
            {
                "first": 50,
                "after": after,
            },
            operation_name="Gallery",
        )

        if not data or data.get("errors"):
            return None

        result = (
            ((data.get("data") or {})
             .get("currentUser") or {})
            .get("cards") or {}
        )

        nodes = result.get("nodes") or []

        total += len(nodes)

        for card in nodes:

            if card.get("sealed"):
                sealed += 1
            else:
                cards.append(card)

        page = result.get("pageInfo") or {}

        if not page.get("hasNextPage"):
            break

        after = page.get("endCursor")

        if not after:
            break

    cards = [
        c for c in cards
        if norm(c.get("rarityTyped")) == "limited"
    ]

    print(
        f"📦 Gallery totale: {total} | "
        f"🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(cards)}",
        flush=True,
    )

    return cards


# ============================================================
# LINEUP / IDENTIFIERS
# ============================================================

def identifiers(card):

    result = set()

    for key in ("assetId", "slug"):

        value = norm(card.get(key))

        if value:
            result.add(value)

    return result


def get_lineup():

    data = graphql(
        """
        query CurrentLineup {
            currentUser {
                blockchainCardsInLineups(
                    sport:FOOTBALL
                )
            }
        }
        """,
        operation_name="CurrentLineup",
    )

    if not data or data.get("errors"):
        return None

    value = (
        ((data.get("data") or {})
         .get("currentUser") or {})
        .get("blockchainCardsInLineups")
    )

    if not isinstance(value, list):
        return None

    return {norm(x) for x in value if x}


def in_lineup(card, lineup):

    if lineup is None:
        return None

    return bool(identifiers(card) & lineup)


def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KID:
        wanted.add(norm(KID))

    return bool(identifiers(card) & wanted)


# ============================================================
# EUR
# ============================================================

def usd_eur():

    global usd_rate, usd_time

    now = time.time()

    if usd_rate and now - usd_time < 300:
        return usd_rate

    try:

        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10,
        )

        if response.status_code != 200:
            return None

        rate = float(
            response.json()["rates"]["EUR"]
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
        value = int(amounts.get("eurCents"))

        if value > 0:
            return value

    except Exception:
        pass

    try:
        usd_cents = float(
            amounts.get("usdCents")
        )
    except Exception:
        usd_cents = 0

    if usd_cents <= 0:
        return None

    rate = usd_eur()

    if not rate:
        return None

    return int(round(usd_cents * rate))


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

    if not player_slug or rarity != "limited":
        return None

    cache_key = (
        player_slug,
        season,
        rarity,
    )

    now = time.time()

    cached = floor_cache.get(cache_key)

    if cached and now - cached[0] < FLOOR_CACHE_SECONDS:
        return cached[1]

    data = graphql(
        """
        query LiveFloor($playerSlug:String,$first:Int) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug:$playerSlug,
                    first:$first
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
        """,
        {
            "playerSlug": player_slug,
            "first": 50,
        },
        operation_name="LiveFloor",
    )

    if not data or data.get("errors"):

        floor_cache[cache_key] = (now, None)
        return None

    offers = (
        (((data.get("data") or {})
          .get("tokens") or {})
         .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:

        market_cards = (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        )

        for market_card in market_cards:

            market_player = (
                market_card.get("anyPlayer") or {}
            )

            try:
                market_season = int(
                    market_card.get("seasonYear")
                )
            except Exception:
                continue

            if not (
                norm(market_player.get("slug"))
                == player_slug
                and
                norm(market_card.get("rarityTyped"))
                == rarity
                and
                market_season == season
            ):
                continue

            amounts = (
                (offer.get("receiverSide") or {})
                .get("amounts") or {}
            )

            price = price_eur(amounts)

            if price is not None:
                prices.append(price)

            break

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"⚠️ {label(card)}: "
            f"solo {len(prices)} listing comparabili",
            flush=True,
        )

        floor_cache[cache_key] = (now, None)
        return None

    floor = min(prices)

    floor_cache[cache_key] = (now, floor)

    print(
        f"📊 {label(card)} → "
        f"{len(prices)} listing | "
        f"FLOOR {eur(floor)}",
        flush=True,
    )

    return floor


# ============================================================
# VALIDATION
# ============================================================

def validate(card, lineup):

    if card.get("sealed"):
        return False, "VAULT", None

    if is_kulenovic(card):
        return False, "KULENOVIC", None

    if norm(card.get("rarityTyped")) != "limited":
        return False, "RARITY", None

    lineup_state = in_lineup(card, lineup)

    if lineup_state is None:
        return False, "LINEUP_UNKNOWN", None

    if lineup_state:
        return False, "LINEUP", None

    floor = live_floor(card)

    if floor is None:
        return False, "PRICE_UNKNOWN", None

    if floor < MIN_PRICE:
        return False, "PRICE_LOW", floor

    if floor > MAX_PRICE:
        return False, "PRICE_HIGH", floor

    return True, "OK", floor


def reject(card, reason, value=None):

    if reason == "PRICE_LOW":

        msg = (
            f"FLOOR {eur(value)} "
            f"SOTTO IL MINIMO ({eur(MIN_PRICE)})"
        )

    elif reason == "PRICE_HIGH":

        msg = (
            f"FLOOR {eur(value)} "
            f"SOPRA IL MASSIMO ({eur(MAX_PRICE)})"
        )

    else:

        messages = {
            "VAULT": "CARTA IN CASSAFORTE",
            "KULENOVIC": "KULENOVIC MAI IN VENDITA",
            "RARITY": "RARITÀ DIVERSA DA LIMITED",
            "LINEUP": "CARTA IN LINEUP",
            "LINEUP_UNKNOWN": "LINEUP NON VERIFICABILE",
            "PRICE_UNKNOWN": "PREZZO LIVE NON VERIFICABILE",
        }

        msg = messages.get(reason, reason)

    print(
        f"🚫 {label(card)} → {msg}",
        flush=True,
    )


# ============================================================
# SOLANA SIGN
# ============================================================

def sign_solana(authorization):

    executable = node()

    if not executable:
        raise RuntimeError("Node.js non disponibile")

    if not PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    request = authorization.get("request") or {}

    typename = request.get("__typename")

    if typename != (
        "SolanaTokenTransferAuthorizationRequest"
    ):
        raise RuntimeError(
            "Authorization non Solana: "
            + str(typename)
        )

    required = [
        "transferProxyProgramAddress",
        "merkleTreeAddress",
        "leafIndex",
        "nonce",
        "expirationTimestamp",
        "receiverAddress",
        "originator",
        "senderAddress",
    ]

    missing = [
        x for x in required
        if request.get(x) is None
    ]

    if missing:
        raise RuntimeError(
            "Authorization Solana incompleta: "
            + ", ".join(missing)
        )

    payload = {
        "authorization": authorization,
        "privateKey": PRIVATE_KEY,
    }

    script = r"""
const fs = require("fs");

const {
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair,
  createSignableMessage,
  getBase58Decoder
} = require("@solana/kit");

const {
  HDKey
} = require("micro-key-producer/slip10.js");

async function main() {

  const input = JSON.parse(
    fs.readFileSync(0, "utf8")
  );

  const request =
    input.authorization.request;

  const privateKeyHex =
    String(input.privateKey)
      .trim()
      .replace(/^0x/i, "");

  if (!/^[0-9a-fA-F]+$/.test(privateKeyHex))
    throw new Error("Private key non valida");

  if (privateKeyHex.length % 2 !== 0)
    throw new Error("Private key HEX non valida");

  const seed =
    Buffer.from(privateKeyHex, "hex");

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive("m/44'/501'/0'/0'");

  if (!derived.privateKey)
    throw new Error("Derivazione Solana fallita");

  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );

  const signer =
    await createSignerFromKeyPair(keyPair);

  const derivedAddress =
    signer.address;

  const senderAddress =
    request.senderAddress;

  console.error(
    "🔑 Derived Solana address:",
    derivedAddress
  );

  console.error(
    "📨 Sorare senderAddress:",
    senderAddress
  );

  if (derivedAddress !== senderAddress) {

    throw new Error(
      "SOLANA ADDRESS MISMATCH: " +
      "chiave derivata=" +
      derivedAddress +
      " | senderAddress=" +
      senderAddress
    );

  }

  const message = [
    "TRANSFER",
    request.transferProxyProgramAddress,
    request.merkleTreeAddress,
    request.leafIndex.toString(),
    request.nonce.toString(),
    request.expirationTimestamp.toString(),
    request.receiverAddress,
    "0x",
    request.originator
  ].join(":");

  console.error(
    "📝 Solana message:",
    message
  );

  const messageBytes =
    new TextEncoder().encode(message);

  const messageHash =
    await crypto.subtle.digest(
      "SHA-256",
      messageBytes
    );

  const signableMessage =
    createSignableMessage(
      new Uint8Array(messageHash)
    );

  const result =
    await signer.signMessages([
      signableMessage
    ]);

  const signature =
    getBase58Decoder().decode(
      result[0][signer.address]
    );

  process.stdout.write(
    JSON.stringify({
      fingerprint:
        input.authorization.fingerprint,

      solanaTokenTransferApproval: {
        signature,
        nonce: request.nonce,
        expirationTimestamp:
          request.expirationTimestamp
      }
    })
  );
}

main().catch(error => {

  console.error(
    error && error.stack
      ? error.stack
      : error
  );

  process.exit(1);

});
"""

    process = subprocess.run(
        [
            executable,
            "-e",
            script,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
    )

    if process.stderr:
        print(
            process.stderr.strip(),
            flush=True,
        )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Firma Solana fallita"
        )

    try:
        return json.loads(process.stdout)
    except Exception as exc:
        raise RuntimeError(
            "Output firma Solana non valido: "
            + str(exc)
        )


# ============================================================
# STARKEX
# ============================================================

def sign_starkex(authorization):

    executable = node()

    if not executable:
        raise RuntimeError("Node.js non disponibile")

    if not PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    request = authorization.get("request") or {}

    if request.get("__typename") != (
        "StarkexTransferAuthorizationRequest"
    ):
        raise RuntimeError(
            "Authorization StarkEx non supportata"
        )

    required = [
        "amount",
        "expirationTimestamp",
        "nonce",
        "receiverPublicKey",
        "receiverVaultId",
        "senderVaultId",
        "token",
    ]

    missing = [
        x for x in required
        if request.get(x) is None
    ]

    if missing:
        raise RuntimeError(
            "Authorization StarkEx incompleta: "
            + ", ".join(missing)
        )

    payload = {
        "authorization": authorization,
        "privateKey": PRIVATE_KEY,
    }

    script = r"""
const fs = require("fs");

const {
  signAuthorizationRequest
} = require("@sorare/crypto");

async function main() {

  const input = JSON.parse(
    fs.readFileSync(0, "utf8")
  );

  const request =
    input.authorization.request;

  request.amount =
    BigInt(request.amount);

  const signature =
    await signAuthorizationRequest(
      input.privateKey,
      request
    );

  process.stdout.write(
    JSON.stringify({
      fingerprint:
        input.authorization.fingerprint,

      starkexTransferApproval: {
        nonce: request.nonce,
        expirationTimestamp:
          request.expirationTimestamp,
        signature
      }
    })
  );
}

main().catch(error => {

  console.error(
    error && error.stack
      ? error.stack
      : error
  );

  process.exit(1);

});
"""

    process = subprocess.run(
        [
            executable,
            "-e",
            script,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
    )

    if process.stderr:
        print(
            process.stderr.strip(),
            flush=True,
        )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Firma StarkEx fallita"
        )

    return json.loads(process.stdout)


# ============================================================
# PREPARE
# ============================================================

def prepare_offer(asset_id, price):

    input_data = {
        "sendAssetIds": [asset_id],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR",
        },
        "clientMutationId": str(uuid.uuid4()),
    }

    print(
        "📤 prepareOffer → "
        + json.dumps(input_data),
        flush=True,
    )

    data = graphql(
        """
        mutation PrepareOffer(
            $input:prepareOfferInput!
        ) {
            prepareOffer(input:$input) {

                authorizations {
                    fingerprint

                    request {
                        __typename

                        ... on StarkexTransferAuthorizationRequest {
                            amount
                            condition
                            expirationTimestamp

                            feeInfoUser {
                                feeLimit
                                sourceVaultId
                                tokenId
                            }

                            nonce
                            receiverPublicKey
                            receiverVaultId
                            senderVaultId
                            token
                        }

                        ... on SolanaTokenTransferAuthorizationRequest {
                            assetId
                            expirationTimestamp
                            leafIndex
                            merkleTreeAddress
                            nonce
                            originator
                            receiverAddress
                            senderAddress
                            transferProxyProgramAddress
                        }
                    }
                }

                errors {
                    message
                }
            }
        }
        """,
        {"input": input_data},
        operation_name="PrepareOffer",
    )

    if not data:
        raise RuntimeError(
            "prepareOffer: nessuna risposta"
        )

    if data.get("errors"):
        raise RuntimeError(
            "prepareOffer GraphQL error: "
            + json.dumps(data["errors"])[:4000]
        )

    result = (
        ((data.get("data") or {})
         .get("prepareOffer"))
        or {}
    )

    errors = result.get("errors") or []

    if errors:
        raise RuntimeError(
            "prepareOffer: "
            + "; ".join(
                str(x.get("message", ""))
                for x in errors
            )
        )

    authorizations = (
        result.get("authorizations") or []
    )

    if not authorizations:
        raise RuntimeError(
            "prepareOffer non ha restituito autorizzazioni"
        )

    approvals = []

    for authorization in authorizations:

        request = authorization.get("request") or {}
        typename = request.get("__typename")

        print(
            f"🔐 Authorization: {typename}",
            flush=True,
        )

        if typename == (
            "StarkexTransferAuthorizationRequest"
        ):
            approval = sign_starkex(authorization)

        elif typename == (
            "SolanaTokenTransferAuthorizationRequest"
        ):
            approval = sign_solana(authorization)

        else:
            raise RuntimeError(
                "Authorization non supportata: "
                + str(typename)
            )

        approvals.append(approval)

    return approvals


# ============================================================
# CREATE OFFER
# ============================================================

def create_offer(asset_id, price, approvals):

    input_data = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "assetId": asset_id,
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR",
        },
        "clientMutationId": str(uuid.uuid4()),
    }

    data = graphql(
        """
        mutation CreateSingleSaleOffer(
            $input:createSingleSaleOfferInput!
        ) {
            createSingleSaleOffer(input:$input) {

                tokenOffer {
                    id
                    startDate
                    endDate
                }

                errors {
                    message
                }
            }
        }
        """,
        {"input": input_data},
        operation_name="CreateSingleSaleOffer",
    )

    if not data:
        raise RuntimeError(
            "createSingleSaleOffer: nessuna risposta"
        )

    if data.get("errors"):
        raise RuntimeError(
            "createSingleSaleOffer GraphQL error: "
            + json.dumps(data["errors"])[:4000]
        )

    result = (
        ((data.get("data") or {})
         .get("createSingleSaleOffer"))
        or {}
    )

    errors = result.get("errors") or []

    if errors:
        raise RuntimeError(
            "createSingleSaleOffer: "
            + "; ".join(
                str(x.get("message", ""))
                for x in errors
            )
        )

    offer = result.get("tokenOffer")

    if not offer:
        raise RuntimeError(
            "createSingleSaleOffer non ha restituito tokenOffer"
        )

    return offer


# ============================================================
# AUTOSELL
# ============================================================

def autosell(card, price):

    asset_id = card.get("assetId")

    if not asset_id:
        raise RuntimeError("assetId mancante")

    print(
        f"💰 SELL {label(card)} → {eur(price)}",
        flush=True,
    )

    if DRY_RUN:
        print(
            "🟡 DRY_RUN: nessuna vendita eseguita",
            flush=True,
        )
        return True

    approvals = prepare_offer(
        asset_id,
        price,
    )

    offer = create_offer(
        asset_id,
        price,
        approvals,
    )

    print(
        f"✅ OFFER CREATA: {offer.get('id')}",
        flush=True,
    )

    return True


# ============================================================
# WORKER
# ============================================================

def worker():

    global worker_started, last_scan

    with worker_lock:

        if worker_started:
            return

        worker_started = True

    print(
        "========================================",
        flush=True,
    )

    print(
        "🤖 SORARE AUTOSELL",
        flush=True,
    )

    print(
        f"Version: {BOT_VERSION}",
        flush=True,
    )

    print(
        f"DRY_RUN: {DRY_RUN}",
        flush=True,
    )

    print(
        f"MIN: {eur(MIN_PRICE)} | "
        f"MAX: {eur(MAX_PRICE)}",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    # --------------------------------------------------------
    # KEY CHECK
    # --------------------------------------------------------

    derived_address = check_solana_key()

    if derived_address:

        print(
            "⚠️ L'indirizzo derivato viene confrontato "
            "con senderAddress durante prepareOffer.",
            flush=True,
        )

    while True:

        try:

            if not check_account():
                time.sleep(15)
                continue

            cards = get_gallery()

            if cards is None:
                time.sleep(15)
                continue

            lineup = get_lineup()

            if lineup is None:

                print(
                    "❌ Impossibile verificare lineup",
                    flush=True,
                )

                time.sleep(15)
                continue

            candidates = 0

            for card in cards:

                try:

                    if card.get("liveSingleSaleOffer"):

                        print(
                            f"⏭️ {label(card)} → GIÀ IN VENDITA",
                            flush=True,
                        )

                        continue

                    ok, reason, price = validate(
                        card,
                        lineup,
                    )

                    if not ok:

                        reject(
                            card,
                            reason,
                            price,
                        )

                        continue

                    candidates += 1

                    print(
                        f"✅ {label(card)} → "
                        f"VENDIBILE {eur(price)}",
                        flush=True,
                    )

                    try:

                        autosell(
                            card,
                            price,
                        )

                    except Exception as exc:

                        print(
                            f"🔴 AutoSell fallito "
                            f"{label(card)}: {exc}",
                            flush=True,
                        )

                except Exception as exc:

                    print(
                        f"❌ Carta {label(card)}: {exc}",
                        flush=True,
                    )

            last_scan = int(time.time())

            print(
                f"🏁 SCANSIONE COMPLETATA | "
                f"candidate={candidates}",
                flush=True,
            )

        except Exception as exc:

            print(
                f"🔥 Worker: {exc}",
                flush=True,
            )

        time.sleep(15)


# ============================================================
# HTTP
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "status": "ok",
        "bot": BOT_VERSION,
        "dry_run": DRY_RUN,
        "min_price": MIN_PRICE,
        "min_price_eur": eur(MIN_PRICE),
        "max_price": MAX_PRICE,
        "max_price_eur": eur(MAX_PRICE),
        "last_scan": last_scan,
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "bot": BOT_VERSION,
        "dry_run": DRY_RUN,
        "min_price": MIN_PRICE,
        "max_price": MAX_PRICE,
        "last_scan": last_scan,
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()

    app.run(
        host="0.0.0.0",
        port=PORT,
    )
