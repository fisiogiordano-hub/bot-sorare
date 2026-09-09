import os,time,uuid,json,base64,shutil,subprocess,threading,re,requests
from flask import Flask,jsonify

app=Flask(__name__)

# CONFIG
SORARE_URL="https://api.sorare.com/graphql"
COVERAGE_URL="https://sorare.com/coverage"
STATE_FILE="bot_state.json"

TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
STARK=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()
GITHUB_TOKEN=os.getenv("GITHUB_TOKEN","").strip()
GITHUB_REPO=os.getenv("GITHUB_REPO","fisiogiordano-hub/bot-sorare").strip()
GITHUB_BRANCH=os.getenv("GITHUB_BRANCH","main").strip()

DRY_RUN=os.getenv("DRY_RUN","false").lower()=="true"
SWAP_AUTO_ACCEPT=os.getenv("SWAP_AUTO_ACCEPT","false").lower()=="true"

MIN_PRICE=32
MAX_PRICE=70
PAY_PER_CARD=20
MAX_AGE=28
MIN_LIVE_LISTINGS=5
SWAP_MIN=1.20
SWAP_MAX=1.25
INTERVAL=10
TIMEOUT=25
USD_CACHE=300
COVERAGE_CACHE=3600

BOT_VERSION="23.2-AUTOBUY-PENDING-FIX"

KSLUG="sandro-kulenovic-2025-limited-385"
KASSET=("0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
        "6796b6c0ed10ba0a6")

processed=set()
acquired_cards={}
pending_autobuys={}

state_lock=threading.Lock()
github_lock=threading.Lock()
coverage_lock=threading.Lock()
worker_lock=threading.Lock()

worker_started=False
usd_rate=None
usd_time=0
coverage_cache=set()
coverage_time=0
current_user_slug=None


# UTILS
def norm(v): return str(v or "").strip().lower()
def card_name(c): return c.get("name") or c.get("slug") or "Carta"

def card_label(c):
    n=card_name(c); s=c.get("slug")
    return f"{n} [{s}]" if s and s!=n else n

def format_eur(c):
    return "N/D" if c is None else f"€{c/100:.2f}"


# GRAPHQL
def headers():
    if not TOKEN: raise RuntimeError("SORARE_JWT_TOKEN non configurato")
    t=TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN
    h={"Authorization":t,"Content-Type":"application/json",
       "Accept":"application/json","User-Agent":f"Sorare-Bot/{BOT_VERSION}"}
    if AUD:h["JWT-AUD"]=AUD
    return h

def graphql(query,variables=None):
    payload={"query":query,"variables":variables or {}}
    for attempt in range(3):
        try:
            r=requests.post(SORARE_URL,json=payload,headers=headers(),timeout=TIMEOUT)
            print(f"🌐 Sorare HTTP {r.status_code}",flush=True)

            if r.status_code==429:
                try:x=int(r.headers.get("Retry-After",attempt+2))
                except:x=attempt+2
                time.sleep(min(x,15));continue

            if r.status_code!=200:
                print(f"❌ Sorare HTTP {r.status_code}: {r.text[:500]}",flush=True)
                time.sleep(attempt+1);continue

            d=r.json()
            if d.get("errors"):
                print("❌ GraphQL:",json.dumps(d["errors"],ensure_ascii=False)[:3000],flush=True)
            return d
        except Exception as e:
            print(f"❌ GraphQL: {e}",flush=True)
            time.sleep(attempt+1)
    return None


# STATE
def normalize_acquired_card(x):
    if not isinstance(x,dict):return None
    aid=str(x.get("assetId") or x.get("asset_id") or "").strip()
    if not aid:return None
    return {
        "assetId":aid,"slug":x.get("slug"),
        "purchase_price_cents":x.get("purchase_price_cents"),
        "status":x.get("status") or "da_vendere",
        "source":x.get("source") or "unknown",
        "offer_id":x.get("offer_id")
    }

def normalize_pending(x):
    if not isinstance(x,dict):return None
    oid=str(x.get("offer_id") or "").strip()
    if not oid:return None
    return {
        "offer_id":oid,
        "original_offer_id":x.get("original_offer_id"),
        "created_at":x.get("created_at"),
        "cards":x.get("cards") or [],
        "price_per_card":x.get("price_per_card",PAY_PER_CARD),
        "status":x.get("status") or "PENDING"
    }

def build_state():
    with state_lock:
        return {
            "processed_offers":sorted(processed),
            "acquired_cards":list(acquired_cards.values()),
            "pending_autobuys":list(pending_autobuys.values()),
            "updated_at":int(time.time())
        }

