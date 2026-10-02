import os,json,time,uuid,shutil,subprocess,threading,requests
from flask import Flask,jsonify

app=Flask(__name__)
URL="https://api.sorare.com/graphql"
TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
STARK=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()
SOLANA=os.getenv("SORARE_SOLANA_PRIVATE_KEY","").strip()
DRY_RUN=os.getenv("DRY_RUN","false").lower()=="true"
INTERVAL=int(os.getenv("INTERVAL","30"))
TIMEOUT=int(os.getenv("TIMEOUT","25"))

MAX_PRICE_CENTS=70
MIN_SELL_PRICE_CENTS=30
MIN_LISTINGS=5
TECHNICAL_START_ETH=.0002
TECHNICAL_STEP_ETH=.0001
RENEWAL_SECONDS=604800

STATE_FILE=os.getenv("BOT_STATE_PATH","bot_state.json").strip()
VERSION="AUTOSell-17.0-EUR-7D-SAME-PRICE"

KULENOVIC_SLUG="sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

lock=threading.RLock()
worker_lock=threading.Lock()
worker_started=False
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def norm(v): return str(v or "").strip().lower()
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
def new_id(): return str(uuid.uuid4())
def asset_id(c): return str(c.get("assetId") or c.get("asset_id") or "").strip()
def eur(v):
    try:return f"€{int(v)/100:.2f}"
    except:return "N/D"

def timestamp(v):
    if v is None:return None
    try:
        n=float(v)
        return n/1000 if n>10_000_000_000 else n
    except:pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp()
    except:return None

def expired(v):
    t=timestamp(v)
    return t is not None and time.time()>=t

def default_state():
    return {"processed_offers":[],"acquired_cards":[],"pending_autobuys":[],"updated_at":int(time.time())}

def save(d):
    tmp=f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(d,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,STATE_FILE)
        return True
    except Exception as e:
        print("❌ State:",e,flush=True)
        try:os.remove(tmp)
        except:pass
        return False

def load():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:d=json.load(f)
        if not isinstance(d,dict):d=default_state()
    except:d=default_state()
    for k,v in default_state().items():d.setdefault(k,v)
    return d

def cards():
    with lock:return list(load()["acquired_cards"])

def update_card(asset,**fields):
    with lock:
        d=load()
        for c in d["acquired_cards"]:
            if norm(asset_id(c))==norm(asset):
                c.update({k:v for k,v in fields.items() if v is not None})
                d["updated_at"]=int(time.time())
                return save(d)
    return False

def upsert_card(card):
    aid=asset_id(card)
    if not aid:return False
    with lock:
        d=load()
        for i,c in enumerate(d["acquired_cards"]):
            if norm(asset_id(c))==norm(aid):
                d["acquired_cards"][i]={**c,**card}
                return save(d)
        d["acquired_cards"].append(card)
        return save(d)

def sellable():
    return [c for c in cards()
            if norm(c.get("status")) in {"da_vendere","ready"} and asset_id(c)]

def selling():
    return [c for c in cards()
            if norm(c.get("status"))=="selling" and asset_id(c)]


# ================= GRAPHQL =================

def headers():
    if not TOKEN:raise RuntimeError("SORARE_JWT_TOKEN mancante")
    h={
        "Authorization":TOKEN if TOKEN.lower().startswith("bearer ") else f"Bearer {TOKEN}",
        "Content-Type":"application/json","Accept":"application/json",
        "User-Agent":f"Sorare-AutoSell/{VERSION}"
    }
    if AUD:h["JWT-AUD"]=AUD
    return h

def gql(query,variables=None):
    for attempt in range(3):
        try:
            r=requests.post(URL,headers=headers(),
                            json={"query":query,"variables":variables or {}},
                            timeout=TIMEOUT)
            print(f"🌐 Sorare HTTP {r.status_code}",flush=True)
            if r.status_code==429:
                time.sleep(2+attempt*2);continue
            if r.status_code!=200:
                print("❌ Sorare:",r.text[:1000],flush=True)
                time.sleep(attempt+1);continue
            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:2000],flush=True)
            return d
        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(attempt+1)
    return None


