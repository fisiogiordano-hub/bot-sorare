import os
import time
import uuid
import json
import requests
import subprocess
import shutil


# ============================================================
# AUTOSSELL — MODULO INDIPENDENTE
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70
SALE_DAYS = 7
INTERVAL = 300
TIMEOUT = 25

DRY_RUN = os.getenv(
    "AUTOSELL_DRY_RUN",
    "true"
).lower() == "true"


# ============================================================
# GRAPHQL
# ============================================================

def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-AutoSell",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):

    try:
        r = requests.post(
            URL,
            json={
                "query": query,
                "variables": variables or {}
            },
            headers=headers(),
            timeout=TIMEOUT
        )

        print(
            f"🌐 Sorare HTTP {r.status_code}",
            flush=True
        )

        if r.status_code != 200:
            print(
                f"❌ HTTP: {r.text[:500]}",
                flush=True
            )
            return None

        data = r.json()

        if data.get("errors"):
            print(
                "❌ GraphQL:",
                json.dumps(
                    data["errors"],
                    ensure_ascii=False
                )[:3000],
                flush=True
            )

        return data

    except Exception as e:
        print(
            f"❌ GraphQL: {e}",
            flush=True
        )
        return None


# ============================================================
# UTILITY
# ============================================================

def norm(value):
    return str(value or "").strip().lower()


