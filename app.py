import os
import time
import uuid
import json
import shutil
import subprocess
import threading
import re
from decimal import Decimal, ROUND_DOWN

import requests
from flask import Flask, jsonify


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

URL = "https://api.sorare.com/graphql"
SCHEMA_URL = "https://api.sorare.com/graphql/schema"

TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
AUD = os.getenv("SORARE_JWT_AUD", "").strip()
STARK = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()
KID = os.getenv("KULENOVIC_ID", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MIN_PRICE = 32
MAX_PRICE = 70
PAY_PER_CARD = 20
MAX_AGE = 28
INTERVAL = 10
TIMEOUT = 30

KSLUG = "sandro-kulenovic-2025-limited-385"

KASSET = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

processed = set()
state_lock = threading.Lock()

_worker_started = False
_worker_lock = threading.Lock()

_schema_lock = threading.Lock()
_schema_text = None

# Cache del cambio ETH/EUR.
# Serve solamente se una live sale offer è espressa in WEI
# e Sorare non restituisce eurCents.
_exchange_lock = threading.Lock()
_exchange_cache = None
_exchange_cache_time = 0
EXCHANGE_CACHE_SECONDS = 60


# ============================================================
# UTILS
# ============================================================

def slug(v):
    v = str(v or "").strip().lower()

    for a, b in [
        ("_", "-"),
        (" ", "-"),
        ("’", ""),
        ("'", ""),
    ]:
        v = v.replace(a, b)

    while "--" in v:
        v = v.replace("--", "-")

    return v


def auth_headers():
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
        "User-Agent": "Sorare-Bot/16.2",
    }

    if AUD:
        h["JWT-AUD"] = AUD

    return h


# ============================================================
# GRAPHQL
# ============================================================

def graphql(query, variables=None):
    payload = {
        "query": query,
        "variables": variables or {},
    }

    for attempt in range(1, 4):
        try:
            r = requests.post(
                URL,
                json=payload,
                headers=auth_headers(),
                timeout=TIMEOUT,
            )

            print(
                f"🌐 Sorare HTTP {r.status_code}",
                flush=True,
            )

            if r.status_code == 429:
                try:
                    wait = int(
                        r.headers.get(
                            "Retry-After",
                            attempt * 3,
                        )
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    wait = attempt * 3

                print(
                    f"⏳ Rate limit: {wait}s",
                    flush=True,
                )

                time.sleep(wait)
                continue

            if r.status_code != 200:
                print(
                    f"❌ HTTP {r.status_code}: "
                    f"{r.text[:1500]}",
                    flush=True,
                )

                time.sleep(attempt)
                continue

            try:
                data = r.json()

            except ValueError:
                print(
                    "❌ JSON Sorare non valido",
                    flush=True,
                )
                return None

            if data.get("errors"):
                print(
                    "❌ GraphQL ERROR:",
                    flush=True,
                )

                for e in data["errors"]:
                    print(
                        json.dumps(
                            e,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                return data

            return data

        except requests.RequestException as e:
            print(
                f"❌ HTTP: {e}",
                flush=True,
            )

            time.sleep(attempt)

        except Exception as e:
            print(
                f"❌ GraphQL: {e}",
                flush=True,
            )

            return None

    return None


# ============================================================
# LIVE SCHEMA
# ============================================================

def get_live_schema():
    global _schema_text

    with _schema_lock:
        if _schema_text is not None:
            return _schema_text

        try:
            r = requests.get(
                SCHEMA_URL,
                timeout=TIMEOUT,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Sorare-Bot/16.2",
                },
            )

            print(
                f"📚 Schema Sorare HTTP {r.status_code}",
                flush=True,
            )

            if r.status_code != 200:
                print(
                    "⚠️ Impossibile scaricare "
                    "lo schema live",
                    flush=True,
                )

                return None

            _schema_text = r.text

            print(
                f"✅ Schema live scaricato "
                f"({len(_schema_text)} caratteri)",
                flush=True,
            )

            return _schema_text

        except Exception as e:
            print(
                f"⚠️ Errore download schema: {e}",
                flush=True,
            )

            return None


def get_input_fields(type_name):
    schema = get_live_schema()

    if not schema:
        return set()

    m = re.search(
        r"\binput\s+"
        + re.escape(type_name)
        + r"\s*\{",
        schema,
        re.MULTILINE,
    )

    if not m:
        print(
            f"⚠️ {type_name} non trovato "
            "nello schema",
            flush=True,
        )

        return set()

    start = m.end()
    depth = 1
    pos = start

    while pos < len(schema) and depth:
        if schema[pos] == "{":
            depth += 1

        elif schema[pos] == "}":
            depth -= 1

        pos += 1

    block = schema[start:pos - 1]
    fields = set()

    for line in block.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        m = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*"
            r"(?:\([^)]*\))?\s*:",
            line,
        )

        if m:
            fields.add(m.group(1))

    print(
        f"🔍 Campi {type_name}: "
        f"{', '.join(sorted(fields))}",
        flush=True,
    )

    return fields


def inspect_live_schema():
    print(
        "🔎 CONTROLLO SCHEMA LIVE",
        flush=True,
    )

    p = get_input_fields(
        "prepareOfferInput"
    )

    c = get_input_fields(
        "createDirectOfferInput"
    )

    if p:
        print(
            "   prepareOfferInput:",
            flush=True,
        )

        for x in sorted(p):
            print(
                f"      • {x}",
                flush=True,
            )

    if c:
        print(
            "   createDirectOfferInput:",
            flush=True,
        )

        for x in sorted(c):
            print(
                f"      • {x}",
                flush=True,
            )

    return p, c


# ============================================================
# ACCOUNT / OFFERS
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
            flush=True,
        )

        return False

    print(
        f"✅ Sorare: "
        f"{user.get('nickname') or user.get('slug')}",
        flush=True,
    )

    return True


