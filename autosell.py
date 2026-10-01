import os,json,time,uuid,subprocess,threading,requests
from flask import Flask,jsonify

app=Flask(__name__)
URL="https://api.sorare.com/graphql"

TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
STARK=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()
SOLANA=os.getenv("SORARE_SOLANA_PRIVATE_KEY","").strip()

DRY=os.getenv("DRY_RUN","false").lower()=="true"
INTERVAL=int(os.getenv("INTERVAL","30"))
TIMEOUT=int(os.getenv("TIMEOUT","25"))
STATE=os.getenv("BOT_STATE_PATH","bot_state.json")

MAX_FLOOR=70
MIN_PRICE=30
MIN_LISTINGS=5

KUL_SLUG="sandro-kulenovic-2025-limited-385"
KUL_ASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

lock=threading.RLock()
started=False


# ============================================================
# UTILS / STATE
# ============================================================

def norm(x): return str(x or "").strip().lower()
def aid(c): return str(c.get("assetId") or c.get("asset_id") or "").strip()
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
def uid(): return str(uuid.uuid4())
def eur(c): return f"€{c/100:.2f}"

def default_state():
    return {
        "processed_offers":[],
        "acquired_cards":[],
        "pending_autobuys":[],
        "updated_at":int(time.time())
    }

def save(d):
    tmp=STATE+"."+uuid.uuid4().hex+".tmp"
    try:
        with open(tmp,"w",encoding="utf8") as f:
            json.dump(d,f,ensure_ascii=False,indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,STATE)
        return True
    except Exception as e:
        print("❌ STATE:",e,flush=True)
        try: os.remove(tmp)
        except: pass
        return False

def load():
    if not os.path.exists(STATE):
        os.makedirs(os.path.dirname(os.path.abspath(STATE)),exist_ok=True)
        save(default_state())

    try:
        with open(STATE,encoding="utf8") as f:
            d=json.load(f)
        if not isinstance(d,dict):
            d=default_state()
    except:
        d=default_state()

    for k,v in default_state().items():
        d.setdefault(k,v)

    return d

def cards():
    with lock:
        x=load().get("acquired_cards",[])
        return x if isinstance(x,list) else []

def update(asset,status=None,offer=None,error=None,price=None):
    with lock:
        d=load()

        for c in d["acquired_cards"]:
            if norm(aid(c))!=norm(asset):
                continue

            if status is not None:
                c["status"]=status
            if offer:
                c["sale_offer_id"]=offer
            if price is not None:
                c["sale_price_eur_cents"]=price

            c["last_error"]=error
            c["updated_at"]=now()
            d["updated_at"]=int(time.time())

            return save(d)

    return False

def sellable():
    return [
        dict(c) for c in cards()
        if isinstance(c,dict)
        and norm(c.get("status")) in ("da_vendere","ready")
        and aid(c)
    ]


# ============================================================
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    h={
        "Authorization":
            TOKEN if TOKEN.lower().startswith("bearer ")
            else "Bearer "+TOKEN,
        "Content-Type":"application/json",
        "Accept":"application/json",
        "User-Agent":"Sorare-AutoSell-15"
    }

    if AUD:
        h["JWT-AUD"]=AUD

    return h

def gql(q,v=None):
    for n in range(3):
        try:
            r=requests.post(
                URL,
                headers=headers(),
                json={"query":q,"variables":v or {}},
                timeout=TIMEOUT
            )

            print("🌐 Sorare HTTP",r.status_code,flush=True)

            if r.status_code==429:
                time.sleep(2+n*2)
                continue

            if r.status_code!=200:
                time.sleep(n+1)
                continue

            d=r.json()

            if d.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(d["errors"])[:2000],
                    flush=True
                )

            return d

        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(n+1)

    return None


# ============================================================
# ACCOUNT / CARD
# ============================================================