def eur(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# LE MIE CARTE
# ============================================================

def get_my_cards():

    data = graphql("""
        query MyCards {
            currentUser {
                cards(first: 100) {
                    nodes {
                        assetId
                        slug
                        name
                        rarityTyped
                        seasonYear

                        anyPlayer {
                            slug
                            displayName
                        }

                        activeSingleSaleOffer {
                            id
                            startDate
                            endDate

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
        }
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    cards = (
        user.get("cards") or {}
    ).get("nodes") or []

    return cards


# ============================================================
# FLOOR LIVE
# ============================================================

def live_floor(card):

    player = card.get("anyPlayer") or {}

    player_slug = norm(
        player.get("slug")
    )

    rarity = norm(
        card.get("rarityTyped")
    )

    try:
        season = int(
            card.get("seasonYear")
        )
    except (TypeError, ValueError):
        return None

    if not player_slug or not rarity:
        return None

    data = graphql("""
        query LiveSales(
            $playerSlug: String,
            $first: Int
        ) {
            tokens {
                liveSingleSaleOffers(
                    playerSlug: $playerSlug
                    first: $first
                ) {
                    nodes {
                        senderSide {
                            anyCards {
                                assetId
                                rarityTyped
                                seasonYear

                                anyPlayer {
                                    slug
                                }
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
    """, {
        "playerSlug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):
        return None

    offers = (
        (
            ((data.get("data") or {})
            .get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        ).get("nodes") or []
    )

    prices = []

    for offer in offers:

        cards = (
            (offer.get("senderSide") or {})
            .get("anyCards") or []
        )

        for c in cards:

            if (
                norm(
                    (c.get("anyPlayer") or {})
                    .get("slug")
                ) != player_slug
            ):
                continue

            if (
                norm(c.get("rarityTyped"))
                != rarity
            ):
                continue

            try:
                c_season = int(
                    c.get("seasonYear")
                )
            except (TypeError, ValueError):
                continue

            if c_season != season:
                continue

            amounts = (
                (offer.get("receiverSide") or {})
                .get("amounts") or {}
            )

            price = eur(
                amounts.get("eurCents")
            )

            if price and price > 0:
                prices.append(price)

            break

    if not prices:
        return None

    return min(prices)


# ============================================================
# FILTRO AUTOSELL
# ============================================================

def eligible(card):

    rarity = norm(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":
        return False, "rarità diversa da LIMITED"

    floor = live_floor(card)

    if floor is None:
        return False, "floor live non disponibile"

    if floor < MIN_PRICE:
        return (
            False,
            f"floor {floor / 100:.2f} sotto €0.32"
        )

    if floor > MAX_PRICE:
        return (
            False,
            f"floor {floor / 100:.2f} sopra €0.70"
        )

    return True, floor


# ============================================================
# FIRMA
# ============================================================

def sign_authorizations(authorizations):

    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    if not STARK:
        raise RuntimeError(
            "SORARE_STARK_PRIVATE_KEY "
            "non configurata"
        )

    script = r'''
const fs = require("fs");
const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function sign(a) {

    const r = a.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            r
        );

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {
        return {
            fingerprint: a.fingerprint,

            starkexTransferApproval: {
                nonce: r.nonce,
                expirationTimestamp:
                    r.expirationTimestamp,
                signature
            }
        };
    }

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {
        return {
            fingerprint: a.fingerprint,

            starkexLimitOrderApproval: {
                nonce: r.nonce,
                expirationTimestamp:
                    r.expirationTimestamp,
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

    throw new Error(
        "Tipo authorization non supportato: "
        + r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(sign)
    )
);
'''

    p = subprocess.run(
        [
            node,
            "-e",
            script
        ],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT
    )

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(p.stdout)


# ============================================================
# PREPARE VENDITA
#
# NOTA:
# settlementInfo NON viene passato qui.
# ============================================================

def prepare_sale(asset_id, price):

    data = graphql("""
        mutation PrepareSale(
            $input: prepareOfferInput!
        ) {
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
    """, {
        "input": {
            "type": "SINGLE_SALE_OFFER",

            "sendAssetIds": [
                asset_id
            ],

            "receiveAssetIds": [],

            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },

            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        return None

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return None

    return result.get(
        "authorizations"
    ) or []


# ============================================================
# CREA VENDITA
# ============================================================

def create_sale(card, price, approvals):

    asset_id = card.get("assetId")

    data = graphql("""
        mutation CreateSingleSale(
            $input: createSingleSaleOfferInput!
        ) {
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
    """, {
        "input": {

            "approvals": approvals,

            "dealId": str(
                uuid.uuid4()
            ),

            "assetId": asset_id,

            "receiveAmount": {
                "amount": str(price),
                "currency": "EUR"
            },

            "clientMutationId": str(
                uuid.uuid4()
            )
        }
    })

    result = (
        ((data or {}).get("data") or {})
        .get("createSingleSaleOffer")
    )

    if not result:
        return False

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ createSingleSaleOffer:",
            json.dumps(
                errors,
                ensure_ascii=False
            ),
            flush=True
        )
        return False

    offer = (
        result.get("tokenOffer")
        or {}
    )

    print(
        f"✅ AUTOSELL: {card.get('name')}"
        f" → €{price / 100:.2f}",
        flush=True
    )

    if offer.get("endDate"):
        print(
            f"   └─ Scadenza: "
            f"{offer['endDate']}",
            flush=True
        )

    return True


# ============================================================
# METTI IN VENDITA
# ============================================================

def sell_card(card, price):

    asset_id = card.get("assetId")

    if not asset_id:
        return False

    print(
        f"\n🟢 AUTOSELL: "
        f"{card.get('name') or card.get('slug')}",
        flush=True
    )

    print(
        f"   ├─ Asset: {asset_id}",
        flush=True
    )

    print(
        f"   └─ Floor live: "
        f"€{price / 100:.2f}",
        flush=True
    )

    if DRY_RUN:
        print(
            "🟡 AUTOSELL_DRY_RUN=true "
            "→ vendita NON eseguita",
            flush=True
        )
        return True

    auth = prepare_sale(
        asset_id,
        price
    )

    if not auth:
        print(
            "❌ AUTOSELL: "
            "prepare fallito",
            flush=True
        )
        return False

    try:
        approvals = sign_authorizations(
            auth
        )
    except Exception as e:
        print(
            f"❌ AUTOSELL firma: {e}",
            flush=True
        )
        return False

    return create_sale(
        card,
        price,
        approvals
    )


# ============================================================
# CICLO
# ============================================================

def run_once():

    print(
        "\n🔎 AUTOSELL: controllo carte...",
        flush=True
    )

    cards = get_my_cards()

    print(
        f"📦 Carte trovate: {len(cards)}",
        flush=True
    )

    for card in cards:

        offer = card.get(
            "activeSingleSaleOffer"
        )

        # Carta ancora in vendita:
        # non viene toccata.
        if offer:
            continue

        ok, result = eligible(card)

        if not ok:
            print(
                f"🚫 AUTOSELL: "
                f"{card.get('name') or card.get('slug')} "
                f"→ {result}",
                flush=True
            )
            continue

        floor = result

        sell_card(
            card,
            floor
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "🤖 AUTOSELL AVVIATO",
        flush=True
    )

    print(
        f"💰 Range: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"⏳ Durata prevista: "
        f"{SALE_DAYS} giorni",
        flush=True
    )

    print(
        f"🧪 DRY_RUN: {DRY_RUN}",
        flush=True
    )

    while True:

        try:
            run_once()

        except Exception as e:
            print(
                f"❌ AUTOSELL: {e}",
                flush=True
            )

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
