import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

SORARE_URL = "https://api.sorare.com/graphql"
TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
SOLANA = os.getenv("SORARE_SOLANA_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MIN_PRICE = 0
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv("BOT_STATE_PATH", "bot_state.json").strip()
VERSION = "AUTOSell-15.0-SOLANA-EUR-DYNAMIC-MINPRICE"

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

state_lock = threading.RLock()
worker_lock = threading.Lock()
worker_started = False

SOLANA_ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


# ============================================================
# UTILS
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id():
    return str(uuid.uuid4())


def asset_id(card):
    return str(card.get("assetId") or card.get("asset_id") or "").strip()


def label(card):
    return card.get("name") or card.get("slug") or asset_id(card) or "Carta"


def eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "processed_offers": [],
        "acquired_cards": [],
        "pending_autobuys": [],
        "updated_at": int(time.time())
    }


def save_document(data):
    tmp = f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        print(f"❌ Errore scrittura state: {e}", flush=True)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def ensure_state():
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not os.path.exists(path):
        save_document(default_state())


def load_document():
    ensure_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_state()

        data.setdefault("processed_offers", [])
        data.setdefault("acquired_cards", [])
        data.setdefault("pending_autobuys", [])
        data.setdefault("updated_at", int(time.time()))
        return data

    except Exception as e:
        print(f"❌ Errore lettura state: {e}", flush=True)
        return default_state()


def get_cards():
    with state_lock:
        cards = load_document().get("acquired_cards", [])
        return cards if isinstance(cards, list) else []


def update_card(
    asset,
    status=None,
    offer_id=None,
    error=None,
    floor=None,
    min_price=None,
    sale_price=None,
    clear_offer=False
):
    wanted = norm(asset)

    with state_lock:
        data = load_document()
        cards = data.get("acquired_cards", [])

        for card in cards:
            if not isinstance(card, dict):
                continue

            if norm(asset_id(card)) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = offer_id

            if clear_offer:
                card.pop("sale_offer_id", None)

            if floor is not None:
                card["last_floor_cents"] = floor

            if min_price is not None:
                card["last_sorare_min_price_cents"] = min_price

            if sale_price is not None:
                card["last_sale_price_cents"] = sale_price

            card["last_error"] = error

            if norm(status) == "selling":
                card["selling_at"] = card.get("selling_at") or now()

            data["acquired_cards"] = cards
            data["updated_at"] = int(time.time())
            return save_document(data)

        print(f"⚠️ Carta non presente nello state: {asset}", flush=True)
        return False


def sellable_cards():
    result = []

    for card in get_cards():
        if not isinstance(card, dict):
            continue

        if norm(card.get("status")) in {"da_vendere", "ready"}:
            if asset_id(card):
                result.append(dict(card))

    return result


# ============================================================
# GRAPHQL
# ============================================================