def load_state_data(d):
    global processed,acquired_cards,pending_autobuys
    if not isinstance(d,dict):return

    processed={norm(x) for x in d.get("processed_offers",[]) if x}

    for x in d.get("acquired_cards",[]) or []:
        c=normalize_acquired_card(x)
        if c:acquired_cards[norm(c["assetId"])]=c

    for x in d.get("pending_autobuys",[]) or []:
        p=normalize_pending(x)
        if p:pending_autobuys[norm(p["offer_id"])]=p

def load_local_state():
    if not os.path.exists(STATE_FILE):return
    try:
        with open(STATE_FILE,encoding="utf-8") as f:load_state_data(json.load(f))
    except Exception as e:print(f"⚠️ Lettura stato: {e}",flush=True)

def save_local_state():
    try:
        tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(build_state(),f,indent=2,ensure_ascii=False)
        os.replace(tmp,STATE_FILE)
        return True
    except Exception as e:
        print(f"❌ Salvataggio stato: {e}",flush=True)
        return False

def github_headers():
    return None if not GITHUB_TOKEN else {
        "Authorization":f"Bearer {GITHUB_TOKEN}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":f"Sorare-Bot/{BOT_VERSION}"
    }

def github_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"

def load_github_state():
    if not GITHUB_TOKEN:return
    try:
        r=requests.get(github_url(),headers=github_headers(),
                       params={"ref":GITHUB_BRANCH},timeout=TIMEOUT)
        if r.status_code==404:return
        if r.status_code!=200:
            print(f"⚠️ GitHub load HTTP {r.status_code}",flush=True);return
        c=r.json().get("content")
        if not c:return
        d=json.loads(base64.b64decode(c.replace("\n","")).decode())
        with state_lock:load_state_data(d)
        print(f"💾 GitHub: {len(processed)} offerte | {len(acquired_cards)} carte | {len(pending_autobuys)} pending",flush=True)
    except Exception as e:print(f"⚠️ GitHub load: {e}",flush=True)

def save_github_state():
    if not GITHUB_TOKEN:return False
    with github_lock:
        try:
            raw=json.dumps(build_state(),indent=2,ensure_ascii=False)
            enc=base64.b64encode(raw.encode()).decode()
            r=requests.get(github_url(),headers=github_headers(),
                           params={"ref":GITHUB_BRANCH},timeout=TIMEOUT)
            sha=r.json().get("sha") if r.status_code==200 else None
            p={"message":f"Update bot_state.json {int(time.time())}",
               "content":enc,"branch":GITHUB_BRANCH}
            if sha:p["sha"]=sha
            r=requests.put(github_url(),headers=github_headers(),
                           json=p,timeout=TIMEOUT)
            if r.status_code not in (200,201):
                print(f"❌ GitHub save HTTP {r.status_code}",flush=True);return False
            print("💾 bot_state.json salvato su GitHub",flush=True)
            return True
        except Exception as e:
            print(f"❌ GitHub save: {e}",flush=True);return False

def load_state():
    load_local_state();load_github_state();save_local_state()
    print(f"💾 Stato: {len(processed)} offerte | {len(acquired_cards)} carte | {len(pending_autobuys)} pending",flush=True)

def persist_state():
    save_local_state()
    if GITHUB_TOKEN:save_github_state()

def mark_done(oid):
    if not oid:return
    with state_lock:processed.add(norm(oid))
    persist_state()


# ACQUIRED
def persist_acquired_card(card,price,source,offer_id):
    aid=str(card.get("assetId") or "").strip()
    if not aid:return

    item={"assetId":aid,"slug":card.get("slug"),
          "purchase_price_cents":price,"status":"da_vendere",
          "source":source,"offer_id":offer_id}

    with state_lock:
        old=acquired_cards.get(norm(aid))
        if old:old.update({k:v for k,v in item.items() if v is not None})
        else:acquired_cards[norm(aid)]=item

    print(f"💾 CARTA ACQUISITA: {card_label(card)} | {source}",flush=True)
    persist_state()


# PENDING
def add_pending_autobuy(oid,original,cards):
    item={
        "offer_id":oid,
        "original_offer_id":original,
        "created_at":int(time.time()),
        "cards":[{"assetId":c.get("assetId"),"slug":c.get("slug"),"name":c.get("name")} for c in cards],
        "price_per_card":PAY_PER_CARD,
        "status":"PENDING"
    }
    with state_lock:pending_autobuys[norm(oid)]=item
    persist_state()
    print(f"💾 AutoBuy pending salvato: {oid}",flush=True)

