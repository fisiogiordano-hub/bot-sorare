import os, time, threading, requests
from decimal import Decimal
from flask import Flask, jsonify

app = Flask(__name__)

URL = "https://api.sorare.com/federation/graphql"
TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
DRY = os.getenv("DRY_RUN", "false").strip().lower() == "true"

MIN_PRICE, MAX_PRICE = 30, 80
PAY, INTERVAL, TIMEOUT = 20, 10, 30

KSLUG = "sandro-kulenovic-2025-limited-385"
KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

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

analizzate = set()
in_elaborazione = set()
lock = threading.Lock()
_started = False


def slug(v):
    return str(v or "").strip().lower().replace("_", "-").replace(" ", "-")


def headers():
    if not TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato.")

    token = TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer " + TOKEN

    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-Bot/2.1",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):
    payload = {"query": query, "variables": variables or {}}

    for n in range(1, 4):
        try:
            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(f"🌐 Sorare HTTP: {r.status_code}", flush=True)

            if r.status_code == 429:
                try:
                    pause = int(r.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    pause = n * 3

                print(f"⚠️ Rate limit. Attendo {pause}s.", flush=True)
                time.sleep(pause)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: {r.text[:500]}",
                    flush=True,
                )
                time.sleep(n)
                continue

            data = r.json()

            if data.get("errors"):
                print("❌ Errore GraphQL:", flush=True)

                for e in data["errors"]:
                    print(
                        " -",
                        e.get("message", str(e)),
                        flush=True,
                    )

                return None

            return data

        except requests.RequestException as e:
            print(f"⚠️ Errore HTTP: {e}", flush=True)
            time.sleep(n)

        except Exception as e:
            print(f"❌ Errore: {e}", flush=True)
            return None

    return None


def verifica_account():
    data = graphql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = (data or {}).get("data", {}).get("currentUser")

    if not user:
        print("❌ currentUser non disponibile.", flush=True)
        return False

    print("========================================", flush=True)
    print("✅ AUTENTICAZIONE SORARE RIUSCITA", flush=True)
    print(f"👤 Manager: {user.get('nickname') or 'N/D'}", flush=True)
    print(f"🔗 Slug: {user.get('slug') or 'N/D'}", flush=True)
    print("========================================", flush=True)

    return True


def recupera_offerte():
    data = graphql("""
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
    """)

    return (
        (data or {})
        .get("data", {})
        .get("currentUser", {})
        .get("pendingTokenOffersReceived", {})
        .get("nodes")
        or []
    )


def dettagli_carte(ids):
    ids = list(dict.fromkeys(str(x).strip() for x in ids if x))

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

    return data.get("data", {}).get("anyCards") or []


def live(card):
    try:
        return int(
            card
            .get("liveSingleSaleOffer", {})
            .get("receiverSide", {})
            .get("amounts", {})
            .get("eurCents")
        )
    except (TypeError, ValueError, AttributeError):
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

        except (TypeError, ValueError, AttributeError):
            pass

    return min(prices) if prices else None


def floor(card):
    for key in ("lowestPriceCard", "lowestPriceCardAnySeason", None):
        source = card if key is None else card.get(key) or {}
        prices = [p for p in (live(source), public(source)) if p]

        if prices:
            return min(prices)

    return None


def controlla_squadra(card):
    player = card.get("anyPlayer") or {}

    name = (
        player.get("displayName")
        or player.get("slug")
        or card.get("name")
        or "Sconosciuto"
    )

    club = player.get("activeClub")

    if not isinstance(club, dict):
        print("      🏟️ Squadra attiva: NESSUNA", flush=True)
        print(f"      👤 Giocatore: {name}", flush=True)
        print("      🔴 GIOCATORE SENZA SQUADRA", flush=True)
        return False

    print(
        f"      🏟️ Squadra attiva: "
        f"{club.get('name') or club.get('slug') or 'N/D'}",
        flush=True,
    )

    competitions = club.get("activeCompetitions") or []

    if not competitions:
        print("      🔴 Nessuna competizione attiva.", flush=True)
        return False

    found = []

    for competition in competitions:
        if not isinstance(competition, dict):
            continue

        s = slug(competition.get("slug"))

        if s in CAMPIONATI:
            found.append(CAMPIONATI[s])

    if not found:
        print("      🔴 CAMPIONATO NON COPERTO", flush=True)
        return False

    print("      🟢 CAMPIONATO COPERTO", flush=True)

    for name in dict.fromkeys(found):
        print(f"         🟢 {name}", flush=True)

    return True


