import os
import time
import uuid
import json
import subprocess
import threading
import requests

from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    ""
).strip()

AUD = os.getenv(
    "SORARE_JWT_AUD",
    ""
).strip()

API_KEY = os.getenv(
    "SORARE_API_KEY",
    ""
).strip()


# Private key Sorare / StarkEx.
#
# ATTENZIONE:
# questa chiave NON deve mai essere stampata nei log.
#
PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# DRY RUN
# ============================================================
#
# Come richiesto:
#
# DRY_RUN=false
#
# significa che le vendite sono REALI.
#
# Puoi comunque sovrascriverlo da Render con:
#
# DRY_RUN=true
#
# ============================================================

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# PREZZI
# ============================================================
#
# EUR in CENTESIMI.
#
# 32 = €0,32
# 70 = €0,70
#
# ============================================================

MIN_PRICE = 32
MAX_PRICE = 70

MIN_LIVE_LISTINGS = 5


# ============================================================
# BOT
# ============================================================

LISTING_DURATION = 7 * 24 * 60 * 60

INTERVAL = 60

TIMEOUT = 30

BOT_VERSION = (
    "32.0-EUR-CENTS-RATELIMIT-PREPARE-FIX"
)


# ============================================================
# RATE LIMIT
# ============================================================
#
# Sorare documenta:
#
# JWT/OAuth: 60 richieste/minuto
# API Key:   200 richieste/minuto
#
# Manteniamo un intervallo minimo tra le richieste.
#
# Se usi APIKEY:
#   0.35s circa
#
# Se usi solo JWT:
#   1.10s circa
#
# ============================================================

if API_KEY:
    REQUEST_DELAY = 0.40
else:
    REQUEST_DELAY = 1.10


request_lock = threading.Lock()

last_request_time = 0.0

rate_limit_until = 0.0

rate_limit_lock = threading.Lock()


# ============================================================
# CACHE
# ============================================================

floor_cache = {}

floor_cache_lock = threading.Lock()

FLOOR_CACHE_TTL = 60


account_cache = None

account_cache_time = 0

ACCOUNT_CACHE_TTL = 300


lineup_cache = None

lineup_cache_time = 0

LINEUP_CACHE_TTL = 60


# ============================================================
# STATE
# ============================================================

worker_started = False

worker_lock = threading.Lock()

usd_rate = None

usd_time = 0


# ============================================================
# UTILITY
# ============================================================

def norm(value):

    return str(
        value or ""
    ).strip().lower()


def label(card):

    parts = [
        card.get("name")
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


def eur(cents):

    if cents is None:
        return "N/D"


    try:

        cents = int(cents)

    except Exception:

        return "N/D"


    return f"€{cents / 100:.2f}"


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

        "Authorization":
            token,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            f"Sorare-AutoSell/{BOT_VERSION}",
    }


    if AUD:

        result["JWT-AUD"] = AUD


    if API_KEY:

        result["APIKEY"] = API_KEY


    return result


# ============================================================
# RATE LIMIT
# ============================================================

def wait_before_request():

    global last_request_time


    with request_lock:

        now = time.time()

        wait = (
            REQUEST_DELAY
            - (now - last_request_time)
        )


        if wait > 0:

            time.sleep(wait)


        last_request_time = time.time()


def activate_rate_limit(seconds):

    global rate_limit_until


    seconds = max(
        1,
        int(seconds),
    )


    with rate_limit_lock:

        rate_limit_until = max(
            rate_limit_until,
            time.time() + seconds,
        )


    print(
        f"⏳ Rate-limit globale: "
        f"pausa {seconds}s",
        flush=True,
    )


def rate_limit_active():

    with rate_limit_lock:

        return (
            time.time()
            < rate_limit_until
        )


def rate_limit_remaining():

    with rate_limit_lock:

        remaining = (
            rate_limit_until
            - time.time()
        )


    return max(
        0,
        int(remaining),
    )


# ============================================================
# GRAPHQL
# ============================================================

