import os
import time
import json
import re
import requests
import threading

from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
COVERAGE_URL = "https://sorare.com/coverage"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()

# IMPORTANTE:
# KID deve essere l'ASSET ID DEL TUO KULENOVIC.
#
# NON usare lo slug del giocatore per identificare
# la tua carta, perché altri manager possono possedere
# altri Kulenovic.
KID = os.getenv("KULENOVIC_ID", "").strip()

BOT_VERSION = "1.1-SWAP"

INTERVAL = 10
TIMEOUT = 25
UNKNOWN_PRICE_RETRY = 60


# ============================================================
# STESSI PARAMETRI AUTOBUY
# ============================================================

MIN_PRICE = 32          # €0.32
MAX_PRICE = 70          # €0.70
MAX_AGE = 28

MIN_LIVE_LISTINGS = 5


# ============================================================
# REGOLA SWAP
# ============================================================

SWAP_MIN_MULTIPLIER = 1.20
SWAP_MAX_MULTIPLIER = 1.25


# ============================================================
# STATE
# ============================================================

processed = set()
unknown_price = {}

state_lock = threading.Lock()

usd_rate = None
usd_rate_time = 0
usd_lock = threading.Lock()

coverage_cache = set()
coverage_time = 0
coverage_lock = threading.Lock()

COVERAGE_CACHE_SECONDS = 3600
USD_CACHE_SECONDS = 300


# ============================================================
# NORMALIZE
# ============================================================

def normalize(value):
    return str(value or "").strip().lower()


# ============================================================
# KULENOVIC
# ============================================================

def is_my_kulenovic(card):
    """
    Restituisce True SOLO se la carta è il TUO Kulenovic.

    È fondamentale NON controllare semplicemente lo slug
    del giocatore, perché un manager potrebbe offrirci
    un altro Kulenovic.

    Il controllo principale è quindi KID = assetId della
    tua specifica carta.
    """

    if not KID:
        print(
            "⚠️ KULENOVIC_ID non configurato: "
            "impossibile proteggere il Kulenovic",
            flush=True
        )

        # Fail-safe:
        # senza KID NON consideriamo nessuna carta come
        # sicuramente nostra.
        return False

    card_asset_id = normalize(
        card.get("assetId")
    )

    return (
        card_asset_id
        and card_asset_id == normalize(KID)
    )


# ============================================================
# COVERAGE
# ============================================================

def load_coverage(force=False):

    global coverage_cache
    global coverage_time

    now = time.time()

    with coverage_lock:

        if (
            not force
            and coverage_cache
            and now - coverage_time
            < COVERAGE_CACHE_SECONDS
        ):
            return set(coverage_cache)

        try:

            r = requests.get(
                COVERAGE_URL,
                timeout=TIMEOUT,
                headers={
                    "User-Agent":
                        f"Sorare-Swap/{BOT_VERSION}"
                }
            )

            if r.status_code != 200:

                print(
                    f"⚠️ Coverage HTTP "
                    f"{r.status_code}",
                    flush=True
                )

                return set(coverage_cache)

            matches = re.findall(
                r'/football/leagues/([^"\'?#<>\s]+)',
                r.text,
                re.IGNORECASE
            )

            result = {
                normalize(x)
                for x in matches
                if normalize(x)
            }

            if not result:

                print(
                    "⚠️ Nessuna competizione "
                    "Football trovata",
                    flush=True
                )

                return set(coverage_cache)

            coverage_cache = result
            coverage_time = now

            print(
                f"🌐 Coverage aggiornata: "
                f"{len(result)} competizioni",
                flush=True
            )

            return set(result)

        except Exception as e:

            print(
                f"⚠️ Coverage: {e}",
                flush=True
            )

            return set(coverage_cache)


# ============================================================
# GRAPHQL
# ============================================================

def headers():

    if not TOKEN:
        raise RuntimeError(
            "SORARE_JWT_TOKEN non configurato"
        )

    token = TOKEN

    if not token.lower().startswith("bearer "):
        token = "Bearer " + token

    h = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent":
            f"Sorare-Swap/{BOT_VERSION}",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


