import os
import time
import uuid
import json
import base64
import shutil
import subprocess
import threading
import requests

from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

SORARE_URL = "https://api.sorare.com/graphql"
STATE_FILE = "bot_state.json"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
EMAIL = os.getenv("SORARE_EMAIL", "").strip()
PASSWORD = os.getenv("SORARE_PASSWORD", "").strip()
TOTP_SECRET = os.getenv("SORARE_TOTP_SECRET", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv(
    "GITHUB_REPO",
    "fisiogiordano-hub/bot-sorare"
).strip()
GITHUB_BRANCH = os.getenv(
    "GITHUB_BRANCH",
    "main"
).strip()

DRY_RUN = os.getenv(
    "DRY_RUN",
    "false"
).lower() == "true"


# ============================================================
# AUTOBUY
# ============================================================

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70
PAY_PER_CARD = 20       # €0.20
MAX_AGE = 28
MIN_LIVE_LISTINGS = 5


# ============================================================
# SWAP
# ============================================================

SWAP_AUTO_ACCEPT = True
SWAP_MIN = 1.20
SWAP_MAX = 1.25


# ============================================================
# GENERAL
# ============================================================

INTERVAL = 10
TIMEOUT = 25
USD_CACHE = 300

BOT_VERSION = "23.9-LIGHT-SWAP-GLOBAL-FLOOR-FIXED-2"


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
)


# ============================================================
# STATE
# ============================================================

processed = {}
acquired_cards = {}
pending_autobuys = {}

state_lock = threading.Lock()
github_lock = threading.Lock()
worker_lock = threading.Lock()

worker_started = False

usd_rate = None
usd_time = 0

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
    if c is None:
        return "N/D"

    return f"€{c / 100:.2f}"

    
def generate_totp_code(secret):
    if not secret:
        return None
    try:
        import pyotp
        totp = pyotp.TOTP(secret.replace(" ", ""))
        return totp.now()
    except Exception as e:
        print(f"⚠️ Errore generazione TOTP: {e}", flush=True)
        return None


def auto_login():
    global TOKEN
    if not EMAIL or not PASSWORD:
        print("❌ Impossibile eseguire auto_login: EMAIL o PASSWORD mancanti nelle variabili d'ambiente.", flush=True)
        return False

    totp_code = generate_totp_code(TOTP_SECRET)
    print("🔑 Tentativo di login automatico con credenziali...", flush=True)

    mutation = """
    mutation SignIn($input: signInInput!) {
      signIn(input: $input) {
        currentUser {
          slug
        }
        jwt {
          token
        }
        otpRequired
        errors {
          message
        }
      }
    }
    """
    
    payload = {
        "email": EMAIL,
        "password": PASSWORD
    }
    if totp_code:
        payload["otpNumber"] = totp_code

    try:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
        }
        if AUD:
            h["JWT-AUD"] = AUD

        r = requests.post(
            SORARE_URL,
            json={"query": mutation, "variables": {"input": payload}},
            headers=h,
            timeout=TIMEOUT
        )

        print(f"🌐 Auto-login HTTP {r.status_code}", flush=True)

        if r.status_code == 200:
            res = r.json()
            if res.get("errors"):
                print(f"❌ Auto-login GraphQL Errors: {res['errors']}", flush=True)
                return False
                
            data = res.get("data", {}).get("signIn", {})
            if data.get("errors"):
                print(f"❌ Auto-login SignIn Errors: {data['errors']}", flush=True)
                return False

            new_token = (data.get("jwt") or {}).get("token")
            if new_token:
                TOKEN = new_token
                print("✅ Login automatico riuscito! Nuovo JWT Token generato.", flush=True)
                return True
            else:
                print(f"❌ Auto-login fallito, risposta completa: {res}", flush=True)
        else:
            print(f"❌ Auto-login HTTP error text: {r.text[:500]}", flush=True)
    except Exception as e:
        print(f"❌ Errore critico durante auto_login: {e}", flush=True)

    return False

