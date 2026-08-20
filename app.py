import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import requests

from flask import Flask, jsonify

app = Flask(__name__)

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"

MIN_PRICE = 30
MAX_PRICE = 80
PAY_PER_CARD = 20
INTERVAL = 10
TIMEOUT = 30

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# Slug accettati dei campionati
CAMPIONATI = {
    "english-league": "English League",
    "premier-league-eng": "English League",
    "premier-league": "English League",

    "ligue-1-fr": "Ligue 1",
    "ligue-1": "Ligue 1",

    "laliga-es": "LALIGA EA SPORTS",
    "laliga": "LALIGA EA SPORTS",
    "la-liga": "LALIGA EA SPORTS",
    "laliga-ea-sports": "LALIGA EA SPORTS",

    "bundesliga-de": "Bundesliga",
    "bundesliga": "Bundesliga",

    "liga-portugal": "Liga Portugal",
    "primeira-liga-pt": "Liga Portugal",
    "liga-portugal-pt": "Liga Portugal",

    "eredivisie-nl": "Eredivisie",
    "eredivisie": "Eredivisie",

    "jupiler-pro-league-be": "Jupiler Pro League",
    "jupiler-pro-league": "Jupiler Pro League",

    "scottish-premiership-sco": "Scottish Premiership",
    "scottish-premiership": "Scottish Premiership",

    "jleague-jp": "J.League",
    "j1-league-jp": "J.League",
    "j-league": "J.League",
    "j1-league": "J.League",

    "second-division-eng": "Seconda divisione inglese",
    "championship-eng": "Seconda divisione inglese",
    "english-championship": "Seconda divisione inglese",
    "championship": "Seconda divisione inglese",

    "austrian-bundesliga-at": "Austrian Bundesliga",
    "austrian-bundesliga": "Austrian Bundesliga",
    "bundesliga-at": "Austrian Bundesliga",

    "croatian-hnl-hr": "Croatian HNL",
    "croatian-first-league-hr": "Croatian HNL",
    "croatian-first-league": "Croatian HNL",
    "croatian-hnl": "Croatian HNL",
    "supersport-hnl": "Croatian HNL",

    "2-bundesliga-de": "2. Bundesliga",
    "2-bundesliga": "2. Bundesliga",

    "ligue-2-fr": "Ligue 2",
    "ligue-2": "Ligue 2",

    "mls-us": "MLS",
    "major-league-soccer-us": "MLS",
    "major-league-soccer": "MLS",
    "mls": "MLS",

    "k-league-1-kr": "K League",
    "k-league-1": "K League",
    "k-league": "K League",

    "super-lig-tr": "Turchia",
    "super-lig": "Turchia",
    "turkish-super-lig": "Turchia",

    "superliga-dk": "Danimarca",
    "superliga": "Danimarca",
    "danish-superliga": "Danimarca",

    "serie-a-it": "Serie A",
    "serie-a": "Serie A",

    "serie-b-it": "Serie B",
    "serie-b": "Serie B",

    "brasileirao-serie-a-br": "Brasile",
    "brasileirao-serie-a": "Brasile",
    "brasileirao": "Brasile",
    "serie-a-br": "Brasile",

    "premier-liga-ru": "Russia",
    "russian-premier-league": "Russia",
    "premier-liga": "Russia",
    "russia-premier-league": "Russia",

    "liga-1-peru": "Perù",
    "liga-1-pe": "Perù",
    "peruvian-primera-division": "Perù",

    "primera-a-colombia": "Colombia",
    "liga-betplay-col": "Colombia",
    "primera-a": "Colombia",
    "liga-betplay": "Colombia",

    "liga-mx": "Messico",

    "laliga-2-es": "LALIGA 2",
    "laliga-hypermotion": "LALIGA 2",
    "laliga-2": "LALIGA 2",
    "segunda-division-spain": "LALIGA 2",
}

processed = set()
lock = threading.Lock()
_worker_started = False


def slug(value):
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )


