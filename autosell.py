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
BOT_VERSION="29.0-AUTOSELL-SOLANA"

KSLUG="sandro-kulenovic-2025-limited-385"
KASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
KID=os.getenv("KULENOVIC_ID","").strip()

worker_started=False
worker_lock=threading.Lock()
usd_rate=usd_time=0


def norm(x): return str(x or "").strip().lower()

def label(c):
    p=[c.get("name") or c.get("slug") or "Carta"]
    if c.get("seasonYear"): p.append(str(c["seasonYear"]))
    if c.get("rarityTyped"): p.append(str(c["rarityTyped"]))
    if c.get("serialNumber"): p.append(f"#{c['serialNumber']}")
    return " • ".join(p)

def eur(c): return "N/D" if c is None else f"€{c/100:.2f}"

def headers():
    if not TOKEN: raise RuntimeError("SORARE_JWT_TOKEN non configurato")
    t=TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN
    h={"Authorization":t,"Content-Type":"application/json","Accept":"application/json",
       "User-Agent":f"Sorare-AutoSell/{BOT_VERSION}"}
    if AUD: h["JWT-AUD"]=AUD
    return h


def graphql(query,variables=None):
    for n in range(3):
        try:
            r=requests.post(URL,json={"query":query,"variables":variables or {}},
                            headers=headers(),timeout=TIMEOUT)
            print(f"🌐 HTTP {r.status_code}",flush=True)
            if r.status_code==429:
                try:w=min(int(r.headers.get("Retry-After",n+2)),15)
                except:w=n+2
                time.sleep(w);continue
            if r.status_code!=200:
                print("❌ HTTP:",r.text[:800],flush=True)
                time.sleep(n+1);continue
            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:2500],flush=True)
            return d
        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(n+1)
    return None