def headers():
    if not TOKEN:
        # Tenta il login automatico se il token manca del tutto
        if EMAIL and PASSWORD:
            print("⚠️ Token assente, provo il login automatico...", flush=True)
            if auto_login():
                pass

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato e auto_login fallito."
        )

    h = {
        "Authorization": (
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else "Bearer " + TOKEN
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-Bot/{BOT_VERSION}"
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):
    """
    Esegue una richiesta GraphQL.

    Restituisce None solamente se la richiesta fallisce
    completamente dopo i tentativi disponibili.

    IMPORTANTE:
    Sorare può restituire HTTP 200 con un campo "errors".
    In quel caso il payload viene comunque restituito.
    """

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
                    retry_after = int(
                        r.headers.get(
                            "Retry-After",
                            attempt + 2
                        )
                    )
                except Exception:
                    retry_after = attempt + 2

                time.sleep(
                    min(retry_after, 15)
                )

                continue

            if r.status_code != 200:

                print(
                    f"❌ Sorare HTTP {r.status_code}: "
                    f"{r.text[:1000]}",
                    flush=True
                )

                time.sleep(attempt + 1)

                continue

            try:
                d = r.json()

            except Exception as e:

                print(
                    f"❌ JSON Sorare non valido: {e}",
                    flush=True
                )

                time.sleep(attempt + 1)

                continue

            if d.get("errors"):
                err_msg = json.dumps(d["errors"], ensure_ascii=False)
                print(
                    "❌ GraphQL:",
                    err_msg[:5000],
                    flush=True
                )

                if "UNAUTHORIZED" in err_msg or "revoked token" in err_msg or "wrong aud" in err_msg:
                    if EMAIL and PASSWORD and attempt == 0:
                        print("🔄 Token non valido/revocato. Tento il rinnovo automatico con credenziali...", flush=True)
                        if auto_login():
                            continue

            return d

        except Exception as e:

            print(
                f"❌ GraphQL: {e}",
                flush=True
            )

            time.sleep(attempt + 1)

    return None


# ============================================================
# NORMALIZATION
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
        x.get("offer_id")
        or ""
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
        "status": x.get(
            "status",
            "PENDING"
        )
    }


# ============================================================
# STATE
# ============================================================

