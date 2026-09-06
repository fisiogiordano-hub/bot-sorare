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

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
PRIVATE_KEY = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# SICUREZZA:
# di default nessuna vendita reale.
DRY_RUN = os.getenv(
    "DRY_RUN",
    "true",
).lower() == "true"


# IMPORTANTE:
# Tutti i prezzi interni sono espressi in CENTESIMI.
#
# €32.00 = 3200
# €70.00 = 7000
#
MIN_PRICE = 3200
MAX_PRICE = 7000

MIN_LIVE_LISTINGS = 5

LISTING_DURATION = 7 * 24 * 60 * 60

INTERVAL = 15
TIMEOUT = 30

BOT_VERSION = "30.2-AUTOSELL-SOLANA-FIX"


KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
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

usd_rate = None
usd_time = 0


# ============================================================
# UTILITY
# ============================================================

def norm(x):
    return str(x or "").strip().lower()


def label(c):

    p = [
        c.get("name")
        or c.get("slug")
        or "Carta"
    ]

    if c.get("seasonYear"):
        p.append(
            str(c["seasonYear"])
        )

    if c.get("rarityTyped"):
        p.append(
            str(c["rarityTyped"])
        )

    if c.get("serialNumber"):
        p.append(
            f"#{c['serialNumber']}"
        )

    return " • ".join(p)


def eur(c):

    if c is None:
        return "N/D"

    return f"€{c / 100:.2f}"


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

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent":
            f"Sorare-AutoSell/{BOT_VERSION}",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