def auth_headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    headers = {
        "Authorization": (
            TOKEN if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


def gql(query, variables=None):
    for attempt in range(3):
        try:
            response = requests.post(
                SORARE_URL,
                headers=auth_headers(),
                json={"query": query, "variables": variables or {}},
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {response.status_code}", flush=True)

            if response.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue

            if response.status_code != 200:
                print(f"❌ Sorare: {response.text[:1000]}", flush=True)
                time.sleep(attempt + 1)
                continue

            data = response.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(data["errors"], ensure_ascii=False)[:3000],
                    flush=True
                )

            return data

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    data = gql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = ((data or {}).get("data") or {}).get("currentUser")

    if not user:
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        "✅ Sorare: " + str(user.get("nickname") or user.get("slug")),
        flush=True
    )
    print(
        "🔐 Stark key account: "
        + ("PRESENTE" if user.get("starkKey") else "NON DISPONIBILE"),
        flush=True
    )
    print(
        "🔑 Solana private key: "
        + ("PRESENTE" if SOLANA else "NON DISPONIBILE"),
        flush=True
    )

    return True


# ============================================================
# CARD DETAILS + MINIMO SORARE
# ============================================================

def card_details(asset):
    data = gql("""
        query Cards($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                slug
                name
                rarityTyped
                seasonYear
                publicMinPrices {
                    eurCents
                }
                anyPlayer {
                    slug
                    displayName
                    activeClub {
                        slug
                        name
                    }
                }
            }
        }
    """, {"ids": [asset]})

    cards = (
        ((data or {}).get("data") or {})
        .get("anyCards") or []
    )

    return cards[0] if cards else None


def sorare_minimum_price(card):
    value = (
        (card.get("publicMinPrices") or {})
        .get("eurCents")
    )

    try:
        value = int(value)
        return value if value > 0 else None
    except Exception:
        return None


# ============================================================
# ACTIVE PUBLIC OFFER
# ============================================================

def find_active_public_offer(asset):
    wanted = norm(asset)

    data = gql("""
        query ActivePublicOffers($first: Int) {
            tokens {
                liveSingleSaleOffers(first: $first) {
                    nodes {
                        id
                        senderSide {
                            anyCards {
                                assetId
                            }
                        }
                    }
                }
            }
        }
    """, {"first": 100})

    if not data:
        print("⚠️ Pre-check offerte: nessuna risposta", flush=True)
        return None

    nodes = (
        (((data.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    for offer in nodes:
        if not isinstance(offer, dict):
            continue

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        )

        if any(
            norm(c.get("assetId")) == wanted
            for c in cards
            if isinstance(c, dict)
        ):
            print(
                f"🟢 PRE-CHECK → CARTA GIÀ IN VENDITA | {offer.get('id')}",
                flush=True
            )
            return offer.get("id")

    print(
        "🔵 PRE-CHECK → nessuna offerta pubblica attiva",
        flush=True
    )
    return None


# ============================================================
# FLOOR
# ============================================================

def usd_to_eur(usd_cents):
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )
        rate = float(r.json()["rates"]["EUR"])
        return round(usd_cents * rate)
    except Exception:
        return None


def amount_to_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        value = int(amounts.get("eurCents") or 0)
        if value > 0:
            return value
    except Exception:
        pass

    try:
        value = int(amounts.get("usdCents") or 0)
        if value > 0:
            return usd_to_eur(value)
    except Exception:
        pass

    return None


def get_floor(card):
    player = card.get("anyPlayer") or {}
    player_slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    if not player_slug or not rarity:
        return None

    data = gql("""
        query LiveOffers($slug: String, $first: Int) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug: $slug
                    first: $first
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
                            }
                        }
                    }
                }
            }
        }
    """, {
        "slug": player_slug,
        "first": 50
    })

    nodes = (
        (((data or {}).get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in nodes:
        sender = offer.get("senderSide") or {}

        for listed in sender.get("anyCards") or []:
            listed_player = listed.get("anyPlayer") or {}

            try:
                same_season = int(listed.get("seasonYear")) == season
            except Exception:
                continue

            if not same_season:
                continue

            if norm(listed_player.get("slug")) != player_slug:
                continue

            if norm(listed.get("rarityTyped")) != rarity:
                continue

            price = amount_to_eur(
                (offer.get("receiverSide") or {}).get("amounts")
            )

            if price is not None:
                prices.append(price)

            break

    print(
        f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    if len(prices) < MIN_LISTINGS:
        return None

    return min(prices)


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):
    return (
        norm(card.get("slug")) == norm(KULENOVIC_SLUG)
        or norm(card.get("assetId")) == norm(KULENOVIC_ASSET)
    )


def validate(card):
    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    minimum = sorare_minimum_price(card)

    if minimum is None:
        return False, "SORARE_MIN_UNKNOWN"

    price = max(floor, minimum)

    return True, {
        "floor": floor,
        "minimum": minimum,
        "price": price
    }


# ============================================================
# BASE58
# ============================================================

def base58_decode(value):
    value = str(value).strip()

    if not value:
        raise ValueError("Base58 vuoto")

    number = 0

    for char in value:
        index = SOLANA_ALPHABET.find(char)

        if index < 0:
            raise ValueError(f"Carattere Base58 non valido: {char}")

        number = number * 58 + index

    raw = (
        b""
        if number == 0
        else number.to_bytes(
            max(1, (number.bit_length() + 7) // 8),
            "big"
        )
    )

    zeros = 0

    for char in value:
        if char != "1":
            break
        zeros += 1

    return b"\x00" * zeros + raw


def solana_key_info():
    if not SOLANA:
        raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY mancante")

    value = SOLANA.strip()

    try:
        decoded = base58_decode(value)

        if len(decoded) in {32, 64}:
            return {"format": "base58", "bytes": decoded}

    except Exception:
        pass

    hex_value = value[2:] if value.startswith("0x") else value

    if (
        len(hex_value) % 2 == 0
        and all(c in "0123456789abcdefABCDEF" for c in hex_value)
    ):
        decoded = bytes.fromhex(hex_value)

        if len(decoded) in {32, 64}:
            return {"format": "hex", "bytes": decoded}

    raise RuntimeError(
        "SORARE_SOLANA_PRIVATE_KEY non valida"
    )


# ============================================================
# SIGN AUTHORIZATIONS
# ============================================================

def sign_authorizations(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    types = [
        (a.get("request") or {}).get("__typename")
        for a in authorizations
    ]

    requires_stark = any(
        t != "SolanaTokenTransferAuthorizationRequest"
        for t in types
    )

    requires_solana = any(
        t == "SolanaTokenTransferAuthorizationRequest"
        for t in types
    )

    if requires_stark and not STARK:
        raise RuntimeError("SORARE_STARK_PRIVATE_KEY mancante")

    if requires_solana and not SOLANA:
        raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY mancante")

    js = r'''
const crypto=require("crypto");
const {signAuthorizationRequest}=require("@sorare/crypto");
const {
  createSignableMessage,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
}=require("@solana/kit");

const input=JSON.parse(require("fs").readFileSync(0,"utf8"));
const ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function b58decode(v){
  let n=0n;
  for(const c of String(v).trim()){
    const i=ALPHABET.indexOf(c);
    if(i<0) throw new Error("Base58 non valido");
    n=n*58n+BigInt(i);
  }
  let b=n===0n?Buffer.alloc(0):Buffer.from(
    (n.toString(16).length%2?"0":"")+n.toString(16),"hex");
  let z=0;
  for(const c of String(v).trim()){if(c!=="1")break;z++;}
  return new Uint8Array(Buffer.concat([Buffer.alloc(z),b]));
}

function b58encode(data){
  const bytes=Buffer.from(data);
  let n=0n;
  for(const b of bytes)n=n*256n+BigInt(b);
  let r="";
  while(n>0n){r=ALPHABET[Number(n%58n)]+r;n/=58n;}
  let z=0;
  for(const b of bytes){if(b!==0)break;z++;}
  return "1".repeat(z)+r;
}

function parseKey(v){
  const clean=String(v||"").trim();
  try{
    const d=b58decode(clean);
    if(d.length===32||d.length===64)return d;
  }catch(_){}
  let h=clean.startsWith("0x")?clean.slice(2):clean;
  if(/^[0-9a-fA-F]+$/.test(h)&&h.length%2===0){
    const d=new Uint8Array(Buffer.from(h,"hex"));
    if(d.length===32||d.length===64)return d;
  }
  throw new Error("Chiave Solana non riconosciuta");
}

async function signerFromKey(v){
  let k=parseKey(v);
  if(k.length===64)k=k.slice(0,32);
  if(k.length!==32)throw new Error("Private key Solana non valida");
  return createSignerFromKeyPair(
    await createKeyPairFromPrivateKeyBytes(k)
  );
}

async function signSolana(auth){
  const req=auth.request;
  const signer=await signerFromKey(input.solanaPrivateKey);

  console.error("🔑 Solana signer → "+signer.address);
  console.error("🎯 senderAddress → "+req.senderAddress);

  if(signer.address!==req.senderAddress)
    throw new Error(
      "La chiave Solana NON corrisponde al senderAddress"
    );

  const message=[
    "TRANSFER",
    req.transferProxyProgramAddress,
    req.merkleTreeAddress,
    req.leafIndex.toString(),
    req.nonce,
    req.expirationTimestamp.toString(),
    req.receiverAddress,
    "0x",
    req.originator
  ].join(":");

  console.error("📝 Solana message costruito");

  const hash=crypto.createHash("sha256")
    .update(Buffer.from(message,"utf8")).digest();

  const signable=createSignableMessage(new Uint8Array(hash));
  const signatures=await signer.signMessages([signable]);
  const sig=signatures[0][signer.address];

  return {
    fingerprint:auth.fingerprint,
    solanaTokenTransferApproval:{
      signature:b58encode(sig),
      nonce:req.nonce,
      expirationTimestamp:req.expirationTimestamp
    }
  };
}

function signStark(auth){
  const req=auth.request;
  const signature=signAuthorizationRequest(input.starkPrivateKey,req);
  const base={fingerprint:auth.fingerprint};

  if(req.__typename==="StarkexTransferAuthorizationRequest")
    return {
      ...base,
      starkexTransferApproval:{
        nonce:req.nonce,
        expirationTimestamp:req.expirationTimestamp,
        signature
      }
    };

  if(req.__typename==="StarkexLimitOrderAuthorizationRequest")
    return {
      ...base,
      starkexLimitOrderApproval:{
        nonce:req.nonce,
        expirationTimestamp:req.expirationTimestamp,
        signature
      }
    };

  if(req.__typename==="MangopayWalletTransferAuthorizationRequest")
    return {
      ...base,
      mangopayWalletTransferApproval:{
        nonce:req.nonce,
        signature
      }
    };

  throw new Error("Authorization non supportata: "+req.__typename);
}

async function main(){
  const approvals=[];

  for(const auth of input.authorizations){
    const type=(auth.request||{}).__typename;
    console.error("🔐 Authorization → "+type);

    approvals.push(
      type==="SolanaTokenTransferAuthorizationRequest"
      ? await signSolana(auth)
      : signStark(auth)
    );
  }

  process.stdout.write(JSON.stringify(approvals));
}

main().catch(e=>{
  console.error(e.stack||e);
  process.exit(1);
});
'''

    process = subprocess.run(
        [node, "-e", js],
        input=json.dumps({
            "starkPrivateKey": STARK,
            "solanaPrivateKey": SOLANA,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if process.stderr:
        print(process.stderr.strip(), flush=True)

    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "Firma fallita")

    try:
        return json.loads(process.stdout)
    except Exception:
        raise RuntimeError(
            "Output firma non valido: " + process.stdout[:1000]
        )


# ============================================================
# PREPARE
# ============================================================

PREPARE_QUERY = """
mutation PrepareOffer($input: prepareOfferInput!) {
  prepareOffer(input: $input) {
    authorizations {
      fingerprint
      request {
        __typename

        ... on StarkexTransferAuthorizationRequest {
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

        ... on StarkexLimitOrderAuthorizationRequest {
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

        ... on MangopayWalletTransferAuthorizationRequest {
          nonce
          amount
          currency
          operationHash
          mangopayWalletId
        }

        ... on SolanaTokenTransferAuthorizationRequest {
          assetId
          leafIndex
          merkleTreeAddress
          originator
          receiverAddress
          senderAddress
          expirationTimestamp
          nonce
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


def prepare_sale(asset, price):
    data = gql(
        PREPARE_QUERY,
        {
            "input": {
                "sendAssetIds": [asset],
                "receiveAssetIds": [],
                "settlementCurrencies": ["EUR"],
                "receiveAmount": {
                    "amount": str(price),
                    "currency": "EUR"
                },
                "clientMutationId": new_id()
            }
        }
    )

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print("❌ prepareOffer: nessun risultato", flush=True)
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    auths = result.get("authorizations") or []

    if not auths:
        print("❌ prepareOffer: nessuna authorization", flush=True)
        return None

    print(f"✅ Authorization ricevute: {len(auths)}", flush=True)
    return auths


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price):
    asset = asset_id(card)

    if not asset:
        return None

    sale_price = int(price)

    if sale_price > MAX_PRICE:
        print(f"🚫 Prezzo oltre massimo → {eur(MAX_PRICE)}", flush=True)
        return None

    print(f"💰 PREZZO VENDITA → {eur(sale_price)}", flush=True)

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → {label(card)} → {eur(sale_price)}",
            flush=True
        )
        return "DRY-RUN"

    existing = find_active_public_offer(asset)

    if existing:
        print("🟢 CARTA GIÀ IN VENDITA SU SORARE", flush=True)
        return existing

    authorizations = prepare_sale(asset, sale_price)

    if not authorizations:
        return None

    try:
        approvals = sign_authorizations(authorizations)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return None

    query = """
    mutation CreateSale($input: createSingleSaleOfferInput!) {
      createSingleSaleOffer(input: $input) {
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
    """

    create_input = {
        "approvals": approvals,
        "dealId": new_id(),
        "assetId": asset,
        "settlementCurrencies": "EUR",
        "receiveAmount": {
            "amount": str(sale_price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    print("📤 createSingleSaleOffer → settlementCurrencies=EUR", flush=True)

    data = gql(query, {"input": create_input})

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print("❌ createSingleSaleOffer: nessun risultato", flush=True)
        return None

    errors = result.get("errors") or []

    if errors:
        text = " ".join(
            str(e.get("message", ""))
            for e in errors
            if isinstance(e, dict)
        )

        if "active public offer already exists" in norm(text):
            print("🟢 CARTA GIÀ IN VENDITA SU SORARE", flush=True)
            return find_active_public_offer(asset) or "ALREADY-LISTED"

        if "price must be greater than" in norm(text):
            print(
                "⚠️ PREZZO SOTTO IL MINIMO TECNICO SORARE",
                flush=True
            )
            return "MIN_PRICE_REJECTED"

        print(
            "❌ createSingleSaleOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    offer_id = (result.get("tokenOffer") or {}).get("id")

    if not offer_id:
        print("❌ Vendita non creata: offer ID assente", flush=True)
        return None

    print(f"✅ INSERZIONE CREATA → {offer_id}", flush=True)
    return offer_id


# ============================================================
# PROCESS
# ============================================================

def process(card):
    asset = asset_id(card)

    print(f"\n💰 AUTOSELL CHECK → {asset}", flush=True)

    existing = find_active_public_offer(asset)

    if existing:
        print("🟢 CARTA GIÀ IN VENDITA", flush=True)
        update_card(
            asset,
            status="SELLING",
            offer_id=existing,
            error=None
        )
        print("🟢 STATO → SELLING", flush=True)
        return

    details = card_details(asset)

    if not details:
        print("❌ Carta non recuperabile", flush=True)
        update_card(asset, status="da_vendere", error="CARD_DETAILS")
        return

    print(
        f"🃏 {details.get('name') or details.get('slug')} "
        f"{details.get('seasonYear')} • "
        f"{norm(details.get('rarityTyped'))}",
        flush=True
    )

    ok, result = validate(details)

    if not ok:
        messages = {
            "KULENOVIC": "KULENOVIC PROTETTO",
            "RARITY": "RARITÀ NON LIMITED",
            "FLOOR_UNKNOWN": "FLOOR NON DISPONIBILE",
            "FLOOR_HIGH": "FLOOR SOPRA €0.70",
            "SORARE_MIN_UNKNOWN": "MINIMO SORARE NON DISPONIBILE"
        }

        print(
            f"🚫 ESCLUSA → {messages.get(result, result)}",
            flush=True
        )

        update_card(
            asset,
            status=(
                "BLOCKED"
                if result in {"KULENOVIC", "RARITY"}
                else "da_vendere"
            ),
            error=result
        )
        return

    floor = result["floor"]
    minimum = result["minimum"]
    price = result["price"]

    print(f"📊 FLOOR → {eur(floor)}", flush=True)
    print(f"🛡️ MINIMO SORARE → {eur(minimum)}", flush=True)

    if floor < minimum:
        print(
            f"💶 FLOOR SOTTO MINIMO → "
            f"VENDO A {eur(minimum)}",
            flush=True
        )
    else:
        print(
            f"💰 FLOOR SOPRA MINIMO → "
            f"VENDO A {eur(floor)}",
            flush=True
        )

    if price > MAX_PRICE:
        update_card(
            asset,
            status="da_vendere",
            error="PRICE_HIGH",
            floor=floor,
            min_price=minimum,
            sale_price=price
        )
        return

    offer_id = create_sale(details, price)

    # Sorare ha appena alzato il minimo tecnico:
    # rileggiamo il minimo una sola volta e ritentiamo.
    if offer_id == "MIN_PRICE_REJECTED":
        print(
            "🔄 RILETTURA MINIMO SORARE...",
            flush=True
        )

        refreshed = card_details(asset)

        if not refreshed:
            update_card(
                asset,
                status="da_vendere",
                error="MIN_PRICE_REFRESH_FAILED",
                floor=floor,
                min_price=minimum,
                sale_price=price
            )
            return

        new_minimum = sorare_minimum_price(refreshed)

        if new_minimum is None:
            update_card(
                asset,
                status="da_vendere",
                error="SORARE_MIN_UNKNOWN",
                floor=floor,
                min_price=minimum,
                sale_price=price
            )
            return

        retry_price = max(floor, new_minimum)

        print(
            f"🔄 NUOVO MINIMO SORARE → "
            f"{eur(new_minimum)}",
            flush=True
        )

        if retry_price > MAX_PRICE:
            update_card(
                asset,
                status="da_vendere",
                error="PRICE_HIGH",
                floor=floor,
                min_price=new_minimum,
                sale_price=retry_price
            )
            return

        offer_id = create_sale(
            refreshed,
            retry_price
        )

        minimum = new_minimum
        price = retry_price

    if not offer_id:
        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED",
            floor=floor,
            min_price=minimum,
            sale_price=price
        )
        print("🔁 Carta resta DA_VENDERE", flush=True)
        return

    if offer_id == "ALREADY-LISTED":
        existing = find_active_public_offer(asset)

        if existing:
            update_card(
                asset,
                status="SELLING",
                offer_id=existing,
                error=None,
                floor=floor,
                min_price=minimum,
                sale_price=price
            )
            print("🟢 CARTA GIÀ IN VENDITA", flush=True)
            return

        update_card(
            asset,
            status="da_vendere",
            error="ALREADY_LISTED_NOT_FOUND"
        )
        return

    if offer_id == "DRY-RUN":
        update_card(
            asset,
            status="da_vendere",
            error=None,
            floor=floor,
            min_price=minimum,
            sale_price=price
        )
        return

    # SELLING SOLO DOPO SUCCESSO REALE
    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        error=None,
        floor=floor,
        min_price=minimum,
        sale_price=price
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(details)} | {eur(price)} | {offer_id}",
        flush=True
    )
    print("🟢 STATO → SELLING", flush=True)


# ============================================================
# RECOVERY / CARTE RIMOSSE MANUALMENTE DALLA VENDITA
# ============================================================

def recovery():
    selling = [
        c for c in get_cards()
        if norm(c.get("status")) == "selling"
    ]

    if not selling:
        print("🔄 Recovery: nessuna carta SELLING.", flush=True)
        return

    print(
        f"🔄 Recovery: controllo {len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:
        asset = asset_id(card)

        if not asset:
            continue

        offer = find_active_public_offer(asset)

        if offer:
            update_card(
                asset,
                status="SELLING",
                offer_id=offer,
                error=None
            )
            continue

        # Offerta rimossa/cancellata manualmente:
        # la carta torna valutabile.
        print(
            f"🔄 OFFERTA NON PIÙ ATTIVA → "
            f"{asset} → DA_VENDERE",
            flush=True
        )

        update_card(
            asset,
            status="da_vendere",
            error=None,
            clear_offer=True
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print(f"📦 VERSIONE: {VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)
    print("💰 RANGE FLOOR: €0.00 - €0.70", flush=True)
    print("📊 NESSUN FLOOR MINIMO", flush=True)
    print(f"📊 LISTING MINIME: {MIN_LISTINGS}", flush=True)
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("🛡️ COVERAGE: DISABILITATA", flush=True)
    print("🛡️ SOURCE: AUTOBUY / SWAP", flush=True)
    print("💶 SETTLEMENT: EUR", flush=True)
    print("💶 MINIMO SORARE: LETTO DINAMICAMENTE", flush=True)
    print("🟢 GIÀ IN VENDITA → NON MODIFICARE", flush=True)
    print("🔄 OFFERTA RIMOSSA → NUOVA VALUTAZIONE", flush=True)
    print("💾 STORAGE:", STATE_FILE, flush=True)

    try:
        auth_headers()
        ensure_state()
        solana_key_info()
    except Exception as e:
        print(f"❌ Configurazione: {e}", flush=True)
        return

    if not check_account():
        return

    recovery()

    while True:
        try:
            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: {len(cards)}",
                flush=True
            )

            for card in cards:
                try:
                    process(card)
                except Exception as e:
                    asset = asset_id(card)
                    print(
                        f"❌ AutoSell {asset}: {e}",
                        flush=True
                    )

                    if asset:
                        update_card(
                            asset,
                            status="da_vendere",
                            error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ Worker: {e}", flush=True)
            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    cards = get_cards()

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "range": "€0.00-€0.70",
        "min_floor": "NONE",
        "max_floor": "€0.70",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "age": "NOT_USED",
        "kulenovic": "NEVER_SELL",
        "coverage": "DISABLED",
        "settlement": "EUR",
        "already_listed": "SKIP",
        "repricing": "DISABLED",
        "sorare_minimum_price": "DYNAMIC_PUBLIC_MIN_PRICE",
        "removed_listing": "REVALUATE",
        "storage": STATE_FILE,
        "cards": len(cards),
        "da_vendere": sum(
            1 for c in cards
            if norm(c.get("status")) in {"da_vendere", "ready"}
        ),
        "selling": sum(
            1 for c in cards
            if norm(c.get("status")) == "selling"
        ),
        "worker": worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": VERSION,
        "worker": worker_started,
        "dry_run": DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():
    cards = get_cards()
    return jsonify({
        "count": len(cards),
        "cards": cards
    })


# ============================================================
# MAIN
# ============================================================

def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()

        print("✅ Thread AutoSell avviato.", flush=True)


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False
    )
