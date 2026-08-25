import os,time,uuid,json,shutil,subprocess,threading,requests
from flask import Flask,jsonify

app=Flask(__name__)
URL="https://api.sorare.com/graphql"
TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
STARK=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()
KID=os.getenv("KULENOVIC_ID","").strip()
DRY_RUN=os.getenv("DRY_RUN","false").lower()=="true"

MIN_PRICE,MAX_PRICE=30,80
PAY_PER_CARD,MAX_AGE=20,28
INTERVAL,TIMEOUT=10,30

KSLUG="sandro-kulenovic-2025-limited-385"
KASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

processed=set()
state_lock=threading.Lock()
_worker_started=False
_worker_lock=threading.Lock()


def slug(v):
    v=str(v or "").strip().lower()
    for a,b in [("_","-"),(" ","-"),("’",""),("'","")]: v=v.replace(a,b)
    while "--" in v: v=v.replace("--","-")
    return v


def auth_headers():
    if not TOKEN: raise RuntimeError("SORARE_JWT_TOKEN non configurato")
    token=TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer "+TOKEN
    h={"Authorization":token,"Content-Type":"application/json","Accept":"application/json","User-Agent":"Sorare-Bot/14.1"}
    if AUD: h["JWT-AUD"]=AUD
    return h


def graphql(query,variables=None):
    payload={"query":query,"variables":variables or {}}
    for attempt in range(1,4):
        try:
            r=requests.post(URL,json=payload,headers=auth_headers(),timeout=TIMEOUT)
            print(f"🌐 Sorare HTTP {r.status_code}",flush=True)

            if r.status_code==429:
                try: wait=int(r.headers.get("Retry-After",attempt*3))
                except: wait=attempt*3
                print(f"⏳ Rate limit: {wait}s",flush=True)
                time.sleep(wait);continue

            if r.status_code!=200:
                print(f"❌ HTTP {r.status_code}: {r.text[:1000]}",flush=True)
                time.sleep(attempt);continue

            try: data=r.json()
            except ValueError:
                print("❌ JSON Sorare non valido",flush=True);return None

            if data.get("errors"):
                print("❌ GraphQL ERROR:",flush=True)
                for e in data["errors"]: print(json.dumps(e,ensure_ascii=False),flush=True)
            return data

        except requests.RequestException as e:
            print(f"❌ HTTP: {e}",flush=True);time.sleep(attempt)
        except Exception as e:
            print(f"❌ GraphQL: {e}",flush=True);return None
    return None


def check_account():
    d=graphql("""query{currentUser{slug nickname starkKey}}""")
    u=((d or {}).get("data") or {}).get("currentUser")
    if not u:
        print("❌ Account Sorare non verificato",flush=True);return False
    print(f"✅ Sorare: {u.get('nickname') or u.get('slug')}",flush=True)
    return True


def get_exchange_rate_id():
    d=graphql("""query{config{exchangeRate{id}}}""")
    try:
        return d["data"]["config"]["exchangeRate"]["id"]
    except (TypeError,KeyError):
        print("❌ exchangeRateId non disponibile",flush=True)
        return None


def get_offers():
    d=graphql("""
    query{
      currentUser{
        pendingTokenOffersReceived(first:50){
          nodes{
            id blockchainId status
            sender{... on User{slug nickname}}
            senderSide{anyCards{assetId slug collection}}
            receiverSide{anyCards{assetId slug collection}}
          }
        }
      }
    }""")
    u=((d or {}).get("data") or {}).get("currentUser") or {}
    return (u.get("pendingTokenOffersReceived") or {}).get("nodes") or []


def card_details(ids):
    ids=list(dict.fromkeys(str(x).strip() for x in ids if x))
    if not ids:return []
    d=graphql("""
    query Cards($assetIds:[String!]){
      anyCards(assetIds:$assetIds){
        assetId slug name rarityTyped
        anyPlayer{
          displayName age
          activeClub{slug name activeCompetitions{slug}}
        }
        lowestPriceCard{
          liveSingleSaleOffer{receiverSide{amounts{eurCents}}}
          publicMinPrices{eurCents}
        }
        lowestPriceCardAnySeason{
          liveSingleSaleOffer{receiverSide{amounts{eurCents}}}
          publicMinPrices{eurCents}
        }
      }
    }""",{"assetIds":ids})
    return ((d or {}).get("data") or {}).get("anyCards") or []


