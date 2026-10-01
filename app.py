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

from flask import Flask, jsonify, Response


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

BOT_VERSION = "23.7-LIGHT-SWAP-LISTED-RECONCILE"


KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
)


# ============================================================
# RUNTIME STATE
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
    n = card_name(c)
    s = c.get("slug")

    if s and s != n:
        return f"{n} [{s}]"

    return n


def format_eur(c):
    return "N/D" if c is None else f"€{c / 100:.2f}"


# ============================================================
# GRAPHQL
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
            else "Bearer " + TOKEN
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
        **({"JWT-AUD": AUD} if AUD else {})
    }


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
                try:
                    retry = int(
                        r.headers.get(
                            "Retry-After",
                            attempt + 2
                        )
                    )
                except Exception:
                    retry = attempt + 2

                time.sleep(min(retry, 15))
                continue

            if r.status_code != 200:
                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )

                time.sleep(attempt + 1)
                continue

            d = r.json()

            if d.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(
                        d["errors"],
                        ensure_ascii=False
                    )[:3000],
                    flush=True
                )

            return d

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

    aid = str(
        x.get("assetId")
        or x.get("asset_id")
        or ""
    ).strip()

    if not aid:
        return None

    return {
        "assetId": aid,
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

    oid = str(
        x.get("offer_id") or ""
    ).strip()

    if not oid:
        return None

    return {
        "offer_id": oid,
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


def load_state_data(d):
    global processed
    global acquired_cards
    global pending_autobuys

    if not isinstance(d, dict):
        return

    processed = {
        norm(x)
        for x in d.get("processed_offers") or []
        if x
    }

    for x in d.get("acquired_cards") or []:
        c = normalize_card(x)

        if c:
            acquired_cards[
                norm(c["assetId"])
            ] = c

    for x in d.get("pending_autobuys") or []:
        p = normalize_pending(x)

        if p:
            pending_autobuys[
                norm(p["offer_id"])
            ] = p


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
            params={
                "ref": GITHUB_BRANCH
            },
            timeout=TIMEOUT
        )

        if r.status_code == 404:
            return

        if r.status_code != 200:
            return

        content = r.json().get("content")

        if not content:
            return

        d = json.loads(
            base64.b64decode(
                content.replace("\n", "")
            ).decode()
        )

        with state_lock:
            load_state_data(d)

        print(
            f"💾 GitHub: "
            f"{len(processed)} offerte | "
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

            enc = base64.b64encode(
                raw.encode()
            ).decode()

            r = requests.get(
                github_url(),
                headers=github_headers(),
                params={
                    "ref": GITHUB_BRANCH
                },
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
                "content": enc,
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


def mark_done(oid):
    if not oid:
        return

    with state_lock:
        processed.add(norm(oid))

    persist()


def add_pending(oid, original, cards):
    item = {
        "offer_id": oid,
        "original_offer_id": original,
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
            norm(oid)
        ] = item

    persist()


def remove_pending(oid):
    with state_lock:
        pending_autobuys.pop(
            norm(oid),
            None
        )

    persist()


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    global current_user_slug

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
        return False

    current_user_slug = u.get("slug")

    print(
        f"✅ Sorare: "
        f"{u.get('nickname') or current_user_slug}",
        flush=True
    )

    print(
        "🔐 Stark key account: "
        + (
            "PRESENTE"
            if u.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    return True


# ============================================================
# RECEIVED OFFERS
# ============================================================

def get_received_offers():
    d = graphql(
        """
        query {
          currentUser {
            pendingTokenOffersReceived(first:50) {
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
                  amounts {
                    eurCents
                    usdCents
                    referenceCurrency
                    wei
                  }

                  anyCards {
                    assetId
                    slug
                    collection
                  }
                }

                receiverSide {
                  amounts {
                    eurCents
                    usdCents
                    referenceCurrency
                    wei
                  }

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
        """
    )

    return (
        (((d or {}).get("data") or {})
         .get("currentUser") or {})
        .get("pendingTokenOffersReceived", {})
        .get("nodes", [])
    )


# ============================================================
# PENDING SENT OFFERS - RECONCILIATION
# ============================================================

def get_pending_sent_offers():
    """
    Recupera tutte le offerte inviate ancora visibili.

    La riconciliazione usa l'intero elenco invece di fare una
    richiesta separata per ogni pending locale.
    """

    d = graphql(
        """
        query {
          currentUser {
            pendingTokenOffersSent(first:100) {
              nodes {
                id
                blockchainId
                status
                type
                createdAt
                acceptedAt
                cancelledAt
                transactionDate

                sender {
                  slug
                }

                receiver {
                  slug
                }

                senderSide {
                  anyCards {
                    assetId
                    slug
                    name
                  }
                }

                receiverSide {
                  anyCards {
                    assetId
                    slug
                    name
                  }
                }
              }
            }
          }
        }
        """
    )

    return (
        (((d or {}).get("data") or {})
         .get("currentUser") or {})
        .get("pendingTokenOffersSent", {})
        .get("nodes", [])
    )


def get_offer_status(offer):
    return norm(
        offer.get("status")
    ).upper()


# ============================================================
# PENDING STATUS
# ============================================================

PENDING_STATUSES = {
    "PENDING",
    "OPEN",
    "AWAITING_RECEIVER",
    "AWAITING_COUNTERPARTY",
    "PROPOSED"
}

SUCCESS_STATUSES = {
    "ACCEPTED",
    "SETTLEMENT_PUBLISHED",
    "SETTLED",
    "COMPLETED"
}

FAILED_STATUSES = {
    "CANCELLED",
    "REJECTED",
    "REFUSED",
    "EXPIRED",
    "SETTLEMENT_FAILED"
}


# ============================================================
# CARDS
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

    d = graphql(
        """
        query($assetIds:[String!]!) {
          anyCards(assetIds:$assetIds) {
            assetId
            slug
            name
            rarityTyped
            seasonYear

            user {
              slug
            }

            tokenOwner {
              user {
                slug
              }
            }

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
        """,
        {
            "assetIds": ids
        }
    )

    return (
        ((d or {}).get("data") or {})
        .get("anyCards")
        or []
    )


def card_owned(c):
    me = norm(current_user_slug)

    return (
        norm(
            (c.get("user") or {}).get("slug")
        ) == me
        or
        norm(
            (
                (c.get("tokenOwner") or {})
                .get("user") or {}
            ).get("slug")
        ) == me
    )


# ============================================================
# CURRENCY
# ============================================================

def usd_eur():
    global usd_rate
    global usd_time

    if (
        usd_rate
        and time.time() - usd_time < USD_CACHE
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


def price_eur(a):
    if not isinstance(a, dict):
        return None

    try:
        x = int(
            a.get("eurCents")
        )

        if x > 0:
            return x

    except Exception:
        pass

    try:
        x = float(
            a.get("usdCents")
        )

    except Exception:
        return None

    rate = usd_eur()

    if x > 0 and rate:
        return int(
            round(x * rate)
        )

    return None


# ============================================================
# LIVE FLOOR
# ============================================================

def live_floor(card):
    p = card.get("anyPlayer") or {}

    ps = norm(
        p.get("slug")
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

    d = graphql(
        """
        query(
          $playerSlug:String,
          $first:Int
        ) {
          tokens {
            liveSingleSaleOffers(
              playerSlug:$playerSlug
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
            "playerSlug": ps,
            "first": 50
        }
    )

    offers = (
        (((d or {}).get("data") or {})
         .get("tokens") or {})
        .get("liveSingleSaleOffers", {})
        .get("nodes", [])
    )

    prices = []

    for o in offers:
        for c in (
            (o.get("senderSide") or {})
            .get("anyCards")
            or []
        ):
            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) == ps
                and
                norm(
                    c.get("rarityTyped")
                ) == rarity
                and
                int(
                    c.get("seasonYear") or -1
                ) == season
            ):
                x = price_eur(
                    (
                        o.get("receiverSide")
                        or {}
                    ).get("amounts") or {}
                )

                if x is not None:
                    prices.append(x)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# LISTINGS
# ============================================================

def listed_cards(asset_ids):
    ids = [
        str(x).strip()
        for x in asset_ids
        if x
    ]

    if not ids:
        return set()

    d = graphql(
        """
        query($assetIds:[String!]!) {
          tokens {
            liveSingleSaleOffers(
              assetIds:$assetIds
              first:100
            ) {
              nodes {
                senderSide {
                  anyCards {
                    assetId
                  }
                }
              }
            }
          }
        }
        """,
        {
            "assetIds": ids
        }
    )

    offers = (
        (((d or {}).get("data") or {})
         .get("tokens") or {})
        .get("liveSingleSaleOffers", {})
        .get("nodes", [])
    )

    wanted = {
        norm(x)
        for x in ids
    }

    result = set()

    for o in offers:
        for c in (
            (o.get("senderSide") or {})
            .get("anyCards")
            or []
        ):
            aid = norm(
                c.get("assetId")
            )

            if aid in wanted:
                result.add(aid)

    return result


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):
    global coverage_cache
    global coverage_time

    if (
        not force
        and coverage_cache
        and time.time() - coverage_time
        < COVERAGE_CACHE
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
            print(
                f"⚠️ Coverage HTTP "
                f"{r.status_code}",
                flush=True
            )

            return set(coverage_cache)

        html = r.text

        result = set(
            norm(x)
            for x in re.findall(
                r'(?:/football/'
                r'(?:leagues|competitions)/|'
                r'slug["\']?\s*[:=]\s*["\'])'
                r'([a-z0-9-]+)',
                html,
                re.I
            )
        )

        if result:
            coverage_cache = result
            coverage_time = time.time()

            print(
                f"🌐 Coverage: "
                f"{len(result)} competizioni",
                flush=True
            )

        else:
            print(
                "⚠️ Coverage non leggibile "
                "dal rendering HTML",
                flush=True
            )

        return set(coverage_cache)

    except Exception as e:
        print(
            f"⚠️ Coverage: {e}",
            flush=True
        )

        return set(coverage_cache)


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(c):
    wanted = {
        norm(KSLUG),
        norm(KASSET)
    }

    extra = os.getenv(
        "KULENOVIC_ID",
        ""
    ).strip()

    if extra:
        wanted.add(
            norm(extra)
        )

    return (
        norm(c.get("assetId")) in wanted
        or
        norm(c.get("slug")) in wanted
    )


# ============================================================
# VALIDATE CARD
# ============================================================

def validate_card(c):
    p = c.get("anyPlayer") or {}

    try:
        age = int(
            p.get("age")
        )
    except Exception:
        return False

    if age >= MAX_AGE:
        return False

    if (
        norm(c.get("rarityTyped")).upper()
        != "LIMITED"
    ):
        return False

    floor = live_floor(c)

    if (
        floor is None
        or floor < MIN_PRICE
        or floor > MAX_PRICE
    ):
        return False

    club = p.get("activeClub") or {}

    active = {
        norm(x.get("slug"))
        for x in (
            club.get("activeCompetitions")
            or []
        )
        if x.get("slug")
    }

    coverage = load_coverage()

    if (
        coverage
        and not active.intersection(coverage)
    ):
        return False

    return True


# ============================================================
# REJECT
# ============================================================

def reject_offer(o):
    bid = norm(
        o.get("blockchainId")
    )

    if not bid:
        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: reject simulato",
            flush=True
        )

        return True

    d = graphql(
        """
        mutation(
          $input:rejectOfferInput!
        ) {
          rejectOffer(input:$input) {
            tokenOffer {
              id
              status
            }

            errors {
              message
            }
          }
        }
        """,
        {
            "input": {
                "blockchainId": bid,
                "clientMutationId": str(
                    uuid.uuid4()
                )
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not r:
        return False

    if r.get("errors"):
        print(
            "❌ Reject:",
            r["errors"],
            flush=True
        )

        return False

    print(
        "✅ Offerta rifiutata",
        flush=True
    )

    return True


# ============================================================
# SIGN
# ============================================================

def sign_authorizations(auth):
    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node or not STARK:
        raise RuntimeError(
            "Node.js o Stark key mancanti"
        )

    script = r'''
const fs=require("fs");
const {
  signAuthorizationRequest
}=require("@sorare/crypto");

const input=JSON.parse(
  fs.readFileSync(0,"utf8")
);

function sign(a){
  const r=a.request;

  if(r.amount!=null)
    r.amount=BigInt(r.amount);

  const signature=
    signAuthorizationRequest(
      input.privateKey,
      r
    );

  if(
    r.__typename===
    "StarkexTransferAuthorizationRequest"
  )
    return {
      fingerprint:a.fingerprint,
      starkexTransferApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature
      }
    };

  if(
    r.__typename===
    "StarkexLimitOrderAuthorizationRequest"
  )
    return {
      fingerprint:a.fingerprint,
      starkexLimitOrderApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature
      }
    };

  if(
    r.__typename===
    "MangopayWalletTransferAuthorizationRequest"
  )
    return {
      fingerprint:a.fingerprint,
      mangopayWalletTransferApproval:{
        nonce:r.nonce,
        signature
      }
    };

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

    p = subprocess.run(
        [
            node,
            "-e",
            script
        ],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": auth
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
        )

    return json.loads(
        p.stdout
    )


# ============================================================
# CREATE OFFER
# ============================================================

def create_offer(
    receiver,
    send_ids,
    receive_ids,
    cash
):
    if not receiver:
        return None

    amount = max(
        0,
        int(cash)
    )

    d = graphql(
        """
        mutation(
          $input:prepareOfferInput!
        ) {
          prepareOffer(input:$input) {

            authorizations {
              fingerprint

              request {
                __typename

                ... on
                StarkexTransferAuthorizationRequest {
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

                ... on
                StarkexLimitOrderAuthorizationRequest {
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

                ... on
                MangopayWalletTransferAuthorizationRequest {
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
        """,
        {
            "input": {
                "receiveAssetIds": receive_ids,
                "sendAssetIds": send_ids,

                "sendAmount": {
                    "amount": str(amount),
                    "currency": "EUR"
                },

                "receiverSlug": receiver,

                "settlementCurrencies": [
                    "EUR"
                ],

                "clientMutationId": str(
                    uuid.uuid4()
                )
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not r or r.get("errors"):
        print(
            "❌ prepareOffer:",
            r.get("errors")
            if r
            else "errore",
            flush=True
        )

        return None

    auth = r.get(
        "authorizations"
    ) or []

    if not auth:
        return None

    try:
        approvals = sign_authorizations(
            auth
        )

    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True
        )

        return None

    d = graphql(
        """
        mutation(
          $input:createDirectOfferInput!
        ) {
          createDirectOffer(input:$input) {

            tokenOffer {
              id
              blockchainId
              status
              type
            }

            errors {
              message
            }
          }
        }
        """,
        {
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
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not r or r.get("errors"):
        print(
            "❌ createDirectOffer:",
            r.get("errors")
            if r
            else "errore",
            flush=True
        )

        return None

    return (
        (r.get("tokenOffer") or {})
        .get("id")
    )


# ============================================================
# AUTOBUY
# ============================================================

def process_autobuy(o):
    oid = norm(
        o.get("id")
    )

    if not oid or oid in processed:
        return

    wanted = (
        (o.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    if not any(
        is_kulenovic(c)
        for c in wanted
    ):
        return

    sender = (
        (o.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    ids = [
        c.get("assetId")
        for c in sender
        if c.get("assetId")
    ]

    if not ids:
        if reject_offer(o):
            mark_done(oid)

        return

    details = card_details(ids)

    valid = [
        c
        for c in details
        if validate_card(c)
    ]

    if not valid:
        print(
            "🚫 AUTOBUY: "
            "nessuna carta idonea",
            flush=True
        )

        if reject_offer(o):
            mark_done(oid)

        return

    print(
        f"✅ AUTOBUY: "
        f"{len(valid)}/{len(ids)} "
        f"carte idonee",
        flush=True
    )

    for c in valid:
        print(
            f"📥 AUTOBUY IDONEA: "
            f"{card_label(c)}",
            flush=True
        )

    receiver = norm(
        (o.get("sender") or {})
        .get("slug")
    )

    new_id = create_offer(
        receiver,
        [],
        [
            c["assetId"]
            for c in valid
        ],
        len(valid) * PAY_PER_CARD
    )

    if not new_id:
        print(
            "❌ AUTOBUY: "
            "impossibile creare "
            "la controproposta",
            flush=True
        )

        return

    add_pending(
        new_id,
        oid,
        valid
    )

    if reject_offer(o):
        mark_done(oid)

    print(
        f"⏳ AUTOBUY IN ATTESA: "
        f"{new_id}",
        flush=True
    )


# ============================================================
# VERIFY ACQUIRED CARDS
# ============================================================

def verify_acquired_cards(item, oid):
    """
    Verifica lo stato reale delle carte.

    Se tutte le carte sono effettivamente di proprietà
    dell'account, le sposta in acquired_cards.
    """

    ids = [
        c.get("assetId")
        for c in item.get("cards") or []
        if c.get("assetId")
    ]

    if not ids:
        print(
            f"⚠️ AutoBuy {oid}: "
            f"nessuna carta associata",
            flush=True
        )

        return False

    details = card_details(ids)

    if len(details) != len(ids):
        print(
            f"⏳ AutoBuy {oid}: "
            f"dettagli carte incompleti "
            f"({len(details)}/{len(ids)})",
            flush=True
        )

        return False

    not_owned = [
        c
        for c in details
        if not card_owned(c)
    ]

    if not_owned:
        print(
            f"⏳ AutoBuy {oid}: "
            f"carte non ancora possedute "
            f"({len(not_owned)}/{len(details)})",
            flush=True
        )

        return False

    with state_lock:
        for c in details:
            acquired_cards[
                norm(c["assetId"])
            ] = {
                "assetId": c["assetId"],
                "slug": c.get("slug"),
                "purchase_price_cents":
                    PAY_PER_CARD,
                "status": "da_vendere",
                "source": "autobuy",
                "offer_id": oid
            }

    persist()

    print(
        f"🎉 AUTOBUY COMPLETATO: "
        f"{oid} → "
        f"{len(details)} carte acquisite",
        flush=True
    )

    return True


# ============================================================
# RECONCILE MISSING PENDING
# ============================================================

def reconcile_missing_pending(item):
    """
    Il pending locale non è più presente tra le offerte
    inviate attualmente visibili.

    Prima controlla se le carte sono comunque diventate
    di proprietà.

    Se sì:
        acquired_cards + rimozione pending.

    Se no:
        il pending viene considerato UNKNOWN/non più
        riconciliabile e rimosso, evitando warning infiniti.
    """

    oid = item.get("offer_id")

    if not oid:
        return

    print(
        f"🔄 RICONCILIAZIONE AutoBuy {oid}: "
        f"offerta non presente tra i pending inviati",
        flush=True
    )

    # Prima controlliamo se l'acquisto è stato comunque
    # completato.
    if verify_acquired_cards(
        item,
        oid
    ):
        remove_pending(oid)
        return

    # Non risulta più pending e le carte non risultano
    # acquisite.
    #
    # Non lasciamo quindi un pending fantasma nello state.
    print(
        f"🧹 AutoBuy {oid}: "
        f"UNKNOWN/non riconciliabile → "
        f"rimozione dal pending locale",
        flush=True
    )

    remove_pending(oid)


# ============================================================
# CHECK / RECONCILE PENDING AUTOBUYS
# ============================================================

def check_pending_autobuys():
    """
    Riconcilia pending_autobuys con lo stato remoto.

    PENDING:
        rimane nello state.

    ACCEPTED / COMPLETED:
        verifica ownership.
        Se le carte sono possedute:
        acquired_cards + rimozione pending.

    REJECTED / CANCELLED / EXPIRED:
        rimozione pending.

    UNKNOWN / ID non trovato:
        controllo ownership.
        Se non acquisito, rimozione del pending locale
        per evitare warning infiniti.
    """

    with state_lock:
        pending = list(
            pending_autobuys.values()
        )

    if not pending:
        return

    print(
        f"🔎 Riconciliazione "
        f"{len(pending)} AutoBuy pending...",
        flush=True
    )

    try:
        offers = get_pending_sent_offers()

    except Exception as e:
        print(
            f"❌ Riconciliazione AutoBuy: "
            f"impossibile leggere le offerte Sorare: "
            f"{e}",
            flush=True
        )

        return

    # Una sola risposta remota viene indicizzata
    # per id GraphQL e blockchainId.
    remote = {}

    for offer in offers:
        for key in (
            offer.get("id"),
            offer.get("blockchainId")
        ):
            if key:
                remote[
                    norm(key)
                ] = offer

    for item in pending:
        oid = str(
            item.get("offer_id") or ""
        ).strip()

        if not oid:
            continue

        try:
            offer = remote.get(
                norm(oid)
            )

            # ------------------------------------------------
            # NON TROVATO
            # ------------------------------------------------

            if not offer:
                reconcile_missing_pending(
                    item
                )

                continue

            # ------------------------------------------------
            # STATO
            # ------------------------------------------------

            status = get_offer_status(
                offer
            )

            print(
                f"📦 AUTOBUY {oid} → "
                f"{status}",
                flush=True
            )

            # ------------------------------------------------
            # ANCORA PENDING
            # ------------------------------------------------

            if status in PENDING_STATUSES:
                continue

            # ------------------------------------------------
            # COMPLETATO
            # ------------------------------------------------

            if status in SUCCESS_STATUSES:

                if verify_acquired_cards(
                    item,
                    oid
                ):
                    remove_pending(
                        oid
                    )

                continue

            # ------------------------------------------------
            # FALLITO / TERMINATO
            # ------------------------------------------------

            if status in FAILED_STATUSES:
                print(
                    f"🗑️ AUTOBUY {oid}: "
                    f"stato finale {status} "
                    f"→ rimozione dal pending",
                    flush=True
                )

                remove_pending(
                    oid
                )

                continue

            # ------------------------------------------------
            # UNKNOWN
            # ------------------------------------------------

            print(
                f"⚠️ AUTOBUY {oid}: "
                f"stato Sorare sconosciuto "
                f"'{status}' → "
                f"nessuna azione distruttiva",
                flush=True
            )

        except Exception as e:
            print(
                f"❌ Riconciliazione AutoBuy "
                f"{oid}: {e}",
                flush=True
            )


# ============================================================
# SWAP
# ============================================================

def prepare_accept(oid):
    d = graphql(
        """
        query {
          config {
            exchangeRate {
              id
            }
          }
        }
        """
    )

    rate = (
        (((d or {}).get("data") or {})
         .get("config") or {})
        .get("exchangeRate", {})
        .get("id")
    )

    if not rate:
        return None, None

    d = graphql(
        """
        mutation(
          $input:prepareAcceptOfferInput!
        ) {
          prepareAcceptOffer(input:$input) {

            authorizations {
              fingerprint

              request {
                __typename

                ... on
                StarkexTransferAuthorizationRequest {
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

                ... on
                StarkexLimitOrderAuthorizationRequest {
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

                ... on
                MangopayWalletTransferAuthorizationRequest {
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
        """,
        {
            "input": {
                "offerId": oid,

                "settlementInfo": {
                    "currency": "WEI",
                    "paymentMethod": "WALLET",
                    "exchangeRateId": rate
                }
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("prepareAcceptOffer")
    )

    if not r or r.get("errors"):
        print(
            "❌ prepareAcceptOffer:",
            r.get("errors")
            if r
            else "errore",
            flush=True
        )

        return None, None

    return (
        r.get("authorizations") or [],
        rate
    )


def accept_offer(o):
    oid = norm(
        o.get("id")
    )

    auth, rate = prepare_accept(
        oid
    )

    if not auth:
        return False

    try:
        approvals = sign_authorizations(
            auth
        )

    except Exception as e:
        print(
            f"❌ Firma ACCEPT: {e}",
            flush=True
        )

        return False

    d = graphql(
        """
        mutation(
          $input:acceptOfferInput!
        ) {
          acceptOffer(input:$input) {

            tokenOffer {
              id
              status
            }

            errors {
              message
            }
          }
        }
        """,
        {
            "input": {
                "approvals": approvals,
                "offerId": oid,

                "settlementInfo": {
                    "currency": "WEI",
                    "paymentMethod": "WALLET",
                    "exchangeRateId": rate
                },

                "clientMutationId": str(
                    uuid.uuid4()
                )
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("acceptOffer")
    )

    if not r or r.get("errors"):
        print(
            "❌ ACCEPT:",
            r.get("errors")
            if r
            else "errore",
            flush=True
        )

        return False

    print(
        "✅ SWAP ACCETTATO",
        flush=True
    )

    return True


def process_swap(o):
    oid = norm(
        o.get("id")
    )

    if not oid or oid in processed:
        return

    sender = (
        (o.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    receiver = (
        (o.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    if not sender or not receiver:
        return

    give_ids = [
        c.get("assetId")
        for c in receiver
        if c.get("assetId")
    ]

    receive_ids = [
        c.get("assetId")
        for c in sender
        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:
        return

    print(
        f"🔍 SWAP {oid}: "
        f"mie carte nell'offerta="
        f"{len(give_ids)} "
        f"carte ricevute="
        f"{len(receive_ids)}",
        flush=True
    )

    give = card_details(
        give_ids
    )

    receive = card_details(
        receive_ids
    )

    if len(give) != len(give_ids):
        print(
            "⚠️ SWAP: impossibile recuperare "
            "tutte le mie carte",
            flush=True
        )

        return

    if len(receive) != len(receive_ids):
        print(
            "⚠️ SWAP: impossibile recuperare "
            "tutte le carte ricevute",
            flush=True
        )

        return

    # --------------------------------------------------------
    # KULENOVIC MAI CEDIBILE
    # --------------------------------------------------------

    if any(
        is_kulenovic(c)
        for c in give
    ):
        print(
            "🔒 SWAP RIFIUTATO: "
            "KULENOVIC NON È CEDIBILE",
            flush=True
        )

        if reject_offer(o):
            mark_done(oid)

        return

    # --------------------------------------------------------
    # SOLO CARTE MIE ATTUALMENTE IN VENDITA
    # --------------------------------------------------------

    listed = listed_cards(
        give_ids
    )

    eligible_give = []

    for c in give:
        aid = norm(
            c.get("assetId")
        )

        if aid in listed:
            eligible_give.append(c)

            print(
                f"🟢 CARTA CEDIBILE "
                f"(IN VENDITA): "
                f"{card_label(c)}",
                flush=True
            )

        else:
            print(
                f"⚪ CARTA ESCLUSA "
                f"(NON IN VENDITA): "
                f"{card_label(c)}",
                flush=True
            )

    if not eligible_give:
        print(
            "🚫 SWAP RIFIUTATO: "
            "nessuna delle mie carte "
            "presenti nell'offerta è "
            "attualmente in vendita",
            flush=True
        )

        if reject_offer(o):
            mark_done(oid)

        return

    # --------------------------------------------------------
    # VALORE CARTE CEDUTE
    # --------------------------------------------------------

    total_given = 0

    for c in eligible_give:
        floor = live_floor(c)

        if floor is None:
            print(
                f"⚠️ SWAP: impossibile "
                f"determinare il valore di "
                f"{card_label(c)}",
                flush=True
            )

            return

        total_given += floor

        print(
            f"📤 CEDO: "
            f"{card_label(c)} → "
            f"{format_eur(floor)}",
            flush=True
        )

    # --------------------------------------------------------
    # VALORE CARTE RICEVUTE
    # --------------------------------------------------------

    total_received = 0

    for c in receive:
        if not validate_card(c):
            print(
                f"🚫 SWAP: carta ricevuta "
                f"non valida → "
                f"{card_label(c)}",
                flush=True
            )

            if reject_offer(o):
                mark_done(oid)

            return

        floor = live_floor(c)

        if floor is None:
            print(
                f"⚠️ SWAP: impossibile "
                f"determinare il valore di "
                f"{card_label(c)}",
                flush=True
            )

            return

        total_received += floor

        print(
            f"📥 RICEVO: "
            f"{card_label(c)} → "
            f"{format_eur(floor)}",
            flush=True
        )

    # --------------------------------------------------------
    # CASH
    # --------------------------------------------------------

    cash = price_eur(
        (
            o.get("senderSide")
            or {}
        ).get("amounts") or {}
    ) or 0

    total_received += cash

    minimum = int(
        round(
            total_given * SWAP_MIN
        )
    )

    maximum = int(
        round(
            total_given * SWAP_MAX
        )
    )

    print(
        f"📤 Totale mie carte "
        f"rimaste nell'offerta: "
        f"{format_eur(total_given)}",
        flush=True
    )

    print(
        f"📥 Totale ricevuto: "
        f"{format_eur(total_received)}",
        flush=True
    )

    print(
        f"🎯 Minimo +20%: "
        f"{format_eur(minimum)}",
        flush=True
    )

    print(
        f"🎯 Massimo +25%: "
        f"{format_eur(maximum)}",
        flush=True
    )

    # --------------------------------------------------------
    # SOTTO +20% → CONTROPROPOSTA
    # --------------------------------------------------------

    if total_received < minimum:
        missing = minimum - total_received

        print(
            f"💰 SWAP sotto +20% → "
            f"richiedo altri "
            f"{format_eur(missing)}",
            flush=True
        )

        receiver_slug = norm(
            (o.get("sender") or {})
            .get("slug")
        )

        send_ids = [
            c.get("assetId")
            for c in eligible_give
            if c.get("assetId")
        ]

        receive_ids = [
            c.get("assetId")
            for c in receive
            if c.get("assetId")
        ]

        new_id = create_offer(
            receiver_slug,
            send_ids,
            receive_ids,
            missing
        )

        if new_id:
            print(
                f"✅ CONTROPROPOSTA "
                f"SWAP INVIATA: "
                f"{new_id}",
                flush=True
            )

            mark_done(oid)

        else:
            print(
                "❌ SWAP: "
                "controproposta non creata",
                flush=True
            )

        return

    # --------------------------------------------------------
    # OLTRE +25% → RIFIUTO
    # --------------------------------------------------------

    if total_received > maximum:
        print(
            "🚫 SWAP oltre +25% → rifiuto",
            flush=True
        )

        if reject_offer(o):
            mark_done(oid)

        return

    # --------------------------------------------------------
    # DENTRO +20/+25% → ACCEPT
    # --------------------------------------------------------

    if (
        SWAP_AUTO_ACCEPT
        and accept_offer(o)
    ):
        for c in receive:
            acquired_cards[
                norm(c["assetId"])
            ] = {
                "assetId": c["assetId"],
                "slug": c.get("slug"),
                "purchase_price_cents":
                    live_floor(c),
                "status": "da_vendere",
                "source": "swap",
                "offer_id": oid
            }

        persist()
        mark_done(oid)


# ============================================================
# DISPATCH
# ============================================================

def process_offer(o):
    receiver = (
        (o.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    sender = (
        (o.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    # Se Kulenovic è richiesto:
    # AUTOBUY sempre.
    if any(
        is_kulenovic(c)
        for c in receiver
    ):
        process_autobuy(o)
        return

    # Se è uno swap:
    if sender and receiver:
        process_swap(o)


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
        "🟢 SWAP: cedibili solo le mie "
        "carte presenti nell'offerta "
        "e attualmente in vendita",
        flush=True
    )

    print(
        "⚪ SWAP: mie carte non in vendita "
        "→ escluse dalla controproposta",
        flush=True
    )

    print(
        "💰 SWAP: +20% calcolato solo "
        "sulle mie carte rimaste "
        "nell'offerta",
        flush=True
    )

    print(
        "🚫 SWAP: nessuna carta mia "
        "in vendita nell'offerta "
        "→ rifiuto",
        flush=True
    )

    print(
        "🔒 KULENOVIC: MAI CEDIBILE",
        flush=True
    )

    print(
        "🎯 KULENOVIC RICHIESTO "
        "→ SEMPRE AUTOBUY",
        flush=True
    )

    print(
        "💾 STATE: "
        "bot_state.json + GitHub",
        flush=True
    )

    print(
        "🔄 PENDING: "
        "riconciliazione stato reale",
        flush=True
    )

    # --------------------------------------------------------
    # LOAD STATE
    # --------------------------------------------------------

    load_local_state()
    load_github_state()

    save_local_state()

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    coverage = load_coverage(
        True
    )

    if coverage:
        print(
            f"🏆 Competizioni Football "
            f"coperte: {len(coverage)}",
            flush=True
        )

    else:
        print(
            "🟡 Coverage non disponibile: "
            "continuo comunque",
            flush=True
        )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():
        print(
            "❌ Account Sorare "
            "non disponibile",
            flush=True
        )

        return

    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    while True:
        try:
            # Prima riconcilia gli AutoBuy creati
            # dal bot.
            check_pending_autobuys()

            # Poi controlla nuove offerte ricevute.
            offers = get_received_offers()

            print(
                f"📨 Offerte ricevute pendenti: "
                f"{len(offers)}",
                flush=True
            )

            for o in offers:
                try:
                    process_offer(o)

                except Exception as e:
                    print(
                        f"❌ Errore offerta: {e}",
                        flush=True
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


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
                    SWAP_MAX,
                "listed_cards_only":
                    True,
                "under_minimum":
                    "counteroffer_cash"
            },

            "pending_reconciliation": True,

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


@app.get("/favicon.ico")
def favicon():
    return Response(
        status=204
    )


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