def get_offers():
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

    user = (
        ((data or {}).get("data") or {})
        .get("currentUser")
        or {}
    )

    return (
        (user.get(
            "pendingTokenOffersReceived"
        ) or {})
        .get("nodes")
        or []
    )


# ============================================================
# CARDS
# ============================================================

def card_details(asset_ids):
    asset_ids = list(dict.fromkeys(
        str(x).strip()
        for x in asset_ids
        if x
    ))

    if not asset_ids:
        return []

    # ========================================================
    # DATI CARTA
    #
    # IL PREZZO NON VIENE PIÙ LETTO DA:
    #   - lowestPriceCard
    #   - lowestPriceCardAnySeason
    #   - latestEnglishAuction
    #
    # Il prezzo viene letto successivamente
    # dal market live:
    #
    # tokens.liveSingleSaleOffers
    # ========================================================

    data = graphql("""
        query Cards($assetIds: [String!]!) {
            anyCards(assetIds: $assetIds) {
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
        "assetIds": asset_ids,
    })

    if not data:
        print(
            "❌ Nessuna risposta da anyCards",
            flush=True,
        )

        return []

    if data.get("errors"):
        print(
            "❌ Errore GraphQL card_details:",
            flush=True,
        )

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return []

    cards = (
        ((data.get("data") or {})
         .get("anyCards"))
        or []
    )

    return cards


# ============================================================
# EXCHANGE RATE
# ============================================================

def get_eth_eur_rate_cents():
    """
    Recupera il cambio ETH -> EUR corrente da Sorare.

    Il valore restituito da:
        config.exchangeRate.ethRates.eurCents

    rappresenta il valore di 1 ETH espresso in centesimi EUR.

    Viene usato SOLO quando una live sale offer
    è quotata in WEI e eurCents non è disponibile.
    """

    global _exchange_cache
    global _exchange_cache_time

    now = time.time()

    with _exchange_lock:
        if (
            _exchange_cache is not None
            and
            now - _exchange_cache_time
            < EXCHANGE_CACHE_SECONDS
        ):
            return _exchange_cache

        data = graphql("""
            query EthEurRate {
                config {
                    exchangeRate {
                        id
                        time

                        ethRates {
                            eurCents
                        }
                    }
                }
            }
        """)

        if not data:
            return None

        if data.get("errors"):
            print(
                "❌ Errore cambio ETH/EUR:",
                flush=True,
            )

            for e in data["errors"]:
                print(
                    json.dumps(
                        e,
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            return None

        config = (
            ((data.get("data") or {})
             .get("config"))
            or {}
        )

        exchange = (
            config.get("exchangeRate")
            or {}
        )

        eth_rates = (
            exchange.get("ethRates")
            or {}
        )

        value = eth_rates.get(
            "eurCents"
        )

        try:
            value = int(value)

        except (
            TypeError,
            ValueError,
        ):
            return None

        if value <= 0:
            return None

        _exchange_cache = value
        _exchange_cache_time = now

        print(
            f"💱 Cambio Sorare ETH/EUR: "
            f"€{value / 100:.2f}",
            flush=True,
        )

        return value


# ============================================================
# PRICE EXTRACTION
# ============================================================

def extract_eur_cents(source):
    """
    Estrae eurCents da MonetaryAmount.
    """

    if not isinstance(source, dict):
        return None

    value = source.get(
        "eurCents"
    )

    if value is None:
        return None

    try:
        value = int(value)

    except (
        TypeError,
        ValueError,
    ):
        return None

    if value > 0:
        return value

    return None


def extract_wei(source):
    """
    Estrae il valore WEI da MonetaryAmount.
    """

    if not isinstance(source, dict):
        return None

    value = source.get("wei")

    if value is None:
        return None

    try:
        value = int(str(value))

    except (
        TypeError,
        ValueError,
    ):
        return None

    if value > 0:
        return value

    return None


def monetary_amount_to_eur_cents(
    amounts
):
    """
    Converte MonetaryAmount in EUR cents.

    Ordine:

    1. eurCents diretto
    2. se referenceCurrency == WEI:
       conversione WEI -> EUR con cambio Sorare
    """

    if not isinstance(
        amounts,
        dict,
    ):
        return None, None

    # --------------------------------------------------------
    # 1. EUR DIRETTO
    # --------------------------------------------------------

    eur = extract_eur_cents(
        amounts
    )

    if eur is not None:
        return eur, "eurCents"

    # --------------------------------------------------------
    # 2. WEI -> EUR
    # --------------------------------------------------------

    reference = str(
        amounts.get(
            "referenceCurrency"
        ) or ""
    ).upper()

    wei = extract_wei(
        amounts
    )

    if (
        reference == "WEI"
        and
        wei is not None
    ):
        eth_eur_cents = (
            get_eth_eur_rate_cents()
        )

        if eth_eur_cents is None:
            return None, None

        # EUR cents =
        # WEI / 1e18 * EUR cents per ETH
        value = (
            Decimal(wei)
            * Decimal(eth_eur_cents)
            / Decimal("1000000000000000000")
        )

        value = int(
            value.quantize(
                Decimal("1"),
                rounding=ROUND_DOWN,
            )
        )

        if value > 0:
            return (
                value,
                "wei→EUR",
            )

    return None, None


def extract_offer_price(
    offer
):
    """
    Legge il prezzo della live Single Sale Offer.
    """

    if not isinstance(
        offer,
        dict,
    ):
        return None, None

    receiver_side = (
        offer.get("receiverSide")
        or {}
    )

    amounts = (
        receiver_side.get("amounts")
        or {}
    )

    return monetary_amount_to_eur_cents(
        amounts
    )


# ============================================================
# NUOVO SISTEMA DI LETTURA FLOOR
# ============================================================

def get_live_single_sale_floor(
    card
):
    """
    NUOVO METODO PREZZO.

    Cerca esclusivamente le LIVE SINGLE SALE OFFERS
    del giocatore.

    NON usa:
        - latestEnglishAuction
        - lowestPriceCard
        - lowestPriceCardAnySeason
        - tokenPrices
        - aste storiche

    Filtra poi le offerte per:

        giocatore
        rarità
        stagione

    e prende il prezzo minimo realmente disponibile.

    Questo evita il problema osservato nei log dove:

        lowestPriceCard
        -> liveSingleSaleOffer
        -> eurCents: null

    """

    if not isinstance(
        card,
        dict,
    ):
        return None

    player = (
        card.get("anyPlayer")
        or {}
    )

    player_slug = str(
        player.get("slug")
        or ""
    ).strip()

    if not player_slug:
        print(
            "      ❌ playerSlug mancante "
            "per ricerca floor",
            flush=True,
        )

        return None

    card_rarity = str(
        card.get("rarityTyped")
        or ""
    ).upper()

    card_season = card.get(
        "seasonYear"
    )

    try:
        card_season = int(
            card_season
        )

    except (
        TypeError,
        ValueError,
    ):
        card_season = None

    if card_rarity != "LIMITED":
        print(
            "      ⚠️ Floor live richiesto "
            f"per rarità {card_rarity}",
            flush=True,
        )

    print(
        "      🔎 FLOOR LIVE:",
        flush=True,
    )

    print(
        f"         👤 playerSlug: "
        f"{player_slug}",
        flush=True,
    )

    print(
        f"         🏷️ rarità: "
        f"{card_rarity}",
        flush=True,
    )

    print(
        f"         📅 stagione: "
        f"{card_season}",
        flush=True,
    )

    candidates = []

    after = None
    page = 0

    # ========================================================
    # PAGINAZIONE LIVE SINGLE SALE OFFERS
    # ========================================================

    while True:
        page += 1

        data = graphql("""
            query LiveSingleSaleOffers(
                $playerSlug: String
                $after: String
            ) {
                tokens {
                    liveSingleSaleOffers(
                        first: 100
                        after: $after
                        playerSlug: $playerSlug
                        sport: FOOTBALL
                    ) {
                        nodes {
                            id
                            status

                            receiverSide {
                                amounts {
                                    eurCents
                                    referenceCurrency
                                    wei
                                }

                                anyCards {
                                    assetId
                                    slug
                                    rarityTyped
                                    seasonYear
                                }
                            }

                            senderSide {
                                anyCards {
                                    assetId
                                    slug
                                    rarityTyped
                                    seasonYear
                                }
                            }
                        }

                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
        """, {
            "playerSlug": player_slug,
            "after": after,
        })

        if not data:
            print(
                "      ❌ Nessuna risposta "
                "dal market live",
                flush=True,
            )

            return None

        if data.get("errors"):
            print(
                "      ❌ Errore GraphQL "
                "liveSingleSaleOffers:",
                flush=True,
            )

            for e in data["errors"]:
                print(
                    json.dumps(
                        e,
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            return None

        root = (
            ((data.get("data") or {})
             .get("tokens"))
            or {}
        )

        connection = (
            root.get(
                "liveSingleSaleOffers"
            )
            or {}
        )

        offers = (
            connection.get("nodes")
            or []
        )

        print(
            f"         📦 pagina {page}: "
            f"{len(offers)} live offer",
            flush=True,
        )

        # ====================================================
        # ANALISI OFFERTE
        # ====================================================

        for offer in offers:

            receiver_side = (
                offer.get(
                    "receiverSide"
                )
                or {}
            )

            receiver_cards = (
                receiver_side.get(
                    "anyCards"
                )
                or []
            )

            # Una single sale offer può avere più carte.
            # La consideriamo compatibile solo se contiene
            # una carta dello stesso player/rarità/stagione.
            matching_card = None

            for market_card in receiver_cards:

                if not isinstance(
                    market_card,
                    dict,
                ):
                    continue

                market_rarity = str(
                    market_card.get(
                        "rarityTyped"
                    )
                    or ""
                ).upper()

                market_season = (
                    market_card.get(
                        "seasonYear"
                    )
                )

                try:
                    market_season = int(
                        market_season
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    market_season = None

                market_slug = str(
                    market_card.get(
                        "slug"
                    )
                    or ""
                ).lower()

                market_asset = str(
                    market_card.get(
                        "assetId"
                    )
                    or ""
                ).lower()

                # ------------------------------------------------
                # Verifica giocatore tramite slug carta.
                #
                # La query è già filtrata per playerSlug, quindi
                # qui usiamo soprattutto rarità/stagione.
                # ------------------------------------------------

                same_rarity = (
                    market_rarity
                    == card_rarity
                )

                same_season = (
                    card_season is None
                    or
                    market_season
                    == card_season
                )

                if (
                    same_rarity
                    and
                    same_season
                ):
                    matching_card = (
                        market_card
                    )

                    break

            if not matching_card:
                continue

            price, source = (
                extract_offer_price(
                    offer
                )
            )

            if price is None:
                print(
                    "         ⚠️ Live offer "
                    "trovata ma prezzo EUR "
                    "non leggibile",
                    flush=True,
                )

                print(
                    json.dumps(
                        {
                            "offerId":
                                offer.get("id"),
                            "amounts":
                                receiver_side.get(
                                    "amounts"
                                ),
                            "card":
                                matching_card,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

                continue

            if price <= 0:
                continue

            candidates.append(
                (
                    price,
                    source,
                    offer.get("id"),
                    matching_card,
                )
            )

            print(
                f"         💶 Live sale: "
                f"€{price / 100:.2f} "
                f"({source})",
                flush=True,
            )

        # ====================================================
        # PAGINA SUCCESSIVA
        # ====================================================

        page_info = (
            connection.get(
                "pageInfo"
            )
            or {}
        )

        if not page_info.get(
            "hasNextPage"
        ):
            break

        next_cursor = (
            page_info.get(
                "endCursor"
            )
        )

        if not next_cursor:
            break

        after = next_cursor

        # Protezione contro loop anomali.
        if page >= 10:
            print(
                "      ⚠️ Limite pagine "
                "market live raggiunto",
                flush=True,
            )

            break

    # ========================================================
    # RISULTATO
    # ========================================================

    if not candidates:
        print(
            "      ❌ Nessuna LIVE SINGLE SALE "
            "compatibile trovata",
            flush=True,
        )

        return None

    price, source, offer_id, market_card = min(
        candidates,
        key=lambda x: x[0],
    )

    print(
        f"      ✅ FLOOR LIVE: "
        f"€{price / 100:.2f}",
        flush=True,
    )

    print(
        f"         fonte: "
        f"liveSingleSaleOffers",
        flush=True,
    )

    print(
        f"         offer: "
        f"{offer_id}",
        flush=True,
    )

    print(
        f"         card: "
        f"{market_card.get('slug')}",
        flush=True,
    )

    return price


def card_price(card):
    """
    NUOVO SISTEMA DI PREZZO.

    L'unica fonte di floor è:

        tokens.liveSingleSaleOffers

    Non vengono più utilizzati:

        latestEnglishAuction
        lowestPriceCard
        lowestPriceCardAnySeason
        publicMinPrices
        tokenPrices

    Se non esiste una live single sale offer leggibile,
    restituisce None.

    In quel caso l'offerta viene lasciata IN SOSPESO.
    """

    return get_live_single_sale_floor(
        card
    )


def is_kulenovic(card):
    wanted = {
        KSLUG.lower(),
        KASSET.lower(),
    }

    if KID:
        wanted.add(
            KID.lower()
        )

    return (
        str(
            card.get("assetId") or ""
        ).lower() in wanted
        or
        str(
            card.get("slug") or ""
        ).lower() in wanted
    )


# ============================================================
# COMPETITIONS / VALIDATION
# ============================================================

def get_competitions(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        return []

    result = []

    for c in (
        club.get(
            "activeCompetitions"
        ) or []
    ):
        if isinstance(c, dict):
            value = slug(
                c.get("slug")
            )

            if value:
                result.append(value)

    return list(
        dict.fromkeys(result)
    )


def check_competition(card):
    club = (
        card.get("anyPlayer") or {}
    ).get("activeClub")

    if not isinstance(club, dict):
        print(
            "      ❌ Nessuna squadra",
            flush=True,
        )

        return False

    name = (
        club.get("name")
        or club.get("slug")
        or "Sconosciuta"
    )

    competitions = get_competitions(
        card
    )

    print(
        f"      🏟️ Squadra: {name}",
        flush=True,
    )

    if not competitions:
        print(
            "      ❌ Nessuna "
            "activeCompetition",
            flush=True,
        )

        return False

    print(
        "      🏆 Competizioni Sorare:",
        flush=True,
    )

    for c in competitions:
        print(
            f"         🆕 {c} ({c})",
            flush=True,
        )

    print(
        "      ✅ COMPETIZIONE COPERTA",
        flush=True,
    )

    return True


def valid_card(card):
    name = (
        card.get("name")
        or card.get("slug")
        or "Carta"
    )

    rarity = str(
        card.get("rarityTyped") or ""
    ).upper()

    player = (
        card.get("anyPlayer") or {}
    )

    try:
        age = int(
            player.get("age")
        )

    except (
        TypeError,
        ValueError,
    ):
        print(
            f"   📄 {name}",
            flush=True,
        )

        print(
            "      ❌ Età non disponibile",
            flush=True,
        )

        return False

    print(
        f"   📄 {name}",
        flush=True,
    )

    print(
        f"      🎂 Età: {age} anni",
        flush=True,
    )

    if age >= MAX_AGE:
        print(
            f"      ❌ Età troppo alta "
            f"(limite: < {MAX_AGE})",
            flush=True,
        )

        return False

    # ========================================================
    # PREZZO
    # ========================================================

    price = card_price(card)

    if price is None:
        print(
            "      ⚠️ FLOOR NON TROVATO",
            flush=True,
        )

        return None

    print(
        f"      💰 Floor €{price / 100:.2f}",
        flush=True,
    )

    if not MIN_PRICE <= price <= MAX_PRICE:
        print(
            "      ❌ Prezzo fuori range",
            flush=True,
        )

        return False

    # ========================================================
    # RARITÀ
    # ========================================================

    if rarity != "LIMITED":
        print(
            f"      ❌ Rarità: {rarity}",
            flush=True,
        )

        return False

    # ========================================================
    # COMPETIZIONE
    # ========================================================

    if not check_competition(card):
        return False

    competitions = get_competitions(
        card
    )

    print(
        f"      ✅ VALIDATA | {age} anni | "
        f"€{price / 100:.2f} | "
        f"{', '.join(competitions)}",
        flush=True,
    )

    return True


# ============================================================
# REJECT
# ============================================================

def reject_offer(offer):
    blockchain_id = str(
        offer.get("blockchainId") or ""
    ).strip()

    if not blockchain_id:
        print(
            "❌ blockchainId mancante",
            flush=True,
        )

        return False

    if DRY_RUN:
        print(
            "🟡 DRY RUN: rifiuto simulato",
            flush=True,
        )

        return True

    data = graphql("""
        mutation Reject(
            $input: rejectOfferInput!
        ) {
            rejectOffer(input: $input) {
                tokenOffer {
                    id
                    status
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": {
            "blockchainId": blockchain_id,
            "clientMutationId": str(
                uuid.uuid4()
            ),
        }
    })

    result = (
        (data or {}).get("data") or {}
    ).get("rejectOffer")

    if not result:
        print(
            "❌ Risposta rejectOffer vuota",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:
        for e in errors:
            print(
                f"❌ Reject: "
                f"{e.get('message', 'Errore')}",
                flush=True,
            )

        return False

    print(
        "✅ Offerta originale rifiutata",
        flush=True,
    )

    return True


# ============================================================
# SIGN
# ============================================================

def sign_authorizations(authorizations):
    node = (
        shutil.which("node")
        or shutil.which("nodejs")
    )

    if not node:
        raise RuntimeError(
            "Node.js non disponibile"
        )

    script = r'''
const fs = require("fs");

const {
    signAuthorizationRequest
} = require("@sorare/crypto");

const input = JSON.parse(
    fs.readFileSync(0, "utf8")
);

function build(a) {
    const r = a.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename ===
            "StarkexTransferAuthorizationRequest" &&
        r.amount != null
    ) {
        r.amount = BigInt(r.amount);
    }

    const signature =
        signAuthorizationRequest(
            input.privateKey,
            r
        );

    if (
        r.__typename ===
        "StarkexTransferAuthorizationRequest"
    ) {
        return {
            fingerprint:
                a.fingerprint,

            starkexTransferApproval: {
                nonce: r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }

    if (
        r.__typename ===
        "StarkexLimitOrderAuthorizationRequest"
    ) {
        return {
            fingerprint:
                a.fingerprint,

            starkexLimitOrderApproval: {
                nonce: r.nonce,

                expirationTimestamp:
                    r.expirationTimestamp,

                signature
            }
        };
    }

    if (
        r.__typename ===
        "MangopayWalletTransferAuthorizationRequest"
    ) {
        return {
            fingerprint:
                a.fingerprint,

            mangopayWalletTransferApproval: {
                nonce: r.nonce,
                signature
            }
        };
    }

    throw new Error(
        "Authorization non supportata: " +
        r.__typename
    );
}

process.stdout.write(
    JSON.stringify(
        input.authorizations.map(build)
    )
);
'''

    p = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "privateKey": STARK,
            "authorizations": authorizations,
        }),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
    )

    if p.returncode != 0:
        raise RuntimeError(
            p.stderr.strip()
            or "Firma fallita"
        )

    return json.loads(
        p.stdout
    )


# ============================================================
# BUILD INPUTS
# ============================================================

def build_prepare_offer_input(
    asset_ids,
    receiver,
    amount,
):
    fields = get_input_fields(
        "prepareOfferInput"
    )

    if not fields:
        raise RuntimeError(
            "Impossibile leggere "
            "prepareOfferInput "
            "dallo schema live"
        )

    result = {}

    if "type" in fields:
        result["type"] = "DIRECT_OFFER"

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = (
            asset_ids
        )

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "settlementCurrencies" in fields:
        result["settlementCurrencies"] = [
            "EUR"
        ]

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(
            uuid.uuid4()
        )

    return result


def build_create_direct_offer_input(
    asset_ids,
    receiver,
    amount,
    approvals,
):
    fields = get_input_fields(
        "createDirectOfferInput"
    )

    if not fields:
        raise RuntimeError(
            "Impossibile leggere "
            "createDirectOfferInput "
            "dallo schema live"
        )

    result = {}

    if "approvals" in fields:
        result["approvals"] = approvals

    if "dealId" in fields:
        result["dealId"] = str(
            uuid.uuid4()
        )

    if "sendAssetIds" in fields:
        result["sendAssetIds"] = []

    if "receiveAssetIds" in fields:
        result["receiveAssetIds"] = (
            asset_ids
        )

    if "sendAmount" in fields:
        result["sendAmount"] = {
            "amount": str(amount),
            "currency": "EUR",
        }

    if "receiverSlug" in fields:
        result["receiverSlug"] = receiver

    if "settlementCurrencies" in fields:
        result["settlementCurrencies"] = [
            "EUR"
        ]

    if "clientMutationId" in fields:
        result["clientMutationId"] = str(
            uuid.uuid4()
        )

    return result


# ============================================================
# COUNTER OFFER
# ============================================================

def counter_offer(offer, cards):
    sender = (
        offer.get("sender") or {}
    )

    receiver = str(
        sender.get("slug") or ""
    ).strip()

    asset_ids = [
        str(c.get("assetId")).strip()
        for c in cards
        if c.get("assetId")
    ]

    if not receiver:
        print(
            "❌ receiverSlug mancante",
            flush=True,
        )

        return False

    if not asset_ids:
        print(
            "❌ Nessuna carta da ricevere",
            flush=True,
        )

        return False

    amount = (
        len(asset_ids)
        * PAY_PER_CARD
    )

    print(
        f"🟢 Controproposta: "
        f"{len(asset_ids)} carta/e → "
        f"€{amount / 100:.2f}",
        flush=True,
    )

    print(
        f"👤 Receiver: {receiver}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON viene ceduto",
        flush=True,
    )

    if DRY_RUN:
        print(
            "🟡 DRY RUN: "
            "controproposta simulata",
            flush=True,
        )

        return True

    if not STARK:
        print(
            "❌ SORARE_STARK_PRIVATE_KEY "
            "mancante",
            flush=True,
        )

        return False

    # ========================================================
    # PREPARE
    # ========================================================

    try:
        prepare_input = (
            build_prepare_offer_input(
                asset_ids,
                receiver,
                amount,
            )
        )

    except Exception as e:
        print(
            f"❌ Costruzione "
            f"prepareOfferInput: {e}",
            flush=True,
        )

        return False

    print(
        "📦 prepareOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            prepare_input,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

    data = graphql("""
        mutation PrepareOffer(
            $input: prepareOfferInput!
        ) {
            prepareOffer(input: $input) {
                authorizations {
                    fingerprint

                    request {
                        __typename

                        ... on
                        StarkexTransferAuthorizationRequest {
                            amount
                            condition
                            expirationTimestamp
                            nonce
                            receiverPublicKey
                            receiverVaultId
                            senderVaultId
                            token

                            feeInfoUser {
                                feeLimit
                                sourceVaultId
                                tokenId
                            }
                        }

                        ... on
                        StarkexLimitOrderAuthorizationRequest {
                            vaultIdSell
                            vaultIdBuy
                            amountSell
                            amountBuy
                            tokenSell
                            tokenBuy
                            nonce
                            expirationTimestamp

                            feeInfo {
                                feeLimit
                                tokenId
                                sourceVaultId
                            }
                        }

                        ... on
                        MangopayWalletTransferAuthorizationRequest {
                            nonce
                            amount
                            currency
                            operationHash
                            mangopayWalletId
                        }
                    }
                }

                errors {
                    message
                }
            }
        }
    """, {
        "input": prepare_input,
    })

    if not data:
        print(
            "❌ Nessuna risposta "
            "da prepareOffer",
            flush=True,
        )

        return False

    if data.get("errors"):
        print(
            "❌ prepareOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("prepareOffer")
    )

    if not result:
        print(
            "❌ prepareOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:
        for e in errors:
            print(
                f"❌ prepareOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True,
            )

        return False

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:
        print(
            "❌ Nessuna autorizzazione "
            "restituita",
            flush=True,
        )

        return False

    print(
        f"🔐 Autorizzazioni ricevute: "
        f"{len(authorizations)}",
        flush=True,
    )

    # ========================================================
    # SIGN
    # ========================================================

    try:
        approvals = sign_authorizations(
            authorizations
        )

    except Exception as e:
        print(
            f"❌ Firma: {e}",
            flush=True,
        )

        return False

    print(
        f"✍️ Autorizzazioni firmate: "
        f"{len(approvals)}",
        flush=True,
    )

    # ========================================================
    # CREATE
    # ========================================================

    try:
        create_input = (
            build_create_direct_offer_input(
                asset_ids,
                receiver,
                amount,
                approvals,
            )
        )

    except Exception as e:
        print(
            f"❌ Costruzione "
            f"createDirectOfferInput: {e}",
            flush=True,
        )

        return False

    debug = dict(create_input)

    if "approvals" in debug:
        debug["approvals"] = (
            f"{len(approvals)} authorization(s)"
        )

    print(
        "📦 createDirectOfferInput:",
        flush=True,
    )

    print(
        json.dumps(
            debug,
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )

    data = graphql("""
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {
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
    """, {
        "input": create_input,
    })

    if not data:
        print(
            "❌ Nessuna risposta "
            "da createDirectOffer",
            flush=True,
        )

        return False

    if data.get("errors"):
        print(
            "❌ createDirectOffer "
            "GRAPHQL GLOBAL ERROR:",
            flush=True,
        )

        for e in data["errors"]:
            print(
                json.dumps(
                    e,
                    ensure_ascii=False,
                ),
                flush=True,
            )

        return False

    result = (
        (data.get("data") or {})
        .get("createDirectOffer")
    )

    if not result:
        print(
            "❌ createDirectOffer "
            "ha restituito NULL",
            flush=True,
        )

        return False

    errors = (
        result.get("errors")
        or []
    )

    if errors:
        for e in errors:
            print(
                f"❌ createDirectOffer: "
                f"{e.get('message', 'Errore')}",
                flush=True,
            )

        return False

    token_offer = (
        result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:
        print(
            "❌ Nessuna offerta creata "
            "da Sorare",
            flush=True,
        )

        return False

    print(
        "=" * 40,
        flush=True,
    )

    print(
        f"✅ CONTROPROPOSTA INVIATA: "
        f"{offer_id}",
        flush=True,
    )

    print(
        f"💰 €{amount / 100:.2f} "
        f"({len(asset_ids)} × €0,20)",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON ceduto",
        flush=True,
    )

    print(
        "=" * 40,
        flush=True,
    )

    return True


# ============================================================
# PROCESS OFFER
# ============================================================

def process_offer(offer):
    offer_id = str(
        offer.get("id") or ""
    ).strip()

    if not offer_id:
        return

    with state_lock:
        if offer_id in processed:
            return

        processed.add(offer_id)

    print(
        "\n" + "=" * 40,
        flush=True,
    )

    print(
        f"📨 OFFERTA {offer_id}",
        flush=True,
    )

    sender_cards = (
        (offer.get("senderSide") or {})
        .get("anyCards")
        or []
    )

    receiver_cards = (
        (offer.get("receiverSide") or {})
        .get("anyCards")
        or []
    )

    if not any(
        is_kulenovic(c)
        for c in receiver_cards
    ):
        print(
            "⏭️ Kulenovic non presente: "
            "ignoro",
            flush=True,
        )

        return

    print(
        "🎯 Kulenovic trovato",
        flush=True,
    )

    ids = [
        c.get("assetId")
        for c in sender_cards
        if c.get("assetId")
    ]

    if not ids:
        print(
            "❌ Nessuna carta offerta",
            flush=True,
        )

        return

    cards = card_details(ids)

    if len(cards) != len(ids):
        print(
            "❌ Impossibile verificare "
            "tutte le carte",
            flush=True,
        )

        return

    print(
        f"🔎 Controllo {len(cards)} carta/e",
        flush=True,
    )

    valid_cards = []
    unknown_price = False

    for card in cards:
        try:
            result = valid_card(
                card
            )

            # ------------------------------------------------
            # None = floor non trovato.
            #
            # IMPORTANTISSIMO:
            # non è una carta non idonea.
            # È una carta che non possiamo ancora classificare.
            # ------------------------------------------------

            if result is None:
                unknown_price = True

                print(
                    "      🟡 Carta NON "
                    "classificata come non idonea",
                    flush=True,
                )

                print(
                    "      🟡 Floor mancante: "
                    "nessuna azione automatica",
                    flush=True,
                )

                continue

            if result:
                valid_cards.append(
                    card
                )

        except Exception as e:
            print(
                f"❌ Errore controllo carta: "
                f"{e}",
                flush=True,
            )

            # Un errore inatteso nel controllo prezzo
            # viene trattato in sicurezza come PRICE UNKNOWN.
            unknown_price = True

    print(
        f"📊 Carte valide: "
        f"{len(valid_cards)}/{len(cards)}",
        flush=True,
    )

    # ========================================================
    # PRICE UNKNOWN
    #
    # NON RIFIUTARE.
    # NON CONTROPROPORRE.
    # LASCIARE PENDING.
    # ========================================================

    if unknown_price:
        print(
            "⚠️ ALMENO UNA CARTA "
            "NON HA UN FLOOR DISPONIBILE.",
            flush=True,
        )

        print(
            "🟡 NESSUNA AZIONE AUTOMATICA.",
            flush=True,
        )

        print(
            "🟡 OFFERTA LASCIATA "
            "IN SOSPESO.",
            flush=True,
        )

        print(
            "🛑 Niente rifiuto.",
            flush=True,
        )

        print(
            "🛑 Niente controproposta.",
            flush=True,
        )

        print(
            "🔁 Potrà essere riprocessata "
            "quando il prezzo sarà disponibile.",
            flush=True,
        )

        return

    # ========================================================
    # NESSUNA CARTA VALIDA
    # ========================================================

    if not valid_cards:
        print(
            "❌ Nessuna carta idonea.",
            flush=True,
        )

        print(
            "🔴 Rifiuto dell'offerta.",
            flush=True,
        )

        reject_offer(offer)

        return

    # ========================================================
    # CONTROPROPOSTA
    # ========================================================

    rejected = (
        len(cards)
        - len(valid_cards)
    )

    if rejected:
        print(
            f"⚠️ {rejected} carta/e "
            f"esclusa/e",
            flush=True,
        )

    if counter_offer(
        offer,
        valid_cards,
    ):
        print(
            "🟢 Controproposta "
            "completata con successo.",
            flush=True,
        )

        if not reject_offer(offer):
            print(
                "⚠️ Impossibile rifiutare "
                "l'offerta originale",
                flush=True,
            )

    else:
        print(
            "🔴 Controproposta NON creata.",
            flush=True,
        )

        print(
            "🟡 Offerta originale lasciata "
            "IN SOSPESO per nuovo tentativo.",
            flush=True,
        )


# ============================================================
# WORKER
# ============================================================

def worker():
    print(
        "🤖 BOT AVVIATO",
        flush=True,
    )

    print(
        "📦 VERSIONE BOT: 16.3",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{PAY_PER_CARD / 100:.2f} per carta",
        flush=True,
    )

    print(
        f"📊 Range floor: "
        f"€{MIN_PRICE / 100:.2f} - "
        f"€{MAX_PRICE / 100:.2f}",
        flush=True,
    )

    print(
        f"🎂 Età massima: "
        f"meno di {MAX_AGE} anni",
        flush=True,
    )

    print(
        "🏆 COMPETIZIONI: TUTTE le "
        "activeCompetitions Sorare",
        flush=True,
    )

    print(
        "🔧 PREPARE: schema LIVE + "
        "settlementCurrencies EUR",
        flush=True,
    )

    print(
        "🔧 CREATE: createDirectOffer",
        flush=True,
    )

    print(
        "🚫 PREPARE: nessun "
        "exchangeRateId forzato",
        flush=True,
    )

    print(
        "🚫 PREPARE: type inviato solo "
        "se presente nello schema LIVE",
        flush=True,
    )

    print(
        "💰 PREZZI: LIVE "
        "tokens.liveSingleSaleOffers",
        flush=True,
    )

    print(
        "✅ FLOOR: giocatore + rarità + "
        "stagione",
        flush=True,
    )

    print(
        "🚫 latestEnglishAuction: "
        "NON UTILIZZATO",
        flush=True,
    )

    print(
        "🚫 lowestPriceCard: "
        "NON UTILIZZATO",
        flush=True,
    )

    print(
        "🚫 lowestPriceCardAnySeason: "
        "NON UTILIZZATO",
        flush=True,
    )

    print(
        "🚫 tokenPrices: "
        "NON UTILIZZATO",
        flush=True,
    )

    print(
        "💱 WEI→EUR: cambio Sorare live "
        "solo se necessario",
        flush=True,
    )

    print(
        "🛡️ PRICE UNKNOWN: "
        "OFFERTA LASCIATA IN SOSPESO",
        flush=True,
    )

    print(
        "🧪 DRY_RUN=" + str(DRY_RUN),
        flush=True,
    )

    if not check_account():
        print(
            "❌ Account non valido. "
            "Worker fermato.",
            flush=True,
        )

        return

    inspect_live_schema()

    while True:
        try:
            offers = get_offers()

            print(
                f"📨 Offerte pendenti: "
                f"{len(offers)}",
                flush=True,
            )

            for offer in offers:
                try:
                    process_offer(
                        offer
                    )

                except Exception as e:
                    print(
                        f"❌ Errore offerta: "
                        f"{e}",
                        flush=True,
                    )

            time.sleep(
                INTERVAL
            )

        except Exception as e:
            print(
                f"❌ Worker: {e}",
                flush=True,
            )

            time.sleep(
                INTERVAL
            )


def start_worker():
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        _worker_started = True

        threading.Thread(
            target=worker,
            name="sorare-worker",
            daemon=True,
        ).start()

        print(
            "✅ Thread Sorare avviato.",
            flush=True,
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "sorare",
        "version": "16.3",
        "dry_run": DRY_RUN,
        "pay_per_card_cents": PAY_PER_CARD,
        "interval_seconds": INTERVAL,
        "min_price_cents": MIN_PRICE,
        "max_price_cents": MAX_PRICE,
        "max_age": MAX_AGE,

        "competition_mode":
            "ALL_ACTIVE_SORARE_COMPETITIONS",

        "prepare_mode":
            "LIVE_SCHEMA_AWARE",

        "settlement_currency": "EUR",

        "exchange_rate_mode":
            "ONLY_FOR_WEI_PRICE_CONVERSION",

        "price_mode":
            "LIVE_SINGLE_SALE_OFFERS",

        "price_matching":
            "PLAYER_RARITY_SEASON",

        "latest_english_auction_fallback":
            False,

        "lowest_price_card":
            False,

        "lowest_price_card_any_season":
            False,

        "token_prices":
            False,

        "wei_to_eur_fallback":
            True,

        "price_unknown_action":
            "LEAVE_OFFER_PENDING",
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "16.3",
        "price_mode":
            "LIVE_SINGLE_SALE_OFFERS",
        "price_unknown_action":
            "LEAVE_OFFER_PENDING",
    })


if __name__ == "__main__":
    start_worker()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
        ),
    )
