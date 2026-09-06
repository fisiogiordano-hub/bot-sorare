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

# Private key esportata dal wallet Sorare.
#
# IMPORTANTE:
# NON deve essere una Solana private key diversa.
# Deve essere la private key Sorare/Ethereum esportata
# dal wallet Sorare.
PRIVATE_KEY = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()


# ============================================================
# MODALITÀ
# ============================================================

# COME RICHIESTO:
# vendita reale attiva.
DRY_RUN = False


# ============================================================
# PREZZI
# ============================================================

# Sorare EUR cents:
#
# 32 = €0,32
# 40 = €0,40
# 70 = €0,70

MIN_PRICE = 32
MAX_PRICE = 70


# Numero minimo di listing comparabili
# necessari per considerare affidabile il floor.
MIN_LIVE_LISTINGS = 5


# ============================================================
# BOT
# ============================================================

LISTING_DURATION = 7 * 24 * 60 * 60

INTERVAL = 15

TIMEOUT = 30

REQUEST_DELAY = 1.5

BOT_VERSION = (
    "34.0-EUR-CENTS-SOLANA-OFFICIAL-GALLERY-FIX"
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
    ""
).strip()


# ============================================================
# STATE
# ============================================================

worker_started = False

worker_lock = threading.Lock()

last_scan_time = 0

last_scan_cards = 0

last_scan_error = None

last_sale = None


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

    if card.get("seasonYear") is not None:

        parts.append(
            str(card["seasonYear"])
        )

    if card.get("rarityTyped"):

        parts.append(
            str(card["rarityTyped"])
        )

    if card.get("serialNumber") is not None:

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

        "Authorization": token,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            f"Sorare-AutoSell/{BOT_VERSION}",
    }

    if AUD:

        result["JWT-AUD"] = AUD

    return result


# ============================================================
# GRAPHQL
# ============================================================

