import os
import time
import uuid
import json
import base64
import shutil
import subprocess
import threading
import re
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

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv(
    "GITHUB_REPO",
    "fisiogiordano-hub/bot-sorare"
).strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
MIN_LIVE_LISTINGS = 5

SWAP_AUTO_ACCEPT = True
SWAP_MIN = 1.20
SWAP_MAX = 1.25

INTERVAL = 10
TIMEOUT = 25
USD_CACHE = 300
COVERAGE_CACHE = 3600

BOT_VERSION = "23.6-LIGHT"

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# STATE
# ============================================================

processed = set()
acquired_cards = {}
pending_autobuys = {}

state_lock = threading.Lock()
github_lock = threading.Lock()
worker_lock = threading.Lock()

worker_started = False

usd_rate = None
usd_time = 0

coverage_cache = set()
coverage_time = 0

current_user_slug = None


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

    if slug and slug != name:
        return f"{name} [{slug}]"

    return name


def format_eur(c):
    if c is None:
        return "N/D"

    return f"€{c / 100:.2f}"


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
    for attempt in range(3):
        try:
            r = requests.post(
                SORARE_URL,
                json={
                    "query": query,
                    "variables": variables or {}
                },
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                wait = min(
                    int(r.headers.get("Retry-After", attempt + 2)),
                    15
                )
                time.sleep(wait)
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:500]}",
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
# STATE
# ============================================================

def normalize_card(x):
    if not isinstance(x, dict):
        return None

    asset_id = str(
        x.get("assetId") or
        x.get("asset_id") or
        ""
    ).strip()

    if not asset_id:
        return None

    return {
        "assetId": asset_id,
        "slug": x.get("slug"),
        "purchase_price_cents": x.get(
            "purchase_price_cents"
        ),
        "status": x.get("status") or "da_vendere",
        "source": x.get("source") or "unknown",
        "offer_id": x.get("offer_id")
    }


def normalize_pending(x):
    if not isinstance(x, dict):
        return None

    offer_id = str(
        x.get("offer_id") or ""
    ).strip()

    if not offer_id:
        return None

    return {
        "offer_id": offer_id,
        "original_offer_id": x.get(
            "original_offer_id"
        ),
        "created_at": x.get("created_at"),
        "cards": x.get("cards") or [],
        "price_per_card": x.get(
            "price_per_card",
            PAY_PER_CARD
        ),
        "status": x.get("status") or "PENDING"
    }


def build_state():
    with state_lock:
        return {
            "processed_offers": sorted(processed),
            "acquired_cards": list(
                acquired_cards.values()
            ),
            "pending_autobuys": list(
                pending_autobuys.values()
            ),
            "updated_at": int(time.time())
        }


def load_state_data(data):
    global processed
    global acquired_cards
    global pending_autobuys

    if not isinstance(data, dict):
        return

    processed = {
        norm(x)
        for x in data.get("processed_offers") or []
        if x
    }

    for x in data.get("acquired_cards") or []:
        card = normalize_card(x)

        if card:
            acquired_cards[
                norm(card["assetId"])
            ] = card

    for x in data.get("pending_autobuys") or []:
        pending = normalize_pending(x)

        if pending:
            pending_autobuys[
                norm(pending["offer_id"])
            ] = pending


def load_local_state():
    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(
            STATE_FILE,
            encoding="utf-8"
        ) as f:
            load_state_data(json.load(f))

    except Exception as e:
        print(
            f"⚠️ Lettura {STATE_FILE}: {e}",
            flush=True
        )


def save_local_state():
    try:
        tmp = STATE_FILE + ".tmp"

        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                build_state(),
                f,
                indent=2,
                ensure_ascii=False
            )

        os.replace(tmp, STATE_FILE)

        return True

    except Exception as e:
        print(
            f"❌ Salvataggio {STATE_FILE}: {e}",
            flush=True
        )
        return False


# ============================================================
# GITHUB STATE
# ============================================================

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
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO}/contents/{STATE_FILE}"
    )


