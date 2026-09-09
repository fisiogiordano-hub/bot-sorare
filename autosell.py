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
MIN_PRICE,MAX_PRICE,MIN_LISTINGS=32,70,5
STATE_FILE=os.getenv("BOT_STATE_PATH","bot_state.json").strip()
VERSION="AUTOSell-12.0-SOLANA-EUR-FIX"
KULENOVIC_SLUG="sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

state_lock=threading.RLock()
worker_lock=threading.Lock()
worker_started=False

def norm(x): return str(x or "").strip().lower()
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
def new_id(): return str(uuid.uuid4())
def asset_id(c): return str(c.get("assetId") or c.get("asset_id") or "").strip()
def label(c): return c.get("name") or c.get("slug") or asset_id(c) or "Carta"
def eur(c): return "N/D" if c is None else f"€{c/100:.2f}"

def default_state():
    return {"processed_offers":[],"acquired_cards":[],"pending_autobuys":[],"updated_at":int(time.time())}

def save_document(data):
    tmp=f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,STATE_FILE)
        return True
    except Exception as e:
        print(f"❌ State: {e}",flush=True)
        try: os.remove(tmp)
        except: pass
        return False

def ensure_state():
    p=os.path.abspath(STATE_FILE)
    d=os.path.dirname(p)
    if d: os.makedirs(d,exist_ok=True)
    if not os.path.exists(p): save_document(default_state())

def load_document():
    ensure_state()
    try:
        with open(STATE_FILE,encoding="utf-8") as f:d=json.load(f)
        if not isinstance(d,dict): return default_state()
        d.setdefault("processed_offers",[])
        d.setdefault("acquired_cards",[])
        d.setdefault("pending_autobuys",[])
        d.setdefault("updated_at",int(time.time()))
        return d
    except Exception as e:
        print(f"❌ Lettura state: {e}",flush=True)
        return default_state()

def get_cards():
    with state_lock:
        c=load_document().get("acquired_cards",[])
        return c if isinstance(c,list) else []

def update_card(asset,status=None,offer_id=None,error=None):
    with state_lock:
        d=load_document()
        cards=d.get("acquired_cards",[])
        found=False
        for c in cards:
            if not isinstance(c,dict) or norm(asset_id(c))!=norm(asset): continue
            found=True
            if status is not None:c["status"]=status
            if offer_id:c["sale_offer_id"]=offer_id
            c["last_error"]=error
            if status=="SELLING":c["selling_at"]=now()
            break
        if not found:return False
        d["acquired_cards"]=cards
        d["updated_at"]=int(time.time())
        return save_document(d)

def sellable_cards():
    return [
        dict(c) for c in get_cards()
        if isinstance(c,dict)
        and norm(c.get("status")) in {"da_vendere","ready"}
        and asset_id(c)
    ]

def auth_headers():
    if not TOKEN: raise RuntimeError("SORARE_JWT_TOKEN mancante")
    h={
        "Authorization":TOKEN if TOKEN.lower().startswith("bearer ") else f"Bearer {TOKEN}",
        "Content-Type":"application/json",
        "Accept":"application/json",
        "User-Agent":f"Sorare-AutoSell/{VERSION}"
    }
    if AUD:h["JWT-AUD"]=AUD
    return h

def gql(query,variables=None):
    for attempt in range(3):
        try:
            r=requests.post(URL,headers=auth_headers(),
                json={"query":query,"variables":variables or {}},
                timeout=TIMEOUT)
            print(f"🌐 Sorare HTTP {r.status_code}",flush=True)
            if r.status_code==429:
                time.sleep(2+attempt*2);continue
            if r.status_code!=200:
                print(f"❌ Sorare: {r.text[:1000]}",flush=True)
                time.sleep(attempt+1);continue
            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:3000],flush=True)
            return d
        except Exception as e:
            print(f"❌ GraphQL: {e}",flush=True)
            time.sleep(attempt+1)
    return None

def check_account():
    d=gql("query{currentUser{slug nickname starkKey}}")
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:
        print("❌ Account Sorare non verificato",flush=True);return False
    print("✅ Sorare:",u.get("nickname") or u.get("slug"),flush=True)
    print("🔐 Stark key account:", "PRESENTE" if u.get("starkKey") else "NON DISPONIBILE",flush=True)
    print("🔑 Solana private key:", "PRESENTE" if SOLANA else "NON DISPONIBILE",flush=True)
    return True

def card_details(asset):
    d=gql("""
    query($ids:[String!]!){
      anyCards(assetIds:$ids){
        assetId slug name rarityTyped seasonYear
        anyPlayer{slug displayName activeClub{slug name}}
      }
    }""",{"ids":[asset]})
    c=(((d or {}).get("data") or {}).get("anyCards") or [])
    return c[0] if c else None