def graphql(
    query,
    variables=None,
):

    for attempt in range(4):

        try:

            response = requests.post(

                URL,

                json={
                    "query": query,
                    "variables":
                        variables or {},
                },

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

                    wait = (
                        3 + attempt * 3
                    )

                wait = min(
                    max(wait, 2),
                    30,
                )

                print(
                    f"⏳ Rate limit: "
                    f"attendo {wait}s",
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
                    response.text[:2000],
                    flush=True,
                )

                time.sleep(
                    2 + attempt
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

                time.sleep(
                    2 + attempt
                )

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


        except Exception as exc:

            print(
                f"❌ GraphQL exception: {exc}",
                flush=True,
            )

            time.sleep(
                2 + attempt
            )


    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql(
        """
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
        """
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

    total = 0

    sealed = 0

    pages = 0


    while True:

        pages += 1

        print(
            f"🔎 Gallery page {pages}...",
            flush=True,
        )


        data = graphql(
            """
            query(
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
        )


        if not data:

            print(
                "❌ Gallery: nessuna risposta",
                flush=True,
            )

            return None


        if data.get("errors"):

            print(
                "❌ Gallery GraphQL error",
                flush=True,
            )

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


        print(
            f"📄 Gallery page {pages}: "
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
            result.get(
                "pageInfo"
            )
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


        time.sleep(
            REQUEST_DELAY
        )


    # --------------------------------------------------------
    # SOLO LIMITED
    # --------------------------------------------------------

    cards = [

        card

        for card in cards

        if norm(
            card.get(
                "rarityTyped"
            )
        ) == "limited"
    ]


    print(
        f"📦 Gallery COMPLETA: "
        f"{total} totali | "
        f"🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(cards)}",
        flush=True,
    )


    global last_scan_time
    global last_scan_cards

    last_scan_time = time.time()

    last_scan_cards = len(cards)


    return cards


# ============================================================
# LINEUP
# ============================================================

def get_lineup():

    data = graphql(
        """
        query {

            currentUser {

                blockchainCardsInLineups(
                    sport:FOOTBALL
                )
            }
        }
        """
    )


    if not data:

        return None


    if data.get("errors"):

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


    lineup = {

        norm(item)

        for item in value

        if item
    }


    print(
        f"📋 Lineup: {len(lineup)} "
        f"identificativi",
        flush=True,
    )


    return lineup


# ============================================================
# IDENTIFIERS
# ============================================================

def identifiers(card):

    result = set()


    asset_id = norm(
        card.get(
            "assetId"
        )
    )


    slug = norm(
        card.get(
            "slug"
        )
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


def in_lineup(
    card,
    lineup,
):

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
                "from": "USD",
                "to": "EUR",
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
    # USD fallback
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
        card.get(
            "anyPlayer"
        )
        or {}
    )


    player_slug = norm(
        player.get(
            "slug"
        )
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
        query(
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
    )


    if not data:

        return None


    if data.get("errors"):

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

                market_season
                == season
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
            f"solo {len(prices)} listing "
            f"comparabili "
            f"(minimo {MIN_LIVE_LISTINGS})",
            flush=True,
        )

        return None


    floor = min(prices)


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
        card.get(
            "rarityTyped"
        )
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
        f"🚫 {label(card)} → {msg}",
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
                os.X_OK,
            )
        ):

            return executable


    return None


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


    # --------------------------------------------------------
    # QUESTO È L'ESEMPIO UFFICIALE SORARE:
    #
    # private key Sorare
    #       ↓
    # SLIP-0010
    #       ↓
    # m/44'/501'/0'/0'
    #       ↓
    # ed25519
    #
    # Il senderAddress deve essere IDENTICO.
    # --------------------------------------------------------

    script = r"""
const fs = require("fs");
const crypto = require("crypto");

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


  const authorization =
    input.authorization;


  const request =
    authorization.request;


  if (
    request.__typename !==
    "SolanaTokenTransferAuthorizationRequest"
  ) {

    throw new Error(
      "Authorization non Solana: "
      + request.__typename
    );

  }


  // --------------------------------------------------------
  // PRIVATE KEY SORARE
  // --------------------------------------------------------

  const privateKeyHex =
    String(input.privateKey)
      .replace(/^0x/i, "")
      .trim();


  if (!/^[0-9a-fA-F]+$/.test(privateKeyHex)) {

    throw new Error(
      "SORARE_STARK_PRIVATE_KEY "
      + "non è una private key hex valida"
    );

  }


  if (privateKeyHex.length !== 64) {

    throw new Error(
      "SORARE_STARK_PRIVATE_KEY deve "
      + "contenere 32 byte / 64 caratteri hex"
    );

  }


  const seed =
    Buffer.from(
      privateKeyHex,
      "hex"
    );


  // --------------------------------------------------------
  // SLIP-0010
  // PATH UFFICIALE SORARE
  // --------------------------------------------------------

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive(
        "m/44'/501'/0'/0'"
      );


  if (
    !derived.privateKey
    ||
    derived.privateKey.length !== 32
  ) {

    throw new Error(
      "Derivazione Solana non valida"
    );

  }


  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );


  const signer =
    await createSignerFromKeyPair(
      keyPair
    );


  // --------------------------------------------------------
  // VERIFICA CRITICA
  // --------------------------------------------------------

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


  if (
    derivedAddress !==
    senderAddress
  ) {

    throw new Error(
      "SOLANA ADDRESS MISMATCH: "
      + "la SORARE private key configurata "
      + "non corrisponde al senderAddress "
      + "della authorization"
    );

  }


  // --------------------------------------------------------
  // MESSAGE UFFICIALE SORARE
  //
  // NON aggiungere assetId.
  // NON aggiungere senderAddress.
  // '0x' è letterale.
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

    String(
      request.nonce
    ),

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
  // SHA-256
  // --------------------------------------------------------

  const messageBytes =
    new TextEncoder().encode(
      message
    );


  const messageHash =
    await crypto.webcrypto.subtle.digest(
      "SHA-256",
      messageBytes
    );


  // --------------------------------------------------------
  // SIGN SHA-256 HASH
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


  const signature =
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

      signature,

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
            + " | OUTPUT="
            + process.stdout[:2000]
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


    input_data = {

        "type":
            "SINGLE_SALE_OFFER",

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

        mutation(
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
            "SolanaTokenTransferAuthorizationRequest"
        ):

            approval = sign_solana(
                authorization
            )


        elif typename == (
            "StarkexTransferAuthorizationRequest"
        ):

            raise RuntimeError(
                "La carta richiede "
                "StarkEx, ma questa versione "
                "gestisce intenzionalmente "
                "la firma Solana solo per "
                "asset Solana."
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
# CREATE OFFER
# ============================================================

def create_offer(
    asset_id,
    price,
    approvals,
):

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
        "📤 createSingleSaleOffer...",
        flush=True,
    )


    data = graphql(
        """
        mutation(
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
    # PREPARE + SIGN
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


    global last_sale

    last_sale = {

        "card":
            label(card),

        "assetId":
            asset_id,

        "price":
            price,

        "priceEur":
            eur(price),

        "offerId":
            offer.get("id"),

        "timestamp":
            int(time.time()),
    }


    return True


# ============================================================
# SCAN
# ============================================================

def scan_once():

    global last_scan_error

    print(
        "",
        flush=True,
    )

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


    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():

        raise RuntimeError(
            "Account non verificabile"
        )


    time.sleep(
        REQUEST_DELAY
    )


    # --------------------------------------------------------
    # GALLERY
    # --------------------------------------------------------

    cards = get_gallery()


    if cards is None:

        raise RuntimeError(
            "Gallery non verificabile"
        )


    # --------------------------------------------------------
    # LINEUP
    # --------------------------------------------------------

    time.sleep(
        REQUEST_DELAY
    )


    lineup = get_lineup()


    if lineup is None:

        raise RuntimeError(
            "Lineup non verificabile"
        )


    print(
        f"🎯 Controllo {len(cards)} "
        f"carte Limited...",
        flush=True,
    )


    candidates = 0

    sold = 0


    # --------------------------------------------------------
    # CARDS
    # --------------------------------------------------------

    for card in cards:

        try:

            # ----------------------------------------------
            # GIÀ IN VENDITA
            # ----------------------------------------------

            if card.get(
                "liveSingleSaleOffer"
            ):

                print(
                    f"⏭️ {label(card)} "
                    f"→ GIÀ IN VENDITA",
                    flush=True,
                )

                continue


            # ----------------------------------------------
            # VALIDAZIONE
            # ----------------------------------------------

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

                time.sleep(
                    REQUEST_DELAY
                )

                continue


            candidates += 1


            # ----------------------------------------------
            # VENDIBILE
            # ----------------------------------------------

            print(
                f"✅ {label(card)} "
                f"→ VENDIBILE "
                f"{eur(price)}",
                flush=True,
            )


            # ----------------------------------------------
            # VENDITA REALE
            # ----------------------------------------------

            try:

                autosell(
                    card,
                    price,
                )

                sold += 1

            except Exception as exc:

                print(
                    f"🔴 AutoSell fallito: "
                    f"{exc}",
                    flush=True,
                )


            time.sleep(
                REQUEST_DELAY
            )


        except Exception as exc:

            print(
                f"❌ Carta "
                f"{label(card)}: "
                f"{exc}",
                flush=True,
            )


            time.sleep(
                REQUEST_DELAY
            )


    print(
        "========================================",
        flush=True,
    )

    print(
        f"🏁 SCANSIONE TERMINATA | "
        f"Limited={len(cards)} | "
        f"Vendibili={candidates} | "
        f"Vendute={sold}",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )


    last_scan_error = None


# ============================================================
# WORKER
# ============================================================

def worker():

    global worker_started
    global last_scan_error


    with worker_lock:

        if worker_started:

            print(
                "⚠️ Worker già avviato",
                flush=True,
            )

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
            "🟡 MODALITÀ DRY RUN",
            flush=True,
        )

    else:

        print(
            "🔴 MODALITÀ REALE ATTIVA "
            "— le offerte verranno create",
            flush=True,
        )


    # --------------------------------------------------------
    # CONTROLLO CONFIG
    # --------------------------------------------------------

    if not TOKEN:

        print(
            "🔴 ERRORE: "
            "SORARE_JWT_TOKEN mancante",
            flush=True,
        )


    if not PRIVATE_KEY:

        print(
            "🔴 ERRORE: "
            "SORARE_STARK_PRIVATE_KEY mancante",
            flush=True,
        )


    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    while True:

        try:

            scan_once()


        except Exception as exc:

            last_scan_error = str(
                exc
            )

            print(
                f"🔥 Worker: {exc}",
                flush=True,
            )


        print(
            f"😴 Prossima scansione "
            f"tra {INTERVAL}s...",
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

        "real_mode":
            not DRY_RUN,

        "min_price":
            MIN_PRICE,

        "min_price_eur":
            eur(MIN_PRICE),

        "max_price":
            MAX_PRICE,

        "max_price_eur":
            eur(MAX_PRICE),

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "last_scan_time":
            last_scan_time,

        "last_scan_cards":
            last_scan_cards,

        "last_scan_error":
            last_scan_error,

        "last_sale":
            last_sale,
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

        "real_mode":
            not DRY_RUN,

        "last_scan_time":
            last_scan_time,

        "last_scan_cards":
            last_scan_cards,

        "last_scan_error":
            last_scan_error,

        "worker_started":
            worker_started,
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

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
        "========================================",
        flush=True,
    )


    threading.Thread(
        target=worker,
        daemon=True,
        name="sorare-autosell-worker",
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
        debug=False,
        threaded=True,
    )