def remove_pending(oid):
    with state_lock:pending_autobuys.pop(norm(oid),None)
    persist_state()


# ACCOUNT
def check_account():
    global current_user_slug
    d=graphql("query{currentUser{slug nickname starkKey}}")
    u=(((d or {}).get("data") or {}).get("currentUser"))
    if not u:
        print("❌ Account Sorare non verificato",flush=True);return False
    current_user_slug=u.get("slug")
    print(f"✅ Sorare: {u.get('nickname') or current_user_slug}",flush=True)
    print("🔐 Stark key account: "+("PRESENTE" if u.get("starkKey") else "NON DISPONIBILE"),flush=True)
    return True


# RECEIVED OFFERS
def get_received_offers():
    d=graphql("""
    query{
      currentUser{
        pendingTokenOffersReceived(first:50){
          nodes{
            id blockchainId status
            sender{... on User{slug nickname}}
            senderSide{amounts{eurCents usdCents referenceCurrency wei} anyCards{assetId slug collection}}
            receiverSide{amounts{eurCents usdCents referenceCurrency wei} anyCards{assetId slug collection}}
          }
        }
      }
    }""")
    u=(((d or {}).get("data") or {}).get("currentUser") or {})
    return ((u.get("pendingTokenOffersReceived") or {}).get("nodes") or [])


# SENT/PENDING AUTOBUY
# FIX: niente query { offer(...) }, che non esiste più.
def get_sent_offers():
    d=graphql("""
    query{
      currentUser{
        pendingTokenOffersSent(first:50){
          nodes{
            id blockchainId status type createdAt acceptedAt cancelledAt transactionDate
            sender{... on User{slug}}
            receiver{... on User{slug}}
            actualReceiver{... on User{slug}}
            senderSide{anyCards{assetId slug name}}
            receiverSide{anyCards{assetId slug name}}
          }
        }
      }
    }""")
    u=(((d or {}).get("data") or {}).get("currentUser") or {})
    return ((u.get("pendingTokenOffersSent") or {}).get("nodes") or [])

def find_sent_offer(oid):
    wanted=norm(oid)
    for o in get_sent_offers():
        if norm(o.get("id"))==wanted:return o
    return None


# CARDS
def card_details(ids):
    ids=list(dict.fromkeys(str(x).strip() for x in ids if x))
    if not ids:return []

    d=graphql("""
    query($assetIds:[String!]!){
      anyCards(assetIds:$assetIds){
        assetId slug name rarityTyped seasonYear
        user{slug}
        tokenOwner{user{slug}}
        anyPlayer{
          slug displayName age
          activeClub{
            slug name
            activeCompetitions{slug}
          }
        }
      }
    }""",{"assetIds":ids})

    if not d or d.get("errors"):return []
    return ((d.get("data") or {}).get("anyCards") or [])

def card_owned_by_me(c):
    if not current_user_slug:return False
    wanted=norm(current_user_slug)
    return (
        norm((c.get("user") or {}).get("slug"))==wanted
        or norm((((c.get("tokenOwner") or {}).get("user") or {}).get("slug")))==wanted
    )


# PRICES
def usd_eur():
    global usd_rate,usd_time
    now=time.time()
    if usd_rate and now-usd_time<USD_CACHE:return usd_rate
    try:
        r=requests.get("https://api.frankfurter.app/latest",
                        params={"from":"USD","to":"EUR"},timeout=10)
        if r.status_code!=200:return None
        rate=float((r.json().get("rates") or {}).get("EUR"))
        if rate<=0:return None
        usd_rate,usd_time=rate,now
        return rate
    except Exception as e:
        print(f"❌ USD/EUR: {e}",flush=True);return None

def price_eur(a):
    if not isinstance(a,dict):return None
    try:
        x=int(a.get("eurCents"))
        if x>0:return x
    except:pass
    try:x=float(a.get("usdCents"))
    except:x=0
    rate=usd_eur()
    return int(round(x*rate)) if x>0 and rate else None

