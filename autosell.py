import os,json,time,uuid,shutil,subprocess,threading,requests
from flask import Flask,jsonify

app=Flask(__name__)

URL="https://api.sorare.com/graphql"

TOKEN=os.getenv("SORARE_JWT_TOKEN","").strip()
AUD=os.getenv("SORARE_JWT_AUD","").strip()
STARK=os.getenv("SORARE_STARK_PRIVATE_KEY","").strip()
SOLANA=os.getenv("SORARE_SOLANA_PRIVATE_KEY","").strip()

DRY_RUN=os.getenv("DRY_RUN","false").strip().lower()=="true"
INTERVAL=int(os.getenv("INTERVAL","30"))
TIMEOUT=int(os.getenv("TIMEOUT","25"))

# ============================================================
# NUOVE VENDITE
# ============================================================

MAX_PRICE_CENTS=70
MIN_SELL_PRICE_CENTS=30
MIN_LISTINGS=5

# ============================================================
# RINNOVO
# ============================================================
#
# IMPORTANTE:
# Sorare restituisce startDate/endDate sull'offerta creata.
# Non inventiamo un campo duration/endDate nella mutation.
# Il bot usa esclusivamente l'endDate restituito da Sorare.
#
# Quando l'offerta scade:
#   prezzo = ultimo prezzo memorizzato
#   nuova vendita = stesso identico prezzo
#
# Nessun floor.
# Nessun cambio prezzo.
# Nessun retry che modifica il prezzo.
# ============================================================

SOURCE_LABEL="AUTOBUY / SWAP"

STATE_FILE=os.getenv("BOT_STATE_PATH","bot_state.json").strip()

VERSION="AUTOSell-16.0-EUR-7D-RENEWAL-SAME-PRICE"

KULENOVIC_SLUG="sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET="0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c7136796b6c0ed10ba0a6"

lock=threading.RLock()
worker_lock=threading.Lock()
worker_started=False

B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ============================================================
# UTILS / STATE
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())


def new_id():
    return str(uuid.uuid4())


def asset_id(c):
    return str(c.get("assetId") or c.get("asset_id") or "").strip()


def label(c):
    return c.get("name") or c.get("slug") or asset_id(c) or "Carta"


def eur(v):
    try:
        return f"€{int(v)/100:.2f}"
    except:
        return "N/D"


def timestamp(v):
    if v is None:
        return None

    try:
        n=float(v)
        return n/1000 if n>10_000_000_000 else n
    except:
        pass

    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(v).replace("Z","+00:00")
        ).timestamp()
    except:
        return None


def expired(v):
    t=timestamp(v)
    return t is not None and time.time()>=t


def default_state():
    return {
        "processed_offers":[],
        "acquired_cards":[],
        "pending_autobuys":[],
        "updated_at":int(time.time())
    }


def save(data):
    tmp=f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"

    try:
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp,STATE_FILE)
        return True

    except Exception as e:
        print(f"❌ State write: {e}",flush=True)

        try:
            os.remove(tmp)
        except:
            pass

        return False


def load():
    try:
        with open(STATE_FILE,"r",encoding="utf-8") as f:
            d=json.load(f)

        if not isinstance(d,dict):
            d=default_state()

    except:
        d=default_state()

    for k,v in default_state().items():
        d.setdefault(k,v)

    return d


def ensure_state():
    path=os.path.abspath(STATE_FILE)

    os.makedirs(
        os.path.dirname(path) or ".",
        exist_ok=True
    )

    if not os.path.exists(path):
        save(default_state())


def cards():
    with lock:
        return list(load().get("acquired_cards",[]))


def upsert_card(card):
    aid=asset_id(card)

    if not aid:
        return False

    with lock:
        d=load()
        arr=d["acquired_cards"]

        for i,c in enumerate(arr):

            if norm(asset_id(c))==norm(aid):

                arr[i]={
                    **c,
                    **card
                }

                d["updated_at"]=int(time.time())

                return save(d)

        arr.append(card)

        d["updated_at"]=int(time.time())

        return save(d)