def build_state():

    with state_lock:

        return {
            "processed_offers": list(
                processed.keys()
            ),
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

    processed = {}

    for x in (
        d.get("processed_offers")
        or []
    ):

        if x:
            processed[norm(x)] = True

    acquired_cards = {}

    for x in (
        d.get("acquired_cards")
        or []
    ):

        c = normalize_card(x)

        if c:
            acquired_cards[
                norm(c["assetId"])
            ] = c

    pending_autobuys = {}

    for x in (
        d.get("pending_autobuys")
        or []
    ):

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

            load_state_data(
                json.load(f)
            )

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

        os.replace(
            tmp,
            STATE_FILE
        )

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

        if r.status_code != 200:
            return

        content = r.json().get(
            "content"
        )

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

            data = {
                "message": (
                    f"Update bot_state.json "
                    f"{int(time.time())}"
                ),
                "content": enc,
                "branch": GITHUB_BRANCH
            }

            if r.status_code == 200:

                data["sha"] = (
                    r.json().get("sha")
                )

            r = requests.put(
                github_url(),
                headers=github_headers(),
                json=data,
                timeout=TIMEOUT
            )

            return r.status_code in (
                200,
                201
            )

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


# ============================================================
# PROCESSED OFFERS
# ============================================================

def mark_done(oid):

    if not oid:
        return

    with state_lock:
        processed[norm(oid)] = True

    persist()


# ============================================================
# PENDING AUTOBUY
# ============================================================

def add_pending(
    oid,
    original,
    cards
):

    p = {
        "offer_id": oid,
        "original_offer_id": original,
        "created_at": int(time.time()),
        "cards": [
            {
                "assetId": c.get(
                    "assetId"
                ),
                "slug": c.get(
                    "slug"
                ),
                "name": c.get(
                    "name"
                )
            }
            for c in cards
        ],
        "price_per_card": PAY_PER_CARD,
        "status": "PENDING"
    }

    with state_lock:

        pending_autobuys[
            norm(oid)
        ] = p

    persist()


def remove_pending(
    oid,
    reason=None
):

    removed = None

    with state_lock:

        removed = pending_autobuys.pop(
            norm(oid),
            None
        )

    if removed:

        if reason:
            print(
                f"🧹 PENDING RIMOSSO: {oid} → "
                f"{reason}",
                flush=True
            )
        else:
            print(
                f"🧹 PENDING RIMOSSO: {oid}",
                flush=True
            )

        persist()

        return True

    return False


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    global current_user_slug

    d = graphql(
        "query{currentUser{slug nickname starkKey}}"
    )

    u = (
        ((d or {}).get("data") or {})
        .get("currentUser")
    )

    if not u:
        return False

    current_user_slug = u.get(
        "slug"
    )

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

    d = graphql("""
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
        (((d or {}).get("data") or {})
        .get("currentUser") or {})
        .get("pendingTokenOffersReceived") or {}
    ).get(
        "nodes",
        []
    )


# ============================================================
# SENT PENDING OFFERS
# ============================================================

def get_pending_sent_offers():

    d = graphql("""
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

    if d is None:
        return None

    current_user = (
        ((d.get("data") or {})
        .get("currentUser"))
    )

    if current_user is None:
        return None

    container = (
        current_user
        .get("pendingTokenOffersSent")
    )

    if container is None:
        return None

    return container.get(
        "nodes",
        []
    )


def get_pending_sent_offer(
    offer_id,
    offers=None
):

    if offers is None:
        offers = get_pending_sent_offers()

    if offers is None:
        return None

    target = norm(offer_id)

    for o in offers:

        if norm(o.get("id")) == target:
            return o

        if norm(o.get("blockchainId")) == target:
            return o

    return None


# ============================================================
# CARD DETAILS
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

    d = graphql("""
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
          name
          displayName
        }
      }
    }
  }
}
""", {
        "assetIds": ids
    })

    return (
        ((d or {}).get("data") or {})
        .get("anyCards") or []
    )


def card_owned(c):

    me = norm(
        current_user_slug
    )

    return (
        norm(
            (c.get("user") or {})
            .get("slug")
        ) == me
        or
        norm(
            ((c.get("tokenOwner") or {})
             .get("user") or {})
            .get("slug")
        ) == me
    )


# ============================================================
# USD / EUR
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

        rate = float(
            requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from": "USD",
                    "to": "EUR"
                },
                timeout=10
            ).json()["rates"]["EUR"]
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

    if (
        x > 0
        and rate
    ):
        return int(
            round(x * rate)
        )

    return None


# ============================================================
# SPECIFIC SEASON FLOOR
# ============================================================

def live_floor(card):

    p = card.get(
        "anyPlayer"
    ) or {}

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

    if not ps:
        return None

    d = graphql("""
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
        "playerSlug": ps,
        "first": 50
    })

    if d is None:
        return None

    data = (
        d.get("data")
        if isinstance(d, dict)
        else None
    )

    if not isinstance(data, dict):
        return None

    tokens = data.get("tokens")

    if not isinstance(tokens, dict):
        return None

    container = tokens.get(
        "liveSingleSaleOffers"
    )

    if not isinstance(container, dict):
        return None

    offers = container.get(
        "nodes",
        []
    )

    if not isinstance(offers, list):
        return None

    prices = []

    for o in offers:

        if not isinstance(o, dict):
            continue

        for c in (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        ):

            try:
                same_season = (
                    int(
                        c.get("seasonYear")
                        or -1
                    ) == season
                )
            except Exception:
                same_season = False

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
                same_season
            ):

                x = price_eur(
                    (o.get("receiverSide") or {})
                    .get("amounts") or {}
                )

                if x is not None:
                    prices.append(x)

                break

    if len(prices) < MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# GLOBAL CROSS-SEASON FLOOR
# ============================================================

def has_low_global_limited(card):
    """
    REGOLA B AUTOBUY:

    NON comprare la carta se esiste almeno una
    Limited dello stesso giocatore, di QUALSIASI stagione,
    con floor/listing sotto €0.32.

    Se Sorare non permette di verificare correttamente
    il mercato, viene applicato fail-safe:
    AUTOBUY BLOCCATO.
    """

    p = card.get(
        "anyPlayer"
    ) or {}

    ps = norm(
        p.get("slug")
    )

    if not ps:

        print(
            f"⚠️ GLOBAL FLOOR: player slug "
            f"mancante per {card_label(card)}",
            flush=True
        )

        return True

    d = graphql("""
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
            slug
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
        "playerSlug": ps,
        "first": 100
    })

    if d is None:

        print(
            f"⚠️ GLOBAL FLOOR: "
            f"impossibile interrogare il mercato "
            f"per {card_label(card)}",
            flush=True
        )

        return True

    data = (
        d.get("data")
        if isinstance(d, dict)
        else None
    )

    if not isinstance(data, dict):

        print(
            f"⚠️ GLOBAL FLOOR: "
            f"risposta senza data "
            f"per {card_label(card)}",
            flush=True
        )

        return True

    tokens = data.get("tokens")

    if not isinstance(tokens, dict):

        print(
            f"⚠️ GLOBAL FLOOR: "
            f"campo tokens assente "
            f"per {card_label(card)}",
            flush=True
        )

        return True

    container = tokens.get(
        "liveSingleSaleOffers"
    )

    if not isinstance(container, dict):

        print(
            f"⚠️ GLOBAL FLOOR: "
            f"liveSingleSaleOffers assente "
            f"per {card_label(card)}",
            flush=True
        )

        return True

    offers = container.get(
        "nodes",
        []
    )

    if not isinstance(offers, list):

        print(
            f"⚠️ GLOBAL FLOOR: "
            f"nodes non disponibili "
            f"per {card_label(card)}",
            flush=True
        )

        return True

    if not offers:

        print(
            f"⚠️ GLOBAL FLOOR: nessuna offerta live "
            f"recuperata per {card_label(card)}",
            flush=True
        )

        return True

    for o in offers:

        if not isinstance(o, dict):
            continue

        price = price_eur(
            (o.get("receiverSide") or {})
            .get("amounts") or {}
        )

        if price is None:
            continue

        cards = (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        )

        for c in cards:

            player_slug = norm(
                (c.get("anyPlayer") or {})
                .get("slug")
            )

            rarity = norm(
                c.get("rarityTyped")
            ).upper()

            if (
                player_slug == ps
                and
                rarity == "LIMITED"
                and
                price < MIN_PRICE
            ):

                print(
                    f"🚫 GLOBAL FLOOR: "
                    f"{card_label(card)} → "
                    f"trovata Limited "
                    f"{c.get('seasonYear') or 'N/D'} "
                    f"a {format_eur(price)} "
                    f"(< {format_eur(MIN_PRICE)})",
                    flush=True
                )

                return True

    print(
        f"🟢 GLOBAL FLOOR OK: "
        f"{card_label(card)} → "
        f"nessuna Limited del giocatore "
        f"sotto {format_eur(MIN_PRICE)}",
        flush=True
    )

    return False