def live_floor(card):
    p=card.get("anyPlayer") or {}
    ps=norm(p.get("slug"))
    rarity=norm(card.get("rarityTyped"))
    try:season=int(card.get("seasonYear"))
    except:return None
    if not ps or not rarity:return None

    d=graphql("""
    query($playerSlug:String,$first:Int){
      tokens{
        liveSingleSaleOffers(playerSlug:$playerSlug,first:$first){
          nodes{
            senderSide{anyCards{assetId rarityTyped seasonYear anyPlayer{slug}}}
            receiverSide{amounts{eurCents usdCents referenceCurrency wei}}
          }
        }
      }
    }""",{"playerSlug":ps,"first":50})

    if not d or d.get("errors"):return None
    offers=((((d.get("data") or {}).get("tokens") or {}).get("liveSingleSaleOffers") or {}).get("nodes") or [])
    prices=[]

    for o in offers:
        for c in ((o.get("senderSide") or {}).get("anyCards") or []):
            try:s=int(c.get("seasonYear"))
            except:continue
            if (
                norm((c.get("anyPlayer") or {}).get("slug"))==ps
                and norm(c.get("rarityTyped"))==rarity
                and s==season
            ):
                x=price_eur((o.get("receiverSide") or {}).get("amounts") or {})
                if x is not None:prices.append(x)
                break

    return min(prices) if len(prices)>=MIN_LIVE_LISTINGS else None


# COVERAGE
def load_coverage(force=False):
    global coverage_cache,coverage_time
    now=time.time()

    with coverage_lock:
        if not force and coverage_cache and now-coverage_time<COVERAGE_CACHE:
            return set(coverage_cache)
        cached=set(coverage_cache)

    try:
        r=requests.get(COVERAGE_URL,timeout=TIMEOUT,
                       headers={"User-Agent":f"Sorare-Bot/{BOT_VERSION}"})
        if r.status_code!=200:return cached

        result={norm(x) for x in re.findall(
            r'/football/leagues/([^"\'?#<>\s]+)',r.text,re.I)}
        if not result:return cached

        with coverage_lock:
            coverage_cache=result;coverage_time=time.time()

        print(f"🌐 Coverage: {len(result)} competizioni",flush=True)
        return set(result)
    except Exception as e:
        print(f"⚠️ Coverage: {e}",flush=True);return cached


# VALIDATION
def is_kulenovic(c):
    wanted={norm(KSLUG),norm(KASSET)}
    x=os.getenv("KULENOVIC_ID","").strip()
    if x:wanted.add(norm(x))
    return norm(c.get("assetId")) in wanted or norm(c.get("slug")) in wanted

def coverage_info(c):
    p=c.get("anyPlayer") or {}
    club=p.get("activeClub") or {}
    active=[norm(x.get("slug")) for x in (club.get("activeCompetitions") or []) if isinstance(x,dict) and x.get("slug")]
    coverage=load_coverage()
    covered=[x for x in active if x in coverage]
    return bool(covered),active,covered

def validate_card(c):
    p=c.get("anyPlayer") or {}
    try:age=int(p.get("age"))
    except:return False,{"code":"AGE_UNKNOWN"}
    if age>=MAX_AGE:return False,{"code":"AGE","age":age}

    rarity=norm(c.get("rarityTyped")).upper()
    if rarity!="LIMITED":return False,{"code":"RARITY","rarity":rarity}

    floor=live_floor(c)
    if floor is None:return False,{"code":"PRICE_UNKNOWN"}
    if floor<MIN_PRICE:return False,{"code":"PRICE_LOW","floor":floor}
    if floor>MAX_PRICE:return False,{"code":"PRICE_HIGH","floor":floor}

    covered,active,covered_comp=coverage_info(c)
    if not covered:return False,{"code":"COVERAGE","active":active,"covered":covered_comp}

    return True,{"floor":floor,"age":age,"rarity":rarity,"covered":covered_comp}

def print_rejection(c,info,ctx):
    code=info.get("code") if info else "UNKNOWN"
    msg={
        "AGE":f"Età {info.get('age')} >= {MAX_AGE}",
        "AGE_UNKNOWN":"Età non disponibile",
        "RARITY":f"Rarità {info.get('rarity')}",
        "PRICE_UNKNOWN":f"Floor live non disponibile o meno di {MIN_LIVE_LISTINGS} inserzioni",
        "PRICE_LOW":f"Floor {format_eur(info.get('floor'))} < {format_eur(MIN_PRICE)}",
        "PRICE_HIGH":f"Floor {format_eur(info.get('floor'))} > {format_eur(MAX_PRICE)}",
        "COVERAGE":"Nessuna competizione coperta"
    }.get(code,code)
    print(f"🚫 {ctx}: {card_label(c)} → {msg}",flush=True)

def already_processed(oid):
    with state_lock:return norm(oid) in processed