def account():
    d=gql("""
    query{
      currentUser{
        slug nickname starkKey
      }
    }""")

    u=((d or {}).get("data") or {}).get("currentUser")

    if not u:
        return False

    print(
        "✅ Sorare:",
        u.get("nickname") or u.get("slug"),
        flush=True
    )

    return True

def details(asset):
    d=gql("""
    query($ids:[String!]!){
      anyCards(assetIds:$ids){
        assetId slug name rarityTyped seasonYear
        anyPlayer{
          slug displayName
          activeClub{slug name}
        }
      }
    }""",{"ids":[asset]})

    x=(
        ((d or {}).get("data") or {})
        .get("anyCards") or []
    )

    return x[0] if x else None


# ============================================================
# ACTIVE OFFER
# ============================================================

def active_offer(asset):
    d=gql("""
    query($id:String!){
      anyCards(assetIds:[$id]){
        assetId
        liveSingleSaleOffer{id startDate endDate}
      }
    }""",{"id":asset})

    x=(
        ((d or {}).get("data") or {})
        .get("anyCards") or []
    )

    for c in x:
        if norm(c.get("assetId"))==norm(asset):
            o=c.get("liveSingleSaleOffer") or {}

            if o.get("id"):
                return o

    return None


# ============================================================
# FLOOR
# ============================================================

def usd_eur(x):
    try:
        r=requests.get(
            "https://api.frankfurter.app/latest",
            params={"from":"USD","to":"EUR"},
            timeout=10
        ).json()["rates"]["EUR"]

        return round(int(x)*float(r))

    except:
        return None

def amount(a):
    try:
        x=int(a.get("eurCents") or 0)

        if x>0:
            return x

    except:
        pass

    try:
        x=int(a.get("usdCents") or 0)

        return usd_eur(x) if x>0 else None

    except:
        return None

