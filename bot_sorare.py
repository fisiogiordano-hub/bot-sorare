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

# Per ora SOLO TEST.
# Il bot NON rifiuta e NON invia controproposte.
DRY_RUN = True

# Regole del bot
PREZZO_MASSIMO_EURO = 0.50
PAGAMENTO_PER_CARTA_EURO = 0.20

SORARE_API_URL = "https://api.sorare.com/graphql"

offerte_gia_analizzate = set()
monitoraggio_avviato = False
lock_avvio = threading.Lock()


# ============================================================
# API SORARE
# ============================================================

def crea_headers():
    if not SORARE_TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato.")

    headers = {
        "Content-Type": "application/json",
    }

    # JWT moderno
    if SORARE_TOKEN.lower().startswith("bearer "):
        token = SORARE_TOKEN
    else:
        token = f"Bearer {SORARE_TOKEN}"

    headers["Authorization"] = token

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

        print(f"🌐 Sorare HTTP: {response.status_code}")

        if response.status_code != 200:
            print("❌ Risposta HTTP non valida:")
            print(response.text[:2000])
            return None

        risultato = response.json()

        if risultato.get("errors"):
            print("❌ Errori GraphQL:")
            for errore in risultato["errors"]:
                print(errore.get("message"))

        return risultato

    except Exception as e:
        print(f"❌ Errore richiesta Sorare: {e}")
        return None


# ============================================================
# TEST AUTENTICAZIONE + OFFERTE
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

    user = risultato.get("data", {}).get("currentUser")

    if not user:
        print("❌ Sorare non ha restituito currentUser.")
        print("Controlleremo il token/audience nei log.")
        return False

    print("")
    print("========================================")
    print("✅ AUTENTICAZIONE SORARE RIUSCITA")
    print(f"👤 Manager: {user.get('nickname')}")
    print(f"🔗 Slug: {user.get('slug')}")
    print("========================================")
    print("")

    return True


# ============================================================
# RECUPERO OFFERTE RICEVUTE
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
                        amounts {
                            eur
                        }
                        anyCards {
                            assetId
                            slug
                            collection
                        }
                    }
                    receiverSide {
                        amounts {
                            eur
                        }
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

    user = risultato.get("data", {}).get("currentUser")

    if not user:
        print("❌ currentUser assente nella risposta.")
        return []

    connessione = user.get("pendingTokenOffersReceived") or {}
    offerte = connessione.get("nodes") or []

    return offerte


# ============================================================
# DETTAGLI DELLE CARTE
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
                eur
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
        {"assetIds": asset_ids},
    )

    if not risultato:
        return []

    return risultato.get("data", {}).get("anyCards") or []


# ============================================================
# CONTROLLO CARTA
# ============================================================

