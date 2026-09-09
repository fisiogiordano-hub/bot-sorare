import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

SORARE_URL = "https://api.sorare.com/graphql"
TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
SOLANA = os.getenv("SORARE_SOLANA_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"
INTERVAL = int(os.getenv("INTERVAL", "30"))
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

MIN_PRICE = 32
MAX_PRICE = 70
MIN_LISTINGS = 5

STATE_FILE = os.getenv("BOT_STATE_PATH", "bot_state.json").strip()
VERSION = "AUTOSell-11.1-SOLANA-FIX"

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

state_lock = threading.RLock()
worker_lock = threading.Lock()
worker_started = False

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ============================================================
# UTILS
# ============================================================

def norm(v):
    return str(v or "").strip().lower()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id():
    return str(uuid.uuid4())


def asset_id(card):
    return str(card.get("assetId") or card.get("asset_id") or "").strip()


def label(card):
    return card.get("name") or card.get("slug") or asset_id(card) or "Carta"


def eur(cents):
    return "N/D" if cents is None else f"€{cents / 100:.2f}"


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "processed_offers": [],
        "acquired_cards": [],
        "pending_autobuys": [],
        "updated_at": int(time.time())
    }


def save_document(data):
    tmp = f"{STATE_FILE}.{uuid.uuid4().hex}.tmp"

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, STATE_FILE)
        return True

    except Exception as e:
        print(f"❌ Errore scrittura state: {e}", flush=True)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def ensure_state():
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(path):
        save_document(default_state())


def load_document():
    ensure_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_state()

        for key in (
            "processed_offers",
            "acquired_cards",
            "pending_autobuys"
        ):
            data.setdefault(key, [])

        data.setdefault("updated_at", int(time.time()))
        return data

    except Exception as e:
        print(f"❌ Errore lettura state: {e}", flush=True)
        return default_state()


def get_cards():
    with state_lock:
        cards = load_document().get("acquired_cards", [])
        return cards if isinstance(cards, list) else []


def update_card(asset, status=None, offer_id=None, error=None):
    wanted = norm(asset)

    with state_lock:
        data = load_document()
        cards = data.get("acquired_cards", [])

        for card in cards:
            if not isinstance(card, dict):
                continue
            if norm(asset_id(card)) != wanted:
                continue

            if status is not None:
                card["status"] = status

            if offer_id:
                card["sale_offer_id"] = offer_id

            card["last_error"] = error

            if status == "SELLING":
                card["selling_at"] = now()

            data["acquired_cards"] = cards
            data["updated_at"] = int(time.time())
            return save_document(data)

        print(f"⚠️ Carta non presente nello state: {asset}", flush=True)
        return False


def sellable_cards():
    return [
        dict(c)
        for c in get_cards()
        if isinstance(c, dict)
        and norm(c.get("status")) in {"da_vendere", "ready"}
        and asset_id(c)
    ]


# ============================================================
# SORARE
# ============================================================

def auth_headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN mancante")

    headers = {
        "Authorization": TOKEN if TOKEN.lower().startswith("bearer ")
        else f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Sorare-AutoSell/{VERSION}"
    }

    if AUD:
        headers["JWT-AUD"] = AUD

    return headers


def gql(query, variables=None):
    for attempt in range(3):
        try:
            r = requests.post(
                SORARE_URL,
                headers=auth_headers(),
                json={"query": query, "variables": variables or {}},
                timeout=TIMEOUT
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue

            if r.status_code != 200:
                print(f"❌ Sorare: {r.text[:1000]}", flush=True)
                time.sleep(attempt + 1)
                continue

            data = r.json()

            if data.get("errors"):
                print(
                    "❌ GraphQL:",
                    json.dumps(data["errors"], ensure_ascii=False)[:3000],
                    flush=True
                )

            return data

        except Exception as e:
            print(f"❌ GraphQL: {e}", flush=True)
            time.sleep(attempt + 1)

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():
    data = gql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = ((data or {}).get("data") or {}).get("currentUser")

    if not user:
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        "✅ Sorare: " + str(user.get("nickname") or user.get("slug")),
        flush=True
    )
    print(
        "🔐 Stark key account: "
        + ("PRESENTE" if user.get("starkKey") else "NON DISPONIBILE"),
        flush=True
    )
    print(
        "🔑 Solana private key: "
        + ("PRESENTE" if SOLANA else "NON DISPONIBILE"),
        flush=True
    )

    return True


