import os
import time
import threading
import requests

from decimal import Decimal, InvalidOperation
from flask import Flask


app = Flask(__name__)


# ============================================================
# CONFIGURAZIONE
# ============================================================

SORARE_TOKEN = os.getenv(
    "SORARE_JWT_TOKEN",
    ""
).strip()

SORARE_JWT_AUD = os.getenv(
    "SORARE_JWT_AUD",
    ""
).strip()

# Può essere asset ID, slug oppure vuota.
KULENOVIC_ID = os.getenv(
    "KULENOVIC_ID",
    ""
).strip()


# ============================================================
# SICUREZZA
# ============================================================

# SEMPRE DRY RUN.
#
# Nessun rifiuto e nessuna controproposta reale.
DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

# Prezzo massimo carta idonea:
# €0,50
PREZZO_MASSIMO_CENTESIMI = 50

# Pagamento:
# €0,20 per ogni carta idonea
PAGAMENTO_PER_CARTA_CENTESIMI = 20


# ============================================================
# SORARE API
# ============================================================

# Endpoint federato attuale.
SORARE_API_URL = (
    "https://api.sorare.com/federation/graphql"
)


# ============================================================
# KULENOVIC
# ============================================================

KULENOVIC_SLUG = (
    "sandro-kulenovic-2025-limited-385"
)

KULENOVIC_ASSET_ID = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)


# ============================================================
# STATO
# ============================================================

offerte_gia_analizzate = set()

monitoraggio_avviato = False

lock_avvio = threading.Lock()


# ============================================================
# HEADERS
# ============================================================

def crea_headers():

    if not SORARE_TOKEN:

        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato."
        )

    token = SORARE_TOKEN

    if not token.lower().startswith("bearer "):

        token = f"Bearer {token}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
    }

    if SORARE_JWT_AUD:

        headers["JWT-AUD"] = SORARE_JWT_AUD

    return headers


# ============================================================
# GRAPHQL
# ============================================================

def esegui_query(query, variables=None):

    payload = {
        "query": query,
        "variables": variables or {},
    }

    try:

        response = requests.post(
            SORARE_API_URL,
            json=payload,
            headers=crea_headers(),
            timeout=30,
        )

        print(
            f"🌐 Sorare HTTP: {response.status_code}"
        )

        # ----------------------------------------------------
        # HTTP ERROR
        # ----------------------------------------------------

        if response.status_code != 200:

            print(
                "❌ Risposta HTTP non valida:"
            )

            print(
                response.text[:3000]
            )

            return None

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        try:

            risultato = response.json()

        except ValueError:

            print(
                "❌ Risposta Sorare non JSON."
            )

            print(
                response.text[:3000]
            )

            return None

        # ----------------------------------------------------
        # GRAPHQL ERRORS
        # ----------------------------------------------------

        errori = risultato.get(
            "errors"
        )

        if errori:

            print(
                "❌ Errori GraphQL:"
            )

            for errore in errori:

                print(
                    "- "
                    + str(
                        errore.get(
                            "message",
                            "Errore sconosciuto",
                        )
                    )
                )

            return None

        return risultato

    except requests.RequestException as e:

        print(
            f"❌ Errore HTTP Sorare: {e}"
        )

        return None

    except Exception as e:

        print(
            f"❌ Errore richiesta Sorare: {e}"
        )

        return None


# ============================================================
# ACCOUNT
# ============================================================

