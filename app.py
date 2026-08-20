import os

import time

import json

import uuid

import shutil

import subprocess

import threading

import requests



from decimal import Decimal

from flask import Flask, jsonify





app = Flask(__name__)





# ============================================================

# CONFIGURAZIONE

# ============================================================



URL = "https://api.sorare.com/graphql"



TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()

AUD = os.getenv("SORARE_JWT_AUD", "").strip()

KID = os.getenv("KULENOVIC_ID", "").strip()

STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()



DRY = os.getenv("DRY_RUN", "false").strip().lower() == "true"



MIN_PRICE = 30

MAX_PRICE = 80

PAY = 20

INTERVAL = 10

TIMEOUT = 30



KSLUG = "sandro-kulenovic-2025-limited-385"



KASSET = (

    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"

    "6796b6c0ed10ba0a6"

)





# ============================================================

# CAMPIONATI

# ============================================================



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

    "brasileirao-serie-a-br": "Brasile",

    "brasileirao-serie-a": "Brasile",

    "brasileirao": "Brasile",

    "serie-a-br": "Brasile",

    "premier-liga-ru": "Russia",

    "russian-premier-league": "Russia",

    "premier-liga": "Russia",

    "russia-premier-league": "Russia",

    "serie-b-it": "Serie B",

    "serie-b": "Serie B",

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





# ============================================================

# STATO

# ============================================================



analizzate = set()

in_elaborazione = set()

lock = threading.Lock()

_started = False





# ============================================================

# UTILITY

# ============================================================



def slug(value):

    return (

        str(value or "")

        .strip()

        .lower()

        .replace("_", "-")

        .replace(" ", "-")

    )





def headers():

    if not TOKEN:

        raise RuntimeError("SORARE_JWT_TOKEN non configurato.")



    token = TOKEN



    if not token.lower().startswith("bearer "):

        token = "Bearer " + token



    result = {

        "Content-Type": "application/json",

        "Accept": "application/json",

        "Authorization": token,

        "User-Agent": "Sorare-Python-Bot/7.0",

    }



    if AUD:

        result["JWT-AUD"] = AUD



    return result





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

                headers=headers(),

                timeout=TIMEOUT,

            )



            print(

                f"🌐 Sorare HTTP: {response.status_code}",

                flush=True,

            )



            if response.status_code == 429:

                try:

                    pause = int(

                        response.headers.get(

                            "Retry-After",

                            attempt * 3,

                        )

                    )

                except (TypeError, ValueError):

                    pause = attempt * 3



                print(

                    f"⚠️ Rate limit. Attendo {pause}s.",

                    flush=True,

                )



                time.sleep(pause)

                continue



            if response.status_code != 200:

                print(

                    f"❌ HTTP {response.status_code}: "

                    f"{response.text[:1500]}",

                    flush=True,

                )



                time.sleep(attempt)

                continue



            try:

                data = response.json()

            except Exception:

                print("❌ Risposta JSON non valida.", flush=True)

                return None



            if data.get("errors"):

                print("❌ Errore GraphQL:", flush=True)



                for error in data["errors"]:

                    print(

                        "   -",

                        error.get(

                            "message",

                            "Errore sconosciuto",

                        ),

                        flush=True,

                    )



                return None



            return data



        except requests.RequestException as error:

            print(

                f"⚠️ Errore HTTP: {error}",

                flush=True,

            )



            time.sleep(attempt)



        except Exception as error:

            print(

                f"❌ Errore: {error}",

                flush=True,

            )



            return None



    return None





# ============================================================

# ACCOUNT

# ============================================================



def verifica_account():

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

        (data or {})

        .get("data", {})

        .get("currentUser")

    )



    if not user:

        print("❌ currentUser non disponibile.", flush=True)

        return False



    print("========================================", flush=True)

    print("✅ AUTENTICAZIONE SORARE RIUSCITA", flush=True)

    print(

        f"👤 Manager: {user.get('nickname') or 'N/D'}",

        flush=True,

    )

    print(

        f"🔗 Slug: {user.get('slug') or 'N/D'}",

        flush=True,

    )

    print("========================================", flush=True)



    return True





# ============================================================

