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

SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
SORARE_JWT_AUD = os.getenv("SORARE_JWT_AUD", "").strip()

# Può essere asset ID, slug oppure vuota.
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()


# ============================================================
# SICUREZZA
# ============================================================

# SEMPRE DRY RUN.
# Nessun rifiuto e nessuna controproposta reale.
DRY_RUN = True


# ============================================================
# REGOLE BOT
# ============================================================

# 50 centesimi = €0,50
PREZZO_MASSIMO_CENTESIMI = 50

# €0,20 per ogni carta idonea
PAGAMENTO_PER_CARTA_CENTESIMI = 20


# ============================================================
# SORARE API
# ============================================================

SORARE_API_URL = "https://api.sorare.com/graphql"


# ============================================================
# KULENOVIC
# ============================================================

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"

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

        if response.status_code != 200:

            print(
                "❌ Risposta HTTP non valida:"
            )

            print(
                response.text[:3000]
            )

            return None

        try:

            risultato = response.json()

        except ValueError:

            print(
                "❌ Sorare ha restituito una risposta "
                "non JSON:"
            )

            print(
                response.text[:3000]
            )

            return None

        # ====================================================
        # ERRORI GRAPHQL
        # ====================================================

        risultato_errors = risultato.get(
            "errors"
        )

        if risultato_errors:

            print(
                "❌ Errori GraphQL:"
            )

            for errore in risultato_errors:

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
# CONVERSIONE PREZZO EUR
# ============================================================

def converti_euro(valore):

    if valore is None:

        return None

    try:

        prezzo = Decimal(
            str(valore)
        )

        if prezzo > 0:

            return prezzo

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        pass

    return None


# ============================================================
# PREZZO CARTA
#
# IMPORTANTE:
#
# NON viene più utilizzato:
#
#     card(slug: $slug)
#
# perché il tuo endpoint Sorare restituisce:
#
#     Field 'card' doesn't exist on type 'Query'
#
# Usiamo invece:
#
#     anyCards(slugs: $slugs)
#
# ============================================================

def recupera_prezzo_floor(carta):

    slug = str(
        carta.get("slug")
        or ""
    ).strip()

    if not slug:

        print(
            "      ⚠️ Slug carta assente."
        )

        return None

    print(
        f"      🔎 Ricerca prezzo floor: {slug}"
    )

    query = """
    query CardFloorPrice(
        $slugs: [String!]!
    ) {

        anyCards(
            slugs: $slugs
        ) {

            slug
            name
            rarityTyped

            publicMinPrices {

                eur
                wei

            }

            liveSingleSaleOffer {

                receiverSide {

                    amounts {

                        eur
                        wei

                    }
                }
            }

            latestEnglishAuction {

                bestBid {

                    amounts {

                        eur
                        wei

                    }
                }
            }
        }
    }
    """

    risultato = esegui_query(
        query,
        {
            "slugs": [slug]
        }
    )

    if not risultato:

        print(
            "      ⚠️ Prezzo non recuperabile."
        )

        return None

    carte = (
        risultato
        .get("data", {})
        .get("anyCards")
        or []
    )

    if not carte:

        print(
            "      ⚠️ Carta non trovata."
        )

        return None

    dati = None

    # ========================================================
    # CERCA ESATTAMENTE LO SLUG RICHIESTO
    # ========================================================

    for carta_api in carte:

        api_slug = str(
            carta_api.get("slug")
            or ""
        ).strip()

        if api_slug.lower() == slug.lower():

            dati = carta_api

            break

    # ========================================================
    # FALLBACK
    # ========================================================

    if dati is None:

        dati = carte[0]

    valori = []

    # ========================================================
    # 1. PUBLIC MIN PRICE
    # ========================================================

    public_min = (
        dati.get(
            "publicMinPrices"
        )
        or {}
    )

    public_eur = public_min.get(
        "eur"
    )

    prezzo_public = converti_euro(
        public_eur
    )

    if prezzo_public is not None:

        valori.append(
            prezzo_public
        )

        print(
            f"      💰 Public min price: "
            f"€{prezzo_public:.2f}"
        )

    # ========================================================
    # 2. LIVE SINGLE SALE
    # ========================================================

    offerta = (
        dati.get(
            "liveSingleSaleOffer"
        )
        or {}
    )

    receiver_side = (
        offerta.get(
            "receiverSide"
        )
        or {}
    )

    amounts = (
        receiver_side.get(
            "amounts"
        )
        or {}
    )

    eur = amounts.get(
        "eur"
    )

    prezzo_live = converti_euro(
        eur
    )

    if prezzo_live is not None:

        valori.append(
            prezzo_live
        )

        print(
            f"      💰 Live sale: "
            f"€{prezzo_live:.2f}"
        )

    # ========================================================
    # 3. BEST BID ASTA
    # ========================================================

    asta = (
        dati.get(
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

    bid_amounts = (
        best_bid.get(
            "amounts"
        )
        or {}
    )

    bid_eur = bid_amounts.get(
        "eur"
    )

    prezzo_bid = converti_euro(
        bid_eur
    )

    if prezzo_bid is not None:

        valori.append(
            prezzo_bid
        )

        print(
            f"      💰 Best bid: "
            f"€{prezzo_bid:.2f}"
        )

    # ========================================================
    # NESSUN PREZZO
    # ========================================================

    if not valori:

        print(
            "      ⚠️ Nessun prezzo EUR disponibile."
        )

        return None

    # ========================================================
    # FLOOR
    # ========================================================

    floor = min(
        valori
    )

    print(
        f"      ✅ FLOOR RILEVATO: "
        f"€{floor:.2f}"
    )

    return floor


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

    prezzo_ok = (
        prezzo_verificabile
        and prezzo
        <= (
            Decimal(
                PREZZO_MASSIMO_CENTESIMI
            )
            / Decimal("100")
        )
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
            f"      Prezzo: €{prezzo:.2f}"
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

        # ====================================================
        # MATCH CONFIGURAZIONE
        # ====================================================

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

        # ====================================================
        # MATCH SLUG
        # ====================================================

        match_slug = (
            slug.lower()
            == KULENOVIC_SLUG.lower()
        )

        # ====================================================
        # MATCH ASSET
        # ====================================================

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
            "ℹ️ L'offerta viene comunque analizzata "
            "in DRY RUN."
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
    # CARTE OFFERTE
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
    # ASSET ID
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
        "⚠️ Nessun rifiuto e nessuna controproposta "
        "verranno eseguiti."
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
                "Bot Sorare avviato in modalità DRY RUN."
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
