import os
import json
import time
import threading
import subprocess
import requests

from decimal import Decimal
from flask import Flask, jsonify


app = Flask(__name__)

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


# ============================================================
# UTILITY
# ============================================================

def slug(v):
    return (
        str(v or "")
        .strip()
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )


def headers():
    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato."
        )

    token = (
        TOKEN
        if TOKEN.lower().startswith("bearer ")
        else "Bearer " + TOKEN
    )

    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-Bot/2.0",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):
    for n in range(1, 4):
        try:
            r = requests.post(
                URL,
                json={
                    "query": query,
                    "variables": variables or {},
                },
                headers=headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP: {r.status_code}",
                flush=True,
            )

            if r.status_code == 429:
                try:
                    pause = int(
                        r.headers.get("Retry-After")
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    pause = n * 3

                print(
                    f"⚠️ Rate limit. "
                    f"Attendo {pause}s.",
                    flush=True,
                )

                time.sleep(pause)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True,
                )

                time.sleep(n)
                continue

            data = r.json()

            if data.get("errors"):
                print(
                    "❌ Errore GraphQL:",
                    flush=True,
                )

                for e in data["errors"]:
                    print(
                        " -",
                        e.get(
                            "message",
                            str(e),
                        ),
                        flush=True,
                    )

                return None

            return data

        except requests.RequestException as e:
            print(
                f"⚠️ Errore HTTP: {e}",
                flush=True,
            )

            time.sleep(n)

        except Exception as e:
            print(
                f"❌ Errore: {e}",
                flush=True,
            )

            return None

    return None


# ============================================================
# OPERAZIONI REALI SORARE
# ============================================================

