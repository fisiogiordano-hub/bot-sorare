import os,time,uuid,json,subprocess,threading,requests
from flask import Flask,jsonify

app=Flask(__name__)
URL="https://api.sorare.com/graphql"
TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
PRIVATE_KEY=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()

DRY_RUN=os.getenv("DRY_RUN","true").lower()=="true"

MIN_PRICE=32
MAX_PRICE=70
MIN_LIVE_LISTINGS=5
LISTING_DURATION=7*24*60*60
INTERVAL=15
TIMEOUT=30
BOT_VERSION="28.0-AUTOSELL-SOLANA-LIMITED-LINEUP-VAULT-SAFE"

KSLUG="sandro-kulenovic-2025-limited-385"
KASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
KID=os.getenv("KULENOVIC_ID","").strip()

worker_started=False
worker_lock=threading.Lock()
usd_rate=None
usd_time=0


def norm(v):
    return str(v or "").strip().lower()


def label(c):
    x=[c.get("name") or c.get("slug") or "Carta"]
    for k in ("seasonYear","rarityTyped"):
        if c.get(k): x.append(str(c[k]))
    if c.get("serialNumber"): x.append("#"+str(c["serialNumber"]))
    return " • ".join(x)


def eur(c):
    return "N/D" if c is None else f"€{c/100:.2f}"


def headers():
    if not TOKEN: raise RuntimeError("SORARE_JWT_TOKEN non configurato")
    t=TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN
    h={"Authorization":t,"Content-Type":"application/json","Accept":"application/json",
       "User-Agent":f"Sorare-AutoSell/{BOT_VERSION}"}
    if AUD: h["JWT-AUD"]=AUD
    return h


def graphql(q,v=None):
    for n in range(3):
        try:
            r=requests.post(URL,json={"query":q,"variables":v or {}},
                            headers=headers(),timeout=TIMEOUT)
            print(f"🌐 HTTP {r.status_code}",flush=True)
            if r.status_code==429:
                try:w=min(int(r.headers.get("Retry-After",n+2)),15)
                except:w=n+2
                time.sleep(w);continue
            if r.status_code!=200:
                print("❌ HTTP:",r.text[:500],flush=True)
                time.sleep(n+1);continue
            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:1500],flush=True)
            return d
        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(n+1)
    return None


def check_account():
    d=graphql("""
    query{currentUser{slug nickname starkKey}}
    """)
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:
        print("❌ Account Sorare non verificato",flush=True)
        return False
    print("✅ Account:",u.get("nickname") or u.get("slug"),flush=True)
    return True


def get_gallery():
    cards=[];after=None;page=0
    while True:
        page+=1
        d=graphql("""
        query($first:Int,$after:String){
          currentUser{cards(first:$first after:$after ownedByMe:true sport:FOOTBALL){
            nodes{assetId slug name rarityTyped seasonYear serialNumber sealed
              anyPlayer{slug displayName} liveSingleSaleOffer{id status}}
            pageInfo{hasNextPage endCursor}
          }}
        }""",{"first":50,"after":after})
        if not d or d.get("errors"): return None
        c=(((d.get("data") or {}).get("currentUser") or {}).get("cards") or {})
        nodes=c.get("nodes") or []
        print(f"📄 Gallery {page}: {len(nodes)}",flush=True)
        cards += [x for x in nodes if not x.get("sealed") and
                  norm(x.get("rarityTyped"))=="limited"]
        p=c.get("pageInfo") or {}
        if not p.get("hasNextPage"): break
        after=p.get("endCursor")
        if not after: break
    print(f"🏆 LIMITED: {len(cards)}",flush=True)
    return cards


def get_lineup():
    d=graphql("""
    query{currentUser{blockchainCardsInLineups(sport:FOOTBALL)}}
    """)
    if not d or d.get("errors"): return None
    v=(((d.get("data") or {}).get("currentUser") or {})
       .get("blockchainCardsInLineups"))
    if not isinstance(v,list): return None
    return {norm(x) for x in v if x}


def ids(c):
    return {norm(c.get("assetId")),norm(c.get("slug"))}-{""}


