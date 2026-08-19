import os
import time
import threading
import requests
from decimal import Decimal
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

SORARE_API_URL = "https://api.sorare.com/graphql"
SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN", "").strip()
SORARE_JWT_AUD = os.getenv("SORARE_JWT_AUD", "").strip()
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "").strip()
SORARE_STARK_PRIVATE_KEY = os.getenv("SORARE_STARK_PRIVATE_KEY", "").strip()

# SICUREZZA: NON MODIFICARE
DRY_RUN = True

PREZZO_MINIMO_CENTESIMI = 30
PREZZO_MASSIMO_CENTESIMI = 80
PAGAMENTO_PER_CARTA_CENTESIMI = 20
INTERVALLO_CONTROLLO = 10
TIMEOUT_HTTP = 30

KULENOVIC_SLUG = "sandro-kulenovic-2025-limited-385"
KULENOVIC_ASSET_ID = (
    "0x0400756aff980aff1d36e274f1c38af4ac587bd3d40c713"
    "6796b6c0ed10ba0a6"
)

# ============================================================
# CAMPIONATI
# ============================================================

CAMPIONATI_COPERTI = {
    "english-league": {
        "nome": "English League",
        "alias": {"english-league", "premier-league-eng", "premier-league"},
    },
    "ligue-1-fr": {
        "nome": "Ligue 1",
        "alias": {"ligue-1-fr", "ligue-1"},
    },
    "laliga-es": {
        "nome": "LALIGA EA SPORTS",
        "alias": {"laliga-es", "laliga", "la-liga", "laliga-ea-sports"},
    },
    "bundesliga-de": {
        "nome": "Bundesliga",
        "alias": {"bundesliga-de", "bundesliga"},
    },
    "liga-portugal": {
        "nome": "Liga Portugal",
        "alias": {"liga-portugal", "primeira-liga-pt", "liga-portugal-pt"},
    },
    "eredivisie-nl": {
        "nome": "Eredivisie",
        "alias": {"eredivisie-nl", "eredivisie"},
    },
    "jupiler-pro-league-be": {
        "nome": "Jupiler Pro League",
        "alias": {"jupiler-pro-league-be", "jupiler-pro-league"},
    },
    "scottish-premiership-sco": {
        "nome": "Scottish Premiership",
        "alias": {"scottish-premiership-sco", "scottish-premiership"},
    },
    "jleague-jp": {
        "nome": "J.League",
        "alias": {"jleague-jp", "j1-league-jp", "j-league", "j1-league"},
    },
    "second-division-eng": {
        "nome": "Seconda divisione inglese",
        "alias": {
            "second-division-eng",
            "championship-eng",
            "english-championship",
            "championship",
        },
    },
    "austrian-bundesliga-at": {
        "nome": "Austrian Bundesliga",
        "alias": {
            "austrian-bundesliga-at",
            "austrian-bundesliga",
            "bundesliga-at",
        },
    },
    "croatian-hnl-hr": {
        "nome": "Croatian HNL",
        "alias": {
            "croatian-hnl-hr",
            "croatian-first-league-hr",
            "croatian-first-league",
            "croatian-hnl",
            "supersport-hnl",
        },
    },
    "2-bundesliga-de": {
        "nome": "2. Bundesliga",
        "alias": {"2-bundesliga-de", "2-bundesliga"},
    },
    "ligue-2-fr": {
        "nome": "Ligue 2",
        "alias": {"ligue-2-fr", "ligue-2"},
    },
    "mls-us": {
        "nome": "MLS",
        "alias": {
            "mls-us",
            "major-league-soccer-us",
            "major-league-soccer",
            "mls",
        },
    },
    "k-league-1-kr": {
        "nome": "K League",
        "alias": {"k-league-1-kr", "k-league-1", "k-league"},
    },
    "super-lig-tr": {
        "nome": "Turchia",
        "alias": {"super-lig-tr", "super-lig", "turkish-super-lig"},
    },
    "superliga-dk": {
        "nome": "Danimarca",
        "alias": {"superliga-dk", "superliga", "danish-superliga"},
    },
    "serie-a-it": {
        "nome": "Serie A",
        "alias": {"serie-a-it", "serie-a"},
    },
    "brasileirao-serie-a-br": {
        "nome": "Brasile",
        "alias": {
            "brasileirao-serie-a-br",
            "brasileirao-serie-a",
            "brasileirao",
            "serie-a-br",
        },
    },
    "premier-liga-ru": {
        "nome": "Russia",
        "alias": {
            "premier-liga-ru",
            "russian-premier-league",
            "premier-liga",
            "russia-premier-league",
        },
    },
    "serie-b-it": {
        "nome": "Serie B",
        "alias": {"serie-b-it", "serie-b"},
    },
    "liga-1-peru": {
        "nome": "Perù",
        "alias": {
            "liga-1-peru",
            "liga-1-pe",
            "peruvian-primera-division",
        },
    },
    "primera-a-colombia": {
        "nome": "Colombia",
        "alias": {
            "primera-a-colombia",
            "liga-betplay-col",
            "primera-a",
            "liga-betplay",
        },
    },
    "liga-mx": {
        "nome": "Messico",
        "alias": {"liga-mx"},
    },
    "laliga-2-es": {
        "nome": "LALIGA 2",
        "alias": {
            "laliga-2-es",
            "laliga-hypermotion",
            "laliga-2",
            "segunda-division-spain",
        },
    },
}