def graphql(query, variables=None):

    payload = {
        "query": query,
        "variables": variables or {}
    }

    for attempt in range(3):

        try:

            r = requests.post(
                URL,
                json=payload,
                headers=headers(),
                timeout=TIMEOUT
            )

            print(
                f"🌐 Sorare HTTP "
                f"{r.status_code}",
                flush=True
            )

            if r.status_code == 429:

                wait = int(
                    r.headers.get(
                        "Retry-After",
                        attempt + 2
                    )
                )

                time.sleep(
                    min(wait, 15)
                )

                continue

            if r.status_code != 200:

                print(
                    f"❌ Sorare HTTP "
                    f"{r.status_code}: "
                    f"{r.text[:500]}",
                    flush=True
                )

                time.sleep(
                    attempt + 1
                )

                continue

            try:

                data = r.json()

            except ValueError:

                print(
                    "❌ JSON Sorare non valido",
                    flush=True
                )

                return None

            if data.get("errors"):

                print(
                    "❌ GraphQL:",
                    json.dumps(
                        data["errors"],
                        ensure_ascii=False
                    )[:3000],
                    flush=True
                )

            return data

        except requests.RequestException as e:

            print(
                f"❌ HTTP Sorare: {e}",
                flush=True
            )

            time.sleep(
                attempt + 1
            )

        except Exception as e:

            print(
                f"❌ GraphQL: {e}",
                flush=True
            )

            return None

    return None


# ============================================================
# ACCOUNT
# ============================================================