def card_price(card):
    values=[]
    for key in ("lowestPriceCard","lowestPriceCardAnySeason"):
        s=card.get(key) or {}
        try:
            x=((s.get("liveSingleSaleOffer") or {}).get("receiverSide") or {}).get("amounts",{}).get("eurCents")
            if x: values.append(int(x))
        except: pass
        p=s.get("publicMinPrices") or []
        if isinstance(p,dict):p=[p]
        for x in p:
            try:
                v=int(x.get("eurCents"))
                if v>0:values.append(v)
            except:pass
    return min(values) if values else None


def is_kulenovic(card):
    wanted={KSLUG.lower(),KASSET.lower()}
    if KID:wanted.add(KID.lower())
    return str(card.get("assetId") or "").lower() in wanted or str(card.get("slug") or "").lower() in wanted


def get_competitions(card):
    club=((card.get("anyPlayer") or {}).get("activeClub"))
    if not isinstance(club,dict):return []
    return list(dict.fromkeys(slug(c.get("slug")) for c in club.get("activeCompetitions") or [] if isinstance(c,dict) and slug(c.get("slug"))))


def valid_card(card):
    name=card.get("name") or card.get("slug") or "Carta"
    rarity=str(card.get("rarityTyped") or "").upper()
    player=card.get("anyPlayer") or {}
    try: age=int(player.get("age"))
    except:
        print(f"   📄 {name}\n      ❌ Età non disponibile",flush=True);return False

    price=card_price(card)
    print(f"   📄 {name}\n      🎂 Età: {age} anni",flush=True)

    if age>=MAX_AGE:
        print(f"      ❌ Età troppo alta (limite: < {MAX_AGE})",flush=True);return False
    if price is None:
        print("      ❌ Prezzo non disponibile",flush=True);return False

    print(f"      💰 Floor €{price/100:.2f}",flush=True)
    if not MIN_PRICE<=price<=MAX_PRICE:
        print("      ❌ Prezzo fuori range",flush=True);return False
    if rarity!="LIMITED":
        print(f"      ❌ Rarità: {rarity}",flush=True);return False

    club=player.get("activeClub")
    if not isinstance(club,dict):
        print("      ❌ Nessuna squadra",flush=True);return False

    comps=get_competitions(card)
    print(f"      🏟️ Squadra: {club.get('name') or club.get('slug') or 'Sconosciuta'}",flush=True)
    if not comps:
        print("      ❌ Nessuna activeCompetition",flush=True);return False

    print("      🏆 Competizioni Sorare:",flush=True)
    for c in comps:print(f"         🆕 {c} ({c})",flush=True)
    print("      ✅ COMPETIZIONE COPERTA",flush=True)
    print(f"      ✅ VALIDATA | {age} anni | €{price/100:.2f} | {', '.join(comps)}",flush=True)
    return True


def reject_offer(offer):
    bid=str(offer.get("blockchainId") or "").strip()
    if not bid:
        print("❌ blockchainId mancante",flush=True);return False
    if DRY_RUN:
        print("🟡 DRY RUN: rifiuto simulato",flush=True);return True

    d=graphql("""
    mutation Reject($input:rejectOfferInput!){
      rejectOffer(input:$input){
        tokenOffer{id status}
        errors{message}
      }
    }""",{"input":{"blockchainId":bid,"clientMutationId":str(uuid.uuid4())}})
    r=((d or {}).get("data") or {}).get("rejectOffer")
    if not r:
        print("❌ Risposta rejectOffer vuota",flush=True);return False
    if r.get("errors"):
        for e in r["errors"]:print(f"❌ Reject: {e.get('message','Errore')}",flush=True)
        return False
    print("✅ Offerta originale rifiutata",flush=True);return True