def kulenovic(c):
    wanted={norm(KSLUG),norm(KASSET)}
    if KID:wanted.add(norm(KID))
    return bool(ids(c)&wanted)


def lineup(c,lids):
    if lids is None:return None
    i=ids(c)
    return bool(i&lids) if i else None


def usd_eur():
    global usd_rate,usd_time
    now=time.time()
    if usd_rate and now-usd_time<300:return usd_rate
    try:
        r=requests.get("https://api.frankfurter.app/latest",
                       params={"from":"USD","to":"EUR"},timeout=10)
        rate=float((r.json().get("rates") or {}).get("EUR"))
        if rate>0:
            usd_rate,usd_time=rate,now
            return rate
    except Exception as e:
        print("❌ USD/EUR:",e,flush=True)
    return None


def price(a):
    if not isinstance(a,dict):return None
    try:
        x=int(a.get("eurCents"))
        if x>0:return x
    except:pass
    try:x=float(a.get("usdCents"))
    except:x=0
    r=usd_eur()
    return int(round(x*r)) if x>0 and r else None


def live_floor(c):
    p=c.get("anyPlayer") or {}
    slug=norm(p.get("slug"))
    rarity=norm(c.get("rarityTyped"))
    try:season=int(c.get("seasonYear"))
    except:return None
    if not slug or rarity!="limited":return None

    d=graphql("""
    query($playerSlug:String,$first:Int){
      tokens{liveSingleSaleOffers(playerSlug:$playerSlug,first:$first){
        nodes{
          senderSide{anyCards{assetId rarityTyped seasonYear anyPlayer{slug}}}
          receiverSide{amounts{eurCents usdCents referenceCurrency wei}}
        }
      }}
    }""",{"playerSlug":slug,"first":50})

    if not d or d.get("errors"):return None

    offers=((((d.get("data") or {}).get("tokens") or {})
             .get("liveSingleSaleOffers") or {}).get("nodes") or [])
    prices=[]

    for o in offers:
        for mc in ((o.get("senderSide") or {}).get("anyCards") or []):
            mp=mc.get("anyPlayer") or {}
            try:ms=int(mc.get("seasonYear"))
            except:continue
            if norm(mp.get("slug"))==slug and norm(mc.get("rarityTyped"))==rarity and ms==season:
                x=price((o.get("receiverSide") or {}).get("amounts") or {})
                if x is not None:prices.append(x)
                break

    if len(prices)<MIN_LIVE_LISTINGS:
        return None
    return min(prices)


def validate(c,lids):
    if c.get("sealed"):return False,("VAULT",None)
    if kulenovic(c):return False,("KULENOVIC",None)
    if norm(c.get("rarityTyped"))!="limited":return False,("RARITY",None)
    il=lineup(c,lids)
    if il is None:return False,("LINEUP_UNKNOWN",None)
    if il:return False,("LINEUP",None)
    f=live_floor(c)
    if f is None:return False,("PRICE_UNKNOWN",None)
    if f<MIN_PRICE:return False,("PRICE_LOW",f)
    if f>MAX_PRICE:return False,("PRICE_HIGH",f)
    return True,("OK",f)


