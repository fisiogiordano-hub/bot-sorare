import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
import requests

from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


MIN_PRICE = 30
MAX_PRICE = 80

# 20 cents per card
PAY_PER_CARD = 20

MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 30


KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# GLOBAL STATE
# ============================================================

processed = set()

state_lock = threading.Lock()

_worker_started = False
_worker_lock = threading.Lock()

_schema_lock = threading.Lock()
_schema_text = None


# ============================================================
# UTILS
# ============================================================

def slug(v):
    v = str(v or "").strip().lower()

    for a, b in [
        ("_", "-"),
        (" ", "-"),
        ("’", ""),
        ("'", ""),
    ]:
        v = v.replace(a, b)

    while "--" in v:
        v = v.replace("--", "-")

    return v


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
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Sorare-Bot/16.0",
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
        "variables": variables or {},
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
                f"🌐 Sorare HTTP {response.status_code}",
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
                except (TypeError, ValueError):
                    wait = attempt * 3

                print(
                    f"⏳ Rate limit: {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            # ------------------------------------------------
            # HTTP ERROR
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    f"❌ HTTP {response.status_code}: "
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

            if data.get("errors"):

                print(
                    "❌ GraphQL ERROR:",
                    flush=True,
                )

                for error in data["errors"]:
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
# LIVE SCHEMA
#
# Sorare ha disabilitato __type/__schema nell'API GraphQL.
# Lo schema pubblico viene invece scaricato da:
#
# https://api.sorare.com/graphql/schema
# ============================================================

def get_live_schema():
    global _schema_text

    with _schema_lock:

        if _schema_text is not None:
            return _schema_text

        try:

            response = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Sorare-Bot/16.0",
                },
            )

            print(
                f"📚 Schema Sorare HTTP "
                f"{response.status_code}",
                flush=True,
            )

            if response.status_code != 200:

                print(
                    "⚠️ Impossibile scaricare lo schema live",
                    flush=True,
                )

                return None

            _schema_text = response.text

            print(
                f"✅ Schema live scaricato "
                f"({len(_schema_text)} caratteri)",
                flush=True,
            )

            return _schema_text

        except Exception as error:

            print(
                f"⚠️ Errore download schema: {error}",
                flush=True,
            )

            return None


def get_input_fields(type_name):
    """
    Estrae dal file schema.graphql i campi di un input.

    Non utilizza introspection GraphQL.
    """

    schema = get_live_schema()

    if not schema:
        return set()

    # Cerca:
    #
    # input prepareOfferInput {
    #
    pattern = (
        r"\binput\s+"
        + re.escape(type_name)
        + r"\s*\{"
    )

    match = re.search(
        pattern,
        schema,
        flags=re.MULTILINE,
    )

    if not match:
        print(
            f"⚠️ {type_name} non trovato nello schema",
            flush=True,
        )
        return set()

    start = match.end()

    # Gli input GraphQL sono semplici blocchi con parentesi.
    depth = 1
    pos = start

    while pos < len(schema) and depth > 0:

        char = schema[pos]

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

        pos += 1

    block = schema[start:pos - 1]

    fields = set()

    for line in block.splitlines():

        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        # Ignora descrizioni multilinea / direttive.
        field_match = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:",
            line,
        )

        if field_match:
            fields.add(
                field_match.group(1)
            )

    print(
        f"🔍 Campi {type_name}: "
        f"{', '.join(sorted(fields))}",
        flush=True,
    )

    return fields