def sign_authorizations(auths):
    node=shutil.which("node") or shutil.which("nodejs")
    if not node:raise RuntimeError("Node.js non disponibile")

    script=r'''
const fs=require("fs"),{signAuthorizationRequest}=require("@sorare/crypto");
const i=JSON.parse(fs.readFileSync(0,"utf8"));
function build(a){
 const r=a.request;
 if(!r)throw new Error("AuthorizationRequest mancante");
 if(r.__typename==="StarkexTransferAuthorizationRequest"&&r.amount!=null)r.amount=BigInt(r.amount);
 const signature=signAuthorizationRequest(i.privateKey,r);
 if(r.__typename==="StarkexTransferAuthorizationRequest")
  return {fingerprint:a.fingerprint,starkexTransferApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature}};
 if(r.__typename==="StarkexLimitOrderAuthorizationRequest")
  return {fingerprint:a.fingerprint,starkexLimitOrderApproval:{nonce:r.nonce,expirationTimestamp:r.expirationTimestamp,signature}};
 if(r.__typename==="MangopayWalletTransferAuthorizationRequest")
  return {fingerprint:a.fingerprint,mangopayWalletTransferApproval:{nonce:r.nonce,signature}};
 throw new Error("Authorization non supportata: "+r.__typename);
}
process.stdout.write(JSON.stringify(i.authorizations.map(build)));
'''

    p=subprocess.run(
        [node,"-e",script],
        input=json.dumps({"privateKey":STARK,"authorizations":auths}),
        text=True,capture_output=True,timeout=TIMEOUT
    )
    if p.returncode!=0:raise RuntimeError(p.stderr.strip() or "Firma fallita")
    return json.loads(p.stdout)


def counter_offer(offer,cards):
    receiver=str((offer.get("sender") or {}).get("slug") or "").strip()
    ids=[str(c["assetId"]).strip() for c in cards if c.get("assetId")]

    if not receiver:
        print("❌ receiverSlug mancante",flush=True);return False
    if not ids:
        print("❌ Nessuna carta da ricevere",flush=True);return False

    amount=len(ids)*PAY_PER_CARD
    print(f"🟢 Controproposta: {len(ids)} carta/e → €{amount/100:.2f}",flush=True)
    print(f"👤 Receiver: {receiver}",flush=True)
    print("🎯 Kulenovic NON viene ceduto",flush=True)

    if DRY_RUN:
        print("🟡 DRY RUN: controproposta simulata",flush=True);return True
    if not STARK:
        print("❌ SORARE_STARK_PRIVATE_KEY mancante",flush=True);return False

    # Sorare richiede il cambio corrente per le operazioni monetarie.
    rate=get_exchange_rate_id()
    if not rate:return False

    send_amount={"amount":str(amount),"currency":"EUR"}

    prepare={
        "sendAssetIds":[],
        "receiveAssetIds":ids,
        "sendAmount":send_amount,
        "receiverSlug":receiver,
        "exchangeRateId":rate,
        "clientMutationId":str(uuid.uuid4())
    }

    d=graphql("""
    mutation PrepareOffer($input:prepareOfferInput!){
      prepareOffer(input:$input){
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
    }""",{"input":prepare})

    if not d:
        print("❌ Nessuna risposta da prepareOffer",flush=True);return False
    if d.get("errors"):
        for e in d["errors"]:print(f"❌ GraphQL: {e.get('message','Errore')}",flush=True)
        return False

    r=((d.get("data") or {}).get("prepareOffer"))
    if not r:
        print("❌ prepareOffer ha restituito NULL",flush=True);return False

    if r.get("errors"):
        for e in r["errors"]:print(f"❌ prepareOffer: {e.get('message','Errore')}",flush=True)
        return False

    auths=r.get("authorizations") or []
    if not auths:
        print("❌ Nessuna autorizzazione restituita",flush=True);return False

    print(f"🔐 Autorizzazioni ricevute: {len(auths)}",flush=True)

    try: approvals=sign_authorizations(auths)
    except Exception as e:
        print(f"❌ Firma: {e}",flush=True);return False

    create={
        "approvals":approvals,
        "dealId":str(uuid.uuid4()),
        "sendAssetIds":[],
        "receiveAssetIds":ids,
        "sendAmount":send_amount,
        "receiverSlug":receiver,
        "exchangeRateId":rate,
        "clientMutationId":str(uuid.uuid4())
    }

    d=graphql("""
    mutation CreateDirectOffer($input:createDirectOfferInput!){
      createDirectOffer(input:$input){
        tokenOffer{id blockchainId status}
        errors{message}
      }
    }""",{"input":create})

    if not d:
        print("❌ Nessuna risposta da createDirectOffer",flush=True);return False
    if d.get("errors"):
        for e in d["errors"]:print(f"❌ GraphQL: {e.get('message','Errore')}",flush=True)
        return False

    r=((d.get("data") or {}).get("createDirectOffer"))
    if not r:
        print("❌ createDirectOffer ha restituito NULL",flush=True);return False

    if r.get("errors"):
        for e in r["errors"]:print(f"❌ createDirectOffer: {e.get('message','Errore')}",flush=True)
        return False

    oid=(r.get("tokenOffer") or {}).get("id")
    if not oid:
        print("❌ Nessuna offerta creata da Sorare",flush=True);return False

    print("="*40,flush=True)
    print(f"✅ CONTROPROPOSTA INVIATA: {oid}",flush=True)
    print(f"💰 €{amount/100:.2f} ({len(ids)} × €0,20)",flush=True)
    print("🎯 Kulenovic NON ceduto",flush=True)
    print("="*40,flush=True)
    return True