def check_account():
    d=graphql("""query{currentUser{slug nickname starkKey}}""")
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:
        print("❌ Account Sorare non verificato",flush=True);return False
    print(f"✅ Account: {u.get('nickname') or u.get('slug')}",flush=True)
    print("🔐 Stark key account: "+("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),flush=True)
    return True


def get_gallery():
    out=[];after=None;total=sealed=0
    while True:
        d=graphql("""
        query($first:Int,$after:String){
          currentUser{cards(first:$first,after:$after,ownedByMe:true,sport:FOOTBALL){
            nodes{assetId slug name rarityTyped seasonYear serialNumber sealed
              anyPlayer{slug displayName} liveSingleSaleOffer{id status}}
            pageInfo{hasNextPage endCursor}
          }}
        }""",{"first":50,"after":after})
        if not d or d.get("errors"): return None
        cards=(((d.get("data") or {}).get("currentUser") or {}).get("cards") or {})
        nodes=cards.get("nodes") or []
        total+=len(nodes)
        for c in nodes:
            if c.get("sealed"): sealed+=1
            else: out.append(c)
        pi=cards.get("pageInfo") or {}
        if not pi.get("hasNextPage"): break
        after=pi.get("endCursor")
        if not after: break
    out=[c for c in out if norm(c.get("rarityTyped"))=="limited"]
    print(f"📦 Gallery: {total} | 🔒 Vault: {sealed} | 🏆 Limited: {len(out)}",flush=True)
    return out


def get_lineup():
    d=graphql("""query{currentUser{blockchainCardsInLineups(sport:FOOTBALL)}}""")
    if not d or d.get("errors"): return None
    v=(((d.get("data") or {}).get("currentUser") or {}).get("blockchainCardsInLineups"))
    return {norm(x) for x in v if x} if isinstance(v,list) else None


def identifiers(c):
    return {norm(c.get("assetId")),norm(c.get("slug"))}


def in_lineup(c,lineup):
    if lineup is None:return None
    return bool(identifiers(c)&lineup)


def is_kulenovic(c):
    wanted={norm(KSLUG),norm(KASSET)}
    if KID:wanted.add(norm(KID))
    return bool(identifiers(c)&wanted)


def usd_eur():
    global usd_rate,usd_time
    now=time.time()
    if usd_rate and now-usd_time<300:return usd_rate
    try:
        r=requests.get("https://api.frankfurter.app/latest",
                       params={"from":"USD","to":"EUR"},timeout=10)
        if r.status_code!=200:return None
        rate=float(r.json()["rates"]["EUR"])
        if rate<=0:return None
        usd_rate,usd_time=rate,now
        return rate
    except:return None


def price_eur(a):
    if not isinstance(a,dict):return None
    try:
        x=int(a.get("eurCents"))
        if x>0:return x
    except:pass
    try:x=float(a.get("usdCents"))
    except:x=0
    rate=usd_eur() if x>0 else None
    return int(round(x*rate)) if rate else None


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
                x=price_eur((o.get("receiverSide") or {}).get("amounts") or {})
                if x is not None:prices.append(x)
                break
    return min(prices) if len(prices)>=MIN_LIVE_LISTINGS else None


def validate(c,lineup):
    if c.get("sealed"):return False,"VAULT",None
    if is_kulenovic(c):return False,"KULENOVIC",None
    if norm(c.get("rarityTyped"))!="limited":return False,"RARITY",None
    il=in_lineup(c,lineup)
    if il is None:return False,"LINEUP_UNKNOWN",None
    if il:return False,"LINEUP",None
    floor=live_floor(c)
    if floor is None:return False,"PRICE_UNKNOWN",None
    if floor<MIN_PRICE:return False,"PRICE_LOW",floor
    if floor>MAX_PRICE:return False,"PRICE_HIGH",floor
    return True,"OK",floor


def reject(c,reason,value=None):
    msgs={
        "VAULT":"CARTA IN CASSAFORTE",
        "KULENOVIC":"KULENOVIC MAI IN VENDITA",
        "RARITY":"RARITÀ DIVERSA DA LIMITED",
        "LINEUP":"CARTA IN LINEUP",
        "LINEUP_UNKNOWN":"LINEUP NON VERIFICABILE",
        "PRICE_UNKNOWN":"PREZZO LIVE NON VERIFICABILE",
    }
    msg=msgs.get(reason)
    if reason=="PRICE_LOW":msg=f"FLOOR {eur(value)} SOTTO IL MINIMO"
    if reason=="PRICE_HIGH":msg=f"FLOOR {eur(value)} SOPRA IL MASSIMO"
    print(f"🚫 {label(c)} → {msg or reason}",flush=True)


def node():
    for d in os.getenv("PATH","").split(os.pathsep):
        x=os.path.join(d,"node")
        if os.path.isfile(x) and os.access(x,os.X_OK):return x
    return None


def sign_solana(auth):
    n=node()
    if not n:raise RuntimeError("Node.js non disponibile")
    if not PRIVATE_KEY:raise RuntimeError("SORARE_STARK_PRIVATE_KEY non configurata")

    script=r'''
const crypto=require("crypto");
const{createSignableMessage,getBase58Decoder,
createKeyPairFromPrivateKeyBytes,createSignerFromKeyPair}=require("@solana/kit");
const{HDKey}=require("micro-key-producer/slip10.js");

(async()=>{
 const i=JSON.parse(require("fs").readFileSync(0,"utf8"));
 const a=i.authorization,r=a.request;
 if(r.__typename!=="SolanaTokenTransferAuthorizationRequest")
   throw Error("Authorization non Solana: "+r.__typename);

 const seed=Buffer.from(i.privateKey.replace(/^0x/,""),"hex");
 const{privateKey}=HDKey.fromMasterSeed(seed).derive("m/44'/501'/0'/0'");
 const kp=await createKeyPairFromPrivateKeyBytes(privateKey);
 const signer=createSignerFromKeyPair(kp);

 if(signer.address!==r.senderAddress)
   throw Error("senderAddress mismatch: "+signer.address+" != "+r.senderAddress);

 const msg=["TRANSFER",r.transferProxyProgramAddress,r.merkleTreeAddress,
 r.leafIndex.toString(),r.nonce,r.expirationTimestamp.toString(),
 r.receiverAddress,"0x",r.originator].join(":");

 const hash=await crypto.webcrypto.subtle.digest(
 "SHA-256",new TextEncoder().encode(msg));
 const sm=createSignableMessage(new Uint8Array(hash));
 const[sigs]=await signer.signMessages([sm]);
 const signature=getBase58Decoder().decode(sigs[signer.address]);

 process.stdout.write(JSON.stringify({
   fingerprint:a.fingerprint,
   solanaTokenTransferApproval:{
     signature,nonce:r.nonce,expirationTimestamp:r.expirationTimestamp
   }
 }));
})().catch(e=>{console.error(e);process.exit(1)});
'''

    p=subprocess.run([n,"-e",script],input=json.dumps({
        "authorization":auth,
        "privateKey":PRIVATE_KEY
    }),text=True,capture_output=True,timeout=60)

    if p.returncode!=0:
        raise RuntimeError(p.stderr.strip() or "firma Solana fallita")

    return json.loads(p.stdout)


def prepare_offer(asset_id,price):
    inp={
        "sendAssetIds":[asset_id],
        "receiveAssetIds":[],
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "settlementCurrencies":["EUR"],
        "clientMutationId":str(uuid.uuid4())
    }

    d=graphql("""
    mutation($input:prepareOfferInput!){
      prepareOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename
            ... on SolanaTokenTransferAuthorizationRequest{
              assetId expirationTimestamp leafIndex merkleTreeAddress
              nonce originator receiverAddress senderAddress transferProxyProgramAddress
            }
          }
        }
        errors{message}
      }
    }""",{"input":inp})

    p=((d or {}).get("data") or {}).get("prepareOffer") or {}
    errors=p.get("errors") or []
    if errors:
        raise RuntimeError("; ".join(x.get("message","") for x in errors))

    auths=p.get("authorizations") or []
    if not auths:raise RuntimeError("prepareOffer non ha restituito autorizzazioni")

    approvals=[]
    for a in auths:
        r=a.get("request") or {}
        if r.get("__typename")!="SolanaTokenTransferAuthorizationRequest":
            raise RuntimeError("Authorization non supportata: "+str(r.get("__typename")))
        approvals.append(sign_solana(a))

    return approvals


def create_offer(asset_id,price,approvals):
    inp={
        "approvals":approvals,
        "dealId":str(uuid.uuid4()),
        "assetId":asset_id,
        "receiveAmount":{"amount":str(price),"currency":"EUR"},
        "duration":LISTING_DURATION,
        "clientMutationId":str(uuid.uuid4())
    }

    d=graphql("""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id}
        errors{message}
      }
    }""",{"input":inp})

    p=((d or {}).get("data") or {}).get("createSingleSaleOffer") or {}
    errors=p.get("errors") or []
    if errors:raise RuntimeError("; ".join(x.get("message","") for x in errors))
    offer=p.get("tokenOffer")
    if not offer:raise RuntimeError("createSingleSaleOffer non ha restituito tokenOffer")
    return offer


def autosell(c,price):
    aid=c.get("assetId")
    if not aid:raise RuntimeError("assetId mancante")

    print(f"💰 SELL {label(c)} → {eur(price)}",flush=True)

    if DRY_RUN:
        print("🟡 DRY_RUN: nessuna vendita eseguita",flush=True)
        return True

    approvals=prepare_offer(aid,price)
    offer=create_offer(aid,price,approvals)
    print(f"✅ OFFER CREATA: {offer.get('id')}",flush=True)
    return True


def worker():
    global worker_started
    with worker_lock:
        if worker_started:return
        worker_started=True

    print(f"🚀 AutoSell {BOT_VERSION} | DRY_RUN={DRY_RUN}",flush=True)

    while True:
        try:
            if not check_account():
                time.sleep(INTERVAL);continue

            cards=get_gallery()
            if cards is None:
                time.sleep(INTERVAL);continue

            lineup=get_lineup()
            if lineup is None:
                print("❌ Impossibile verificare lineup",flush=True)
                time.sleep(INTERVAL);continue

            for c in cards:
                try:
                    if c.get("liveSingleSaleOffer"):
                        continue

                    ok,reason,price=validate(c,lineup)
                    if not ok:
                        reject(c,reason,price)
                        continue

                    print(f"✅ {label(c)} → VENDIBILE {eur(price)}",flush=True)

                    try:
                        autosell(c,price)
                    except Exception as e:
                        print(f"🔴 AutoSell fallito: {e}",flush=True)

                except Exception as e:
                    print(f"❌ Carta {label(c)}: {e}",flush=True)

        except Exception as e:
            print(f"🔥 Worker: {e}",flush=True)

        time.sleep(INTERVAL)


@app.get("/")
def home():
    return jsonify({
        "status":"ok",
        "bot":BOT_VERSION,
        "dry_run":DRY_RUN
    })


@app.get("/health")
def health():
    return jsonify({"status":"ok","dry_run":DRY_RUN})


if __name__=="__main__":
    threading.Thread(target=worker,daemon=True).start()
    port=int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0",port=port)