def load_github_state():
    if not GITHUB_TOKEN:
        return

    try:
        r = requests.get(
            github_url(),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=TIMEOUT
        )

        if r.status_code == 404:
            return

        if r.status_code != 200:
            return

        content = r.json().get("content")

        if not content:
            return

        data = json.loads(
            base64.b64decode(
                content.replace("\n", "")
            ).decode()
        )

        with state_lock:
            load_state_data(data)

        print(
            f"💾 GitHub: {len(processed)} offerte | "
            f"{len(acquired_cards)} carte | "
            f"{len(pending_autobuys)} pending",
            flush=True
        )

    except Exception as e:
        print(
            f"⚠️ GitHub load: {e}",
            flush=True
        )


def save_github_state():
    if not GITHUB_TOKEN:
        return False

    with github_lock:
        try:
            raw = json.dumps(
                build_state(),
                indent=2,
                ensure_ascii=False
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

            sha = (
                r.json().get("sha")
                if r.status_code == 200
                else None
            )

            data = {
                "message": (
                    f"Update bot_state.json "
                    f"{int(time.time())}"
                ),
                "content": encoded,
                "branch": GITHUB_BRANCH
            }

            if sha:
                data["sha"] = sha

            r = requests.put(
                github_url(),
                headers=github_headers(),
                json=data,
                timeout=TIMEOUT
            )

            return r.status_code in (200, 201)

        except Exception as e:
            print(
                f"❌ GitHub save: {e}",
                flush=True
            )
            return False


def persist():
    save_local_state()

    if GITHUB_TOKEN:
        save_github_state()


def mark_done(offer_id):
    if not offer_id:
        return

    with state_lock:
        processed.add(norm(offer_id))

    persist()


def add_pending(
    offer_id,
    original_offer_id,
    cards
):
    item = {
        "offer_id": offer_id,
        "original_offer_id": original_offer_id,
        "created_at": int(time.time()),
        "cards": [
            {
                "assetId": c.get("assetId"),
                "slug": c.get("slug"),
                "name": c.get("name")
            }
            for c in cards
        ],
        "price_per_card": PAY_PER_CARD,
        "status": "PENDING"
    }

    with state_lock:
        pending_autobuys[
            norm(offer_id)
        ] = item

    persist()


def remove_pending(offer_id):
    with state_lock:
        pending_autobuys.pop(
            norm(offer_id),
            None
        )

    persist()


# ============================================================
# ACCOUNT / OFFERTE
# ============================================================

def check_account():
    global current_user_slug

    data = graphql(
        "query{currentUser{slug nickname starkKey}}"
    )

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:
        return False

    current_user_slug = user.get("slug")

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or current_user_slug}",
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