# ================= CARTE / OFFERTE =================

def card_details(asset):
    d=gql("""query($ids:[String!]!){
      anyCards(assetIds:$ids){
        assetId slug name rarityTyped seasonYear
        anyPlayer{slug displayName activeClub{slug name}}
      }
    }""",{"ids":[asset]})
    a=((d or {}).get("data") or {}).get("anyCards") or []
    return a[0] if a else None

def amount_to_eur(a):
    if not isinstance(a,dict):return None
    try:
        v=int(a.get("eurCents") or 0)
        if v:return v
    except:pass
    return None

def active_offers():
    d=gql("""query($first:Int){
      tokens{
        liveSingleSaleOffers(first:$first){
          nodes{
            id startDate endDate
            senderSide{
              anyCards{
                assetId slug name rarityTyped seasonYear
                anyPlayer{slug displayName}
              }
            }
            receiverSide{amounts{eurCents usdCents}}
          }
        }
      }
    }""",{"first":100})
    return (((d or {}).get("data") or {}).get("tokens") or {}
            ).get("liveSingleSaleOffers",{}).get("nodes") or []

def find_active_public_offer(asset):
    d=gql("""query($id:String!){
      anyCards(assetIds:[$id]){
        assetId
        liveSingleSaleOffer{
          id startDate endDate
          receiverSide{amounts{eurCents usdCents}}
        }
      }
    }""",{"id":asset})
    for c in ((d or {}).get("data") or {}).get("anyCards") or []:
        o=c.get("liveSingleSaleOffer") or {}
        if o.get("id"):
            return {
                "id":o["id"],
                "endDate":o.get("endDate"),
                "price":amount_to_eur((o.get("receiverSide") or {}).get("amounts"))
            }
    for o in active_offers():
        for c in ((o.get("senderSide") or {}).get("anyCards") or []):
            if norm(c.get("assetId"))==norm(asset):
                return {
                    "id":o.get("id"),
                    "endDate":o.get("endDate"),
                    "price":amount_to_eur((o.get("receiverSide") or {}).get("amounts"))
                }
    return None

def sync_active_offers():
    offers=active_offers()
    for o in offers:
        price=amount_to_eur((o.get("receiverSide") or {}).get("amounts"))
        for c in ((o.get("senderSide") or {}).get("anyCards") or []):
            aid=asset_id(c)
            if aid:
                old=next((x for x in cards() if norm(asset_id(x))==norm(aid)),{})
                upsert_card({
                    **old,**c,"assetId":aid,"status":"selling",
                    "sale_offer_id":o.get("id"),
                    "sale_price_cents":price if price is not None else old.get("sale_price_cents"),
                    "sale_offer_end_date":o.get("endDate") or old.get("sale_offer_end_date"),
                    "source":old.get("source") or "MANUAL",
                    "last_error":None
                })
    print(f"🔄 Sync offerte attive → {len(offers)}",flush=True)


# ================= NUOVE VENDITE =================

def get_floor(c):
    p=c.get("anyPlayer") or {}
    slug=norm(p.get("slug"));rarity=norm(c.get("rarityTyped"))
    try:season=int(c.get("seasonYear"))
    except:return None

    d=gql("""query($slug:String,$first:Int){
      tokens{
        liveSingleSaleOffers(playerSlug:$slug,first:$first){
          nodes{
            senderSide{anyCards{assetId rarityTyped seasonYear anyPlayer{slug}}}
            receiverSide{amounts{eurCents}}
          }
        }
      }
    }""",{"slug":slug,"first":50})

    prices=[]
    nodes=((((d or {}).get("data") or {}).get("tokens") or {})
           .get("liveSingleSaleOffers") or {}).get("nodes") or []

    for o in nodes:
        for x in ((o.get("senderSide") or {}).get("anyCards") or []):
            if (norm(x.get("rarityTyped"))==rarity and
                norm((x.get("anyPlayer") or {}).get("slug"))==slug and
                int(x.get("seasonYear",0))==season):
                p=amount_to_eur((o.get("receiverSide") or {}).get("amounts"))
                if p is not None:prices.append(p)
                break

    return min(prices) if len(prices)>=MIN_LISTINGS else None