def analizza_carta(card):
    if not isinstance(card, dict):
        return False

    name = card.get("name") or card.get("slug") or "Carta"
    rarity = str(card.get("rarityTyped") or "").upper()
    price = floor(card)

    print(f"\n   📄 {name}", flush=True)
    print(f"      Asset ID: {card.get('assetId') or 'N/D'}", flush=True)
    print(f"      Slug: {card.get('slug') or 'N/D'}", flush=True)
    print(f"      Rarità: {rarity or 'N/D'}", flush=True)

    if price is None:
        print("      🔴 Prezzo floor NON verificabile", flush=True)
        price_ok = False

    else:
        print(f"      💰 Prezzo floor: €{price / 100:.2f}", flush=True)

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
    ok = price_ok and rarity_ok and club_ok

    print(
        "      🟢 CARTA IDONEA"
        if ok
        else "      ❌ CARTA NON IDONEA",
        flush=True,
    )

    return ok


def kulenovic_richiesto(cards):
    wanted = {
        KASSET.lower(),
        KSLUG.lower(),
    }

    if KID:
        wanted.add(KID.lower())

    for card in cards:
        asset = str(card.get("assetId") or "").lower()
        card_slug = str(card.get("slug") or "").lower()

        if asset in wanted or card_slug in wanted:
            print("🎯 KULENOVIC RICONOSCIUTO!", flush=True)
            return True

    return False


# ============================================================
# RIFIUTO REALE
# ============================================================

def rifiuta_offerta(offer):
    offer_id = str(offer.get("id") or "").strip()
    blockchain_id = str(offer.get("blockchainId") or "").strip()

    print(f"🔴 RIFIUTO RICHIESTO: {offer_id}", flush=True)

    if DRY:
        print("🟡 DRY RUN: rifiuto non inviato.", flush=True)
        return True

    if not blockchain_id:
        print(
            "❌ Rifiuto fallito: blockchainId mancante.",
            flush=True,
        )
        return False

    mutation = """
        mutation RejectOffer($input: rejectOfferInput!) {
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
    """

    variables = {
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(time.time_ns()),
        }
    }

    print(
        f"🔑 Blockchain ID: {blockchain_id}",
        flush=True,
    )

    data = graphql(mutation, variables)

    if not data:
        print(
            "❌ Rifiuto fallito: nessuna risposta.",
            flush=True,
        )
        return False

    result = (
        data.get("data") or {}
    ).get("rejectOffer")

    if not result:
        print(
            "❌ Rifiuto fallito: payload assente.",
            flush=True,
        )
        return False

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ Errore durante il rifiuto:",
            flush=True,
        )

        for error in errors:
            print(
                f"   - {error.get('message', 'Errore sconosciuto')}",
                flush=True,
            )

        return False

    token_offer = result.get("tokenOffer") or {}

    print(
        f"✅ OFFERTA RIFIUTATA REALMENTE: "
        f"{token_offer.get('id') or offer_id}",
        flush=True,
    )

    return True


# ============================================================
# CONTROPROPOSTA
# ============================================================