def sorare_action(action, payload):
    """
    Esegue l'operazione reale tramite Node.js.

    La private key NON viene passata come argomento:
    viene letta direttamente dalla variabile d'ambiente.
    """

    if DRY:
        print(
            f"🟡 DRY RUN: {action} non eseguita.",
            flush=True,
        )
        return True

    try:
        result = subprocess.run(
            [
                "node",
                "sorare_actions.js",
                action,
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ.copy(),
        )

        if result.stdout:
            print(
                result.stdout.strip(),
                flush=True,
            )

        if result.returncode != 0:
            if result.stderr:
                print(
                    result.stderr.strip(),
                    flush=True,
                )

            return False

        try:
            data = json.loads(
                result.stdout.strip().splitlines()[-1]
            )
        except Exception:
            print(
                "❌ Risposta operazione Sorare "
                "non interpretabile.",
                flush=True,
            )
            return False

        if not data.get("ok"):
            print(
                f"❌ Operazione Sorare fallita: "
                f"{data.get('error', 'errore sconosciuto')}",
                flush=True,
            )
            return False

        return True

    except subprocess.TimeoutExpired:
        print(
            "❌ Timeout operazione Sorare.",
            flush=True,
        )
        return False

    except Exception as e:
        print(
            f"❌ Errore operazione Sorare: {e}",
            flush=True,
        )
        return False


# ============================================================
# ACCOUNT
# ============================================================

def verifica_account():
    d = graphql(
        """
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
        """
    )

    u = (
        (d or {})
        .get("data", {})
        .get("currentUser")
    )

    if not u:
        print(
            "❌ currentUser non disponibile.",
            flush=True,
        )
        return False

    print(
        "========================================",
        flush=True,
    )

    print(
        "✅ AUTENTICAZIONE SORARE RIUSCITA",
        flush=True,
    )

    print(
        f"👤 Manager: "
        f"{u.get('nickname') or 'N/D'}",
        flush=True,
    )

    print(
        f"🔗 Slug: "
        f"{u.get('slug') or 'N/D'}",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    return True


# ============================================================
# OFFERTE
# ============================================================

def recupera_offerte():
    q = """
    query {
        currentUser {
            pendingTokenOffersReceived(first: 50) {
                nodes {
                    id
                    status
                    blockchainId

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

    d = graphql(q)

    return (
        (
            (d or {})
            .get("data", {})
            .get("currentUser", {})
            .get("pendingTokenOffersReceived")
            or {}
        )
        .get("nodes")
        or []
    )


# ============================================================
# DETTAGLI CARTE
# ============================================================

def dettagli_carte(ids):
    ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in ids
            if x
        )
    )

    if not ids:
        return []

    q = """
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
    """

    d = graphql(
        q,
        {"assetIds": ids},
    )

    if not d:
        return None

    return (
        d
        .get("data", {})
        .get("anyCards")
        or []
    )


# ============================================================
# PREZZI
# ============================================================

def live(card):
    try:
        return int(
            card
            .get("liveSingleSaleOffer", {})
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

    for x in values:
        try:
            cents = int(
                x.get("eurCents")
            )

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
            x
            for x in (
                live(source),
                public(source),
            )
            if x
        ]

        if prices:
            return min(prices)

    return None


# ============================================================
# SQUADRA / CAMPIONATO
# ============================================================

def controlla_squadra(card):
    player = (
        card.get("anyPlayer")
        or {}
    )

    name = (
        player.get("displayName")
        or player.get("slug")
        or card.get("name")
        or "Sconosciuto"
    )

    club = player.get("activeClub")

    if not isinstance(club, dict):
        print(
            "      🏟️ Squadra attiva: NESSUNA",
            flush=True,
        )

        print(
            f"      👤 Giocatore: {name}",
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

    comps = (
        club.get("activeCompetitions")
        or []
    )

    if not comps:
        print(
            "      🔴 Nessuna competizione attiva.",
            flush=True,
        )

        return False

    found = []

    for c in comps:
        if not isinstance(c, dict):
            continue

        s = slug(c.get("slug"))

        if s in CAMPIONATI:
            found.append(
                CAMPIONATI[s]
            )

    if not found:
        print(
            "      🔴 CAMPIONATO NON COPERTO",
            flush=True,
        )

        return False

    print(
        "      🟢 CAMPIONATO COPERTO",
        flush=True,
    )

    for x in dict.fromkeys(found):
        print(
            f"         🟢 {x}",
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
        card.get("rarityTyped")
        or ""
    ).upper()

    f = floor(card)

    print(
        f"\n   📄 {name}",
        flush=True,
    )

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

    if f is None:
        print(
            "      🔴 Prezzo floor NON verificabile",
            flush=True,
        )

        priceok = False

    else:
        print(
            f"      💰 Prezzo floor: €{f / 100:.2f}",
            flush=True,
        )

        priceok = (
            MIN_PRICE
            <= f
            <= MAX_PRICE
        )

        print(
            "      🟢 Prezzo valido"
            if priceok
            else "      🔴 Prezzo fuori intervallo",
            flush=True,
        )

    rarityok = rarity == "LIMITED"

    print(
        "      🟢 Rarità LIMITED"
        if rarityok
        else "      🔴 Rarità NON valida",
        flush=True,
    )

    clubok = controlla_squadra(card)

    ok = (
        priceok
        and rarityok
        and clubok
    )

    print(
        "      🟢 CARTA IDONEA"
        if ok
        else "      ❌ CARTA NON IDONEA",
        flush=True,
    )

    return ok


# ============================================================
# KULENOVIC
# ============================================================

def kulenovic_richiesto(cards):
    targets = {
        KASSET.lower(),
        KSLUG.lower(),
    }

    if KID:
        targets.add(
            KID.lower()
        )

    for c in cards:
        asset = str(
            c.get("assetId") or ""
        ).lower()

        card_slug = str(
            c.get("slug") or ""
        ).lower()

        if (
            asset in targets
            or card_slug in targets
        ):
            print(
                "🎯 KULENOVIC RICONOSCIUTO!",
                flush=True,
            )

            return True

    return False


# ============================================================
# RIFIUTO REALE
# ============================================================

def rifiuta_offerta(offer):
    oid = str(
        offer.get("id") or ""
    )

    print(
        f"🔴 RIFIUTO RICHIESTO: {oid}",
        flush=True,
    )

    if DRY:
        print(
            "🟡 DRY RUN: rifiuto non inviato.",
            flush=True,
        )
        return True

    blockchain_id = (
        offer.get("blockchainId")
    )

    if not blockchain_id:
        print(
            "❌ blockchainId mancante: "
            "rifiuto NON eseguito.",
            flush=True,
        )

        return False

    ok = sorare_action(
        "reject",
        {
            "blockchainId": blockchain_id,
        },
    )

    if ok:
        print(
            "✅ OFFERTA RIFIUTATA REALMENTE.",
            flush=True,
        )

    return ok


# ============================================================
# CONTROPROPOSTA REALE
# ============================================================

def prepara_controproposta(offer, cards):
    if not cards:
        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )
        return False

    sender = (
        offer.get("sender")
        or {}
    )

    target = (
        sender.get("slug")
        or ""
    ).strip()

    if not target:
        print(
            "❌ Slug del manager non disponibile.",
            flush=True,
        )
        return False

    ids = [
        str(c["assetId"])
        for c in cards
        if c.get("assetId")
    ]

    if not ids:
        print(
            "❌ Nessun asset id valido.",
            flush=True,
        )
        return False

    payment_cents = (
        len(ids) * PAY
    )

    payment = (
        Decimal(payment_cents)
        / Decimal(100)
    )

    print(
        "\n========================================",
        flush=True,
    )

    print(
        "🟢 CONTROPROPOSTA",
        flush=True,
    )

    print(
        f"👤 Destinatario: {target}",
        flush=True,
    )

    print(
        "📥 Carte che riceviamo:",
        flush=True,
    )

    for c in cards:
        print(
            f"   🟢 "
            f"{c.get('name') or c.get('slug')}",
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
            "🟡 DRY RUN: "
            "controproposta non inviata.",
            flush=True,
        )
        return True

    ok = sorare_action(
        "counter",
        {
            "offerId": offer.get("id"),
            "receiverSlug": target,
            "receiveAssetIds": ids,
            "amountCents": payment_cents,
        },
    )

    if ok:
        print(
            "✅ CONTROPROPOSTA INVIATA REALMENTE.",
            flush=True,
        )

    return ok


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(o):
    oid = str(
        o.get("id") or ""
    ).strip()

    if not oid:
        return

    with lock:
        if (
            oid in analizzate
            or oid in in_elaborazione
        ):
            return

        in_elaborazione.add(oid)

    done = False

    try:
        sender = (
            o.get("sender")
            or {}
        )

        received = (
            o.get("senderSide", {})
            .get("anyCards")
            or []
        )

        requested = (
            o.get("receiverSide", {})
            .get("anyCards")
            or []
        )

        print(
            "\n========================================",
            flush=True,
        )

        print(
            "📨 NUOVA OFFERTA",
            flush=True,
        )

        print(
            f"🆔 ID: {oid}",
            flush=True,
        )

        print(
            f"📌 Stato: {o.get('status')}",
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

        # ----------------------------------------------------
        # KULENOVIC OBBLIGATORIO
        # ----------------------------------------------------

        if not kulenovic_richiesto(requested):
            print(
                "❌ Kulenovic non richiesto.",
                flush=True,
            )

            done = rifiuta_offerta(o)

            return

        # ----------------------------------------------------
        # CARTE RICEVUTE
        # ----------------------------------------------------

        ids = [
            c.get("assetId")
            for c in received
            if (
                isinstance(c, dict)
                and c.get("assetId")
            )
        ]

        if not ids:
            print(
                "🔴 Nessuna carta ricevuta.",
                flush=True,
            )

            done = rifiuta_offerta(o)

            return

        # ----------------------------------------------------
        # DETTAGLI
        # ----------------------------------------------------

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

        for c in cards:
            if analizza_carta(c):
                good.append(c)

        total = len(ids)
        ng = len(good)
        bad = max(
            0,
            total - ng,
        )

        print(
            "\n----------------------------------------",
            flush=True,
        )

        print(
            f"📊 CARTE TOTALI: {total}",
            flush=True,
        )

        print(
            f"📊 CARTE IDONEE: {ng}",
            flush=True,
        )

        print(
            f"📊 CARTE NON IDONEE: {bad}",
            flush=True,
        )

        # ----------------------------------------------------
        # ZERO IDONEE = RIFIUTO
        # ----------------------------------------------------

        if not ng:
            print(
                "\n🔴 NESSUNA CARTA IDONEA",
                flush=True,
            )

            print(
                "🔴 DECISIONE: RIFIUTARE L'OFFERTA",
                flush=True,
            )

            done = rifiuta_offerta(o)

            return

        # ----------------------------------------------------
        # ESCLUSIONE NON IDONEE
        # ----------------------------------------------------

        if bad:
            print(
                "\n🟡 Carte non idonee "
                "ESCLUSE dalla controproposta.",
                flush=True,
            )

            print(
                f"🟢 Rimangono {ng} carte idonee.",
                flush=True,
            )

        payment = (
            Decimal(ng * PAY)
            / Decimal(100)
        )

        print(
            "\n🟢 DECISIONE: CONTROPROPOSTA",
            flush=True,
        )

        print(
            "❌ Noi NON cediamo Kulenovic.",
            flush=True,
        )

        print(
            "📥 Noi riceviamo SOLO "
            "le carte idonee:",
            flush=True,
        )

        for c in good:
            print(
                f"   🟢 "
                f"{c.get('name') or c.get('slug')}",
                flush=True,
            )

        print(
            f"💰 Pagamento: €{payment:.2f}",
            flush=True,
        )

        done = prepara_controproposta(
            o,
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
            "⚠️ Le operazioni reali "
            "possono trasferire carte/fondi.",
            flush=True,
        )

    print(
        "💰 REGOLA PREZZO: €0,30 - €0,80",
        flush=True,
    )

    print(
        "💰 PAGAMENTO: €0,20 "
        "per ogni carta idonea",
        flush=True,
    )

    print(
        f"🏆 {len(set(CAMPIONATI.values()))} "
        "campionati coperti.",
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

    print(
        "\n🔐 VERIFICA CHIAVE STARK",
        flush=True,
    )

    if STARK:
        try:
            int(
                STARK.removeprefix("0x"),
                16,
            )

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

                for o in offers:
                    elabora_offerta(o)

        except Exception as e:
            print(
                f"⚠️ Errore monitor: {e}",
                flush=True,
            )

        time.sleep(INTERVAL)


# ============================================================
# FLASK
# ============================================================

_started = False


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
        "regola": (
            "carte non idonee escluse; "
            "almeno una idonea = "
            "controproposta; "
            "zero idonee = rifiuto"
        ),
    })


start_monitor()
