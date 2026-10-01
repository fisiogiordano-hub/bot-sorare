import os, json, time, uuid, shutil, subprocess, threading, requests
from flask import Flask, jsonify

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

MAX_FLOOR = 70
MIN_PRICE = 30
MIN_LISTINGS = 5

TECH_ETH = 0.0002
TECH_STEP = 0.0001

VERSION = "AUTOSell-14-AUTOBUY-SWAP"

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

lock = threading.RLock()
worker_started = False

ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


# ============================================================
# UTILS
# ============================================================

def norm(x):
    return str(x or "").strip().lower()


def now():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime()
    )


def uid():
    return str(uuid.uuid4())


def asset(card):
    return str(
        card.get("assetId")
        or card.get("asset_id")
        or ""
    ).strip()


def eur(cents):
    return (
        "N/D"
        if cents is None
        else f"€{cents / 100:.2f}"
    )


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


def load():
    path = os.path.abspath(STATE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True) \
        if os.path.dirname(path) else None

    if not os.path.exists(path):
        save(default_state())

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            d = json.load(f)

        if not isinstance(d, dict):
            return default_state()

        for k, v in default_state().items():
            d.setdefault(k, v)

        return d

    except Exception as e:
        print("STATE READ:", e, flush=True)
        return default_state()


def save(data):
    tmp = f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"

    try:
        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print("STATE WRITE:", e, flush=True)

        try:
            os.remove(tmp)
        except Exception:
            pass

        return False


def cards():
    with lock:
        x = load().get("acquired_cards", [])
        return x if isinstance(x, list) else []


def update_card(
    asset_id,
    status=None,
    offer_id=None,
    price=None,
    end_date=None,
    error=None
):
    wanted = norm(asset_id)

    with lock:
        data = load()

        for c in data["acquired_cards"]:

            if not isinstance(c, dict):
                continue

            if norm(asset(c)) != wanted:
                continue

            if status is not None:
                c["status"] = status

            if offer_id:
                c["sale_offer_id"] = offer_id

            if price is not None:
                c["sale_price"] = price

            if end_date is not None:
                c["sale_end_date"] = end_date

            c["last_error"] = error

            data["updated_at"] = int(time.time())

            return save(data)

    return False