def sign_solana(auth):
    node=which("node")
    if not node:raise RuntimeError("Node.js non disponibile")
    if not PRIVATE_KEY:raise RuntimeError("SORARE_STARK_PRIVATE_KEY non configurata")

    script=r'''
const crypto=require("crypto");
const{createSignableMessage,getBase58Decoder,
createKeyPairFromPrivateKeyBytes,createSignerFromKeyPair}=require("@solana/kit");
const{HDKey}=require("micro-key-producer/slip10.js");
const PATH="m/44'/501'/0'/0'";

async function main(){
 const input=JSON.parse(require("fs").readFileSync(0,"utf8"));
 const a=input.authorization,r=a.request;
 const seed=Buffer.from(input.privateKey.replace(/^0x/,""),"hex");
 const{privateKey:pk}=HDKey.fromMasterSeed(seed).derive(PATH);
 const kp=await createKeyPairFromPrivateKeyBytes(pk);
 const signer=createSignerFromKeyPair(kp);

 if(signer.address!==r.senderAddress)
   throw Error("Solana senderAddress mismatch");

 const msg=[
 "TRANSFER",r.transferProxyProgramAddress,r.merkleTreeAddress,
 r.leafIndex.toString(),r.nonce,r.expirationTimestamp.toString(),
 r.receiverAddress,"0x",r.originator
 ].join(":");

 const hash=await crypto.webcrypto.subtle.digest(
 "SHA-256",new TextEncoder().encode(msg));
 const sm=createSignableMessage(new Uint8Array(hash));
 const[sigs]=await signer.signMessages([sm]);
 const sig=getBase58Decoder().decode(sigs[signer.address]);

 process.stdout.write(JSON.stringify({
   fingerprint:a.fingerprint,
   solanaTokenTransferApproval:{
     signature:sig,nonce:r.nonce,
     expirationTimestamp:r.expirationTimestamp
   }
 }));
}
main().catch(e=>{console.error(e.stack||e);process.exit(1)});
'''

    p=subprocess.run([node,"-e",script],
        input=json.dumps({"privateKey":PRIVATE_KEY,"authorization":auth}),
        text=True,capture_output=True,timeout=TIMEOUT)

    if p.stderr:print(p.stderr.strip(),flush=True)
    if p.returncode:raise RuntimeError(p.stderr.strip() or "Firma Solana fallita")
    try:return json.loads(p.stdout)
    except:raise RuntimeError("Output firma Solana non valido")


def sign_auths(auths):
    out=[]
    for a in auths:
        t=((a.get("request") or {}).get("__typename"))
        if t!="SolanaTokenTransferAuthorizationRequest":
            raise RuntimeError("Authorization non supportata: "+str(t))
        out.append(sign_solana(a))
    return out


def prepare(c,p):
    aid=str(c.get("assetId") or "").strip()
    if not aid:return None

    d=graphql("""
    mutation($input:prepareOfferInput!){
      prepareOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename
            ...on StarkexTransferAuthorizationRequest{
              amount condition expirationTimestamp nonce receiverPublicKey
              receiverVaultId senderVaultId token
              feeInfoUser{feeLimit sourceVaultId tokenId}
            }
            ...on StarkexLimitOrderAuthorizationRequest{
              vaultIdSell vaultIdBuy amountSell amountBuy tokenSell tokenBuy
              nonce expirationTimestamp
              feeInfo{feeLimit tokenId sourceVaultId}
            }
            ...on MangopayWalletTransferAuthorizationRequest{
              nonce amount currency operationHash mangopayWalletId
            }
            ...on SolanaTokenTransferAuthorizationRequest{
              assetId leafIndex merkleTreeAddress originator receiverAddress
              senderAddress expirationTimestamp nonce transferProxyProgramAddress
            }
          }
        }
        errors{message}
      }
    }""",{"input":{
        "type":"SINGLE_SALE_OFFER",
        "receiveAssetIds":[],
        "sendAssetIds":[aid],
        "receiveAmount":{"amount":str(p),"currency":"EUR"},
        "settlementCurrencies":["EUR"],
        "clientMutationId":str(uuid.uuid4())
    }})

    r=(((d or {}).get("data") or {}).get("prepareOffer"))
    if not r:return None
    e=r.get("errors") or []
    if e:
        print("❌ prepareOffer:",json.dumps(e),flush=True)
        return None
    a=r.get("authorizations") or []
    return a or None


def create_sale(c,p,approvals):
    aid=str(c.get("assetId") or "").strip()
    d=graphql("""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id blockchainId status}
        errors{message}
      }
    }""",{"input":{
        "approvals":approvals,
        "assetId":aid,
        "dealId":str(uuid.uuid4()),
        "duration":LISTING_DURATION,
        "receiveAmount":{"amount":str(p),"currency":"EUR"},
        "settlementCurrencies":["EUR"],
        "clientMutationId":str(uuid.uuid4())
    }})

    r=(((d or {}).get("data") or {}).get("createSingleSaleOffer"))
    if not r:return False
    if r.get("errors"):
        print("❌ createSale:",json.dumps(r["errors"]),flush=True)
        return False
    o=r.get("tokenOffer") or {}
    if not o.get("id"):return False
    print(f"✅ LISTING {o['id']} {eur(p)}",flush=True)
    return True


