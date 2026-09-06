import os
import time
import uuid
import json
import subprocess
import threading
from typing import Optional, Dict, Any, List, Tuple

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

PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# REAL MODE
# ============================================================

DRY_RUN = False


# ============================================================
# PRICE
# ============================================================

MIN_PRICE = 32       # €0.32
MAX_PRICE = 70       # €0.70

MIN_LIVE_LISTINGS = 5


# ============================================================
# BOT
# ============================================================

REQUEST_DELAY = float(
    os.getenv(
        "REQUEST_DELAY",
        "1.5",
    )
)

TIMEOUT = 30

SCAN_DELAY = int(
    os.getenv(
        "SCAN_DELAY",
        "15",
    )
)

BOT_VERSION = (
    "36.0-EUR-CENTS-SOLANA-PREPARE-SCHEMA-FIX"
)


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4"
    "ac587bd3d40c7136796b6c0ed10ba0a6"
)

KID = os.getenv(
    "KULENOVIC_ID",
    "",
).strip()


# ============================================================
# STATE
# ============================================================

worker_started = False

worker_lock = threading.Lock()

last_scan = 0

floor_cache: Dict[
    Tuple[str, int, str],
    Tuple[float, Optional[int]]
] = {}

FLOOR_CACHE_SECONDS = 60


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        f"Sorare-AutoSell/{BOT_VERSION}"
    ),
})


# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def eur(cents):

    if cents is None:
        return "N/D"

    try:
        cents = int(cents)
    except Exception:
        return "N/D"

    return f"€{cents / 100:.2f}"


def label(card):

    parts = [
        card.get("name")
        or (
            card.get("anyPlayer") or {}
        ).get("displayName")
        or card.get("slug")
        or "Carta"
    ]

    if card.get("seasonYear"):
        parts.append(
            str(card["seasonYear"])
        )

    if card.get("rarityTyped"):
        parts.append(
            str(card["rarityTyped"])
        )

    if card.get("serialNumber"):
        parts.append(
            f"#{card['serialNumber']}"
        )

    return " • ".join(parts)


def sleep_request_delay():

    if REQUEST_DELAY > 0:
        time.sleep(REQUEST_DELAY)


# ============================================================
# HEADERS
# ============================================================

def headers():

    if not TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith(
        "bearer "
    ):
        token = "Bearer " + token

    result = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent":
            f"Sorare-AutoSell/{BOT_VERSION}",
    }

    if AUD:
        result["JWT-AUD"] = AUD

    if API_KEY:
        result["APIKEY"] = API_KEY

    return result


# ============================================================
# GRAPHQL
# ============================================================