def update_card(asset,**fields):

    with lock:

        d=load()

        for c in d["acquired_cards"]:

            if norm(asset_id(c))==norm(asset):

                c.update({
                    k:v
                    for k,v in fields.items()
                    if v is not None
                })

                d["updated_at"]=int(time.time())

                return save(d)

    return False


def sellable():

    return [
        dict(c)
        for c in cards()
        if isinstance(c,dict)
        and norm(c.get("status")) in {
            "da_vendere",
            "ready"
        }
        and asset_id(c)
    ]


def selling():

    return [
        dict(c)
        for c in cards()
        if isinstance(c,dict)
        and norm(c.get("status"))=="selling"
        and asset_id(c)
    ]


# ============================================================
# GRAPHQL
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN mancante"
        )

    h={
        "Authorization":
            TOKEN
            if TOKEN.lower().startswith("bearer ")
            else f"Bearer {TOKEN}",

        "Content-Type":"application/json",
        "Accept":"application/json",
        "User-Agent":
            f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        h["JWT-AUD"]=AUD

    return h


def gql(query,variables=None):

    for attempt in range(3):

        try:

            r=requests.post(
                URL,
                headers=headers(),
                json={
                    "query":query,
                    "variables":variables or {}
                },
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True
            )

            if r.status_code==429:

                try:
                    d=float(
                        r.headers.get(
                            "Retry-After",
                            2+attempt*2
                        )
                    )
                except:
                    d=2+attempt*2

                time.sleep(
                    max(1,min(d,60))
                )

                continue

            if r.status_code!=200:

                print(
                    f"❌ Sorare: {r.text[:1200]}",
                    flush=True
                )

                time.sleep(attempt+1)

                continue

            data=r.json()

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:2000],
                    flush=True
                )

            return data

        except Exception as e:

            print(
                f"❌ GraphQL: {e}",
                flush=True
            )

            time.sleep(attempt+1)

    return None


# ============================================================
# CARD
# ============================================================

def card_details(asset):

    d=gql(
        """
        query($ids:[String!]!){
          anyCards(assetIds:$ids){
            assetId
            slug
            name
            rarityTyped
            seasonYear

            anyPlayer{
              slug
              displayName

              activeClub{
                slug
                name
              }
            }
          }
        }
        """,
        {"ids":[asset]}
    )

    a=(
        ((d or {}).get("data") or {})
        .get("anyCards") or []
    )

    return a[0] if a else None


def amount_to_eur(a):

    if not isinstance(a,dict):
        return None

    try:
        v=int(a.get("eurCents") or 0)

        if v>0:
            return v

    except:
        pass

    try:
        v=int(a.get("usdCents") or 0)

        if v>0:

            r=requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from":"USD",
                    "to":"EUR"
                },
                timeout=10
            )

            return round(
                v*
                float(
                    r.json()["rates"]["EUR"]
                )
            )

    except:
        pass

    return None


# ============================================================
# OFFERTE ATTIVE
# ============================================================