def auth_headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato")

    token = TOKEN
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-Bot/9.0",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):
    for attempt in range(1, 4):
        try:
            r = requests.post(
                URL,
                json={"query": query, "variables": variables or {}},
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(f"🌐 Sorare HTTP {r.status_code}", flush=True)

            if r.status_code == 429:
                try:
                    wait = int(
                        r.headers.get("Retry-After", attempt * 3)
                    )
                except (TypeError, ValueError):
                    wait = attempt * 3

                print(f"⏳ Rate limit: {wait}s", flush=True)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: {r.text[:500]}",
                    flush=True,
                )
                time.sleep(attempt)
                continue

            data = r.json()

            if data.get("errors"):
                for error in data["errors"]:
                    print(
                        "❌ GraphQL:",
                        error.get("message"),
                        flush=True,
                    )
                return None

            return data

        except Exception as error:
            print(f"❌ GraphQL: {error}", flush=True)
            time.sleep(attempt)

    return None


def check_account():
    data = graphql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:
        print("❌ Account Sorare non verificato", flush=True)
        return False

    print(
        f"✅ Sorare: {user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    return True


def get_offers():
    data = graphql("""
        query {
            currentUser {
                pendingTokenOffersReceived(first: 50) {
                    nodes {
                        id
                        blockchainId
                        status

                        sender {
                            ... on User {
                                slug
                                nickname
                            }
                        }

                        senderSide {
                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }

                        receiverSide {
                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }
                    }
                }
            }
        }
    """)

    return (
        (((data or {}).get("data") or {})
        .get("currentUser") or {})
        .get("pendingTokenOffersReceived") or {}
    ).get("nodes") or []


def card_details(asset_ids):
    ids = list(dict.fromkeys(
        str(x).strip() for x in asset_ids if x
    ))

    if not ids:
        return []

    data = graphql("""
        query Cards($assetIds: [String!]) {
            anyCards(assetIds: $assetIds) {
                assetId
                slug
                name
                rarityTyped

                anyPlayer {
                    displayName

                    activeClub {
                        slug
                        name

                        activeCompetitions {
                            slug
                        }
                    }
                }

                lowestPriceCard {
                    liveSingleSaleOffer {
                        receiverSide {
                            amounts {
                                eurCents
                            }
                        }
                    }

                    publicMinPrices {
                        eurCents
                    }
                }

                lowestPriceCardAnySeason {
                    liveSingleSaleOffer {
                        receiverSide {
                            amounts {
                                eurCents
                            }
                        }
                    }

                    publicMinPrices {
                        eurCents
                    }
                }
            }
        }
    """, {"assetIds": ids})

    return (
        ((data or {}).get("data") or {})
        .get("anyCards") or []
    )


def card_price(card):
    values = []

    for source in (
        card.get("lowestPriceCard") or {},
        card.get("lowestPriceCardAnySeason") or {},
    ):
        try:
            live = (
                source
                .get("liveSingleSaleOffer", {})
                .get("receiverSide", {})
                .get("amounts", {})
                .get("eurCents")
            )

            if live:
                values.append(int(live))
        except (TypeError, ValueError, AttributeError):
            pass

        public = source.get("publicMinPrices") or []

        if isinstance(public, dict):
            public = [public]

        for item in public:
            try:
                value = int(item.get("eurCents"))

                if value > 0:
                    values.append(value)

            except (TypeError, ValueError, AttributeError):
                pass

    return min(values) if values else None


def is_kulenovic(card):
    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(KID.lower())

    asset = str(card.get("assetId") or "").lower()
    card_slug = str(card.get("slug") or "").lower()

    return asset in wanted or card_slug in wanted


def valid_card(card):
    name = card.get("name") or card.get("slug") or "Carta"
    rarity = str(card.get("rarityTyped") or "").upper()
    price = card_price(card)

    print(f"   📄 {name}", flush=True)

    if price is None:
        print("      ❌ Prezzo non disponibile", flush=True)
        return False

    print(
        f"      💰 Floor €{price / 100:.2f}",
        flush=True,
    )

    if not MIN_PRICE <= price <= MAX_PRICE:
        print("      ❌ Prezzo fuori range", flush=True)
        return False

    if rarity != "LIMITED":
        print(f"      ❌ Rarità: {rarity}", flush=True)
        return False

    player = card.get("anyPlayer") or {}
    club = player.get("activeClub")

    if not club:
        print("      ❌ Nessuna squadra", flush=True)
        return False

    covered = []

    for competition in club.get("activeCompetitions") or []:
        cslug = slug(competition.get("slug"))

        if cslug in CAMPIONATI:
            covered.append(CAMPIONATI[cslug])

    if not covered:
        print("      ❌ Campionato non coperto", flush=True)
        return False

    leagues = ", ".join(dict.fromkeys(covered))

    print(
        f"      ✅ LIMITED / €{price / 100:.2f} / {leagues}",
        flush=True,
    )

    return True


def reject_offer(offer):
    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:
        print("❌ blockchainId mancante", flush=True)
        return False

    if DRY_RUN:
        print("🟡 DRY RUN: rifiuto simulato", flush=True)
        return True

    data = graphql("""
        mutation Reject($input: rejectOfferInput!) {
            rejectOffer(input: $input) {
                tokenOffer {
                    id
                    status
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(uuid.uuid4()),
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                "❌ Reject:",
                error.get("message"),
                flush=True,
            )
        return False

    print("✅ Offerta originale rifiutata", flush=True)
    return True


def sign_authorizations(authorizations):
    node = shutil.which("node") or shutil.which("nodejs")

    if not node:
        raise RuntimeError("Node.js non disponibile")

    script = r"""
const fs = require("fs");
const { signAuthorizationRequest } = require("@sorare/crypto");

const input = JSON.parse(fs.readFileSync(0, "utf8"));

function build(a) {
    const r = a.request;

    if (!r) {
        throw new Error("AuthorizationRequest mancante");
    }

    if (
        r.__typename === "StarkexTransferAuthorizationRequest" &&
        r.amount !== undefined &&
        r.amount !== null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature = signAuthorizationRequest(
        input.privateKey,
        r
    );

    if (r.__typename === "StarkexTransferAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (r.__typename === "StarkexLimitOrderAuthorizationRequest") {
        return {
            fingerprint: a.fingerprint,
            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp: r.expirationTimestamp,
                signature
            }
        };
    }

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {
        return {
            fingerprint: a.fingerprint,
            mangopayWalletTransferApproval: {
                nonce: r.nonce,
                signature
            }
        };
    }

    throw new Error("Authorization non supportata");
}

process.stdout.write(
    JSON.stringify(input.authorizations.map(build))
);
"""

    process = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip() or "Firma fallita"
        )

    return json.loads(process.stdout)


def counter_offer(offer, cards):
    sender = offer.get("sender") or {}
    receiver = str(sender.get("slug") or "").strip()

    asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if card.get("assetId")
    ]

    if not receiver or not asset_ids:
        print(
            "❌ Dati controproposta mancanti",
            flush=True,
        )
        return False

    amount = len(asset_ids) * PAY_PER_CARD

    print(
        f"🟢 Controproposta: {len(asset_ids)} carta/e "
        f"→ €{amount / 100:.2f}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: controproposta simulata",
            flush=True,
        )
        return True

    if not STARK:
        print(
            "❌ SORARE_STARK_PRIVATE_KEY mancante",
            flush=True,
        )
        return False

    prepare_query = """
        mutation Prepare($input: prepareOfferInput!) {
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
                    }
                }

                errors {
                    message
                }
            }
        }
    """

    prepare_input = {
        "type": "DIRECT_OFFER",
        "sendAssetIds": [],
        "receiveAssetIds": asset_ids,
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },
        "receiverSlug": receiver,
        "clientMutationId": str(uuid.uuid4()),
    }

    data = graphql(
        prepare_query,
        {"input": prepare_input},
    )

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print("❌ prepareOffer fallito", flush=True)
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                "❌ Prepare:",
                error.get("message"),
                flush=True,
            )
        return False

    authorizations = result.get("authorizations") or []

    if not authorizations:
        print(
            "❌ Nessuna autorizzazione",
            flush=True,
        )
        return False

    try:
        approvals = sign_authorizations(
            authorizations
        )
    except Exception as error:
        print(
            f"❌ Firma: {error}",
            flush=True,
        )
        return False

    create_query = """
        mutation Create($input: createDirectOfferInput!) {
            createDirectOffer(input: $input) {
                tokenOffer {
                    id
                    blockchainId
                    status
                }

                errors {
                    message
                }
            }
        }
    """

    create_input = {
        "approvals": approvals,
        "dealId": str(uuid.uuid4()),
        "sendAssetIds": [],
        "receiveAssetIds": asset_ids,
        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },
        "receiverSlug": receiver,
        "clientMutationId": str(uuid.uuid4()),
    }

    data = graphql(
        create_query,
        {"input": create_input},
    )

    result = (
        ((data or {}).get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        print(
            "❌ createDirectOffer fallito",
            flush=True,
        )
        return False

    errors = result.get("errors") or []

    if errors:
        for error in errors:
            print(
                "❌ Create:",
                error.get("message"),
                flush=True,
            )
        return False

    token_offer = result.get("tokenOffer") or {}
    offer_id = token_offer.get("id")

    if not offer_id:
        print(
            "❌ Nessuna offerta creata",
            flush=True,
        )
        return False

    print(
        f"✅ CONTROPROPOSTA INVIATA: {offer_id}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    return True


def process_offer(offer):
    offer_id = str(offer.get("id") or "")

    if not offer_id:
        return

    with lock:
        if offer_id in processed:
            return
        processed.add(offer_id)

    print(
        f"\n📨 OFFERTA {offer_id}",
        flush=True,
    )

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards") or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards") or []
    )

    # Kulenovic deve essere nella parte che riceviamo.
    if not any(
        is_kulenovic(card)
        for card in receiver_cards
    ):
        print(
            "⏭️ Kulenovic non presente: ignoro",
            flush=True,
        )
        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    ids = [
        card.get("assetId")
        for card in sender_cards
        if card.get("assetId")
    ]

    if not ids:
        print(
            "❌ Nessuna carta offerta",
            flush=True,
        )
        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "❌ Impossibile verificare tutte le carte",
            flush=True,
        )
        return

    print(
        f"🔎 Controllo {len(cards)} carta/e",
        flush=True,
    )

    if not all(valid_card(card) for card in cards):
        print(
            "❌ Offerta non idonea",
            flush=True,
        )
        return

    if not reject_offer(offer):
        print(
            "❌ Impossibile rifiutare l'offerta",
            flush=True,
        )
        return

    counter_offer(offer, cards)


def worker():
    print("🤖 BOT AVVIATO", flush=True)
    print(
        f"💰 Pagamento: €{PAY_PER_CARD / 100:.2f} per carta",
        flush=True,
    )
    print(
        f"📊 Range floor: €{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True,
    )
    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if not check_account():
        print(
            "❌ Account non valido. Worker fermato.",
            flush=True,
        )
        return

    while True:
        try:
            offers = get_offers()

            print(
                f"📨 Offerte pendenti: {len(offers)}",
                flush=True,
            )

            for offer in offers:
                try:
                    process_offer(offer)
                except Exception as error:
                    print(
                        f"❌ Errore offerta: {error}",
                        flush=True,
                    )

            time.sleep(INTERVAL)

        except Exception as error:
            print(
                f"❌ Worker: {error}",
                flush=True,
            )
            time.sleep(INTERVAL)


def start_worker():
    global _worker_started

    with lock:
        if _worker_started:
            return

        _worker_started = True

    thread = threading.Thread(
        target=worker,
        daemon=True,
        name="sorare-worker",
    )
    thread.start()


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "sorare",
        "dry_run": DRY_RUN,
        "pay_per_card_cents": PAY_PER_CARD,
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    start_worker()

    port = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