def process_offer(offer):
    oid=str(offer.get("id") or "").strip()
    if not oid:return

    with state_lock:
        if oid in processed:return
        processed.add(oid)

    print("\n"+"="*40,flush=True)
    print(f"📨 OFFERTA {oid}",flush=True)

    sender=(offer.get("senderSide") or {}).get("anyCards") or []
    receiver=(offer.get("receiverSide") or {}).get("anyCards") or []

    if not any(is_kulenovic(c) for c in receiver):
        print("⏭️ Kulenovic non presente: ignoro",flush=True);return

    print("🎯 Kulenovic trovato",flush=True)

    ids=[c.get("assetId") for c in sender if c.get("assetId")]
    if not ids:
        print("❌ Nessuna carta offerta",flush=True);return

    cards=card_details(ids)
    if len(cards)!=len(ids):
        print("❌ Impossibile verificare tutte le carte",flush=True);return

    print(f"🔎 Controllo {len(cards)} carta/e",flush=True)
    valid=[]

    for c in cards:
        try:
            if valid_card(c):valid.append(c)
        except Exception as e:print(f"❌ Errore controllo carta: {e}",flush=True)

    print(f"📊 Carte valide: {len(valid)}/{len(cards)}",flush=True)

    if not valid:
        print("❌ Nessuna carta idonea.\n🔴 Rifiuto dell'offerta.",flush=True)
        reject_offer(offer);return

    if len(valid)<len(cards):
        print(f"⚠️ {len(cards)-len(valid)} carta/e esclusa/e",flush=True)

    ok=counter_offer(offer,valid)
    print("🟢 Controproposta completata con successo." if ok else "🔴 Controproposta NON creata.",flush=True)

    if not reject_offer(offer):
        print("⚠️ Impossibile rifiutare l'offerta originale",flush=True)


def worker():
    print("🤖 BOT AVVIATO\n📦 VERSIONE BOT: 14.1",flush=True)
    print(f"💰 Pagamento: €{PAY_PER_CARD/100:.2f} per carta",flush=True)
    print(f"📊 Range floor: €{MIN_PRICE/100:.2f} - €{MAX_PRICE/100:.2f}",flush=True)
    print(f"🎂 Età massima: meno di {MAX_AGE} anni",flush=True)
    print("🏆 COMPETIZIONI: TUTTE le activeCompetitions Sorare",flush=True)
    print("🔧 PREPARE: DIRECT_OFFER + exchangeRateId",flush=True)
    print("🔧 CREATE: createDirectOffer",flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}",flush=True)

    if not check_account():
        print("❌ Account non valido. Worker fermato.",flush=True);return

    while True:
        try:
            offers=get_offers()
            print(f"📨 Offerte pendenti: {len(offers)}",flush=True)
            for o in offers:
                try:process_offer(o)
                except Exception as e:print(f"❌ Errore offerta: {e}",flush=True)
            time.sleep(INTERVAL)
        except Exception as e:
            print(f"❌ Worker: {e}",flush=True);time.sleep(INTERVAL)


def start_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:return
        _worker_started=True
        threading.Thread(target=worker,name="sorare-worker",daemon=True).start()
        print("✅ Thread Sorare avviato.",flush=True)


@app.get("/")
def home():
    return jsonify({
        "status":"online","bot":"sorare","version":"14.1",
        "dry_run":DRY_RUN,"pay_per_card_cents":PAY_PER_CARD,
        "interval_seconds":INTERVAL,"max_age":MAX_AGE,
        "competition_mode":"ALL_ACTIVE_SORARE_COMPETITIONS",
        "offer_mode":"DIRECT_OFFER_NO_TYPE"
    })


@app.get("/health")
def health():
    return jsonify({"status":"ok","bot":"running","version":"14.1"})


if __name__=="__main__":
    start_worker()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