# OFFERTE

# ============================================================



def recupera_offerte():

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



    if data is None:

        return None



    return (

        data

        .get("data", {})

        .get("currentUser", {})

        .get("pendingTokenOffersReceived", {})

        .get("nodes")

        or []

    )





# ============================================================

# DETTAGLI CARTE

# ============================================================



def dettagli_carte(ids):

    ids = list(

        dict.fromkeys(

            str(value).strip()

            for value in ids

            if value

        )

    )



    if not ids:

        return []



    data = graphql(

        """

        query CardDetails($assetIds: [String!]) {

            anyCards(assetIds: $assetIds) {

                assetId

                slug

                name

                rarityTyped

                collection



                anyPlayer {

                    displayName

                    slug



                    activeClub {

                        slug

                        name



                        activeCompetitions {

                            slug

                        }

                    }

                }



                anyTeam {

                    name



                    activeCompetitions {

                        slug

                    }

                }



                lowestPriceCard {

                    assetId

                    slug

                    name

                    rarityTyped

                    seasonYear



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

                    assetId

                    slug

                    name

                    rarityTyped

                    seasonYear



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

        {"assetIds": ids},

    )



    if not data:

        return None



    return (

        data

        .get("data", {})

        .get("anyCards")

        or []

    )





def live(card):

    try:

        return int(

            (

                card.get("liveSingleSaleOffer")

                or {}

            )

            .get("receiverSide", {})

            .get("amounts", {})

            .get("eurCents")

        )

    except (

        TypeError,

        ValueError,

        AttributeError,

    ):

        return None





def public(card):

    values = card.get("publicMinPrices")



    if isinstance(values, dict):

        values = [values]



    if not isinstance(values, list):

        return None



    prices = []



    for value in values:

        try:

            cents = int(value.get("eurCents"))



            if cents > 0:

                prices.append(cents)



        except (

            TypeError,

            ValueError,

            AttributeError,

        ):

            pass



    return min(prices) if prices else None





def floor(card):

    for key in (

        "lowestPriceCard",

        "lowestPriceCardAnySeason",

        None,

    ):

        source = (

            card

            if key is None

            else card.get(key) or {}

        )



        prices = [

            price

            for price in (

                live(source),

                public(source),

            )

            if price

        ]



        if prices:

            return min(prices)



    return None





# ============================================================

# CONTROLLO SQUADRA / CAMPIONATO

# ============================================================



def controlla_squadra(card):

    player = card.get("anyPlayer") or {}



    player_name = (

        player.get("displayName")

        or player.get("slug")

        or card.get("name")

        or "Sconosciuto"

    )



    club = player.get("activeClub")



    if not isinstance(club, dict):

        print("      🏟️ Squadra attiva: NESSUNA", flush=True)

        print(

            f"      👤 Giocatore: {player_name}",

            flush=True,

        )

        print(

            "      🔴 GIOCATORE SENZA SQUADRA",

            flush=True,

        )

        return False



    print(

        "      🏟️ Squadra attiva: "

        f"{club.get('name') or club.get('slug') or 'N/D'}",

        flush=True,

    )



    competitions = club.get("activeCompetitions") or []



    if not competitions:

        print(

            "      🔴 Nessuna competizione attiva.",

            flush=True,

        )

        return False



    found = []



    for competition in competitions:

        if not isinstance(competition, dict):

            continue



        competition_slug = slug(

            competition.get("slug")

        )



        if competition_slug in CAMPIONATI:

            found.append(

                CAMPIONATI[competition_slug]

            )



    if not found:

        print(

            "      🔴 CAMPIONATO NON COPERTO",

            flush=True,

        )

        return False



    print("      🟢 CAMPIONATO COPERTO", flush=True)



    for championship in dict.fromkeys(found):

        print(

            f"         🟢 {championship}",

            flush=True,

        )



    return True





# ============================================================

# ANALISI CARTA

# ============================================================



def analizza_carta(card):

    if not isinstance(card, dict):

        return False



    name = (

        card.get("name")

        or card.get("slug")

        or "Carta"

    )



    rarity = str(

        card.get("rarityTyped") or ""

    ).upper()



    price = floor(card)



    print(f"\n   📄 {name}", flush=True)



    print(

        f"      Asset ID: "

        f"{card.get('assetId') or 'N/D'}",

        flush=True,

    )



    print(

        f"      Slug: "

        f"{card.get('slug') or 'N/D'}",

        flush=True,

    )



    print(

        f"      Rarità: "

        f"{rarity or 'N/D'}",

        flush=True,

    )



    if price is None:

        print(

            "      🔴 Prezzo floor NON verificabile",

            flush=True,

        )

        price_ok = False



    else:

        print(

            f"      💰 Prezzo floor: "

            f"€{price / 100:.2f}",

            flush=True,

        )



        price_ok = MIN_PRICE <= price <= MAX_PRICE



        print(

            "      🟢 Prezzo valido"

            if price_ok

            else "      🔴 Prezzo fuori intervallo",

            flush=True,

        )



    rarity_ok = rarity == "LIMITED"



    print(

        "      🟢 Rarità LIMITED"

        if rarity_ok

        else "      🔴 Rarità NON valida",

        flush=True,

    )



    club_ok = controlla_squadra(card)



    valid = price_ok and rarity_ok and club_ok



    print(

        "      🟢 CARTA IDONEA"

        if valid

        else "      ❌ CARTA NON IDONEA",

        flush=True,

    )



    return valid





# ============================================================

# KULENOVIC

# ============================================================



def kulenovic_richiesto(cards):

    wanted = {

        KASSET.lower(),

        KSLUG.lower(),

    }



    if KID:

        wanted.add(KID.lower())



    for card in cards:

        if not isinstance(card, dict):

            continue



        asset = str(

            card.get("assetId") or ""

        ).lower()



        card_slug = str(

            card.get("slug") or ""

        ).lower()



        if asset in wanted or card_slug in wanted:

            print(

                "🎯 KULENOVIC RICONOSCIUTO!",

                flush=True,

            )

            return True



    return False





# ============================================================

# RIFIUTO

# ============================================================



def rifiuta_offerta(offer):

    offer_id = str(

        offer.get("id") or ""

    ).strip()



    blockchain_id = str(

        offer.get("blockchainId") or ""

    ).strip()



    print(

        f"🔴 RIFIUTO RICHIESTO: {offer_id}",

        flush=True,

    )



    if DRY:

        print(

            "🟡 DRY RUN: rifiuto non inviato.",

            flush=True,

        )

        return True



    if not blockchain_id:

        print(

            "❌ Rifiuto fallito: blockchainId mancante.",

            flush=True,

        )

        return False



    data = graphql(

        """

        mutation RejectOffer(

            $input: rejectOfferInput!

        ) {

            rejectOffer(input: $input) {

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

            "input": {

                "blockchainId": blockchain_id,

                "clientMutationId": str(uuid.uuid4()),

            }

        },

    )



    if not data:

        return False



    result = (

        data.get("data") or {}

    ).get("rejectOffer")



    if not result:

        return False



    errors = result.get("errors") or []



    if errors:

        for error in errors:

            print(

                "❌ " + str(

                    error.get(

                        "message",

                        "Errore sconosciuto",

                    )

                ),

                flush=True,

            )

        return False



    token_offer = result.get("tokenOffer") or {}



    print(

        "✅ OFFERTA RIFIUTATA REALMENTE: "

        f"{token_offer.get('id') or offer_id}",

        flush=True,

    )



    return True





# ============================================================

# NODE / SORARE CRYPTO

# ============================================================



def trova_node():

    return (

        shutil.which("node")

        or shutil.which("nodejs")

    )





def firma_con_sorare_crypto(authorizations):

    node = trova_node()



    if not node:

        raise RuntimeError("Node.js non disponibile.")



    signer_script = r"""

const fs = require("fs");

const {

  signAuthorizationRequest

} = require("@sorare/crypto");



const input = JSON.parse(

  fs.readFileSync(0, "utf8")

);



const privateKey = input.privateKey;

const authorizations = input.authorizations;



if (!privateKey) {

  throw new Error("privateKey mancante");

}



function buildApproval(

  privateKey,

  authorization

) {

  const fingerprint =

    authorization.fingerprint;



  const request =

    authorization.request;



  if (!request) {

    throw new Error(

      "AuthorizationRequest senza request"

    );

  }



  /*

   * Sorare restituisce "amount".

   * NON usare amountAsNumber.

   *

   * Per la firma StarkEx il valore viene

   * convertito in BigInt solamente qui,

   * prima di signAuthorizationRequest().

   */



  if (

    request.__typename ===

      "StarkexTransferAuthorizationRequest"

  ) {

    if (

      request.amount !== undefined &&

      request.amount !== null

    ) {

      request.amount = BigInt(

        request.amount

      );

    }

  }



  const signature =

    signAuthorizationRequest(

      privateKey,

      request

    );



  if (

    request.__typename ===

      "StarkexTransferAuthorizationRequest"

  ) {

    return {

      fingerprint,



      starkexTransferApproval: {

        nonce: request.nonce,



        expirationTimestamp:

          request.expirationTimestamp,



        signature

      }

    };

  }



  if (

    request.__typename ===

      "StarkexLimitOrderAuthorizationRequest"

  ) {

    return {

      fingerprint,



      starkexLimitOrderApproval: {

        nonce: request.nonce,



        expirationTimestamp:

          request.expirationTimestamp,



        signature

      }

    };

  }



  if (

    request.__typename ===

      "MangopayWalletTransferAuthorizationRequest"

  ) {

    return {

      fingerprint,



      mangopayWalletTransferApproval: {

        nonce: request.nonce,



        signature

      }

    };

  }



  throw new Error(

    "AuthorizationRequest non supportata: " +

    request.__typename

  );

}



const approvals =

  authorizations.map(

    authorization =>

      buildApproval(

        privateKey,

        authorization

      )

  );



process.stdout.write(

  JSON.stringify(approvals)

);

"""



    process = subprocess.run(

        [

            node,

            "-e",

            signer_script,

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

            or "Errore signer Sorare."

        )



    try:

        return json.loads(process.stdout)



    except json.JSONDecodeError as error:

        raise RuntimeError(

            "Risposta signer non valida: "

            + process.stdout[:1000]

        ) from error





# ============================================================

# CONTROPROPOSTA

# ============================================================



def crea_controproposta(offer, cards):

    sender = offer.get("sender") or {}



    receiver_slug = str(

        sender.get("slug") or ""

    ).strip()



    if not receiver_slug:

        print(

            "❌ Destinatario non disponibile.",

            flush=True,

        )

        return False



    receive_asset_ids = [

        str(card.get("assetId")).strip()

        for card in cards

        if (

            isinstance(card, dict)

            and card.get("assetId")

        )

    ]



    if not receive_asset_ids:

        print(

            "❌ Nessuna carta da ricevere.",

            flush=True,

        )

        return False



    send_amount = len(receive_asset_ids) * PAY



    print(

        "\n========================================",

        flush=True,

    )



    print("🟢 CONTROPROPOSTA", flush=True)



    print(

        f"👤 Destinatario: {receiver_slug}",

        flush=True,

    )



    print(

        "📥 Carte che riceviamo:",

        flush=True,

    )



    for card in cards:

        print(

            "   🟢 " + str(

                card.get("name")

                or card.get("slug")

                or "Carta"

            ),

            flush=True,

        )



    print(

        f"💰 Pagamento: €{send_amount / 100:.2f}",

        flush=True,

    )



    print(

        "🎯 Kulenovic: NON viene ceduto",

        flush=True,

    )



    print(

        "========================================",

        flush=True,

    )



    if DRY:

        print(

            "🟡 DRY RUN: controproposta NON inviata.",

            flush=True,

        )

        return True



    if not STARK:

        print(

            "❌ SORARE_STARK_PRIVATE_KEY non configurata.",

            flush=True,

        )

        return False



    # ========================================================

    # PREPARE OFFER

    # ========================================================



    prepare_mutation = """

        mutation PrepareOffer(

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

    """



    prepare_input = {

        "type": "DIRECT_OFFER",



        "sendAssetIds": [],



        "receiveAssetIds": receive_asset_ids,



        "sendAmount": {

            "amount": str(send_amount),

            "currency": "EUR",

        },



        "receiverSlug": receiver_slug,



        "clientMutationId": str(uuid.uuid4()),

    }



    print(

        "\n🔧 prepareOffer...",

        flush=True,

    )



    print(

        f"📦 Riceviamo {len(receive_asset_ids)} carta/e",

        flush=True,

    )



    print(

        f"💰 Paghiamo €{send_amount / 100:.2f}",

        flush=True,

    )



    print(

        "🎯 sendAssetIds = []",

        flush=True,

    )



    data = graphql(

        prepare_mutation,

        {

            "input": prepare_input

        },

    )



    if not data:

        print(

            "❌ prepareOffer fallito.",

            flush=True,

        )

        return False



    result = (

        data.get("data") or {}

    ).get("prepareOffer")



    if not result:

        print(

            "❌ prepareOffer: risposta vuota.",

            flush=True,

        )

        return False



    errors = result.get("errors") or []



    if errors:

        print(

            "❌ prepareOffer rifiutato:",

            flush=True,

        )



        for error in errors:

            print(

                "   - " + str(

                    error.get(

                        "message",

                        "Errore sconosciuto",

                    )

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

            "❌ Nessuna AuthorizationRequest.",

            flush=True,

        )

        return False



    print(

        "✅ prepareOffer riuscito.",

        flush=True,

    )



    print(

        f"🔐 Autorizzazioni: {len(authorizations)}",

        flush=True,

    )



    # ========================================================

    # FIRMA

    # ========================================================



    try:

        approvals = firma_con_sorare_crypto(

            authorizations

        )



    except Exception as error:

        print(

            "❌ Firma Stark fallita:",

            flush=True,

        )



        print(

            f"   {error}",

            flush=True,

        )



        return False



    if not approvals:

        print(

            "❌ Nessuna approval generata.",

            flush=True,

        )

        return False



    print(

        f"✅ Approval firmate: {len(approvals)}",

        flush=True,

    )



    # ========================================================

    # CREATE DIRECT OFFER

    # ========================================================



    create_mutation = """

        mutation CreateDirectOffer(

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

    """



    create_input = {

        "approvals": approvals,



        "dealId": str(uuid.uuid4()),



        "sendAssetIds": [],



        "receiveAssetIds": receive_asset_ids,



        "sendAmount": {

            "amount": str(send_amount),

            "currency": "EUR",

        },



        "receiverSlug": receiver_slug,



        "clientMutationId": str(uuid.uuid4()),

    }



    print(

        "\n🚀 createDirectOffer...",

        flush=True,

    )



    created = graphql(

        create_mutation,

        {

            "input": create_input

        },

    )



    if not created:

        print(

            "❌ createDirectOffer fallito.",

            flush=True,

        )

        return False



    create_result = (

        created.get("data") or {}

    ).get("createDirectOffer")



    if not create_result:

        print(

            "❌ createDirectOffer: risposta vuota.",

            flush=True,

        )

        return False



    create_errors = (

        create_result.get("errors")

        or []

    )



    if create_errors:

        print(

            "❌ Sorare ha rifiutato la controproposta:",

            flush=True,

        )



        for error in create_errors:

            print(

                "   - " + str(

                    error.get(

                        "message",

                        "Errore sconosciuto",

                    )

                ),

                flush=True,

            )



        return False



    token_offer = (

        create_result.get("tokenOffer")

        or {}

    )



    offer_id = token_offer.get("id")



    if not offer_id:

        print(

            "❌ createDirectOffer non ha restituito un'offerta.",

            flush=True,

        )

        return False



    print(

        "\n========================================",

        flush=True,

    )



    print(

        "✅ CONTROPROPOSTA INVIATA REALMENTE",

        flush=True,

    )



    print(

        f"🆔 Offerta: {offer_id}",

        flush=True,

    )



    print(

        f"👤 Destinatario: {receiver_slug}",

        flush=True,

    )



    print(

        f"💰 Pagamento: €{send_amount / 100:.2f}",

        flush=True,

    )



    print(

        "🎯 Kulenovic NON è stato ceduto.",

        flush=True,

    )



    print(

        "=============================== 