def is_kulenovic(c):
    return norm(c.get("slug"))==KULENOVIC_SLUG or norm(c.get("assetId"))==KULENOVIC_ASSET

def is_sealed(c):
    return (norm(c.get("rarityTyped"))=="sealed" or
            "sealed" in norm(c.get("name")) or
            "sealed" in norm(c.get("slug")))

def validate(c):
    if is_kulenovic(c):return False,"KULENOVIC"
    if is_sealed(c):return False,"SEALED"
    if norm(c.get("rarityTyped"))!="limited":return False,"RARITY"
    floor=get_floor(c)
    if floor is None:return False,"FLOOR_UNKNOWN"
    if floor>MAX_PRICE_CENTS:return False,"FLOOR_HIGH"
    return True,max(floor,MIN_SELL_PRICE_CENTS)


# ================= SOLANA — INVARIATO =================

def b58decode(v):
    n=0
    for x in str(v).strip():
        i=B58.find(x)
        if i<0:raise ValueError("Base58 non valido")
        n=n*58+i
    raw=b"" if n==0 else n.to_bytes(max(1,(n.bit_length()+7)//8),"big")
    z=len(str(v))-len(str(v).lstrip("1"))
    return b"\0"*z+raw

def solana_key_info():
    if not SOLANA:raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY mancante")
    try:
        b=b58decode(SOLANA)
        if len(b) in {32,64}:return b
    except:pass
    h=SOLANA[2:] if SOLANA.startswith("0x") else SOLANA
    try:
        b=bytes.fromhex(h)
        if len(b) in {32,64}:return b
    except:pass
    raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY non valida")

def sign_authorizations(auths):
    node=shutil.which("node") or shutil.which("nodejs")
    if not node:raise RuntimeError("Node.js non disponibile")

    types=[(a.get("request") or {}).get("__typename") for a in auths]
    if any(t!="SolanaTokenTransferAuthorizationRequest" for t in types) and not STARK:
        raise RuntimeError("SORARE_STARK_PRIVATE_KEY mancante")
    if any(t=="SolanaTokenTransferAuthorizationRequest" for t in types):
        solana_key_info()

    js=r'''
const crypto=require("crypto");
const {signAuthorizationRequest}=require("@sorare/crypto");
const {createSignableMessage,createKeyPairFromPrivateKeyBytes,createSignerFromKeyPair}=require("@solana/kit");
const fs=require("fs"),input=JSON.parse(fs.readFileSync(0,"utf8"));
const A="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function d(v){let n=0n;for(const c of String(v).trim()){const i=A.indexOf(c);if(i<0)throw Error("Base58");n=n*58n+BigInt(i)}let h=n?n.toString(16):"";if(h.length%2)h="0"+h;let b=h?Buffer.from(h,"hex"):Buffer.alloc(0),z=0;for(const c of String(v).trim()){if(c!="1")break;z++}return new Uint8Array(Buffer.concat([Buffer.alloc(z),b]))}
function e(x){let b=Buffer.from(x),n=0n;for(const q of b)n=n*256n+BigInt(q);let s="";while(n){let r=Number(n%58n);s=A[r]+s;n/=58n}let z=0;for(const q of b){if(q)break;z++}return"1".repeat(z)+s}

async function sol(a){
 let k=d(input.solanaPrivateKey);if(k.length===64)k=k.slice(0,32);if(k.length!==32)throw Error("Solana key");
 let kp=await createKeyPairFromPrivateKeyBytes(k),sg=createSignerFromKeyPair(kp),r=a.request;
 if(sg.address!==r.senderAddress)throw Error("Solana key/sender mismatch");
 let m=["TRANSFER",r.transferProxyProgramAddress,r.merkleTreeAddress,r.leafIndex.toString(),r.nonce,r.expirationTimestamp.toString(),r.receiverAddress,"0x",r.originator].join(":");
 let h=crypto.createHash("sha256").update(Buffer.from(m)).digest();
 let z=await sg.signMessages([createSignableMessage(new Uint8Array(h))]);
 return{fingerprint:a.fingerprint,solanaTokenTransferApproval:{signature:e(z[0][sg.address]),nonce:r.nonce,expirationTimestamp:r.expirationTimestamp}};
}

function stark(a){
 let r=a.request,s=signAuthorizationRequest(input.starkPrivateKey,r),b={fingerprint:a.fingerprint};
 if(r.__typename==="StarkexTransferAuthorizationRequest")
   return{...b,starkexTransferApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s}};
 if(r.__typename==="StarkexLimitOrderAuthorizationRequest")
   return{...b,starkexLimitOrderApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s}};
 if(r.__typename==="MangopayWalletTransferAuthorizationRequest")
   return{...b,mangopayWalletTransferApproval:{nonce:r.nonce,signature:s}};
 throw Error("Authorization "+r.__typename);
}

(async()=>{
 let out=[];
 for(const a of input.authorizations){
  let t=(a.request||{}).__typename;
  out.push(t==="SolanaTokenTransferAuthorizationRequest"?await sol(a):stark(a))
 }
 process.stdout.write(JSON.stringify(out))
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
'''

    p=subprocess.run(
        [node,"-e",js],
        input=json.dumps({
            "starkPrivateKey":STARK,
            "solanaPrivateKey":SOLANA,
            "authorizations":auths
        }),
        text=True,capture_output=True,timeout=TIMEOUT)

    if p.stderr:print(p.stderr.strip(),flush=True)
    if p.returncode:raise RuntimeError(p.stderr.strip() or "Firma fallita")
    return json.loads(p.stdout)


# ================= CREATE SALE =================

PREPARE="""
mutation($input:prepareOfferInput!){
 prepareOffer(input:$input){
  authorizations{
   fingerprint
   request{
    __typename
    ... on StarkexTransferAuthorizationRequest{
     amount condition expirationTimestamp nonce receiverPublicKey receiverVaultId senderVaultId token
     feeInfoUser{feeLimit sourceVaultId tokenId}
    }
    ... on StarkexLimitOrderAuthorizationRequest{
     vaultIdSell vaultIdBuy amountSell amountBuy tokenSell tokenBuy nonce expirationTimestamp
     feeInfo{feeLimit tokenId sourceVaultId}
    }
    ... on MangopayWalletTransferAuthorizationRequest{
     nonce amount currency operationHash mangopayWalletId
    }
    ... on SolanaTokenTransferAuthorizationRequest{
     assetId leafIndex merkleTreeAddress originator receiverAddress senderAddress expirationTimestamp nonce transferProxyProgramAddress
    }
   }
  }
  errors{message}
 }
}
"""

def prepare_sale(asset,price):
    # CORREZIONE: "type" NON esiste più in prepareOfferInput.
    return gql(PREPARE,{
        "input":{
            "sendAssetIds":[asset],
            "receiveAssetIds":[],
            "settlementCurrencies":["EUR"],
            "receiveAmount":{"amount":str(price),"currency":"EUR"},
            "clientMutationId":new_id()
        }
    })

def prepare_authorizations(asset,price):
    d=prepare_sale(asset,price)
    r=((d or {}).get("data") or {}).get("prepareOffer")
    if not r or r.get("errors"):
        return None
    return r.get("authorizations") or None

def create_sale_once(card,price):
    asset=asset_id(card)
    if not asset:return None,"NO_ASSET",None
    if DRY_RUN:return "DRY-RUN",None,None

    auth=prepare_authorizations(asset,price)
    if not auth:return None,"PREPARE_FAILED",None

    try:approvals=sign_authorizations(auth)
    except Exception as e:return None,str(e),None

    q="""mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id startDate endDate}
        errors{message}
      }
    }"""

    d=gql(q,{"input":{
        "approvals":approvals,
        "dealId":new_id(),
        "assetId":asset,
        "settlementCurrencies":["EUR"],
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "duration":RENEWAL_SECONDS,
        "clientMutationId":new_id()
    }})

    r=((d or {}).get("data") or {}).get("createSingleSaleOffer")
    if not r:return None,"CREATE_NO_RESULT",None

    errors=r.get("errors") or []
    if errors:
        text=" ".join(str(x.get("message","")) for x in errors)
        if "active public offer already exists" in norm(text):
            x=find_active_public_offer(asset)
            return ((x or {}).get("id") or "ALREADY-LISTED"),"ALREADY-LISTED",(x or {}).get("endDate")
        if "is not owned by" in norm(text) and "on solana" in norm(text):
            return None,"NOT_OWNED",None
        return None,text,None

    o=r.get("tokenOffer") or {}
    return o.get("id"),None,o.get("endDate")

def technical_error(s):
    s=norm(s)
    return any(x in s for x in [
        "price must be greater than",
        "price must be at least",
        "minimum price",
        "min price"
    ])

def eth_rate():
    try:
        r=requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids":"ethereum","vs_currencies":"eur"},
            timeout=10)
        v=float(r.json()["ethereum"]["eur"])
        return v if v>0 else None
    except:return None

def eth_eur(eth,rate):
    try:return max(1,round(float(eth)*float(rate)*100))
    except:return None

def create_sale(card,price,technical=True):
    oid,err,end=create_sale_once(card,price)
    if oid:return oid,price,end
    if not technical or not technical_error(err or ""):
        return None,None,err

    rate=eth_rate()
    if rate is None:return None,None,"ETH_EUR_UNAVAILABLE"

    eth=TECHNICAL_START_ETH
    for _ in range(1000):
        p=max(eth_eur(eth,rate),MIN_SELL_PRICE_CENTS)
        oid,err,end=create_sale_once(card,p)
        if oid:return oid,p,end
        if not technical_error(err or ""):return None,None,err
        eth=round(eth+TECHNICAL_STEP_ETH,4)

    return None,None,"TECHNICAL_RETRY_LIMIT"


# ================= PROCESS NUOVE CARTE =================

def process(card):
    asset=asset_id(card)
    if not asset:return

    existing=find_active_public_offer(asset)
    if existing:
        update_card(
            asset,status="selling",
            sale_offer_id=existing.get("id"),
            sale_price_cents=existing.get("price"),
            sale_offer_end_date=existing.get("endDate"),
            last_error=None)
        return

    details=card_details(asset)
    if not details:
        update_card(asset,status="da_vendere",last_error="CARD_DETAILS")
        return

    ok,result=validate(details)
    if not ok:
        update_card(
            asset,
            status="blocked" if result in {"KULENOVIC","SEALED","RARITY"} else "da_vendere",
            last_error=result)
        return

    oid,used,err=create_sale(details,int(result),True)

    if err=="NOT_OWNED":
        update_card(asset,status="not_owned",last_error="SORARE_NOT_OWNED_ON_SOLANA")
    elif not oid:
        update_card(asset,status="da_vendere",last_error=err or "CREATE_FAILED")
    else:
        update_card(
            asset,status="selling",
            sale_offer_id=oid,
            sale_price_cents=used,
            sale_offer_end_date=None,
            last_error=None)


# ================= RINNOVO =================

def renew_expired_sales():
    for c in selling():
        asset=asset_id(c)
        active=find_active_public_offer(asset)

        if active:
            update_card(
                asset,status="selling",
                sale_offer_id=active.get("id"),
                sale_price_cents=active.get("price") or c.get("sale_price_cents"),
                sale_offer_end_date=active.get("endDate"),
                last_error=None)
            continue

        price=c.get("sale_price_cents")
        end=c.get("sale_offer_end_date")

        if price is None or not end or not expired(end):
            continue

        try:price=int(price)
        except:continue

        print(f"♻️ RINNOVO {asset} → {eur(price)}",flush=True)

        details=card_details(asset)
        if not details:continue

        # NESSUN floor.
        # NESSUNA modifica del prezzo.
        # ESATTAMENTE il prezzo precedente.
        oid,err,new_end=create_sale_once(details,price)

        if err=="NOT_OWNED":
            update_card(asset,status="not_owned",
                        last_error="SORARE_NOT_OWNED_ON_SOLANA")
            continue

        if oid=="ALREADY-LISTED":
            x=find_active_public_offer(asset)
            if x:
                update_card(
                    asset,status="selling",
                    sale_offer_id=x.get("id"),
                    sale_price_cents=x.get("price") or price,
                    sale_offer_end_date=x.get("endDate"),
                    last_error=None)
            continue

        if oid:
            update_card(
                asset,status="selling",
                sale_offer_id=oid,
                sale_price_cents=price,
                sale_offer_end_date=new_end,
                last_error=None)
            print(f"✅ RINNOVATA {asset} → {eur(price)} | 7 GIORNI",flush=True)
        else:
            update_card(
                asset,status="selling",
                sale_price_cents=price,
                last_error=err or "RENEWAL_FAILED")


# ================= WORKER =================

def check_account():
    d=gql("query{currentUser{slug nickname starkKey}}")
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:return False
    print(f"✅ Sorare: {u.get('nickname') or u.get('slug')}",flush=True)
    print("🔐 Stark key: "+("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),flush=True)
    print("🔑 Solana key: "+("PRESENTE" if SOLANA else "NON DISPONIBILE"),flush=True)
    return True

def worker():
    print(f"🤖 {VERSION}",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)
    print("💰 NUOVE VENDITE: floor massimo €0.70 / minimo €0.30",flush=True)
    print("♻️ RINNOVO: STESSO PREZZO + 7 GIORNI",flush=True)
    print("🛡️ RINNOVO: BOT + VENDITE MANUALI",flush=True)
    print("🚫 NESSUN CAMBIO PREZZO AL RINNOVO",flush=True)
    print("🔒 KULENOVIC / SEALED: MAI VENDUTE",flush=True)

    try:
        headers()
        os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)),exist_ok=True)
        if not os.path.exists(STATE_FILE):save(default_state())
    except Exception as e:
        print("❌ Config:",e,flush=True)
        return

    if not check_account():return

    while True:
        try:
            sync_active_offers()
            renew_expired_sales()

            for c in sellable():
                try:process(c)
                except Exception as e:
                    print(f"❌ AutoSell {asset_id(c)}:",e,flush=True)

            time.sleep(INTERVAL)
        except Exception as e:
            print("❌ Worker:",e,flush=True)
            time.sleep(INTERVAL)


