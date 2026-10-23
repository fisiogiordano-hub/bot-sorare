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
# CONFIG & 2FA LOGIN AUTOMATICO SICURO
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

  try:
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

    r1 = requests.post(
        SORARE_URL, json=payload_step1, headers=headers, timeout=25
    )
    if r1.status_code != 200:
      print(f"❌ Errore HTTP Step 1: {r1.status_code} - {r1.text}", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

    res1 = r1.json()
    if not res1:
      print("❌ Risposta JSON vuota nello Step 1", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

    challenge = (
        res1.get("data", {}).get("signIn", {}).get("otpSessionChallenge")
    )
    if not challenge:
      print(f"❌ Impossibile ottenere la sfida 2FA: {res1}", flush=True)
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

    r2 = requests.post(
        SORARE_URL, json=payload_step2, headers=headers, timeout=25
    )
    if r2.status_code != 200:
      print(f"❌ Errore HTTP Step 2: {r2.status_code} - {r2.text}", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

    res2 = r2.json()
    if not res2:
      print("❌ Risposta JSON vuota nello Step 2", flush=True)
      return os.getenv("SORARE_JWT_TOKEN", "").strip()

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
# AUTOBUY & CONFIG
# ============================================================

MIN_PRICE = 32  # €0.32
MAX_PRICE = 70  # €0.70
PAY_PER_CARD = 20  # €0.20
MAX_AGE = 28
MIN_LIVE_LISTINGS = 5

SWAP_AUTO_ACCEPT = True
SWAP_MIN = 1.20
SWAP_MAX = 1.25

INTERVAL = 10
TIMEOUT = 25
USD_CACHE = 300

BOT_VERSION = "23.9-LIGHT-SWAP-GLOBAL-FLOOR-FIXED-2"

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
)

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
# UTILS & GRAPHQL
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


def graphql(query, variables=None):
  for attempt in range(3):
    try:
      r = requests.post(
          SORARE_URL,
          json={"query": query, "variables": variables or {}},
          headers=headers(),
          timeout=TIMEOUT,
      )
      if r.status_code == 429:
        time.sleep(attempt + 2)
        continue
      if r.status_code != 200:
        time.sleep(attempt + 1)
        continue
      d = r.json()
      if d.get("errors"):
        print(
            "❌ GraphQL:",
            json.dumps(d["errors"], ensure_ascii=False)[:5000],
            flush=True,
        )
      return d
    except Exception as e:
      time.sleep(attempt + 1)
  return None


def load_local_state():
  if not os.path.exists(STATE_FILE):
    return
  try:
    with open(STATE_FILE, encoding="utf-8") as f:
      d = json.load(f)
      # state load logic
  except Exception:
    pass


def load_github_state():
  pass


def check_account():
  global current_user_slug
  d = graphql("query{currentUser{slug nickname starkKey}}")
  u = (((d or {}).get("data") or {}).get("currentUser"))
  if not u:
    return False
  current_user_slug = u.get("slug")
  print(f"✅ Sorare: {u.get('nickname') or current_user_slug}", flush=True)
  return True


# ============================================================
# WORKER THREAD (Richiesto da wsgi.py)
# ============================================================


def start_worker():
  global worker_started
  with worker_lock:
    if worker_started:
      return
    worker_started = True

  def run():
    load_local_state()
    load_github_state()
    check_account()
    print("✅ Thread Sorare avviato.", flush=True)
    while True:
      try:
        # Ciclo principale del bot
        time.sleep(INTERVAL)
      except Exception as e:
        print(f"⚠️ Errore nel worker loop: {e}", flush=True)
        time.sleep(10)

  t = threading.Thread(target=run, daemon=True)
  t.start()


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
  start_worker()
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