NUMERO_CAMPIONATI = len(CAMPIONATI_COPERTI)

# Precalcoliamo gli alias una sola volta
ALIAS_CAMPIONATI = {}

for dati in CAMPIONATI_COPERTI.values():
    for alias in dati["alias"]:
        ALIAS_CAMPIONATI[
            alias.strip().lower().replace("_", "-").replace(" ", "-")
        ] = dati["nome"]

# ============================================================
# STATO
# ============================================================

offerte_gia_analizzate = set()
offerte_in_elaborazione = set()
stato_lock = threading.Lock()
monitoraggio_avviato = False
monitor_thread = None

# ============================================================
# UTILITY
# ============================================================

def normalizza(valore):
    if valore is None:
        return ""
    return str(valore).strip().lower().replace("_", "-").replace(" ", "-")


def trova_campionato(slug):
    return ALIAS_CAMPIONATI.get(normalizza(slug))


def stampa_campionati():
    print("🏆 REGOLA CAMPIONATI:")
    print(f"   {NUMERO_CAMPIONATI} campionati coperti.")
    for dati in CAMPIONATI_COPERTI.values():
        print(f"   • {dati['nome']}")


# ============================================================
# CONFIGURAZIONE
# ============================================================

def verifica_configurazione():
    print("\n========================================")
    print("🔧 VERIFICA CONFIGURAZIONE")
    print("========================================")

    configurazioni = {
        "SORARE_JWT_TOKEN": SORARE_TOKEN,
        "SORARE_JWT_AUD": SORARE_JWT_AUD,
        "KULENOVIC_ID": KULENOVIC_ID,
        "SORARE_STARK_PRIVATE_KEY": SORARE_STARK_PRIVATE_KEY,
    }

    tutto_ok = True

    for nome, valore in configurazioni.items():
        if valore:
            print(f"✅ {nome} presente.")
        else:
            print(f"❌ {nome} NON presente.")
            tutto_ok = False

    print(f"🟡 DRY_RUN = {DRY_RUN}")
    print("========================================")
    return tutto_ok


def test_firma_stark():
    print("\n========================================")
    print("🔐 VERIFICA CHIAVE STARK")
    print("========================================")

    if not SORARE_STARK_PRIVATE_KEY:
        print("❌ SORARE_STARK_PRIVATE_KEY non configurata.")
        return False

    chiave = SORARE_STARK_PRIVATE_KEY.strip()
    chiave_hex = chiave[2:] if chiave.lower().startswith("0x") else chiave

    try:
        valore = int(chiave_hex, 16)
        if valore <= 0:
            raise ValueError
    except ValueError:
        print("❌ Chiave Stark NON valida.")
        return False

    print("✅ SORARE_STARK_PRIVATE_KEY presente.")
    print("✅ Formato esadecimale verificato.")
    print("🟡 Test crittografico locale saltato.")
    print("🟡 Nessun modulo starknet-py interno viene importato.")
    print("🟢 Il controllo della chiave non blocca il monitoraggio.")
    print("🟡 DRY RUN: nessuna operazione reale.")
    print("========================================")
    return True