def floor(c):
    p=c.get("anyPlayer") or {}

    slug=norm(p.get("slug"))
    rarity=norm(c.get("rarityTyped"))

    try:
        season=int(c.get("seasonYear"))
    except:
        return None

    if not slug or not rarity:
        return None

    d=gql("""
    query($slug:String,$first:Int){
      tokens{
        liveSingleSaleOffers(playerSlug:$slug,first:$first){
          nodes{
            senderSide{
              anyCards{
                assetId rarityTyped seasonYear
                anyPlayer{slug}
              }
            }
            receiverSide{
              amounts{eurCents usdCents}
            }
          }
        }
      }
    }""",{"slug":slug,"first":50})

    nodes=(
        (((d or {}).get("data") or {})
        .get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices=[]

    for o in nodes:
        for x in (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        ):
            p2=x.get("anyPlayer") or {}

            try:
                same=int(x.get("seasonYear"))==season
            except:
                same=False

            if (
                same
                and norm(p2.get("slug"))==slug
                and norm(x.get("rarityTyped"))==rarity
            ):
                v=amount(
                    (o.get("receiverSide") or {})
                    .get("amounts") or {}
                )

                if v is not None:
                    prices.append(v)

                break

    print(
        f"📊 Listing: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return (
        min(prices)
        if len(prices)>=MIN_LISTINGS
        else None
    )


# ============================================================
# VALIDATION
# ============================================================

def source_allowed(source):
    return norm(source) in ("autobuy","swap")

def kul(c):
    return (
        norm(c.get("slug"))==norm(KUL_SLUG)
        or norm(c.get("assetId"))==norm(KUL_ASSET)
    )

def validate(original,detail):
    source=norm(original.get("source"))

    print(
        "🏷️ SOURCE →",
        source or "(vuota)",
        flush=True
    )

    if not source_allowed(source):
        return False,"SOURCE",None

    if kul(original) or kul(detail):
        return False,"KULENOVIC",None

    if norm(detail.get("rarityTyped")).upper()!="LIMITED":
        return False,"RARITY",None

    f=floor(detail)

    if f is None:
        return False,"FLOOR_UNKNOWN",None

    if f>MAX_FLOOR:
        return False,"FLOOR_HIGH",f

    price=max(MIN_PRICE,f)

    print(
        f"🎯 PREZZO → {eur(price)}",
        flush=True
    )

    return True,"OK",price


# ============================================================
# PREPARE
# ============================================================

PREPARE="""
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

def prepare(asset,price):
    d=gql(
        PREPARE,
        {
            "input":{
                "sendAssetIds":[asset],
                "receiveAssetIds":[],
                "settlementCurrencies":["EUR"],
                "receiveAmount":{
                    "amount":str(price),
                    "currency":"EUR"
                },
                "clientMutationId":uid()
            }
        }
    )

    r=(
        ((d or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not r or r.get("errors"):
        print(
            "❌ prepareOffer",
            r,
            flush=True
        )
        return None

    return r.get("authorizations") or []


# ============================================================
# SIGN
#
# IMPORTANT:
# Sorare's Solana key is derived from the Sorare/Ethereum
# private key (STARK) using SLIP-0010:
#
# m/44'/501'/0'/0'
#
# SORARE_SOLANA_PRIVATE_KEY is intentionally NOT used here.
# ============================================================

def sign(auths):
    node=os.getenv("NODE_BIN","node")

    js=r'''
const fs=require("fs");

const {
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair,
  createSignableMessage,
  getBase58Decoder
}=require("@solana/kit");

const {HDKey}=require("micro-key-producer/slip10.js");

const {
  signAuthorizationRequest
}=require("@sorare/crypto");

const input=JSON.parse(
  fs.readFileSync(0,"utf8")
);

const PATH="m/44'/501'/0'/0'";

function deriveSolanaSigner(privateKey){
  const hex=String(privateKey||"")
    .trim()
    .replace(/^0x/,"");

  if(!/^[0-9a-fA-F]{64}$/.test(hex)){
    throw new Error(
      "SORARE_STARK_PRIVATE_KEY non valida: "
      +"attesi 32 byte esadecimali"
    );
  }

  const seed=Buffer.from(hex,"hex");

  const {privateKey:derived}=HDKey
    .fromMasterSeed(seed)
    .derive(PATH);

  return createKeyPairFromPrivateKeyBytes(
    derived
  ).then(createSignerFromKeyPair);
}

async function solana(a){
  const r=a.request;

  const signer=await deriveSolanaSigner(
    input.starkPrivateKey
  );

  console.error(
    "🔑 DERIVED SOLANA →",
    signer.address
  );

  console.error(
    "📨 SORARE SENDER →",
    r.senderAddress
  );

  if(signer.address!==r.senderAddress){
    throw new Error(
      "Derived Solana address != Sorare senderAddress"
    );
  }

  const message=[
    "TRANSFER",
    r.transferProxyProgramAddress,
    r.merkleTreeAddress,
    String(r.leafIndex),
    String(r.nonce),
    String(r.expirationTimestamp),
    r.receiverAddress,
    "0x",
    r.originator
  ].join(":");

  const messageBytes=
    new TextEncoder().encode(message);

  const messageHash=
    await crypto.subtle.digest(
      "SHA-256",
      messageBytes
    );

  const signableMessage=
    createSignableMessage(
      new Uint8Array(messageHash)
    );

  /*
   * IMPORTANT:
   * signMessages() returns an array.
   * The first element contains the signatures map.
   */
  const [signatures]=
    await signer.signMessages([
      signableMessage
    ]);

  const signature=
    getBase58Decoder().decode(
      signatures[signer.address]
    );

  return {
    fingerprint:a.fingerprint,

    solanaTokenTransferApproval:{
      signature,
      nonce:r.nonce,
      expirationTimestamp:r.expirationTimestamp
    }
  };
}

function stark(a){
  const r=a.request;

  const signature=
    signAuthorizationRequest(
      input.starkPrivateKey,
      r
    );

  const base={
    fingerprint:a.fingerprint
  };

  if(
    r.__typename===
    "StarkexTransferAuthorizationRequest"
  ){
    return {
      ...base,
      starkexTransferApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature
      }
    };
  }

  if(
    r.__typename===
    "StarkexLimitOrderAuthorizationRequest"
  ){
    return {
      ...base,
      starkexLimitOrderApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature
      }
    };
  }

  if(
    r.__typename===
    "MangopayWalletTransferAuthorizationRequest"
  ){
    return {
      ...base,
      mangopayWalletTransferApproval:{
        nonce:r.nonce,
        signature
      }
    };
  }

  throw new Error(
    "Authorization non supportata: "+
    r.__typename
  );
}

(async()=>{
  const out=[];

  for(const a of input.authorizations){
    const type=
      (a.request||{}).__typename;

    if(
      type===
      "SolanaTokenTransferAuthorizationRequest"
    ){
      out.push(await solana(a));
    }else{
      out.push(stark(a));
    }
  }

  process.stdout.write(
    JSON.stringify(out)
  );

})().catch(e=>{
  console.error(
    e.stack||e
  );

  process.exit(1);
});
'''

    p=subprocess.run(
        [node,"-e",js],
        input=json.dumps({
            "authorizations":auths,
            "starkPrivateKey":STARK
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.stderr:
        print(
            p.stderr.strip(),
            flush=True
        )

    if p.returncode!=0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    if not p.stdout.strip():
        raise RuntimeError(
            "Node non ha restituito una firma"
        )

    try:
        return json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(
            "Output firma non JSON: "+
            p.stdout[:1000]
        ) from e


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(asset,price):
    if DRY:
        print(
            f"🟡 DRY RUN → {eur(price)}",
            flush=True
        )
        return "DRY-RUN"

    auth=prepare(asset,price)

    if not auth:
        return None

    try:
        approvals=sign(auth)

    except Exception as e:
        print(
            "❌ FIRMA:",
            e,
            flush=True
        )
        return None

    q="""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{id startDate endDate}
        errors{message}
      }
    }"""

    d=gql(
        q,
        {
            "input":{
                "approvals":approvals,
                "dealId":uid(),
                "assetId":asset,
                "settlementCurrencies":"EUR",
                "receiveAmount":{
                    "amount":str(price),
                    "currency":"EUR"
                },
                "clientMutationId":uid()
            }
        }
    )

    r=(
        ((d or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not r:
        return None

    errors=r.get("errors") or []

    if errors:
        text=" ".join(
            str(x.get("message",""))
            for x in errors
            if isinstance(x,dict)
        )

        if "active public offer already exists" in norm(text):
            o=active_offer(asset)

            return (
                o.get("id")
                if o
                else "ALREADY-LISTED"
            )

        print(
            "❌ SALE:",
            text,
            flush=True
        )

        return None

    offer=r.get("tokenOffer") or {}
    oid=offer.get("id")

    if not oid:
        return None

    print(
        "✅ INSERZIONE →",
        oid,
        flush=True
    )

    return oid


# ============================================================
# PROCESS
# ============================================================

def process(original):
    asset=aid(original)

    print(
        f"\n💰 AUTOSELL → {asset}",
        flush=True
    )

    print(
        "🏷️ SOURCE →",
        original.get("source",""),
        flush=True
    )

    old=active_offer(asset)

    if old:
        print(
            "🟢 GIÀ IN VENDITA →",
            old["id"],
            flush=True
        )

        update(
            asset,
            "SELLING",
            old["id"],
            None
        )

        return

    previous=original.get(
        "sale_price_eur_cents"
    )

    if previous:
        print(
            f"🔄 EXPIRED → SAME PRICE "
            f"{eur(int(previous))}",
            flush=True
        )

        oid=create_sale(
            asset,
            int(previous)
        )

        if oid:
            update(
                asset,
                "SELLING",
                oid,
                None,
                int(previous)
            )
        else:
            update(
                asset,
                "da_vendere",
                error="RELIST_FAILED"
            )

        return

    d=details(asset)

    if not d:
        update(
            asset,
            "da_vendere",
            error="CARD_DETAILS"
        )
        return

    print(
        f"🃏 {d.get('name') or d.get('slug')} "
        f"{d.get('seasonYear')} • "
        f"{norm(d.get('rarityTyped'))}",
        flush=True
    )

    ok,reason,price=validate(
        original,
        d
    )

    if not ok:
        print(
            "🚫 ESCLUSA →",
            reason,
            flush=True
        )

        update(
            asset,
            "BLOCKED"
            if reason in (
                "SOURCE",
                "KULENOVIC",
                "RARITY"
            )
            else "da_vendere",
            error=reason
        )

        return

    print(
        f"🎯 PREZZO → {eur(price)}",
        flush=True
    )

    update(
        asset,
        "SELLING",
        error=None,
        price=price
    )

    oid=create_sale(
        asset,
        price
    )

    if not oid:
        update(
            asset,
            "da_vendere",
            error="CREATE_SALE_FAILED"
        )
        return

    update(
        asset,
        "SELLING",
        oid,
        None,
        price
    )

    print(
        "🟢 STATO → SELLING",
        flush=True
    )


# ============================================================
# EXPIRATION / RECOVERY
# ============================================================

def recovery():
    for c in cards():
        if norm(c.get("status"))!="selling":
            continue

        asset=aid(c)
        old=active_offer(asset)

        if old:
            continue

        price=c.get(
            "sale_price_eur_cents"
        )

        if not price:
            update(
                asset,
                "da_vendere",
                error="EXPIRED_NO_PRICE"
            )
            continue

        print(
            f"🔄 SCADUTA → {asset} → "
            f"stesso prezzo {eur(int(price))}",
            flush=True
        )

        oid=create_sale(
            asset,
            int(price)
        )

        if oid:
            update(
                asset,
                "SELLING",
                oid,
                None,
                int(price)
            )
        else:
            update(
                asset,
                "da_vendere",
                error="RELIST_FAILED"
            )


# ============================================================
# WORKER
# ============================================================

def worker():
    print(
        "🤖 AUTOSELL AUTOSell-15",
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

    try:
        headers()

        os.makedirs(
            os.path.dirname(
                os.path.abspath(STATE)
            ),
            exist_ok=True
        )

        if not account():
            return

    except Exception as e:
        print(
            "❌ CONFIG:",
            e,
            flush=True
        )
        return

    while True:
        try:
            recovery()

            for c in sellable():
                try:
                    process(c)

                except Exception as e:
                    print(
                        "❌ PROCESS:",
                        e,
                        flush=True
                    )

                    update(
                        aid(c),
                        "da_vendere",
                        error=str(e)
                    )

            time.sleep(INTERVAL)

        except Exception as e:
            print(
                "❌ WORKER:",
                e,
                flush=True
            )

            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    c=cards()

    return jsonify({
        "status":"online",
        "version":"AUTOSell-15",
        "source":"autobuy+swap",
        "floor_max":"€0.70",
        "min_price":"€0.30",
        "expired":"same_previous_price",
        "cards":len(c),

        "da_vendere":sum(
            norm(x.get("status"))
            in ("da_vendere","ready")
            for x in c
            if isinstance(x,dict)
        ),

        "selling":sum(
            norm(x.get("status"))=="selling"
            for x in c
            if isinstance(x,dict)
        ),

        "worker":started
    })

@app.get("/health")
def health():
    return jsonify({
        "status":"ok",
        "worker":started,
        "dry_run":DRY
    })

@app.get("/cards")
def card_endpoint():
    c=cards()

    return jsonify({
        "count":len(c),
        "cards":c
    })


def start():
    global started

    with lock:
        if started:
            return

        started=True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()


if __name__=="__main__":
    start()

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