def verifica_account():

    query = """
    query CurrentUserTest {

        currentUser {

            slug
            nickname

        }
    }
    """

    risultato = esegui_query(query)

    if not risultato:

        return False

    user = (
        risultato
        .get("data", {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ Sorare non ha restituito currentUser."
        )

        return False

    print("")
    print("========================================")
    print("✅ AUTENTICAZIONE SORARE RIUSCITA")

    print(
        f"👤 Manager: "
        f"{user.get('nickname') or 'N/D'}"
    )

    print(
        f"🔗 Slug: "
        f"{user.get('slug') or 'N/D'}"
    )

    print("========================================")
    print("")

    return True


# ============================================================
# OFFERTE PENDING
# ============================================================

def recupera_offerte():

    query = """
    query PendingOffers {

        currentUser {

            pendingTokenOffersReceived(
                first: 50
            ) {

                nodes {

                    id
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

    risultato = esegui_query(query)

    if not risultato:

        return []

    user = (
        risultato
        .get("data", {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ currentUser assente."
        )

        return []

    connessione = (
        user.get(
            "pendingTokenOffersReceived"
        )
        or {}
    )

    return (
        connessione.get("nodes")
        or []
    )


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(asset_ids):

    if not asset_ids:

        return []

    query = """
    query CardDetails(
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

                activeClub {

                    slug

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
        }
    }
    """

    risultato = esegui_query(
        query,
        {
            "assetIds": asset_ids
        }
    )

    if not risultato:

        return []

    carte = (
        risultato
        .get("data", {})
        .get("anyCards")
        or []
    )

    return carte


# ============================================================
# CONVERSIONE WEI -> EUR
# ============================================================

def wei_to_eur(wei):

    if wei is None:

        return None

    try:

        valore = Decimal(
            str(wei)
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return None

    if valore <= 0:

        return None

    # --------------------------------------------------------
    # ATTENZIONE:
    #
    # Sorare restituisce l'importo crypto in WEI.
    #
    # Non interpretiamo più:
    #
    # amounts.eur
    # amounts.fiat
    #
    # perché sono proprio i campi che ti stanno causando
    # gli errori 422.
    #
    # --------------------------------------------------------

    eth = (
        valore
        / Decimal("1000000000000000000")
    )

    return eth


# ============================================================
# PREZZO CARTA
#
# IMPORTANTE:
#
# NON utilizziamo:
#
# card(slug:)
#
# perché il tuo endpoint risponde:
#
# Field 'card' doesn't exist on type 'Query'
#
# Utilizziamo invece:
#
# tokens {
#     nfts(assetIds: ...)
# }
#
# che è il percorso corretto per il token/NFT.
# ============================================================

def recupera_prezzo_floor(carta):

    asset_id = str(
        carta.get("assetId")
        or ""
    ).strip()

    slug = str(
        carta.get("slug")
        or ""
    ).strip()

    if not asset_id:

        print(
            "      ⚠️ Asset ID assente."
        )

        return None

    print(
        f"      🔎 Ricerca prezzo floor: {slug or asset_id}"
    )

    query = """
    query TokenMarketData(
        $assetIds: [String!]!
    ) {

        tokens {

            nfts(
                assetIds: $assetIds
            ) {

                assetId
                slug
                publicMinPrice
                privateMinPrice

                latestEnglishAuction {

                    bestBid {

                        amounts {

                            wei

                        }
                    }
                }

                liveSingleSaleOffer {

                    senderSide {

                        amounts {

                            wei

                        }
                    }

                    receiverSide {

                        amounts {

                            wei

                        }
                    }
                }
            }
        }
    }
    """

    risultato = esegui_query(
        query,
        {
            "assetIds": [asset_id]
        }
    )

    if not risultato:

        print(
            "      ⚠️ Prezzo non recuperabile."
        )

        return None

    data = (
        risultato
        .get("data", {})
    )

    tokens = (
        data
        .get("tokens", {})
        .get("nfts")
        or []
    )

    if not tokens:

        print(
            "      ⚠️ Token non trovato."
        )

        return None

    token = tokens[0]

    valori_wei = []

    # ========================================================
    # PUBLIC MIN PRICE
    # ========================================================

    public_min_price = token.get(
        "publicMinPrice"
    )

    if public_min_price is not None:

        try:

            valore = Decimal(
                str(public_min_price)
            )

            if valore > 0:

                valori_wei.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            pass

    # ========================================================
    # PRIVATE MIN PRICE
    # ========================================================

    private_min_price = token.get(
        "privateMinPrice"
    )

    if private_min_price is not None:

        try:

            valore = Decimal(
                str(private_min_price)
            )

            if valore > 0:

                valori_wei.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            pass

    # ========================================================
    # ASTA
    # ========================================================

    asta = (
        token.get(
            "latestEnglishAuction"
        )
        or {}
    )

    best_bid = (
        asta.get(
            "bestBid"
        )
        or {}
    )

    amounts = (
        best_bid.get(
            "amounts"
        )
        or {}
    )

    bid_wei = amounts.get(
        "wei"
    )

    if bid_wei is not None:

        try:

            valore = Decimal(
                str(bid_wei)
            )

            if valore > 0:

                valori_wei.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            pass

    # ========================================================
    # LIVE SINGLE SALE
    # ========================================================

    sale = (
        token.get(
            "liveSingleSaleOffer"
        )
        or {}
    )

    # --------------------------------------------------------
    # senderSide
    # --------------------------------------------------------

    sender_side = (
        sale.get(
            "senderSide"
        )
        or {}
    )

    sender_amounts = (
        sender_side.get(
            "amounts"
        )
        or {}
    )

    sender_wei = sender_amounts.get(
        "wei"
    )

    if sender_wei is not None:

        try:

            valore = Decimal(
                str(sender_wei)
            )

            if valore > 0:

                valori_wei.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            pass

    # --------------------------------------------------------
    # receiverSide
    # --------------------------------------------------------

    receiver_side = (
        sale.get(
            "receiverSide"
        )
        or {}
    )

    receiver_amounts = (
        receiver_side.get(
            "amounts"
        )
        or {}
    )

    receiver_wei = receiver_amounts.get(
        "wei"
    )

    if receiver_wei is not None:

        try:

            valore = Decimal(
                str(receiver_wei)
            )

            if valore > 0:

                valori_wei.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            pass

    # ========================================================
    # NESSUN PREZZO
    # ========================================================

    if not valori_wei:

        print(
            "      ⚠️ Nessun prezzo WEI disponibile."
        )

        return None

    # ========================================================
    # FLOOR WEI
    # ========================================================

    floor_wei = min(
        valori_wei
    )

    # ========================================================
    # CONVERSIONE
    #
    # NOTA:
    #
    # WEI -> ETH è possibile direttamente.
    #
    # Per decidere il limite di €0,50 dobbiamo conoscere
    # anche il cambio ETH/EUR.
    #
    # Il codice tenta quindi prima un prezzo fiat già
    # disponibile in eventuali campi compatibili.
    # ========================================================

    prezzo_euro = recupera_cambio_eth_eur(
        floor_wei
    )

    if prezzo_euro is None:

        print(
            "      ⚠️ Prezzo ETH trovato, "
            "ma conversione EUR non disponibile."
        )

        print(
            f"      ℹ️ Floor WEI: {floor_wei}"
        )

        return None

    print(
        f"      💰 Prezzo floor: €{prezzo_euro:.4f}"
    )

    return prezzo_euro


# ============================================================
# CAMBIO ETH/EUR
# ============================================================

def recupera_cambio_eth_eur(wei):

    if wei is None:

        return None

    try:

        wei_decimal = Decimal(
            str(wei)
        )

        if wei_decimal <= 0:

            return None

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return None

    # ========================================================
    # APPROCCIO 1:
    #
    # endpoint pubblico CoinGecko.
    #
    # Serve solamente per convertire il valore WEI in EUR.
    # Non viene usato per determinare se la carta esiste.
    # ========================================================

    try:

        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "ethereum",
                "vs_currencies": "eur",
            },
            timeout=10,
        )

        if response.status_code == 200:

            dati = response.json()

            eth_eur = (
                dati
                .get("ethereum", {})
                .get("eur")
            )

            if eth_eur is not None:

                cambio = Decimal(
                    str(eth_eur)
                )

                if cambio > 0:

                    eth = (
                        wei_decimal
                        / Decimal(
                            "1000000000000000000"
                        )
                    )

                    return eth * cambio

    except Exception as e:

        print(
            f"      ⚠️ Cambio ETH/EUR non disponibile: {e}"
        )

    return None


# ============================================================
# CONTROLLO CARTA
# ============================================================

def analizza_carta(carta):

    asset_id = (
        carta.get("assetId")
    )

    slug = (
        carta.get("slug")
    )

    nome = (
        carta.get("name")
        or slug
        or asset_id
        or "Carta sconosciuta"
    )

    rarita = str(
        carta.get("rarityTyped")
        or ""
    ).upper()

    player = (
        carta.get("anyPlayer")
        or {}
    )

    club = (
        player.get("activeClub")
        or {}
    )

    competizioni = (
        club.get("activeCompetitions")
        or []
    )

    # ========================================================
    # PREZZO
    # ========================================================

    prezzo = recupera_prezzo_floor(
        carta
    )

    prezzo_verificabile = (
        prezzo is not None
    )

    prezzo_massimo = (
        Decimal(
            PREZZO_MASSIMO_CENTESIMI
        )
        / Decimal("100")
    )

    prezzo_ok = (
        prezzo_verificabile
        and prezzo <= prezzo_massimo
    )

    # ========================================================
    # CAMPIONATO
    # ========================================================

    campionato_coperto = (
        len(competizioni) > 0
    )

    # ========================================================
    # RARITÀ
    # ========================================================

    rarita_ok = (
        rarita == "LIMITED"
    )

    # ========================================================
    # IDONEITÀ
    # ========================================================

    idonea = (
        rarita_ok
        and prezzo_ok
        and campionato_coperto
    )

    # ========================================================
    # LOG
    # ========================================================

    print("")

    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: {asset_id}"
    )

    print(
        f"      Rarità: {rarita or 'N/D'}"
    )

    if prezzo is not None:

        print(
            f"      Prezzo: €{prezzo:.4f}"
        )

        if prezzo_ok:

            print(
                "      🟢 Prezzo entro il limite"
            )

        else:

            print(
                "      🔴 Prezzo superiore al limite"
            )

    else:

        print(
            "      Prezzo: N/D"
        )

        print(
            "      ⚠️ Prezzo non verificabile"
        )

    print(
        f"      Competizioni attive: "
        f"{len(competizioni)}"
    )

    if competizioni:

        print(
            "      🟢 Campionato coperto"
        )

    else:

        print(
            "      🔴 Campionato NON coperto"
        )

    if rarita_ok:

        print(
            "      🟢 Rarità LIMITED"
        )

    else:

        print(
            "      🔴 Rarità NON valida"
        )

    if idonea:

        print(
            "      ✅ CARTA IDONEA"
        )

    else:

        print(
            "      ❌ CARTA NON IDONEA"
        )

    return idonea


# ============================================================
# KULENOVIC
# ============================================================

def controlla_kulenovic(carte_richieste):

    print("")

    print(
        "🔎 CARTA/E RICHIESTA/E DAL MANAGER:"
    )

    kulenovic_presente = False

    configurato = (
        KULENOVIC_ID.strip()
        if KULENOVIC_ID
        else ""
    )

    for carta in carte_richieste:

        asset_id = str(
            carta.get("assetId")
            or ""
        ).strip()

        slug = str(
            carta.get("slug")
            or ""
        ).strip()

        collection = str(
            carta.get("collection")
            or ""
        ).strip()

        print(
            f"   Asset ID: {asset_id}"
        )

        print(
            f"   Slug: {slug}"
        )

        print(
            f"   Collection: {collection}"
        )

        # ----------------------------------------------------
        # MATCH CONFIGURAZIONE
        # ----------------------------------------------------

        match_configurazione = (
            bool(configurato)
            and (
                asset_id.lower()
                == configurato.lower()
                or
                slug.lower()
                == configurato.lower()
            )
        )

        # ----------------------------------------------------
        # MATCH SLUG
        # ----------------------------------------------------

        match_slug = (
            slug.lower()
            == KULENOVIC_SLUG.lower()
        )

        # ----------------------------------------------------
        # MATCH ASSET
        # ----------------------------------------------------

        match_asset = (
            asset_id.lower()
            == KULENOVIC_ASSET_ID.lower()
        )

        if (
            match_configurazione
            or match_slug
            or match_asset
        ):

            kulenovic_presente = True

            print(
                "   🎯 KULENOVIC RICONOSCIUTO"
            )

    if kulenovic_presente:

        print(
            "🎯 KULENOVIC RICONOSCIUTO!"
        )

    else:

        print(
            "ℹ️ Kulenovic non riconosciuto nell'offerta."
        )

        print(
            "ℹ️ L'offerta viene comunque analizzata in DRY RUN."
        )

    return kulenovic_presente


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offerta):

    offerta_id = (
        offerta.get("id")
    )

    if not offerta_id:

        return

    if offerta_id in offerte_gia_analizzate:

        return

    offerte_gia_analizzate.add(
        offerta_id
    )

    print("")
    print("========================================")
    print("📨 NUOVA OFFERTA")

    print(
        f"🆔 ID: {offerta_id}"
    )

    print(
        f"📌 Stato: {offerta.get('status')}"
    )

    sender = (
        offerta.get("sender")
        or {}
    )

    nickname = (
        sender.get("nickname")
        or sender.get("slug")
        or "Sconosciuto"
    )

    print(
        f"👤 Manager: {nickname}"
    )

    sender_side = (
        offerta.get("senderSide")
        or {}
    )

    receiver_side = (
        offerta.get("receiverSide")
        or {}
    )

    # ========================================================
    # CARTE OFFERTE DAL MANAGER
    # ========================================================

    carte_offerte = (
        sender_side.get("anyCards")
        or []
    )

    # ========================================================
    # CARTE CHE NOI DOVREMMO DARE
    # ========================================================

    carte_che_diamo = (
        receiver_side.get("anyCards")
        or []
    )

    print(
        f"📦 Carte offerte: "
        f"{len(carte_offerte)}"
    )

    print(
        f"📦 Carte richieste: "
        f"{len(carte_che_diamo)}"
    )

    # ========================================================
    # KULENOVIC
    # ========================================================

    kulenovic_presente = (
        controlla_kulenovic(
            carte_che_diamo
        )
    )

    # ========================================================
    # ASSET ID CARTE RICEVUTE
    # ========================================================

    asset_ids = [
        carta.get("assetId")
        for carta in carte_offerte
        if carta.get("assetId")
    ]

    if not asset_ids:

        print("")

        print(
            "🔴 DECISIONE: RIFIUTARE"
        )

        print(
            "   Motivo: nessuna carta ricevuta."
        )

        print(
            "🟡 DRY RUN: nessuna operazione eseguita."
        )

        print(
            "----------------------------------------"
        )

        return

    # ========================================================
    # DETTAGLI
    # ========================================================

    dettagli = recupera_dettagli_carte(
        asset_ids
    )

    if not dettagli:

        print(
            "⚠️ Impossibile recuperare "
            "i dettagli delle carte."
        )

        print(
            "----------------------------------------"
        )

        return

    # ========================================================
    # ANALISI
    # ========================================================

    carte_idonee = []

    print("")

    print(
        "🔎 ANALISI DELLE CARTE RICEVUTE:"
    )

    for carta in dettagli:

        if analizza_carta(carta):

            carte_idonee.append(
                carta
            )

    numero_totale = len(
        asset_ids
    )

    numero_idonee = len(
        carte_idonee
    )

    numero_non_idonee = (
        numero_totale
        - numero_idonee
    )

    print("")

    print(
        "----------------------------------------"
    )

    print(
        f"📊 CARTE TOTALI: "
        f"{numero_totale}"
    )

    print(
        f"📊 CARTE IDONEE: "
        f"{numero_idonee}"
    )

    print(
        f"📊 CARTE NON IDONEE: "
        f"{numero_non_idonee}"
    )

    # ========================================================
    # NESSUNA IDONEA
    # ========================================================

    if numero_idonee == 0:

        print("")

        print(
            "🔴 DECISIONE: RIFIUTARE L'OFFERTA"
        )

        print(
            "   Motivo: nessuna carta ricevuta è idonea."
        )

        print("")

        print(
            "🟡 DRY RUN: nessun rifiuto eseguito."
        )

        print(
            "----------------------------------------"
        )

        return

    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    pagamento_centesimi = (
        numero_idonee
        * PAGAMENTO_PER_CARTA_CENTESIMI
    )

    pagamento_euro = (
        Decimal(
            pagamento_centesimi
        )
        / Decimal("100")
    )

    print("")

    print(
        "🟢 DECISIONE: CONTROPROPOSTA"
    )

    print("")

    print(
        "📤 DALLA PROPOSTA VIENE RIMOSSA:"
    )

    if kulenovic_presente:

        print(
            "   ❌ Kulenovic"
        )

    else:

        print(
            "   ℹ️ Kulenovic non presente"
        )

    print("")

    print(
        "🗑️ VENGONO ELIMINATE "
        "LE CARTE NON IDONEE:"
    )

    if numero_non_idonee == 0:

        print(
            "   Nessuna"
        )

    else:

        print(
            f"   ❌ {numero_non_idonee} carta/e"
        )

    print("")

    print(
        "📥 RIMANGONO SOLO LE CARTE IDONEE:"
    )

    for carta in carte_idonee:

        nome_carta = (
            carta.get("name")
            or carta.get("slug")
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ {nome_carta}"
        )

    print("")

    print(
        f"💰 PAGAMENTO AL MANAGER: "
        f"€{pagamento_euro:.2f}"
    )

    print(
        f"   {numero_idonee} × €0,20"
    )

    print("")

    print(
        "📋 CONTROPROPOSTA PREVISTA:"
    )

    print(
        "   ❌ Noi NON cediamo Kulenovic"
    )

    for carta in carte_idonee:

        nome_carta = (
            carta.get("name")
            or carta.get("slug")
            or "Carta sconosciuta"
        )

        print(
            f"   ✅ Noi riceviamo: {nome_carta}"
        )

    print(
        f"   💰 Noi paghiamo: "
        f"€{pagamento_euro:.2f}"
    )

    print("")

    if DRY_RUN:

        print(
            "🟡 DRY RUN ATTIVO:"
        )

        print(
            "   Nessuna controproposta è stata inviata."
        )

    print(
        "----------------------------------------"
    )

    print("")


# ============================================================
# MONITORAGGIO
# ============================================================

def monitor_offerte():

    print(
        "🤖 BOT SORARE AVVIATO"
    )

    print(
        "🟡 MODALITÀ DRY RUN ATTIVA"
    )

    print(
        "⚠️ Nessun rifiuto e nessuna "
        "controproposta verranno eseguiti."
    )

    print("")

    if not verifica_account():

        print(
            "❌ Impossibile autenticarsi a Sorare."
        )

        return

    while True:

        try:

            print(
                "🔎 Controllo offerte..."
            )

            offerte = recupera_offerte()

            print(
                f"📨 Offerte pending ricevute: "
                f"{len(offerte)}"
            )

            for offerta in offerte:

                elabora_offerta(
                    offerta
                )

        except Exception as e:

            print(
                f"⚠️ Errore nel ciclo: {e}"
            )

        time.sleep(60)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    global monitoraggio_avviato

    with lock_avvio:

        if not monitoraggio_avviato:

            monitoraggio_avviato = True

            thread = threading.Thread(
                target=monitor_offerte,
                daemon=True,
            )

            thread.start()

            return (
                "Bot Sorare avviato "
                "in modalità DRY RUN."
            )

    return (
        "Bot Sorare già attivo."
    )


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