def get_received_offers():
    data = graphql("""
    query{
      currentUser{
        pendingTokenOffersReceived(first:50){
          nodes{
            id
            blockchainId
            status
            sender{
              ... on User{
                slug
                nickname
              }
            }
            senderSide{
              amounts{
                eurCents
                usdCents
                referenceCurrency
                wei
              }
              anyCards{
                assetId
                slug
                collection
              }
            }
            receiverSide{
              amounts{
                eurCents
                usdCents
                referenceCurrency
                wei
              }
              anyCards{
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
        (
            ((data or {}).get("data") or {})
            .get("currentUser") or {}
        )
        .get("pendingTokenOffersReceived", {})
        .get("nodes", [])
    )


def get_pending_sent_offer(offer_id):
    data = graphql("""
    query{
      currentUser{
        pendingTokenOffersSent(first:50){
          nodes{
            id
            blockchainId
            status
            type
            createdAt
            acceptedAt
            cancelledAt
            transactionDate
            sender{
              ... on User{
                slug
              }
            }
            receiver{
              ... on User{
                slug
              }
            }
            senderSide{
              anyCards{
                assetId
                slug
                name
              }
            }
            receiverSide{
              anyCards{
                assetId
                slug
                name
              }
            }
          }
        }
      }
    }
    """)

    offers = (
        (
            ((data or {}).get("data") or {})
            .get("currentUser") or {}
        )
        .get("pendingTokenOffersSent", {})
        .get("nodes", [])
    )

    wanted = norm(offer_id)

    for offer in offers:
        if norm(offer.get("id")) == wanted:
            return offer

    return None


# ============================================================
# CARTE
# ============================================================

def card_details(ids):
    ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in ids
            if x
        )
    )

    if not ids:
        return []

    data = graphql("""
    query($assetIds:[String!]!){
      anyCards(assetIds:$assetIds){
        assetId
        slug
        name
        rarityTyped
        seasonYear
        user{
          slug
        }
        tokenOwner{
          user{
            slug
          }
        }
        anyPlayer{
          slug
          displayName
          age
          activeClub{
            slug
            name
            activeCompetitions{
              slug
            }
          }
        }
      }
    }
    """, {
        "assetIds": ids
    })

    return (
        ((data or {}).get("data") or {})
        .get("anyCards") or []
    )


def card_owned(card):
    me = norm(current_user_slug)

    return (
        norm(
            (card.get("user") or {}).get("slug")
        ) == me
        or
        norm(
            (
                (card.get("tokenOwner") or {})
                .get("user") or {}
            ).get("slug")
        ) == me
    )


def usd_eur():
    global usd_rate
    global usd_time

    if (
        usd_rate and
        time.time() - usd_time < USD_CACHE
    ):
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

        rate = float(
            r.json()["rates"]["EUR"]
        )

        if rate > 0:
            usd_rate = rate
            usd_time = time.time()
            return rate

    except Exception:
        pass

    return None


def price_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        value = int(
            amounts.get("eurCents")
        )

        if value > 0:
            return value

    except Exception:
        pass

    try:
        value = float(
            amounts.get("usdCents")
        )

    except Exception:
        return None

    rate = usd_eur()

    if value > 0 and rate:
        return int(round(value * rate))

    return None


def live_floor(card):
    player = card.get("anyPlayer") or {}

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

    data = graphql("""
    query($playerSlug:String,$first:Int){
      tokens{
        liveSingleSaleOffers(
          playerSlug:$playerSlug
          first:$first
        ){
          nodes{
            senderSide{
              anyCards{
                assetId
                rarityTyped
                seasonYear
                anyPlayer{
                  slug
                }
              }
            }
            receiverSide{
              amounts{
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

    offers = (
        (
            ((data or {}).get("data") or {})
            .get("tokens") or {}
        )
        .get("liveSingleSaleOffers", {})
        .get("nodes", [])
    )

    prices = []

    for offer in offers:
        for c in (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        ):
            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) == player_slug
                and
                norm(
                    c.get("rarityTyped")
                ) == rarity
                and
                int(
                    c.get("seasonYear") or -1
                ) == season
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
        return None

    return min(prices)


# ============================================================
# COVERAGE / VALIDAZIONE
# ============================================================

def load_coverage(force=False):
    global coverage_cache
    global coverage_time

    if (
        not force
        and coverage_cache
        and time.time() - coverage_time < COVERAGE_CACHE
    ):
        return set(coverage_cache)

    try:
        r = requests.get(
            COVERAGE_URL,
            timeout=TIMEOUT,
            headers={
                "User-Agent":
                    f"Sorare-Bot/{BOT_VERSION}"
            }
        )

        if r.status_code != 200:
            return set(coverage_cache)

        result = {
            norm(x)
            for x in re.findall(
                r'/football/leagues/([^"\'?#<>\s]+)',
                r.text,
                re.I
            )
        }

        if result:
            coverage_cache = result
            coverage_time = time.time()

            print(
                f"🌐 Coverage: "
                f"{len(result)} competizioni",
                flush=True
            )

        return set(coverage_cache)

    except Exception:
        return set(coverage_cache)


def is_kulenovic(card):
    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    extra = os.getenv(
        "KULENOVIC_ID",
        ""
    ).strip()

    if extra:
        wanted.add(norm(extra))

    return (
        norm(card.get("assetId")) in wanted
        or
        norm(card.get("slug")) in wanted
    )


def validate_card(card):
    player = card.get("anyPlayer") or {}

    try:
        age = int(player.get("age"))
    except Exception:
        return False

    if age >= MAX_AGE:
        return False

    if (
        norm(
            card.get("rarityTyped")
        ).upper() != "LIMITED"
    ):
        return False

    floor = live_floor(card)

    if floor is None:
        return False

    if floor < MIN_PRICE:
        return False

    if floor > MAX_PRICE:
        return False

    club = player.get("activeClub") or {}

    active = {
        norm(x.get("slug"))
        for x in (
            club.get("activeCompetitions") or []
        )
        if x.get("slug")
    }

    if not active.intersection(
        load_coverage()
    ):
        return False

    return True


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    blockchain_id = norm(
        offer.get("blockchainId")
    )

    if not blockchain_id:
        print(
            "❌ Reject: blockchainId mancante",
            flush=True
        )
        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: reject simulato",
            flush=True
        )
        return True

    data = graphql("""
    mutation($input:rejectOfferInput!){
      rejectOffer(input:$input){
        tokenOffer{
          id
          status
        }
        errors{
          message
        }
      }
    }
    """, {
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result:
        return False

    if result.get("errors"):
        print(
            "❌ Reject:",
            result["errors"],
            flush=True
        )
        return False

    print(
        "✅ Offerta rifiutata",
        flush=True
    )

    return True


# ============================================================
# FIRMA
# ============================================================

def sign_authorizations(authorizations):
    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js mancante"
        )

    if not STARK:
        raise RuntimeError(
            "Stark key mancante"
        )

    script = r'''
const fs = require("fs");
const {
  signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
  fs.readFileSync(0, "utf8")
);

function sign(a) {
  const r = a.request;

  if (r.amount != null) {
    r.amount = BigInt(r.amount);
  }

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
        signature
      }
    };
  }

  throw new Error(
    "Authorization non supportata"
  );
}

process.stdout.write(
  JSON.stringify(
    input.authorizations.map(sign)
  )
);
'''

    process = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
        )

    return json.loads(
        process.stdout
    )


# ============================================================
# CREAZIONE CONTROPROPOSTA
# ============================================================

def create_offer(
    receiver,
    send_ids,
    receive_ids,
    cash
):
    if not receiver:
        print(
            "❌ create_offer: receiver mancante",
            flush=True
        )
        return None

    amount = max(
        0,
        int(cash)
    )

    data = graphql("""
    mutation($input:prepareOfferInput!){
      prepareOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename

            ... on StarkexTransferAuthorizationRequest{
              amount
              condition
              expirationTimestamp
              nonce
              receiverPublicKey
              receiverVaultId
              senderVaultId
              token
              feeInfoUser{
                feeLimit
                sourceVaultId
                tokenId
              }
            }

            ... on StarkexLimitOrderAuthorizationRequest{
              vaultIdSell
              vaultIdBuy
              amountSell
              amountBuy
              tokenSell
              tokenBuy
              nonce
              expirationTimestamp
              feeInfo{
                feeLimit
                tokenId
                sourceVaultId
              }
            }

            ... on MangopayWalletTransferAuthorizationRequest{
              nonce
              amount
              currency
              operationHash
              mangopayWalletId
            }
          }
        }
        errors{
          message
        }
      }
    }
    """, {
        "input": {
            "receiveAssetIds": receive_ids,
            "sendAssetIds": send_ids,
            "sendAmount": {
                "amount": str(amount),
                "currency": "EUR"
            },
            "receiverSlug": receiver,
            "settlementCurrencies": ["EUR"],
            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print(
            "❌ prepareOffer: errore",
            flush=True
        )
        return None

    if result.get("errors"):
        print(
            "❌ prepareOffer:",
            result["errors"],
            flush=True
        )
        return None

    authorizations = (
        result.get("authorizations") or []
    )

    if not authorizations:
        print(
            "❌ prepareOffer: "
            "nessuna authorization",
            flush=True
        )
        return None

    try:
        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True
        )
        return None

    data = graphql("""
    mutation($input:createDirectOfferInput!){
      createDirectOffer(input:$input){
        tokenOffer{
          id
          blockchainId
          status
          type
        }
        errors{
          message
        }
      }
    }
    """, {
        "input": {
            "receiveAssetIds": receive_ids,
            "sendAssetIds": send_ids,
            "sendAmount": {
                "amount": str(amount),
                "currency": "EUR"
            },
            "receiverSlug": receiver,
            "clientMutationId": str(
                uuid.uuid4()
            ),
            "approvals": approvals,
            "dealId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        print(
            "❌ createDirectOffer: errore",
            flush=True
        )
        return None

    if result.get("errors"):
        print(
            "❌ createDirectOffer:",
            result["errors"],
            flush=True
        )
        return None

    token_offer = (
        result.get("tokenOffer") or {}
    )

    return token_offer.get("id")


# ============================================================
# AUTOBUY
# ============================================================

def process_autobuy(offer):
    offer_id = norm(
        offer.get("id")
    )

    if not offer_id:
        return

    if offer_id in processed:
        return

    wanted = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    if not any(
        is_kulenovic(card)
        for card in wanted
    ):
        return

    sender = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    ids = [
        card.get("assetId")
        for card in sender
        if card.get("assetId")
    ]

    print(
        f"🔍 AUTOBUY {offer_id}: "
        f"{len(ids)} carte ricevute",
        flush=True
    )

    if not ids:
        print(
            "🚫 AUTOBUY: nessuna carta → rifiuto",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    details = card_details(ids)

    if len(details) != len(ids):
        print(
            f"❌ AUTOBUY: dettagli incompleti "
            f"{len(details)}/{len(ids)}",
            flush=True
        )

        # Nessuna decisione lasciata sospesa.
        if reject_offer(offer):
            mark_done(offer_id)

        return

    # ========================================================
    # ANALISI DI OGNI CARTA
    # ========================================================

    valid = []
    invalid = []

    for card in details:
        try:
            if validate_card(card):
                valid.append(card)

                print(
                    f"✅ IDONEA: "
                    f"{card_label(card)}",
                    flush=True
                )

            else:
                invalid.append(card)

                print(
                    f"🚫 NON IDONEA: "
                    f"{card_label(card)}",
                    flush=True
                )

        except Exception as e:
            invalid.append(card)

            print(
                f"❌ ERRORE VALIDAZIONE: "
                f"{card_label(card)} → {e}",
                flush=True
            )

    print(
        f"🔎 AUTOBUY ANALISI: "
        f"{len(valid)}/{len(ids)} carte idonee",
        flush=True
    )

    # ========================================================
    # NESSUNA CARTA IDONEA
    # ========================================================

    if not valid:
        print(
            "🚫 AUTOBUY: "
            "nessuna carta idonea → rifiuto",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    # ========================================================
    # ALMENO UNA CARTA IDONEA
    #
    # LA CONTROPROPOSTA CONTIENE SOLO LE IDONEE
    # ========================================================

    receiver = norm(
        (offer.get("sender") or {})
        .get("slug")
    )

    valid_ids = [
        card["assetId"]
        for card in valid
        if card.get("assetId")
    ]

    print(
        f"🎯 AUTOBUY: "
        f"{len(valid_ids)} carte idonee",
        flush=True
    )

    print(
        "📤 Controproposta solo con:",
        flush=True
    )

    for card in valid:
        print(
            f"   → {card_label(card)}",
            flush=True
        )

    new_id = create_offer(
        receiver,
        [],
        valid_ids,
        len(valid_ids) * PAY_PER_CARD
    )

    # ========================================================
    # CONTROPROPOSTA FALLITA
    #
    # NON LASCIARE L'ORIGINALE PENDENTE
    # ========================================================

    if not new_id:
        print(
            "❌ AUTOBUY: "
            "controproposta non creata",
            flush=True
        )

        print(
            "🚫 AUTOBUY: "
            "rifiuto dell'offerta originale",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    # ========================================================
    # CONTROPROPOSTA CREATA
    # ========================================================

    add_pending(
        new_id,
        offer_id,
        valid
    )

    # L'offerta originale viene chiusa.
    if reject_offer(offer):
        mark_done(offer_id)

    print(
        f"⏳ AUTOBUY IN ATTESA: "
        f"{new_id} | "
        f"{len(valid_ids)} carte idonee",
        flush=True
    )


# ============================================================
# CONTROLLO AUTOBUY PENDING
# ============================================================

def check_pending_autobuys():
    with state_lock:
        pending = list(
            pending_autobuys.values()
        )

    if not pending:
        return

    print(
        f"🔎 Controllo "
        f"{len(pending)} AutoBuy pending...",
        flush=True
    )

    for item in pending:
        offer_id = item.get("offer_id")

        try:
            offer = get_pending_sent_offer(
                offer_id
            )

            if not offer:
                print(
                    f"⚠️ AutoBuy {offer_id}: "
                    f"non presente tra pending inviati",
                    flush=True
                )
                continue

            status = norm(
                offer.get("status")
            ).upper()

            print(
                f"📦 AUTOBUY {offer_id} "
                f"→ {status}",
                flush=True
            )

            if status in {
                "CANCELLED",
                "REJECTED",
                "ENDED",
                "SETTLEMENT_FAILED"
            }:
                remove_pending(offer_id)
                continue

            if status not in {
                "ACCEPTED",
                "SETTLEMENT_PUBLISHED"
            }:
                continue

            ids = [
                card.get("assetId")
                for card in item.get("cards") or []
                if card.get("assetId")
            ]

            details = card_details(ids)

            if len(details) != len(ids):
                continue

            if not all(
                card_owned(card)
                for card in details
            ):
                continue

            for card in details:
                acquired_cards[
                    norm(card["assetId"])
                ] = {
                    "assetId": card["assetId"],
                    "slug": card.get("slug"),
                    "purchase_price_cents":
                        PAY_PER_CARD,
                    "status": "da_vendere",
                    "source": "autobuy",
                    "offer_id": offer_id
                }

            persist()
            remove_pending(offer_id)

            print(
                f"🎉 AUTOBUY COMPLETATO: "
                f"{offer_id}",
                flush=True
            )

        except Exception as e:
            print(
                f"❌ Controllo AutoBuy: {e}",
                flush=True
            )


# ============================================================
# SWAP
# ============================================================

def prepare_accept(offer_id):
    data = graphql(
        "query{config{exchangeRate{id}}}"
    )

    rate = (
        (
            ((data or {}).get("data") or {})
            .get("config") or {}
        )
        .get("exchangeRate", {})
        .get("id")
    )

    if not rate:
        return None, None

    data = graphql("""
    mutation($input:prepareAcceptOfferInput!){
      prepareAcceptOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename

            ... on StarkexTransferAuthorizationRequest{
              amount
              condition
              expirationTimestamp
              nonce
              receiverPublicKey
              receiverVaultId
              senderVaultId
              token
              feeInfoUser{
                feeLimit
                sourceVaultId
                tokenId
              }
            }

            ... on StarkexLimitOrderAuthorizationRequest{
              vaultIdSell
              vaultIdBuy
              amountSell
              amountBuy
              tokenSell
              tokenBuy
              nonce
              expirationTimestamp
              feeInfo{
                feeLimit
                tokenId
                sourceVaultId
              }
            }

            ... on MangopayWalletTransferAuthorizationRequest{
              nonce
              amount
              currency
              operationHash
              mangopayWalletId
            }
          }
        }
        errors{
          message
        }
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

    if not result:
        return None, None

    if result.get("errors"):
        print(
            "❌ prepareAcceptOffer:",
            result["errors"],
            flush=True
        )
        return None, None

    return (
        result.get("authorizations") or [],
        rate
    )


def accept_offer(offer):
    offer_id = norm(
        offer.get("id")
    )

    authorizations, rate = prepare_accept(
        offer_id
    )

    if not authorizations:
        return False

    try:
        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ Firma ACCEPT: {e}",
            flush=True
        )
        return False

    data = graphql("""
    mutation($input:acceptOfferInput!){
      acceptOffer(input:$input){
        tokenOffer{
          id
          status
        }
        errors{
          message
        }
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
            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("acceptOffer")
    )

    if not result:
        return False

    if result.get("errors"):
        print(
            "❌ Accept:",
            result["errors"],
            flush=True
        )
        return False

    print(
        "✅ SWAP ACCETTATO",
        flush=True
    )

    return True


def process_swap(offer):
    offer_id = norm(
        offer.get("id")
    )

    if not offer_id:
        return

    if offer_id in processed:
        return

    sender = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    receiver = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    if not sender or not receiver:
        return

    # Carte che TU dai
    give_ids = [
        card.get("assetId")
        for card in receiver
        if card.get("assetId")
    ]

    # Carte che TU ricevi
    receive_ids = [
        card.get("assetId")
        for card in sender
        if card.get("assetId")
    ]

    if not give_ids or not receive_ids:
        return

    give = card_details(give_ids)
    receive = card_details(receive_ids)

    if (
        len(give) != len(give_ids)
        or
        len(receive) != len(receive_ids)
    ):
        return

    # Kulenovic non è cedibile
    if any(
        is_kulenovic(card)
        for card in give
    ):
        print(
            "🔒 SWAP RIFIUTATO: "
            "KULENOVIC NON È CEDIBILE",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    # ========================================================
    # VALORE CARTE CEDUTE
    # ========================================================

    total_given = 0

    for card in give:
        floor = live_floor(card)

        if floor is None:
            return

        total_given += floor

        print(
            f"📤 CEDO "
            f"{card_label(card)} → "
            f"{format_eur(floor)}",
            flush=True
        )

    # ========================================================
    # VALORE CARTE RICEVUTE
    # ========================================================

    total_received = 0

    for card in receive:
        if not validate_card(card):
            print(
                f"🚫 SWAP: carta ricevuta "
                f"non valida → "
                f"{card_label(card)}",
                flush=True
            )

            if reject_offer(offer):
                mark_done(offer_id)

            return

        floor = live_floor(card)

        if floor is None:
            return

        total_received += floor

        print(
            f"📥 RICEVO "
            f"{card_label(card)} → "
            f"{format_eur(floor)}",
            flush=True
        )

    # Cash già presente nell'offerta
    cash = price_eur(
        (
            offer.get("senderSide") or {}
        ).get("amounts") or {}
    ) or 0

    total_received += cash

    minimum = int(
        round(total_given * SWAP_MIN)
    )

    maximum = int(
        round(total_given * SWAP_MAX)
    )

    print(
        f"📤 Totale ceduto: "
        f"{format_eur(total_given)}",
        flush=True
    )

    print(
        f"📥 Totale ricevuto: "
        f"{format_eur(total_received)}",
        flush=True
    )

    print(
        f"🎯 Range accettabile: "
        f"{format_eur(minimum)} - "
        f"{format_eur(maximum)}",
        flush=True
    )

    # ========================================================
    # SOTTO +20% → CONTROPROPOSTA
    # ========================================================

    if total_received < minimum:
        missing = minimum - total_received

        print(
            f"💰 SWAP sotto +20% → "
            f"richiedo altri "
            f"{format_eur(missing)}",
            flush=True
        )

        receiver_slug = norm(
            (offer.get("sender") or {})
            .get("slug")
        )

        new_id = create_offer(
            receiver_slug,
            give_ids,
            receive_ids,
            missing
        )

        if new_id:
            print(
                f"✅ CONTROPROPOSTA SWAP "
                f"INVIATA: {new_id}",
                flush=True
            )

            mark_done(offer_id)

        return

    # ========================================================
    # OLTRE +25% → RIFIUTO
    # ========================================================

    if total_received > maximum:
        print(
            "🚫 SWAP oltre +25% → rifiuto",
            flush=True
        )

        if reject_offer(offer):
            mark_done(offer_id)

        return

    # ========================================================
    # +20% / +25% → ACCETTA
    # ========================================================

    if (
        SWAP_AUTO_ACCEPT
        and
        accept_offer(offer)
    ):
        for card in receive:
            acquired_cards[
                norm(card["assetId"])
            ] = {
                "assetId": card["assetId"],
                "slug": card.get("slug"),
                "purchase_price_cents":
                    live_floor(card),
                "status": "da_vendere",
                "source": "swap",
                "offer_id": offer_id
            }

        persist()
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

    if any(
        is_kulenovic(card)
        for card in receiver
    ):
        process_autobuy(offer)
        return

    if sender and receiver:
        process_swap(offer)


# ============================================================
# WORKER
# ============================================================

def worker():
    print(
        "🤖 BOT AVVIATO",
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
        f"💰 AutoBuy: "
        f"€{PAY_PER_CARD / 100:.2f}/carta",
        flush=True
    )

    print(
        f"📊 AutoBuy floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"🎂 Età: < {MAX_AGE}",
        flush=True
    )

    print(
        f"📊 Inserzioni minime: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        "🔄 SWAP: +20% / +25%",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI CEDIBILE",
        flush=True
    )

    print(
        "🎯 KULENOVIC RICHIESTO → AUTOBUY",
        flush=True
    )

    print(
        "💾 STATE: "
        "bot_state.json + GitHub",
        flush=True
    )

    load_local_state()
    load_github_state()
    save_local_state()

    coverage = load_coverage(True)

    if not coverage:
        print(
            "❌ Coverage non disponibile",
            flush=True
        )
        return

    print(
        f"🏆 Competizioni Football coperte: "
        f"{len(coverage)}",
        flush=True
    )

    if not check_account():
        return

    while True:
        try:
            check_pending_autobuys()

            offers = get_received_offers()

            print(
                f"📨 Offerte ricevute pendenti: "
                f"{len(offers)}",
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
            name="sorare-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    with state_lock:
        return jsonify({
            "status": "online",
            "bot": "sorare",
            "version": BOT_VERSION,
            "dry_run": DRY_RUN,

            "autobuy": {
                "price_cents": PAY_PER_CARD,
                "min_floor_cents": MIN_PRICE,
                "max_floor_cents": MAX_PRICE,
                "max_age": MAX_AGE,
                "min_live_listings":
                    MIN_LIVE_LISTINGS
            },

            "swap": {
                "auto_accept":
                    SWAP_AUTO_ACCEPT,
                "min_multiplier":
                    SWAP_MIN,
                "max_multiplier":
                    SWAP_MAX
            },

            "kulenovic":
                "NEVER_CEDIBLE",

            "processed_offers":
                len(processed),

            "acquired_cards":
                len(acquired_cards),

            "pending_autobuys":
                len(pending_autobuys),

            "github_state":
                bool(GITHUB_TOKEN),

            "covered_competitions":
                len(coverage_cache)
        })


@app.get("/health")
def health():
    with state_lock:
        return jsonify({
            "status": "ok",
            "bot": "running",
            "version": BOT_VERSION,
            "worker_started":
                worker_started,
            "processed_offers":
                len(processed),
            "acquired_cards":
                len(acquired_cards),
            "pending_autobuys":
                len(pending_autobuys),
            "dry_run":
                DRY_RUN,
            "swap_auto_accept":
                SWAP_AUTO_ACCEPT
        })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