# ============================================================
# CARD
# ============================================================

def card_details(asset):
    data = gql("""
        query Cards($ids: [String!]!) {
            anyCards(assetIds: $ids) {
                assetId
                slug
                name
                rarityTyped
                seasonYear
                anyPlayer {
                    slug
                    displayName
                    activeClub {
                        slug
                        name
                    }
                }
            }
        }
    """, {"ids": [asset]})

    cards = (
        ((data or {}).get("data") or {}).get("anyCards") or []
    )

    return cards[0] if cards else None


# ============================================================
# EUR / FLOOR
# ============================================================

def usd_to_eur(usd_cents):
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EUR"},
            timeout=10
        )
        return round(usd_cents * float(r.json()["rates"]["EUR"]))
    except Exception:
        return None


def amount_to_eur(amounts):
    if not isinstance(amounts, dict):
        return None

    try:
        value = int(amounts.get("eurCents") or 0)
        if value > 0:
            return value
    except Exception:
        pass

    try:
        value = int(amounts.get("usdCents") or 0)
        if value > 0:
            return usd_to_eur(value)
    except Exception:
        pass

    return None


def get_floor(card):
    player = card.get("anyPlayer") or {}
    slug = norm(player.get("slug"))
    rarity = norm(card.get("rarityTyped"))

    try:
        season = int(card.get("seasonYear"))
    except Exception:
        return None

    if not slug or not rarity:
        return None

    data = gql("""
        query LiveOffers($slug: String, $first: Int) {
            tokens {
                liveSingleSaleOffers(playerSlug: $slug, first: $first) {
                    nodes {
                        senderSide {
                            anyCards {
                                assetId
                                rarityTyped
                                seasonYear
                                anyPlayer { slug }
                            }
                        }
                        receiverSide {
                            amounts {
                                eurCents
                                usdCents
                            }
                        }
                    }
                }
            }
        }
    """, {"slug": slug, "first": 50})

    nodes = (
        (((data or {}).get("data") or {}).get("tokens") or {})
        .get("liveSingleSaleOffers") or {}
    ).get("nodes") or []

    prices = []

    for offer in nodes:
        sender = offer.get("senderSide") or {}

        for listed in sender.get("anyCards") or []:
            listed_player = listed.get("anyPlayer") or {}

            try:
                same_season = int(listed.get("seasonYear")) == season
            except Exception:
                continue

            if not same_season:
                continue

            if norm(listed_player.get("slug")) != slug:
                continue

            if norm(listed.get("rarityTyped")) != rarity:
                continue

            price = amount_to_eur(
                (offer.get("receiverSide") or {}).get("amounts")
            )

            if price is not None:
                prices.append(price)

            break

    print(
        f"📊 Listing trovate: {len(prices)}/{MIN_LISTINGS}",
        flush=True
    )

    return min(prices) if len(prices) >= MIN_LISTINGS else None


# ============================================================
# VALIDATION
# ============================================================

def is_kulenovic(card):
    return (
        norm(card.get("slug")) == norm(KULENOVIC_SLUG)
        or norm(card.get("assetId")) == norm(KULENOVIC_ASSET)
    )


def validate(card):
    if is_kulenovic(card):
        return False, "KULENOVIC"

    if norm(card.get("rarityTyped")).upper() != "LIMITED":
        return False, "RARITY"

    floor = get_floor(card)

    if floor is None:
        return False, "FLOOR_UNKNOWN"

    if floor < MIN_PRICE:
        return False, "FLOOR_LOW"

    if floor > MAX_PRICE:
        return False, "FLOOR_HIGH"

    return True, floor


# ============================================================
# SOLANA BASE58
# ============================================================