# ============================================================
# LISTED CARDS
# ============================================================

def listed_cards(asset_ids):
    """
    Verifica quali asset sono attualmente presenti
    nelle live single sale di Sorare.

    IMPORTANTE:
    liveSingleSaleOffers NON accetta assetIds
    nello schema GraphQL che stiamo utilizzando.

    Pertanto:
      1. recuperiamo le live sale;
      2. filtriamo localmente gli assetId richiesti.

    Return:
      set() -> query riuscita, nessun asset trovato
      set(ids) -> asset trovati
      None -> impossibile verificare il mercato
    """

    ids = [
        str(x).strip()
        for x in asset_ids
        if x
    ]

    if not ids:
        return set()

    wanted = {
        norm(x)
        for x in ids
    }

    d = graphql("""
query($first:Int){
  tokens{
    liveSingleSaleOffers(
      first:$first
    ){
      nodes{
        senderSide{
          anyCards{
            assetId
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
        "first": 100
    })

    if d is None:

        print(
            "⚠️ LISTED CARDS: "
            "query mercato fallita",
            flush=True
        )

        return None

    data = (
        d.get("data")
        if isinstance(d, dict)
        else None
    )

    if not isinstance(data, dict):

        print(
            "⚠️ LISTED CARDS: "
            "risposta GraphQL senza data",
            flush=True
        )

        return None

    tokens = data.get("tokens")

    if not isinstance(tokens, dict):

        print(
            "⚠️ LISTED CARDS: "
            "campo tokens assente",
            flush=True
        )

        return None

    container = tokens.get(
        "liveSingleSaleOffers"
    )

    if not isinstance(container, dict):

        print(
            "⚠️ LISTED CARDS: "
            "liveSingleSaleOffers assente",
            flush=True
        )

        return None

    offers = container.get(
        "nodes"
    )

    if not isinstance(offers, list):

        print(
            "⚠️ LISTED CARDS: "
            "nodes non disponibile",
            flush=True
        )

        return None

    result = set()

    for o in offers:

        if not isinstance(o, dict):
            continue

        cards = (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        )

        for c in cards:

            if not isinstance(c, dict):
                continue

            aid = norm(
                c.get("assetId")
            )

            if aid in wanted:

                result.add(aid)

    print(
        f"🔎 LISTED CARDS: "
        f"{len(result)}/{len(wanted)} "
        f"carte richieste trovate in vendita",
        flush=True
    )

    return result


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
        norm(
            c.get("assetId")
        ) in wanted
        or
        norm(
            c.get("slug")
        ) in wanted
    )