def graphql(
    query,
    variables=None,
):

    for attempt in range(3):

        try:

            r = requests.post(
                URL,
                json={
                    "query": query,
                    "variables": variables or {},
                },
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 HTTP {r.status_code}",
                flush=True,
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if r.status_code == 429:

                try:

                    wait = min(
                        int(
                            r.headers.get(
                                "Retry-After",
                                attempt + 2,
                            )
                        ),
                        15,
                    )

                except Exception:

                    wait = attempt + 2

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

            if r.status_code != 200:

                print(
                    "❌ HTTP:",
                    r.text[:1200],
                    flush=True,
                )

                time.sleep(
                    attempt + 1
                )

                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = r.json()

            except Exception:

                print(
                    "❌ Risposta non JSON:",
                    r.text[:1200],
                    flush=True,
                )

                time.sleep(
                    attempt + 1
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
                        indent=2,
                    )[:5000],
                    flush=True,
                )

            return data

        except Exception as e:

            print(
                f"❌ GraphQL exception: {e}",
                flush=True,
            )

            time.sleep(
                attempt + 1
            )

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    d = graphql(
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

    u = (
        ((d or {}).get("data") or {})
        .get("currentUser")
    )

    if not u:

        print(
            "❌ Account Sorare non verificato",
            flush=True,
        )

        return False

    print(
        f"✅ Account: "
        f"{u.get('nickname') or u.get('slug')}",
        flush=True,
    )

    print(
        "🔐 Stark key account: "
        + (
            "PRESENTE"
            if u.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True,
    )

    return True


# ============================================================
# GALLERY
# ============================================================

def get_gallery():

    out = []

    after = None

    total = 0
    sealed = 0

    while True:

        d = graphql(
            """
            query($first:Int,$after:String){

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
        )

        if not d or d.get("errors"):
            return None

        cards = (
            ((d.get("data") or {})
             .get("currentUser") or {})
            .get("cards") or {}
        )

        nodes = cards.get(
            "nodes"
        ) or []

        total += len(nodes)

        for card in nodes:

            if card.get("sealed"):

                sealed += 1

            else:

                out.append(card)

        page = (
            cards.get("pageInfo")
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

    # Solo LIMITED
    out = [
        c
        for c in out
        if norm(
            c.get("rarityTyped")
        ) == "limited"
    ]

    print(
        f"📦 Gallery: {total} | "
        f"🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(out)}",
        flush=True,
    )

    return out


# ============================================================
# LINEUP
# ============================================================

def get_lineup():

    d = graphql(
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

    if not d or d.get("errors"):
        return None

    value = (
        ((d.get("data") or {})
         .get("currentUser") or {})
        .get("blockchainCardsInLineups")
    )

    if not isinstance(
        value,
        list,
    ):
        return None

    return {
        norm(x)
        for x in value
        if x
    }


def identifiers(c):

    result = set()

    asset_id = norm(
        c.get("assetId")
    )

    slug = norm(
        c.get("slug")
    )

    if asset_id:
        result.add(asset_id)

    if slug:
        result.add(slug)

    return result


def in_lineup(
    c,
    lineup,
):

    if lineup is None:
        return None

    return bool(
        identifiers(c) & lineup
    )


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(c):

    wanted = {
        norm(KSLUG),
        norm(KASSET),
    }

    if KID:

        wanted.add(
            norm(KID)
        )

    return bool(
        identifiers(c) & wanted
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

        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "USD",
                "to": "EUR",
            },
            timeout=10,
        )

        if r.status_code != 200:
            return None

        rate = float(
            r.json()["rates"]["EUR"]
        )

        if rate <= 0:
            return None

        usd_rate = rate
        usd_time = now

        return rate

    except Exception as e:

        print(
            f"⚠️ Cambio USD/EUR "
            f"non disponibile: {e}",
            flush=True,
        )

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

        usd = float(
            amounts.get(
                "usdCents"
            )
        )

    except Exception:

        usd = 0

    if usd <= 0:
        return None

    rate = usd_eur()

    if not rate:
        return None

    return int(
        round(
            usd * rate
        )
    )


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(c):

    player = (
        c.get("anyPlayer")
        or {}
    )

    player_slug = norm(
        player.get("slug")
    )

    rarity = norm(
        c.get("rarityTyped")
    )

    try:

        season = int(
            c.get("seasonYear")
        )

    except Exception:

        return None

    if not player_slug:
        return None

    if rarity != "limited":
        return None

    d = graphql(
        """
        query(
            $playerSlug:String,
            $first:Int
        ){

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

    if not d or d.get("errors"):
        return None

    offers = (
        (((d.get("data") or {})
          .get("tokens") or {})
         .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        )

        for market_card in cards:

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
                ) == player_slug

                and

                norm(
                    market_card.get(
                        "rarityTyped"
                    )
                ) == rarity

                and

                market_season == season
            )

            if not same_card:
                continue

            amount = (
                (offer.get(
                    "receiverSide"
                ) or {})
                .get("amounts")
                or {}
            )

            value = price_eur(
                amount
            )

            if value is not None:
                prices.append(value)

            break

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"⚠️ {label(c)}: "
            f"solo {len(prices)} listing "
            f"comparabili "
            f"(minimo "
            f"{MIN_LIVE_LISTINGS})",
            flush=True,
        )

        return None

    floor = min(prices)

    print(
        f"📊 {label(c)} "
        f"→ {len(prices)} listing "
        f"| FLOOR {eur(floor)}",
        flush=True,
    )

    return floor


# ============================================================
# VALIDATION
# ============================================================

def validate(
    c,
    lineup,
):

    if c.get("sealed"):
        return False, "VAULT", None

    if is_kulenovic(c):
        return False, "KULENOVIC", None

    if norm(
        c.get("rarityTyped")
    ) != "limited":

        return False, "RARITY", None

    lineup_state = in_lineup(
        c,
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

    floor = live_floor(c)

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

    return True, "OK", floor


def reject(
    c,
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
        f"🚫 {label(c)} → {msg}",
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

        key
        for key in required
        if request.get(key) is None
    ]

    if missing:

        raise RuntimeError(
            "Authorization incompleta, "
            "mancano: "
            + ", ".join(missing)
        )

    script = r'''
const crypto = require("crypto");

const {
  createKeyPairFromPrivateKeyBytes,
  getAddressFromPublicKey,
  signBytes,
  getBase58Decoder
} = require("@solana/kit");

const { HDKey } =
  require("micro-key-producer/slip10.js");


async function main() {

  const input = JSON.parse(
    require("fs").readFileSync(
      0,
      "utf8"
    )
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
      "Authorization non Solana: " +
      request.__typename
    );
  }

  const privateKeyHex =
    input.privateKey.replace(
      /^0x/i,
      ""
    );

  if (
    !/^[0-9a-fA-F]+$/.test(
      privateKeyHex
    )
  ) {

    throw new Error(
      "Private key non esadecimale"
    );
  }

  const seed =
    Buffer.from(
      privateKeyHex,
      "hex"
    );

  if (seed.length === 0) {

    throw new Error(
      "Private key vuota"
    );
  }

  /*
   * Derivazione Solana:
   *
   * Ethereum private key
   *        ↓
   * SLIP-0010
   *        ↓
   * m/44'/501'/0'/0'
   *        ↓
   * Ed25519
   */

  const derived =
    HDKey
      .fromMasterSeed(seed)
      .derive(
        "m/44'/501'/0'/0'"
      );

  if (!derived.privateKey) {

    throw new Error(
      "Derivazione private key fallita"
    );
  }

  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );

  /*
   * L'indirizzo viene ricavato
   * dalla public key.
   */

  const derivedAddress =
    await getAddressFromPublicKey(
      keyPair.publicKey
    );

  const expectedAddress =
    request.senderAddress;

  if (!expectedAddress) {

    throw new Error(
      "senderAddress non presente "
      + "nell'Authorization"
    );
  }

  console.error(
    "🔑 Derived Solana address:",
    derivedAddress
  );

  console.error(
    "🎯 Sorare senderAddress:",
    expectedAddress
  );

  if (
    derivedAddress !==
    expectedAddress
  ) {

    throw new Error(
      "senderAddress mismatch: " +
      derivedAddress +
      " != " +
      expectedAddress
    );
  }

  /*
   * Messaggio Sorare.
   */

  const message = [

    "TRANSFER",
    request.transferProxyProgramAddress,
    request.merkleTreeAddress,
    request.leafIndex.toString(),
    request.nonce,
    request.expirationTimestamp.toString(),
    request.receiverAddress,
    "0x",
    request.originator

  ].join(":");


  console.error(
    "📝 Solana message:",
    message
  );


  /*
   * SHA-256 del messaggio UTF-8.
   */

  const hash =
    await crypto.webcrypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(
        message
      )
    );


  const signatureBytes =
    await signBytes(
      keyPair.privateKey,
      new Uint8Array(hash)
    );


  const signature =
    getBase58Decoder().decode(
      signatureBytes
    );


  const result = {

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
'''

    payload = {
        "authorization":
            authorization,

        "privateKey":
            PRIVATE_KEY,
    }

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

    except Exception as e:

        raise RuntimeError(
            "Output firma Solana "
            "non valido: "
            + str(e)
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

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    if not isinstance(
        price,
        int,
    ):

        raise RuntimeError(
            "Prezzo non intero"
        )

    if price < MIN_PRICE:

        raise RuntimeError(
            f"Prezzo {eur(price)} "
            f"sotto MIN_PRICE {eur(MIN_PRICE)}"
        )

    if price > MAX_PRICE:

        raise RuntimeError(
            f"Prezzo {eur(price)} "
            f"sopra MAX_PRICE {eur(MAX_PRICE)}"
        )

    # --------------------------------------------------------
    # INPUT
    # --------------------------------------------------------

    inp = {

        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "receiveAmount": {

            "amount":
                str(price),

            "currency":
                "EUR",
        },

        "settlementCurrencies": [
            "EUR"
        ],

        "clientMutationId":
            str(uuid.uuid4()),
    }

    print(
        "🧾 prepareOffer...",
        flush=True,
    )

    # IMPORTANTE:
    # non stampiamo TOKEN/private key.

    print(
        "🧾 prepareOffer input:",
        json.dumps(
            inp,
            ensure_ascii=False,
        ),
        flush=True,
    )

    d = graphql(
        """
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
                    code
                }
            }
        }
        """,
        {
            "input":
                inp,
        },
    )

    # --------------------------------------------------------
    # NESSUNA RISPOSTA
    # --------------------------------------------------------

    if not d:

        raise RuntimeError(
            "prepareOffer: "
            "nessuna risposta"
        )

    # --------------------------------------------------------
    # GRAPHQL TOP LEVEL ERRORS
    # --------------------------------------------------------

    if d.get("errors"):

        print(
            "❌ prepareOffer "
            "TOP-LEVEL ERRORS:",
            flush=True,
        )

        print(
            json.dumps(
                d["errors"],
                ensure_ascii=False,
                indent=2,
            )[:10000],
            flush=True,
        )

        raise RuntimeError(
            "prepareOffer GraphQL error"
        )

    # --------------------------------------------------------
    # DATA
    # --------------------------------------------------------

    data = (
        d.get("data")
        or {}
    )

    prepare = (
        data.get(
            "prepareOffer"
        )
        or {}
    )

    # --------------------------------------------------------
    # RISPOSTA DIAGNOSTICA
    # --------------------------------------------------------

    print(
        "📥 prepareOffer response:",
        flush=True,
    )

    print(
        json.dumps(
            prepare,
            ensure_ascii=False,
            indent=2,
        )[:15000],
        flush=True,
    )

    # --------------------------------------------------------
    # BUSINESS ERRORS
    # --------------------------------------------------------

    errors = (
        prepare.get(
            "errors"
        )
        or []
    )

    if errors:

        messages = []

        for error in errors:

            if isinstance(
                error,
                dict,
            ):

                message = error.get(
                    "message"
                )

                code = error.get(
                    "code"
                )

                if code:

                    messages.append(
                        f"{code}: {message}"
                    )

                else:

                    messages.append(
                        str(message)
                    )

            else:

                messages.append(
                    str(error)
                )

        raise RuntimeError(
            "prepareOffer error: "
            + " | ".join(
                messages
            )
        )

    # --------------------------------------------------------
    # AUTHORIZATIONS
    # --------------------------------------------------------

    authorizations = (
        prepare.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        raise RuntimeError(
            "prepareOffer non ha "
            "restituito autorizzazioni"
        )

    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )

    # --------------------------------------------------------
    # SIGN
    # --------------------------------------------------------

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
            f"🔐 Authorization {index}: "
            f"{typename}",
            flush=True,
        )

        if typename == (
            "SolanaTokenTransferAuthorizationRequest"
        ):

            approval = sign_solana(
                authorization
            )

            approvals.append(
                approval
            )

        else:

            raise RuntimeError(
                "Authorization "
                "non supportata: "
                + str(typename)
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

    if not approvals:

        raise RuntimeError(
            "Nessuna approval disponibile"
        )

    inp = {

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

        "duration":
            LISTING_DURATION,

        "clientMutationId":
            str(uuid.uuid4()),
    }

    print(
        "📤 createSingleSaleOffer...",
        flush=True,
    )

    d = graphql(
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
                inp,
        },
    )

    if not d:

        raise RuntimeError(
            "createSingleSaleOffer: "
            "nessuna risposta"
        )

    if d.get("errors"):

        raise RuntimeError(
            "createSingleSaleOffer "
            "GraphQL error: "
            + json.dumps(
                d["errors"],
                ensure_ascii=False,
            )[:5000]
        )

    result = (
        ((d.get("data") or {})
         .get("createSingleSaleOffer"))
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
            "; ".join(
                str(
                    x.get(
                        "message",
                        ""
                    )
                )
                for x in errors
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
    c,
    price,
):

    asset_id = c.get(
        "assetId"
    )

    if not asset_id:

        raise RuntimeError(
            "assetId mancante"
        )

    print(
        f"💰 SELL {label(c)} "
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
        f"| MAX={eur(MAX_PRICE)}",
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
                    "❌ Impossibile verificare "
                    "lineup",
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
                    # Già in vendita
                    # ----------------------------------------

                    if card.get(
                        "liveSingleSaleOffer"
                    ):

                        continue

                    # ----------------------------------------
                    # VALIDATION
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

                    except Exception as e:

                        print(
                            f"🔴 AutoSell fallito: "
                            f"{e}",
                            flush=True,
                        )

                except Exception as e:

                    print(
                        f"❌ Carta "
                        f"{label(card)}: "
                        f"{e}",
                        flush=True,
                    )

        except Exception as e:

            print(
                f"🔥 Worker: {e}",
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

        "min_price_eur":
            MIN_PRICE / 100,

        "max_price_eur":
            MAX_PRICE / 100,
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

        "min_price_eur":
            MIN_PRICE / 100,

        "max_price_eur":
            MAX_PRICE / 100,
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