def base58_decode(value):
    value = str(value).strip()
    if not value:
        raise ValueError("Base58 vuoto")

    number = 0

    for char in value:
        index = B58.find(char)
        if index < 0:
            raise ValueError(f"Carattere Base58 non valido: {char}")
        number = number * 58 + index

    raw = number.to_bytes(
        max(1, (number.bit_length() + 7) // 8),
        "big"
    )

    zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * zeros + raw.lstrip(b"\x00")


def base58_encode(data):
    data = bytes(data)

    if not data:
        return ""

    number = int.from_bytes(data, "big")
    chars = []

    while number:
        number, rem = divmod(number, 58)
        chars.append(B58[rem])

    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(chars))


def solana_key_info():
    if not SOLANA:
        raise RuntimeError("SORARE_SOLANA_PRIVATE_KEY mancante")

    value = SOLANA.strip()

    try:
        decoded = base58_decode(value)
        if len(decoded) in {32, 64}:
            return decoded
    except Exception:
        pass

    hex_value = value[2:] if value.startswith("0x") else value

    if len(hex_value) % 2 == 0 and all(
        c in "0123456789abcdefABCDEF" for c in hex_value
    ):
        decoded = bytes.fromhex(hex_value)
        if len(decoded) in {32, 64}:
            return decoded

    raise RuntimeError(
        "SORARE_SOLANA_PRIVATE_KEY non valida"
    )


# ============================================================
# FIRMA
# ============================================================

