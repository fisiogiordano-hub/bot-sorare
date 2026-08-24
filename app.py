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

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()

DRY_RUN = (
    os.getenv("DRY_RUN", "false")
    .strip()
    .lower()
    == "true"
)

MIN_PRICE = 30
MAX_PRICE = 80
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
# CAMPIONATI / COMPETIZIONI CONOSCIUTE
#
# Queste servono soprattutto per dare un nome leggibile
# alla competizione restituita da Sorare.
#
# IMPORTANTE:
# il bot NON si limita più a questo dizionario.
#
# Se Sorare restituisce una nuova competizione attiva che
# non è presente qui, la carta viene comunque considerata
# coperta e lo slug viene mostrato nei log.
# ============================================================

CAMPIONATI = {

    # --------------------------------------------------------
    # INGHILTERRA
    # --------------------------------------------------------

    "english-league": "English League",
    "premier-league-eng": "English League",
    "premier-league": "English League",
    "english-premier-league": "English League",

    "second-division-eng": "English Second Division",
    "championship-eng": "English Second Division",
    "english-championship": "English Second Division",
    "championship": "English Second Division",

    # --------------------------------------------------------
    # FRANCIA
    # --------------------------------------------------------

    "ligue-1-fr": "Ligue 1",
    "ligue-1": "Ligue 1",
    "ligue-1-mcdonalds": "Ligue 1",

    "ligue-2-fr": "Ligue 2",
    "ligue-2": "Ligue 2",
    "ligue-2-bkt": "Ligue 2",

    # --------------------------------------------------------
    # SPAGNA
    # --------------------------------------------------------

    "laliga-es": "LALIGA EA SPORTS",
    "laliga": "LALIGA EA SPORTS",
    "la-liga": "LALIGA EA SPORTS",
    "laliga-ea-sports": "LALIGA EA SPORTS",

    "laliga-2-es": "LALIGA HYPERMOTION",
    "laliga-hypermotion": "LALIGA HYPERMOTION",
    "laliga-2": "LALIGA HYPERMOTION",
    "segunda-division-spain": "LALIGA HYPERMOTION",

    # --------------------------------------------------------
    # GERMANIA
    # --------------------------------------------------------

    "bundesliga-de": "Bundesliga",
    "bundesliga": "Bundesliga",

    "2-bundesliga-de": "2. Bundesliga",
    "2-bundesliga": "2. Bundesliga",

    # --------------------------------------------------------
    # ITALIA
    # --------------------------------------------------------

    "serie-a-it": "Serie A",
    "serie-a": "Serie A",

    "serie-b-it": "Serie B",
    "serie-b": "Serie B",

    # --------------------------------------------------------
    # PORTOGALLO
    # --------------------------------------------------------

    "liga-portugal": "Primeira Liga",
    "primeira-liga-pt": "Primeira Liga",
    "liga-portugal-pt": "Primeira Liga",
    "primeira-liga": "Primeira Liga",

    # --------------------------------------------------------
    # OLANDA
    # --------------------------------------------------------

    "eredivisie-nl": "Eredivisie",
    "eredivisie": "Eredivisie",
    "vriendenloterij-eredivisie": "Eredivisie",

    # --------------------------------------------------------
    # BELGIO
    # --------------------------------------------------------

    "jupiler-pro-league-be": "Jupiler Pro League",
    "jupiler-pro-league": "Jupiler Pro League",

    # --------------------------------------------------------
    # SCOZIA
    # --------------------------------------------------------

    "scottish-premiership-sco": "Scottish Premiership",
    "scottish-premiership": "Scottish Premiership",

    # --------------------------------------------------------
    # GIAPPONE
    # --------------------------------------------------------

    "jleague-jp": "J.League",
    "j1-league-jp": "J.League",
    "j-league": "J.League",
    "j1-league": "J.League",
    "j1-100-year-vision-league": "J1 100 Year Vision League",

    # --------------------------------------------------------
    # AUSTRIA
    # --------------------------------------------------------

    "austrian-bundesliga-at": "Austrian Bundesliga",
    "austrian-bundesliga": "Austrian Bundesliga",
    "bundesliga-at": "Austrian Bundesliga",

    # --------------------------------------------------------
    # CROAZIA
    # --------------------------------------------------------

    "croatian-hnl-hr": "SuperSport HNL",
    "croatian-first-league-hr": "SuperSport HNL",
    "croatian-first-league": "SuperSport HNL",
    "croatian-hnl": "SuperSport HNL",
    "supersport-hnl": "SuperSport HNL",
    "super-sport-hnl": "SuperSport HNL",

    # --------------------------------------------------------
    # MLS
    # --------------------------------------------------------

    "mls-us": "Major League Soccer",
    "major-league-soccer-us": "Major League Soccer",
    "major-league-soccer": "Major League Soccer",
    "mls": "Major League Soccer",

    # --------------------------------------------------------
    # COREA
    # --------------------------------------------------------

    "k-league-1-kr": "K League 1",
    "k-league-1": "K League 1",
    "k-league": "K League",

    # --------------------------------------------------------
    # TURCHIA
    # --------------------------------------------------------

    "super-lig-tr": "Süper Lig",
    "super-lig": "Süper Lig",
    "turkish-super-lig": "Süper Lig",
    "super-lig-turkey": "Süper Lig",

    # --------------------------------------------------------
    # DANIMARCA
    # --------------------------------------------------------

    "superliga-dk": "Danish Superliga",
    "superliga": "Danish Superliga",
    "danish-superliga": "Danish Superliga",

    # --------------------------------------------------------
    # BRASILE
    # --------------------------------------------------------

    "brasileirao-serie-a-br": "Campeonato Brasileiro Série A",
    "brasileirao-serie-a": "Campeonato Brasileiro Série A",
    "brasileirao": "Campeonato Brasileiro Série A",
    "serie-a-br": "Campeonato Brasileiro Série A",

    # --------------------------------------------------------
    # RUSSIA
    # --------------------------------------------------------

    "premier-liga-ru": "Russian Premier League",
    "russian-premier-league": "Russian Premier League",
    "premier-liga": "Russian Premier League",
    "russia-premier-league": "Russian Premier League",

    # --------------------------------------------------------
    # PERÙ
    # --------------------------------------------------------

    "liga-1-peru": "Primera División del Perú",
    "liga-1-pe": "Primera División del Perú",
    "peruvian-primera-division": "Primera División del Perú",

    # --------------------------------------------------------
    # COLOMBIA
    # --------------------------------------------------------

    "primera-a-colombia": "Primera A",
    "liga-betplay-col": "Primera A",
    "primera-a": "Primera A",
    "liga-betplay": "Primera A",

    # --------------------------------------------------------
    # MESSICO
    # --------------------------------------------------------

    "liga-mx": "Liga MX",

    # --------------------------------------------------------
    # CILE
    # --------------------------------------------------------

    "primera-division-chile": "Primera División de Chile",
    "primera-division-cl": "Primera División de Chile",

    # --------------------------------------------------------
    # ECUADOR
    # --------------------------------------------------------

    "liga-pro": "Liga Pro",
    "liga-pro-ecuador": "Liga Pro",

    # --------------------------------------------------------
    # NORVEGIA
    # --------------------------------------------------------

    "eliteserien": "Eliteserien",

    # --------------------------------------------------------
    # SVIZZERA
    # --------------------------------------------------------

    "super-league": "Super League",
    "swiss-super-league": "Super League",

    # --------------------------------------------------------
    # ARGENTINA
    # --------------------------------------------------------

    "superliga-argentina": "Superliga Argentina de Fútbol",
    "superliga-argentina-de-futbol":
        "Superliga Argentina de Fútbol",

    # --------------------------------------------------------
    # COMPETIZIONI INTERNAZIONALI
    # --------------------------------------------------------

    "champions-league": "Champions League",
    "uefa-champions-league": "Champions League",

    "europa-league": "Europa League",
    "uefa-europa-league": "Europa League",

    "europa-conference-league":
        "Europa Conference League",

    "afc-champions-league-elite":
        "AFC Champions League Elite",

    "libertadores": "Libertadores",
    "sudamericana": "Sudamericana",

    "leagues-cup": "Leagues Cup",

    "club-world-cup": "FIFA Club World Cup",

    "world-cup": "World Cup",

    "world-cup-qualifiers":
        "World Cup Qualifiers",

    "afc-world-cup-qualifiers":
        "AFC World Cup Qualifiers",

    "conmebol-world-cup-qualifiers":
        "CONMEBOL World Cup Qualifiers",

    "concacaf-world-cup-qualifiers":
        "CONCACAF World Cup Qualifiers",

    "nations-league": "Nations League",

    "copa-america": "Copa America",

    "africa-cup-of-nations":
        "CAF Africa Cup of Nations",

    "european-championship":
        "European Championship",

    "european-championship-qualifiers":
        "European Championship Qualifiers",
}


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
    """
    Normalizza uno slug Sorare.

    Esempio:
        SuperSport HNL
        supersport-hnl
        SUPERSPORT_HNL

    diventano una forma confrontabile.
    """

    value = str(value or "").strip().lower()

    replacements = {
        "_": "-",
        " ": "-",
        "’": "",
        "'": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    while "--" in value:
        value = value.replace("--", "-")

    return value


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
        "User-Agent": "Sorare-Bot/11.0",
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

            if response.status_code != 200:

                print(
                    f"❌ HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:500]}",
                    flush=True,
                )

                time.sleep(attempt)
                continue

            try:

                data = response.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )

                return None

            errors = data.get("errors") or []

            if errors:

                for error in errors:

                    print(
                        "❌ GraphQL:",
                        error.get(
                            "message",
                            "Errore",
                        ),
                        flush=True,
                    )

                return None

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

    data = graphql("""
        query CurrentUser {
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
# OFFERTE
# ============================================================

def get_offers():

    data = graphql("""
        query PendingOffers {
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
        (
            ((data or {}).get("data") or {})
            .get("currentUser") or {}
        )
        .get("pendingTokenOffersReceived") or {}
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

    data = graphql("""
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
    """, {
        "assetIds": asset_ids
    })

    return (
        ((data or {}).get("data") or {})
        .get("anyCards") or []
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

        source = card.get(source_name) or {}

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

        except (
            TypeError,
            ValueError,
            AttributeError,
        ):
            pass

        public = (
            source.get("publicMinPrices")
            or []
        )

        if isinstance(public, dict):
            public = [public]

        for item in public:

            try:

                value = int(
                    item.get("eurCents")
                )

                if value > 0:
                    values.append(value)

            except (
                TypeError,
                ValueError,
                AttributeError,
            ):
                pass

    return min(values) if values else None


# ============================================================
# KULENOVIC
# ============================================================

def is_kulenovic(card):

    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(KID.lower())

    asset_id = str(
        card.get("assetId") or ""
    ).lower()

    card_slug = str(
        card.get("slug") or ""
    ).lower()

    return (
        asset_id in wanted
        or card_slug in wanted
    )


# ============================================================
# CONTROLLO COMPETIZIONE
# ============================================================

def get_competitions(card):

    player = card.get("anyPlayer") or {}

    club = player.get("activeClub")

    if not isinstance(club, dict):
        return []

    competitions = (
        club.get("activeCompetitions")
        or []
    )

    result = []

    for competition in competitions:

        if not isinstance(
            competition,
            dict,
        ):
            continue

        raw_slug = (
            competition.get("slug")
            or ""
        )

        normalized = slug(raw_slug)

        if not normalized:
            continue

        known_name = CAMPIONATI.get(
            normalized
        )

        result.append({
            "slug": normalized,
            "name": (
                known_name
                or raw_slug
                or normalized
            ),
            "known": bool(known_name),
        })

    return result


def check_competition(card):

    player = card.get("anyPlayer") or {}

    club = player.get("activeClub")

    if not isinstance(club, dict):

        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )

        return False

    club_name = (
        club.get("name")
        or club.get("slug")
        or "Squadra sconosciuta"
    )

    print(
        f"      🏟️ Squadra: {club_name}",
        flush=True,
    )

    competitions = get_competitions(card)

    if not competitions:

        print(
            "      ❌ Nessuna competizione "
            "attiva restituita da Sorare",
            flush=True,
        )

        return False

    print(
        "      🏆 Competizioni Sorare:",
        flush=True,
    )

    for competition in competitions:

        if competition["known"]:

            print(
                f"         ✅ "
                f"{competition['name']} "
                f"({competition['slug']})",
                flush=True,
            )

        else:

            print(
                f"         🆕 "
                f"{competition['name']} "
                f"({competition['slug']}) "
                f"[NUOVO SLUG]",
                flush=True,
            )

    # --------------------------------------------------------
    # NUOVA LOGICA
    #
    # Non richiediamo più che lo slug sia presente nel
    # dizionario CAMPIONATI.
    #
    # Se Sorare ci restituisce una activeCompetition,
    # la consideriamo una competizione coperta.
    #
    # Questo evita falsi negativi quando Sorare introduce
    # un nuovo slug o cambia il nome interno.
    # --------------------------------------------------------

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


# ============================================================
# CONTROLLO CARTA
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

    player = card.get("anyPlayer") or {}

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
            f"      ❌ Età non valida: {age}",
            flush=True,
        )

        return False

    print(
        f"      🎂 Età: {age} anni",
        flush=True,
    )

    if age >= MAX_AGE:

        print(
            f"      ❌ Età troppo alta "
            f"(limite: meno di {MAX_AGE})",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

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

    if not MIN_PRICE <= price <= MAX_PRICE:

        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # RARITÀ
    # --------------------------------------------------------

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
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

    competitions = get_competitions(card)

    names = []

    for competition in competitions:

        name = competition.get("name")

        if name and name not in names:
            names.append(name)

    print(
        f"      ✅ VALIDATA | "
        f"{age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(names)}",
        flush=True,
    )

    return True


# ============================================================
# RIFIUTO OFFERTA
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

    data = graphql("""
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
    """, {
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            ),
        }
    })

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

    errors = result.get("errors") or []

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
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# FIRMA SORARE
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
        "StarkexTransferAuthorizationRequest" &&
        r.amount !== undefined &&
        r.amount !== null
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
        "Authorization non supportata: " +
        r.__typename
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
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if process.returncode != 0:

        raise RuntimeError(
            process.stderr.strip()
            or "Firma fallita"
        )

    try:

        return json.loads(
            process.stdout
        )

    except json.JSONDecodeError as error:

        raise RuntimeError(
            "Risposta firma non valida"
        ) from error