def graphql(
    query,
    variables=None,
    operation_name=None,
    retries=2,
):

    attempt = 0


    while attempt <= retries:

        if rate_limit_active():

            remaining = rate_limit_remaining()

            print(
                f"⏸️ API in pausa "
                f"per rate-limit "
                f"({remaining}s)",
                flush=True,
            )

            time.sleep(
                max(1, remaining)
            )


        wait_before_request()


        try:

            payload = {

                "query":
                    query,

                "variables":
                    variables or {},
            }


            if operation_name:

                payload[
                    "operationName"
                ] = operation_name


            response = requests.post(

                URL,

                json=payload,

                headers=headers(),

                timeout=TIMEOUT,
            )


            print(
                f"🌐 HTTP "
                f"{response.status_code}",
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

                    wait = 30


                # Evitiamo attese esagerate,
                # ma rispettiamo Retry-After.

                wait = max(
                    5,
                    min(
                        wait,
                        120,
                    ),
                )


                activate_rate_limit(
                    wait
                )


                attempt += 1

                if attempt > retries:

                    print(
                        "❌ 429 persistente "
                        "dopo i retry",
                        flush=True,
                    )

                    return None


                continue


            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    "❌ HTTP:",
                    response.text[:2000],
                    flush=True,
                )


                attempt += 1

                if attempt > retries:
                    return None


                time.sleep(
                    min(
                        10,
                        attempt * 2,
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
                    response.text[:2000],
                    flush=True,
                )


                attempt += 1

                continue


            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False,
                    )[:4000],
                    flush=True,
                )


            return data


        except requests.RequestException as exc:

            print(
                f"❌ HTTP exception: "
                f"{exc}",
                flush=True,
            )


            attempt += 1


            if attempt > retries:
                return None


            time.sleep(
                min(
                    10,
                    attempt * 2,
                )
            )


        except Exception as exc:

            print(
                f"❌ GraphQL exception: "
                f"{exc}",
                flush=True,
            )


            attempt += 1


            if attempt > retries:
                return None


            time.sleep(
                min(
                    10,
                    attempt * 2,
                )
            )


    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    global account_cache
    global account_cache_time


    now = time.time()


    if (
        account_cache
        and
        now - account_cache_time
        < ACCOUNT_CACHE_TTL
    ):

        user = account_cache

    else:

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
                "❌ Account Sorare "
                "non verificato",
                flush=True,
            )

            return False


        account_cache = user

        account_cache_time = now


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

    total = 0

    sealed = 0


    while True:

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


        if not data or data.get(
            "errors"
        ):

            return None


        result = (

            ((data.get("data") or {})

             .get("currentUser") or {})

            .get("cards")

            or {}
        )


        nodes = result.get(
            "nodes"
        ) or []


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


    cards = [

        card

        for card in cards

        if norm(
            card.get("rarityTyped")
        ) == "limited"
    ]


    print(
        f"📦 Gallery: {total} | "
        f"🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(cards)}",
        flush=True,
    )


    return cards


# ============================================================
# LINEUP
# ============================================================

def get_lineup():

    global lineup_cache
    global lineup_cache_time


    now = time.time()


    if (
        lineup_cache is not None
        and
        now - lineup_cache_time
        < LINEUP_CACHE_TTL
    ):

        return lineup_cache


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

        .get(
            "blockchainCardsInLineups"
        )
    )


    if not isinstance(
        value,
        list,
    ):

        return None


    lineup_cache = {

        norm(item)

        for item in value

        if item
    }


    lineup_cache_time = now


    return lineup_cache


def identifiers(card):

    result = set()


    asset_id = norm(
        card.get("assetId")
    )

    slug = norm(
        card.get("slug")
    )


    if asset_id:

        result.add(
            asset_id
        )


    if slug:

        result.add(
            slug
        )


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

KSLUG = (
    "sandro-kulenovic-"
    "2025-limited-385"
)


KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4"
    "ac587bd3d40c7136796b6c0ed10ba0a6"
)