def sign_authorizations(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    js = r'''
const crypto = require("crypto");
const { signAuthorizationRequest } = require("@sorare/crypto");
const {
    createSignableMessage,
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair
} = require("@solana/kit");

const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const ALPHABET =
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ" +
    "abcdefghijkmnopqrstuvwxyz";

function b58decode(value) {
    let n = 0n;

    for (const c of String(value).trim()) {
        const i = ALPHABET.indexOf(c);
        if (i < 0) throw new Error("Base58 non valido");
        n = n * 58n + BigInt(i);
    }

    let hex = n.toString(16);
    if (hex.length % 2) hex = "0" + hex;

    let b = hex === "0"
        ? Buffer.alloc(0)
        : Buffer.from(hex, "hex");

    let zeros = 0;
    for (const c of value) {
        if (c !== "1") break;
        zeros++;
    }

    return new Uint8Array(
        Buffer.concat([Buffer.alloc(zeros), b])
    );
}

function b58encode(bytes) {
    const data = Buffer.from(bytes);
    let n = 0n;

    for (const b of data)
        n = n * 256n + BigInt(b);

    let out = "";

    while (n > 0n) {
        const r = Number(n % 58n);
        out = ALPHABET[r] + out;
        n /= 58n;
    }

    let zeros = 0;
    for (const b of data) {
        if (b !== 0) break;
        zeros++;
    }

    return "1".repeat(zeros) + out;
}

function parseKey(value) {
    const clean = String(value || "").trim();

    try {
        const b = b58decode(clean);
        if (b.length === 32 || b.length === 64) return b;
    } catch (_) {}

    let hex = clean.startsWith("0x")
        ? clean.slice(2)
        : clean;

    if (
        /^[0-9a-fA-F]+$/.test(hex) &&
        hex.length % 2 === 0
    ) {
        const b = new Uint8Array(Buffer.from(hex, "hex"));
        if (b.length === 32 || b.length === 64) return b;
    }

    throw new Error("Chiave Solana non riconosciuta");
}

async function solanaSigner(privateKey) {
    let key = parseKey(privateKey);

    if (key.length === 64)
        key = key.slice(0, 32);

    const kp = await createKeyPairFromPrivateKeyBytes(key);
    return createSignerFromKeyPair(kp);
}

async function signSolana(auth) {
    const req = auth.request;
    const signer = await solanaSigner(input.solanaPrivateKey);

    console.error("🔑 Solana signer → " + signer.address);
    console.error("🎯 senderAddress → " + req.senderAddress);

    if (signer.address !== req.senderAddress) {
        throw new Error(
            "La chiave Solana non corrisponde al senderAddress"
        );
    }

    const message = [
        "TRANSFER",
        req.transferProxyProgramAddress,
        req.merkleTreeAddress,
        String(req.leafIndex),
        req.nonce,
        String(req.expirationTimestamp),
        req.receiverAddress,
        "0x",
        req.originator
    ].join(":");

    console.error("📝 Solana message costruito");

    const hash = crypto
        .createHash("sha256")
        .update(Buffer.from(message, "utf8"))
        .digest();

    const signable = createSignableMessage(
        new Uint8Array(hash)
    );

    const signatures = await signer.signMessages([signable]);
    const signature = b58encode(
        signatures[0][signer.address]
    );

    return {
        fingerprint: auth.fingerprint,
        solanaTokenTransferApproval: {
            signature,
            nonce: req.nonce,
            expirationTimestamp: req.expirationTimestamp
        }
    };
}

function signStark(auth) {
    const req = auth.request;
    const signature = signAuthorizationRequest(
        input.starkPrivateKey,
        req
    );

    const base = {
        fingerprint: auth.fingerprint,
        nonce: req.nonce,
        signature
    };

    if (req.__typename === "StarkexTransferAuthorizationRequest")
        return {
            fingerprint: auth.fingerprint,
            starkexTransferApproval: {
                nonce: req.nonce,
                expirationTimestamp: req.expirationTimestamp,
                signature
            }
        };

    if (req.__typename === "StarkexLimitOrderAuthorizationRequest")
        return {
            fingerprint: auth.fingerprint,
            starkexLimitOrderApproval: {
                nonce: req.nonce,
                expirationTimestamp: req.expirationTimestamp,
                signature
            }
        };

    if (req.__typename === "MangopayWalletTransferAuthorizationRequest")
        return {
            fingerprint: auth.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: req.nonce,
                signature
            }
        };

    throw new Error("Authorization non supportata: " + req.__typename);
}

async function main() {
    const approvals = [];

    for (const auth of input.authorizations) {
        const type = (auth.request || {}).__typename;

        console.error("🔐 Authorization → " + type);

        approvals.push(
            type === "SolanaTokenTransferAuthorizationRequest"
                ? await signSolana(auth)
                : signStark(auth)
        );
    }

    process.stdout.write(JSON.stringify(approvals));
}

main().catch(e => {
    console.error(e.stack || e);
    process.exit(1);
});
'''

    process = subprocess.run(
        [node, "-e", js],
        input=json.dumps({
            "starkPrivateKey": STARK,
            "solanaPrivateKey": SOLANA,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if process.stderr:
        print(process.stderr.strip(), flush=True)

    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "Firma fallita")

    try:
        return json.loads(process.stdout)
    except Exception:
        raise RuntimeError(
            "Output firma non valido: " + process.stdout[:1000]
        )


# ============================================================
# PREPARE
# ============================================================

PREPARE_QUERY = """
mutation PrepareOffer($input: prepareOfferInput!) {
    prepareOffer(input: $input) {
        authorizations {
            fingerprint
            request {
                __typename

                ... on StarkexTransferAuthorizationRequest {
                    amount
                    condition
                    expirationTimestamp
                    nonce
                    receiverPublicKey
                    receiverVaultId
                    senderVaultId
                    token
                    feeInfoUser {
                        feeLimit
                        sourceVaultId
                        tokenId
                    }
                }

                ... on StarkexLimitOrderAuthorizationRequest {
                    vaultIdSell
                    vaultIdBuy
                    amountSell
                    amountBuy
                    tokenSell
                    tokenBuy
                    nonce
                    expirationTimestamp
                    feeInfo {
                        feeLimit
                        tokenId
                        sourceVaultId
                    }
                }

                ... on MangopayWalletTransferAuthorizationRequest {
                    nonce
                    amount
                    currency
                    operationHash
                    mangopayWalletId
                }

                ... on SolanaTokenTransferAuthorizationRequest {
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
        errors {
            message
        }
    }
}
"""


def prepare_sale(asset, price):
    base = {
        "sendAssetIds": [asset],
        "receiveAssetIds": [],
        "settlementCurrencies": ["EUR"],
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    for payload in (
        {**base, "type": "SINGLE_SALE_OFFER"},
        base
    ):
        data = gql(PREPARE_QUERY, {"input": payload})
        result = ((data or {}).get("data") or {}).get("prepareOffer")

        if result is not None:
            break

        print(
            "🔁 prepareOffer: retry schema compatibile",
            flush=True
        )

    if not result:
        print("❌ prepareOffer: nessun risultato", flush=True)
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(errors, ensure_ascii=False),
            flush=True
        )
        return None

    auths = result.get("authorizations") or []

    if not auths:
        print("❌ prepareOffer: nessuna authorization", flush=True)
        return None

    print(f"✅ Authorization ricevute: {len(auths)}", flush=True)
    return auths


# ============================================================
# CREATE SALE
# ============================================================

def create_sale(card, price):
    asset = asset_id(card)

    if not asset:
        return None, False

    if DRY_RUN:
        print(
            f"🟡 DRY RUN → {label(card)} → {eur(price)}",
            flush=True
        )
        return "DRY-RUN", False

    authorizations = prepare_sale(asset, price)

    if not authorizations:
        return None, False

    try:
        approvals = sign_authorizations(authorizations)
    except Exception as e:
        print(f"❌ Firma: {e}", flush=True)
        return None, False

    query = """
        mutation CreateSale($input: createSingleSaleOfferInput!) {
            createSingleSaleOffer(input: $input) {
                tokenOffer {
                    id
                    startDate
                    endDate
                }
                errors {
                    message
                }
            }
        }
    """

    create_input = {
        "approvals": approvals,
        "dealId": new_id(),
        "assetId": asset,
        "receiveAmount": {
            "amount": str(price),
            "currency": "EUR"
        },
        "clientMutationId": new_id()
    }

    data = gql(query, {"input": create_input})

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        print("❌ createSingleSaleOffer: nessun risultato", flush=True)
        return None, False

    errors = result.get("errors") or []

    if errors:
        text = json.dumps(errors, ensure_ascii=False)

        print(
            f"❌ createSingleSaleOffer: {text}",
            flush=True
        )

        # ----------------------------------------------------
        # FIX IMPORTANTE:
        # Sorare dice che esiste già un'offerta pubblica.
        # La carta È GIÀ IN VENDITA.
        # Non va rimessa DA_VENDERE.
        # ----------------------------------------------------
        if "active public offer already exists" in text.lower():
            print(
                "✅ OFFERTA GIÀ ATTIVA → carta marcata SELLING",
                flush=True
            )
            return "ALREADY_SELLING", True

        return None, False

    offer = result.get("tokenOffer") or {}
    offer_id = offer.get("id")

    if not offer_id:
        print(
            "❌ Vendita non creata: offer ID assente",
            flush=True
        )
        return None, False

    print(
        f"✅ INSERZIONE CREATA → {offer_id}",
        flush=True
    )

    return offer_id, True


# ============================================================
# PROCESS
# ============================================================

def process(card):
    asset = asset_id(card)

    print(f"\n💰 AUTOSELL CHECK → {asset}", flush=True)

    details = card_details(asset)

    if not details:
        print("❌ Carta non recuperabile", flush=True)
        update_card(asset, "da_vendere", error="CARD_DETAILS")
        return

    print(
        f"🃏 {details.get('name') or details.get('slug')} "
        f"{details.get('seasonYear')} • "
        f"{norm(details.get('rarityTyped'))}",
        flush=True
    )

    ok, result = validate(details)

    if not ok:
        messages = {
            "KULENOVIC": "KULENOVIC PROTETTO",
            "RARITY": "RARITÀ NON LIMITED",
            "FLOOR_UNKNOWN": "FLOOR NON DISPONIBILE",
            "FLOOR_LOW": "FLOOR SOTTO €0.32",
            "FLOOR_HIGH": "FLOOR SOPRA €0.70"
        }

        print(
            f"🚫 ESCLUSA → {messages.get(result, result)}",
            flush=True
        )

        update_card(
            asset,
            "BLOCKED" if result in {"KULENOVIC", "RARITY"} else "da_vendere",
            error=result
        )
        return

    price = result

    print(
        f"✅ CARTA VALIDA → floor {eur(price)}",
        flush=True
    )

    if not update_card(asset, "SELLING", error=None):
        print("❌ Impossibile impostare SELLING", flush=True)
        return

    offer_id, success = create_sale(details, price)

    # --------------------------------------------------------
    # Carta già in vendita su Sorare.
    # NON fare rollback.
    # --------------------------------------------------------

    if success and offer_id == "ALREADY_SELLING":
        update_card(
            asset,
            status="SELLING",
            error=None
        )

        print(
            "🟢 Carta già in vendita → nessun nuovo tentativo",
            flush=True
        )
        return

    if not success or not offer_id:
        update_card(
            asset,
            status="da_vendere",
            error="CREATE_SALE_FAILED"
        )

        print(
            "🔁 Carta rimessa in DA_VENDERE",
            flush=True
        )
        return

    update_card(
        asset,
        status="SELLING",
        offer_id=offer_id,
        error=None
    )

    print(
        f"🎉 AUTOSELL COMPLETATO → "
        f"{label(details)} | {eur(price)} | {offer_id}",
        flush=True
    )


# ============================================================
# RECOVERY
# ============================================================

def recovery():
    selling = [
        c for c in get_cards()
        if norm(c.get("status")) == "selling"
    ]

    if not selling:
        print(
            "🔄 Recovery: nessuna carta SELLING.",
            flush=True
        )
        return

    print(
        f"🔄 Recovery: {len(selling)} carte SELLING",
        flush=True
    )

    for card in selling:
        print(
            f"   └─ {asset_id(card)} | "
            f"offer={card.get('sale_offer_id')}",
            flush=True
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print("🤖 AUTOSELL AVVIATO", flush=True)
    print(f"📦 VERSIONE: {VERSION}", flush=True)
    print(f"🧪 DRY_RUN={DRY_RUN}", flush=True)
    print("💰 RANGE: €0.32 - €0.70", flush=True)
    print(f"📊 LISTING MINIME: {MIN_LISTINGS}", flush=True)
    print("🎂 ETÀ: NON UTILIZZATA", flush=True)
    print("🔒 KULENOVIC: MAI VENDUTO", flush=True)
    print("🛡️ COVERAGE: DISABILITATA", flush=True)
    print("🛡️ SOURCE: AUTOBUY / SWAP", flush=True)
    print(f"💾 STORAGE: {STATE_FILE}", flush=True)

    try:
        auth_headers()
        ensure_state()
    except Exception as e:
        print(f"❌ Configurazione: {e}", flush=True)
        return

    if not check_account():
        return

    recovery()

    while True:
        try:
            cards = sellable_cards()

            print(
                f"🗄️ Carte DA VENDERE: {len(cards)}",
                flush=True
            )

            for card in cards:
                try:
                    process(card)
                except Exception as e:
                    asset = asset_id(card)
                    print(
                        f"❌ AutoSell {asset}: {e}",
                        flush=True
                    )

                    if asset:
                        update_card(
                            asset,
                            status="da_vendere",
                            error=str(e)
                        )

            time.sleep(INTERVAL)

        except Exception as e:
            print(f"❌ Worker: {e}", flush=True)
            time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    cards = get_cards()

    return jsonify({
        "status": "online",
        "bot": "autosell",
        "version": VERSION,
        "dry_run": DRY_RUN,
        "range": "€0.32-€0.70",
        "min_live_listings": MIN_LISTINGS,
        "rarity": "LIMITED",
        "age": "NOT_USED",
        "kulenovic": "NEVER_SELL",
        "coverage": "DISABLED",
        "storage": STATE_FILE,
        "cards": len(cards),
        "da_vendere": sum(
            1 for c in cards
            if norm(c.get("status")) in {"da_vendere", "ready"}
        ),
        "selling": sum(
            1 for c in cards
            if norm(c.get("status")) == "selling"
        ),
        "worker": worker_started
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "autosell",
        "version": VERSION,
        "worker": worker_started,
        "dry_run": DRY_RUN
    })


@app.get("/cards")
def cards_endpoint():
    cards = get_cards()
    return jsonify({
        "count": len(cards),
        "cards": cards
    })


# ============================================================
# MAIN
# ============================================================

def start_worker():
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            daemon=True,
            name="autosell-worker"
        ).start()

        print("✅ Thread AutoSell avviato.", flush=True)


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        debug=False
    )
