import os, json, time, uuid, shutil, subprocess, threading, requests
from flask import Flask, jsonify

# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)
URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
SOLANA = os.getenv("SORARE_SOLANA_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))
STATE_FILE = os.getenv("BOT_STATE_PATH", "bot_state.json")

MAX_PRICE = 70
MIN_PRICE = 30
MIN_LISTINGS = 5

KULENOVIC = "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

VERSION = "AUTOSell-17.0-EUR-MIN30-SEALED-NOTOWNED"

lock = threading.RLock()
worker_started = False


# ============================================================
# UTILS / STATE
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def aid(card):
    return str(card.get("assetId") or card.get("asset_id") or "").strip()


def label(card):
    return card.get("name") or card.get("slug") or aid(card) or "Carta"


def eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


def new_id():
    return str(uuid.uuid4())


def default_state():
    return {
        "processed_offers": [],
        "acquired_cards": [],
        "pending_autobuys": [],
        "updated_at": int(time.time())
    }


def save(data):
    tmp = STATE_FILE + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        print("❌ STATE:", e, flush=True)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def load():
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)

    if not os.path.exists(STATE_FILE):
        save(default_state())

    try:
        with open(STATE_FILE, encoding="utf8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = default_state()
    except Exception:
        d = default_state()

    d.setdefault("processed_offers", [])
    d.setdefault("acquired_cards", [])
    d.setdefault("pending_autobuys", [])
    d.setdefault("updated_at", int(time.time()))
    return d


def cards():
    with lock:
        return load()["acquired_cards"]


def update(asset, status=None, error=None, offer_id=None,
           price=None, start=None, end=None):

    with lock:
        d = load()

        for c in d["acquired_cards"]:
            if norm(aid(c)) != norm(asset):
                continue

            if status is not None:
                c["status"] = status
            if error is not None:
                c["last_error"] = error
            else:
                c["last_error"] = None
            if offer_id:
                c["sale_offer_id"] = offer_id
            if price is not None:
                c["sale_price_eur"] = int(price)
            if start is not None:
                c["sale_start_date"] = start
            if end is not None:
                c["sale_end_date"] = end

            d["updated_at"] = int(time.time())
            return save(d)

    return False


def sellable():
    return [
        dict(c) for c in cards()
        if isinstance(c, dict)
        and norm(c.get("status")) in {"da_vendere", "ready"}
        and aid(c)
    ]


# ============================================================
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    h = {
        "Authorization": TOKEN if TOKEN.lower().startswith("bearer ")
        else f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def gql(query, variables=None):
    for attempt in range(3):
        try:
            r = requests.post(
                URL,
                headers=headers(),
                json={"query": query, "variables": variables or {}},
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue

            if r.status_code != 200:
                time.sleep(attempt + 1)
                continue

            data = r.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(data["errors"], ensure_ascii=False)[:2000],
                    flush=True
                )

            return data

        except Exception as e:
            print("❌ GraphQL:", e, flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    d = gql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    u = ((d or {}).get("data") or {}).get("currentUser")

    if not u:
        return False

    print(
        "✅ Sorare:",
        u.get("nickname") or u.get("slug"),
        flush=True
    )

    print(
        "🔐 Stark key account:",
        "PRESENTE" if u.get("starkKey") else "NON DISPONIBILE",
        flush=True
    )

    print(
        "🔑 Solana private key:",
        "PRESENTE" if SOLANA else "NON DISPONIBILE",
        flush=True
    )

    return True


# ============================================================
# OWNERSHIP
# ============================================================

def ownership(asset):
    """
    Verifica direttamente tramite Sorare che l'asset
    appartenga ancora al wallet/account corrente.
    """

    d = gql("""
        query Ownership($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                owner {
                    __typename
                    ... on User {
                        id
                    }
                }
            }
        }
    """, {"ids": [asset]})

    arr = ((d or {}).get("data") or {}).get("anyCards") or []

    for c in arr:
        if norm(c.get("assetId")) != norm(asset):
            continue

        owner = c.get("owner") or {}

        # Per gli asset Solana, il controllo definitivo
        # viene comunque effettuato da createSingleSaleOffer.
        if owner:
            return True

    return None


def already_not_owned(card):
    return norm(card.get("status")) == "not_owned"


# ============================================================
# CARD DETAILS
# ============================================================

def details(asset):
    d = gql("""
        query Cards($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                slug
                name
                rarityTyped
                seasonYear
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

    arr = ((d or {}).get("data") or {}).get("anyCards") or []
    return arr[0] if arr else None


# ============================================================
# ACTIVE OFFER
# ============================================================

def active_offer(asset):
    d = gql("""
        query Offer($assetId: String!) {
            anyCards(assetIds: [$assetId]) {
                assetId
                liveSingleSaleOffer {
                    id
                    startDate
                    endDate
                    receiverSide {
                        amounts {
                            eurCents
                            usdCents
                        }
                    }
                }
            }
        }
    """, {"assetId": asset})

    for c in ((d or {}).get("data") or {}).get("anyCards") or []:
        if norm(c.get("assetId")) != norm(asset):
            continue

        o = c.get("liveSingleSaleOffer") or {}

        if o.get("id"):
            print("🟢 PRE-CHECK → CARTA GIÀ IN VENDITA", flush=True)
            print("🟢 OFFER →", o["id"], flush=True)
            return o

    print("🔵 PRE-CHECK → nessuna offerta pubblica attiva", flush=True)
    return None


# ============================================================
# FLOOR
# ============================================================

def usd_eur(cents):
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )
        return round(cents * float(r.json()["rates"]["EUR"]))
    except Exception:
        return None


def amount(a):
    if not isinstance(a, dict):
        return None

    try:
        x = int(a.get("eurCents") or 0)
        if x > 0:
            return x
    except Exception:
        pass

    try:
        x = int(a.get("usdCents") or 0)
        if x > 0:
            return usd_eur(x)
    except Exception:
        pass

    return None


def floor(card):
    p = card.get("anyPlayer") or {}
    slug = norm(p.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    if not slug or not rarity:
        return None

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    d = gql("""
        query Offers($slug: String, $first: Int) {
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
                                anyPlayer { slug }
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
    """, {"slug": slug, "first": 50})

    nodes = (
        (((d or {}).get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for o in nodes:
        for c in (o.get("senderSide") or {}).get("anyCards") or []:
            p2 = c.get("anyPlayer") or {}

            try:
                same_season = int(c.get("seasonYear")) == season
            except Exception:
                same_season = False

            if (
                same_season
                and norm(c.get("rarityTyped")) == rarity
                and norm(p2.get("slug")) == slug
            ):
                x = amount(
                    (o.get("receiverSide") or {}).get("amounts")
                )
                if x is not None:
                    prices.append(x)
                break

    print(
        f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return min(prices) if len(prices) >= MIN_LISTINGS else None


# ============================================================
# VALIDATION
# ============================================================

def validate(card):
    if (
        norm(card.get("slug")) == norm(KULENOVIC_SLUG)
        or norm(card.get("assetId")) == norm(KULENOVIC)
    ):
        return False, "KULENOVIC"

    if norm(card.get("rarityTyped")) != "limited":
        return False, "RARITY"

    # SEALED: qualunque campo boolean/stringa disponibile
    # viene considerato.
    sealed = (
        card.get("sealed") is True
        or norm(card.get("sealed")) in {"true", "sealed"}
        or norm(card.get("status")) == "sealed"
    )

    if sealed:
        return False, "SEALED"

    f = floor(card)

    if f is None:
        return False, "FLOOR_UNKNOWN"

    if f > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, max(f, MIN_PRICE)


# ============================================================
# SIGNING
# ============================================================

def sign(auths):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    js = r'''
const {
  signAuthorizationRequest
} = require("@sorare/crypto");

const {
  createSignableMessage,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
} = require("@solana/kit");

const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

const A =
"123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
"abcdefghijkmnopqrstuvwxyz";

function b58(v) {
  let n = 0n;
  for (const c of String(v).trim()) {
    const i = A.indexOf(c);
    if (i < 0) throw Error("Base58 non valido");
    n = n * 58n + BigInt(i);
  }

  let h = n ? n.toString(16) : "";
  if (h.length % 2) h = "0" + h;

  let b = h ? Buffer.from(h, "hex") : Buffer.alloc(0);
  let z = 0;

  for (const c of String(v).trim()) {
    if (c !== "1") break;
    z++;
  }

  return new Uint8Array(Buffer.concat([Buffer.alloc(z), b]));
}

function b58e(data) {
  const b = Buffer.from(data);
  let n = 0n;

  for (const x of b)
    n = n * 256n + BigInt(x);

  let s = "";

  while (n) {
    s = A[Number(n % 58n)] + s;
    n /= 58n;
  }

  let z = 0;

  for (const x of b) {
    if (x) break;
    z++;
  }

  return "1".repeat(z) + s;
}

function key(v) {
  let x;

  try {
    x = b58(v);
    if (x.length === 32 || x.length === 64) return x;
  } catch (_) {}

  let h = String(v).replace(/^0x/, "");

  if (/^[0-9a-fA-F]+$/.test(h) && h.length % 2 === 0) {
    x = new Uint8Array(Buffer.from(h, "hex"));
    if (x.length === 32 || x.length === 64) return x;
  }

  throw Error("Chiave Solana non valida");
}

async function solana(auth) {
  const r = auth.request;
  let k = key(input.solanaPrivateKey);

  if (k.length === 64) k = k.slice(0, 32);

  const kp = await createKeyPairFromPrivateKeyBytes(k);
  const signer = createSignerFromKeyPair(kp);

  console.error("🔑 Solana signer → " + signer.address);

  if (signer.address !== r.senderAddress)
    throw Error("La chiave Solana non corrisponde al senderAddress");

  const msg = [
    "TRANSFER",
    r.transferProxyProgramAddress,
    r.merkleTreeAddress,
    r.leafIndex.toString(),
    r.nonce,
    r.expirationTimestamp.toString(),
    r.receiverAddress,
    "0x",
    r.originator
  ].join(":");

  const crypto = require("crypto");

  const hash = crypto
    .createHash("sha256")
    .update(Buffer.from(msg))
    .digest();

  const signed = await signer.signMessages([
    createSignableMessage(new Uint8Array(hash))
  ]);

  return {
    fingerprint: auth.fingerprint,
    solanaTokenTransferApproval: {
      signature: b58e(signed[0][signer.address]),
      nonce: r.nonce,
      expirationTimestamp: r.expirationTimestamp
    }
  };
}

function stark(auth) {
  const r = auth.request;
  const sig = signAuthorizationRequest(input.starkPrivateKey, r);
  const base = { fingerprint: auth.fingerprint };

  if (r.__typename === "StarkexTransferAuthorizationRequest")
    return {...base, starkexTransferApproval:{
      nonce:r.nonce,
      expirationTimestamp:r.expirationTimestamp,
      signature:sig
    }};

  if (r.__typename === "StarkexLimitOrderAuthorizationRequest")
    return {...base, starkexLimitOrderApproval:{
      nonce:r.nonce,
      expirationTimestamp:r.expirationTimestamp,
      signature:sig
    }};

  if (r.__typename === "MangopayWalletTransferAuthorizationRequest")
    return {...base, mangopayWalletTransferApproval:{
      nonce:r.nonce,
      signature:sig
    }};

  throw Error("Authorization non supportata: " + r.__typename);
}

(async()=>{
  const out=[];

  for(const a of input.authorizations) {
    out.push(
      a.request.__typename ===
      "SolanaTokenTransferAuthorizationRequest"
      ? await solana(a)
      : stark(a)
    );
  }

  process.stdout.write(JSON.stringify(out));
})().catch(e=>{
  console.error(e.stack || e);
  process.exit(1);
});
'''

    p = subprocess.run(
        [node, "-e", js],
        input=json.dumps({
            "starkPrivateKey": STARK,
            "solanaPrivateKey": SOLANA,
            "authorizations": auths
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.stderr:
        print(p.stderr.strip(), flush=True)

    if p.returncode:
        raise RuntimeError(p.stderr or "Firma fallita")

    return json.loads(p.stdout)


# ============================================================
# SALE
# ============================================================

PREPARE = """
mutation Prepare($input: prepareOfferInput!) {
  prepareOffer(input:$input) {
    authorizations {
      fingerprint
      request {
        __typename

        ... on StarkexTransferAuthorizationRequest {
          amount condition expirationTimestamp nonce
          receiverPublicKey receiverVaultId senderVaultId token
          feeInfoUser {
            feeLimit sourceVaultId tokenId
          }
        }

        ... on StarkexLimitOrderAuthorizationRequest {
          vaultIdSell vaultIdBuy amountSell amountBuy
          tokenSell tokenBuy nonce expirationTimestamp
          feeInfo {
            feeLimit tokenId sourceVaultId
          }
        }

        ... on MangopayWalletTransferAuthorizationRequest {
          nonce amount currency operationHash mangopayWalletId
        }

        ... on SolanaTokenTransferAuthorizationRequest {
          assetId leafIndex merkleTreeAddress originator
          receiverAddress senderAddress expirationTimestamp
          nonce transferProxyProgramAddress
        }
      }
    }
    errors { message }
  }
}
"""


def create_sale(card, price):
    asset = aid(card)

    if DRY_RUN:
        print(f"🟡 DRY RUN → {label(card)} → {eur(price)}", flush=True)
        return "DRY-RUN", None, None

    inp = {
        "sendAssetIds": [asset],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    print(f"📦 prepareOffer → EUR {eur(price)}", flush=True)

    d = gql(PREPARE, {"input": inp})
    r = ((d or {}).get("data") or {}).get("prepareOffer")

    if not r or r.get("errors"):
        return None, None, "PREPARE_FAILED"

    auths = r.get("authorizations") or []

    if not auths:
        return None, None, "NO_AUTH"

    try:
        approvals = sign(auths)
    except Exception as e:
        return None, None, str(e)

    mutation = """
    mutation Create($input:createSingleSaleOfferInput!) {
      createSingleSaleOffer(input:$input) {
        tokenOffer {
          id startDate endDate
        }
        errors { message }
      }
    }
    """

    inp = {
        "approvals": approvals,
        "dealId": new_id(),
        "assetId": asset,
        "settlementCurrencies": "EUR",
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    print(
        f"📤 createSingleSaleOffer → EUR {eur(price)}",
        flush=True
    )

    d = gql(mutation, {"input": inp})
    r = ((d or {}).get("data") or {}).get("createSingleSaleOffer")

    if not r:
        return None, None, "CREATE_FAILED"

    errors = r.get("errors") or []

    if errors:
        text = " ".join(
            str(x.get("message", ""))
            for x in errors
            if isinstance(x, dict)
        )

        if "not owned by" in norm(text):
            print("🚫 CARTA NON PIÙ POSSEDUTA DAL WALLET SOLANA", flush=True)
            return None, None, "NOT_OWNED"

        if "active public offer already exists" in norm(text):
            o = active_offer(asset)
            if o:
                return o["id"], o, "ALREADY_LISTED"

        print("❌ createSingleSaleOffer:", errors, flush=True)
        return None, None, text

    o = r.get("tokenOffer") or {}

    if not o.get("id"):
        return None, None, "NO_OFFER_ID"

    print("✅ INSERZIONE CREATA →", o["id"], flush=True)

    return o["id"], o, None


# ============================================================
# PROCESS
# ============================================================

def process(card):
    asset = aid(card)

    if not asset:
        return

    # BLOCCO IMMEDIATO
    if already_not_owned(card):
        return

    print(f"\n💰 AUTOSELL CHECK → {asset}", flush=True)

    # Controllo offerta prima di fare qualsiasi cosa.
    active = active_offer(asset)

    if active:
        update(
            asset,
            status="SELLING",
            offer_id=active["id"],
            price=active.get("price") or card.get("sale_price_eur"),
            start=active.get("startDate"),
            end=active.get("endDate")
        )
        return

    d = details(asset)

    if not d:
        update(asset, status="da_vendere", error="CARD_DETAILS")
        return

    # Se l'asset non è più disponibile nei dati Sorare,
    # non tentiamo la vendita.
    if not d.get("assetId"):
        update(asset, status="NOT_OWNED", error="NOT_OWNED")
        return

    print(
        f"🃏 {d.get('name') or d.get('slug')} "
        f"{d.get('seasonYear')} • "
        f"{d.get('rarityTyped')}",
        flush=True
    )

    ok, result = validate(d)

    if not ok:
        msg = {
            "KULENOVIC": "KULENOVIC PROTETTO",
            "RARITY": "RARITÀ NON LIMITED",
            "SEALED": "CARTA SEALED",
            "FLOOR_UNKNOWN": "FLOOR NON DISPONIBILE",
            "FLOOR_HIGH": "FLOOR SOPRA €0.70"
        }.get(result, result)

        print("🚫 ESCLUSA →", msg, flush=True)

        update(
            asset,
            status="BLOCKED" if result in
            {"KULENOVIC", "RARITY", "SEALED"}
            else "da_vendere",
            error=result
        )
        return

    price = result

    print(
        f"✅ PREZZO DI VENDITA → {eur(price)}",
        flush=True
    )

    oid, offer, error = create_sale(d, price)

    if error == "NOT_OWNED":
        update(
            asset,
            status="NOT_OWNED",
            error="NOT_OWNED"
        )
        print(
            f"🧹 STATE → {asset} → NOT_OWNED",
            flush=True
        )
        print(
            "🚫 VENDITA BLOCCATA → CARTA NON PIÙ POSSEDUTA",
            flush=True
        )
        return

    if not oid:
        update(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )
        return

    if oid == "DRY-RUN":
        update(
            asset,
            status="SELLING",
            offer_id=oid,
            price=price
        )
        return

    update(
        asset,
        status="SELLING",
        offer_id=oid,
        price=price,
        start=(offer or {}).get("startDate"),
        end=(offer or {}).get("endDate")
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(d)} | offer={oid} | price={eur(price)}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def renew(card):
    asset = aid(card)
    price = card.get("sale_price_eur")

    if not asset or price is None:
        return

    active = active_offer(asset)

    if active:
        update(
            asset,
            status="SELLING",
            offer_id=active["id"],
            price=active.get("price") or price,
            start=active.get("startDate"),
            end=active.get("endDate")
        )
        return

    d = details(asset)

    if not d:
        update(asset, status="NOT_OWNED", error="NOT_OWNED")
        return

    oid, offer, error = create_sale(d, max(int(price), MIN_PRICE))

    if error == "NOT_OWNED":
        update(asset, status="NOT_OWNED", error="NOT_OWNED")
        print(
            f"🧹 STATE → {asset} → NOT_OWNED",
            flush=True
        )
        return

    if not oid:
        update(asset, status="da_vendere", error="RENEW_FAILED")
        return

    update(
        asset,
        status="SELLING",
        offer_id=oid,
        price=price,
        start=(offer or {}).get("startDate"),
        end=(offer or {}).get("endDate")
    )


def recovery():
    active = [
        c for c in cards()
        if norm(c.get("status")) == "selling"
    ]

    if not active:
        print("🔄 Recovery: nessuna carta SELLING.", flush=True)
        return

    print(
        f"🔄 Recovery: {len(active)} carte SELLING",
        flush=True
    )

    ts = int(time.time())

    for c in active:
        end = c.get("sale_end_date")

        if not end:
            print(
                f"   └─ {aid(c)} | scadenza non disponibile",
                flush=True
            )
            continue

        try:
            end_ts = int(
                time.mktime(
                    time.strptime(
                        end,
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                )
            )
        except Exception:
            continue

        if ts >= end_ts:
            print(
                f"⏰ OFFERTA SCADUTA → {aid(c)}",
                flush=True
            )
            renew(c)


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print("📦 VERSIONE:", VERSION, flush=True)
    print("🧪 DRY_RUN=", DRY_RUN, flush=True)
    print("💰 FLOOR MAX: €0.70", flush=True)
    print("💰 PREZZO MINIMO: €0.30", flush=True)
    print("📊 LISTING MINIME: 5", flush=True)
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("🔒 SEALED: MAI VENDUTO", flush=True)
    print("🛡️ COVERAGE: DISABILITATA", flush=True)
    print("🛡️ SOURCE: AUTOBUY / SWAP", flush=True)
    print("💶 SETTLEMENT: EUR", flush=True)
    print("🟢 PRE-CHECK: GIÀ IN VENDITA → NON RIPROVARE", flush=True)
    print("♻️ SCADENZA: RINNOVO AUTOMATICO", flush=True)
    print("💰 RINNOVO: STESSO PREZZO, FLOOR IGNORATO", flush=True)
    print("🧹 NON OWNED: BLOCCO PERMANENTE NELLO STATE", flush=True)
    print("💾 STORAGE:", STATE_FILE, flush=True)

    try:
        headers()
        load()
    except Exception as e:
        print("❌ Configurazione:", e, flush=True)
        return

    if not check_account():
        return

    while True:
        try:
            recovery()

            todo = sellable()

            print(
                f"🗄️ Carte DA VENDERE: {len(todo)}",
                flush=True
            )

            for c in todo:
                try:
                    process(c)
                except Exception as e:
                    print(
                        f"❌ AutoSell {aid(c)}: {e}",
                        flush=True
                    )

            time.sleep(INTERVAL)

        except Exception as e:
            print("❌ Worker:", e, flush=True)
            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    c = cards()

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "floor_max": "€0.70",
        "min_sell_price": "€0.30",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "kulenovic": "NEVER_SELL",
        "sealed": "NEVER_SELL",
        "not_owned": "PERMANENT_BLOCK",
        "settlement": "EUR",
        "renew_expired": "SAME_PRICE",
        "cards": len(c),
        "da_vendere": sum(
            norm(x.get("status")) in {"da_vendere", "ready"}
            for x in c
        ),
        "selling": sum(
            norm(x.get("status")) == "selling"
            for x in c
        ),
        "not_owned": sum(
            norm(x.get("status")) == "not_owned"
            for x in c
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
def cards_api():
    c = cards()
    return jsonify({"count": len(c), "cards": c})


# ============================================================
# MAIN
# ============================================================

def start():
    global worker_started

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
    start()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False
    )