# ============================================================
# SORARE
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    h = {
        "Authorization":
            TOKEN if TOKEN.lower().startswith("bearer ")
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
                json={
                    "query": query,
                    "variables": variables or {}
                },
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue

            if r.status_code != 200:
                print(
                    r.text[:1000],
                    flush=True
                )
                time.sleep(attempt + 1)
                continue

            d = r.json()

            if d.get("errors"):
                print(
                    "GraphQL:",
                    json.dumps(
                        d["errors"],
                        ensure_ascii=False
                    )[:2000],
                    flush=True
                )

            return d

        except Exception as e:
            print("GraphQL:", e, flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT / CARD
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

    u = (
        ((d or {}).get("data") or {})
        .get("currentUser")
    )

    if not u:
        return False

    print(
        "✅ Sorare:",
        u.get("nickname") or u.get("slug"),
        flush=True
    )

    return True


def card_details(asset_id):

    d = gql("""
        query($ids:[String!]!) {
            anyCards(assetIds:$ids) {
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
    """, {"ids": [asset_id]})

    x = (
        ((d or {}).get("data") or {})
        .get("anyCards")
        or []
    )

    return x[0] if x else None


# ============================================================
# ACTIVE OFFER
# ============================================================

def active_offer(asset_id):

    d = gql("""
        query($id:String!) {
            anyCards(assetIds:[$id]) {
                assetId
                liveSingleSaleOffer {
                    id
                    endDate
                }
            }
        }
    """, {"id": asset_id})

    x = (
        ((d or {}).get("data") or {})
        .get("anyCards")
        or []
    )

    for c in x:

        if norm(c.get("assetId")) != norm(asset_id):
            continue

        o = c.get("liveSingleSaleOffer") or {}

        if o.get("id"):
            return o

    return None


# ============================================================
# EUR / FLOOR
# ============================================================

def usd_to_eur(cents):

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

        return round(cents * rate)

    except Exception:
        return None


def amount_eur(a):

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
            return usd_to_eur(x)
    except Exception:
        pass

    return None


def get_floor(card):

    p = card.get("anyPlayer") or {}
    slug = norm(p.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    if not slug or not rarity:
        return None

    d = gql("""
        query($slug:String,$first:Int) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug:$slug
                    first:$first
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
    """, {
        "slug": slug,
        "first": 50
    })

    nodes = (
        (((d or {}).get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in nodes:

        for c in (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        ):

            cp = c.get("anyPlayer") or {}

            try:
                same_season = (
                    int(c.get("seasonYear"))
                    == season
                )
            except Exception:
                continue

            if (
                same_season
                and norm(cp.get("slug")) == slug
                and norm(c.get("rarityTyped")) == rarity
            ):
                x = amount_eur(
                    (offer.get("receiverSide") or {})
                    .get("amounts")
                )

                if x is not None:
                    prices.append(x)

                break

    print(
        f"📊 Listing: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return (
        min(prices)
        if len(prices) >= MIN_LISTINGS
        else None
    )


# ============================================================
# VALIDATION / PRICE
# ============================================================

def validate(card):

    if (
        norm(card.get("slug"))
        == norm(KULENOVIC_SLUG)
        or norm(asset(card))
        == norm(KULENOVIC_ASSET)
    ):
        return False, "KULENOVIC"

    if norm(
        card.get("rarityTyped")
    ).upper() != "LIMITED":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor > MAX_FLOOR:
        return False, "FLOOR_HIGH"

    # NUOVO MINIMO ASSOLUTO
    price = max(floor, MIN_PRICE)

    return True, price


# ============================================================
# ETH
# ============================================================

def eth_rate():

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "ethereum",
                "vs_currencies": "eur"
            },
            timeout=10
        )

        x = float(
            r.json()["ethereum"]["eur"]
        )

        return x if x > 0 else None

    except Exception as e:
        print("ETH/EUR:", e, flush=True)
        return None


def eth_eur(eth, rate):

    try:
        return max(
            1,
            round(float(eth) * float(rate) * 100)
        )
    except Exception:
        return None


# ============================================================
# SOLANA SIGNING
# ============================================================

def sign_authorizations(auths):

    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError("Node.js non disponibile")

    js = r'''
const crypto = require("crypto");

const {
  signAuthorizationRequest
} = require("@sorare/crypto");

const {
  createSignableMessage,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
} = require("@solana/kit");

const input = JSON.parse(
  require("fs").readFileSync(0,"utf8")
);

const A =
 "123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
 "abcdefghijkmnopqrstuvwxyz";

function b58d(v){
 let n=0n;
 for(const c of String(v).trim()){
  const i=A.indexOf(c);
  if(i<0) throw Error("Base58 non valido");
  n=n*58n+BigInt(i);
 }
 let b=n===0n?Buffer.alloc(0):
   Buffer.from(
     (n.toString(16).length%2?"0":"")
     +n.toString(16),"hex");
 let z=0;
 for(const c of String(v).trim()){
  if(c!=="1") break;
  z++;
 }
 return new Uint8Array(
   Buffer.concat([Buffer.alloc(z),b])
 );
}

function b58e(data){
 const b=Buffer.from(data);
 let n=0n;
 for(const x of b) n=n*256n+BigInt(x);
 let s="";
 while(n>0n){
  s=A[Number(n%58n)]+s;
  n/=58n;
 }
 let z=0;
 for(const x of b){
  if(x!==0) break;
  z++;
 }
 return "1".repeat(z)+s;
}

function key(v){
 try{
  const b=b58d(v);
  if(b.length===32||b.length===64)return b;
 }catch(_){}

 let h=String(v||"");
 if(h.startsWith("0x"))h=h.slice(2);

 if(
  /^[0-9a-fA-F]+$/.test(h)&&
  h.length%2===0
 ){
  const b=new Uint8Array(Buffer.from(h,"hex"));
  if(b.length===32||b.length===64)return b;
 }

 throw Error("Chiave Solana non valida");
}

async function solSigner(v){
 let b=key(v);
 if(b.length===64)b=b.slice(0,32);

 const kp=await createKeyPairFromPrivateKeyBytes(b);
 return createSignerFromKeyPair(kp);
}

async function sol(auth){

 const r=auth.request;
 const signer=await solSigner(
   input.solanaPrivateKey
 );

 if(signer.address!==r.senderAddress)
   throw Error(
     "Chiave Solana diversa dal senderAddress"
   );

 const message=[
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

 const hash=crypto.createHash("sha256")
   .update(Buffer.from(message))
   .digest();

 const signable=createSignableMessage(
   new Uint8Array(hash)
 );

 const sigs=await signer.signMessages([signable]);
 const sig=sigs[0][signer.address];

 return {
  fingerprint:auth.fingerprint,
  solanaTokenTransferApproval:{
   signature:b58e(sig),
   nonce:r.nonce,
   expirationTimestamp:r.expirationTimestamp
  }
}

function stark(auth){

 const r=auth.request;

 const signature=signAuthorizationRequest(
   input.starkPrivateKey,r
 );

 const base={
   fingerprint:auth.fingerprint
 };

 if(r.__typename==="StarkexTransferAuthorizationRequest")
  return {
   ...base,
   starkexTransferApproval:{
    nonce:r.nonce,
    expirationTimestamp:r.expirationTimestamp,
    signature
   }
  };

 if(r.__typename==="StarkexLimitOrderAuthorizationRequest")
  return {
   ...base,
   starkexLimitOrderApproval:{
    nonce:r.nonce,
    expirationTimestamp:r.expirationTimestamp,
    signature
   }
  };

 if(r.__typename==="MangopayWalletTransferAuthorizationRequest")
  return {
   ...base,
   mangopayWalletTransferApproval:{
    nonce:r.nonce,
    signature
   }
  };

 throw Error(
   "Authorization non supportata: "
   +r.__typename
 );
}

async function main(){

 const out=[];

 for(const a of input.authorizations){

  const t=(a.request||{}).__typename;

  out.push(
   t==="SolanaTokenTransferAuthorizationRequest"
   ?await sol(a)
   :stark(a)
  );
 }

 process.stdout.write(JSON.stringify(out));
}

main().catch(e=>{
 console.error(e.stack||e);
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
        print(p.stderr, flush=True)

    if p.returncode:
        raise RuntimeError(
            p.stderr or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# PREPARE / CREATE
# ============================================================

PREPARE = """
mutation($input:prepareOfferInput!){
 prepareOffer(input:$input){
  authorizations{
   fingerprint
   request{
    __typename

    ... on StarkexTransferAuthorizationRequest{
     amount condition expirationTimestamp nonce
     receiverPublicKey receiverVaultId senderVaultId token
     feeInfoUser{
      feeLimit sourceVaultId tokenId
     }
    }

    ... on StarkexLimitOrderAuthorizationRequest{
     vaultIdSell vaultIdBuy amountSell amountBuy
     tokenSell tokenBuy nonce expirationTimestamp
     feeInfo{
      feeLimit tokenId sourceVaultId
     }
    }

    ... on MangopayWalletTransferAuthorizationRequest{
     nonce amount currency operationHash mangopayWalletId
    }

    ... on SolanaTokenTransferAuthorizationRequest{
     assetId leafIndex merkleTreeAddress originator
     receiverAddress senderAddress expirationTimestamp nonce
     transferProxyProgramAddress
    }
   }
  }
  errors{message}
 }
}
"""


def prepare(asset_id, price):

    d = gql(
        PREPARE,
        {
            "input": {
                "sendAssetIds": [asset_id],
                "receiveAssetIds": [],
                "settlementCurrencies": ["EUR"],
                "receiveAmount": {
                    "amount": str(price),
                    "currency": "EUR"
                },
                "clientMutationId": uid()
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not r or r.get("errors"):
        print(
            "prepareOffer:",
            json.dumps(
                (r or {}).get("errors") or [],
                ensure_ascii=False
            ),
            flush=True
        )
        return None

    return r.get("authorizations") or []


def create_once(card, price):

    a = asset(card)

    if not a:
        return None, None

    if DRY_RUN:
        print(
            f"🟡 DRY RUN {a} → {eur(price)}",
            flush=True
        )
        return "DRY-RUN", None, None

    auths = prepare(a, price)

    if not auths:
        return None, "PREPARE", None

    try:
        approvals = sign_authorizations(auths)
    except Exception as e:
        return None, str(e), None

    q = """
    mutation($input:createSingleSaleOfferInput!){
     createSingleSaleOffer(input:$input){
      tokenOffer{
       id
       startDate
       endDate
      }
      errors{message}
     }
    }
    """

    d = gql(
        q,
        {
            "input": {
                "approvals": approvals,
                "dealId": uid(),
                "assetId": a,
                "settlementCurrencies": "EUR",
                "receiveAmount": {
                    "amount": str(price),
                    "currency": "EUR"
                },
                "clientMutationId": uid()
            }
        }
    )

    r = (
        ((d or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not r:
        return None, "CREATE", None

    errors = r.get("errors") or []

    if errors:

        text_error = " ".join(
            str(x.get("message", ""))
            for x in errors
            if isinstance(x, dict)
        )

        if "active public offer already exists" in norm(text_error):

            o = active_offer(a)

            return (
                o.get("id") if o else "ALREADY-LISTED",
                "ALREADY-LISTED",
                o.get("endDate") if o else None
            )

        return None, text_error, None

    offer = r.get("tokenOffer") or {}

    if not offer.get("id"):
        return None, "OFFER_ID_MISSING", None

    return (
        offer["id"],
        None,
        offer.get("endDate")
    )


def technical_error(e):
    x = norm(e)
    return (
        "price must be greater than" in x
        or "price must be at least" in x
        or "minimum price" in x
        or "min price" in x
    )


def create_sale(card, price):

    offer, error, end = create_once(
        card,
        price
    )

    if offer:
        return offer, price, end

    if not technical_error(error or ""):
        return None, None, None

    rate = eth_rate()

    if rate is None:
        return None, None, None

    eth = TECH_ETH

    for _ in range(1000):

        p = eth_eur(eth, rate)

        if p is None:
            return None, None, None

        offer, error, end = create_once(
            card,
            p
        )

        if offer:
            return offer, p, end

        if not technical_error(error or ""):
            return None, None, None

        eth = round(
            eth + TECH_STEP,
            4
        )

    return None, None, None


# ============================================================
# SOURCE FILTER
# ============================================================

def allowed_source(card):

    return norm(
        card.get("source")
    ) in {
        "autobuy",
        "swap"
    }


def sellable():

    out = []

    for c in cards():

        if not isinstance(c, dict):
            continue

        if not allowed_source(c):
            continue

        if norm(c.get("status")) not in {
            "da_vendere",
            "ready"
        }:
            continue

        if asset(c):
            out.append(dict(c))

    return out


# ============================================================
# EXPIRATION / RESELL
# ============================================================

def expired(c):

    end = c.get("sale_end_date")

    if not end:
        return False

    try:
        return (
            time.time()
            >= time.mktime(
                time.strptime(
                    end,
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            )
        )
    except Exception:
        return False


def recover_expired():

    for c in cards():

        if not isinstance(c, dict):
            continue

        if norm(c.get("status")) != "selling":
            continue

        if not allowed_source(c):
            continue

        if not expired(c):
            continue

        price = c.get("sale_price")

        if not price:
            continue

        print(
            f"🔄 SCADUTA → "
            f"{asset(c)} → "
            f"ripubblico a {eur(price)}",
            flush=True
        )

        update_card(
            asset(c),
            status="da_vendere",
            error=None
        )


# ============================================================
# PROCESS
# ============================================================

def process(c):

    a = asset(c)

    print(
        f"\n💰 AUTOSELL → {a}",
        flush=True
    )

    # --------------------------------------------------------
    # SE GIÀ LISTATA NON CREARE DUPLICATI
    # --------------------------------------------------------

    existing = active_offer(a)

    if existing:

        print(
            f"🟢 GIÀ IN VENDITA → "
            f"{existing.get('id')}",
            flush=True
        )

        # Se il prezzo è già salvato,
        # non lo tocchiamo.
        update_card(
            a,
            status="selling",
            offer_id=existing.get("id"),
            end_date=existing.get("endDate"),
            error=None
        )

        return

    # --------------------------------------------------------
    # CARD DETAILS
    # --------------------------------------------------------

    details = card_details(a)

    if not details:
        update_card(
            a,
            status="da_vendere",
            error="CARD_DETAILS"
        )
        return

    # --------------------------------------------------------
    # VALIDAZIONE NORMALE
    # --------------------------------------------------------

    ok, result = validate(details)

    if not ok:

        update_card(
            a,
            status=(
                "BLOCKED"
                if result in {
                    "KULENOVIC",
                    "RARITY"
                }
                else "da_vendere"
            ),
            error=result
        )

        print(
            f"🚫 ESCLUSA → {result}",
            flush=True
        )

        return

    price = result

    print(
        f"🎯 PREZZO → {eur(price)}",
        flush=True
    )

    update_card(
        a,
        status="selling",
        error=None
    )

    offer, final_price, end = create_sale(
        details,
        price
    )

    if not offer:

        update_card(
            a,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        return

    update_card(
        a,
        status="selling",
        offer_id=offer,
        price=final_price or price,
        end_date=end,
        error=None
    )

    print(
        f"✅ SELLING → {offer} "
        f"| {eur(final_price or price)}",
        flush=True
    )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        f"🤖 AUTOSELL {VERSION}",
        flush=True
    )

    print(
        "🎯 SOURCE: AUTOBUY + SWAP",
        flush=True
    )

    print(
        "💰 FLOOR MAX: €0.70",
        flush=True
    )

    print(
        "💰 MIN PRICE: €0.30",
        flush=True
    )

    print(
        "🔄 EXPIRED: SAME PREVIOUS PRICE",
        flush=True
    )

    if not check_account():
        return

    while True:

        try:

            # Prima recupera eventuali
            # offerte scadute.
            recover_expired()

            for c in sellable():

                try:
                    process(c)

                except Exception as e:

                    a = asset(c)

                    print(
                        f"❌ {a}: {e}",
                        flush=True
                    )

                    update_card(
                        a,
                        status="da_vendere",
                        error=str(e)
                    )

            time.sleep(INTERVAL)

        except Exception as e:

            print(
                "WORKER:",
                e,
                flush=True
            )

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

        "source": [
            "autobuy",
            "swap"
        ],

        "floor_max": "€0.70",
        "minimum_price": "€0.30",

        "relist_expired":
            "same_previous_price",

        "min_live_listings":
            MIN_LISTINGS,

        "rarity": "LIMITED",
        "kulenovic": "NEVER_SELL",
        "settlement": "EUR",

        "cards": len(c),

        "da_vendere": sum(
            1 for x in c
            if norm(x.get("status"))
            in {"da_vendere", "ready"}
        ),

        "selling": sum(
            1 for x in c
            if norm(x.get("status"))
            == "selling"
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

    c = cards()

    return jsonify({
        "count": len(c),
        "cards": c
    })


# ============================================================
# MAIN
# ============================================================

def start_worker():

    global worker_started

    with lock:

        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()


if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        ),
        debug=False
    )
