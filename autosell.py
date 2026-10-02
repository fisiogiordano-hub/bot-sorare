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

MAX_PRICE=70
MIN_PRICE=30
MIN_LISTINGS=5
TECH_START=.0002
TECH_STEP=.0001
STATE_FILE=os.getenv("BOT_STATE_PATH","bot_state.json")
VERSION="AUTOSell-17.0-EUR-7D-SAME-PRICE"

KUL_SLUG="sandro-kulenovic-2025-limited-385"
KUL_ASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

lock=threading.RLock()
worker_started=False
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def norm(x): return str(x or "").strip().lower()
def aid(c): return str(c.get("assetId") or c.get("asset_id") or "").strip()
def eur(x):
    try:return f"€{int(x)/100:.2f}"
    except:return "N/D"
def uid(): return str(uuid.uuid4())

def ts(x):
    try:
        n=float(x)
        return n/1000 if n>10_000_000_000 else n
    except: pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(x).replace("Z","+00:00")).timestamp()
    except:return None

def expired(x):
    t=ts(x)
    return t is not None and time.time()>=t

def default_state():
    return {"processed_offers":[],"acquired_cards":[],"pending_autobuys":[],"updated_at":int(time.time())}

def load():
    try:
        with open(STATE_FILE,encoding="utf8") as f:d=json.load(f)
        return d if isinstance(d,dict) else default_state()
    except:return default_state()

def save(d):
    tmp=STATE_FILE+"."+uuid.uuid4().hex+".tmp"
    try:
        with open(tmp,"w",encoding="utf8") as f:
            json.dump(d,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,STATE_FILE);return True
    except Exception as e:
        print("❌ State:",e,flush=True)
        try:os.remove(tmp)
        except:pass
        return False

def cards():
    with lock:return list(load().get("acquired_cards",[]))

def update_card(asset,**fields):
    with lock:
        d=load()
        for c in d["acquired_cards"]:
            if norm(aid(c))==norm(asset):
                c.update({k:v for k,v in fields.items() if v is not None})
                d["updated_at"]=int(time.time())
                return save(d)
    return False

def upsert(c):
    a=aid(c)
    if not a:return
    with lock:
        d=load()
        for i,x in enumerate(d["acquired_cards"]):
            if norm(aid(x))==norm(a):
                d["acquired_cards"][i]={**x,**c}
                d["updated_at"]=int(time.time())
                save(d);return
        d["acquired_cards"].append(c)
        d["updated_at"]=int(time.time())
        save(d)

def sellable():
    return [c for c in cards()
            if norm(c.get("status")) in {"da_vendere","ready"} and aid(c)]

def selling():
    return [c for c in cards()
            if norm(c.get("status"))=="selling" and aid(c)]

def headers():
    if not TOKEN:raise RuntimeError("SORARE_JWT_TOKEN mancante")
    h={"Authorization":TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN,
       "Content-Type":"application/json","Accept":"application/json"}
    if AUD:h["JWT-AUD"]=AUD
    return h

def gql(q,v=None):
    for n in range(3):
        try:
            r=requests.post(URL,headers=headers(),json={"query":q,"variables":v or {}},timeout=TIMEOUT)
            print(f"🌐 Sorare HTTP {r.status_code}",flush=True)
            if r.status_code==429:
                time.sleep(2+n*2);continue
            if r.status_code!=200:
                print("❌",r.text[:1000],flush=True);continue
            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:2000],flush=True)
            return d
        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(n+1)
    return None

def amount(a):
    if not isinstance(a,dict):return None
    try:
        x=int(a.get("eurCents") or 0)
        if x:return x
    except:pass
    return None

CARD="""
anyCards(assetIds:$ids){
 assetId slug name rarityTyped seasonYear
 anyPlayer{slug displayName activeClub{slug name}}
}
"""

def details(asset):
    d=gql("query($ids:[String!]!){"+CARD+"}",{"ids":[asset]})
    a=((d or {}).get("data") or {}).get("anyCards") or []
    return a[0] if a else None