def analizza_carta(carta):
    asset_id = carta.get("assetId")
    nome = (
        carta.get("name")
        or carta.get("slug")
        or asset_id
        or "Carta sconosciuta"
    )

    rarita = str(carta.get("rarityTyped") or "").upper()

    prezzi = carta.get("publicMinPrices") or {}
    prezzo = prezzi.get("eur")

    player = carta.get("anyPlayer") or {}
    club = player.get("activeClub") or {}

    competizioni = club.get("activeCompetitions") or []

    campionato_coperto = len(competizioni) > 0

    idonea = (
        rarita == "LIMITED"
        and prezzo is not None
        and float(prezzo) <= PREZZO_MASSIMO_EURO
        and campionato_coperto
    )

    print("")
    print(f"   📄 {nome}")
    print(f"      Asset ID: {asset_id}")
    print(f"      Rarità: {rarita}")
    print(f"      Prezzo: €{prezzo if prezzo is not None else 'N/D'}")
    print(
        f"      Competizioni attive: "
        f"{len(competizioni)}"
    )

    if idonea:
        print("      ✅ IDONEA")
    else:
        print("      ❌ NON IDONEA")

    return idonea


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offerta):
    offerta_id = offerta.get("id")

    if not offerta_id:
        return

    if offerta_id in offerte_gia_analizzate:
        return

    offerte_gia_analizzate.add(offerta_id)

    print("")
    print("========================================")
    print("📨 NUOVA OFFERTA")
    print(f"🆔 ID: {offerta_id}")
    print(f"📌 Stato: {offerta.get('status')}")

    sender = offerta.get("sender") or {}
    print(
        f"👤 Manager: "
        f"{sender.get('nickname') or sender.get('slug') or 'Sconosciuto'}"
    )

    sender_side = offerta.get("senderSide") or {}
    receiver_side = offerta.get("receiverSide") or {}

    carte_offerte = sender_side.get("anyCards") or []
    carte_che_diamo = receiver_side.get("anyCards") or []

    print(f"📦 Carte offerte dal manager: {len(carte_offerte)}")
    print(f"📦 Carte richieste al bot: {len(carte_che_diamo)}")

    # --------------------------------------------------------
    # CONTROLLO KULENOVIC
    # --------------------------------------------------------

    kulenovic_presente = False

    for carta in carte_che_diamo:
        if (
            carta.get("assetId") == KULENOVIC_ID
            or carta.get("slug") == KULENOVIC_ID
        ):
            kulenovic_presente = True
            break

    if not kulenovic_presente:
        print("ℹ️ Offerta ignorata: Kulenovic non è presente.")
        print("========================================")
        return

    print("🎯 KULENOVIC PRESENTE!")

    # --------------------------------------------------------
    # DETTAGLI CARTE RICEVUTE
    # --------------------------------------------------------

    asset_ids = [
        carta.get("assetId")
        for carta in carte_offerte
        if carta.get("assetId")
    ]

    dettagli = recupera_dettagli_carte(asset_ids)

    if not dettagli:
        print("⚠️ Impossibile recuperare i dettagli delle carte.")
        print("========================================")
        return

    carte_idonee = []

    print("")
    print("🔎 ANALISI DELLE CARTE:")

    for carta in dettagli:
        if analizza_carta(carta):
            carte_idonee.append(carta)

    # --------------------------------------------------------
    # DECISIONE
    # --------------------------------------------------------

    numero_idonee = len(carte_idonee)

    print("")
    print("----------------------------------------")
    print(f"📊 CARTE IDONEE: {numero_idonee}")

    if numero_idonee == 0:
        print("🔴 DECISIONE: RIFIUTARE L'OFFERTA")
        print("🟡 DRY RUN: nessuna operazione eseguita.")
        print("----------------------------------------")
        print("")

        return

    conguaglio = numero_idonee * PAGAMENTO_PER_CARTA_EURO

    print(f"💰 CONGUAGLIO: €{conguaglio:.2f}")
    print("🟢 DECISIONE: CONTROPROPOSTA")
    print("")
    print("La controproposta prevista sarebbe:")

    for carta in carte_idonee:
        print(
            f"   ➡️ Ricevere: "
            f"{carta.get('name') or carta.get('slug')}"
        )

    print(f"   ➡️ Ricevere anche €{conguaglio:.2f}")
    print("   ➡️ Kulenovic: NON viene ceduto")

    print("")
    print("🟡 DRY RUN: nessuna operazione eseguita.")
    print("----------------------------------------")
    print("")


# ============================================================
# CICLO MONITORAGGIO
# ============================================================

def monitor_offerte():
    print("🤖 BOT SORARE AVVIATO")
    print("🟡 MODALITÀ DRY RUN ATTIVA")
    print("⚠️ Nessun rifiuto e nessuna controproposta verranno eseguiti.")
    print("")

    if not verifica_account():
        print("❌ Impossibile autenticarsi a Sorare.")
        return

    while True:
        try:
            print("🔎 Controllo offerte...")

            offerte = recupera_offerte()

            print(f"📨 Offerte pending ricevute: {len(offerte)}")

            for offerta in offerte:
                elabora_offerta(offerta)

        except Exception as e:
            print(f"⚠️ Errore nel ciclo: {e}")

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

            return "Bot Sorare avviato in modalità DRY RUN."

    return "Bot Sorare già attivo."


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
    )