def sell(c,p):
    print(f"💰 SELL {label(c)} → {eur(p)}",flush=True)
    if c.get("sealed"):return False
    if DRY_RUN:
        print("🟡 DRY_RUN → simulata",flush=True)
        return True
    a=prepare(c,p)
    if not a:return False
    try:a=sign_auths(a)
    except Exception as e:
        print("❌ Firma:",e,flush=True)
        return False
    return create_sale(c,p,a)


def process(c,lids):
    n=label(c)
    if c.get("sealed") or kulenovic(c):return
    if c.get("liveSingleSaleOffer"):
        print(f"⏳ {n} → già in vendita",flush=True)
        return

    ok,(code,p)=validate(c,lids)
    if not ok:
        print(f"🚫 {n} → {code}"+(f" {eur(p)}" if p else ""),flush=True)
        return

    print(f"✅ {n} → VENDIBILE {eur(p)}",flush=True)
    print("🟢 AutoSell completato" if sell(c,p) else "🔴 AutoSell fallito",flush=True)


def worker():
    print(f"🤖 AUTOSELL {BOT_VERSION} | DRY_RUN={DRY_RUN}",flush=True)
    print("🚫 AUTOBUY | 🚫 SWAP | 🏆 LIMITED | 🔒 VAULT SKIP | 🛡️ LINEUP BLOCK",flush=True)

    if not check_account():return

    while True:
        try:
            cards=get_gallery()
            if cards is None:
                time.sleep(INTERVAL);continue

            lids=get_lineup()
            if lids is None:
                print("🛡️ LINEUP NON VERIFICABILE → NESSUNA VENDITA",flush=True)
                time.sleep(INTERVAL);continue

            print(f"🛡️ LINEUP: {len(lids)}",flush=True)

            for c in cards:
                try:process(c,lids)
                except Exception as e:print(f"❌ {label(c)}: {e}",flush=True)
                time.sleep(.25)

        except Exception as e:
            print("❌ Worker:",e,flush=True)

        time.sleep(INTERVAL)


def start_worker():
    global worker_started
    with worker_lock:
        if worker_started:return
        worker_started=True
        threading.Thread(target=worker,daemon=True,name="autosell-worker").start()


def which(name):
    for p in os.getenv("PATH","").split(os.pathsep):
        x=os.path.join(p,name)
        if os.path.isfile(x) and os.access(x,os.X_OK):return x
    return None


@app.get("/")
def home():
    return jsonify({
        "status":"online","bot":"sorare-autosell","version":BOT_VERSION,
        "dry_run":DRY_RUN,"autosell":True,"autobuy":False,"swap":False,
        "min_price_cents":MIN_PRICE,"max_price_cents":MAX_PRICE,
        "min_live_listings":MIN_LIVE_LISTINGS,
        "listing_duration_days":7,"kulenovic":"NEVER_SELL",
        "rarity":"LIMITED_ONLY","vault_cards":"EXCLUDED",
        "lineup_check":"blockchainCardsInLineups",
        "lineup_unknown_action":"BLOCK_SELL",
        "price_source":"liveSingleSaleOffers",
        "price_match":"PLAYER_RARITY_SEASON",
        "price_currency":"EUR","solana_authorization":"ED25519",
        "worker_started":worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status":"ok","bot":"autosell","version":BOT_VERSION,
        "worker_started":worker_started,"dry_run":DRY_RUN,
        "autosell":True,"autobuy":False,"swap":False,
        "rarity":"LIMITED_ONLY","vault_cards":"EXCLUDED",
        "lineup_unknown_action":"BLOCK_SELL"
    })


if __name__=="__main__":
    start_worker()
    app.run(host="0.0.0.0",
            port=int(os.getenv("PORT","10000")),
            debug=False)