KID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()


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
# USD / EUR
# ============================================================

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

                "from":
                    "USD",

                "to":
                    "EUR",
            },

            timeout=10,
        )


        if response.status_code != 200:

            return None


        rate = float(

            response.json()
            ["rates"]
            ["EUR"]
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
    # EUR NATIVO
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
    # USD FALLBACK
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

def floor_cache_key(card):

    player = (
        card.get("anyPlayer")
        or {}
    )


    return (

        norm(
            player.get("slug")
        ),

        norm(
            card.get(
                "rarityTyped"
            )
        ),

        int(
            card.get(
                "seasonYear"
            )
            or 0
        ),
    )


def live_floor(card):

    key = floor_cache_key(
        card
    )


    now = time.time()


    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    with floor_cache_lock:

        cached = floor_cache.get(
            key
        )


    if cached:

        cached_time, cached_value = cached


        if (
            now - cached_time
            < FLOOR_CACHE_TTL
        ):

            print(
                f"📦 FLOOR CACHE "
                f"{label(card)} "
                f"→ {eur(cached_value)}",
                flush=True,
            )

            return cached_value


    player = (
        card.get("anyPlayer")
        or {}
    )


    player_slug = norm(
        player.get("slug")
    )


    rarity = norm(
        card.get(
            "rarityTyped"
        )
    )


    try:

        season = int(
            card.get(
                "seasonYear"
            )
        )

    except Exception:

        return None


    if not player_slug:

        return None


    if rarity != "limited":

        return None


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
            "playerSlug":
                player_slug,

            "first":
                50,
        },

        operation_name="LiveFloor",
    )


    if not data or data.get(
        "errors"
    ):

        return None


    offers = (

        (((data.get("data") or {})

          .get("tokens") or {})

         .get(
             "liveSingleSaleOffers"
         )

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

                prices.append(
                    price
                )


            break


    if len(prices) < MIN_LIVE_LISTINGS:

        print(

            f"⚠️ {label(card)}: "
            f"solo {len(prices)} "
            f"listing comparabili "
            f"(minimo "
            f"{MIN_LIVE_LISTINGS})",

            flush=True,
        )


        with floor_cache_lock:

            floor_cache[key] = (
                now,
                None,
            )


        return None


    floor = min(
        prices
    )


    with floor_cache_lock:

        floor_cache[key] = (
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

def validate(
    card,
    lineup,
):

    # --------------------------------------------------------
    # VAULT
    # --------------------------------------------------------

    if card.get("sealed"):

        return (
            False,
            "VAULT",
            None,
        )


    # --------------------------------------------------------
    # KULENOVIC
    # --------------------------------------------------------

    if is_kulenovic(card):

        return (
            False,
            "KULENOVIC",
            None,
        )


    # --------------------------------------------------------
    # RARITY
    # --------------------------------------------------------

    if norm(
        card.get("rarityTyped")
    ) != "limited":

        return (
            False,
            "RARITY",
            None,
        )


    # --------------------------------------------------------
    # LINEUP
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    floor = live_floor(
        card
    )


    if floor is None:

        return (
            False,
            "PRICE_UNKNOWN",
            None,
        )


    # --------------------------------------------------------
    # MIN
    # --------------------------------------------------------

    if floor < MIN_PRICE:

        return (
            False,
            "PRICE_LOW",
            floor,
        )


    # --------------------------------------------------------
    # MAX
    # --------------------------------------------------------

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

    for directory in os.getenv(
        "PATH",
        "",
    ).split(os.pathsep):

        executable = os.path.join(
            directory,
            "node",
        )


        if (

            os.path.isfile(
                executable
            )

            and

            os.access(
                executable,
                os.X_OK
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
        Number(request.nonce),

      expirationTimestamp:
        Number(
          request.expirationTimestamp
        ),

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

            or
            "Firma StarkEx fallita"
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

            + " | OUTPUT="

            + process.stdout[:1000]
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


    supported = {

        "SolanaTokenTransferAuthorizationRequest",

        "StarkexV6TokenTransferAuthorizationRequest",
    }


    if typename not in supported:

        raise RuntimeError(

            "Authorization Solana "
            "non supportata: "

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
const crypto = require("crypto");
const fs = require("fs");

const {
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair,
  createSignableMessage,
  getBase58Decoder,
  getBase58Encoder
} = require("@solana/kit");

const {
  HDKey
} = require(
  "micro-key-producer/slip10.js"
);


async function main() {

  const input = JSON.parse(
    fs.readFileSync(0, "utf8")
  );


  const authorization =
    input.authorization;


  const request =
    authorization.request;


  const privateKeyHex =
    input.privateKey
      .replace(/^0x/i, "");


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


  /*
   * Sorare:
   *
   * Ethereum private key
   *        ↓
   * SLIP-0010
   *        ↓
   * m/44'/501'/0'/0'
   *        ↓
   * Ed25519 Solana key
   */

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive(
        "m/44'/501'/0'/0'"
      );


  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );


  const signer =
    await createSignerFromKeyPair(
      keyPair
    );


  /*
   * Messaggio ESATTO richiesto
   * da Sorare.
   */

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
      .nonce,

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


  const messageBytes =
    new TextEncoder().encode(
      message
    );


  /*
   * Sorare richiede:
   *
   * SHA-256(message)
   *        ↓
   * Ed25519 sign
   *        ↓
   * Base58
   */

  const hash =
    await crypto.webcrypto.subtle.digest(
      "SHA-256",
      messageBytes
    );


  const signable =
    createSignableMessage(
      new Uint8Array(hash)
    );


  const result =
    await signer.signMessages(
      [signable]
    );


  /*
   * Il risultato è bytes.
   *
   * GraphQL richiede invece:
   *
   * signature: String!
   *
   * quindi Base58.
   */

  const signatureBytes =
    result[0][signer.address];


  const signature =
    getBase58Encoder().encode(
      signatureBytes
    );


  /*
   * Controllo opzionale:
   *
   * la signature deve essere una
   * stringa Base58.
   */

  if (
    typeof signature !==
    "string"
  ) {

    throw new Error(
      "Firma Solana non Base58"
    );

  }


  const output = {

    fingerprint:
      authorization.fingerprint,

    solanaTokenTransferApproval: {

      signature,

      nonce:
        request.nonce,

      expirationTimestamp:
        Number(
          request.expirationTimestamp
        )
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

            or
            "Firma Solana fallita"
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

            + " | OUTPUT="

            + process.stdout[:1000]
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
    # Lo schema GraphQL LIVE attuale di Sorare
    # NON contiene "type" dentro prepareOfferInput.
    #
    # Quindi NON inviare:
    #
    # "type": "SINGLE_SALE_OFFER"
    #
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
            {
                "sendAssetIds":
                    input_data[
                        "sendAssetIds"
                    ],

                "receiveAssetIds":
                    [],

                "settlementCurrencies":
                    ["EUR"],

                "receiveAmount":
                    input_data[
                        "receiveAmount"
                    ],
            },

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


                        ... on StarkexV6TokenTransferAuthorizationRequest {

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

        operation_name=
            "PrepareOffer",
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
            )[:4000]
        )


    prepare = (

        ((data.get("data") or {})

         .get("prepareOffer"))

        or {}
    )


    errors = (

        prepare.get(
            "errors"
        )

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
            "autorizzazioni."
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


        elif typename in (

            "SolanaTokenTransferAuthorizationRequest",

            "StarkexV6TokenTransferAuthorizationRequest",

        ):

            approval = sign_solana(
                authorization
            )


        else:

            raise RuntimeError(

                "Authorization non "
                "supportata: "

                + str(typename)
            )


        approvals.append(
            approval
        )


    return approvals


# ============================================================
# CREATE OFFER
# ============================================================

def create_offer(
    asset_id,
    price,
    approvals,
):

    print(
        "📤 createSingleSaleOffer...",
        flush=True,
    )


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

        "settlementCurrencies": [

            "EUR"
        ],

        "duration":
            LISTING_DURATION,

        "clientMutationId":
            str(uuid.uuid4()),
    }


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

        operation_name=
            "CreateSingleSaleOffer",
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
            )[:4000]
        )


    result = (

        ((data.get("data") or {})

         .get(
             "createSingleSaleOffer"
         ))

        or {}
    )


    errors = (

        result.get(
            "errors"
        )

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
            "non ha restituito "
            "tokenOffer"
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

            "🟡 DRY_RUN=true: "
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

            f"📅 Start: "
            f"{offer.get('startDate')}",

            flush=True,
        )


    if offer.get("endDate"):

        print(

            f"📅 End: "
            f"{offer.get('endDate')}",

            flush=True,
        )


    return True


# ============================================================
# WORKER
# ============================================================

def worker():

    global worker_started


    with worker_lock:

        if worker_started:

            return

        worker_started = True


    print(

        f"🚀 AutoSell {BOT_VERSION} "
        f"| DRY_RUN={DRY_RUN} "
        f"| MIN={eur(MIN_PRICE)} "
        f"| MAX={eur(MAX_PRICE)} "
        f"| REQUEST_DELAY={REQUEST_DELAY}s",

        flush=True,
    )


    if DRY_RUN:

        print(

            "🟡 MODALITÀ DRY RUN "
            "ATTIVA",

            flush=True,
        )

    else:

        print(

            "🔴 MODALITÀ REALE ATTIVA "
            "— le offerte verranno create",

            flush=True,
        )


    while True:

        try:

            # ------------------------------------------------
            # ACCOUNT
            # ------------------------------------------------

            if not check_account():

                time.sleep(
                    INTERVAL
                )

                continue


            # ------------------------------------------------
            # GALLERY
            # ------------------------------------------------

            cards = get_gallery()


            if cards is None:

                time.sleep(
                    INTERVAL
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
                    INTERVAL
                )

                continue


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

                        continue


                    # ----------------------------------------
                    # VALIDAZIONE
                    # ----------------------------------------

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


                    # ----------------------------------------
                    # VENDIBILE
                    # ----------------------------------------

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

                            f"🔴 AutoSell fallito: "
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


        except Exception as exc:

            print(

                f"🔥 Worker: "
                f"{exc}",

                flush=True,
            )


        # ----------------------------------------------------
        # CICLO
        # ----------------------------------------------------

        print(

            f"⏱️ Prossimo ciclo "
            f"tra {INTERVAL}s",

            flush=True,
        )


        time.sleep(
            INTERVAL
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

        "request_delay":
            REQUEST_DELAY,

        "api_key":
            bool(API_KEY),
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

        "rate_limit_active":
            rate_limit_active(),
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