# ============================================================
# CONTROPROPOSTA
# ============================================================

def counter_offer(offer, cards):

    sender = offer.get("sender") or {}

    receiver = str(
        sender.get("slug") or ""
    ).strip()

    asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if isinstance(card, dict)
        and card.get("assetId")
    ]

    if not receiver or not asset_ids:

        print(
            "❌ Dati controproposta mancanti",
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
            "❌ "
            "SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    prepare_input = {

        "type": "DIRECT_OFFER",

        "sendAssetIds": [],

        "receiveAssetIds": asset_ids,

        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },

        "receiverSlug": receiver,

        "clientMutationId": str(
            uuid.uuid4()
        ),
    }

    data = graphql("""
        mutation Prepare(
            $input: prepareOfferInput!
        ) {

            prepareOffer(input: $input) {

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
    """, {
        "input": prepare_input
    })

    result = (
        ((data or {}).get("data") or {})
        .get("prepareOffer")
    )

    if not result:

        print(
            "❌ prepareOffer fallito",
            flush=True,
        )

        return False

    errors = result.get("errors") or []

    if errors:

        for error in errors:

            print(
                "❌ Prepare:",
                error.get(
                    "message",
                    "Errore",
                ),
                flush=True,
            )

        return False

    authorizations = (
        result.get("authorizations")
        or []
    )

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

    create_input = {

        "approvals": approvals,

        "dealId": str(
            uuid.uuid4()
        ),

        "sendAssetIds": [],

        "receiveAssetIds": asset_ids,

        "sendAmount": {
            "amount": str(amount),
            "currency": "EUR",
        },

        "receiverSlug": receiver,

        "clientMutationId": str(
            uuid.uuid4()
        ),
    }

    data = graphql("""
        mutation Create(
            $input: createDirectOfferInput!
        ) {

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
    """, {
        "input": create_input
    })

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
                error.get(
                    "message",
                    "Errore",
                ),
                flush=True,
            )

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:

        print(
            "❌ Nessuna offerta creata",
            flush=True,
        )

        return False

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

    return True