# ================= FLASK =================

@app.get("/")
def home():
    c=cards()
    count=lambda s:sum(1 for x in c if norm(x.get("status"))==s)
    return jsonify({
        "status":"online",
        "bot":"autosell",
        "version":VERSION,
        "dry_run":DRY_RUN,
        "floor_max":"€0.70",
        "min_sell_price":"€0.30",
        "renewal":"SAME_PRICE",
        "renewal_period":"7_DAYS",
        "manual_listings":"RENEWED",
        "sealed":"NEVER_SELL",
        "kulenovic":"NEVER_SELL",
        "settlement":"EUR",
        "cards":len(c),
        "da_vendere":count("da_vendere")+count("ready"),
        "selling":count("selling"),
        "not_owned":count("not_owned"),
        "worker":worker_started
    })

@app.get("/health")
def health():
    return jsonify({
        "status":"ok",
        "bot":"autosell",
        "version":VERSION,
        "worker":worker_started,
        "dry_run":DRY_RUN
    })

@app.get("/cards")
def cards_endpoint():
    c=cards()
    return jsonify({"count":len(c),"cards":c})

def start_worker():
    global worker_started
    with worker_lock:
        if worker_started:return
        worker_started=True
        threading.Thread(target=worker,daemon=True).start()

if __name__=="__main__":
    start_worker()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),debug=False)
