import os
import time
import uuid
import json
import secrets
import subprocess
import threading
import requests

from flask import Flask, jsonify


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
PRIVATE_KEY = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

MIN_PRICE = 32                 # €0.32
MAX_PRICE = 70                 # €0.70
MIN_LIVE_LISTINGS = 5

LISTING_DURATION = 7 * 24 * 60 * 60
INTERVAL = 15
TIMEOUT = 30

BOT_VERSION = "30.0-AUTOSELL-SOLANA-FIX"

# ------------------------------------------------------------
# KULENOVIC — MAI IN VENDITA
# ------------------------------------------------------------

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

KID = os.getenv("KULENOVIC_ID", "").strip()


# ============================================================
# GLOBALS
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
    p = [c.get("name") or c.get("slug") or "Carta"]

    if c.get("seasonYear"):
        p.append(str(c["seasonYear"]))

    if c.get("rarityTyped"):
        p.append(str(c["rarityTyped"]))

    if c.get("serialNumber"):
        p.append(f"#{c['serialNumber']}")

    return " • ".join(p)


def eur(c):
    if c is None:
        return "N/D"

    return f"€{c / 100:.2f}"


def random_id():
    """
    Sorare examples use random byte strings for IDs.
    """
    return secrets.token_hex(16)


def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{BOT_VERSION}",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):
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
                flush=True
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
                                attempt + 2
                            )
                        ),
                        15,
                    )
                except Exception:
                    wait = attempt + 2

                print(
                    f"⏳ Rate limit → attendo {wait}s",
                    flush=True
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
                    flush=True
                )

                time.sleep(attempt + 1)
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
                    flush=True
                )
                return None

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

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
                f"❌ GraphQL exception: {e}",
                flush=True
            )

            time.sleep(attempt + 1)

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

    user = (
        ((d or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:
        print(
            "❌ Account Sorare non verificato",
            flush=True
        )
        return False

    print(
        f"✅ Account: "
        f"{user.get('nickname') or user.get('slug')}",
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

        if not d or d.get("errors"):
            return None

        cards = (
            ((d.get("data") or {})
             .get("currentUser") or {})
            .get("cards") or {}
        )

        nodes = cards.get("nodes") or []

        total += len(nodes)

        for card in nodes:
            if card.get("sealed"):
                sealed += 1
            else:
                out.append(card)

        page = cards.get("pageInfo") or {}

        if not page.get("hasNextPage"):
            break

        after = page.get("endCursor")

        if not after:
            break

    # --------------------------------------------------------
    # SOLO LIMITED
    # --------------------------------------------------------

    out = [
        c
        for c in out
        if norm(c.get("rarityTyped")) == "limited"
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

    if not isinstance(value, list):
        return None

    return {
        norm(x)
        for x in value
        if x
    }


def identifiers(c):
    result = set()

    asset = norm(c.get("assetId"))
    slug = norm(c.get("slug"))

    if asset:
        result.add(asset)

    if slug:
        result.add(slug)

    return result


def in_lineup(c, lineup):
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
        wanted.add(norm(KID))

    return bool(
        identifiers(c) & wanted
    )


# ============================================================
# USD → EUR
# ============================================================

def usd_eur():
    global usd_rate
    global usd_time

    now = time.time()

    if usd_rate and now - usd_time < 300:
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

    except Exception:
        return None


def price_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    # --------------------------------------------------------
    # EUR FIRST
    # --------------------------------------------------------

    try:
        cents = int(
            amounts.get("eurCents")
        )

        if cents > 0:
            return cents

    except Exception:
        pass

    # --------------------------------------------------------
    # USD FALLBACK
    # --------------------------------------------------------

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

    return int(
        round(usd_cents * rate)
    )


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(c):
    player = c.get("anyPlayer") or {}

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
    )

    if not d or d.get("errors"):
        return None

    offers = (
        ((d.get("data") or {})
         .get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in offers:
        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        )

        for market_card in cards:
            market_player = (
                market_card.get("anyPlayer") or {}
            )

            try:
                market_season = int(
                    market_card.get("seasonYear")
                )
            except Exception:
                continue

            if (
                norm(
                    market_player.get("slug")
                ) == player_slug
                and
                norm(
                    market_card.get("rarityTyped")
                ) == rarity
                and
                market_season == season
            ):
                amount = price_eur(
                    (offer.get("receiverSide") or {})
                    .get("amounts") or {}
                )

                if amount is not None:
                    prices.append(amount)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# VALIDATION
# ============================================================

def validate(c, lineup):
    if c.get("sealed"):
        return False, "VAULT", None

    if is_kulenovic(c):
        return False, "KULENOVIC", None

    if norm(c.get("rarityTyped")) != "limited":
        return False, "RARITY", None

    lineup_status = in_lineup(
        c,
        lineup
    )

    if lineup_status is None:
        return False, "LINEUP_UNKNOWN", None

    if lineup_status:
        return False, "LINEUP", None

    floor = live_floor(c)

    if floor is None:
        return False, "PRICE_UNKNOWN", None

    if floor < MIN_PRICE:
        return False, "PRICE_LOW", floor

    if floor > MAX_PRICE:
        return False, "PRICE_HIGH", floor

    return True, "OK", floor


def reject(c, reason, value=None):
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
        message = (
            f"FLOOR {eur(value)} "
            f"SOTTO IL MINIMO"
        )

    elif reason == "PRICE_HIGH":
        message = (
            f"FLOOR {eur(value)} "
            f"SOPRA IL MASSIMO"
        )

    else:
        message = messages.get(
            reason,
            reason
        )

    print(
        f"🚫 {label(c)} → {message}",
        flush=True,
    )


# ============================================================
# NODE
# ============================================================

def node():
    for directory in os.getenv(
        "PATH",
        ""
    ).split(os.pathsep):

        executable = os.path.join(
            directory,
            "node"
        )

        if (
            os.path.isfile(executable)
            and os.access(
                executable,
                os.X_OK
            )
        ):
            return executable

    return None


# ============================================================
# SOLANA SIGN
# ============================================================

def sign_solana(authorization):
    """
    Signs a SolanaTokenTransferAuthorizationRequest.

    IMPORTANT:
    senderAddress is NOT part of the signed message.

    If Sorare does not return senderAddress in the GraphQL
    payload, we do NOT abort locally.

    The derived Solana address is printed for diagnostics.
    """

    node_path = node()

    if not node_path:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not PRIVATE_KEY:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        )

    script = r"""
const crypto = require("crypto");

const {
  createSignableMessage,
  getBase58Decoder,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
} = require("@solana/kit");

const {
  HDKey
} = require("micro-key-producer/slip10.js");


(async () => {

  const input = JSON.parse(
    require("fs")
      .readFileSync(0, "utf8")
  );

  const authorization = input.authorization;
  const request = authorization.request;

  if (!request) {
    throw new Error(
      "Authorization.request mancante"
    );
  }

  if (
    request.__typename !==
    "SolanaTokenTransferAuthorizationRequest"
  ) {
    throw new Error(
      "Authorization non Solana: " +
      request.__typename
    );
  }


  // ----------------------------------------------------------
  // PRIVATE KEY
  // ----------------------------------------------------------

  const seed = Buffer.from(
    input.privateKey.replace(/^0x/, ""),
    "hex"
  );

  const derived = HDKey
    .fromMasterSeed(seed)
    .derive("m/44'/501'/0'/0'");


  const keyPair =
    await createKeyPairFromPrivateKeyBytes(
      derived.privateKey
    );

  const signer =
    createSignerFromKeyPair(keyPair);


  // ----------------------------------------------------------
  // REQUEST FIELDS
  // ----------------------------------------------------------

  const {
    leafIndex,
    merkleTreeAddress,
    originator,
    receiverAddress,
    senderAddress,
    expirationTimestamp,
    nonce,
    transferProxyProgramAddress
  } = request;


  // ----------------------------------------------------------
  // DIAGNOSTICS
  // ----------------------------------------------------------

  console.error(
    "🔑 Derived Solana address:",
    signer.address
  );

  console.error(
    "📨 Request senderAddress:",
    senderAddress === undefined
      ? "MISSING"
      : senderAddress
  );


  // ----------------------------------------------------------
  // OPTIONAL CHECK
  //
  // Do NOT fail if senderAddress is missing.
  // It is not part of the signed message.
  // ----------------------------------------------------------

  if (
    senderAddress !== undefined &&
    senderAddress !== null &&
    senderAddress !== "" &&
    signer.address !== senderAddress
  ) {
    throw new Error(
      "Solana key mismatch: derived=" +
      signer.address +
      " request=" +
      senderAddress
    );
  }


  // ----------------------------------------------------------
  // REQUIRED FIELD VALIDATION
  // ----------------------------------------------------------

  const required = {
    leafIndex,
    merkleTreeAddress,
    originator,
    receiverAddress,
    expirationTimestamp,
    nonce,
    transferProxyProgramAddress
  };

  for (const [name, value] of Object.entries(required)) {
    if (
      value === undefined ||
      value === null ||
      value === ""
    ) {
      throw new Error(
        "Campo Solana mancante: " + name
      );
    }
  }


  // ----------------------------------------------------------
  // SORARE MESSAGE
  //
  // DO NOT MODIFY THIS ORDER.
  // ----------------------------------------------------------

  const message = [
    "TRANSFER",
    transferProxyProgramAddress,
    merkleTreeAddress,
    leafIndex.toString(),
    nonce.toString(),
    expirationTimestamp.toString(),
    receiverAddress,
    "0x",
    originator
  ].join(":");


  console.error(
    "🧾 Solana message:",
    message
  );


  // ----------------------------------------------------------
  // SHA-256
  // ----------------------------------------------------------

  const messageHash =
    await crypto.webcrypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(message)
    );


  // ----------------------------------------------------------
  // SIGN HASH
  // ----------------------------------------------------------

  const signableMessage =
    createSignableMessage(
      new Uint8Array(messageHash)
    );

  const [signatures] =
    await signer.signMessages([
      signableMessage
    ]);


  // ----------------------------------------------------------
  // BASE58
  // ----------------------------------------------------------

  const signature =
    getBase58Decoder().decode(
      signatures[signer.address]
    );


  if (!signature) {
    throw new Error(
      "Firma Solana vuota"
    );
  }


  // ----------------------------------------------------------
  // APPROVAL
  //
  // Exactly the fields Sorare expects.
  // ----------------------------------------------------------

  const approval = {
    fingerprint:
      authorization.fingerprint,

    solanaTokenTransferApproval: {
      signature: signature,
      nonce: nonce,
      expirationTimestamp:
        expirationTimestamp
    }
  };


  process.stdout.write(
    JSON.stringify(approval)
  );

})().catch(error => {

  console.error(error);

  process.exit(1);

});
"""

    payload = {
        "authorization": authorization,
        "privateKey": PRIVATE_KEY,
    }

    process = subprocess.run(
        [
            node_path,
            "-e",
            script,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
    )

    # --------------------------------------------------------
    # PASS NODE DIAGNOSTICS TO PYTHON LOG
    # --------------------------------------------------------

    if process.stderr:
        for line in process.stderr.splitlines():
            print(
                line,
                flush=True
            )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "Firma Solana fallita"
        )

    try:
        result = json.loads(
            process.stdout
        )
    except Exception as e:
        raise RuntimeError(
            "Output firma non valido: "
            + str(e)
        )

    if not result.get("fingerprint"):
        raise RuntimeError(
            "Fingerprint mancante nella firma"
        )

    approval = result.get(
        "solanaTokenTransferApproval"
    )

    if not approval:
        raise RuntimeError(
            "solanaTokenTransferApproval mancante"
        )

    if not approval.get("signature"):
        raise RuntimeError(
            "Signature mancante"
        )

    return result


# ============================================================
# PREPARE OFFER
# ============================================================

def prepare_offer(asset_id, price):
    """
    IMPORTANT:
    No 'type' field.

    Your current API explicitly rejects:
        Field is not defined on prepareOfferInput

    So this input intentionally contains only fields
    supported by the current endpoint.
    """

    input_data = {
        "sendAssetIds": [
            asset_id
        ],

        "receiveAssetIds": [],

        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR",
        },

        "settlementCurrencies": [
            "EUR"
        ],

        "clientMutationId": random_id(),
    }


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
            }
          }
        }
        """,
        {
            "input": input_data
        },
    )


    prepared = (
        ((d or {}).get("data") or {})
        .get("prepareOffer") or {}
    )


    errors = (
        prepared.get("errors")
        or []
    )

    if errors:
        raise RuntimeError(
            "; ".join(
                x.get("message", "")
                for x in errors
            )
        )


    authorizations = (
        prepared.get("authorizations")
        or []
    )

    if not authorizations:
        raise RuntimeError(
            "prepareOffer non ha restituito "
            "autorizzazioni"
        )


    approvals = []


    for index, authorization in enumerate(
        authorizations
    ):
        request = (
            authorization.get("request")
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


        if (
            typename ==
            "SolanaTokenTransferAuthorizationRequest"
        ):
            approvals.append(
                sign_solana(
                    authorization
                )
            )

        else:
            raise RuntimeError(
                "Authorization non supportata: "
                + str(typename)
            )


    if not approvals:
        raise RuntimeError(
            "Nessuna approval generata"
        )


    return approvals


# ============================================================
# CREATE SINGLE SALE OFFER
# ============================================================

def create_offer(
    asset_id,
    price,
    approvals
):
    input_data = {
        "approvals": approvals,

        "dealId": random_id(),

        "assetId": asset_id,

        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR",
        },

        "clientMutationId": random_id(),
    }


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
            }

            errors {
              message
            }
          }
        }
        """,
        {
            "input": input_data
        },
    )


    result = (
        ((d or {}).get("data") or {})
        .get("createSingleSaleOffer") or {}
    )


    errors = (
        result.get("errors")
        or []
    )

    if errors:
        raise RuntimeError(
            "; ".join(
                x.get("message", "")
                for x in errors
            )
        )


    offer = result.get(
        "tokenOffer"
    )

    if not offer:
        raise RuntimeError(
            "createSingleSaleOffer non ha "
            "restituito tokenOffer"
        )


    return offer


# ============================================================
# AUTOSELL
# ============================================================

def autosell(c, price):
    asset_id = c.get("assetId")

    if not asset_id:
        raise RuntimeError(
            "assetId mancante"
        )


    print(
        f"💰 SELL {label(c)} → {eur(price)}",
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
        price
    )


    print(
        f"✍️ Approval generate: "
        f"{len(approvals)}",
        flush=True,
    )


    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    offer = create_offer(
        asset_id,
        price,
        approvals
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
        f"