def usd_to_eur(x):
    try:
        r=requests.get("https://api.frankfurter.app/latest",
            params={"from":"USD","to":"EUR"},timeout=10)
        return round(int(x)*float(r.json()["rates"]["EUR"]))
    except:return None

def amount_to_eur(a):
    if not isinstance(a,dict):return None
    try:
        x=int(a.get("eurCents") or 0)
        if x>0:return x
    except:pass
    try:
        x=int(a.get("usdCents") or 0)
        return usd_to_eur(x) if x>0 else None
    except:return None

def get_floor(card):
    p=card.get("anyPlayer") or {}
    slug=norm(p.get("slug"))
    rarity=norm(card.get("rarityTyped"))
    try:season=int(card.get("seasonYear"))
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

    nodes=((((d or {}).get("data") or {}).get("tokens") or {})
           .get("liveSingleSaleOffers") or {}).get("nodes") or []
    prices=[]
    for o in nodes:
        for c in (o.get("senderSide") or {}).get("anyCards") or []:
            try:same=int(c.get("seasonYear"))==season
            except:continue
            if same and norm((c.get("anyPlayer") or {}).get("slug"))==slug and norm(c.get("rarityTyped"))==rarity:
                x=amount_to_eur((o.get("receiverSide") or {}).get("amounts"))
                if x is not None:prices.append(x)
                break
    print(f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",flush=True)
    return min(prices) if len(prices)>=MIN_LISTINGS else None

def is_kulenovic(c):
    return norm(c.get("slug"))==norm(KULENOVIC_SLUG) or norm(c.get("assetId"))==norm(KULENOVIC_ASSET)

def validate(c):
    if is_kulenovic(c):return False,"KULENOVIC"
    if norm(c.get("rarityTyped")).upper()!="LIMITED":return False,"RARITY"
    f=get_floor(c)
    if f is None:return False,"FLOOR_UNKNOWN"
    if f<MIN_PRICE:return False,"FLOOR_LOW"
    if f>MAX_PRICE:return False,"FLOOR_HIGH"
    return True,f

B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58dec(v):
    n=0
    for ch in str(v).strip():
        i=B58.find(ch)
        if i<0:raise ValueError(f"Base58 non valido: {ch}")
        n=n*58+i
    h="" if n==0 else format(n,"x")
    if len(h)%2:h="0"+h
    b=bytes.fromhex(h) if h else b""
    z=len(str(v))-len(str(v).lstrip("1"))
    return b"\0"*z+b.lstrip(b"\0")

def b58enc(data):
    data=bytes(data);n=int.from_bytes(data,"big");s=""
    while n:
        n,r=divmod(n,58);s=B58[r]+s
    return "1"*(len(data)-len(data.lstrip(b"\0")))+s

def sign_authorizations(auths):
    node=shutil.which("node") or shutil.which("nodejs")
    if not node:raise RuntimeError("Node.js non disponibile")

    js=r'''
const crypto=require("crypto");
const{signAuthorizationRequest}=require("@sorare/crypto");
const{createSignableMessage,getBase58Decoder,createKeyPairFromPrivateKeyBytes,createSignerFromKeyPair}=require("@solana/kit");
const fs=require("fs");
const input=JSON.parse(fs.readFileSync(0,"utf8"));
const A="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function b58d(v){
 let n=0n;
 for(const c of v){let i=A.indexOf(c);if(i<0)throw Error("Base58 non valido");n=n*58n+BigInt(i)}
 let h=n?n.toString(16):"";if(h.length%2)h="0"+h;
 let b=h?Buffer.from(h,"hex"):Buffer.alloc(0),z=0;
 for(const c of v){if(c!="1")break;z++}
 return new Uint8Array(Buffer.concat([Buffer.alloc(z),b]));
}
function b58e(x){
 let b=Buffer.from(x),n=BigInt("0x"+(b.toString("hex")||"0")),s="";
 while(n){let r=Number(n%58n);s=A[r]+s;n/=58n}
 let z=0;for(const x of b){if(x)break;z++}
 return"1".repeat(z)+s;
}
function key(v){
 let b;
 try{b=b58d(v)}catch{
  let h=v.startsWith("0x")?v.slice(2):v;
  if(!/^[0-9a-f]+$/i.test(h)||h.length%2)throw Error("Chiave non valida");
  b=new Uint8Array(Buffer.from(h,"hex"));
 }
 if(b.length===64)b=b.slice(0,32);
 if(b.length!==32)throw Error("Chiave Solana: attesi 32/64 byte");
 return b;
}
async function sol(a){
 const r=a.request;
 const kp=await createKeyPairFromPrivateKeyBytes(key(input.solanaPrivateKey));
 const signer=createSignerFromKeyPair(kp);
 console.error("🔑 Solana signer → "+signer.address);
 console.error("🎯 senderAddress → "+r.senderAddress);
 if(signer.address!==r.senderAddress)throw Error("La chiave Solana non corrisponde al senderAddress");
 const msg=[
  "TRANSFER",r.transferProxyProgramAddress,r.merkleTreeAddress,
  r.leafIndex.toString(),r.nonce,r.expirationTimestamp.toString(),
  r.receiverAddress,"0x",r.originator
 ].join(":");
 const hash=crypto.createHash("sha256").update(Buffer.from(msg)).digest();
 const sigs=await signer.signMessages([createSignableMessage(new Uint8Array(hash))]);
 return{
  fingerprint:a.fingerprint,
  solanaTokenTransferApproval:{
   signature:b58e(sigs[0][signer.address]),
   nonce:r.nonce,
   expirationTimestamp:r.expirationTimestamp
  }
 };
}
function stark(a){
 const r=a.request,s=signAuthorizationRequest(input.starkPrivateKey,r);
 const x={fingerprint:a.fingerprint};
 if(r.__typename==="StarkexTransferAuthorizationRequest")
  x.starkexTransferApproval={nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s};
 else if(r.__typename==="StarkexLimitOrderAuthorizationRequest")
  x.starkexLimitOrderApproval={nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature:s};
 else if(r.__typename==="MangopayWalletTransferAuthorizationRequest")
  x.mangopayWalletTransferApproval={nonce:r.nonce,signature:s};
 else throw Error("Authorization non supportata: "+r.__typename);
 return x;
}
(async()=>{
 const out=[];
 for(const a of input.authorizations){
  const t=(a.request||{}).__typename;
  console.error("🔐 Authorization → "+t);
  out.push(t==="SolanaTokenTransferAuthorizationRequest"?await sol(a):stark(a));
 }
 process.stdout.write(JSON.stringify(out));
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
'''

    p=subprocess.run([node,"-e",js],
        input=json.dumps({"starkPrivateKey":STARK,"solanaPrivateKey":SOLANA,"authorizations":auths}),
        text=True,capture_output=True,timeout=TIMEOUT)

    if p.stderr:print(p.stderr.strip(),flush=True)
    if p.returncode:raise RuntimeError(p.stderr.strip() or "Firma fallita")
    return json.loads(p.stdout)

PREPARE_QUERY="""
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
     assetId leafIndex merkleTreeAddress originator receiverAddress senderAddress
     expirationTimestamp nonce transferProxyProgramAddress
    }
   }
  }
  errors{message}
 }
}"""

def prepare_sale(asset,price):
    # Il tuo schema attuale rifiuta "type".
    inp={
        "sendAssetIds":[asset],
        "receiveAssetIds":[],
        "settlementCurrencies":"EUR",
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "clientMutationId":new_id()
    }
    d=gql(PREPARE_QUERY,{"input":inp})
    r=(((d or {}).get("data") or {}).get("prepareOffer"))
    if not r:return None
    e=r.get("errors") or []
    if e:
        print("❌ prepareOffer:",json.dumps(e,ensure_ascii=False),flush=True)
        return None
    a=r.get("authorizations") or []
    print(f"✅ Authorization ricevute: {len(a)}",flush=True)
    return a if a else None

def create_sale(card,price):
    asset=asset_id(card)
    if not asset:return None
    if DRY_RUN:
        print(f"🟡 DRY RUN → {label(card)} → {eur(price)}",flush=True)
        return "DRY-RUN"

    auths=prepare_sale(asset,price)
    if not auths:return None

    try:approvals=sign_authorizations(auths)
    except Exception as e:
        print(f"❌ Firma: {e}",flush=True);return None

    q="""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id startDate endDate}
        errors{message}
      }
    }"""

    inp={
        "approvals":approvals,
        "dealId":new_id(),
        "assetId":asset,
        "settlementCurrencies":"EUR",
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "clientMutationId":new_id()
    }

    d=gql(q,{"input":inp})
    r=(((d or {}).get("data") or {}).get("createSingleSaleOffer"))
    if not r:
        print("❌ createSingleSaleOffer: nessun risultato",flush=True);return None

    e=r.get("errors") or []
    if e:
        print("❌ createSingleSaleOffer:",json.dumps(e,ensure_ascii=False),flush=True)
        return None

    oid=(r.get("tokenOffer") or {}).get("id")
    if not oid:
        print("❌ Vendita non creata: offer ID assente",flush=True);return None

    print(f"✅ INSERZIONE CREATA → {oid}",flush=True)
    return oid

def process(card):
    asset=asset_id(card)
    print(f"\n💰 AUTOSELL CHECK → {asset}",flush=True)
    details=card_details(asset)
    if not details:
        update_card(asset,status="da_vendere",error="CARD_DETAILS");return

    print(f"🃏 {details.get('name') or details.get('slug')} {details.get('seasonYear')} • {norm(details.get('rarityTyped'))}",flush=True)
    ok,res=validate(details)

    if not ok:
        msg={
            "KULENOVIC":"KULENOVIC PROTETTO",
            "RARITY":"RARITÀ NON LIMITED",
            "FLOOR_UNKNOWN":"FLOOR NON DISPONIBILE",
            "FLOOR_LOW":"FLOOR SOTTO €0.32",
            "FLOOR_HIGH":"FLOOR SOPRA €0.70"
        }.get(res,res)
        print(f"🚫 ESCLUSA → {msg}",flush=True)
        update_card(asset,"BLOCKED" if res in {"KULENOVIC","RARITY"} else "da_vendere",error=res)
        return

    print(f"✅ CARTA VALIDA → floor {eur(res)}",flush=True)

    if not update_card(asset,status="SELLING",error=None):
        print("❌ Impossibile impostare SELLING",flush=True);return

    oid=create_sale(details,res)
    if not oid:
        update_card(asset,status="da_vendere",error="CREATE_SALE_FAILED")
        print("🔁 Carta rimessa in DA_VENDERE",flush=True);return

    update_card(asset,status="SELLING",offer_id=oid,error=None)
    print(f"🎉 AUTOSELL COMPLETATO → {label(details)} | {eur(res)} | {oid}",flush=True)

def recovery():
    s=[c for c in get_cards() if norm(c.get("status"))=="selling"]
    print(f"🔄 Recovery: {len(s)} carte SELLING" if s else "🔄 Recovery: nessuna carta SELLING.",flush=True)

def worker():
    global worker_started
    print("🤖 AUTOSELL AVVIATO",flush=True)
    print(f"📦 VERSIONE: {VERSION}",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)
    print("💰 RANGE: €0.32 - €0.70",flush=True)
    print(f"📊 LISTING MINIME: {MIN_LISTINGS}",flush=True)
    print("🎂 ETÀ: NON UTILIZZATA",flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO",flush=True)
    print("🛡️ COVERAGE: DISABILITATA",flush=True)
    print("🛡️ SOURCE: AUTOBUY / SWAP",flush=True)
    print(f"💾 STORAGE: {STATE_FILE}",flush=True)

    try:
        auth_headers();ensure_state()
    except Exception as e:
        print(f"❌ Configurazione: {e}",flush=True);return
    if not check_account():return
    recovery()

    while True:
        try:
            cards=sellable_cards()
            print(f"🗄️ Carte DA VENDERE: {len(cards)}",flush=True)
            for c in cards:
                try:process(c)
                except Exception as e:
                    a=asset_id(c)
                    print(f"❌ AutoSell {a}: {e}",flush=True)
                    if a:update_card(a,"da_vendere",error=str(e))
            time.sleep(INTERVAL)
        except Exception as e:
            print(f"❌ Worker: {e}",flush=True)
            time.sleep(INTERVAL)

@app.get("/")
def home():
    c=get_cards()
    return jsonify({
        "status":"online","bot":"autosell","version":VERSION,
        "dry_run":DRY_RUN,"range":"€0.32-€0.70",
        "min_live_listings":MIN_LISTINGS,"rarity":"LIMITED",
        "age":"NOT_USED","kulenovic":"NEVER_SELL",
        "coverage":"DISABLED","storage":STATE_FILE,
        "cards":len(c),
        "da_vendere":sum(norm(x.get("status")) in {"da_vendere","ready"} for x in c),
        "selling":sum(norm(x.get("status"))=="selling" for x in c),
        "worker":worker_started
    })

@app.get("/health")
def health():
    return jsonify({"status":"ok","bot":"autosell","version":VERSION,"worker":worker_started,"dry_run":DRY_RUN})

@app.get("/cards")
def cards_endpoint():
    c=get_cards()
    return jsonify({"count":len(c),"cards":c})

def start_worker():
    global worker_started
    with worker_lock:
        if worker_started:return
        worker_started=True
        threading.Thread(target=worker,daemon=True,name="autosell-worker").start()
        print("✅ Thread AutoSell avviato.",flush=True)

if __name__=="__main__":
    start_worker()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),debug=False)