# ============================================================
# HTTP / GRAPHQL
# ============================================================

def crea_headers():
    if not SORARE_TOKEN:
        raise RuntimeError("SORARE_JWT_TOKEN non configurato.")

    token = SORARE_TOKEN
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": token,
        "User-Agent": "Sorare-DryRun-Bot/1.0",
    }

    if SORARE_JWT_AUD:
        headers["JWT-AUD"] = SORARE_JWT_AUD

    return headers


def esegui_query(query, variables=None, tentativi=3):
    payload = {"query": query, "variables": variables or {}}
    ultimo_errore = None

    for tentativo in range(1, tentativi + 1):
        try:
            response = requests.post(
                SORARE_API_URL,
                json=payload,
                headers=crea_headers(),
                timeout=TIMEOUT_HTTP,
            )

            print(f"🌐 Sorare HTTP: {response.status_code}")

            if response.status_code == 429:
                print("⚠️ Rate limit Sorare.")
                if tentativo < tentativi:
                    time.sleep(tentativo * 3)
                    continue
                return None

            if response.status_code != 200:
                print("❌ Risposta HTTP non valida:")
                print(response.text[:1000])
                ultimo_errore = f"HTTP {response.status_code}"

                if tentativo < tentativi:
                    time.sleep(tentativo)
                    continue
                return None

            try:
                risultato = response.json()
            except ValueError:
                print("❌ Risposta Sorare non JSON.")
                return None

            errori = risultato.get("errors")
            if errori:
                print("❌ Errori GraphQL:")
                for errore in errori:
                    if isinstance(errore, dict):
                        print(f"- {errore.get('message', 'Errore sconosciuto')}")
                    else:
                        print(f"- {errore}")
                return None

            return risultato

        except requests.RequestException as e:
            ultimo_errore = e
            print(
                f"⚠️ Errore HTTP "
                f"(tentativo {tentativo}/{tentativi}): {e}"
            )
            if tentativo < tentativi:
                time.sleep(tentativo)

        except Exception as e:
            print(f"❌ Errore richiesta Sorare: {e}")
            return None

    print(f"❌ Richiesta fallita: {ultimo_errore}")
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

    user = risultato.get("data", {}).get("currentUser")

    if not user:
        print("❌ Sorare non ha restituito currentUser.")
        return False

    print("\n========================================")
    print("✅ AUTENTICAZIONE SORARE RIUSCITA")
    print(f"👤 Manager: {user.get('nickname') or 'N/D'}")
    print(f"🔗 Slug: {user.get('slug') or 'N/D'}")
    print("========================================\n")
    return True


# ============================================================
# OFFERTE
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
        return None

    user = risultato.get("data", {}).get("currentUser")
    if not user:
        print("❌ currentUser assente.")
        return None

    nodes = (
        user.get("pendingTokenOffersReceived") or {}
    ).get("nodes")

    if nodes is None:
        return []

    return nodes if isinstance(nodes, list) else None


# ============================================================
# DETTAGLI CARTE
# ============================================================