def check_account():

    data = graphql("""
        query {
            currentUser {
                slug
                nickname
                starkKey
            }
        }
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
    )

    if not user:

        print(
            "❌ Account Sorare non verificato",
            flush=True
        )

        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True
    )

    return True


# ============================================================
# OFFERTE PENDENTI
# ============================================================

def get_offers():

    data = graphql("""
        query {

            currentUser {

                pendingTokenOffersReceived(
                    first: 50
                ) {

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

                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

                            anyCards {
                                assetId
                                slug
                                collection
                            }
                        }

                        receiverSide {

                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
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
    """)

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    return (
        user
        .get("pendingTokenOffersReceived", {})
        .get("nodes")
        or []
    )


# ============================================================
# CARD DETAILS
# ============================================================

def card_details(asset_ids):

    ids = list(
        dict.fromkeys(
            str(x).strip()
            for x in asset_ids
            if x
        )
    )

    if not ids:
        return []

    data = graphql("""
        query Cards($assetIds: [String!]!) {

            anyCards(
                assetIds: $assetIds
            ) {

                assetId
                slug
                name
                rarityTyped
                seasonYear

                anyPlayer {

                    slug
                    displayName
                    age

                    activeClub {

                        slug
                        name

                        activeCompetitions {
                            slug
                        }
                    }
                }
            }
        }
    """, {
        "assetIds": ids
    })

    if not data or data.get("errors"):
        return []

    return (
        ((data.get("data") or {})
        .get("anyCards"))
        or []
    )


# ============================================================
# USD -> EUR
# ============================================================

def usd_eur():

    global usd_rate
    global usd_rate_time

    now = time.time()

    with usd_lock:

        if (
            usd_rate
            and now - usd_rate_time
            < USD_CACHE_SECONDS
        ):
            return usd_rate

        try:

            r = requests.get(
                "https://api.frankfurter.app/latest",
                params={
                    "from": "USD",
                    "to": "EUR"
                },
                timeout=10
            )

            if r.status_code != 200:
                return None

            rate = float(
                (r.json().get("rates") or {})
                .get("EUR")
            )

            if rate <= 0:
                return None

            usd_rate = rate
            usd_rate_time = now

            print(
                f"💱 1 USD = "
                f"{rate:.6f} EUR",
                flush=True
            )

            return rate

        except Exception as e:

            print(
                f"❌ USD/EUR: {e}",
                flush=True
            )

            return None


# ============================================================
# AMOUNT -> EUR CENTS
# ============================================================

def price_eur_cents(amounts):

    if not isinstance(amounts, dict):
        return None

    # -------------------------
    # EUR DIRECT
    # -------------------------

    try:

        eur = int(
            amounts.get("eurCents")
        )

        if eur > 0:
            return eur

    except (
        TypeError,
        ValueError
    ):
        pass

    # -------------------------
    # USD -> EUR
    # -------------------------

    try:

        usd = float(
            amounts.get("usdCents")
        )

    except (
        TypeError,
        ValueError
    ):

        usd = 0

    if usd > 0:

        rate = usd_eur()

        if rate:

            return int(
                round(
                    usd * rate
                )
            )

    # -------------------------
    # WEI ESCLUSO
    # -------------------------

    if amounts.get("wei") is not None:

        print(
            "🚫 WEI escluso dal "
            "calcolo FIAT",
            flush=True
        )

    return None


# ============================================================
# LIVE FLOOR
# ============================================================

def get_live_floor(card):

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = normalize(
        player.get("slug")
    )

    rarity = normalize(
        card.get("rarityTyped")
    )

    try:

        season = int(
            card.get("seasonYear")
        )

    except (
        TypeError,
        ValueError
    ):

        return None, "unknown_price"

    if not player_slug or not rarity:

        return None, "unknown_price"

    data = graphql("""
        query LiveSales(
            $playerSlug: String,
            $first: Int
        ) {

            tokens {

                liveSingleSaleOffers(
                    playerSlug: $playerSlug
                    first: $first
                ) {

                    nodes {

                        id

                        senderSide {

                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }

                            anyCards {

                                assetId
                                rarityTyped
                                seasonYear

                                anyPlayer {
                                    slug
                                }
                            }
                        }

                        receiverSide {

                            amounts {
                                eurCents
                                usdCents
                                referenceCurrency
                                wei
                            }
                        }
                    }
                }
            }
        }
    """, {
        "playerSlug": player_slug,
        "first": 50
    })

    if not data or data.get("errors"):

        return None, "unknown_price"

    offers = (
        (
            ((data.get("data") or {})
            .get("tokens") or {})
            .get("liveSingleSaleOffers")
            or {}
        )
        .get("nodes")
        or []
    )

    prices = []

    for offer in offers:

        sender = (
            offer.get("senderSide")
            or {}
        )

        compatible = False

        for c in (
            sender.get("anyCards")
            or []
        ):

            c_player = normalize(
                (c.get("anyPlayer") or {})
                .get("slug")
            )

            try:

                c_season = int(
                    c.get("seasonYear")
                )

            except (
                TypeError,
                ValueError
            ):

                c_season = None

            if (
                c_player == player_slug
                and normalize(
                    c.get("rarityTyped")
                ) == rarity
                and c_season == season
            ):

                compatible = True
                break

        if not compatible:
            continue

        amounts = (
            (offer.get("receiverSide") or {})
            .get("amounts")
            or {}
        )

        price = price_eur_cents(
            amounts
        )

        if price is not None:
            prices.append(price)

    # ========================================================
    # MINIMO 5 LISTING
    # ========================================================

    if len(prices) < MIN_LIVE_LISTINGS:

        print(
            f"      ⚠️ Inserzioni valide: "
            f"{len(prices)}/"
            f"{MIN_LIVE_LISTINGS}",
            flush=True
        )

        return None, "invalid"

    floor = min(prices)

    print(
        f"      📊 Inserzioni valide: "
        f"{len(prices)}",
        flush=True
    )

    print(
        f"      💰 FLOOR LIVE: "
        f"€{floor / 100:.2f}",
        flush=True
    )

    return floor, "valid"


# ============================================================
# COMPETIZIONI
# ============================================================

def competitions(card):

    club = (
        (card.get("anyPlayer") or {})
        .get("activeClub")
        or {}
    )

    result = []

    for c in (
        club.get("activeCompetitions")
        or []
    ):

        if (
            isinstance(c, dict)
            and c.get("slug")
        ):

            slug = normalize(
                c["slug"]
            )

            if slug:
                result.append(slug)

    return list(
        dict.fromkeys(result)
    )


def covered_competitions(card):

    active = competitions(card)

    coverage = load_coverage()

    covered = [
        x for x in active
        if x in coverage
    ]

    not_covered = [
        x for x in active
        if x not in coverage
    ]

    return (
        active,
        covered,
        not_covered
    )


# ============================================================
# VALID CARD - STESSI FILTRI AUTOBUY
# ============================================================

def valid_card(card):

    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    player = (
        card.get("anyPlayer")
        or {}
    )

    print(
        f"   📄 {name}",
        flush=True
    )

    # -------------------------
    # ETÀ
    # -------------------------

    try:

        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError
    ):

        print(
            "      ❌ Età sconosciuta",
            flush=True
        )

        return False, "invalid"

    print(
        f"      🎂 Età: {age}",
        flush=True
    )

    if age >= MAX_AGE:

        print(
            "      ❌ Età troppo alta",
            flush=True
        )

        return False, "invalid"

    # -------------------------
    # RARITÀ
    # -------------------------

    rarity = normalize(
        card.get("rarityTyped")
    ).upper()

    if rarity != "LIMITED":

        print(
            f"      ❌ Rarità: {rarity}",
            flush=True
        )

        return False, "invalid"

    # -------------------------
    # FLOOR
    # -------------------------

    price, reason = get_live_floor(
        card
    )

    if reason == "invalid":

        print(
            "      ❌ Meno di "
            "5 inserzioni valide",
            flush=True
        )

        return False, "invalid"

    if price is None:

        print(
            "      🟡 Prezzo sconosciuto",
            flush=True
        )

        return False, "unknown_price"

    print(
        f"      💰 Floor: "
        f"€{price / 100:.2f}",
        flush=True
    )

    # -------------------------
    # RANGE AUTOBUY
    # -------------------------

    if not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):

        print(
            "      ❌ Prezzo fuori "
            "range AutoBuy",
            flush=True
        )

        return False, "invalid"

    # -------------------------
    # COVERAGE
    # -------------------------

    active, covered, not_covered = (
        covered_competitions(card)
    )

    if not active:

        print(
            "      ❌ Nessuna "
            "activeCompetition",
            flush=True
        )

        return False, "invalid"

    if not covered:

        print(
            "      ❌ Nessuna "
            "competizione covered",
            flush=True
        )

        return False, "invalid"

    print(
        f"      🏆 Covered: "
        f"{', '.join(covered)}",
        flush=True
    )

    print(
        "      ✅ CARTA VALIDA",
        flush=True
    )

    return True, "valid"


# ============================================================
# CASH OFFERTO DAL MANAGER
# ============================================================

def get_cash_offered_eur_cents(offer):

    sender_side = (
        offer.get("senderSide")
        or {}
    )

    amounts = (
        sender_side.get("amounts")
        or {}
    )

    value = price_eur_cents(
        amounts
    )

    if value is None:
        return 0

    return value


# ============================================================
# RETRY UNKNOWN
# ============================================================

def retry_unknown(offer_id):

    now = time.time()

    with state_lock:

        last = unknown_price.get(
            offer_id
        )

        if last is None:

            unknown_price[
                offer_id
            ] = now

            return True

        if (
            now - last
            >= UNKNOWN_PRICE_RETRY
        ):

            unknown_price[
                offer_id
            ] = now

            return True

    return False


# ============================================================
# COMPLETED
# ============================================================

def completed(offer_id):

    with state_lock:

        processed.add(
            offer_id
        )

        unknown_price.pop(
            offer_id,
            None
        )


# ============================================================
# ANALISI SWAP
# ============================================================

def analyze_swap(offer):

    offer_id = normalize(
        offer.get("id")
    )

    if not offer_id:
        return

    with state_lock:

        if offer_id in processed:
            return

    if not retry_unknown(offer_id):
        return

    sender_side = (
        offer.get("senderSide")
        or {}
    )

    receiver_side = (
        offer.get("receiverSide")
        or {}
    )

    # ========================================================
    # DIREZIONE CORRETTA
    #
    # senderSide:
    #   carte + cash che IL MANAGER offre a noi
    #
    # receiverSide:
    #   carte che NOI dovremmo dare al manager
    # ========================================================

    cards_they_give = (
        sender_side.get("anyCards")
        or []
    )

    cards_we_give = (
        receiver_side.get("anyCards")
        or []
    )

    # ========================================================
    # NON È UNO SWAP
    # ========================================================

    if not cards_they_give:

        completed(offer_id)
        return

    if not cards_we_give:

        completed(offer_id)
        return

    print(
        "\n" + "=" * 65,
        flush=True
    )

    print(
        f"🔄 SWAP RICEVUTO: "
        f"{offer_id}",
        flush=True
    )

    sender = (
        offer.get("sender")
        or {}
    )

    print(
        f"👤 Manager: "
        f"{sender.get('nickname') or sender.get('slug')}",
        flush=True
    )

    print(
        "=" * 65,
        flush=True
    )

    # ========================================================
    # ASSET IDS
    # ========================================================

    give_ids = [
        c.get("assetId")
        for c in cards_we_give
        if c.get("assetId")
    ]

    receive_ids = [
        c.get("assetId")
        for c in cards_they_give
        if c.get("assetId")
    ]

    if not give_ids or not receive_ids:

        completed(offer_id)
        return

    # ========================================================
    # CARD DETAILS
    # ========================================================

    cards_we_give_details = card_details(
        give_ids
    )

    cards_they_give_details = card_details(
        receive_ids
    )

    if (
        len(cards_we_give_details)
        != len(give_ids)
    ):

        print(
            "⚠️ Impossibile verificare "
            "tutte le nostre carte",
            flush=True
        )

        return

    if (
        len(cards_they_give_details)
        != len(receive_ids)
    ):

        print(
            "⚠️ Impossibile verificare "
            "tutte le carte ricevute",
            flush=True
        )

        return

    # ========================================================
    # CONTROLLO FONDAMENTALE
    #
    # IL TUO KULENOVIC NON PUÒ MAI ESSERE CEDUTO.
    #
    # ATTENZIONE:
    # non blocchiamo "Kulenovic" in generale.
    #
    # Se il manager offre UN ALTRO Kulenovic:
    #   → viene valutato normalmente.
    #
    # Se il manager richiede IL TUO Kulenovic:
    #   → swap rifiutato/ignorato.
    # ========================================================

    my_kulenovic_requested = any(
        is_my_kulenovic(card)
        for card in cards_we_give_details
    )

    if my_kulenovic_requested:

        print(
            "\n🚫 PROTEZIONE KULENOVIC",
            flush=True
        )

        print(
            "   Il manager sta richiedendo "
            "IL TUO Kulenovic.",
            flush=True
        )

        print(
            "   ❌ Questo Kulenovic "
            "non può MAI essere ceduto.",
            flush=True
        )

        print(
            "   🛑 SWAP NON APPROVABILE",
            flush=True
        )

        completed(offer_id)

        return

    # ========================================================
    # FLOOR CARTE CHE CEDIAMO
    # ========================================================

    total_given_floor = 0

    for card in cards_we_give_details:

        name = (
            card.get("name")
            or card.get("slug")
            or "Carta"
        )

        print(
            f"\n📤 CEDI: {name}",
            flush=True
        )

        floor, reason = get_live_floor(
            card
        )

        if floor is None:

            print(
                "   🟡 Floor sconosciuto "
                "→ SWAP PENDING",
                flush=True
            )

            return

        total_given_floor += floor

        print(
            f"   💰 Floor ceduta: "
            f"€{floor / 100:.2f}",
            flush=True
        )

    # ========================================================
    # FLOOR CARTE CHE RICEVI
    #
    # QUI KULENOVIC DI UN ALTRO MANAGER
    # VIENE TRATTATO NORMALMENTE.
    # ========================================================

    total_received_floor = 0

    for card in cards_they_give_details:

        name = (
            card.get("name")
            or card.get("slug")
            or "Carta"
        )

        print(
            f"\n📥 RICEVO: {name}",
            flush=True
        )

        # ----------------------------------------------------
        # NOTA:
        # NON controlliamo is_my_kulenovic() qui.
        #
        # Se è un Kulenovic appartenente al manager,
        # viene valutato normalmente.
        # ----------------------------------------------------

        ok, reason = valid_card(
            card
        )

        if reason == "unknown_price":

            print(
                "🟡 Floor sconosciuto "
                "→ SWAP PENDING",
                flush=True
            )

            return

        if not ok:

            print(
                "❌ Una carta ricevuta "
                "non soddisfa i "
                "parametri AutoBuy",
                flush=True
            )

            print(
                "🔴 SWAP NON APPROVABILE",
                flush=True
            )

            completed(offer_id)

            return

        floor, floor_reason = get_live_floor(
            card
        )

        if floor is None:

            print(
                "🟡 Floor non verificabile "
                "→ SWAP PENDING",
                flush=True
            )

            return

        total_received_floor += floor

        print(
            f"   💰 Floor ricevuto: "
            f"€{floor / 100:.2f}",
            flush=True
        )

    # ========================================================
    # CASH
    #
    # Il denaro offerto dal manager viene aggiunto
    # al floor delle carte ricevute.
    # ========================================================

    cash_eur = get_cash_offered_eur_cents(
        offer
    )

    print(
        "\n💶 CASH OFFERTO: "
        f"€{cash_eur / 100:.2f}",
        flush=True
    )

    # ========================================================
    # TOTALI
    # ========================================================

    total_received = (
        total_received_floor
        + cash_eur
    )

    minimum_required = int(
        round(
            total_given_floor
            * SWAP_MIN_MULTIPLIER
        )
    )

    maximum_allowed = int(
        round(
            total_given_floor
            * SWAP_MAX_MULTIPLIER
        )
    )

    print(
        "\n" + "-" * 65,
        flush=True
    )

    print(
        f"📤 FLOOR TOTALE CEDUTO: "
        f"€{total_given_floor / 100:.2f}",
        flush=True
    )

    print(
        f"📥 FLOOR CARTE RICEVUTE: "
        f"€{total_received_floor / 100:.2f}",
        flush=True
    )

    print(
        f"💶 CASH RICEVUTO: "
        f"€{cash_eur / 100:.2f}",
        flush=True
    )

    print(
        f"📥 VALORE TOTALE RICEVUTO: "
        f"€{total_received / 100:.2f}",
        flush=True
    )

    print(
        f"📈 MINIMO RICHIESTO (+20%): "
        f"€{minimum_required / 100:.2f}",
        flush=True
    )

    print(
        f"📈 MASSIMO CONSENTITO (+25%): "
        f"€{maximum_allowed / 100:.2f}",
        flush=True
    )

    # ========================================================
    # SICUREZZA EXTRA
    #
    # Non deve mai essere possibile avere un valore ceduto
    # pari a zero.
    # ========================================================

    if total_given_floor <= 0:

        print(
            "❌ Floor ceduto non valido",
            flush=True
        )

        completed(offer_id)

        return

    # ========================================================
    # DECISIONE
    # ========================================================

    if total_received < minimum_required:

        print(
            "\n❌ SWAP NON APPROVABILE",
            flush=True
        )

        print(
            "   Motivo: valore ricevuto "
            "inferiore al +20%",
            flush=True
        )

        completed(offer_id)

        return

    if total_received > maximum_allowed:

        print(
            "\n❌ SWAP NON APPROVABILE",
            flush=True
        )

        print(
            "   Motivo: valore ricevuto "
            "superiore al +25%",
            flush=True
        )

        completed(offer_id)

        return

    # ========================================================
    # APPROVABILE
    # ========================================================

    percentage = (
        (
            total_received
            / total_given_floor
        )
        - 1
    ) * 100

    print(
        "\n✅ SWAP APPROVABILE",
        flush=True
    )

    print(
        f"📈 Premium effettivo: "
        f"+{percentage:.2f}%",
        flush=True
    )

    print(
        "🛑 MODALITÀ ANALISI:",
        flush=True
    )

    print(
        "   NESSUNA AZIONE ESEGUITA",
        flush=True
    )

    completed(offer_id)


# ============================================================
# WORKER
# ============================================================

def worker():

    print(
        "🤖 SWAP BOT AVVIATO",
        flush=True
    )

    print(
        f"📦 VERSIONE: "
        f"{BOT_VERSION}",
        flush=True
    )

    print(
        "🛡️ MODALITÀ: "
        "ANALYSIS ONLY",
        flush=True
    )

    print(
        "🔒 IL TUO KULENOVIC: "
        "MAI CEDIBILE",
        flush=True
    )

    if KID:

        print(
            "🔑 KULENOVIC_ID: "
            "CONFIGURATO",
            flush=True
        )

    else:

        print(
            "⚠️ KULENOVIC_ID: "
            "NON CONFIGURATO",
            flush=True
        )

    print(
        "🎂 Età: "
        f"< {MAX_AGE}",
        flush=True
    )

    print(
        "💎 Rarità: LIMITED",
        flush=True
    )

    print(
        f"💰 Floor AutoBuy: "
        f"€{MIN_PRICE / 100:.2f}"
        f" - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True
    )

    print(
        f"📊 Inserzioni minime: "
        f"{MIN_LIVE_LISTINGS}",
        flush=True
    )

    print(
        f"📈 Swap minimo: "
        f"+{(SWAP_MIN_MULTIPLIER - 1) * 100:.0f}%",
        flush=True
    )

    print(
        f"📈 Swap massimo: "
        f"+{(SWAP_MAX_MULTIPLIER - 1) * 100:.0f}%",
        flush=True
    )

    print(
        "💶 Cash incluso "
        "nel valore ricevuto",
        flush=True
    )

    print(
        "🚫 Nessuna accettazione "
        "automatica",
        flush=True
    )

    print(
        "🚫 Nessun rifiuto "
        "automatico",
        flush=True
    )

    # ========================================================
    # COVERAGE
    # ========================================================

    covered = load_coverage(
        force=True
    )

    if not covered:

        print(
            "❌ Coverage non disponibile",
            flush=True
        )

        return

    print(
        f"🏆 Competizioni Football "
        f"coperte: {len(covered)}",
        flush=True
    )

    # ========================================================
    # ACCOUNT
    # ========================================================

    if not check_account():
        return

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True
            )

            for offer in offers:

                try:

                    analyze_swap(
                        offer
                    )

                except Exception as e:

                    print(
                        f"❌ Errore SWAP: "
                        f"{e}",
                        flush=True
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as e:

            print(
                f"❌ Worker SWAP: "
                f"{e}",
                flush=True
            )

            time.sleep(
                INTERVAL
            )


# ============================================================
# START
# ============================================================

worker_started = False
worker_lock = threading.Lock()


def start_worker():

    global worker_started

    with worker_lock:

        if worker_started:
            return

        worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-swap-worker",
            daemon=True
        ).start()

        print(
            "✅ Thread Swap avviato.",
            flush=True
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():

    covered = load_coverage()

    return jsonify({

        "status":
            "online",

        "bot":
            "sorare-swap",

        "version":
            BOT_VERSION,

        "mode":
            "ANALYSIS_ONLY",

        "interval_seconds":
            INTERVAL,

        "min_price_cents":
            MIN_PRICE,

        "max_price_cents":
            MAX_PRICE,

        "max_age":
            MAX_AGE,

        "min_live_listings":
            MIN_LIVE_LISTINGS,

        "swap_min_multiplier":
            SWAP_MIN_MULTIPLIER,

        "swap_max_multiplier":
            SWAP_MAX_MULTIPLIER,

        "cash_included":
            True,

        "my_kulenovic_protected":
            True,

        "my_kulenovic_id_configured":
            bool(KID),

        "automatic_accept":
            False,

        "automatic_reject":
            False,

        "covered_competitions_count":
            len(covered),

        "price_mode":
            "LIVE_SINGLE_SALE_EXACT_PLAYER_RARITY_SEASON",

        "price_eur_mode":
            "EUR_DIRECT_OR_USD_CONVERTED",

        "usd_conversion":
            True,

        "wei_conversion":
            False,

        "wei_excluded":
            True
    })


@app.get("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot":
            "sorare-swap",

        "version":
            BOT_VERSION,

        "mode":
            "ANALYSIS_ONLY",

        "my_kulenovic_protected":
            True,

        "my_kulenovic_id_configured":
            bool(KID)
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "SWAP_PORT",
                "10001"
            )
        )
    )