def inspect_live_schema():
    """
    Mostra i campi realmente presenti nelle input live.

    NON usa __type.
    """

    print(
        "🔎 CONTROLLO SCHEMA LIVE",
        flush=True,
    )

    prepare_fields = get_input_fields(
        "prepareOfferInput"
    )

    create_fields = get_input_fields(
        "createDirectOfferInput"
    )

    if prepare_fields:

        print(
            "   prepareOfferInput:",
            flush=True,
        )

        for field in sorted(prepare_fields):
            print(
                f"      • {field}",
                flush=True,
            )

    if create_fields:

        print(
            "   createDirectOfferInput:",
            flush=True,
        )

        for field in sorted(create_fields):
            print(
                f"      • {field}",
                flush=True,
            )

    return prepare_fields, create_fields


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql(
        """
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
        """
    )

    user = (
        (data or {}).get("data") or {}
    ).get("currentUser")

    if not user:

        print(
            "❌ Account Sorare non verificato",
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
# OFFERS
# ============================================================

def get_offers():

    data = graphql(
        """
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
        """
    )

    user = (
        (data or {}).get("data") or {}
    ).get("currentUser") or {}

    return (
        user.get(
            "pendingTokenOffersReceived"
        ) or {}
    ).get("nodes") or []


# ============================================================
# CARDS
# ============================================================

def card_details(asset_ids):

    asset_ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
        )
    )

    if not asset_ids:
        return []

    data = graphql(
        """
        query Cards($assetIds: [String!]) {

            anyCards(assetIds: $assetIds) {

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
            "assetIds": asset_ids
        },
    )

    return (
        ((data or {}).get("data") or {})
        .get("anyCards")
        or []
    )


def card_price(card):

    values = []

    for name in (
        "lowestPriceCard",
        "lowestPriceCardAnySeason",
    ):

        source = card.get(name) or {}

        try:

            live = (
                (
                    source.get(
                        "liveSingleSaleOffer"
                    )
                    or {}
                )
                .get("receiverSide", {})
                .get("amounts", {})
                .get("eurCents")
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

        public = (
            source.get(
                "publicMinPrices"
            )
            or []
        )

        if isinstance(public, dict):
            public = [public]

        for item in public:

            try:

                value = int(
                    item.get(
                        "eurCents"
                    )
                )

                if value > 0:
                    values.append(value)

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                pass

    return (
        min(values)
        if values
        else None
    )


def is_kulenovic(card):

    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(
            KID.lower()
        )

    return (
        str(
            card.get("assetId") or ""
        ).lower()
        in wanted
        or
        str(
            card.get("slug") or ""
        ).lower()
        in wanted
    )


# ============================================================
# COMPETITIONS
# ============================================================

def get_competitions(card):

    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        return []

    result = []

    for competition in (
        club.get("activeCompetitions")
        or []
    ):

        if isinstance(
            competition,
            dict,
        ):

            value = slug(
                competition.get(
                    "slug"
                )
            )

            if value:
                result.append(value)

    return list(
        dict.fromkeys(result)
    )


def check_competition(card):

    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):

        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )

        return False

    name = (
        club.get("name")
        or club.get("slug")
        or "Sconosciuta"
    )

    competitions = get_competitions(
        card
    )

    print(
        f"      🏟️ Squadra: {name}",
        flush=True,
    )

    if not competitions:

        print(
            "      ❌ Nessuna activeCompetition",
            flush=True,
        )

        return False

    print(
        "      🏆 Competizioni Sorare:",
        flush=True,
    )

    for competition in competitions:

        print(
            f"         🆕 {competition} "
            f"({competition})",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


# ============================================================
# CARD VALIDATION
# ============================================================

def valid_card(card):

    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    player = (
        card.get("anyPlayer") or {}
    )

    try:

        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError,
    ):

        print(
            f"   📄 {name}",
            flush=True,
        )

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return False

    price = card_price(card)

    print(
        f"   📄 {name}",
        flush=True,
    )

    print(
        f"      🎂 Età: {age} anni",
        flush=True,
    )

    # Età: deve essere strettamente inferiore
    # a MAX_AGE.
    if age >= MAX_AGE:

        print(
            f"      ❌ Età troppo alta "
            f"(limite: < {MAX_AGE})",
            flush=True,
        )

        return False

    if price is None:

        print(
            "      ❌ Prezzo non disponibile",
            flush=True,
        )

        return False

    print(
        f"      💰 Floor €{price / 100:.2f}",
        flush=True,
    )

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )

        return False

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )

        return False

    if not check_competition(card):
        return False

    competitions = get_competitions(
        card
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
# REJECT OFFER
# ============================================================

def reject_offer(offer):

    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:

        print(
            "❌ blockchainId mancante",
            flush=True,
        )

        return False

    if DRY_RUN:

        print(
            "🟡 DRY RUN: rifiuto simulato",
            flush=True,
        )

        return True

    data = graphql(
        """
        mutation Reject(
            $input: rejectOfferInput!
        ) {

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
        """,
        {
            "input": {
                "blockchainId": blockchain_id,
                "clientMutationId": str(
                    uuid.uuid4()
                ),
            }
        },
    )

    result = (
        ((data or {}).get("data") or {})
        .get("rejectOffer")
    )

    if not result:

        print(
            "❌ Risposta rejectOffer vuota",
            flush=True,
        )

        return False

    errors = (
        result.get("errors") or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ Reject: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# SIGN AUTHORIZATIONS
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

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
        &&
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
        input=json.dumps(
            {
                "privateKey": STARK,
                "authorizations":
                    authorizations,
            }
        ),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if process.returncode != 0:

        raise RuntimeError(
            process.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        process.stdout
    )


# ============================================================
# BUILD PREPARE OFFER INPUT
# ============================================================

def build_prepare_offer_input(
    asset_ids,
    receiver,
    amount,
):
    """
    Costruisce prepareOfferInput
    in maniera compatibile con lo schema
    live di Sorare.

    IMPORTANTISSIMO:

    - NON forza type se lo schema live
      non lo prevede.
    - NON inserisce exchangeRateId.
    - aggiunge settlementCurrencies
      se disponibile.
    """

    fields = get_input_fields(
        "prepareOfferInput"
    )

    if not fields:

        raise RuntimeError(
            "Impossibile leggere "
            "prepareOfferInput dallo schema live"
        )

    result = {}

    # --------------------------------------------------------
    # Tipo offerta
    #
    # Se l'API live lo prevede lo usiamo.
    # Se non lo prevede NON lo inviamo.
    # --------------------------------------------------------

    if "type" in fields:

        result["type"] = (
            "DIRECT_OFFER"
        )

    # --------------------------------------------------------
    # Assets
    # --------------------------------------------------------

    if "sendAssetIds" in fields:

        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:

        result["receiveAssetIds"] = asset_ids

    # --------------------------------------------------------
    # IMPORTO
    #
    # Stiamo pagando in EUR.
    #
    # Sorare usa i centesimi per EUR:
    # 20 = €0,20
    # --------------------------------------------------------

    if "sendAmount" in fields:

        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    # --------------------------------------------------------
    # Receiver
    # --------------------------------------------------------

    if "receiverSlug" in fields:

        result["receiverSlug"] = receiver

    # --------------------------------------------------------
    # SETTLEMENT CURRENCIES
    #
    # Questo è importante per il problema:
    #
    # "send_amount must be fixed in the
    # reference currency"
    #
    # Dichiariamo esplicitamente EUR.
    # --------------------------------------------------------

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = ["EUR"]

    # --------------------------------------------------------
    # Client mutation ID
    # --------------------------------------------------------

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

    # --------------------------------------------------------
    # NON aggiungiamo:
    #
    # exchangeRateId
    #
    # perché appartiene ai flussi di
    # settlement/accept/bid e non deve
    # essere inserito arbitrariamente
    # in prepareOfferInput.
    # --------------------------------------------------------

    return result


# ============================================================
# BUILD CREATE DIRECT OFFER INPUT
# ============================================================

def build_create_direct_offer_input(
    asset_ids,
    receiver,
    amount,
    approvals,
):
    """
    Costruisce createDirectOfferInput
    rispettando lo schema live.
    """

    fields = get_input_fields(
        "createDirectOfferInput"
    )

    if not fields:

        raise RuntimeError(
            "Impossibile leggere "
            "createDirectOfferInput "
            "dallo schema live"
        )

    result = {}

    # --------------------------------------------------------
    # APPROVALS
    # --------------------------------------------------------

    if "approvals" in fields:

        result[
            "approvals"
        ] = approvals

    # --------------------------------------------------------
    # DEAL ID
    # --------------------------------------------------------

    if "dealId" in fields:

        result[
            "dealId"
        ] = str(uuid.uuid4())

    # --------------------------------------------------------
    # SEND ASSETS
    # --------------------------------------------------------

    if "sendAssetIds" in fields:

        result[
            "sendAssetIds"
        ] = []

    # --------------------------------------------------------
    # RECEIVE ASSETS
    # --------------------------------------------------------

    if "receiveAssetIds" in fields:

        result[
            "receiveAssetIds"
        ] = asset_ids

    # --------------------------------------------------------
    # SEND AMOUNT
    # --------------------------------------------------------

    if "sendAmount" in fields:

        result[
            "sendAmount"
        ] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    # --------------------------------------------------------
    # RECEIVER
    # --------------------------------------------------------

    if "receiverSlug" in fields:

        result[
            "receiverSlug"
        ] = receiver

    # --------------------------------------------------------
    # SETTLEMENT CURRENCIES
    # --------------------------------------------------------

    if "settlementCurrencies" in fields:

        result[
            "settlementCurrencies"
        ] = ["EUR"]

    # --------------------------------------------------------
    # CLIENT MUTATION ID
    # --------------------------------------------------------

    if "clientMutationId" in fields:

        result[
            "clientMutationId"
        ] = str(uuid.uuid4())

    return result


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(
    offer,
    cards,
):

    sender = (
        offer.get("sender") or {}
    )

    receiver = str(
        sender.get("slug") or ""
    ).strip()

    asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if card.get("assetId")
    ]

    if not receiver:

        print(
            "❌ receiverSlug mancante",
            flush=True,
        )

        return False

    if not asset_ids:

        print(
            "❌ Nessuna carta da ricevere",
            flush=True,
        )

        return False

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
        f"👤 Receiver: {receiver}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    if DRY_RUN:

        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True,
        )

        return True

    if not STARK:

        print(
            "❌ SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    # ========================================================
    # PREPARE
    # ========================================================

    try:

        prepare_input = (
            build_prepare_offer_input(
                asset_ids,
                receiver,
                amount,
            )
        )

    except Exception as error:

        print(
            f"❌ Costruzione "
            f"prepareOfferInput: {error}",
            flush=True,
        )

        return False

    print(
        "📦 prepareOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            prepare_input,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
        """,
        {
            "input": prepare_input
        },
    )

    if not data:

        print(
            "❌ Nessuna risposta "
            "da prepareOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ prepareOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
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
        result.get("errors") or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ prepareOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    authorizations = (
        result.get(
            "authorizations"
        )
        or []
    )

    if not authorizations:

        print(
            "❌ Nessuna autorizzazione "
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

        approvals = sign_authorizations(
            authorizations
        )

    except Exception as error:

        print(
            f"❌ Firma: {error}",
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
    # ========================================================

    try:

        create_input = (
            build_create_direct_offer_input(
                asset_ids,
                receiver,
                amount,
                approvals,
            )
        )

    except Exception as error:

        print(
            f"❌ Costruzione "
            f"createDirectOfferInput: "
            f"{error}",
            flush=True,
        )

        return False

    debug = dict(
        create_input
    )

    if "approvals" in debug:

        debug["approvals"] = (
            f"{len(approvals)} "
            "authorization(s)"
        )

    print(
        "📦 createDirectOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            debug,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

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
            "input": create_input
        },
    )

    if not data:

        print(
            "❌ Nessuna risposta "
            "da createDirectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):

        print(
            "❌ createDirectOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for error in data["errors"]:

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
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
        result.get("errors") or []
    )

    if errors:

        for error in errors:

            print(
                f"❌ createDirectOffer: "
                f"{error.get('message', 'Errore')}",
                flush=True,
            )

        return False

    token_offer = (
        result.get("tokenOffer")
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

    print(
        "=" * 40,
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
        "=" * 40,
        flush=True,
    )

    return True


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):

    offer_id = str(
        offer.get("id") or ""
    ).strip()

    if not offer_id:
        return

    with state_lock:

        if offer_id in processed:
            return

        processed.add(
            offer_id
        )

    print(
        "\n" + "=" * 40,
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )

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
        f"{len(valid_cards)}/{len(cards)}",
        flush=True,
    )

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

    rejected = (
        len(cards)
        - len(valid_cards)
    )

    if rejected:

        print(
            f"⚠️ {rejected} carta/e "
            f"esclusa/e",
            flush=True,
        )

    result = counter_offer(
        offer,
        valid_cards,
    )

    if result:

        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

        if not reject_offer(
            offer
        ):

            print(
                "⚠️ Impossibile rifiutare "
                "l'offerta originale",
                flush=True,
            )

    else:

        print(
            "🔴 Controproposta "
            "NON creata.",
            flush=True,
        )

        print(
            "🟡 Offerta originale "
            "lasciata IN SOSPESO "
            "per nuovo tentativo.",
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
        "📦 VERSIONE BOT: 16.0",
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
        "TUTTE le activeCompetitions Sorare",
        flush=True,
    )

    print(
        "🔧 PREPARE: schema LIVE "
        "+ settlementCurrencies EUR",
        flush=True,
    )

    print(
        "🔧 CREATE: createDirectOffer",
        flush=True,
    )

    print(
        "🚫 PREPARE: nessun "
        "exchangeRateId forzato",
        flush=True,
    )

    print(
        "🚫 PREPARE: type inviato "
        "solo se presente nello schema LIVE",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    if not check_account():

        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # SCHEMA
    #
    # NON utilizziamo più:
    #
    # __type
    #
    # perché Sorare ha disabilitato
    # l'introspection GraphQL.
    # --------------------------------------------------------

    inspect_live_schema()

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

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
                f"❌ Worker: {error}",
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
            return

        _worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True,
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    return jsonify(
        {
            "status": "online",
            "bot": "sorare",
            "version": "16.0",
            "dry_run": DRY_RUN,
            "pay_per_card_cents":
                PAY_PER_CARD,
            "interval_seconds":
                INTERVAL,
            "max_age":
                MAX_AGE,
            "competition_mode":
                "ALL_ACTIVE_SORARE_COMPETITIONS",
            "prepare_mode":
                "LIVE_SCHEMA_AWARE",
            "settlement_currency":
                "EUR",
            "exchange_rate_mode":
                "NOT_FORCED_IN_PREPARE",
        }
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status": "ok",
            "bot": "running",
            "version": "16.0",
        }
    )


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000",
            )
        ),
    )