# REJECT
def reject_offer(o):
    bid=norm(o.get("blockchainId"))
    if not bid:return False
    if DRY_RUN:
        print("🟡 DRY RUN: reject simulato",flush=True);return True

    d=graphql("""
    mutation($input:rejectOfferInput!){
      rejectOffer(input:$input){
        tokenOffer{id status}
        errors{message}
      }
    }""",{"input":{"blockchainId":bid,"clientMutationId":str(uuid.uuid4())}})

    r=(((d or {}).get("data") or {}).get("rejectOffer"))
    if not r:return False
    e=r.get("errors") or []
    if e:
        print("❌ Reject:",json.dumps(e,ensure_ascii=False),flush=True);return False
    print("✅ Offerta originale rifiutata",flush=True)
    return True


# SIGN
def sign_authorizations(auth):
    node=shutil.which("node") or shutil.which("nodejs")
    if not node:raise RuntimeError("Node.js non disponibile")
    if not STARK:raise RuntimeError("SORARE_STARK_PRIVATE_KEY non configurata")

    script=r'''
const fs=require("fs");
const {signAuthorizationRequest}=require("@sorare/crypto");
const input=JSON.parse(fs.readFileSync(0,"utf8"));

function sign(a){
 const r=a.request;
 if(!r)throw new Error("AuthorizationRequest mancante");
 if(r.__typename==="StarkexTransferAuthorizationRequest"&&r.amount!=null)r.amount=BigInt(r.amount);
 const signature=signAuthorizationRequest(input.privateKey,r);
 if(r.__typename==="StarkexTransferAuthorizationRequest")
  return {fingerprint:a.fingerprint,starkexTransferApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature}};
 if(r.__typename==="StarkexLimitOrderAuthorizationRequest")
  return {fingerprint:a.fingerprint,starkexLimitOrderApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature}};
 if(r.__typename==="MangopayWalletTransferAuthorizationRequest")
  return {fingerprint:a.fingerprint,mangopayWalletTransferApproval:{nonce:r.nonce,signature}};
 throw new Error("Authorization non supportata: "+r.__typename);
}
process.stdout.write(JSON.stringify(input.authorizations.map(sign)));
'''

    p=subprocess.run([node,"-e",script],
        input=json.dumps({"privateKey":STARK,"authorizations":auth}),
        text=True,capture_output=True,timeout=TIMEOUT)

    if p.returncode!=0:raise RuntimeError(p.stderr.strip() or "Firma fallita")
    return json.loads(p.stdout)


# AUTOBUY CREATE
def counter_offer(offer,cards):
    receiver=norm((offer.get("sender") or {}).get("slug"))
    ids=[str(c["assetId"]).strip() for c in cards if c.get("assetId")]
    if not receiver or not ids:return None

    amount=len(ids)*PAY_PER_CARD
    print(f"🟢 AUTOBUY: creo controproposta {len(ids)} carta/e → €{amount/100:.2f}",flush=True)

    if DRY_RUN:
        x="DRYRUN:"+str(uuid.uuid4())
        print(f"🟡 DRY RUN: {x}",flush=True)
        return x

    # FIX: settlementCurrencies RIMOSSO.
    d=graphql("""
    mutation($input:prepareOfferInput!){
      prepareOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename
            ... on StarkexTransferAuthorizationRequest{
              amount condition expirationTimestamp nonce
              receiverPublicKey receiverVaultId senderVaultId token
              feeInfoUser{feeLimit sourceVaultId tokenId}
            }
            ... on StarkexLimitOrderAuthorizationRequest{
              vaultIdSell vaultIdBuy amountSell amountBuy
              tokenSell tokenBuy nonce expirationTimestamp
              feeInfo{feeLimit tokenId sourceVaultId}
            }
            ... on MangopayWalletTransferAuthorizationRequest{
              nonce amount currency operationHash mangopayWalletId
            }
          }
        }
        errors{message}
      }
    }""",{
        "input":{
            "receiveAssetIds":ids,
            "sendAssetIds":[],
            "sendAmount":{"amount":str(amount),"currency":"EUR"},
            "receiverSlug":receiver,
            "clientMutationId":str(uuid.uuid4())
        }
    })

    r=(((d or {}).get("data") or {}).get("prepareOffer"))
    if not r:return None

    e=r.get("errors") or []
    if e:
        print("❌ prepareOffer:",json.dumps(e,ensure_ascii=False),flush=True);return None

    auth=r.get("authorizations") or []
    if not auth:
        print("❌ prepareOffer: nessuna authorization",flush=True);return None

    try:approvals=sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma AutoBuy: {e}",flush=True);return None

    d=graphql("""
    mutation($input:createDirectOfferInput!){
      createDirectOffer(input:$input){
        tokenOffer{id blockchainId status type}
        errors{message}
      }
    }""",{
        "input":{
            "receiveAssetIds":ids,
            "sendAssetIds":[],
            "sendAmount":{"amount":str(amount),"currency":"EUR"},
            "receiverSlug":receiver,
            "clientMutationId":str(uuid.uuid4()),
            "approvals":approvals,
            "dealId":str(uuid.uuid4())
        }
    })

    r=(((d or {}).get("data") or {}).get("createDirectOffer"))
    if not r:return None

    e=r.get("errors") or []
    if e:
        print("❌ createDirectOffer:",json.dumps(e,ensure_ascii=False),flush=True);return None

    oid=(r.get("tokenOffer") or {}).get("id")
    if not oid:
        print("❌ createDirectOffer: ID mancante",flush=True);return None

    print(f"✅ CONTROPROPOSTA INVIATA: {oid}",flush=True)
    return oid