def active_offers():

    d=gql(
        """
        query($first:Int){
          tokens{
            liveSingleSaleOffers(first:$first){
              nodes{
                id
                startDate
                endDate

                senderSide{
                  anyCards{
                    assetId
                    slug
                    name
                    rarityTyped
                    seasonYear

                    anyPlayer{
                      slug
                      displayName
                    }
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
        }
        """,
        {"first":100}
    )

    return (
        (
            ((d or {}).get("data") or {})
            .get("tokens") or {}
        )
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []


def find_active_public_offer(asset):

    wanted=norm(asset)

    d=gql(
        """
        query($id:String!){
          anyCards(assetIds:[$id]){
            assetId

            liveSingleSaleOffer{
              id
              startDate
              endDate

              receiverSide{
                amounts{
                  eurCents
                  usdCents
                }
              }
            }
          }
        }
        """,
        {"id":asset}
    )

    if d and not d.get("errors"):

        for c in (
            (d.get("data") or {})
            .get("anyCards") or []
        ):

            if norm(c.get("assetId"))!=wanted:
                continue

            o=c.get(
                "liveSingleSaleOffer"
            ) or {}

            if o.get("id"):

                return {
                    "id":o["id"],
                    "startDate":o.get("startDate"),
                    "endDate":o.get("endDate"),
                    "price":
                        amount_to_eur(
                            (
                                o.get("receiverSide")
                                or {}
                            ).get("amounts")
                        )
                }

    for o in active_offers():

        for c in (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        ):

            if norm(c.get("assetId"))==wanted:

                return {
                    "id":o.get("id"),
                    "startDate":o.get("startDate"),
                    "endDate":o.get("endDate"),
                    "price":
                        amount_to_eur(
                            (
                                o.get("receiverSide")
                                or {}
                            ).get("amounts")
                        )
                }

    return None


# ============================================================
# SYNC OFFERTE MANUALI + BOT
# ============================================================

def sync_active_offers():

    offers=active_offers()

    if not offers:
        return

    known={
        norm(asset_id(c)):c
        for c in cards()
    }

    for o in offers:

        price=amount_to_eur(
            (o.get("receiverSide") or {})
            .get("amounts")
        )

        end=o.get("endDate")
        start=o.get("startDate")
        oid=o.get("id")

        for c in (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        ):

            aid=asset_id(c)

            if not aid or not oid:
                continue

            old=known.get(
                norm(aid),
                {}
            )

            # IMPORTANTE:
            # se Sorare ci dà il prezzo corrente,
            # quello diventa il prezzo memorizzato.
            #
            # Non calcoliamo alcun prezzo.

            upsert_card({

                **old,
                **c,

                "assetId":aid,
                "status":"selling",

                "sale_offer_id":oid,

                "sale_price_cents":
                    price
                    if price is not None
                    else old.get(
                        "sale_price_cents"
                    ),

                "sale_offer_start_date":
                    start
                    or old.get(
                        "sale_offer_start_date"
                    ),

                "sale_offer_end_date":
                    end
                    or old.get(
                        "sale_offer_end_date"
                    ),

                "source":
                    old.get("source")
                    or "MANUAL",

                "last_error":None
            })

    print(
        f"🔄 Sync offerte attive → {len(offers)}",
        flush=True
    )


# ============================================================
# VALIDAZIONE NUOVE VENDITE
# ============================================================

def get_floor(card):

    p=card.get("anyPlayer") or {}

    slug=norm(p.get("slug"))
    rarity=norm(
        card.get("rarityTyped")
    )

    try:
        season=int(
            card.get("seasonYear")
        )
    except:
        return None

    if not slug or not rarity:
        return None

    d=gql(
        """
        query($slug:String,$first:Int){
          tokens{
            liveSingleSaleOffers(
              playerSlug:$slug,
              first:$first
            ){
              nodes{

                senderSide{
                  anyCards{
                    assetId
                    rarityTyped
                    seasonYear

                    anyPlayer{
                      slug
                    }
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
        }
        """,
        {
            "slug":slug,
            "first":50
        }
    )

    nodes=(
        (
            ((d or {}).get("data") or {})
            .get("tokens") or {}
        )
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices=[]

    for o in nodes:

        for x in (
            (o.get("senderSide") or {})
            .get("anyCards") or []
        ):

            try:
                same=(
                    int(x.get("seasonYear"))
                    ==season
                )
            except:
                same=False

            if (
                same
                and norm(
                    x.get("rarityTyped")
                )==rarity
                and norm(
                    (x.get("anyPlayer") or {})
                    .get("slug")
                )==slug
            ):

                p=amount_to_eur(
                    (
                        o.get("receiverSide")
                        or {}
                    ).get("amounts")
                )

                if p is not None:
                    prices.append(p)

                break

    print(
        f"📊 Listing trovate: "
        f"{len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return (
        min(prices)
        if len(prices)>=MIN_LISTINGS
        else None
    )


def is_kulenovic(c):

    return (
        norm(c.get("slug"))
        ==norm(KULENOVIC_SLUG)
        or
        norm(c.get("assetId"))
        ==norm(KULENOVIC_ASSET)
    )


def is_sealed(c):

    r=norm(
        c.get("rarityTyped")
    )

    n=norm(c.get("name"))
    s=norm(c.get("slug"))

    return (
        r=="sealed"
        or "sealed" in n
        or "sealed" in s
    )


def validate(c):

    if is_kulenovic(c):
        return False,"KULENOVIC"

    if is_sealed(c):
        return False,"SEALED"

    if norm(
        c.get("rarityTyped")
    )!="limited":
        return False,"RARITY"

    floor=get_floor(c)

    if floor is None:
        return False,"FLOOR_UNKNOWN"

    if floor>MAX_PRICE_CENTS:
        return False,"FLOOR_HIGH"

    return True,max(
        floor,
        MIN_SELL_PRICE_CENTS
    )


# ============================================================
# SOLANA
# ============================================================

def b58decode(v):

    n=0

    for x in str(v).strip():

        i=B58.find(x)

        if i<0:
            raise ValueError(
                "Base58 non valido"
            )

        n=n*58+i

    raw=(
        b""
        if n==0
        else n.to_bytes(
            max(
                1,
                (n.bit_length()+7)//8
            ),
            "big"
        )
    )

    z=(
        len(str(v))
        -
        len(str(v).lstrip("1"))
    )

    return b"\0"*z+raw


def solana_key_info():

    if not SOLANA:
        raise RuntimeError(
            "SORARE_SOLANA_PRIVATE_KEY mancante"
        )

    try:

        b=b58decode(SOLANA)

        if len(b) in {32,64}:
            return b

    except:
        pass

    h=(
        SOLANA[2:]
        if SOLANA.startswith("0x")
        else SOLANA
    )

    try:

        b=bytes.fromhex(h)

        if len(b) in {32,64}:
            return b

    except:
        pass

    raise RuntimeError(
        "SORARE_SOLANA_PRIVATE_KEY non valida"
    )


def sign_authorizations(auths):

    node=(
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    types=[
        (a.get("request") or {})
        .get("__typename")
        for a in auths
    ]

    if (
        any(
            t!="SolanaTokenTransferAuthorizationRequest"
            for t in types
        )
        and not STARK
    ):
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY mancante"
        )

    if any(
        t=="SolanaTokenTransferAuthorizationRequest"
        for t in types
    ):
        solana_key_info()

    js=r'''
const crypto=require("crypto");
const {signAuthorizationRequest}=require("@sorare/crypto");
const {
  createSignableMessage,
  createKeyPairFromPrivateKeyBytes,
  createSignerFromKeyPair
}=require("@solana/kit");

const fs=require("fs");

const input=JSON.parse(
  fs.readFileSync(0,"utf8")
);

const A=
  "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function d(v){

  let n=0n;

  for(
    const c of String(v).trim()
  ){

    const i=A.indexOf(c);

    if(i<0)
      throw Error("Base58");

    n=n*58n+BigInt(i);
  }

  let h=n?n.toString(16):"";

  if(h.length%2)
    h="0"+h;

  let b=h
    ?Buffer.from(h,"hex")
    :Buffer.alloc(0);

  let z=0;

  for(
    const c of String(v).trim()
  ){
    if(c!="1")break;
    z++;
  }

  return new Uint8Array(
    Buffer.concat([
      Buffer.alloc(z),
      b
    ])
  );
}

function e(x){

  let b=Buffer.from(x);
  let n=0n;

  for(const q of b)
    n=n*256n+BigInt(q);

  let s="";

  while(n){

    let r=Number(n%58n);

    s=A[r]+s;

    n/=58n;
  }

  let z=0;

  for(const q of b){

    if(q)break;

    z++;
  }

  return "1".repeat(z)+s;
}

async function sol(a){

  let k=d(
    input.solanaPrivateKey
  );

  if(k.length===64)
    k=k.slice(0,32);

  if(k.length!==32)
    throw Error("Solana key");

  let kp=
    await createKeyPairFromPrivateKeyBytes(k);

  let sg=
    createSignerFromKeyPair(kp);

  let r=a.request;

  if(
    sg.address!==r.senderAddress
  ){
    throw Error(
      "Solana key/sender mismatch"
    );
  }

  let m=[
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

  let h=
    crypto
      .createHash("sha256")
      .update(Buffer.from(m))
      .digest();

  let z=
    await sg.signMessages([
      createSignableMessage(
        new Uint8Array(h)
      )
    ]);

  return {
    fingerprint:a.fingerprint,

    solanaTokenTransferApproval:{
      signature:
        e(z[0][sg.address]),

      nonce:r.nonce,

      expirationTimestamp:
        r.expirationTimestamp
    }
  };
}

function stark(a){

  let r=a.request;

  let s=
    signAuthorizationRequest(
      input.starkPrivateKey,
      r
    );

  let b={
    fingerprint:a.fingerprint
  };

  if(
    r.__typename===
    "StarkexTransferAuthorizationRequest"
  ){
    return {
      ...b,

      starkexTransferApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature:s
      }
    };
  }

  if(
    r.__typename===
    "StarkexLimitOrderAuthorizationRequest"
  ){
    return {
      ...b,

      starkexLimitOrderApproval:{
        nonce:r.nonce,
        expirationTimestamp:
          r.expirationTimestamp,
        signature:s
      }
    };
  }

  if(
    r.__typename===
    "MangopayWalletTransferAuthorizationRequest"
  ){
    return {
      ...b,

      mangopayWalletTransferApproval:{
        nonce:r.nonce,
        signature:s
      }
    };
  }

  throw Error(
    "Authorization "+r.__typename
  );
}

(async()=>{

  let out=[];

  for(
    const a of input.authorizations
  ){

    let t=
      (a.request || {})
      .__typename;

    out.push(
      t===
      "SolanaTokenTransferAuthorizationRequest"
      ?await sol(a)
      :stark(a)
    );
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
            "starkPrivateKey":STARK,
            "solanaPrivateKey":SOLANA,
            "authorizations":auths
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

    if p.returncode:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# PREPARE SALE
# ============================================================
#
# FIX PRINCIPALE:
#
# NON inviamo più:
#
#     "type":"SINGLE_SALE_OFFER"
#
# perché lo schema live del tuo endpoint ha risposto:
#
#     Field is not defined on prepareOfferInput
#
# ============================================================

PREPARE="""
mutation($input:prepareOfferInput!){
  prepareOffer(input:$input){

    authorizations{

      fingerprint

      request{

        __typename

        ... on StarkexTransferAuthorizationRequest{
          amount
          condition
          expirationTimestamp
          nonce
          receiverPublicKey
          receiverVaultId
          senderVaultId
          token

          feeInfoUser{
            feeLimit
            sourceVaultId
            tokenId
          }
        }

        ... on StarkexLimitOrderAuthorizationRequest{
          vaultIdSell
          vaultIdBuy
          amountSell
          amountBuy
          tokenSell
          tokenBuy
          nonce
          expirationTimestamp

          feeInfo{
            feeLimit
            tokenId
            sourceVaultId
          }
        }

        ... on MangopayWalletTransferAuthorizationRequest{
          nonce
          amount
          currency
          operationHash
          mangopayWalletId
        }

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

    errors{
      message
    }
  }
}
"""


def prepare_sale(asset,price):

    # ========================================================
    # NESSUN "type".
    #
    # Il tuo endpoint live ha dimostrato che prepareOfferInput
    # non lo accetta.
    # ========================================================

    variables={
        "input":{
            "sendAssetIds":[asset],
            "receiveAssetIds":[],
            "settlementCurrencies":["EUR"],

            "receiveAmount":{
                "amount":str(price),
                "currency":"EUR"
            },

            "clientMutationId":new_id()
        }
    }

    d=gql(
        PREPARE,
        variables
    )

    r=(
        ((d or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not r:
        return None

    if r.get("errors"):

        print(
            "❌ prepareOffer:",
            json.dumps(
                r["errors"],
                ensure_ascii=False
            )[:2000],
            flush=True
        )

        return None

    return r.get(
        "authorizations"
    ) or None


# ============================================================
# CREATE SALE
# ============================================================

def create_sale_once(card,price):

    asset=asset_id(card)

    if not asset:
        return None,None,None

    if DRY_RUN:
        return "DRY-RUN",None,None

    auth=prepare_sale(
        asset,
        price
    )

    if not auth:
        return None,"PREPARE_FAILED",None

    try:
        approvals=sign_authorizations(
            auth
        )

    except Exception as e:

        return None,str(e),None

    q="""
    mutation(
      $input:createSingleSaleOfferInput!
    ){
      createSingleSaleOffer(input:$input){

        tokenOffer{
          id
          startDate
          endDate
        }

        errors{
          message
        }
      }
    }
    """

    d=gql(
        q,
        {
            "input":{

                "approvals":approvals,

                "dealId":new_id(),

                "assetId":asset,

                "receiveAmount":{
                    "amount":str(price),
                    "currency":"EUR"
                },

                "clientMutationId":new_id()
            }
        }
    )

    r=(
        ((d or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not r:
        return None,"CREATE_NO_RESULT",None

    errors=r.get("errors") or []

    if errors:

        text=" ".join(
            str(x.get("message",""))
            for x in errors
            if isinstance(x,dict)
        )

        if (
            "is not owned by" in
            norm(text)
            and
            "on solana" in
            norm(text)
        ):
            return None,"NOT_OWNED",None

        if (
            "active public offer already exists"
            in norm(text)
        ):

            x=find_active_public_offer(
                asset
            )

            return (
                (x or {}).get("id")
                if x
                else "ALREADY-LISTED",

                "ALREADY-LISTED",

                (x or {}).get("endDate")
                if x
                else None
            )

        return None,text,None

    o=r.get("tokenOffer") or {}

    return (
        o.get("id"),
        None,
        o.get("endDate")
    )


# ============================================================
# NUOVE CARTE
# ============================================================

def process(card):

    asset=asset_id(card)

    if (
        not asset
        or norm(card.get("status"))
        =="not_owned"
    ):
        return

    existing=find_active_public_offer(
        asset
    )

    if existing:

        update_card(
            asset,

            status="selling",

            sale_offer_id=
                existing.get("id"),

            sale_price_cents=
                existing.get("price"),

            sale_offer_start_date=
                existing.get("startDate"),

            sale_offer_end_date=
                existing.get("endDate"),

            last_error=None
        )

        return

    details=card_details(asset)

    if not details:

        update_card(
            asset,
            status="da_vendere",
            last_error="CARD_DETAILS"
        )

        return

    ok,result=validate(details)

    if not ok:

        status=(
            "blocked"
            if result in {
                "KULENOVIC",
                "SEALED",
                "RARITY"
            }
            else "da_vendere"
        )

        update_card(
            asset,
            status=status,
            last_error=result
        )

        return

    price=int(result)

    update_card(
        asset,
        status="selling",
        sale_price_cents=price,
        last_error=None
    )

    oid,used,err=create_sale_once(
        details,
        price
    )

    if err=="NOT_OWNED":

        update_card(
            asset,
            status="not_owned",
            last_error=
                "SORARE_NOT_OWNED_ON_SOLANA"
        )

        return

    if not oid:

        update_card(
            asset,
            status="da_vendere",
            last_error=
                err
                or "CREATE_SALE_FAILED"
        )

        return

    # Dopo la creazione salviamo il prezzo
    # usato e l'endDate restituito da Sorare.

    update_card(
        asset,

        status="selling",

        sale_offer_id=oid,

        sale_price_cents=
            used
            if used is not None
            else price,

        sale_offer_end_date=err,

        last_error=None
    )


# ============================================================
# RINNOVO
# ============================================================

def renew_expired_sales():

    """
    RINNOVO PURO.

    Non calcola floor.
    Non chiama validate().
    Non modifica il prezzo.
    Non applica €0.30.
    Non applica €0.70.
    Non usa ETH.
    Non fa retry aumentando il prezzo.

    L'unico prezzo possibile è quello già
    memorizzato nell'ultima vendita.
    """

    for card in selling():

        asset=asset_id(card)

        if not asset:
            continue

        # ----------------------------------------------------
        # 1. Controlliamo se l'offerta è ancora attiva.
        # ----------------------------------------------------

        active=find_active_public_offer(
            asset
        )

        if active:

            # Se Sorare ci dà un prezzo corrente,
            # quello è il prezzo reale dell'offerta.
            #
            # Non calcoliamo nulla.

            update_card(
                asset,

                status="selling",

                sale_offer_id=
                    active.get("id"),

                sale_price_cents=
                    active.get("price")
                    if active.get("price")
                    is not None
                    else card.get(
                        "sale_price_cents"
                    ),

                sale_offer_start_date=
                    active.get(
                        "startDate"
                    ),

                sale_offer_end_date=
                    active.get(
                        "endDate"
                    ),

                last_error=None
            )

            continue

        # ----------------------------------------------------
        # 2. Nessuna offerta attiva.
        #
        # Recuperiamo SOLO il prezzo già memorizzato.
        # ----------------------------------------------------

        price=card.get(
            "sale_price_cents"
        )

        end=card.get(
            "sale_offer_end_date"
        )

        # Nessun prezzo = non inventiamo.
        if price is None:
            print(
                f"⏸️ RINNOVO BLOCCATO "
                f"{asset}: prezzo sconosciuto",
                flush=True
            )
            continue

        # Nessuna scadenza = non inventiamo.
        if not end:
            print(
                f"⏸️ RINNOVO BLOCCATO "
                f"{asset}: endDate sconosciuto",
                flush=True
            )
            continue

        # Non è ancora scaduta.
        if not expired(end):
            continue

        try:
            price=int(price)
        except:
            print(
                f"⏸️ RINNOVO BLOCCATO "
                f"{asset}: prezzo non valido",
                flush=True
            )
            continue

        print(
            f"♻️ RINNOVO {asset} "
            f"→ {eur(price)}",
            flush=True
        )

        details=card_details(asset)

        if not details:
            update_card(
                asset,
                last_error=
                    "RENEWAL_CARD_DETAILS"
            )
            continue

        # ----------------------------------------------------
        # IMPORTANTISSIMO:
        #
        # Qui passiamo ESATTAMENTE price.
        #
        # Nessun floor.
        # Nessun validate.
        # Nessun nuovo prezzo.
        # ----------------------------------------------------

        oid,err,end2=create_sale_once(
            details,
            price
        )

        if err=="NOT_OWNED":

            update_card(
                asset,

                status="not_owned",

                last_error=
                    "SORARE_NOT_OWNED_ON_SOLANA"
            )

            continue

        if oid=="ALREADY-LISTED":

            x=find_active_public_offer(
                asset
            )

            if x:

                update_card(
                    asset,

                    status="selling",

                    sale_offer_id=
                        x.get("id"),

                    sale_price_cents=
                        x.get("price")
                        if x.get("price")
                        is not None
                        else price,

                    sale_offer_start_date=
                        x.get("startDate"),

                    sale_offer_end_date=
                        x.get("endDate"),

                    last_error=None
                )

            continue

        if not oid:

            # IMPORTANTE:
            # manteniamo il vecchio prezzo.
            # Non lo cambiamo.

            update_card(
                asset,

                status="selling",

                sale_price_cents=price,

                last_error=
                    err
                    or "RENEWAL_FAILED"
            )

            print(
                f"❌ RINNOVO FALLITO "
                f"{asset} | {err}",
                flush=True
            )

            continue

        # ----------------------------------------------------
        # RINNOVO RIUSCITO
        # ----------------------------------------------------

        update_card(
            asset,

            status="selling",

            sale_offer_id=oid,

            sale_price_cents=price,

            sale_offer_start_date=None,

            sale_offer_end_date=end2,

            last_error=None
        )

        print(
            f"♻️ RINNOVATA "
            f"{asset} | {eur(price)} "
            f"| end={end2}",
            flush=True
        )


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    d=gql(
        """
        query{
          currentUser{
            slug
            nickname
            starkKey
          }
        }
        """
    )

    u=(
        ((d or {}).get("data") or {})
        .get("currentUser")
    )

    if not u:

        print(
            "❌ Account Sorare non verificato",
            flush=True
        )

        return False

    print(
        f"✅ Sorare: "
        f"{u.get('nickname') or u.get('slug')}",
        flush=True
    )

    print(
        "🔐 Stark key: "
        +
        (
            "PRESENTE"
            if u.get("starkKey")
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    print(
        "🔑 Solana key: "
        +
        (
            "PRESENTE"
            if SOLANA
            else "NON DISPONIBILE"
        ),
        flush=True
    )

    return True


def recovery():

    for c in selling():

        print(
            f"🔄 {asset_id(c)} | "
            f"{eur(c.get('sale_price_cents'))} | "
            f"{c.get('sale_offer_end_date')}",
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
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True
    )

    print(
        "💰 NUOVE VENDITE: "
        "floor massimo €0.70 / minimo €0.30",
        flush=True
    )

    print(
        "♻️ RENEWAL: "
        "STESSO PREZZO + NUOVO PERIODO SORARE",
        flush=True
    )

    print(
        "🛡️ RINNOVO: BOT + VENDITE MANUALI",
        flush=True
    )

    print(
        "🚫 RINNOVO: NESSUN CAMBIO PREZZO",
        flush=True
    )

    print(
        "🔒 KULENOVIC / SEALED: MAI VENDUTE",
        flush=True
    )

    try:

        headers()
        ensure_state()

    except Exception as e:

        print(
            f"❌ Configurazione: {e}",
            flush=True
        )

        return

    if not check_account():
        return

    recovery()

    while True:

        try:

            # ------------------------------------------------
            # 1. Sincronizziamo TUTTE le offerte attive.
            #
            # Questo comprende anche quelle create
            # manualmente dall'utente.
            # ------------------------------------------------

            sync_active_offers()

            # ------------------------------------------------
            # 2. Rinnoviamo solo quelle realmente scadute.
            # ------------------------------------------------

            renew_expired_sales()

            # ------------------------------------------------
            # 3. Nuove carte.
            # ------------------------------------------------

            for c in sellable():

                try:

                    process(c)

                except Exception as e:

                    a=asset_id(c)

                    print(
                        f"❌ AutoSell {a}: {e}",
                        flush=True
                    )

                    if a:

                        current=next(
                            (
                                x
                                for x in cards()
                                if norm(
                                    asset_id(x)
                                )==norm(a)
                            ),
                            None
                        )

                        if (
                            not current
                            or
                            norm(
                                current.get("status")
                            )!="not_owned"
                        ):

                            update_card(
                                a,
                                status="da_vendere",
                                last_error=str(e)
                            )

            time.sleep(INTERVAL)

        except Exception as e:

            print(
                f"❌ Worker: {e}",
                flush=True
            )

            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    c=cards()

    count=lambda s:sum(
        1
        for x in c
        if norm(x.get("status"))==s
    )

    return jsonify({

        "status":"online",

        "bot":"autosell",

        "version":VERSION,

        "dry_run":DRY_RUN,

        "floor_max":"€0.70",

        "min_sell_price":"€0.30",

        "min_live_listings":
            MIN_LISTINGS,

        "rarity":"LIMITED",

        "sealed":"NEVER_SELL",

        "kulenovic":"NEVER_SELL",

        "renewal":
            "SAME_PRICE",

        "renewal_period":
            "SORARE_DEFAULT",

        "manual_listings":
            "RENEWED",

        "price_change_on_renewal":
            False,

        "settlement":"EUR",

        "storage":STATE_FILE,

        "cards":len(c),

        "da_vendere":
            count("da_vendere")
            +
            count("ready"),

        "selling":
            count("selling"),

        "not_owned":
            count("not_owned"),

        "worker":
            worker_started
    })


@app.get("/health")
def health():

    return jsonify({

        "status":"ok",

        "bot":"autosell",

        "version":VERSION,

        "worker":
            worker_started,

        "dry_run":
            DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():

    c=cards()

    return jsonify({

        "count":len(c),

        "cards":c
    })


def start_worker():

    global worker_started

    with worker_lock:

        if worker_started:
            return

        worker_started=True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()


if __name__=="__main__":

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
