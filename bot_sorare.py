import os
import re
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

# Può essere lasciato vuoto.
# Il bot riconoscerà comunque Kulenovic tramite slug.
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()

KULENOVIC_SLUG = os.getenv(
    "KULENOVIC_SLUG",
    "sandro-kulenovic-2025-limited-385"
).strip().lower()

SORARE_API_URL = "https://api.sorare.com/graphql"

# ============================================================
# SICUREZZA
# ============================================================

# IMPORTANTE:
# Nessuna operazione reale viene eseguita.
DRY_RUN = True

# ============================================================
# REGOLE BOT
# ============================================================

PREZZO_MASSIMO_CENTESIMI = 50
PAGAMENTO_PER_CARTA_CENTESIMI = 20

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

                slug

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
# ESTRAZIONE STAGIONE DALLO SLUG
# ============================================================

def estrai_stagione_da_slug(slug):

    if not slug:
        return None

    match = re.search(
        r"-(\d{4})-(?:limited|rare|super-rare|unique)-\d+$",
        slug.lower()
    )

    if not match:
        return None

    try:
        return int(
            match.group(1)
        )
    except Exception:
        return None


# ============================================================
# ESTRAZIONE PLAYER SLUG
# ============================================================

def estrai_player_slug(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = (
        player.get("slug")
        or ""
    ).strip().lower()

    if player_slug:
        return player_slug

    card_slug = (
        card.get("slug")
        or ""
    ).strip().lower()

    if not card_slug:
        return None

    # Esempio:
    #
    # sandro-kulenovic-2025-limited-385
    #
    # diventa:
    #
    # sandro-kulenovic

    match = re.match(
        r"^(.+)-\d{4}-(?:limited|rare|super-rare|unique)-\d+$",
        card_slug
    )

    if match:
        return match.group(1)

    return None


# ============================================================
# RECUPERO PREZZO FLOOR
# ============================================================

def recupera_prezzo_carta(carta):

    slug = (
        carta.get("slug")
        or ""
    ).strip().lower()

    player_slug = estrai_player_slug(
        carta
    )

    stagione = estrai_stagione_da_slug(
        slug
    )

    if not player_slug:

        print(
            "      ⚠️ Player slug non recuperabile"
        )

        return None

    if stagione is None:

        print(
            "      ⚠️ Stagione non recuperabile "
            "dallo slug"
        )

        return None

    print(
        f"      🔎 Ricerca prezzo floor: "
        f"{player_slug} / {stagione}"
    )

    query = """
    query CardFloorPrice(
        $playerSlug: String!
        $seasonStartYear: Int!
    ) {

        football {

            player(
                slug: $playerSlug
            ) {

                lowestPriceAnyCard(
                    rarity: limited
                    seasonStartYear: $seasonStartYear
                ) {

                    liveSingleSaleOffer {

                        receiverSide {

                            amounts {
                                eur
                                wei
                            }
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
            "playerSlug": player_slug,
            "seasonStartYear": stagione,
        }
    )

    if not risultato:

        print(
            "      ⚠️ Prezzo non recuperabile"
        )

        return None

    football = (
        risultato
        .get("data", {})
        .get("football")
        or {}
    )

    player = (
        football.get("player")
        or {}
    )

    floor = (
        player.get(
            "lowestPriceAnyCard"
        )
        or {}
    )

    offerta = (
        floor.get(
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

    # --------------------------------------------------------
    # EUR
    # --------------------------------------------------------

    eur = amounts.get("eur")

    if eur is not None:

        try:

            # Sorare può restituire:
            # "0.43"
            # oppure un numero.

            valore = float(eur)

            return int(
                round(
                    valore * 100
                )
            )

        except (
            TypeError,
            ValueError
        ):

            pass

    # --------------------------------------------------------
    # WEI
    # --------------------------------------------------------
    #
    # Se EUR non è disponibile, proviamo WEI.
    # 1 ETH = 1000000000000000000 wei
    #
    # Per confrontare il limite in EUR avremmo però
    # bisogno del cambio ETH/EUR corrente.
    #
    # Quindi NON lo usiamo per decidere l'idoneità.
    # --------------------------------------------------------

    wei = amounts.get("wei")

    if wei is not None:

        print(
            f"      ℹ️ Prezzo WEI disponibile: {wei}"
        )

    print(
        "      ⚠️ Prezzo EUR non disponibile"
    )

    return None


# ============================================================
# RICONOSCIMENTO KULENOVIC
# ============================================================

def carta_e_kulenovic(carta):

    asset_id = str(
        carta.get("assetId")
        or ""
    ).strip().lower()

    slug = str(
        carta.get("slug")
        or ""
    ).strip().lower()

    nome = str(
        carta.get("name")
        or ""
    ).strip().lower()

    configured_id = str(
        KULENOVIC_ID
        or ""
    ).strip().lower()

    # --------------------------------------------------------
    # 1. Asset ID configurato
    # --------------------------------------------------------

    if configured_id:

        if asset_id == configured_id:

            return True

        if slug == configured_id:

            return True

    # --------------------------------------------------------
    # 2. Slug configurato
    # --------------------------------------------------------

    if KULENOVIC_SLUG:

        if slug == KULENOVIC_SLUG:

            return True

    # --------------------------------------------------------
    # 3. Nome
    # --------------------------------------------------------

    if "sandro kulenovic" in nome:

        return True

    # --------------------------------------------------------
    # 4. Fallback player slug
    # --------------------------------------------------------

    player = (
        carta.get("anyPlayer")
        or {}
    )

    player_slug = str(
        player.get("slug")
        or ""
    ).strip().lower()

    if player_slug == "sandro-kulenovic":

        return True

    return False


# ============================================================
# CONTROLLO KULENOVIC
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

        collection = carta.get(
            "collection"
        )

        print(
            f"   Asset ID: {asset_id}"
        )

        print(
            f"   Slug: {slug}"
        )

        print(
            f"   Collection: {collection}"
        )

        if carta_e_kulenovic(carta):

            kulenovic_presente = True

            print(
                "   🎯 KULENOVIC RICONOSCIUTO"
            )

    return kulenovic_presente


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

    player = (
        carta.get("anyPlayer")
        or {}
    )

    club = (
        player.get("activeClub")
        or {}
    )

    competizioni = (
        club.get(
            "activeCompetitions"
        )
        or []
    )

    # --------------------------------------------------------
    # PREZZO
    # --------------------------------------------------------

    prezzo_centesimi = recupera_prezzo_carta(
        carta
    )

    # --------------------------------------------------------
    # REGOLE
    # --------------------------------------------------------

    rarita_ok = (
        rarita == "LIMITED"
    )

    campionato_coperto = (
        len(competizioni) > 0
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

        prezzo_euro = (
            prezzo_centesimi / 100
        )

        print(
            f"      Prezzo floor: "
            f"€{prezzo_euro:.2f}"
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

    carte_offerte = (
        sender_side.get(
            "anyCards"
        )
        or []
    )

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
            "ℹ️ Kulenovic non riconosciuto."
        )

        print(
            f"   Slug atteso: "
            f"{KULENOVIC_SLUG}"
        )

        print(
            "ℹ️ L'offerta viene comunque "
            "analizzata in DRY RUN."
        )

    # ========================================================
    # ASSET CARTE RICEVUTE
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
            "nessuna operazione eseguita."
        )

        print("----------------------------------------")
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
        pagamento_centesimi / 100
    )

    print("")
    print(
        "🟢 DECISIONE: CONTROPROPOSTA"
    )

    print("")
    print(
        "📤 DALLA PROPOSTA VIENE RIMOSSA:"
    )

    print(
        "   ❌ Kulenovic"
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
        "   ❌ Noi NON cediamo Kulenovic"
    )

    for carta in carte_idonee:

        print(
            f"   ✅ Noi riceviamo: "
            f"{carta.get('name') or carta.get('slug')}"
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
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
