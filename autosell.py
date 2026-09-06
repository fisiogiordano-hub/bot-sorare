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

BOT_VERSION="28.2-AUTOSELL-SOLANA-SAFE"

KSLUG="sandro-kulenovic-2025-limited-385"
KASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"
KID=os.getenv("KULENOVIC_ID","").strip()

worker_started=False
worker_lock=threading.Lock()
usd_rate=None
usd_time=0


def norm(x):
    return str(x or "").strip().lower()


def label(c):
    p=[c.get("name") or c.get("slug") or "Carta"]
    if c.get("seasonYear"):p.append(str(c["seasonYear"]))
    if c.get("rarityTyped"):p.append(str(c["rarityTyped"]))
    if c.get("serialNumber"):p.append(f"#{c['serialNumber']}")
    return " • ".join(p)


def eur(x):
    return "N/D" if x is None else f"€{x/100:.2f}"


def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")
    t=TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN
    h={
        "Authorization":t,
        "Content-Type":"application/json",
        "Accept":"application/json",
        "User-Agent":f"Sorare-AutoSell/{BOT_VERSION}"
    }
    if AUD:h["JWT-AUD"]=AUD
    return h


def graphql(query,variables=None):
    for n in range(3):
        try:
            r=requests.post(
                URL,
                json={"query":query,"variables":variables or {}},
                headers=headers(),
                timeout=TIMEOUT
            )
            print(f"🌐 HTTP {r.status_code}",flush=True)

            if r.status_code==429:
                try:w=min(int(r.headers.get("Retry-After",n+2)),15)
                except:w=n+2
                time.sleep(w)
                continue

            if r.status_code!=200:
                print("❌ HTTP:",r.text[:800],flush=True)
                time.sleep(n+1)
                continue

            d=r.json()

            if d.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(d["errors"],ensure_ascii=False)[:2500],
                    flush=True
                )

            return d

        except Exception as e:
            print("❌ GraphQL:",e,flush=True)
            time.sleep(n+1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    d=graphql("""
    query{
      currentUser{
        slug
        nickname
        starkKey
      }
    }""")

    u=((d or {}).get("data") or {}).get("currentUser")

    if not u:
        print("❌ Account Sorare non verificato",flush=True)
        return False

    print(
        f"✅ Account: {u.get('nickname') or u.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key: "+
        ("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),
        flush=True
    )

    return True


# ============================================================
# GALLERY
# ============================================================

def get_gallery():
    out=[]
    after=None
    total=sealed=0

    while True:
        d=graphql("""
        query($first:Int,$after:String){
          currentUser{
            cards(
              first:$first
              after:$after
              ownedByMe:true
              sport:FOOTBALL
            ){
              nodes{
                assetId
                slug
                name
                rarityTyped
                seasonYear
                serialNumber
                sealed
                anyPlayer{
                  slug
                  displayName
                }
                liveSingleSaleOffer{
                  id
                  status
                }
              }
              pageInfo{
                hasNextPage
                endCursor
              }
            }
          }
        }""",{"first":50,"after":after})

        if not d or d.get("errors"):
            return None

        cards=(
            ((d.get("data") or {}).get("currentUser") or {})
            .get("cards") or {}
        )

        nodes=cards.get("nodes") or []
        total+=len(nodes)

        for c in nodes:
            if c.get("sealed"):
                sealed+=1
            elif norm(c.get("rarityTyped"))=="limited":
                out.append(c)

        pi=cards.get("pageInfo") or {}

        if not pi.get("hasNextPage"):
            break

        after=pi.get("endCursor")

        if not after:
            break

    print(
        f"📦 Gallery: {total} | 🔒 Vault: {sealed} | "
        f"🏆 Limited: {len(out)}",
        flush=True
    )

    return out


# ============================================================
# LINEUP
# ============================================================

def get_lineup():
    d=graphql("""
    query{
      currentUser{
        blockchainCardsInLineups(sport:FOOTBALL)
      }
    }""")

    if not d or d.get("errors"):
        return None

    v=(
        ((d.get("data") or {}).get("currentUser") or {})
        .get("blockchainCardsInLineups")
    )

    if not isinstance(v,list):
        return None

    return {norm(x) for x in v if x}


def identifiers(c):
    return {
        norm(c.get("assetId")),
        norm(c.get("slug"))
    }-{""}


def in_lineup(c,lineup):
    if lineup is None:
        return None

    return bool(identifiers(c)&lineup)


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(c):
    wanted={norm(KSLUG),norm(KASSET)}

    if KID:
        wanted.add(norm(KID))

    return bool(identifiers(c)&wanted)


# ============================================================
# USD/EUR
# ============================================================

def usd_eur():
    global usd_rate,usd_time

    now=time.time()

    if usd_rate and now-usd_time<300:
        return usd_rate

    try:
        r=requests.get(
            "https://api.frankfurter.app/latest",
            params={"from":"USD","to":"EUR"},
            timeout=10
        )

        if r.status_code!=200:
            return None

        rate=float(r.json()["rates"]["EUR"])

        if rate<=0:
            return None

        usd_rate=rate
        usd_time=now

        return rate

    except:
        return None


def price_eur(a):
    if not isinstance(a,dict):
        return None

    try:
        x=int(a.get("eurCents"))
        if x>0:
            return x
    except:
        pass

    try:
        x=float(a.get("usdCents"))
    except:
        x=0

    if x<=0:
        return None

    rate=usd_eur()

    return int(round(x*rate)) if rate else None


# ============================================================
# FLOOR
# ============================================================

def live_floor(c):
    p=c.get("anyPlayer") or {}
    slug=norm(p.get("slug"))
    rarity=norm(c.get("rarityTyped"))

    try:
        season=int(c.get("seasonYear"))
    except:
        return None

    if not slug or rarity!="limited":
        return None

    d=graphql("""
    query($playerSlug:String,$first:Int){
      tokens{
        liveSingleSaleOffers(
          playerSlug:$playerSlug
          first:$first
        ){
          nodes{
            senderSide{
              anyCards{
                rarityTyped
                seasonYear
                anyPlayer{slug}
              }
            }
            receiverSide{
              amounts{
                eurCents
                usdCents
              }
            }
          }
        }
      }
    }""",{
        "playerSlug":slug,
        "first":50
    })

    if not d or d.get("errors"):
        return None

    offers=(
        (((d.get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {})
        .get("nodes") or []
    )

    prices=[]

    for o in offers:
        for mc in (o.get("senderSide") or {}).get("anyCards") or []:
            mp=mc.get("anyPlayer") or {}

            try:
                ms=int(mc.get("seasonYear"))
            except:
                continue

            if (
                norm(mp.get("slug"))==slug and
                norm(mc.get("rarityTyped"))==rarity and
                ms==season
            ):
                x=price_eur(
                    (o.get("receiverSide") or {}).get("amounts") or {}
                )

                if x is not None:
                    prices.append(x)

                break

    if len(prices)<MIN_LIVE_LISTINGS:
        return None

    return min(prices)


# ============================================================
# VALIDAZIONE
# ============================================================

def validate(c,lineup):
    if c.get("sealed"):
        return False,"VAULT",None

    if is_kulenovic(c):
        return False,"KULENOVIC",None

    if norm(c.get("rarityTyped"))!="limited":
        return False,"RARITY",None

    il=in_lineup(c,lineup)

    if il is None:
        return False,"LINEUP_UNKNOWN",None

    if il:
        return False,"LINEUP",None

    floor=live_floor(c)

    if floor is None:
        return False,"PRICE_UNKNOWN",None

    if floor<MIN_PRICE:
        return False,"PRICE_LOW",floor

    if floor>MAX_PRICE:
        return False,"PRICE_HIGH",floor

    return True,"OK",floor


def reject(c,reason,value=None):
    if reason=="VAULT":
        m="CARTA IN CASSAFORTE"
    elif reason=="KULENOVIC":
        m="KULENOVIC MAI IN VENDITA"
    elif reason=="RARITY":
        m="RARITÀ DIVERSA DA LIMITED"
    elif reason=="LINEUP":
        m="LINEUP"
    elif reason=="LINEUP_UNKNOWN":
        m="LINEUP NON VERIFICABILE"
    elif reason=="PRICE_UNKNOWN":
        m="PREZZO LIVE NON VERIFICABILE"
    elif reason=="PRICE_LOW":
        m=f"PRICE_LOW {eur(value)}"
    elif reason=="PRICE_HIGH":
        m=f"PRICE_HIGH {eur(value)}"
    else:
        m=reason

    print(f"🚫 {label(c)} → {m}",flush=True)


# ============================================================
# NODE
# ============================================================

def node():
    for d in os.getenv("PATH","").split(os.pathsep):
        p=os.path.join(d,"node")

        if os.path.isfile(p) and os.access(p,os.X_OK):
            return p

    return None


# ============================================================
# SOLANA
# ============================================================

def sign_solana(auth):
    n=node()

    if not n:
        raise RuntimeError("Node.js non disponibile")

    if not PRIVATE_KEY:
        raise RuntimeError("SORARE_STARK_PRIVATE_KEY non configurata")

    script=r'''
const fs=require("fs");
const crypto=require("crypto");

const {
  createSignableMessage,
  getBase58Encoder,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
}=require("@solana/kit");

const {HDKey}=require("micro-key-producer/slip10.js");

async function main(){
  const i=JSON.parse(fs.readFileSync(0,"utf8"));
  const a=i.authorization;
  const r=a.request;

  if(r.__typename!=="SolanaTokenTransferAuthorizationRequest")
    throw Error("Authorization non Solana: "+r.__typename);

  const seed=Buffer.from(
    i.privateKey.replace(/^0x/,""),
    "hex"
  );

  const {privateKey}=HDKey
    .fromMasterSeed(seed)
    .derive("m/44'/501'/0'/0'");

  const kp=await createKeyPairFromPrivateKeyBytes(privateKey);
  const signer=createSignerFromKeyPair(kp);

  if(signer.address!==r.senderAddress)
    throw Error(
      "senderAddress mismatch: "+
      signer.address+" != "+r.senderAddress
    );

  const message=[
    "TRANSFER",
    r.transferProxyProgramAddress,
    r.merkleTreeAddress,
    String(r.leafIndex),
    r.nonce,
    String(r.expirationTimestamp),
    r.receiverAddress,
    "0x",
    r.originator
  ].join(":");

  const hash=await crypto.webcrypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(message)
  );

  const signable=createSignableMessage(
    new Uint8Array(hash)
  );

  const [sigs]=await signer.signMessages([signable]);

  const signature=getBase58Encoder().encode(
    sigs[signer.address]
  );

  process.stdout.write(JSON.stringify({
    fingerprint:a.fingerprint,
    solanaTokenTransferApproval:{
      signature,
      nonce:r.nonce,
      expirationTimestamp:r.expirationTimestamp
    }
  }));
}

main().catch(e=>{
  console.error(e.stack||e.message||String(e));
  process.exit(1);
});
'''

    p=subprocess.run(
        [n,"-e",script],
        input=json.dumps({
            "privateKey":PRIVATE_KEY,
            "authorization":auth
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode!=0:
        raise RuntimeError(
            p.stderr.strip() or "Firma Solana fallita"
        )

    try:
        return json.loads(p.stdout)
    except:
        raise RuntimeError(
            "Output firma Solana non valido: "+
            p.stdout[:500]
        )


# ============================================================
# AUTHORIZATIONS
# ============================================================

def sign_authorizations(auths):
    approvals=[]

    for i,a in enumerate(auths):
        r=a.get("request") or {}
        t=r.get("__typename")

        print(
            f"🔐 Authorization {i}: {t}",
            flush=True
        )

        if t=="SolanaTokenTransferAuthorizationRequest":
            approvals.append(sign_solana(a))

        else:
            raise RuntimeError(
                "Authorization non supportata: "+str(t)
            )

    return approvals


# ============================================================
# PREPARE
# ============================================================

def prepare_sale(c,price):
    asset=str(c.get("assetId") or "").strip()

    if not asset:
        print("❌ AssetId mancante",flush=True)
        return None

    # IMPORTANTE:
    # lo schema GraphQL ATTUALE non contiene "type"
    # in prepareOfferInput.
    d=graphql("""
    mutation($input:prepareOfferInput!){
      prepareOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename

            ... on SolanaTokenTransferAuthorizationRequest{
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
        errors{message}
      }
    }""",{
        "input":{
            "sendAssetIds":[asset],
            "receiveAssetIds":[],
            "receiveAmount":{
                "amount":str(price),
                "currency":"EUR"
            },
            "settlementCurrencies":["EUR"],
            "clientMutationId":str(uuid.uuid4())
        }
    })

    result=(
        ((d or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print("❌ prepareOffer: nessun risultato",flush=True)
        return None

    errors=result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(errors,ensure_ascii=False),
            flush=True
        )
        return None

    auths=result.get("authorizations") or []

    if not auths:
        print("❌ Nessuna authorization",flush=True)
        return None

    return auths


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(c,price,approvals):
    asset=str(c.get("assetId") or "").strip()

    d=graphql("""
    mutation($input:createSingleSaleOfferInput!){
      createSingleSaleOffer(input:$input){
        tokenOffer{
          id
          blockchainId
          status
        }
        errors{message}
      }
    }""",{
        "input":{
            "approvals":approvals,
            "assetId":asset,
            "dealId":str(uuid.uuid4()),
            "duration":LISTING_DURATION,
            "receiveAmount":{
                "amount":str(price),
                "currency":"EUR"
            },
            "settlementCurrencies":["EUR"]
        }
    })

    result=(
        ((d or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print("❌ createSingleSaleOffer: nessun risultato",flush=True)
        return False

    errors=result.get("errors") or []

    if errors:
        print(
            "❌ createSingleSaleOffer:",
            json.dumps(errors,ensure_ascii=False),
            flush=True
        )
        return False

    offer=result.get("tokenOffer") or {}

    if not offer.get("id"):
        print("❌ Listing non creato",flush=True)
        return False

    print(
        f"✅ LISTING CREATO {offer['id']} → {eur(price)}",
        flush=True
    )

    return True


# ============================================================
# SELL
# ============================================================

def sell(c,price):
    print(
        f"💰 SELL {label(c)} → {eur(price)}",
        flush=True
    )

    if c.get("sealed"):
        print("🔒 VAULT → BLOCCATA",flush=True)
        return False

    if DRY_RUN:
        print("🟡 DRY_RUN=True → SIMULAZIONE",flush=True)
        return True

    auths=prepare_sale(c,price)

    if not auths:
        return False

    try:
        approvals=sign_authorizations(auths)
    except Exception as e:
        print("❌ Firma:",e,flush=True)
        return False

    return create_sale(c,price,approvals)


# ============================================================
# PROCESS
# ============================================================

def process(c,lineup):
    x=label(c)

    if c.get("sealed"):
        return

    if is_kulenovic(c):
        print(f"🛡️ {x} → KULENOVIC, SKIP",flush=True)
        return

    if c.get("liveSingleSaleOffer"):
        print(f"⏳ {x} → GIÀ IN VENDITA",flush=True)
        return

    ok,reason,value=validate(c,lineup)

    if not ok:
        reject(c,reason,value)
        return

    print(
        f"✅ {x} → VENDIBILE {eur(value)}",
        flush=True
    )

    if sell(c,value):
        print(f"🟢 AutoSell completato",flush=True)
    else:
        print(f"🔴 AutoSell fallito",flush=True)


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO",flush=True)
    print(f"📦 VERSIONE {BOT_VERSION}",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)
    print("🚫 AUTOBUY OFF",flush=True)
    print("🚫 SWAP OFF",flush=True)
    print("🏆 LIMITED ONLY",flush=True)
    print("🔒 VAULT EXCLUDED",flush=True)
    print("🛡️ LINEUP BLOCK",flush=True)
    print("🛡️ KULENOVIC NEVER SELL",flush=True)
    print("💰 €0.32 - €0.70",flush=True)
    print("📊 MIN 5 LIVE LISTINGS",flush=True)
    print("⏱️ LISTING 7 DAYS",flush=True)

    if not check_account():
        print("🛑 AutoSell fermato",flush=True)
        return

    while True:
        try:
            cards=get_gallery()

            if cards is None:
                time.sleep(INTERVAL)
                continue

            lineup=get_lineup()

            if lineup is None:
                print(
                    "🛡️ LINEUP NON VERIFICABILE → "
                    "NESSUNA VENDITA",
                    flush=True
                )
                time.sleep(INTERVAL)
                continue

            print(
                f"🛡️ Lineup: {len(lineup)}",
                flush=True
            )

            for c in cards:
                try:
                    process(c,lineup)
                except Exception as e:
                    print(
                        f"❌ {label(c)}: {e}",
                        flush=True
                    )

                time.sleep(.25)

            print(
                f"😴 Prossimo controllo {INTERVAL}s",
                flush=True
            )

            time.sleep(INTERVAL)

        except Exception as e:
            print("❌ Worker:",e,flush=True)
            time.sleep(INTERVAL)


# ============================================================
# HTTP
# ============================================================

@app.get("/")
def home():
    return jsonify({
        "status":"online",
        "bot":"sorare-autosell",
        "version":BOT_VERSION,
        "dry_run":DRY_RUN,
        "autosell":True,
        "autobuy":False,
        "swap":False,
        "min_price_cents":MIN_PRICE,
        "max_price_cents":MAX_PRICE,
        "min_live_listings":MIN_LIVE_LISTINGS,
        "listing_duration_days":7,
        "kulenovic":"NEVER_SELL",
        "rarity":"LIMITED_ONLY",
        "vault_cards":"EXCLUDED",
        "lineup_unknown_action":"BLOCK_SELL",
        "price_source":"liveSingleSaleOffers",
        "price_match":"PLAYER_RARITY_SEASON",
        "solana_authorization":"ED25519",
        "worker_started":worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status":"ok",
        "bot":"autosell",
        "version":BOT_VERSION,
        "worker_started":worker_started,
        "dry_run":DRY_RUN,
        "autosell":True,
        "autobuy":False,
        "swap":False
    })


def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started=True

        threading.Thread(
            target=worker,
            name="autosell-worker",
            daemon=True
        ).start()


if __name__=="__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT","10000")),
        debug=False
    )