def recupera_dettagli_carte(asset_ids):
    asset_ids = list(dict.fromkeys(
        str(x).strip() for x in asset_ids if x
    ))

    if not asset_ids:
        return None

    query = """
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

    risultato = esegui_query(query, {"assetIds": asset_ids})

    if not risultato:
        return None

    carte = risultato.get("data", {}).get("anyCards")

    return carte if isinstance(carte, list) else []


# ============================================================
# PREZZI
# ============================================================

def eur_cents(amounts):
    if not isinstance(amounts, dict):
        return None

    valore = amounts.get("eurCents")

    try:
        valore = int(valore)
    except (ValueError, TypeError):
        return None

    return valore if valore > 0 else None


def prezzo_live(carta):
    if not isinstance(carta, dict):
        return None

    offerta = carta.get("liveSingleSaleOffer") or {}
    receiver = offerta.get("receiverSide") or {}
    cents = eur_cents(receiver.get("amounts") or {})

    return Decimal(cents) / Decimal(100) if cents else None


def prezzo_public(carta):
    if not isinstance(carta, dict):
        return None

    valore = carta.get("publicMinPrices")

    if isinstance(valore, dict):
        cents = eur_cents(valore)

    elif isinstance(valore, list):
        prezzi = [
            eur_cents(x)
            for x in valore
            if isinstance(x, dict)
        ]
        prezzi = [x for x in prezzi if x is not None]
        cents = min(prezzi) if prezzi else None

    else:
        cents = None

    return Decimal(cents) / Decimal(100) if cents else None


def recupera_prezzo_floor(carta):
    slug = str(carta.get("slug") or "").strip()

    if not slug:
        print("      ⚠️ Slug carta assente.")
        return None

    print(f"      🔎 Ricerca prezzo floor: {slug}")

    valori = []

    for chiave in (
        "lowestPriceCard",
        "lowestPriceCardAnySeason",
    ):
        lowest = carta.get(chiave) or {}

        if lowest.get("slug"):
            print(
                f"      🎯 Carta floor trovata: "
                f"{lowest['slug']}"
            )

        live = prezzo_live(lowest)
        public = prezzo_public(lowest)

        if live is not None:
            valori.append(live)
            print(f"      💰 Offerta vendita: €{live:.2f}")

        if public is not None:
            valori.append(public)
            print(f"      💰 Public min price: €{public:.2f}")

        if valori:
            break

    # Fallback carta originale
    if not valori:
        live = prezzo_live(carta)
        public = prezzo_public(carta)

        if live is not None:
            valori.append(live)
            print(f"      💰 Offerta carta: €{live:.2f}")

        if public is not None:
            valori.append(public)
            print(f"      💰 Public min carta: €{public:.2f}")

    if not valori:
        print("      ⚠️ Prezzo non disponibile.")
        return None

    floor = min(valori)
    print(f"      ✅ FLOOR VERIFICATO: €{floor:.2f}")
    return floor


# ============================================================
# SQUADRA / CAMPIONATO
# ============================================================

def controlla_squadra_e_campionato(carta):
    player = carta.get("anyPlayer") or {}

    nome = (
        player.get("displayName")
        or player.get("slug")
        or carta.get("name")
        or "Giocatore sconosciuto"
    )

    active_club = player.get("activeClub")

    # REGOLA FONDAMENTALE:
    # SENZA SQUADRA = CARTA NON IDONEA
    if not isinstance(active_club, dict):
        print("      🏟️ Squadra attiva: NESSUNA")
        print(f"      👤 Giocatore: {nome}")
        print("      🔴 GIOCATORE SENZA SQUADRA")
        print("      🔴 CAMPIONATO NON VALIDO")
        return False

    club_name = (
        active_club.get("name")
        or active_club.get("slug")
        or "N/D"
    )

    print(f"      🏟️ Squadra attiva: {club_name}")

    competizioni = active_club.get("activeCompetitions") or []

    if not isinstance(competizioni, list) or not competizioni:
        print("      🔴 Nessuna competizione attiva.")
        print("      🔴 CAMPIONATO NON COPERTO")
        return False

    trovati = []

    for competizione in competizioni:
        if not isinstance(competizione, dict):
            continue

        slug = normalizza(competizione.get("slug"))
        if not slug:
            continue

        print(f"         • {slug}")

        nome_campionato = trova_campionato(slug)
        if nome_campionato:
            trovati.append(nome_campionato)

    if trovati:
        print("      🟢 CAMPIONATO COPERTO")
        for nome_campionato in dict.fromkeys(trovati):
            print(f"         🟢 {nome_campionato}")
        return True

    print("      🔴 CAMPIONATO NON COPERTO")
    return False


# ============================================================
# ANALISI CARTA
# ============================================================

def analizza_carta(carta):
    nome = (
        carta.get("name")
        or carta.get("slug")
        or "Carta sconosciuta"
    )

    asset_id = carta.get("assetId")
    slug = carta.get("slug")
    rarita = str(carta.get("rarityTyped") or "").upper().strip()

    prezzo = recupera_prezzo_floor(carta)

    prezzo_ok = (
        prezzo is not None
        and Decimal(PREZZO_MINIMO_CENTESIMI) / 100
        <= prezzo
        <= Decimal(PREZZO_MASSIMO_CENTESIMI) / 100
    )

    rarita_ok = rarita == "LIMITED"
    campionato_ok = controlla_squadra_e_campionato(carta)

    idonea = prezzo_ok and rarita_ok and campionato_ok

    print("")
    print(f"   📄 {nome}")
    print(f"      Asset ID: {asset_id or 'N/D'}")
    print(f"      Slug: {slug or 'N/D'}")
    print(f"      Rarità: {rarita or 'N/D'}")

    if prezzo is None:
        print("      🔴 Prezzo NON verificabile")
    elif prezzo < Decimal("0.30"):
        print(f"      🔴 Prezzo inferiore: €{prezzo:.2f}")
    elif prezzo > Decimal("0.80"):
        print(f"      🔴 Prezzo superiore: €{prezzo:.2f}")
    else:
        print(f"      🟢 Prezzo tra €0,30 e €0,80")

    print(
        "      🟢 Rarità LIMITED"
        if rarita_ok
        else "      🔴 Rarità NON valida"
    )

    print("      🟢 CARTA IDONEA" if idonea else "      ❌ CARTA NON IDONEA")

    return idonea


# ============================================================
# KULENOVIC
# ============================================================

def controlla_kulenovic(carte):
    print("\n🔎 CARTA/E RICHIESTA/E DAL MANAGER:")

    configurato = KULENOVIC_ID.lower().strip()
    trovato = False

    for carta in carte:
        asset_id = str(carta.get("assetId") or "").strip()
        slug = str(carta.get("slug") or "").strip()
        collection = str(carta.get("collection") or "").strip()

        print(f"   Asset ID: {asset_id}")
        print(f"   Slug: {slug}")
        print(f"   Collection: {collection}")

        if (
            (configurato and (
                asset_id.lower() == configurato
                or slug.lower() == configurato
            ))
            or slug.lower() == KULENOVIC_SLUG.lower()
            or asset_id.lower() == KULENOVIC_ASSET_ID.lower()
        ):
            trovato = True
            print("   🎯 KULENOVIC RICONOSCIUTO")

    if trovato:
        print("🎯 KULENOVIC RICONOSCIUTO!")
    else:
        print("ℹ️ Kulenovic non riconosciuto nell'offerta.")

    return trovato


# ============================================================
# ELABORAZIONE OFFERTA
# ============================================================

def elabora_offerta(offerta):
    if not isinstance(offerta, dict):
        return False

    offerta_id = str(offerta.get("id") or "").strip()
    if not offerta_id:
        return False

    with stato_lock:
        if offerta_id in offerte_gia_analizzate:
            return True

        if offerta_id in offerte_in_elaborazione:
            return True

        offerte_in_elaborazione.add(offerta_id)

    completata = False

    try:
        print("\n========================================")
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
        carte_richieste = receiver_side.get("anyCards") or []

        print(f"📦 Carte offerte: {len(carte_offerte)}")
        print(f"📦 Carte richieste: {len(carte_richieste)}")

        # Deve essere richiesto Kulenovic
        if not controlla_kulenovic(carte_richieste):
            print("\n🔴 DECISIONE SIMULATA: RIFIUTARE L'OFFERTA")
            print("   Motivo: Kulenovic non è richiesto.")
            print("🟡 DRY RUN: nessun rifiuto eseguito.")
            completata = True
            return True

        asset_ids = [
            str(c.get("assetId")).strip()
            for c in carte_offerte
            if isinstance(c, dict) and c.get("assetId")
        ]

        if not asset_ids:
            print("\n🔴 DECISIONE SIMULATA: RIFIUTARE L'OFFERTA")
            print("   Motivo: nessuna carta ricevuta.")
            print("🟡 DRY RUN: nessun rifiuto eseguito.")
            completata = True
            return True

        dettagli = recupera_dettagli_carte(asset_ids)

        if dettagli is None:
            print("⚠️ Impossibile recuperare i dettagli.")
            return False

        if not dettagli:
            print("⚠️ Nessun dettaglio carta restituito.")
            return False

        print("\n🔎 ANALISI DELLE CARTE RICEVUTE:")

        carte_idonee = []

        for carta in dettagli:
            if analizza_carta(carta):
                carte_idonee.append(carta)

        totale = len(asset_ids)
        idonee = len(carte_idonee)
        non_idonee = max(0, totale - idonee)

        print("\n----------------------------------------")
        print(f"📊 CARTE TOTALI: {totale}")
        print(f"📊 CARTE IDONEE: {idonee}")
        print(f"📊 CARTE NON IDONEE: {non_idonee}")

        # NESSUNA CARTA IDONEA = RIFIUTO
        if idonee == 0:
            print("\n🔴 DECISIONE SIMULATA: RIFIUTARE L'OFFERTA")
            print("   Motivo: nessuna carta ricevuta è idonea.")
            print("🟡 DRY RUN: nessun rifiuto eseguito.")
            print("----------------------------------------")
            completata = True
            return True

        pagamento = (
            Decimal(idonee * PAGAMENTO_PER_CARTA_CENTESIMI)
            / Decimal("100")
        )

        print("\n🟢 DECISIONE SIMULATA: CONTROPROPOSTA")

        print("\n🗑️ CARTE NON IDONEE:")
        print(f"   ❌ {non_idonee} carta/e" if non_idonee else "   Nessuna")

        print("\n📥 CARTE IDONEE:")
        for carta in carte_idonee:
            print(
                f"   ✅ {carta.get('name') or carta.get('slug') or 'Carta sconosciuta'}"
            )

        print(f"\n💰 PAGAMENTO SIMULATO: €{pagamento:.2f}")
        print(f"   {idonee} × €0,20")

        print("\n📋 CONTROPROPOSTA SIMULATA:")
        print("   ❌ Noi NON cediamo Kulenovic")

        for carta in carte_idonee:
            print(
                f"   ✅ Noi riceviamo: "
                f"{carta.get('name') or carta.get('slug') or 'Carta sconosciuta'}"
            )

        print(f"   💰 Noi pagheremmo: €{pagamento:.2f}")
        print("\n🟡 DRY RUN ATTIVO:")
        print("   Nessuna controproposta è stata inviata.")
        print("----------------------------------------")

        completata = True
        return True

    except Exception as e:
        print(f"❌ Errore elaborazione offerta {offerta_id}: {e}")
        return False

    finally:
        with stato_lock:
            offerte_in_elaborazione.discard(offerta_id)

            if completata:
                offerte_gia_analizzate.add(offerta_id)


# ============================================================
# MONITOR
# ============================================================

def monitor_offerte():
    print("\n🤖 BOT SORARE AVVIATO")
    print("🟡 MODALITÀ DRY RUN ATTIVA")
    print("⚠️ Nessun rifiuto e nessuna controproposta verranno eseguiti.")
    print(f"💰 REGOLA PREZZO: €0,30 - €0,80")
    print(f"💰 PAGAMENTO: €0,20 per ogni carta idonea")

    stampa_campionati()
    verifica_configurazione()
    test_firma_stark()

    if not verifica_account():
        print("❌ Impossibile autenticarsi a Sorare.")
        print("❌ Monitoraggio terminato.")
        return

    print("🟢 MONITORAGGIO OFFERTE ATTIVO.\n")

    while True:
        try:
            print("🔎 Controllo offerte...")
            offerte = recupera_offerte()

            if offerte is None:
                print("⚠️ Controllo offerte fallito.")
            else:
                print(f"📨 Offerte pending ricevute: {len(offerte)}")

                for offerta in offerte:
                    elabora_offerta(offerta)

        except Exception as e:
            print(f"⚠️ Errore nel ciclo: {e}")

        time.sleep(INTERVALLO_CONTROLLO)


# ============================================================
# AVVIO
# ============================================================

def avvia_monitoraggio():
    global monitoraggio_avviato, monitor_thread

    with stato_lock:
        if monitoraggio_avviato:
            return False

        monitoraggio_avviato = True

        monitor_thread = threading.Thread(
            target=monitor_offerte,
            name="sorare-monitor",
            daemon=True,
        )

        monitor_thread.start()
        return True


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():
    if avvia_monitoraggio():
        return "Bot Sorare avviato in modalità DRY RUN.", 200

    return "Bot Sorare già attivo.", 200


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "sorare",
        "dry_run": DRY_RUN,
        "monitoraggio_avviato": monitoraggio_avviato,
    })


# ============================================================
# START LOCALE
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    avvia_monitoraggio()

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