# AUTOBUY
def process_autobuy(o):
    original=norm(o.get("id"))
    if not original or already_processed(original):return

    receiver_cards=((o.get("receiverSide") or {}).get("anyCards") or [])
    if not any(is_kulenovic(c) for c in receiver_cards):return

    sender_cards=((o.get("senderSide") or {}).get("anyCards") or [])
    ids=[c.get("assetId") for c in sender_cards if c.get("assetId")]

    if not ids:
        if reject_offer(o):mark_done(original)
        return

    print(f"\n📨 AUTOBUY {original}",flush=True)

    details=card_details(ids)
    if len(details)!=len(ids):
        print("❌ AUTOBUY: impossibile verificare tutte le carte",flush=True)
        if reject_offer(o):mark_done(original)
        return

    valid=[]
    for c in details:
        ok,info=validate_card(c)
        if ok:
            print(f"✅ AUTOBUY - Carta valida: {card_label(c)}",flush=True)
            print(f"   └─ Floor live: {format_eur(info['floor'])}",flush=True)
            valid.append(c)
        else:print_rejection(c,info,"AUTOBUY")

    if not valid:
        print("🔴 AUTOBUY: nessuna carta valida → RIFIUTO",flush=True)
        if reject_offer(o):mark_done(original)
        return

    new_id=counter_offer(o,valid)
    if not new_id:
        print("❌ AUTOBUY: controproposta non creata",flush=True);return

    add_pending_autobuy(new_id,original,valid)

    if reject_offer(o):mark_done(original)

    print(f"⏳ AUTOBUY IN ATTESA: {new_id}",flush=True)


# CHECK PENDING
def check_pending_autobuys():
    with state_lock:pending=list(pending_autobuys.values())
    if not pending:return

    print(f"🔎 Controllo {len(pending)} AutoBuy pending...",flush=True)

    # Recuperiamo tutte le offerte inviate in una sola query.
    sent=get_sent_offers()

    for item in pending:
        oid=item.get("offer_id")
        if not oid:continue

        try:
            offer=next((x for x in sent if norm(x.get("id"))==norm(oid)),None)

            if not offer:
                print(f"⚠️ AutoBuy {oid}: non presente nelle pending sent",flush=True)
                continue

            status=norm(offer.get("status")).upper()
            print(f"📦 AUTOBUY {oid} → {status}",flush=True)

            if status in {"CANCELLED","REJECTED","ENDED","SETTLEMENT_FAILED"}:
                print(f"❌ AUTOBUY concluso senza acquisto: {oid} → {status}",flush=True)
                remove_pending(oid)
                continue

            if status not in {"ACCEPTED","SETTLEMENT_PUBLISHED"}:continue

            cards=item.get("cards") or []
            ids=[c.get("assetId") for c in cards if c.get("assetId")]
            if not ids:
                remove_pending(oid);continue

            details=card_details(ids)
            if len(details)!=len(ids):
                print(f"⏳ AUTOBUY {oid}: carte non verificabili",flush=True);continue

            if not all(card_owned_by_me(c) for c in details):
                print(f"⏳ AUTOBUY {oid}: offerta {status}, ma carta non ancora nostra",flush=True)
                continue

            for c in details:
                persist_acquired_card(c,PAY_PER_CARD,"autobuy",oid)

            print(f"🎉 AUTOBUY COMPLETATO: {oid}",flush=True)
            remove_pending(oid)

        except Exception as e:
            print(f"❌ Controllo AutoBuy {oid}: {e}",flush=True)