def prepara_controproposta(offer, cards):
    if not cards:
        return False

    sender = offer.get("sender") or {}
    target = (sender.get("slug") or "").strip()

    if not target:
        print(
            "❌ Slug del manager non disponibile.",
            flush=True,
        )
        return False

    receive_ids = [
        str(card["assetId"])
        for card in cards
        if card.get("assetId")
    ]

    if not receive_ids:
        print(
            "❌ Nessun asset id valido.",
            flush=True,
        )
        return False

    payment = Decimal(len(receive_ids) * PAY) / Decimal(100)

    print(
        "\n========================================",
        flush=True,
    )
    print("🟢 CONTROPROPOSTA", flush=True)
    print(f"👤 Destinatario: {target}", flush=True)
    print("📥 Carte che riceviamo:", flush=True)

    for card in cards:
        print(
            f"   🟢 {card.get('name') or card.get('slug')}",
            flush=True,
        )

    print(
        f"💰 Pagamento: €{payment:.2f}",
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
            "🟡 DRY RUN: controproposta non inviata.",
            flush=True,
        )
        return True

    print(
        "⚠️ CONTROPROPOSTA REALE NON INVIATA.",
        flush=True,
    )
    print(
        "⚠️ Richiede prepareOffer + firma Stark "
        "ufficiale + createDirectOffer.",
        flush=True,
    )

    return False


# ============================================================
# ELABORAZIONE
# ============================================================

def elabora_offerta(offer):
    oid = str(offer.get("id") or "").strip()

    if not oid:
        return

    with lock:
        if oid in analizzate or oid in in_elaborazione:
            return

        in_elaborazione.add(oid)

    done = False

    try:
        sender = offer.get("sender") or {}
        sender_side = offer.get("senderSide") or {}
        receiver_side = offer.get("receiverSide") or {}

        received = sender_side.get("anyCards") or []
        requested = receiver_side.get("anyCards") or []

        print(
            "\n========================================",
            flush=True,
        )
        print("📨 NUOVA OFFERTA", flush=True)
        print(f"🆔 ID: {oid}", flush=True)
        print(
            f"🔑 Blockchain ID: "
            f"{offer.get('blockchainId') or 'N/D'}",
            flush=True,
        )
        print(
            f"📌 Stato: {offer.get('status')}",
            flush=True,
        )
        print(
            f"👤 Manager: "
            f"{sender.get('nickname') or sender.get('slug') or 'N/D'}",
            flush=True,
        )
        print(
            f"📦 Carte offerte: {len(received)}",
            flush=True,
        )
        print(
            f"📦 Carte richieste: {len(requested)}",
            flush=True,
        )

        if not kulenovic_richiesto(requested):
            print(
                "❌ Kulenovic non richiesto.",
                flush=True,
            )

            done = rifiuta_offerta(offer)
            return

        ids = [
            card.get("assetId")
            for card in received
            if isinstance(card, dict)
            and card.get("assetId")
        ]

        if not ids:
            print(
                "🔴 Nessuna carta ricevuta.",
                flush=True,
            )

            done = rifiuta_offerta(offer)
            return

        cards = dettagli_carte(ids)

        if cards is None:
            print(
                "⚠️ Dettagli carte non disponibili.",
                flush=True,
            )
            print(
                "⚠️ Offerta lasciata non elaborata.",
                flush=True,
            )
            return

        good = []

        print(
            "\n🔎 ANALISI DELLE CARTE RICEVUTE:",
            flush=True,
        )

        for card in cards:
            if analizza_carta(card):
                good.append(card)

        total = len(ids)
        valid = len(good)
        invalid = max(0, total - valid)

        print(
            "\n----------------------------------------",
            flush=True,
        )
        print(
            f"📊 CARTE TOTALI: {total}",
            flush=True,
        )
        print(
            f"📊 CARTE IDONEE: {valid}",
            flush=True,
        )
        print(
            f"📊 CARTE NON IDONEE: {invalid}",
            flush=True,
        )

        if valid == 0:
            print(
                "\n🔴 NESSUNA CARTA IDONEA",
                flush=True,
            )
            print(
                "🔴 DECISIONE: RIFIUTARE L'OFFERTA",
                flush=True,
            )

            done = rifiuta_offerta(offer)
            return

        if invalid:
            print(
                "\n🟡 Carte non idonee ESCLUSE "
                "dalla controproposta.",
                flush=True,
            )

            print(
                f"🟢 Rimangono {valid} carte idonee.",
                flush=True,
            )

        payment = Decimal(valid * PAY) / Decimal(100)

        print(
            "\n🟢 DECISIONE: CONTROPROPOSTA",
            flush=True,
        )
        print(
            "❌ Noi NON cediamo Kulenovic.",
            flush=True,
        )
        print(
            "📥 Noi riceviamo SOLO le carte idonee:",
            flush=True,
        )

        for card in good:
            print(
                f"   🟢 {card.get('name') or card.get('slug')}",
                flush=True,
            )

        print(
            f"💰 Pagamento: €{payment:.2f}",
            flush=True,
        )

        done = prepara_controproposta(
            offer,
            good,
        )

    except Exception as e:
        print(
            f"❌ Errore offerta {oid}: {e}",
            flush=True,
        )

    finally:
        with lock:
            in_elaborazione.discard(oid)

            if done:
                analizzate.add(oid)


