import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

# ============================================================
# CONFIGURAZIONE
# ============================================================

SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
SORARE_JWT_AUD = os.getenv("SORARE_JWT_AUD", "").strip()
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

# IMPORTANTE:
# Per ora NON esegue rifiuti o controproposte reali.
DRY_RUN = True

PREZZO_MASSIMO_CENTESIMI = 50
PAGAMENTO_PER_CARTA_CENTESIMI = 20

SORARE_API_URL = "https://api.sorare.com/graphql"

offerte_gia_analizzate = set()
monitoraggio_avviato = False
lock_avvio = threading.Lock()


# ============================================================
# API SORARE
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
                response.text[:2000]
            )

            return None

        risultato = response.json()

        if risultato.get("errors"):

            print(
                "❌ Errori GraphQL:"
            )

            for errore in risultato["errors"]:

                print(
                    f"- {errore.get('message', 'Errore sconosciuto')}"
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
        f"👤 Manager: {user.get('nickname')}"
    )
    print(
        f"🔗 Slug: {user.get('slug')}"
    )
    print("========================================")
    print("")

    return True


# ============================================================
# RECUPERO OFFERTE
# ============================================================

def recupera_offerte():

    query = """
    query PendingOffers {

        currentUser {

            pendingTokenOffersReceived(first: 50) {

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
    query CardDetails($assetIds: [String!]) {

        anyCards(assetIds: $assetIds) {

            assetId
            slug
            name
            rarityTyped

            publicMinPrices {
                eurCents
                referenceCurrency
            }

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

    return (
        risultato
        .get("data", {})
        .get("anyCards")
        or []
    )


# ============================================================
# STAMPA CARTA
# ============================================================

def stampa_carta(carta, prefisso=""):

    print(
        f"{prefisso}Nome: "
        f"{carta.get('name') or 'N/D'}"
    )

    print(
        f"{prefisso}Asset ID: "
        f"{carta.get('assetId') or 'N/D'}"
    )

    print(
        f"{prefisso}Slug: "
        f"{carta.get('slug') or 'N/D'}"
    )

    print(
        f"{prefisso}Collection: "
        f"{carta.get('collection') or 'N/D'}"
    )


# ============================================================
# ANALISI CARTA
# ============================================================

def analizza_carta(carta):

    asset_id = carta.get(
        "assetId"
    )

    nome = (
        carta.get("name")
        or carta.get("slug")
        or asset_id
        or "Carta sconosciuta"
    )

    rarita = str(
        carta.get("rarityTyped")
        or ""
    ).upper()

    prezzi = (
        carta.get("publicMinPrices")
        or {}
    )

    prezzo_centesimi = (
        prezzi.get("eurCents")
    )

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

    # Per ora registriamo semplicemente
    # le competizioni restituite da Sorare.
    campionato_coperto = (
        len(competizioni) > 0
    )

    rarita_ok = (
        rarita == "LIMITED"
    )

    prezzo_ok = (
        prezzo_centesimi is not None
        and int(prezzo_centesimi)
        <= PREZZO_MASSIMO_CENTESIMI
    )

    idonea = (
        rarita_ok
        and prezzo_ok
        and campionato_coperto
    )

    print("")
    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: {asset_id}"
    )

    print(
        f"      Rarità: {rarita}"
    )

    if prezzo_centesimi is not None:

        print(
            f"      Prezzo: "
            f"€{int(prezzo_centesimi) / 100:.2f}"
        )

    else:

        print(
            "      Prezzo: N/D"
        )

    print(
        f"      Competizioni attive: "
        f"{len(competizioni)}"
    )

    if competizioni:

        print(
            "      Competizioni:"
        )

        for competizione in competizioni:

            print(
                f"         - "
                f"{competizione.get('slug')}"
            )

    if idonea:

        print(
            "      ✅ IDONEA"
        )

    else:

        print(
            "      ❌ NON IDONEA"
        )

    return idonea


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
        f"📌 Stato: "
        f"{offerta.get('status')}"
    )

    sender = (
        offerta.get("sender")
        or {}
    )

    print(
        f"👤 Manager: "
        f"{sender.get('nickname') or sender.get('slug') or 'Sconosciuto'}"
    )

    sender_side = (
        offerta.get("senderSide")
        or {}
    )

    receiver_side = (
        offerta.get("receiverSide")
        or {}
    )

    carte_offerte = (
        sender_side.get("anyCards")
        or []
    )

    carte_che_diamo = (
        receiver_side.get("anyCards")
        or []
    )

    print(
        f"📦 Carte offerte dal manager: "
        f"{len(carte_offerte)}"
    )

    print(
        f"📦 Carte richieste al bot: "
        f"{len(carte_che_diamo)}"
    )

    # ========================================================
    # DEBUG CARTA RICHIESTA
    # ========================================================

    print("")
    print(
        "🔎 CARTA/E RICHIESTA/E DAL MANAGER:"
    )

    for carta in carte_che_diamo:

        stampa_carta(
            carta,
            "   "
        )

    # ========================================================
    # IDENTIFICAZIONE KULENOVIC
    # ========================================================

    kulenovic_presente = False

    for carta in carte_che_diamo:

        asset_id = (
            carta.get("assetId")
            or ""
        )

        slug = (
            carta.get("slug")
            or ""
        )

        if (
            KULENOVIC_ID
            and (
                asset_id == KULENOVIC_ID
                or slug == KULENOVIC_ID
            )
        ):

            kulenovic_presente = True

            break

    if kulenovic_presente:

        print("")
        print(
            "🎯 KULENOVIC IDENTIFICATO "
            "TRAMITE KULENOVIC_ID."
        )

    else:

        print("")
        print(
            "⚠️ KULENOVIC_ID non coincide "
            "con assetId/slug della carta."
        )

        print(
            "⚠️ Per questa fase di test "
            "analizziamo comunque l'offerta."
        )

    # ========================================================
    # CARTE RICEVUTE
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
            "   Motivo: "
            "nessuna carta ricevuta."
        )

        if DRY_RUN:

            print(
                "🟡 DRY RUN: "
                "nessuna operazione eseguita."
            )

        print("----------------------------------------")

        return

    # ========================================================
    # DETTAGLI CARTE
    # ========================================================

    dettagli = recupera_dettagli_carte(
        asset_ids
    )

    if not dettagli:

        print("")
        print(
            "⚠️ Impossibile recuperare "
            "i dettagli delle carte ricevute."
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

    numero_idonee = len(
        carte_idonee
    )

    numero_totale = len(
        asset_ids
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

    # ========================================================
    # NESSUNA IDONEA
    # ========================================================

    if numero_idonee == 0:

        print("")
        print(
            "🔴 DECISIONE: "
            "RIFIUTARE L'OFFERTA"
        )

        print(
            "   Motivo: "
            "nessuna carta ricevuta è idonea."
        )

        if DRY_RUN:

            print(
                "🟡 DRY RUN: "
                "nessun rifiuto eseguito."
            )

        print("----------------------------------------")
        print("")

        return

    # ========================================================
    # CONTROPROPOSTA SIMULATA
    # ========================================================

    pagamento_centesimi = (
        numero_idonee
        * PAGAMENTO_PER_CARTA_CENTESIMI
    )

    pagamento_euro = (
        pagamento_centesimi / 100
    )

    print("")
    print(
        "🟢 DECISIONE: "
        "CONTROPROPOSTA"
    )

    print("")
    print(
        "📤 DALLA CONTROPROPOSTA "
        "VIENE RIMOSSA:"
    )

    print(
        "   ❌ KULENOVIC"
    )

    print("")
    print(
        "📥 RIMANGONO SOLO LE "
        "CARTE IDONEE:"
    )

    for carta in carte_idonee:

        print(
            f"   ✅ "
            f"{carta.get('name') or carta.get('slug')}"
        )

    numero_non_idonee = (
        numero_totale
        - numero_idonee
    )

    print("")
    print(
        f"🗑️ CARTE NON IDONEE ESCLUSE: "
        f"{numero_non_idonee}"
    )

    print("")
    print(
        f"💰 PAGAMENTO AL MANAGER: "
        f"€{pagamento_euro:.2f}"
    )

    print(
        f"   {numero_idonee} × €0,20"
    )

    if DRY_RUN:

        print("")
        print(
            "🟡 DRY RUN: "
            "nessuna controproposta eseguita."
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
