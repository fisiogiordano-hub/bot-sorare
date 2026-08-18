import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

SORARE_JWT_TOKEN = os.getenv("SORARE_JWT_TOKEN", "")
SORARE_API_URL = "https://api.sorare.com/graphql"

@app.route('/')
def home():
    return "Bot Sorare operativo e in ascolto!"

def esegui_query_sorare(query, variables=None):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SORARE_JWT_TOKEN}"
    }
    payload = {
        "query": query,
        "variables": variables or {}
    }
    try:
        response = requests.post(SORARE_API_URL, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Errore API Sorare ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"❌ Eccezione durante la richiesta HTTP: {e}")
        return None

def trova_id_kulenovic():
    print("🔍 Cerco la carta di Kulenovic nel tuo inventario...")
    query_inventario = """
        Query GetMyCards {
            viewer {
                cards(first: 50) {
                    nodes {
                        id
                        player {
                            displayName
                        }
                    }
                }
            }
        }
    """
    risultato = esegui_query_sorare(query_inventario)
    if not risultato:
        return None
    
    cards = risultato.get("data", {}).get("viewer", {}).get("cards", {}).get("nodes", [])
    for card in cards:
        player = card.get("player") or {}
        name = player.get("displayName", "")
        if "Kulenović" in name or "Kulenovic" in name:
            kulu_id = card.get("id")
            print(f"✅ Trovata carta di Kulenovic! ID ufficiale: {kulu_id}")
            return kulu_id
            
    print("⚠️ Attenzione: Nessuna carta di Kulenovic trovata nel tuo inventario.")
    return None

def controlla_offerte(kul_id):
    print("🔄 Controllo offerte in arrivo...")
    query_offerte = """
        Query GetReceivedOffers {
            viewer {
                receivedOffers(first: 10) {
                    nodes {
                        id
                        status
                        outgoingCards {
                            id
                        }
                        incomingCards {
                            id
                            rarity
                            erc721Token {
                                price
                            }
                            player {
                                activeClub {
                                    activeCompetitions {
                                        supported
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    """
    risultato = esegui_query_sorare(query_offerte)
    if not risultato:
        return

    offerte = risultato.get("data", {}).get("viewer", {}).get("receivedOffers", {}).get("nodes", [])
    
    for offerta in offerte:
        if offerta.get("status") != "pending":
            continue
            
        offerta_id = offerta.get("id")
        outgoing_cards = offerta.get("outgoingCards", [])
        
        kulu_richiesto = any(card.get("id") == kul_id for card in outgoing_cards)
        if not kulu_richiesto:
            continue

        print(f"🎯 Segnale Kulenovic rilevato nell'offerta {offerta_id}!")

        incoming_cards = offerta.get("incomingCards", [])
        carte_idonee_ids = []

        for card in incoming_cards:
            card_id = card.get("id")
            rarita = card.get("rarity")
            token_info = card.get("erc721Token") or {}
            prezzo = token_info.get("price") or 0.0
            
            player_info = card.get("player") or {}
            active_club = player_info.get("activeClub") or {}
            competitions = active_club.get("activeCompetitions") or []
            campionato_coperto = any(comp.get("supported", False) for comp in competitions) if competitions else False
            
            if rarita == "limited" and prezzo <= 0.50 and campionato_coperto:
                carte_idonee_ids.append(card_id)
            else:
                print(f"⚠️ Carta scartata (Rarità: {rarita}, Prezzo: {prezzo}€, Campionato coperto: {campionato_coperto})")

        if not carte_idonee_ids:
            print("🚫 Nessuna carta idonea. Rifiuto offerta...")
            mutazione_reject = """
                mutation RejectOffer($input: RejectOfferInput!) {
                    rejectOffer(input: $input) {
                        offer { id status }
                        errors { message }
                    }
                }
            """
            esegui_query_sorare(mutazione_reject, {"input": {"offerId": offerta_id}})
        else:
            num_carte = len(carte_idonee_ids)
            totale_euro = num_carte * 0.20
            print(f"✅ Trovate {num_carte} carte idonee! Invio controproposta con {totale_euro}€.")
            
            mutazione_counter = """
                mutation CounterOffer($input: CounterOfferInput!) {
                    counterOffer(input: $input) {
                        offer { id status }
                        errors { message }
                    }
                }
            """
            variables = {
                "input": {
                    "initialOfferId": offerta_id,
                    "recvCardIds": carte_idonee_ids,
                    "sendCardIds": [],
                    "sendAmount": {
                        "amount": str(totale_euro),
                        "currency": "EUR"
                    }
                }
            }
            esegui_query_sorare(mutazione_counter, variables)

def loop_background():
    kul_id = None
    while not kul_id:
        kul_id = trova_id_kulenovic()
        if not kul_id:
            time.sleep(15)

    while True:
        try:
            controlla_offerte(kul_id)
        except Exception as e:
            print(f"❌ Errore nel ciclo: {e}")
        time.sleep(30)

if __name__ == '__main__':
    # Avvia il controllo in background
    t = threading.Thread(target=loop_background)
    t.daemon = True
    t.start()
    
    # Avvia Flask per mantenere aperta la porta su Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