def graphql(
    query,
    variables=None,
    operation_name=None,
):

    for attempt in range(5):

        try:

            sleep_request_delay()

            payload = {
                "query": query,
                "variables": variables or {},
            }

            if operation_name:
                payload["operationName"] = (
                    operation_name
                )

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

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                try:
                    wait = int(
                        retry_after
                    )
                except Exception:
                    wait = 3 * (
                        attempt + 1
                    )

                wait = min(
                    max(wait, 2),
                    30,
                )

                print(
                    f"⏳ Rate limit "
                    f"→ attendo {wait}s",
                    flush=True,
                )

                time.sleep(wait)

                continue

            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    "❌ HTTP:",
                    response.text[:3000],
                    flush=True,
                )

                time.sleep(
                    min(
                        attempt + 1,
                        10,
                    )
                )

                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = response.json()

            except Exception:

                print(
                    "❌ Risposta non JSON:",
                    response.text[:3000],
                    flush=True,
                )

                time.sleep(
                    min(
                        attempt + 1,
                        10,
                    )
                )

                continue

            # ------------------------------------------------
            # GRAPHQL ERROR
            # ------------------------------------------------

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                    )[:5000],
                    flush=True,
                )

            return data

        except Exception as exc:

            print(
                f"❌ GraphQL exception: {exc}",
                flush=True,
            )

            time.sleep(
                min(
                    attempt + 1,
                    10,
                )
            )

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
        + str(
            user.get("nickname")
            or user.get("slug")
        ),
        flush=True,
    )

    print(
        "🔐 Stark key account: "
        + (
            "PRESENTE"
            if user.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True,
    )

    return True


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

        print(
            f"🔎 Gallery page "
            f"{page_number}...",
            flush=True,
        )

        data = graphql(
            """
            query Gallery(
                $first:Int,
                $after:String
            ){

                currentUser {

                    cards(
                        first:$first,
                        after:$after,
                        ownedByMe:true,
                        sport:FOOTBALL
                    ){

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

        if not data:

            return None

        if data.get("errors"):

            return None

        result = (
            ((data.get("data") or {})
             .get("currentUser") or {})
            .get("cards")
            or {}
        )

        nodes = (
            result.get("nodes")
            or []
        )

        print(
            f"📄 Gallery page "
            f"{page_number}: "
            f"{len(nodes)} carte",
            flush=True,
        )

        total += len(nodes)

        for card in nodes:

            if card.get("sealed"):

                sealed += 1

            else:

                cards.append(card)

        page = (
            result.get("pageInfo")
            or {}
        )

        if not page.get(
            "hasNextPage"
        ):
            break

        after = page.get(
            "endCursor"
        )

        if not after:
            break

    # --------------------------------------------------------
    # SOLO LIMITED
    # --------------------------------------------------------

    cards = [
        card
        for card in cards
        if norm(
            card.get("rarityTyped")
        ) == "limited"
    ]

    print(
        f"📦 Gallery totale: {total} | "
        f"🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(cards)}",
        flush=True,
    )

    return cards


# ============================================================
# LINEUP
# ============================================================

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

    if not data or data.get(
        "errors"
    ):
        return None

    value = (
        ((data.get("data") or {})
         .get("currentUser") or {})
        .get("blockchainCardsInLineups")
    )

    if not isinstance(
        value,
        list,
    ):
        return None

    return {
        norm(item)
        for item in value
        if item
    }


# ============================================================
# IDENTIFIERS
# ============================================================

def identifiers(card):

    result = set()

    asset_id = norm(
        card.get("assetId")
    )

    slug = norm(
        card.get("slug")
    )

    if asset_id:
        result.add(asset_id)

    if slug:
        result.add(slug)

    return result


def in_lineup(card, lineup):

    if lineup is None:
        return None

    return bool(
        identifiers(card)
        & lineup
    )


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KID:
        wanted.add(
            norm(KID)
        )

    return bool(
        identifiers(card)
        & wanted
    )


# ============================================================
# USD -> EUR
# ============================================================

usd_rate = None
usd_time = 0


def usd_eur():

    global usd_rate
    global usd_time

    now = time.time()

    if (
        usd_rate
        and
        now - usd_time < 300
    ):
        return usd_rate

    try:

        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR",
            },
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

    if not isinstance(
        amounts,
        dict,
    ):
        return None

    # --------------------------------------------------------
    # EUR
    # --------------------------------------------------------

    try:

        value = int(
            amounts.get(
                "eurCents"
            )
        )

        if value > 0:
            return value

    except Exception:
        pass

    # --------------------------------------------------------
    # USD
    # --------------------------------------------------------

    try:

        usd_cents = float(
            amounts.get(
                "usdCents"
            )
        )

    except Exception:

        usd_cents = 0

    if usd_cents <= 0:
        return None

    rate = usd_eur()

    if not rate:
        return None

    return int(
        round(
            usd_cents * rate
        )
    )


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

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

    except Exception:

        return None

    if not player_slug:
        return None

    if rarity != "limited":
        return None

    cache_key = (
        player_slug,
        season,
        rarity,
    )

    now = time.time()

    cached = floor_cache.get(
        cache_key
    )

    if cached:

        cached_time, cached_floor = cached

        if (
            now - cached_time
            < FLOOR_CACHE_SECONDS
        ):

            if cached_floor is not None:

                print(
                    f"📦 CACHE FLOOR "
                    f"{label(card)} "
                    f"→ {eur(cached_floor)}",
                    flush=True,
                )

            return cached_floor

    data = graphql(
        """
        query LiveFloor(
            $playerSlug:String,
            $first:Int
        ){

            tokens {

                liveSingleSaleOffers(
                    playerSlug:$playerSlug,
                    first:$first
                ){

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
        """,
        {
            "playerSlug": player_slug,
            "first": 50,
        },
        operation_name="LiveFloor",
    )

    if not data or data.get(
        "errors"
    ):

        floor_cache[
            cache_key
        ] = (
            now,
            None,
        )

        return None

    offers = (
        (((data.get("data") or {})
          .get("tokens") or {})
         .get("liveSingleSaleOffers")
         or {})
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender_side = (
            offer.get(
                "senderSide"
            )
            or {}
        )

        market_cards = (
            sender_side.get(
                "anyCards"
            )
            or []
        )

        for market_card in market_cards:

            market_player = (
                market_card.get(
                    "anyPlayer"
                )
                or {}
            )

            try:

                market_season = int(
                    market_card.get(
                        "seasonYear"
                    )
                )

            except Exception:

                continue

            same_card = (
                norm(
                    market_player.get(
                        "slug"
                    )
                )
                == player_slug

                and

                norm(
                    market_card.get(
                        "rarityTyped"
                    )
                )
                == rarity

                and

                market_season == season
            )

            if not same_card:
                continue

            amounts = (
                (
                    offer.get(
                        "receiverSide"
                    )
                    or {}
                )
                .get(
                    "amounts"
                )
                or {}
            )

            price = price_eur(
                amounts
            )

            if price is not None:
                prices.append(price)

            break

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"⚠️ {label(card)}: "
            f"solo {len(prices)} listing "
            f"comparabili "
            f"(minimo "
            f"{MIN_LIVE_LISTINGS})",
            flush=True,
        )

        floor_cache[
            cache_key
        ] = (
            now,
            None,
        )

        return None

    floor = min(prices)

    floor_cache[
        cache_key
    ] = (
        now,
        floor,
    )

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

        return (
            False,
            "VAULT",
            None,
        )

    if is_kulenovic(card):

        return (
            False,
            "KULENOVIC",
            None,
        )

    if norm(
        card.get("rarityTyped")
    ) != "limited":

        return (
            False,
            "RARITY",
            None,
        )

    lineup_state = in_lineup(
        card,
        lineup,
    )

    if lineup_state is None:

        return (
            False,
            "LINEUP_UNKNOWN",
            None,
        )

    if lineup_state:

        return (
            False,
            "LINEUP",
            None,
        )

    floor = live_floor(card)

    if floor is None:

        return (
            False,
            "PRICE_UNKNOWN",
            None,
        )

    if floor < MIN_PRICE:

        return (
            False,
            "PRICE_LOW",
            floor,
        )

    if floor > MAX_PRICE:

        return (
            False,
            "PRICE_HIGH",
            floor,
        )

    return (
        True,
        "OK",
        floor,
    )


# ============================================================
# REJECT
# ============================================================

def reject(
    card,
    reason,
    value=None,
):

    messages = {

        "VAULT":
            "CARTA IN CASSAFORTE",

        "KULENOVIC":
            "KULENOVIC MAI IN VENDITA",

        "RARITY":
            "RARITÀ DIVERSA DA LIMITED",

        "LINEUP":
            "CARTA IN LINEUP",

        "LINEUP_UNKNOWN":
            "LINEUP NON VERIFICABILE",

        "PRICE_UNKNOWN":
            "PREZZO LIVE NON VERIFICABILE",
    }

    if reason == "PRICE_LOW":

        msg = (
            f"FLOOR {eur(value)} "
            f"SOTTO IL MINIMO "
            f"({eur(MIN_PRICE)})"
        )

    elif reason == "PRICE_HIGH":

        msg = (
            f"FLOOR {eur(value)} "
            f"SOPRA IL MASSIMO "
            f"({eur(MAX_PRICE)})"
        )

    else:

        msg = messages.get(
            reason,
            reason,
        )

    print(
        f"🚫 {label(card)} "
        f"→ {msg}",
        flush=True,
    )


# ============================================================
# NODE
# ============================================================

def node():

    configured = os.getenv(
        "NODE_BINARY",
        "",
    ).strip()

    if configured:

        if (
            os.path.isfile(configured)
            and
            os.access(
                configured,
                os.X_OK,
            )
        ):

            return configured

    for directory in os.getenv(
        "PATH",
        "",
    ).split(os.pathsep):

        if not directory:
            continue

        executable = os.path.join(
            directory,
            "node",
        )

        if (
            os.path.isfile(executable)
            and
            os.access(
                executable,
                os.X_OK,
            )
        ):

            return executable

    return None


# ============================================================
# STARKEX SIGN
# ============================================================

def sign_starkex(
    authorization,
):

    executable = node()

    if not executable:

        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not PRIVATE_KEY:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    request = (
        authorization.get(
            "request"
        )
        or {}
    )

    typename = request.get(
        "__typename"
    )

    if typename != (
        "StarkexTransferAuthorizationRequest"
    ):

        raise RuntimeError(
            "Authorization StarkEx "
            "non supportata: "
            + str(typename)
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
        field
        for field in required
        if request.get(field) is None
    ]

    if missing:

        raise RuntimeError(
            "Authorization StarkEx "
            "incompleta. Mancano: "
            + ", ".join(missing)
        )

    payload = {
        "authorization":
            authorization,
        "privateKey":
            PRIVATE_KEY,
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

  const authorization =
    input.authorization;

  const request =
    authorization.request;

  if (
    request.__typename !==
    "StarkexTransferAuthorizationRequest"
  ) {

    throw new Error(
      "Authorization non StarkEx: "
      + request.__typename
    );
  }

  request.amount =
    BigInt(request.amount);

  const signature =
    await signAuthorizationRequest(
      input.privateKey,
      request
    );

  const result = {

    fingerprint:
      authorization.fingerprint,

    starkexTransferApproval: {

      nonce:
        request.nonce,

      expirationTimestamp:
        request.expirationTimestamp,

      signature
    }
  };

  process.stdout.write(
    JSON.stringify(result)
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
        input=json.dumps(
            payload
        ),
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

    try:

        return json.loads(
            process.stdout
        )

    except Exception as exc:

        raise RuntimeError(
            "Output firma StarkEx "
            "non valido: "
            + str(exc)
        )


# ============================================================
# SOLANA SIGN
# ============================================================

def sign_solana(
    authorization,
):

    executable = node()

    if not executable:

        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not PRIVATE_KEY:

        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    request = (
        authorization.get(
            "request"
        )
        or {}
    )

    typename = request.get(
        "__typename"
    )

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
        field
        for field in required
        if request.get(field) is None
    ]

    if missing:

        raise RuntimeError(
            "Authorization Solana "
            "incompleta. Mancano: "
            + ", ".join(missing)
        )

    payload = {
        "authorization":
            authorization,
        "privateKey":
            PRIVATE_KEY,
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


const DERIVATION_PATH =
  "m/44'/501'/0'/0'";


async function main() {

  const input = JSON.parse(
    fs.readFileSync(0, "utf8")
  );

  const authorization =
    input.authorization;

  const request =
    authorization.request;


  // --------------------------------------------------------
  // PRIVATE KEY
  // --------------------------------------------------------

  const privateKeyHex =
    String(input.privateKey)
      .replace(/^0x/i, "")
      .trim();


  if (
    !/^[0-9a-fA-F]+$/.test(
      privateKeyHex
    )
  ) {

    throw new Error(
      "SORARE_STARK_PRIVATE_KEY "
      + "non è HEX valido"
    );

  }


  if (
    privateKeyHex.length % 2 !== 0
  ) {

    throw new Error(
      "SORARE_STARK_PRIVATE_KEY "
      + "ha lunghezza HEX dispari"
    );

  }


  const seed =
    Buffer.from(
      privateKeyHex,
      "hex"
    );


  if (
    seed.length === 0
  ) {

    throw new Error(
      "Private key vuota"
    );

  }


  // --------------------------------------------------------
  // SLIP-0010
  // --------------------------------------------------------

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive(
        DERIVATION_PATH
      );


  if (
    !derived.privateKey
  ) {

    throw new Error(
      "Derivazione Solana "
      + "senza private key"
    );

  }


  // --------------------------------------------------------
  // ED25519
  // --------------------------------------------------------

  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );


  const signer =
    await createSignerFromKeyPair(
      keyPair
    );


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


  // --------------------------------------------------------
  // CRITICAL SECURITY CHECK
  // --------------------------------------------------------

  if (
    derivedAddress !== senderAddress
  ) {

    throw new Error(
      "SOLANA ADDRESS MISMATCH: "
      + "chiave derivata="
      + derivedAddress
      + " | senderAddress="
      + senderAddress
    );

  }


  // --------------------------------------------------------
  // OFFICIAL SORARE MESSAGE
  // --------------------------------------------------------

  const message = [

    "TRANSFER",

    request
      .transferProxyProgramAddress,

    request
      .merkleTreeAddress,

    request
      .leafIndex
      .toString(),

    request
      .nonce
      .toString(),

    request
      .expirationTimestamp
      .toString(),

    request
      .receiverAddress,

    "0x",

    request
      .originator

  ].join(":");


  console.error(
    "📝 Solana message:",
    message
  );


  // --------------------------------------------------------
  // UTF-8
  // --------------------------------------------------------

  const messageBytes =
    new TextEncoder().encode(
      message
    );


  // --------------------------------------------------------
  // SHA-256
  // --------------------------------------------------------

  const messageHash =
    await crypto.subtle.digest(
      "SHA-256",
      messageBytes
    );


  // --------------------------------------------------------
  // ED25519 SIGN
  // --------------------------------------------------------

  const signableMessage =
    createSignableMessage(
      new Uint8Array(
        messageHash
      )
    );


  const result =
    await signer.signMessages(
      [signableMessage]
    );


  const signatureBytes =
    getBase58Decoder().decode(
      result[0][
        signer.address
      ]
    );


  // --------------------------------------------------------
  // APPROVAL
  // --------------------------------------------------------

  const output = {

    fingerprint:
      authorization.fingerprint,

    solanaTokenTransferApproval: {

      signature:
        signatureBytes,

      nonce:
        request.nonce,

      expirationTimestamp:
        request.expirationTimestamp

    }

  };


  process.stdout.write(
    JSON.stringify(output)
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
        input=json.dumps(
            payload
        ),
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

        return json.loads(
            process.stdout
        )

    except Exception as exc:

        raise RuntimeError(
            "Output firma Solana "
            "non valido: "
            + str(exc)
        )


# ============================================================
# PREPARE OFFER
# ============================================================

def prepare_offer(
    asset_id,
    price,
):

    print(
        f"🧾 prepareOffer → "
        f"{eur(price)} "
        f"({price} cents)",
        flush=True,
    )


    # ========================================================
    # IMPORTANTISSIMO
    #
    # IL TUO ENDPOINT HA RISPOSTO:
    #
    # Field is not defined on prepareOfferInput
    #
    # per "type".
    #
    # Quindi la prima richiesta NON usa type.
    # ========================================================

    input_data = {

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "settlementCurrencies": [
            "EUR"
        ],

        "receiveAmount": {

            "amount":
                str(price),

            "currency":
                "EUR",
        },

        "clientMutationId":
            str(uuid.uuid4()),
    }


    print(
        "📤 prepareOffer payload: "
        + json.dumps(
            input_data,
            ensure_ascii=False,
        ),
        flush=True,
    )


    query = """
        mutation PrepareOffer(
            $input:prepareOfferInput!
        ){

            prepareOffer(
                input:$input
            ){

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
    """


    data = graphql(
        query,
        {
            "input":
                input_data
        },
        operation_name="PrepareOffer",
    )


    if not data:

        raise RuntimeError(
            "prepareOffer: "
            "nessuna risposta"
        )


    if data.get("errors"):

        raise RuntimeError(
            "prepareOffer GraphQL error: "
            + json.dumps(
                data["errors"],
                ensure_ascii=False,
            )[:5000]
        )


    prepare = (
        ((data.get("data") or {})
         .get("prepareOffer"))
        or {}
    )


    errors = (
        prepare.get("errors")
        or []
    )


    if errors:

        raise RuntimeError(
            "prepareOffer: "
            + "; ".join(
                str(
                    error.get(
                        "message",
                        ""
                    )
                )
                for error in errors
            )
        )


    authorizations = (
        prepare.get(
            "authorizations"
        )
        or []
    )


    if not authorizations:

        raise RuntimeError(
            "prepareOffer non ha restituito "
            "autorizzazioni"
        )


    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )


    approvals = []


    for index, authorization in enumerate(
        authorizations
    ):

        request = (
            authorization.get(
                "request"
            )
            or {}
        )


        typename = request.get(
            "__typename"
        )


        print(
            f"🔐 Authorization "
            f"{index + 1}/"
            f"{len(authorizations)}: "
            f"{typename}",
            flush=True,
        )


        if typename == (
            "StarkexTransferAuthorizationRequest"
        ):

            approval = sign_starkex(
                authorization
            )


        elif typename == (
            "SolanaTokenTransferAuthorizationRequest"
        ):

            approval = sign_solana(
                authorization
            )


        else:

            raise RuntimeError(
                "Authorization non supportata: "
                + str(typename)
            )


        approvals.append(
            approval
        )


    return approvals


# ============================================================
# CREATE SINGLE SALE OFFER
# ============================================================

def create_offer(
    asset_id,
    price,
    approvals,
):

    # ========================================================
    # CURRENT createSingleSaleOfferInput
    #
    # Non inviamo settlementCurrencies.
    # ========================================================

    input_data = {

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "assetId":
            asset_id,

        "receiveAmount": {

            "amount":
                str(price),

            "currency":
                "EUR",
        },

        "clientMutationId":
            str(uuid.uuid4()),
    }


    print(
        "📤 createSingleSaleOffer "
        "→ invio offerta",
        flush=True,
    )


    data = graphql(
        """
        mutation CreateSingleSaleOffer(
            $input:createSingleSaleOfferInput!
        ){

            createSingleSaleOffer(
                input:$input
            ){

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
        {
            "input":
                input_data
        },
        operation_name="CreateSingleSaleOffer",
    )


    if not data:

        raise RuntimeError(
            "createSingleSaleOffer: "
            "nessuna risposta"
        )


    if data.get("errors"):

        raise RuntimeError(
            "createSingleSaleOffer "
            "GraphQL error: "
            + json.dumps(
                data["errors"],
                ensure_ascii=False,
            )[:5000]
        )


    result = (
        ((data.get("data") or {})
         .get("createSingleSaleOffer"))
        or {}
    )


    errors = (
        result.get("errors")
        or []
    )


    if errors:

        raise RuntimeError(
            "createSingleSaleOffer: "
            + "; ".join(
                str(
                    error.get(
                        "message",
                        ""
                    )
                )
                for error in errors
            )
        )


    offer = result.get(
        "tokenOffer"
    )


    if not offer:

        raise RuntimeError(
            "createSingleSaleOffer "
            "non ha restituito tokenOffer"
        )


    return offer


# ============================================================
# AUTOSELL
# ============================================================

def autosell(
    card,
    price,
):

    asset_id = card.get(
        "assetId"
    )


    if not asset_id:

        raise RuntimeError(
            "assetId mancante"
        )


    print(
        f"💰 SELL {label(card)} "
        f"→ {eur(price)}",
        flush=True,
    )


    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🟡 DRY_RUN: "
            "nessuna vendita eseguita",
            flush=True,
        )

        return True


    # --------------------------------------------------------
    # PREPARE
    # --------------------------------------------------------

    approvals = prepare_offer(
        asset_id,
        price,
    )


    print(
        f"✍️ Firma completata: "
        f"{len(approvals)} approval",
        flush=True,
    )


    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    offer = create_offer(
        asset_id,
        price,
        approvals,
    )


    print(
        f"✅ OFFER CREATA: "
        f"{offer.get('id')}",
        flush=True,
    )


    if offer.get("startDate"):

        print(
            f"🕐 Start: "
            f"{offer.get('startDate')}",
            flush=True,
        )


    if offer.get("endDate"):

        print(
            f"🕐 End: "
            f"{offer.get('endDate')}",
            flush=True,
        )


    return True


# ============================================================
# WORKER
# ============================================================

def worker():

    global worker_started
    global last_scan


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
        f"MIN: {eur(MIN_PRICE)}",
        flush=True,
    )

    print(
        f"MAX: {eur(MAX_PRICE)}",
        flush=True,
    )

    print(
        f"REQUEST_DELAY: "
        f"{REQUEST_DELAY}s",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )


    if DRY_RUN:

        print(
            "🟡 DRY RUN ATTIVO",
            flush=True,
        )

    else:

        print(
            "🔴 MODALITÀ REALE ATTIVA — "
            "le offerte verranno create",
            flush=True,
        )


    while True:

        try:

            print(
                "========================================",
                flush=True,
            )

            print(
                "🔄 NUOVA SCANSIONE GALLERY",
                flush=True,
            )

            print(
                "========================================",
                flush=True,
            )


            # ------------------------------------------------
            # ACCOUNT
            # ------------------------------------------------

            if not check_account():

                time.sleep(
                    15
                )

                continue


            # ------------------------------------------------
            # GALLERY
            # ------------------------------------------------

            cards = get_gallery()


            if cards is None:

                print(
                    "❌ Gallery non disponibile",
                    flush=True,
                )

                time.sleep(
                    15
                )

                continue


            # ------------------------------------------------
            # LINEUP
            # ------------------------------------------------

            lineup = get_lineup()


            if lineup is None:

                print(
                    "❌ Impossibile "
                    "verificare lineup",
                    flush=True,
                )

                time.sleep(
                    15
                )

                continue


            print(
                f"🔎 Carte da analizzare: "
                f"{len(cards)}",
                flush=True,
            )


            sell_candidates = 0
            already_listed = 0
            rejected = 0


            # ------------------------------------------------
            # CARDS
            # ------------------------------------------------

            for card in cards:

                try:

                    # ----------------------------------------
                    # GIÀ IN VENDITA
                    # ----------------------------------------

                    if card.get(
                        "liveSingleSaleOffer"
                    ):

                        already_listed += 1

                        print(
                            f"⏭️ {label(card)} "
                            f"→ GIÀ IN VENDITA",
                            flush=True,
                        )

                        continue


                    # ----------------------------------------
                    # VALIDATION
                    # ----------------------------------------

                    ok, reason, price = validate(
                        card,
                        lineup,
                    )


                    if not ok:

                        rejected += 1

                        reject(
                            card,
                            reason,
                            price,
                        )

                        continue


                    # ----------------------------------------
                    # SELLABLE
                    # ----------------------------------------

                    sell_candidates += 1


                    print(
                        f"✅ {label(card)} "
                        f"→ VENDIBILE "
                        f"{eur(price)}",
                        flush=True,
                    )


                    # ----------------------------------------
                    # SELL
                    # ----------------------------------------

                    try:

                        autosell(
                            card,
                            price,
                        )

                    except Exception as exc:

                        print(
                            f"🔴 AutoSell fallito "
                            f"{label(card)}: "
                            f"{exc}",
                            flush=True,
                        )


                except Exception as exc:

                    print(
                        f"❌ Carta "
                        f"{label(card)}: "
                        f"{exc}",
                        flush=True,
                    )


            last_scan = int(
                time.time()
            )


            print(
                "========================================",
                flush=True,
            )

            print(
                f"🏁 SCANSIONE COMPLETATA",
                flush=True,
            )

            print(
                f"📦 Limited analizzate: "
                f"{len(cards)}",
                flush=True,
            )

            print(
                f"⏭️ Già in vendita: "
                f"{already_listed}",
                flush=True,
            )

            print(
                f"🚫 Rifiutate: "
                f"{rejected}",
                flush=True,
            )

            print(
                f"💰 Candidate vendita: "
                f"{sell_candidates}",
                flush=True,
            )

            print(
                "========================================",
                flush=True,
            )


        except Exception as exc:

            print(
                f"🔥 Worker: {exc}",
                flush=True,
            )


        time.sleep(
            SCAN_DELAY
        )


# ============================================================
# HTTP
# ============================================================

@app.get("/")
def home():

    return jsonify({

        "status":
            "ok",

        "bot":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "min_price":
            MIN_PRICE,

        "min_price_eur":
            eur(MIN_PRICE),

        "max_price":
            MAX_PRICE,

        "max_price_eur":
            eur(MAX_PRICE),

        "last_scan":
            last_scan,

    })


@app.get("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot":
            BOT_VERSION,

        "dry_run":
            DRY_RUN,

        "min_price":
            MIN_PRICE,

        "max_price":
            MAX_PRICE,

        "last_scan":
            last_scan,

    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )


    app.run(
        host="0.0.0.0",
        port=port,
    )
