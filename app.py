import base64
import json
import os
import shutil
import subprocess
import threading
import time
from flask import Flask, jsonify
import pyotp
import requests

app = Flask(__name__)


# ============================================================
# CONFIG & 2FA LOGIN AUTOMATICO
# ============================================================

SORARE_URL = "https://api.sorare.com/graphql"
STATE_FILE = "bot_state.json"

EMAIL = os.getenv("SORARE_EMAIL", "").strip()
PASSWORD = os.getenv("SORARE_PASSWORD", "").strip()
TOTP_SECRET = os.getenv("SORARE_TOTP_SECRET", "").strip()

STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "fisiogiordano-hub/bot-sorare").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def get_fresh_sorare_token():
  """Effettua automaticamente il login a due passaggi su Sorare e restituisce un JWT valido."""
  if not EMAIL or not PASSWORD or not TOTP_SECRET:
    print(
        "⚠️ Credenziali o TOTP Secret mancanti nelle variabili d'ambiente."
        " Utilizzo token fisso se disponibile...",
        flush=True,
    )
    return os.getenv("SORARE_JWT_TOKEN", "").strip()

  print("🔐 Generazione automatica del token JWT Sorare in corso...", flush=True)

  headers = {
      "Content-Type": "application/json",
      "User-Agent": "Sorare-Bot/AutoLogin",
  }

  # --- STEP 1: Ottenere la sfida (otpSessionChallenge) ---
  query_step1 = """
    mutation SignInStep1($input: signInInput!) {
        signIn(input: $input) {
            otpSessionChallenge
            errors { message }
        }
    }
    """
  payload_step1 = {
      "query": query_step1,
      "variables": {"input": {"email": EMAIL, "password": PASSWORD}},
  }

  try:
    res1 = requests.post(SORARE_URL, json=payload_step1, headers=headers).json()
    challenge = (
        res1.get("data", {}).get("signIn", {}).get("otpSessionChallenge")
    )

    if not challenge:
      print(f"❌ Errore Step 1 login Sorare: {res1}", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

    # --- STEP 2: Generare OTP e completare il login ---
    totp = pyotp.TOTP(TOTP_SECRET)
    current_otp = totp.now()

    query_step2 = """
        mutation SignInStep2($input: signInInput!) {
            signIn(input: $input) {
                jwtToken { token }
                errors { message }
            }
        }
        """
    input_step2 = {
        "email": EMAIL,
        "password": PASSWORD,
        "otpAttempt": current_otp,
        "otpSessionChallenge": challenge,
    }
    payload_step2 = {
        "query": query_step2,
        "variables": {"input": input_step2},
    }

    res2 = requests.post(SORARE_URL, json=payload_step2, headers=headers).json()
    sign_in_data = res2.get("data", {}).get("signIn", {})
    errors = sign_in_data.get("errors", [])

    if errors:
      print(f"❌ Errore Step 2 login Sorare: {errors}", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

    token = sign_in_data.get("jwtToken", {}).get("token")
    if token:
      print(
          "✅ Nuovo token JWT Sorare generato con successo in automatico!",
          flush=True,
      )
      return token
  except Exception as e:
    print(f"❌ Eccezione durante il login automatico: {e}", flush=True)

  return os.getenv("SORARE_JWT_TOKEN", "").strip()


# Token dinamico aggiornato all'avvio
TOKEN = get_fresh_sorare_token()


# ============================================================
# AUTOBUY
# ============================================================

MIN_PRICE = 32  # €0.32
MAX_PRICE = 70  # €0.70
PAY_PER_CARD = 20  # €0.20
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


def headers():
  if not TOKEN:
    raise RuntimeError("SORARE_JWT_TOKEN non configurato")

  return {
      "Authorization": (
          TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer " + TOKEN
      ),
      "Content-Type": "application/json",
      "Accept": "application/json",
      "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
  }


# ============================================================
# GRAPHQL
# ============================================================


def graphql(query, variables=None):
  """Esegue una richiesta GraphQL.

  Restituisce None solamente se la richiesta fallisce completamente dopo i
  tentativi disponibili.

  IMPORTANTE: Sorare può restituire HTTP 200 con un campo "errors". In quel caso
  il payload viene comunque restituito.
  """

  for attempt in range(3):

    try:
      r = requests.post(
          SORARE_URL,
          json={"query": query, "variables": variables or {}},
          headers=headers(),
          timeout=TIMEOUT,
      )

      print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

      if r.status_code == 429:

        try:
          retry_after = int(r.headers.get("Retry-After", attempt + 2))
        except Exception:
          retry_after = attempt + 2

        time.sleep(min(retry_after, 15))

        continue

      if r.status_code != 200:

        print(
            f"❌ Sorare HTTP {r.status_code}: {r.text[:1000]}",
            flush=True,
        )

        time.sleep(attempt + 1)

        continue

      try:
        d = r.json()

      except Exception as e:

        print(f"❌ JSON Sorare non valido: {e}", flush=True)

        time.sleep(attempt + 1)

        continue

      if d.get("errors"):

        print(
            "❌ GraphQL:",
            json.dumps(d["errors"], ensure_ascii=False)[:5000],
            flush=True,
        )

      return d

    except Exception as e:

      print(f"❌ GraphQL: {e}", flush=True)

      time.sleep(attempt + 1)

  return None


# ============================================================
# NORMALIZATION
# ============================================================


def normalize_card(x):

  if not isinstance(x, dict):
    return None

  aid = str(x.get("assetId") or x.get("asset_id") or "").strip()

  if not aid:
    return None

  return {
      "assetId": aid,
      "slug": x.get("slug"),
      "purchase_price_cents": x.get("purchase_price_cents"),
      "status": x.get("status") or "da_vendere",
      "source": x.get("source") or "unknown",
      "offer_id": x.get("offer_id"),
  }


def normalize_pending(x):

  if not isinstance(x, dict):
    return None

  oid = str(x.get("offer_id") or "").strip()

  if not oid:
    return None

  return {
      "offer_id": oid,
      "original_offer_id": x.get("original_offer_id"),
      "created_at": x.get("created_at"),
      "cards": x.get("cards") or [],
      "price_per_card": x.get("price_per_card", PAY_PER_CARD),
      "status": x.get("status", "PENDING"),
  }


# ============================================================
# STATE
# ============================================================


def build_state():

  with state_lock:

    return {
        "processed_offers": list(processed.keys()),
        "acquired_cards": list(acquired_cards.values()),
        "pending_autobuys": list(pending_autobuys.values()),
        "updated_at": int(time.time()),
    }


def load_state_data(d):

  global processed
  global acquired_cards
  global pending_autobuys

  if not isinstance(d, dict):
    return

  processed = {}

  for x in d.get("processed_offers") or []:

    if x:
      processed[norm(x)] = True

  acquired_cards = {}

  for x in d.get("acquired_cards") or []:

    c = normalize_card(x)

    if c:
      acquired_cards[norm(c["assetId"])] = c

  pending_autobuys = {}

  for x in d.get("pending_autobuys") or []:

    p = normalize_pending(x)

    if p:
      pending_autobuys[norm(p["offer_id"])] = p


def load_local_state():

  if not os.path.exists(STATE_FILE):
    return

  try:

    with open(STATE_FILE, encoding="utf-8") as f:

      load_state_data(json.load(f))

  except Exception as e:

    print(f"⚠️ Lettura {STATE_FILE}: {e}", flush=True)


def save_local_state():

  try:

    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:

      json.dump(build_state(), f, indent=2, ensure_ascii=False)

    os.replace(tmp, STATE_FILE)

    return True

  except Exception as e:

    print(f"❌ Salvataggio {STATE_FILE}: {e}", flush=True)

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
      "User-Agent": f"Sorare-Bot/{BOT_VERSION}",
  }


def github_url():
  return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"


def load_github_state():

  if not GITHUB_TOKEN:
    return

  try:

    r = requests.get(
        github_url(),
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=TIMEOUT,
    )

    if r.status_code != 200:
      return

    content = r.json().get("content")

    if not content:
      return

    d = json.loads(base64.b64decode(content.replace("\n", "")).decode())

    with state_lock:
      load_state_data(d)

    print(
        f"💾 GitHub: {len(processed)} offerte | {len(acquired_cards)} carte |"
        f" {len(pending_autobuys)} pending",
        flush=True,
    )

  except Exception as e:

    print(f"⚠️ GitHub load: {e}", flush=True)


def save_github_state():

  if not GITHUB_TOKEN:
    return False

  with github_lock:

    try:

      raw = json.dumps(build_state(), indent=2, ensure_ascii=False)

      enc = base64.b64encode(raw.encode()).decode()

      r = requests.get(
          github_url(),
          headers=github_headers(),
          params={"ref": GITHUB_BRANCH},
          timeout=TIMEOUT,
      )

      data = {
          "message": f"Update bot_state.json {int(time.time())}",
          "content": enc,
          "branch": GITHUB_BRANCH,
      }

      if r.status_code == 200:

        data["sha"] = r.json().get("sha")

      r = requests.put(
          github_url(), headers=github_headers(), json=data, timeout=TIMEOUT
      )

      return r.status_code in (200, 201)

    except Exception as e:

      print(f"❌ GitHub save: {e}", flush=True)

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


def add_pending(oid, original, cards):

  p = {
      "offer_id": oid,
      "original_offer_id": original,
      "created_at": int(time.time()),
      "cards": [
          {
              "assetId": c.get("assetId"),
              "slug": c.get("slug"),
              "name": c.get("name"),
          }
          for c in cards
      ],
      "price_per_card": PAY_PER_CARD,
      "status": "PENDING",
  }

  with state_lock:

    pending_autobuys[norm(oid)] = p

  persist()


def remove_pending(oid, reason=None):

  removed = None

  with state_lock:

    removed = pending_autobuys.pop(norm(oid), None)

  if removed:

    if reason:
      print(f"🧹 PENDING RIMOSSO: {oid} → {reason}", flush=True)
    else:
      print(f"🧹 PENDING RIMOSSO: {oid}", flush=True)

    persist()

    return True

  return False


# ============================================================
# ACCOUNT
# ============================================================


def check_account():

  global current_user_slug

  d = graphql("query{currentUser{slug nickname starkKey}}")

  u = (((d or {}).get("data") or {}).get("currentUser"))

  if not u:
    return False

  current_user_slug = u.get("slug")

  print(f"✅ Sorare: {u.get('nickname') or current_user_slug}", flush=True)

  print(
      "🔐 Stark key account: "
      + ("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),
      flush=True,
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
      ((d or {}).get("data") or {})
      .get("currentUser", {})
      .get("pendingTokenOffersReceived", {})
  ).get("nodes", [])


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

  current_user = ((d.get("data") or {}).get("currentUser"))

  if current_user is None:
    return None

  container = current_user.get("pendingTokenOffersSent")

  if container is None:
    return None

  return container.get("nodes", [])


def get_pending_sent_offer(offer_id, offers=None):

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

  ids = list(dict.fromkeys(str(x).strip() for x in ids if x))

  if not ids:
    return []

  d = graphql(
      """
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
""",
      {"assetIds": ids},
  )

  return ((d or {}).get("data") or {}).get("anyCards") or []


def card_owned(c):

  me = norm(current_user_slug)

  return (
      norm(((c.get("user") or {}).get("slug"))) == me
      or norm(((c.get("tokenOwner") or {}).get("user") or {}).get("slug")) == me
  )


# ============================================================
# USD / EUR
# ============================================================


def usd_eur():

  global usd_rate
  global usd_time

  if usd_rate and time.time() - usd_time < USD_CACHE:
    return usd_rate

  try:

    rate = float(
        requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10,
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

    x = int(a.get("eurCents"))

    if x > 0:
      return x

  except Exception:
    pass

  try:

    x = float(a.get("usdCents"))

  except Exception:
    return None

  rate = usd_eur()

  if x > 0 and rate:
    return int(round(x * rate))

  return None


# ============================================================
# SPECIFIC SEASON FLOOR
# ============================================================


def live_floor(card):

  p = card.get("anyPlayer") or {}

  ps = norm(p.get("slug"))

  rarity = norm(card.get("rarityTyped"))

  try:

    season = int(card.get("seasonYear"))

  except Exception:

    return None

  if not ps:
    return None

  d = graphql(
      """
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
""",
      {"playerSlug": ps, "first": 50},
  )

  if d is None:
    return None

  data = d.get("data") if isinstance(d, dict) else None

  if not isinstance(data, dict):
    return None

  tokens = data.get("tokens")

  if not isinstance(tokens, dict):
    return None

  container = tokens.get("liveSingleSaleOffers")

  if not isinstance(container, dict):
    return None

  offers = container.get("nodes", [])

  if not isinstance(offers, list):
    return None

  prices = []

  for o in offers:

    if not isinstance(o, dict):
      continue

    for c in (o.get("senderSide") or {}).get("anyCards") or []:

      try:
        same_season = int(c.get("seasonYear") or -1) == season
      except Exception:
        same_season = False

      if (
          norm((c.get("anyPlayer") or {}).get("slug")) == ps
          and norm(c.get("rarityTyped")) == rarity
          and same_season
      ):

        x = price_eur((o.get("receiverSide") or {}).get("amounts") or {})

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
  """REGOLA B AUTOBUY:

  NON comprare la carta se esiste almeno una Limited dello stesso giocatore, di
  qualsiasi stagione, con floor/listing sotto €0.32.

  Se Sorare non permette di verificare correttamente il mercato, viene applicato
  fail-safe: AUTOBUY BLOCCATO.
  """

  p = card.get("anyPlayer") or {}

  ps = norm(p.get("slug"))

  if not ps:

    print(
        f"⚠️ GLOBAL FLOOR: player slug mancante per {card_label(card)}",
        flush=True,
    )

    return True

  d = graphql(
      """
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
""",
      {"playerSlug": ps, "first": 100},
  )

  if d is None:

    print(
        f"⚠️ GLOBAL FLOOR: impossibile interrogare il mercato per"
        f" {card_label(card)}",
        flush=True,
    )

    return True

  data = d.get("data") if isinstance(d, dict) else None

  if not isinstance(data, dict):

    print(
        f"⚠️ GLOBAL FLOOR: risposta senza data per {card_label(card)}",
        flush=True,
    )

    return True

  tokens = data.get("tokens")

  if not isinstance(tokens, dict):

    print(
        f"⚠️ GLOBAL FLOOR: campo tokens assente per {card_label(card)}",
        flush=True,
    )

    return True

  container = tokens.get("liveSingleSaleOffers")

  if not isinstance(container, dict):

    print(
        f"⚠️ GLOBAL FLOOR: liveSingleSaleOffers assente per {card_label(card)}",
        flush=True,
    )

    return True

  offers = container.get("nodes", [])

  if not isinstance(offers, list):

    print(
        f"⚠️ GLOBAL FLOOR: nodes non disponibili per {card_label(card)}",
        flush=True,
    )

    return True

  if not offers:

    print(
        f"⚠️ GLOBAL FLOOR: nessuna offerta live recuperata per"
        f" {card_label(card)}",
        flush=True,
    )

    return True

  for o in offers:

    if not isinstance(o, dict):
      continue

    price = price_eur((o.get("receiverSide") or {}).get("amounts") or {})

    if price is None:
      continue

    cards = (o.get("senderSide") or {}).get("anyCards") or []

    for c in cards:

      player_slug = norm((c.get("anyPlayer") or {}).get("slug"))

      rarity = norm(c.get("rarityTyped")).upper()

      if (
          player_slug == ps
          and rarity == "LIMITED"
          and price < MIN_PRICE
      ):

        print(
            f"🚫 GLOBAL FLOOR: {card_label(card)} → trovata Limited"
            f" {c.get('seasonYear') or 'N/D'} a {format_eur(price)}"
            f" (< {format_eur(MIN_PRICE)})",
            flush=True,
        )

        return True

  print(
      f"🟢 GLOBAL FLOOR OK: {card_label(card)} → nessuna Limited del giocatore"
      f" sotto {format_eur(MIN_PRICE)}",
      flush=True,
  )

  return False


# ============================================================
# LISTED CARDS
# ============================================================


def listed_cards(asset_ids):
  """Verifica quali asset sono attualmente presenti nelle live single sale di

  Sorare.

  IMPORTANTE: liveSingleSaleOffers NON accetta assetIds nello schema GraphQL che
  stiamo utilizzando.

  Pertanto: 1. recuperiamo le live sale; 2. filtriamo localmente gli assetId
  richiesti.

  Return: set() -> query riuscita, nessun asset trovato set(ids) -> asset trovati
  None -> impossibile verificare il mercato
  """

  ids = [str(x).strip() for x in asset_ids if x]

  if not ids:
    return set()

  wanted = {norm(x) for x in ids}

  d = graphql(
      """
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
""",
      {"first": 100},
  )

  if d is None:

    print("⚠️ LISTED CARDS: query mercato fallita", flush=True)

    return None

  data = d.get("data") if isinstance(d, dict) else None

  if not isinstance(data, dict):

    print("⚠️ LISTED CARDS: risposta GraphQL senza data", flush=True)

    return None

  tokens = data.get("tokens")

  if not isinstance(tokens, dict):

    print("⚠️ LISTED CARDS: campo tokens assente", flush=True)

    return None

  container = tokens.get("liveSingleSaleOffers")

  if not isinstance(container, dict):

    print("⚠️ LISTED CARDS: liveSingleSaleOffers assente", flush=True)

    return None

  offers = container.get("nodes")

  if not isinstance(offers, list):

    print("⚠️ LISTED CARDS: nodes non disponibile", flush=True)

    return None

  result = set()

  for o in offers:

    if not isinstance(o, dict):
      continue

    cards = (o.get("senderSide") or {}).get("anyCards") or []

    for c in cards:

      if not isinstance(c, dict):
        continue

      aid = norm(c.get("assetId"))

      if aid in wanted:

        result.add(aid)

  print(
      f"🔎 LISTED CARDS: {len(result)}/{len(wanted)} carte richieste trovate"
      " in vendita",
      flush=True,
  )

  return result


# ============================================================
# KULENOVIC
# ============================================================


def is_kulenovic(c):

  wanted = {norm(KSLUG), norm(KASSET)}

  extra = os.getenv("KULENOVIC_ID", "").strip()

  if extra:
    wanted.add(norm(extra))

  return norm(c.get("assetId")) in wanted or norm(c.get("slug")) in wanted


# ============================================================
# COVERAGE
# ============================================================

AUTOBUY_BLOCKED_COMPETITIONS = {
    "k-league-2",
    "play-offs-1-2-kr",
    "fa-cup-kr",
}


def get_active_competitions(c):

  player = c.get("anyPlayer") or {}

  club = player.get("activeClub") or {}

  competitions = club.get("activeCompetitions") or []

  result = []

  for comp in competitions:

    if not isinstance(comp, dict):
      continue

    slug = norm(comp.get("slug"))

    name = comp.get("displayName") or comp.get("name") or slug

    if slug or name:

      result.append({
          "slug": slug,
          "name": name,
          "blocked": slug in AUTOBUY_BLOCKED_COMPETITIONS,
      })

  return result


# ============================================================
# AUTOBUY VALIDATION
# ============================================================


def validate_card(c, log_reason=True):

  label = card_label(c)

  player = c.get("anyPlayer") or {}

  # --------------------------------------------------------
  # 1. ETÀ
  # --------------------------------------------------------

  age = player.get("age")

  try:
    age = int(age)

  except Exception:

    if log_reason:

      print(
          f"🚫 AUTOBUY NON IDONEA: {label} → età non disponibile",
          flush=True,
      )

    return False

  if age >= MAX_AGE:
    if log_reason:
      print(f"🚫 AUTOBUY NON IDONEA: {label} → età {age} >= {MAX_AGE}", flush=True)
    return False

  return True


# ============================================================
# FLASK HEALTHCHECK
# ============================================================


@app.route("/")
def index():
  return jsonify({
      "status": "online",
      "bot_version": BOT_VERSION,
      "user": current_user_slug,
  })


if __name__ == "__main__":
  load_local_state()
  load_github_state()
  check_account()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