# ============================================================
# ELABORAZIONE OFFERTA
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

        processed.add(offer_id)

    print(
        "\n========================================",
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
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

    # --------------------------------------------------------
    # KULENOVIC DEVE ESSERE NELLA PARTE CHE
    # L'ALTRA PERSONA VUOLE RICEVERE
    # --------------------------------------------------------

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
            "❌ Impossibile verificare tutte "
            "le carte",
            flush=True,
        )

        return

    print(
        f"🔎 Controllo {len(cards)} carta/e",
        flush=True,
    )

    # --------------------------------------------------------
    # FILTRO CARTE
    # --------------------------------------------------------

    valid_cards = []

    for card in cards:

        if valid_card(card):
            valid_cards.append(card)

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/{len(cards)}",
        flush=True,
    )

    # --------------------------------------------------------
    # NESSUNA CARTA VALIDA
    # --------------------------------------------------------

    if not valid_cards:

        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        all_old = True

        for card in cards:

            player = (
                card.get("anyPlayer")
                or {}
            )

            age = player.get("age")

            if age is None:

                all_old = False
                break

            try:
                age = int(age)

            except (
                TypeError,
                ValueError,
            ):

                all_old = False
                break

            if age < MAX_AGE:

                all_old = False
                break

        if all_old:

            print(
                f"🚫 Tutte le carte hanno "
                f"{MAX_AGE} anni o più.",
                flush=True,
            )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        reject_offer(offer)

        return

    # --------------------------------------------------------
    # CI SONO CARTE VALIDE
    # --------------------------------------------------------

    rejected_count = (
        len(cards)
        - len(valid_cards)
    )

    if rejected_count:

        print(
            f"⚠️ {rejected_count} carta/e "
            f"esclusa/e dal limite di età/"
            f"criteri.",
            flush=True,
        )

    # --------------------------------------------------------
    # PRIMA RIFIUTIAMO L'OFFERTA ORIGINALE
    # --------------------------------------------------------

    if not reject_offer(offer):

        print(
            "❌ Impossibile rifiutare "
            "l'offerta originale",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # POI PROPONIAMO SOLO LE CARTE VALIDE
    # --------------------------------------------------------

    counter_offer(
        offer,
        valid_cards,
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
        "tutte le activeCompetitions "
        "restituite da Sorare",
        flush=True,
    )

    print(
        f"🧪 DRY_RUN={DRY_RUN}",
        flush=True,
    )

    if not check_account():

        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return

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

            time.sleep(INTERVAL)

        except Exception as error:

            print(
                f"❌ Worker: {error}",
                flush=True,
            )

            time.sleep(INTERVAL)


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
# FLASK / RENDER
# ============================================================

@app.get("/")
def home():

    return jsonify({

        "status": "online",

        "bot": "sorare",

        "version": "11.0",

        "dry_run": DRY_RUN,

        "pay_per_card_cents":
            PAY_PER_CARD,

        "interval_seconds":
            INTERVAL,

        "max_age":
            MAX_AGE,

        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",
    })


@app.get("/health")
def health():

    return jsonify({

        "status": "ok",

        "bot": "running",

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