def active_offers():
    q="""
    query($first:Int){
      tokens{liveSingleSaleOffers(first:$first){
        nodes{
          id startDate endDate
          senderSide{anyCards{
            assetId slug name rarityTyped seasonYear
            anyPlayer{slug displayName}
          }}
          receiverSide{amounts{eurCents usdCents}}
        }
      }}
    }"""
    d=gql(q,{"first":100})
    return (((((d or {}).get("data") or {}).get("tokens") or {})
             .get("liveSingleSaleOffers") or {}).get("nodes") or []))

def find_offer(asset):
    d=gql("""
    query($id:String!){
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
        if norm(c.get("assetId"))==norm(asset) and o.get("id"):
            return {"id":o["id"],"endDate":o.get("endDate"),
                    "price":amount(o.get("receiverSide",{}).get("amounts"))}
    return None

def sync_offers():
    for o in active_offers():
        p=amount((o.get("receiverSide") or {}).get("amounts"))
        for c in ((o.get("senderSide") or {}).get("anyCards") or []):
            a=aid(c)
            if not a or not o.get("id"):continue
            old=next((x for x in cards() if norm(aid(x))==norm(a)),{})
            upsert({**old,**c,"assetId":a,"status":"selling",
                    "sale_offer_id":o["id"],
                    "sale_price_cents":p if p is not None else old.get("sale_price_cents"),
                    "sale_offer_end_date":o.get("endDate") or old.get("sale_offer_end_date"),
                    "source":old.get("source") or "MANUAL",
                    "last_error":None})
    print("🔄 Sync offerte attive completato",flush=True)

def floor(c):
    p=c.get("anyPlayer") or {}
    slug=norm(p.get("slug"));rarity=norm(c.get("rarityTyped"))
    try:season=int(c.get("seasonYear"))
    except:return None
    if not slug or not rarity:return None

    d=gql("""
    query($slug:String,$first:Int){
      tokens{liveSingleSaleOffers(playerSlug:$slug,first:$first){
        nodes{
          senderSide{anyCards{assetId rarityTyped seasonYear anyPlayer{slug}}}
          receiverSide{amounts{eurCents usdCents}}
        }
      }}
    }""",{"slug":slug,"first":50})

    prices=[]
    for o in (((((d or {}).get("data") or {}).get("tokens") or {})
               .get("liveSingleSaleOffers") or {}).get("nodes") or []):
        for x in ((o.get("senderSide") or {}).get("anyCards") or []):
            try:same=int(x.get("seasonYear"))==season
            except:same=False
            if same and norm(x.get("rarityTyped"))==rarity and norm((x.get("anyPlayer") or {}).get("slug"))==slug:
                p=amount((o.get("receiverSide") or {}).get("amounts"))
                if p is not None:prices.append(p)
                break
    print(f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",flush=True)
    return min(prices) if len(prices)>=MIN_LISTINGS else None

def validate(c):
    if norm(c.get("slug"))==norm(KUL_SLUG) or norm(c.get("assetId"))==norm(KUL_ASSET):
        return False,"KULENOVIC"
    if norm(c.get("rarityTyped"))=="sealed" or "sealed" in norm(c.get("name")) or "sealed" in norm(c.get("slug")):
        return False,"SEALED"
    if norm(c.get("rarityTyped"))!="limited":return False,"RARITY"
    f=floor(c)
    if f is None:return False,"FLOOR_UNKNOWN"
    if f>MAX_PRICE:return False,"FLOOR_HIGH"
    return True,max(f,MIN_PRICE)

# ---------------- SIGNING ----------------

def b58(v):
    n=0
    for c in str(v).strip():
        i=B58.find(c)
        if i<0:raise ValueError("Base58")
        n=n*58+i
    raw=b"" if n==0 else n.to_bytes(max(1,(n.bit_length()+7)//8),"big")
    return b"\0"*(len(str(v))-len(str(v).lstrip("1")))+raw

def solkey():
    if not SOLANA:raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY mancante")
    try:
        b=b58(SOLANA)
        if len(b) in (32,64):return b
    except:pass
    h=SOLANA[2:] if SOLANA.startswith("0x") else SOLANA
    b=bytes.fromhex(h)
    if len(b) in (32,64):return b
    raise RuntimeError("Solana key non valida")

def sign(auths):
    node=shutil.which("node") or shutil.which("nodejs")
    if not node:raise RuntimeError("Node.js non disponibile")

    if any((a.get("request") or {}).get("__typename")=="SolanaTokenTransferAuthorizationRequest" for a in auths):
        solkey()

    js=r'''
const crypto=require("crypto"),{signAuthorizationRequest}=require("@sorare/crypto");
const{createSignableMessage,createKeyPairFromPrivateKeyBytes,createSignerFromKeyPair}=require("@solana/kit");
const fs=require("fs"),x=JSON.parse(fs.readFileSync(0,"utf8"));
const A="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function d(v){let n=0n;for(const c of String(v).trim()){let i=A.indexOf(c);if(i<0)throw Error("Base58");n=n*58n+BigInt(i)}let h=n?n.toString(16):"";if(h.length%2)h="0"+h;let b=h?Buffer.from(h,"hex"):Buffer.alloc(0),z=0;for(const c of String(v).trim()){if(c!="1")break;z++}return new Uint8Array(Buffer.concat([Buffer.alloc(z),b]))}
function e(x){let b=Buffer.from(x),n=0n;for(const q of b)n=n*256n+BigInt(q);let s="";while(n){s=A[Number(n%58n)]+s;n/=58n}let z=0;for(const q of b){if(q)break;z++}return"1".repeat(z)+s}
async function sol(a){let k=d(x.sol);if(k.length===64)k=k.slice(0,32);let kp=await createKeyPairFromPrivateKeyBytes(k),sg=createSignerFromKeyPair(kp),r=a.request;if(sg.address!==r.senderAddress)throw Error("Solana key/sender mismatch");let m=["TRANSFER",r.transferProxyProgramAddress,r.merkleTreeAddress,r.leafIndex.toString(),r.nonce,r.expirationTimestamp.toString(),r.receiverAddress,"0x",r.originator].join(":");let h=crypto.createHash("sha256").update(Buffer.from(m)).digest(),z=await sg.signMessages([createSignableMessage(new Uint8Array(h))]);return{fingerprint:a.fingerprint,solanaTokenTransferApproval:{signature:e(z[0][sg.address]),nonce:r.nonce,expirationTimestamp:r.expirationTimestamp}}}
function stark(a){let r=a.request,s=signAuthorizationRequest(x.stark,r),b={fingerprint:a.fingerprint};if(r.__typename==="StarkexTransferAuthorizationRequest")return{...b,starkexTransferApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s}};if(r.__typename==="StarkexLimitOrderAuthorizationRequest")return{...b,starkexLimitOrderApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s}};if(r.__typename==="MangopayWalletTransferAuthorizationRequest")return{...b,mangopayWalletTransferApproval:{nonce:r.nonce,signature:s}};throw Error("Authorization "+r.__typename)}
(async()=>{let o=[];for(let a of x.a)o.push((a.request||{}).__typename==="SolanaTokenTransferAuthorizationRequest"?await sol(a):stark(a));process.stdout.write(JSON.stringify(o))})().catch(e=>{console.error(e.stack||e);process.exit(1)})
'''

    p=subprocess.run([node,"-e",js],
        input=json.dumps({"stark":STARK,"sol":SOLANA,"a":auths}),
        text=True,capture_output=True,timeout=TIMEOUT)
    if p.stderr:print(p.stderr.strip(),flush=True)
    if p.returncode:raise RuntimeError(p.stderr.strip() or "Firma fallita")
    return json.loads(p.stdout)

# ---------------- SALE ----------------

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

def prepare(asset,price):
    # CORREZIONE API:
    # il tuo schema live NON accetta "type" dentro prepareOfferInput.
    d=gql(PREPARE,{"input":{
        "sendAssetIds":[asset],
        "receiveAssetIds":[],
        "settlementCurrencies":["EUR"],
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "clientMutationId":uid()
    }})
    r=((d or {}).get("data") or {}).get("prepareOffer")
    if not r or r.get("errors"):return None
    return r.get("authorizations") or None

def technical(s):
    s=norm(s)
    return any(x in s for x in
               ["price must be greater than","price must be at least",
                "minimum price","min price"])

def create_once(c,price):
    a=aid(c)
    if DRY_RUN:return "DRY-RUN",None,None

    auth=prepare(a,price)
    if not auth:return None,"PREPARE_FAILED",None

    try:approvals=sign(auth)
    except Exception as e:return None,str(e),None

    q="""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id startDate endDate}
        errors{message}
      }
    }"""

    d=gql(q,{"input":{
        "approvals":approvals,
        "dealId":uid(),
        "assetId":a,
        "settlementCurrencies":"EUR",
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "clientMutationId":uid()
    }})

    r=((d or {}).get("data") or {}).get("createSingleSaleOffer")
    if not r:return None,"CREATE_NO_RESULT",None

    errors=r.get("errors") or []
    if errors:
        text=" ".join(str(x.get("message","")) for x in errors)
        if "not owned by" in norm(text) and "solana" in norm(text):
            return None,"NOT_OWNED",None
        if "active public offer already exists" in norm(text):
            x=find_offer(a)
            return (x or {}).get("id"),"ALREADY-LISTED",(x or {}).get("endDate")
        return None,text,None

    o=r.get("tokenOffer") or {}
    return o.get("id"),None,o.get("endDate")

def eth_rate():
    try:
        r=requests.get("https://api.coingecko.com/api/v3/simple/price",
                        params={"ids":"ethereum","vs_currencies":"eur"},timeout=10)
        return float(r.json()["ethereum"]["eur"])
    except:return None

def eth_cents(e,r):
    try:return max(1,round(float(e)*float(r)*100))
    except:return None

def create(c,price,technical_retry=True):
    oid,err,end=create_once(c,price)
    if oid:return oid,price,end
    if err=="NOT_OWNED":return None,None,"NOT_OWNED"
    if not technical_retry or not technical(err or ""):
        return None,None,err

    rate=eth_rate()
    if not rate:return None,None,"ETH_EUR_UNAVAILABLE"

    e=TECH_START
    for _ in range(1000):
        p=max(eth_cents(e,rate),MIN_PRICE)
        oid,err,end=create_once(c,p)
        if oid:return oid,p,end
        if err=="NOT_OWNED":return None,None,"NOT_OWNED"
        if not technical(err or ""):return None,None,err
        e=round(e+TECH_STEP,4)

    return None,None,"TECHNICAL_RETRY_LIMIT"

# ---------------- NUOVE CARTE ----------------

def process(c):
    a=aid(c)
    if not a or norm(c.get("status"))=="not_owned":return

    x=find_offer(a)
    if x:
        update_card(a,status="selling",sale_offer_id=x.get("id"),
                    sale_price_cents=x.get("price"),
                    sale_offer_end_date=x.get("endDate"),last_error=None)
        return

    d=details(a)
    if not d:
        update_card(a,status="da_vendere",last_error="CARD_DETAILS");return

    ok,r=validate(d)
    if not ok:
        update_card(a,status="blocked" if r in {"KULENOVIC","SEALED","RARITY"} else "da_vendere",
                    last_error=r)
        return

    price=int(r)
    oid,used,err=create(d,price,True)

    if err=="NOT_OWNED":
        update_card(a,status="not_owned",last_error="SORARE_NOT_OWNED_ON_SOLANA")
    elif not oid:
        update_card(a,status="da_vendere",last_error=err or "CREATE_SALE_FAILED")
    else:
        update_card(a,status="selling",sale_offer_id=oid,
                    sale_price_cents=used or price,
                    sale_offer_end_date=None,last_error=None)

# ---------------- RINNOVO ----------------

def renew():
    for c in selling():
        a=aid(c)
        active=find_offer(a)

        if active:
            update_card(a,status="selling",
                        sale_offer_id=active.get("id"),
                        sale_price_cents=active.get("price") or c.get("sale_price_cents"),
                        sale_offer_end_date=active.get("endDate") or c.get("sale_offer_end_date"),
                        last_error=None)
            continue

        price=c.get("sale_price_cents")
        end=c.get("sale_offer_end_date")

        if price is None or not end or not expired(end):
            continue

        try:price=int(price)
        except:continue

        print(f"♻️ RINNOVO {a} → {eur(price)}",flush=True)

        d=details(a)
        if not d:continue

        # NESSUN FLOOR.
        # NESSUNA MODIFICA DEL PREZZO.
        oid,used,err=create(d,price,False)

        if err=="NOT_OWNED":
            update_card(a,status="not_owned",
                        last_error="SORARE_NOT_OWNED_ON_SOLANA")
        elif oid=="ALREADY-LISTED":
            x=find_offer(a)
            if x:
                update_card(a,status="selling",
                            sale_offer_id=x.get("id"),
                            sale_price_cents=x.get("price") or price,
                            sale_offer_end_date=x.get("endDate"),
                            last_error=None)
        elif oid:
            update_card(a,status="selling",
                        sale_offer_id=oid,
                        sale_price_cents=price,
                        sale_offer_end_date=None,
                        last_error=None)
            print(f"♻️ RINNOVATA → {a} | {eur(price)}",flush=True)
        else:
            update_card(a,status="selling",
                        sale_price_cents=price,
                        last_error=err or "RENEWAL_FAILED")

# ---------------- ACCOUNT ----------------

def account():
    d=gql("query{currentUser{slug nickname starkKey}}")
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:return False
    print(f"✅ Sorare: {u.get('nickname') or u.get('slug')}",flush=True)
    print("🔐 Stark key: "+("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),flush=True)
    print("🔑 Solana key: "+("PRESENTE" if SOLANA else "NON DISPONIBILE"),flush=True)
    return True

def worker():
    print(f"🤖 AUTOSELL {VERSION}",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)
    print("💰 NUOVE VENDITE: floor massimo €0.70 / minimo €0.30",flush=True)
    print("♻️ RENEWAL: STESSO PREZZO + NUOVO PERIODO SORARE",flush=True)
    print("🛡️ RINNOVO: BOT + VENDITE MANUALI",flush=True)
    print("🚫 RINNOVO: NESSUN CAMBIO PREZZO",flush=True)
    print("🔒 KULENOVIC / SEALED: MAI VENDUTE",flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)),exist_ok=True)
    if not os.path.exists(STATE_FILE):save(default_state())

    try:headers()
    except Exception as e:
        print("❌ Configurazione:",e,flush=True);return

    if not account():return

    while True:
        try:
            sync_offers()
            renew()

            for c in sellable():
                try:process(c)
                except Exception as e:
                    print("❌ AutoSell",aid(c),e,flush=True)
                    if aid(c):update_card(aid(c),status="da_vendere",last_error=str(e))

            time.sleep(INTERVAL)
        except Exception as e:
            print("❌ Worker:",e,flush=True)
            time.sleep(INTERVAL)

# ---------------- FLASK ----------------

@app.get("/")
def home():
    c=cards()
    count=lambda s:sum(norm(x.get("status"))==s for x in c)
    return jsonify({
        "status":"online","bot":"autosell","version":VERSION,
        "dry_run":DRY_RUN,"floor_max":"€0.70","min_sell_price":"€0.30",
        "min_live_listings":MIN_LISTINGS,"rarity":"LIMITED",
        "sealed":"NEVER_SELL","kulenovic":"NEVER_SELL",
        "renewal":"SAME_PRICE","renewal_period":"7_DAYS",
        "manual_listings":"RENEWED","settlement":"EUR",
        "storage":STATE_FILE,"cards":len(c),
        "da_vendere":count("da_vendere")+count("ready"),
        "selling":count("selling"),"not_owned":count("not_owned"),
        "worker":worker_started
    })

@app.get("/health")
def health():
    return jsonify({"status":"ok","bot":"autosell",
                    "version":VERSION,"worker":worker_started,
                    "dry_run":DRY_RUN})

@app.get("/cards")
def cards_api():
    c=cards()
    return jsonify({"count":len(c),"cards":c})

def start():
    global worker_started
    if worker_started:return
    worker_started=True
    threading.Thread(target=worker,daemon=True).start()

if __name__=="__main__":
    start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),debug=False)
