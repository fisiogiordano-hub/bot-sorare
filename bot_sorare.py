import os
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SORARE_JWT_TOKEN = os.getenv("SORARE_JWT_TOKEN", "")
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "")
SORARE_API_URL = "https://api.sorare.com/graphql"

offerte_gia_gestite = set()

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
        response = requests.post(SORARE_API_URL, json=payload, headers=headers, timeout=25)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Errore API Sorare ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"❌ Eccezione durante la richiesta HTTP: {e}")
        return None

def elabora_offerta_specifica(offerta_id, kul_id):
    print(f"⚡ Elaborazione offerta {offerta_id} per la carta ID: {kul_id}")
    
    query_dettaglio = """
        Query GetOfferDetails($id: ID!) {
            offer(id: $id) {
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
    """
    risultato = esegui_query_sorare(query_dettaglio, {"id": offerta_id})
    if not risultato:
        return

    offerta = risultato.get("data", {}).get("offer")
    if not offerta or offerta.get("status") != "pending":
        print("⚠️ L'offerta non è più pendente o non è valida.")
        return
        
    outgoing_cards = offerta.get("outgoingCards", [])
    kulu_richiesto = any(card.get("id") == kul_id for card in outgoing_cards)
    if not kulu_richiesto:
        print("⚠️ La carta di Kulenovic non è inclusa in questa offerta.")
        return

    print(f"🎯 Segnale Kulenovic confermato nell'offerta {offerta_id}!")

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
        
        # Condizione aggiornata: TUTTE le competizioni attive devono essere supportate
        campionato_coperto = all(comp.get("supported", False) for comp in competitions) if competitions else False
        
        if rarita == "limited" and prezzo <= 0.50 and campionato_coperto:
            carte_idonee_ids.append(card_id)
        else:
            print(f"⚠️ Carta scartata (Rarità: {rarita}, Prezzo: {prezzo}€, Tutte le competizioni coperte: {campionato_coperto})")

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
        print(f"✅ Trovate {num_carte} carte idonee con competizioni interamente coperte! Invio controproposta.")
        
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
        risposta_counter = esegui_query_sorare(mutazione_counter, variables)
        print(f"Risposta controproposta: {risposta_counter}")

def monitor_offerte():
    """Controlla le offerte pendenti regolarmente."""
    time.sleep(10)
    print("🔄 Monitoraggio offerte Sorare avviato correttamente...")
    
    query_offerte = """
        Query GetPendingOffers {
            viewer {
                receivedOffers(status: pending) {
                    nodes {
                        id
                    }
                }
            }
        }
    """
    
    while True:
        try:
            risultato = esegui_query_sorare(query_offerte)
            if risultato:
                offerte = risultato.get("data", {}).get("viewer", {}).get("receivedOffers", {}).get("nodes", [])
                for offerta in offerte:
                    offerta_id = offerta.get("id")
                    if offerta_id and offerta_id not in offerte_gia_gestite:
                        print(f"🔎 Nuova offerta rilevata: {offerta_id}")
                        offerte_gia_gestite.add(offerta_id)
                        threading.Thread(target=elabora_offerta_specifica, args=(offerta_id, KULENOVIC_ID)).start()
        except Exception as e:
            print(f"⚠️ Errore nel ciclo di controllo: {e}")
        
        time.sleep(15)

# Avvio automatico del thread di monitoraggio indipendente
t = threading.Thread(target=monitor_offerte, daemon=True)
t.start()

@app.route('/', methods=['GET'])
def home():
    return "Bot Sorare operativo e in esecuzione!"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
