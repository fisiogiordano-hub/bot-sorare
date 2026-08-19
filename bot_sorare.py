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

# Puoi lasciare qui l'assetId di Kulenovic.
# Il controllo usa anche lo slug, quindi non dipende solo da questa variabile.
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

SORARE_API_URL = "https://api.sorare.com/graphql"

# ============================================================
# SICUREZZA
# ============================================================

# TRUE = il bot analizza soltanto.
# NON rifiuta e NON invia controproposte.
DRY_RUN = True

# ============================================================
# REGOLE
# ============================================================

PREZZO_MASSIMO_CENTESIMI = 50
PAGAMENTO_PER_CARTA_CENTESIMI = 20

offerte_gia_analizzate = set()

monitoraggio_avviato = False
lock_avvio = threading.Lock()


# ============================================================
# HEADERS SORARE
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
        except Exception:

            print(
                "❌ Sorare ha restituito "
                "una risposta non JSON."
            )

            return None

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

    # IMPORTANTE:
    #
    # NON chiediamo più:
    # - eur
    # - eurCents
    # - price
    #
    # perché il tuo endpoint GraphQL ha già restituito 422
    # per questi campi.
    #
    # Recuperiamo solamente i dati necessari alla carta.
    #
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
# PREZZO
# ============================================================

def recupera_prezzo_carta(carta):

    """
    Recupera il prezzo senza utilizzare campi GraphQL
    che il tuo endpoint ha già dichiarato inesistenti.

    Per ora, se il prezzo non è presente nei dati restituiti
    dalla query, viene considerato NON VERIFICABILE.

    Questo evita di classificare erroneamente una carta
    come idonea.
    """

    # Possibili dati eventualmente già presenti
    # nell'oggetto restituito da Sorare.

    valori_possibili = [
        carta.get("eurCents"),
        carta.get("priceCents"),
        carta.get("minimumPriceCents"),
    ]

    for valore in valori_possibili:

        if valore is None:
            continue

        try:
            return int(valore)
        except Exception:
            pass

    # Se Sorare non ha restituito un prezzo,
    # non inventiamo il valore.
    return None


# ============================================================
# CONTROLLO CARTA
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

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    prezzo_centesimi = recupera_prezzo_carta(
        carta
    )

    # --------------------------------------------------------
    # COMPETIZIONI
    # --------------------------------------------------------

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

    campionato_coperto = (
        len(competizioni) > 0
    )

    # --------------------------------------------------------
    # REGOLE
    # --------------------------------------------------------

    rarita_ok = (
        rarita == "LIMITED"
    )

    prezzo_verificabile = (
        prezzo_centesimi is not None
    )

    prezzo_ok = (
        prezzo_verificabile
        and prezzo_centesimi
        <= PREZZO_MASSIMO_CENTESIMI
    )

    idonea = (
        rarita_ok
        and prezzo_ok
        and campionato_coperto
    )

    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    print("")
    print(
        f"   📄 {nome}"
    )

    print(
        f"      Asset ID: {asset_id}"
    )

    print(
        f"      Rarità: "
        f"{rarita or 'N/D'}"
    )

    if prezzo_centesimi is not None:

        print(
            f"      Prezzo: "
            f"€{prezzo_centesimi / 100:.2f}"
        )

        if prezzo_ok:

            print(
                "      🟢 Prezzo ≤ €0,50"
            )

        else:

            print(
                "      🔴 Prezzo > €0,50"
            )

    else:

        print(
            "      Prezzo: "
            "N/D (non verificabile)"
        )

    print(
        f"      Competizioni attive: "
        f"{len(competizioni)}"
    )

    if campionato_coperto:

        print(
            "      🟢 Campionato coperto"
        )

    else:

        print(
            "      🔴 Campionato NON coperto"
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

    for carta in carte_richieste:

        asset_id = carta.get(
            "assetId"
        )

        slug = carta.get(
            "slug"
        )

        print(
            f"   Asset ID: {asset_id}"
        )

        print(
            f"   Slug: {slug}"
        )

        print(
            f"   Collection: "
            f"{carta.get('collection')}"
        )

        # ----------------------------------------------------
        # Riconoscimento robusto di Kulenovic
        # ----------------------------------------------------

        if slug:

            slug_lower = slug.lower()

            if (
                "kulenovic"
                in slug_lower
            ):

                kulenovic_presente = True

        if (
            KULENOVIC_ID
            and (
                asset_id == KULENOVIC_ID
                or slug == KULENOVIC_ID
            )
        ):

            kulenovic_presente = True

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

    if (
        offerta_id
        in offerte_gia_analizzate
    ):
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

    # --------------------------------------------------------
    # Carte OFFERTE dal manager
    # --------------------------------------------------------

    carte_offerte = (
        sender_side.get(
            "anyCards"
        )
        or []
    )

    # --------------------------------------------------------
    # Carte RICHIESTE dal manager
    # --------------------------------------------------------

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
            "   Motivo: nessuna carta ricevuta."
        )

        print(
            "🟡 DRY RUN: "
            "nessun rifiuto eseguito."
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
    #
    # REGOLA:
    #
    # 1. Kulenovic viene RIMOSSO.
    # 2. Le carte non idonee vengono RIMOSSE.
    # 3. Restano SOLO le carte idonee.
    # 4. Per ogni carta idonea paghiamo €0,20.
    #
    # ========================================================

    pagamento_centesimi = (
        numero_idonee
        * PAGAMENTO_PER_CARTA_CENTESIMI
    )

    pagamento_euro = (
        pagamento_centesimi
        / 100
    )

    print("")
    print(
        "🟢 DECISIONE: CONTROPROPOSTA"
    )

    print("")
    print(
        "📤 DALLA PROPOSTA VIENE RIMOSSO:"
    )

    if kulenovic_presente:

        print(
            "   ❌ Kulenovic"
        )

    else:

        print(
            "   ❌ Kulenovic "
            "(non riconosciuto, comunque NON viene ceduto)"
        )

    print("")
    print(
        "🗑️ VENGONO RIMOSSE LE "
        "CARTE NON IDONEE:"
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
            f"{carta.get('name') or carta.get('slug')}"
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
        "   ❌ Kulenovic viene RIMOSSO"
    )

    for carta in carte_idonee:

        print(
            f"   ✅ Riceviamo: "
            f"{carta.get('name') or carta.get('slug')}"
        )

    print(
        f"   💰 Paghiamo: "
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
                f"⚠️ Errore nel ciclo: "
                f"{e}"
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
