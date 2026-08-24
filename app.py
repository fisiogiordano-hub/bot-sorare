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


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"

TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    ""
).strip()

AUD = os.getenv(
    "SORARE_JWT_AUD",
    ""
).strip()

STARK = os.getenv(
    "SORARE_STARK_PRIVATE_KEY",
    ""
).strip()

KID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()

DRY_RUN = (
    os.getenv("DRY_RUN", "false")
    .strip()
    .lower()
    == "true"
)

# ============================================================
# FILTRI CARTE
# ============================================================

# €0.30
MIN_PRICE = 30

# €0.80
MAX_PRICE = 80

# €0.20 per carta
PAY_PER_CARD = 20

# Deve essere MENO di 28
MAX_AGE = 28


# ============================================================
# WORKER
# ============================================================

INTERVAL = 10
TIMEOUT = 30


# ============================================================
# KULENOVIC
# ============================================================

KSLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATO
# ============================================================

processed = set()

state_lock = threading.Lock()

_worker_started = False
_worker_lock = threading.Lock()


# ============================================================
# UTILITY
# ============================================================

def slug(value):

    value = str(value or "").strip().lower()

    value = value.replace("_", "-")
    value = value.replace(" ", "-")
    value = value.replace("’", "")
    value = value.replace("'", "")

    while "--" in value:
        value = value.replace("--", "-")

    return value


# ============================================================
# AUTH
# ============================================================