# ============================================================
# MONITOR
# ============================================================

def monitor():
    print(
        "\n🤖 BOT SORARE AVVIATO",
        flush=True,
    )

    print(
        "🟡 MODALITÀ DRY RUN ATTIVA"
        if DRY
        else "🟢 MODALITÀ REALE ATTIVA",
        flush=True,
    )

    if not DRY:
        print(
            "⚠️ Il rifiuto reale è attivo.",
            flush=True,
        )

    print(
        "💰 REGOLA PREZZO: €0,30 - €0,80",
        flush=True,
    )
    print(
        "💰 PAGAMENTO: €0,20 per ogni carta idonea",
        flush=True,
    )
    print(
        f"🏆 {len(set(CAMPIONATI.values()))} campionati coperti.",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )
    print(
        "🔧 VERIFICA CONFIGURAZIONE",
        flush=True,
    )
    print(
        "========================================",
        flush=True,
    )

    for name, value in (
        ("SORARE_JWT_TOKEN", TOKEN),
        ("SORARE_JWT_AUD", AUD),
        ("KULENOVIC_ID", KID),
        ("SORARE_STARK_PRIVATE_KEY", STARK),
    ):
        print(
            f"✅ {name} presente."
            if value
            else f"❌ {name} NON presente.",
            flush=True,
        )

    print(
        f"🔵 DRY_RUN = {DRY}",
        flush=True,
    )

    if STARK:
        try:
            int(STARK.removeprefix("0x"), 16)

            print(
                "✅ Formato esadecimale verificato.",
                flush=True,
            )

        except ValueError:
            print(
                "❌ Chiave Stark non esadecimale.",
                flush=True,
            )

    else:
        print(
            "❌ Chiave Stark assente.",
            flush=True,
        )

    if not verifica_account():
        print(
            "❌ Autenticazione Sorare fallita.",
            flush=True,
        )
        return

    print(
        "🟢 MONITORAGGIO OFFERTE ATTIVO.",
        flush=True,
    )

    while True:
        try:
            print(
                "\n🔎 Controllo offerte...",
                flush=True,
            )

            offers = recupera_offerte()

            if offers is None:
                print(
                    "⚠️ Controllo offerte fallito.",
                    flush=True,
                )

            else:
                print(
                    f"📨 Offerte pending ricevute: "
                    f"{len(offers)}",
                    flush=True,
                )

                for offer in offers:
                    elabora_offerta(offer)

        except Exception as e:
            print(
                f"⚠️ Errore monitor: {e}",
                flush=True,
            )

        time.sleep(INTERVAL)


def start_monitor():
    global _started

    if _started:
        return

    _started = True

    threading.Thread(
        target=monitor,
        name="sorare-monitor",
        daemon=True,
    ).start()


@app.route("/")
def home():
    return "Bot Sorare attivo.", 200


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "sorare",
        "dry_run": DRY,
        "monitoraggio": (
            "attivo"
            if _started
            else "in avvio"
        ),
        "rifiuto_reale": not DRY,
        "regola": (
            "carte non idonee escluse; "
            "almeno una idonea = controproposta; "
            "zero idonee = rifiuto"
        ),
    })


start_monitor()