# SWAP
def get_exchange_rate_id():
    d=graphql("query{config{exchangeRate{id}}}")
    return ((((d or {}).get("data") or {}).get("config") or {}).get("exchangeRate") or {}).get("id")

def prepare_accept(oid):
    rate=get_exchange_rate_id()
    if not rate:return None,None

    d=graphql("""
    mutation($input:prepareAcceptOfferInput!){
      prepareAcceptOffer(input:$input){
        authorizations{
          fingerprint
          request{
            __typename
            ... on StarkexTransferAuthorizationRequest{
              amount condition expirationTimestamp nonce receiverPublicKey
              receiverVaultId senderVaultId token
              feeInfoUser{feeLimit sourceVaultId tokenId}
            }
            ... on StarkexLimitOrderAuthorizationRequest{
              vaultIdSell vaultIdBuy amountSell amountBuy tokenSell tokenBuy
              nonce expirationTimestamp feeInfo{feeLimit tokenId sourceVaultId}
            }
            ... on MangopayWalletTransferAuthorizationRequest{
              nonce amount currency operationHash mangopayWalletId
            }
          }
        }
        errors{message}
      }
    }""",{"input":{
        "offerId":oid,
        "settlementInfo":{
            "currency":"WEI",
            "paymentMethod":"WALLET",
            "exchangeRateId":rate
        }
    }})

    r=(((d or {}).get("data") or {}).get("prepareAcceptOffer"))
    if not r:return None,None

    e=r.get("errors") or []
    if e:
        print("❌ prepareAcceptOffer:",json.dumps(e,ensure_ascii=False),flush=True)
        return None,None

    return r.get("authorizations") or [],rate

def accept_offer(o):
    oid=norm(o.get("id"))
    if DRY_RUN:
        print("🟡 DRY RUN: ACCEPT simulato",flush=True);return True

    auth,rate=prepare_accept(oid)
    if not auth:return False

    try:approvals=sign_authorizations(auth)
    except Exception as e:
        print(f"❌ Firma ACCEPT: {e}",flush=True);return False

    d=graphql("""
    mutation($input:acceptOfferInput!){
      acceptOffer(input:$input){
        tokenOffer{id status}
        errors{message}
      }
    }""",{"input":{
        "approvals":approvals,
        "offerId":oid,
        "settlementInfo":{
            "currency":"WEI",
            "paymentMethod":"WALLET",
            "exchangeRateId":rate
        },
        "clientMutationId":str(uuid.uuid4())
    }})

    r=(((d or {}).get("data") or {}).get("acceptOffer"))
    if not r:return False

    e=r.get("errors") or []
    if e:
        print("❌ acceptOffer:",json.dumps(e,ensure_ascii=False),flush=True);return False

    print("✅ SWAP ACCETTATO",flush=True)
    return True


def process_swap(o):
    oid=norm(o.get("id"))
    if not oid or already_processed(oid):return

    sender=((o.get("senderSide") or {}).get("anyCards") or [])
    receiver=((o.get("receiverSide") or {}).get("anyCards") or [])
    if not sender or not receiver:return

    give_ids=[c.get("assetId") for c in receiver if c.get("assetId")]
    receive_ids=[c.get("assetId") for c in sender if c.get("assetId")]

    if not give_ids or not receive_ids:
        mark_done(oid);return

    print(f"\n🔄 SWAP {oid}",flush=True)

    give=card_details(give_ids)
    receive=card_details(receive_ids)

    if len(give)!=len(give_ids) or len(receive)!=len(receive_ids):
        if reject_offer(o):mark_done(oid)
        return

    if any(is_kulenovic(c) for c in give):
        print("🔒 SWAP RIFIUTATO: KULENOVIC NON È CEDIBILE",flush=True)
        if reject_offer(o):mark_done(oid)
        return

    total_given=0
    for c in give:
        floor=live_floor(c)
        if floor is None:
            print_rejection(c,{"code":"PRICE_UNKNOWN"},"SWAP - CARTA CEDUTA")
            if reject_offer(o):mark_done(oid)
            return
        total_given+=floor
        print(f"📤 SWAP ceduta: {card_label(c)} → {format_eur(floor)}",flush=True)

    total_received=0
    for c in receive:
        ok,info=validate_card(c)
        if not ok:
            print_rejection(c,info,"SWAP - CARTA RICEVUTA")
            if reject_offer(o):mark_done(oid)
            return
        total_received+=info["floor"]
        print(f"📥 SWAP ricevuta: {card_label(c)} → {format_eur(info['floor'])}",flush=True)

    total_received+=price_eur((o.get("senderSide") or {}).get("amounts") or {}) or 0

    minimum=int(round(total_given*SWAP_MIN))
    maximum=int(round(total_given*SWAP_MAX))

    print(f"📤 Ceduto: {format_eur(total_given)}",flush=True)
    print(f"📥 Ricevuto: {format_eur(total_received)}",flush=True)
    print(f"🎯 Range: {format_eur(minimum)} - {format_eur(maximum)}",flush=True)

    if total_received<minimum or total_received>maximum:
        if reject_offer(o):mark_done(oid)
        return

    if not SWAP_AUTO_ACCEPT:
        print("🛑 SWAP_AUTO_ACCEPT=False",flush=True)
        mark_done(oid);return

    if accept_offer(o):
        for c in receive:
            floor=live_floor(c)
            persist_acquired_card(c,floor,"swap",oid)
        mark_done(oid)