def auth_headers():

    if not TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):

        token = "Bearer " + token

    headers = {

        "Authorization": token,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "User-Agent":
            "Sorare-Bot/14.0",
    }

    if AUD:

        headers["JWT-AUD"] = AUD

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):

    payload = {

        "query": query,

        "variables":
            variables or {},
    }

    for attempt in range(1, 4):

        try:

            response = requests.post(

                URL,

                json=payload,

                headers=auth_headers(),

                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                try:

                    wait = int(
                        response.headers.get(
                            "Retry-After",
                            attempt * 3,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    wait = attempt * 3

                print(
                    f"⏳ Rate limit: "
                    f"attendo {wait}s",
                    flush=True,
                )

                time.sleep(wait)

                continue

            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    f"❌ HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:1500]}",
                    flush=True,
                )

                time.sleep(attempt)

                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                data = response.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )

                return None

            # ------------------------------------------------
            # GRAPHQL ERRORS
            # ------------------------------------------------

            errors = data.get("errors") or []

            if errors:

                print(
                    "❌ GraphQL ERROR:",
                    flush=True,
                )

                for error in errors:

                    print(
                        json.dumps(
                            error,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                return data

            return data

        except requests.RequestException as error:

            print(
                f"❌ HTTP: {error}",
                flush=True,
            )

            time.sleep(attempt)

        except Exception as error:

            print(
                f"❌ GraphQL: {error}",
                flush=True,
            )

            return None

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql(
        """
        query CurrentUser {

            currentUser {

                slug

                nickname

                starkKey
            }
        }
        """
    )

    user = (

        ((data or {}).get("data") or {})

        .get("currentUser")
    )

    if not user:

        print(
            "❌ Account Sorare "
            "non verificato",
            flush=True,
        )

        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    return True


# ============================================================
# OFFERTE PENDENTI
# ============================================================

def get_offers():

    data = graphql(
        """
        query PendingOffers {

            currentUser {

                pendingTokenOffersReceived(
                    first: 50
                ) {

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
        """
    )

    return (

        (

            ((data or {}).get("data") or {})

            .get("currentUser")

            or {}
        )

        .get("pendingTokenOffersReceived")

        or {}
    ).get("nodes") or []


# ============================================================
# DETTAGLI CARTE
# ============================================================

def card_details(asset_ids):

    asset_ids = list(
        dict.fromkeys(

            str(value).strip()

            for value in asset_ids

            if value
        )
    )

    if not asset_ids:

        return []

    data = graphql(
        """
        query Cards(
            $assetIds: [String!]
        ) {

            anyCards(
                assetIds: $assetIds
            ) {

                assetId

                slug

                name

                rarityTyped

                anyPlayer {

                    displayName

                    age

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
        """,
        {
            "assetIds":
                asset_ids
        },
    )

    return (

        ((data or {}).get("data") or {})

        .get("anyCards")

        or []
    )


# ============================================================
# PREZZO
# ============================================================

def card_price(card):

    values = []

    for source_name in (

        "lowestPriceCard",

        "lowestPriceCardAnySeason",
    ):

        source = (

            card.get(source_name)

            or {}
        )

        # ----------------------------------------------------
        # LIVE SINGLE SALE
        # ----------------------------------------------------

        try:

            live = (

                source

                .get(
                    "liveSingleSaleOffer",
                    {},
                )

                .get(
                    "receiverSide",
                    {},
                )

                .get(
                    "amounts",
                    {},
                )

                .get(
                    "eurCents"
                )
            )

            if live:

                values.append(
                    int(live)
                )

        except (
            TypeError,
            ValueError,
            AttributeError,
        ):

            pass

        # ----------------------------------------------------
        # PUBLIC MIN PRICES
        # ----------------------------------------------------

        public = (

            source.get(
                "publicMinPrices"
            )

            or []
        )

        if isinstance(
            public,
            dict,
        ):

            public = [public]

        for item in public:

            try:

                value = int(
                    item.get(
                        "eurCents"
                    )
                )

                if value > 0:

                    values.append(
                        value
                    )

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):

                pass

    if not values:

        return None

    return min(values)


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {

        KSLUG.lower(),

        KASSET.lower(),
    }

    if KID:

        wanted.add(
            KID.lower()
        )

    asset_id = str(
        card.get("assetId")
        or ""
    ).lower()

    card_slug = str(
        card.get("slug")
        or ""
    ).lower()

    return (

        asset_id in wanted

        or

        card_slug in wanted
    )


# ============================================================
# COMPETIZIONI
#
# QUALSIASI activeCompetition Sorare
# viene considerata valida.
# ============================================================

def get_competitions(card):

    player = (

        card.get("anyPlayer")

        or {}
    )

    club = player.get(
        "activeClub"
    )

    if not isinstance(
        club,
        dict,
    ):

        return []

    competitions = (

        club.get(
            "activeCompetitions"
        )

        or []
    )

    result = []

    for competition in competitions:

        if not isinstance(
            competition,
            dict,
        ):

            continue

        raw = (

            competition.get(
                "slug"
            )

            or ""
        )

        normalized = slug(raw)

        if not normalized:

            continue

        result.append(
            normalized
        )

    return list(
        dict.fromkeys(result)
    )


def check_competition(card):

    player = (

        card.get("anyPlayer")

        or {}
    )

    club = player.get(
        "activeClub"
    )

    if not isinstance(
        club,
        dict,
    ):

        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )

        return False

    club_name = (

        club.get("name")

        or

        club.get("slug")

        or

        "Sconosciuta"
    )

    print(
        f"      🏟️ Squadra: "
        f"{club_name}",
        flush=True,
    )

    competitions = (
        get_competitions(card)
    )

    if not competitions:

        print(
            "      ❌ Nessuna "
            "activeCompetition",
            flush=True,
        )

        return False

    print(
        "      🏆 Competizioni Sorare:",
        flush=True,
    )

    for competition in competitions:

        print(
            f"         🆕 "
            f"{competition} "
            f"({competition})",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


# ============================================================
# VALIDAZIONE CARTA
# ============================================================

def valid_card(card):

    name = (

        card.get("name")

        or

        card.get("slug")

        or

        "Carta"
    )

    rarity = str(

        card.get("rarityTyped")

        or ""
    ).upper()

    player = (

        card.get("anyPlayer")

        or {}
    )

    age = player.get("age")

    price = card_price(card)

    print(
        f"   📄 {name}",
        flush=True,
    )

    # --------------------------------------------------------
    # ETÀ
    # --------------------------------------------------------

    if age is None:

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return False

    try:

        age = int(age)

    except (
        TypeError,
        ValueError,
    ):

        print(
            f"      ❌ Età non valida: "
            f"{age}",
            flush=True,
        )

        return False

    print(
        f"      🎂 Età: "
        f"{age} anni",
        flush=True,
    )

    if age >= MAX_AGE:

        print(
            f"      ❌ Età troppo alta "
            f"(limite: meno di "
            f"{MAX_AGE})",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    if price is None:

        print(
            "      ❌ Prezzo "
            "non disponibile",
            flush=True,
        )

        return False

    print(
        f"      💰 Floor "
        f"€{price / 100:.2f}",
        flush=True,
    )

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo "
            "fuori range",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # RARITÀ
    # --------------------------------------------------------

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: "
            f"{rarity}",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # COMPETIZIONE
    # --------------------------------------------------------

    if not check_competition(card):

        return False

    # --------------------------------------------------------
    # RISULTATO
    # --------------------------------------------------------

    competitions = (
        get_competitions(card)
    )

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return True


# ============================================================
# RIFIUTO OFFERTA
# ============================================================

def reject_offer(offer):

    blockchain_id = str(

        offer.get(
            "blockchainId"
        )

        or ""
    ).strip()

    if not blockchain_id:

        print(
            "❌ blockchainId mancante",
            flush=True,
        )

        return False

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "rifiuto simulato",
            flush=True,
        )

        return True

    data = graphql(
        """
        mutation Reject(
            $input: rejectOfferInput!
        ) {

            rejectOffer(
                input: $input
            ) {

                tokenOffer {

                    id

                    status
                }

                errors {

                    message
                }
            }
        }
        """,
        {
            "input": {

                "blockchainId":
                    blockchain_id,

                "clientMutationId":
                    str(uuid.uuid4()),
            }
        },
    )

    result = (

        ((data or {}).get("data") or {})

        .get("rejectOffer")
    )

    if not result:

        print(
            "❌ Risposta "
            "rejectOffer vuota",
            flush=True,
        )

        return False

    errors = (

        result.get("errors")

        or []
    )

    if errors:

        for error in errors:

            print(
                "❌ Reject:",
                error.get(
                    "message",
                    "Errore",
                ),
                flush=True,
            )

        return False

    print(
        "✅ Offerta originale "
        "rifiutata",
        flush=True,
    )

    return True


# ============================================================
# FIRMA AUTHORIZATIONS
# ============================================================

def sign_authorizations(
    authorizations
):

    node = (

        shutil.which("node")

        or

        shutil.which("nodejs")
    )

    if not node:

        raise RuntimeError(
            "Node.js non disponibile"
        )

    script = r'''
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);


function build(a) {

    const r = a.request;

    if (!r) {

        throw new Error(
            "AuthorizationRequest mancante"
        );
    }


    // --------------------------------------------------------
    // StarkEx transfer
    // --------------------------------------------------------

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        &&
        r.amount !== undefined
        &&
        r.amount !== null
    ) {

        r.amount = BigInt(
            r.amount
        );
    }


    const signature =
        signAuthorizationRequest(
            input.privateKey,
            r
        );


    // --------------------------------------------------------
    // StarkEx transfer approval
    // --------------------------------------------------------

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            starkexTransferApproval: {

                nonce:
                    r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }


    // --------------------------------------------------------
    // StarkEx limit order approval
    // --------------------------------------------------------

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            starkexLimitOrderApproval: {

                nonce:
                    r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }


    // --------------------------------------------------------
    // Mangopay
    // --------------------------------------------------------

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {

        return {

            fingerprint:
                a.fingerprint,

            mangopayWalletTransferApproval: {

                nonce:
                    r.nonce,

                signature
            }
        };
    }


    throw new Error(
        "Authorization non supportata: "
        + r.__typename
    );
}


process.stdout.write(
    JSON.stringify(
        input.authorizations.map(build)
    )
);
'''

    process = subprocess.run(

        [
            node,
            "-e",
            script,
        ],

        input=json.dumps({

            "privateKey":
                STARK,

            "authorizations":
                authorizations,
        }),

        text=True,

        capture_output=True,

        timeout=TIMEOUT,
    )

    if process.returncode != 0:

        raise RuntimeError(

            process.stderr.strip()

            or

            "Firma fallita"
        )

    try:

        return json.loads(
            process.stdout
        )

    except json.JSONDecodeError as error:

        raise RuntimeError(
            "Risposta firma "
            "non valida"
        ) from error


# ============================================================
# PREPARE DIRECT OFFER
#
# IMPORTANTE:
#
# NON USIAMO PIÙ __type
# NON USIAMO PIÙ introspection
#
# La struttura segue la documentazione
# Sorare per DIRECT_OFFER.
# ============================================================

def build_prepare_direct_offer_input(
    receiver,
    asset_ids,
    amount,
):

    payload = {

        "type":
            "DIRECT_OFFER",

        # Kulenovic NON viene ceduto
        "sendAssetIds":
            [],

        # Carte ricevute dal bot
        "receiveAssetIds":
            asset_ids,

        # Quanto paga il bot
        "sendAmount": {

            "amount":
                str(amount),

            "currency":
                "EUR",
        },

        # Mittente dell'offerta originale
        "receiverSlug":
            receiver,

        "clientMutationId":
            str(uuid.uuid4()),
    }

    print(
        "📦 prepareOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

    return payload


# ============================================================
# CONTROPROPOSTA
# ============================================================

def counter_offer(
    offer,
    cards,
):

    sender = (

        offer.get("sender")

        or {}
    )

    receiver = str(

        sender.get("slug")

        or ""
    ).strip()

    # --------------------------------------------------------
    # ASSET IDS
    # --------------------------------------------------------

    asset_ids = [

        str(
            card.get("assetId")
        ).strip()

        for card in cards

        if (

            isinstance(
                card,
                dict,
            )

            and

            card.get("assetId")
        )
    ]

    if not receiver:

        print(
            "❌ receiverSlug mancante",
            flush=True,
        )

        return False

    if not asset_ids:

        print(
            "❌ Nessuna carta "
            "da ricevere",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # IMPORTO
    # --------------------------------------------------------

    amount = (

        len(asset_ids)

        * PAY_PER_CARD
    )

    print(
        f"🟢 Controproposta: "
        f"{len(asset_ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True,
    )

    print(
        f"👤 Receiver: "
        f"{receiver}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True,
        )

        return True

    # --------------------------------------------------------
    # PRIVATE KEY
    # --------------------------------------------------------

    if not STARK:

        print(
            "❌ "
            "SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    # ========================================================
    # PREPARE
    # ========================================================

    try:

        prepare_input = (

            build_prepare_direct_offer_input(

                receiver=receiver,

                asset_ids=asset_ids,

                amount=amount,
            )
        )

    except Exception as error:

        print(
            f"❌ Errore costruzione "
            f"prepareOfferInput: "
            f"{error}",
            flush=True,
        )

        return False


    # --------------------------------------------------------
    # PREPARE OFFER
    # --------------------------------------------------------

    data = graphql(
        """
        mutation PrepareOffer(
            $input: prepareOfferInput!
        ) {

            prepareOffer(
                input: $input
            ) {

                authorizations {

                    fingerprint

                    request {

                        __typename

                        ... on
                        StarkexTransferAuthorizationRequest {

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

                        ... on
                        StarkexLimitOrderAuthorizationRequest {

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

                        ... on
                        MangopayWalletTransferAuthorizationRequest {

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
        """,
        {
            "input":
                prepare_input
        },
    )


    # ========================================================
    # RISPOSTA PREPARE
    # ========================================================

    if not data:

        print(
            "❌ Nessuna risposta "
            "da prepareOffer",
            flush=True,
        )

        return False


    global_errors = (

        data.get("errors")

        or []
    )

    if global_errors:

        print(
            "❌ prepareOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in global_errors:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False


    result = (

        ((data or {}).get("data") or {})

        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False


    errors = (

        result.get("errors")

        or []
    )

    if errors:

        print(
            "❌ prepareOffer "
            "ERRORI:",
            flush=True,
        )

        for error in errors:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False


    # ========================================================
    # AUTHORIZATIONS
    # ========================================================

    authorizations = (

        result.get(
            "authorizations"
        )

        or []
    )

    if not authorizations:

        print(
            "❌ prepareOffer riuscito "
            "ma nessuna autorizzazione "
            "restituita",
            flush=True,
        )

        return False


    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )


    # ========================================================
    # FIRMA
    # ========================================================

    try:

        approvals = (

            sign_authorizations(
                authorizations
            )
        )

    except Exception as error:

        print(
            f"❌ Firma: {error}",
            flush=True,
        )

        return False


    if not approvals:

        print(
            "❌ Nessuna approval "
            "prodotta",
            flush=True,
        )

        return False


    print(
        f"✍️ Autorizzazioni firmate: "
        f"{len(approvals)}",
        flush=True,
    )


    # ========================================================
    # CREATE DIRECT OFFER
    #
    # NIENTE introspection.
    # ========================================================

    create_input = {

        "approvals":
            approvals,

        "dealId":
            str(uuid.uuid4()),

        "sendAssetIds":
            [],

        "receiveAssetIds":
            asset_ids,

        "sendAmount": {

            "amount":
                str(amount),

            "currency":
                "EUR",
        },

        "receiverSlug":
            receiver,

        "clientMutationId":
            str(uuid.uuid4()),
    }


    print(
        "📦 createDirectOfferInput:",
        flush=True,
    )

    # Non stampiamo le signatures
    # per non sporcare inutilmente il log.
    debug_create = dict(
        create_input
    )

    debug_create["approvals"] = (
        f"{len(approvals)} authorization(s)"
    )

    print(
        json.dumps(
            debug_create,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )


    # ========================================================
    # CREATE DIRECT OFFER
    # ========================================================

    data = graphql(
        """
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {

            createDirectOffer(
                input: $input
            ) {

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
        """,
        {
            "input":
                create_input
        },
    )


    # ========================================================
    # GLOBAL ERRORS
    # ========================================================

    if not data:

        print(
            "❌ Nessuna risposta "
            "da createDirectOffer",
            flush=True,
        )

        return False


    global_errors = (

        data.get("errors")

        or []
    )

    if global_errors:

        print(
            "❌ createDirectOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in global_errors:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False


    # ========================================================
    # RESULT
    # ========================================================

    result = (

        ((data or {}).get("data") or {})

        .get("createDirectOffer")
    )

    if not result:

        print(
            "❌ createDirectOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False


    errors = (

        result.get("errors")

        or []
    )

    if errors:

        print(
            "❌ createDirectOffer "
            "ERRORI:",
            flush=True,
        )

        for error in errors:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False


    # ========================================================
    # TOKEN OFFER
    # ========================================================

    token_offer = (

        result.get(
            "tokenOffer"
        )

        or {}
    )

    offer_id = token_offer.get(
        "id"
    )

    if not offer_id:

        print(
            "❌ Nessuna offerta "
            "creata da Sorare",
            flush=True,
        )

        return False


    # ========================================================
    # SUCCESS
    # ========================================================

    print(
        "========================================",
        flush=True,
    )

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{offer_id}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    return True


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):

    offer_id = str(

        offer.get("id")

        or ""
    ).strip()

    if not offer_id:

        return


    # --------------------------------------------------------
    # EVITA DOPPIO PROCESSAMENTO
    # --------------------------------------------------------

    with state_lock:

        if offer_id in processed:

            return

        processed.add(
            offer_id
        )


    print(
        "\n========================================",
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )


    # --------------------------------------------------------
    # CARTE OFFERTE DAL MITTENTE
    # --------------------------------------------------------

    sender_cards = (

        (

            offer.get(
                "senderSide"
            )

            or {}
        )

        .get("anyCards")

        or []
    )


    # --------------------------------------------------------
    # CARTE DEL BOT
    # --------------------------------------------------------

    receiver_cards = (

        (

            offer.get(
                "receiverSide"
            )

            or {}
        )

        .get("anyCards")

        or []
    )


    # ========================================================
    # KULENOVIC
    # ========================================================

    if not any(

        is_kulenovic(card)

        for card in receiver_cards
    ):

        print(
            "⏭️ Kulenovic non presente: "
            "ignoro",
            flush=True,
        )

        return


    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )


    # ========================================================
    # CARTE OFFERTE
    # ========================================================

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


    # ========================================================
    # DETTAGLI
    # ========================================================

    cards = card_details(
        ids
    )


    if len(cards) != len(ids):

        print(
            "❌ Impossibile verificare "
            "tutte le carte",
            flush=True,
        )

        return


    print(
        f"🔎 Controllo "
        f"{len(cards)} carta/e",
        flush=True,
    )


    # ========================================================
    # VALIDAZIONE
    # ========================================================

    valid_cards = []


    for card in cards:

        try:

            if valid_card(card):

                valid_cards.append(
                    card
                )

        except Exception as error:

            print(
                f"❌ Errore controllo carta: "
                f"{error}",
                flush=True,
            )


    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/"
        f"{len(cards)}",
        flush=True,
    )


    # ========================================================
    # NESSUNA CARTA VALIDA
    # ========================================================

    if not valid_cards:

        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        reject_offer(
            offer
        )

        return


    # ========================================================
    # CARTE ESCLUSE
    # ========================================================

    rejected_count = (

        len(cards)

        -

        len(valid_cards)
    )


    if rejected_count:

        print(
            f"⚠️ {rejected_count} "
            f"carta/e esclusa/e",
            flush=True,
        )


    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    counter_result = (

        counter_offer(

            offer,

            valid_cards,
        )
    )


    if counter_result:

        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

    else:

        print(
            "🔴 Controproposta "
            "NON creata.",
            flush=True,
        )


    # ========================================================
    # RIFIUTO DELL'OFFERTA ORIGINALE
    #
    # Manteniamo il comportamento
    # che avevi richiesto.
    # ========================================================

    if not reject_offer(
        offer
    ):

        print(
            "⚠️ Impossibile "
            "rifiutare l'offerta originale",
            flush=True,
        )


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 BOT AVVIATO",
        flush=True,
    )

    print(
        "📦 VERSIONE BOT: 14.0",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} "
        f"per carta",
        flush=True,
    )

    print(
        f"📊 Range floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True,
    )

    print(
        f"🎂 Età massima: "
        f"meno di {MAX_AGE} anni",
        flush=True,
    )

    print(
        "🏆 COMPETIZIONI: "
        "TUTTE le activeCompetitions "
        "Sorare",
        flush=True,
    )

    print(
        "🔧 PREPARE: "
        "schema fisso DIRECT_OFFER "
        "(NO introspection)",
        flush=True,
    )

    print(
        "🔧 CREATE: "
        "createDirectOffer "
        "(NO introspection)",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )


    # ========================================================
    # ACCOUNT
    # ========================================================

    if not check_account():

        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return


    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True,
            )


            for offer in offers:

                try:

                    process_offer(
                        offer
                    )

                except Exception as error:

                    print(
                        f"❌ Errore offerta: "
                        f"{error}",
                        flush=True,
                    )


            time.sleep(
                INTERVAL
            )


        except Exception as error:

            print(
                f"❌ Worker: "
                f"{error}",
                flush=True,
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# START WORKER
# ============================================================

def start_worker():

    global _worker_started

    with _worker_lock:

        if _worker_started:

            print(
                "ℹ️ Worker già avviato.",
                flush=True,
            )

            return

        _worker_started = True


        thread = threading.Thread(

            target=worker,

            name="sorare-worker",

            daemon=True,
        )

        thread.start()


        print(
            "✅ Thread Sorare avviato.",
            flush=True,
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    return jsonify({

        "status":
            "online",

        "bot":
            "sorare",

        "version":
            "14.0",

        "dry_run":
            DRY_RUN,

        "pay_per_card_cents":
            PAY_PER_CARD,

        "interval_seconds":
            INTERVAL,

        "max_age":
            MAX_AGE,

        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",

        "offer_mode":
            "DIRECT_OFFER_NO_INTROSPECTION",
    })


@app.get("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot":
            "running",

        "version":
            "14.0",
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_worker()

    port = int(

        os.getenv(
            "PORT",
            "10000",
        )
    )

    app.run(

        host="0.0.0.0",

        port=port,
    )
