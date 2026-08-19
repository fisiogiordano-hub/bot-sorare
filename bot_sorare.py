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

# Può essere:
# - asset ID
# - slug
# - oppure lasciata vuota
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

# ============================================================
# SICUREZZA
# ============================================================

# IMPORTANTE:
# Lasciamo DRY RUN attivo.
# Nessun rifiuto e nessuna controproposta reale.
DRY_RUN = True

# ============================================================
# REGOLE DEL BOT
# ============================================================

PREZZO_MASSIMO_CENTESIMI = 50
PAGAMENTO_PER_CARTA_CENTESIMI = 20

# ============================================================
# SORARE API
# ============================================================

# Endpoint GraphQL
SORARE_API_URL = "https://api.sorare.com/graphql"

# ============================================================
# KULENOVIC
# ============================================================

# Valori conosciuti dall'offerta reale che stai testando.
#
# Il bot li usa come fallback anche se KULENOVIC_ID
# su Render è vuoto o contiene un valore errato.

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
            f"🌐 Sorare HTTP: "
            f"{response.status_code}"
        )

        if response.status_code != 200:

            print(
                "❌ Risposta HTTP non valida:"
            )

            print(
                response.text[:3000]
            )

            return None

        risultato = response.json()

        if risultato.get("errors"):

            print(
                "❌ Errori GraphQL:"
            )

            for errore in risultato["errors"]:

                print(
                    "- "
                    + str(
                        errore.get(
                            "message",
                            "Errore sconosciuto"
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
            "❌ Sorare non ha restituito "
            "currentUser."
        )

        return False

    print("")
    print("========================================")
    print("✅ AUTENTICAZIONE SORARE RIUSCITA")

    print(
        f"👤 Manager: "
        f"{user.get('nickname')}"
    )

    print(
        f"🔗 Slug: "
        f"{user.get('slug')}"
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
# UTILITÀ SLUG
# ============================================================

def estrai_player_slug(slug):

    if not slug:

        return None

    # Esempio:
    #
    # alessio-cragno-2023-limited-157
    #
    # diventa:
    #
    # alessio-cragno

    parti = slug.split("-")

    if len(parti) < 4:

        return None

    # Cerchiamo la parte dell'anno.
    indice_anno = None

    for i, parte in enumerate(parti):

        if (
            len(parte) == 4
            and parte.isdigit()
            and parte.startswith("20")
        ):

            indice_anno = i
            break

    if indice_anno is None:

        return None

    player_slug = "-".join(
        parti[:indice_anno]
    )

    return player_slug or None


# ============================================================
# PREZZO: TOKEN PRICES
# ============================================================

def recupera_prezzo_floor(carta):

    slug = carta.get("slug")

    if not slug:

        print(
            "      ⚠️ Slug carta assente."
        )

        return None

    player_slug = estrai_player_slug(
        slug
    )

    if not player_slug:

        print(
            f"      ⚠️ Impossibile estrarre "
            f"player slug da: {slug}"
        )

        return None

    rarita = str(
        carta.get("rarityTyped")
        or ""
    ).upper()

    if not rarita:

        print(
            "      ⚠️ Rarità assente."
        )

        return None

    print(
        f"      🔎 Ricerca prezzo floor: "
        f"{player_slug}"
    )

    # ========================================================
    # QUERY CORRETTA
    #
    # Sorare documenta tokenPrices come fonte degli ultimi
    # prezzi pubblici di un giocatore/rara/collection.
    #
    # Usiamo SOLO:
    #
    # amounts {
    #     eur
    # }
    #
    # e non:
    # eurCents
    # price
    # priceInFiat
    # amount
    # amountInFiat
    # ========================================================

    query = """
    query TokenPrices(
        $playerSlug: String!
        $rarity: Rarity!
        $collection: Collection!
    ) {

        tokens {

            tokenPrices(
                playerSlug: $playerSlug
                rarity: $rarity
                collection: $collection
            ) {

                amounts {

                    eur

                }

                date
            }
        }
    }
    """

    variables = {
        "playerSlug": player_slug,
        "rarity": rarita,
        "collection": "FOOTBALL",
    }

    risultato = esegui_query(
        query,
        variables
    )

    if not risultato:

        print(
            "      ⚠️ Prezzo non recuperabile"
        )

        return None

    dati = (
        risultato
        .get("data", {})
        .get("tokens", {})
    )

    prezzi = (
        dati.get("tokenPrices")
        or []
    )

    valori = []

    for prezzo in prezzi:

        amounts = (
            prezzo.get("amounts")
            or {}
        )

        eur = amounts.get("eur")

        if eur is None:

            continue

        try:

            valore = Decimal(
                str(eur)
            )

            if valore > 0:

                valori.append(
                    valore
                )

        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ):

            continue

    if not valori:

        print(
            "      ⚠️ Nessun prezzo EUR "
            "disponibile."
        )

        return None

    # ========================================================
    # FLOOR
    # ========================================================

    floor = min(valori)

    print(
        f"      💰 Prezzo floor trovato: "
        f"€{floor:.2f}"
    )

    return floor


# ============================================================
# CONTROLLO CARTA
# ============================================================

def analizza_carta(carta):

    asset_id = carta.get(
        "assetId"
    )

    slug = carta.get(
        "slug"
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
        <= Decimal(
            PREZZO_MASSIMO_CENTESIMI
        ) / Decimal("100")
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
        f"      Asset ID: "
        f"{asset_id}"
    )

    print(
        f"      Rarità: "
        f"{rarita or 'N/D'}"
    )

    if prezzo is not None:

        print(
            f"      Prezzo: "
            f"€{prezzo:.2f}"
        )

        if prezzo_ok:

            print(
                "      🟢 Prezzo entro "
                "il limite"
            )

        else:

            print(
                "      🔴 Prezzo superiore "
                "al limite"
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
        "🔎 CARTA/E RICHIESTA/E "
        "DAL MANAGER:"
    )

    kulenovic_presente = False

    # Valori configurati su Render
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
            f"   Asset ID: "
            f"{asset_id}"
        )

        print(
            f"   Slug: "
            f"{slug}"
        )

        print(
            f"   Collection: "
            f"{collection}"
        )

        # ====================================================
        # MATCH
        #
        # 1. valore configurato
        # 2. slug ufficiale noto
        # 3. asset ID ufficiale noto
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

        match_slug = (
            slug.lower()
            == KULENOVIC_SLUG.lower()
        )

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
            "ℹ️ Kulenovic non riconosciuto "
            "nell'offerta."
        )

        print(
            "ℹ️ L'offerta viene comunque "
            "analizzata in DRY RUN."
        )

    return kulenovic_presente


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offerta):

    offerta_id = offerta.get(
        "id"
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
        f"🆔 ID: "
        f"{offerta_id}"
    )

    print(
        f"📌 Stato: "
        f"{offerta.get('status')}"
    )

    sender = (
        offerta.get("sender")
        or {}
    )

    print(
        f"👤 Manager: "
        f"{sender.get('nickname') "
        "or sender.get('slug') "
        "or 'Sconosciuto'}"
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
        sender_side.get(
            "anyCards"
        )
        or []
    )

    # ========================================================
    # CARTE CHE NOI DOVREMMO DARE
    # ========================================================

    carte_che_diamo = (
        receiver_side.get(
            "anyCards"
        )
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
            "🟡 DRY RUN: nessuna operazione "
            "eseguita."
        )

        print("----------------------------------------")

        return

    # ========================================================
    # DETTAGLI
    # ========================================================

    dettagli = (
        recupera_dettagli_carte(
            asset_ids
        )
    )

    if not dettagli:

        print(
            "⚠️ Impossibile recuperare "
            "i dettagli delle carte."
        )

        print("----------------------------------------")

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

    print("----------------------------------------")

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
            "   Motivo: nessuna carta "
            "ricevuta è idonea."
        )

        print("")

        print(
            "🟡 DRY RUN: "
            "nessun rifiuto eseguito."
        )

        print("----------------------------------------")

        return

    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    pagamento_centesimi = (
        numero_idonee
        * PAGAMENTO_PER_CARTA_CENTESIMI
    )

    pagamento_euro = (
        Decimal(pagamento_centesimi)
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

        print(
            f"   ✅ "
            f"{carta.get('name') "
            "or carta.get('slug')}"
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

        print(
            f"   ✅ Noi riceviamo: "
            f"{carta.get('name') "
            "or carta.get('slug')}"
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
            "   Nessuna controproposta "
            "è stata inviata."
        )

    print("----------------------------------------")
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
            "❌ Impossibile autenticarsi "
            "a Sorare."
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
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