# DISPATCH
def process_offer(o):
    receiver=((o.get("receiverSide") or {}).get("anyCards") or [])
    sender=((o.get("senderSide") or {}).get("anyCards") or [])

    if any(is_kulenovic(c) for c in receiver):
        process_autobuy(o)
    elif sender and receiver:
        process_swap(o)


# WORKER
def worker():
    print("🤖 BOT AVVIATO",flush=True)
    print(f"📦 VERSIONE: {BOT_VERSION}",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)
    print(f"💰 AutoBuy: €{PAY_PER_CARD/100:.2f}/carta",flush=True)
    print(f"📊 AutoBuy floor: €{MIN_PRICE/100:.2f} - €{MAX_PRICE/100:.2f}",flush=True)
    print(f"🎂 Età: < {MAX_AGE}",flush=True)
    print(f"📊 Inserzioni minime: {MIN_LIVE_LISTINGS}",flush=True)
    print("🔄 SWAP: +20% / +25%",flush=True)
    print("💶 SWAP CASH: solo cash già offerto",flush=True)
    print("🚫 SWAP NON aggiunge cash",flush=True)
    print("🔒 KULENOVIC: MAI CEDIBILE",flush=True)
    print("🎯 KULENOVIC RICHIESTO → SEMPRE AUTOBUY",flush=True)

    load_state()
    coverage=load_coverage(force=True)

    if not coverage:
        print("❌ Coverage non disponibile → bot fermato",flush=True);return

    print(f"🏆 Competizioni Football coperte: {len(coverage)}",flush=True)

    if not check_account():return

    while True:
        try:
            check_pending_autobuys()

            offers=get_received_offers()
            print(f"📨 Offerte ricevute pendenti: {len(offers)}",flush=True)

            for o in offers:
                try:process_offer(o)
                except Exception as e:print(f"❌ Errore offerta: {e}",flush=True)

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ Worker: {e}",flush=True)
            time.sleep(INTERVAL)


def start_worker():
    global worker_started
    with worker_lock:
        if worker_started:return
        worker_started=True
        threading.Thread(target=worker,name="sorare-worker",daemon=True).start()
        print("✅ Thread Sorare avviato.",flush=True)


# FLASK
@app.get("/")
def home():
    with coverage_lock:covered=len(coverage_cache)
    with state_lock:
        p=len(processed);a=len(acquired_cards);pending=len(pending_autobuys)

    return jsonify({
        "status":"online","bot":"sorare","version":BOT_VERSION,
        "dry_run":DRY_RUN,
        "autobuy":{
            "price_cents":PAY_PER_CARD,
            "min_floor_cents":MIN_PRICE,
            "max_floor_cents":MAX_PRICE,
            "max_age":MAX_AGE,
            "min_live_listings":MIN_LIVE_LISTINGS
        },
        "swap":{
            "auto_accept":SWAP_AUTO_ACCEPT,
            "min_multiplier":SWAP_MIN,
            "max_multiplier":SWAP_MAX
        },
        "kulenovic":"NEVER_CEDIBLE",
        "processed_offers":p,
        "acquired_cards":a,
        "pending_autobuys":pending,
        "github_state":bool(GITHUB_TOKEN),
        "covered_competitions":covered
    })

@app.get("/health")
def health():
    with state_lock:
        return jsonify({
            "status":"ok",
            "bot":"running",
            "version":BOT_VERSION,
            "worker_started":worker_started,
            "processed_offers":len(processed),
            "acquired_cards":len(acquired_cards),
            "pending_autobuys":len(pending_autobuys),
            "dry_run":DRY_RUN,
            "swap_auto_accept":SWAP_AUTO_ACCEPT
        })


if __name__=="__main__":
    start_worker()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
