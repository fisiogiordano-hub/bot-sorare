import os
import time
import threading
import requests
from queue import Queue
from flask import Flask, request, jsonify

# Inizializzazione Flask
app = Flask(__name__)

# Configurazione API Sorare GraphQL e Token JWT
SORARE_API_URL = "https://api.sorare.com/graphql"
SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN")

def esegui_query_sorare(query, variables=None):
    if not SORARE_TOKEN:
        print("⚠️ ERRORE: SORARE_JWT_TOKEN non configurato su Render!")
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SORARE_TOKEN}"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    
    try:
        response = requests.post(SORARE_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Errore API Sorare: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Eccezione durante la richiesta GraphQL: {e}")
        return None

@app.route('/')
def home():
    return "La sentinella di San Drino Kulenovic è attiva con coda di sicurezza anti-sovraccarico!"

# Coda protettiva per le offerte
offerta_queue = Queue()

def processatore_offerte():
    while True:
        offerta = offerta_queue.get()
        try:
            print(f"🔄 Prelievo offerta dalla coda in corso...")
            
            # Estraiamo l'ID dell'offerta dal webhook
            offerta_id = offerta.get("payload", {}).get("id") or offerta.get("id")
            if not offerta_id:
                print("⚠️ Impossibile trovare l'ID dell'offerta.")
                continue

            # Query per i dettagli dell'offerta
            query_dettagli = """
                Query GetOfferDetails($offerId: String!) {
                    offer(id: $offerId) {
                        id
                        incomingCards {
                            id
                            rarity
                            name
                            erc721Token {
                                price
                            }
                            singleCardStats {
                                hasRedCard
                                hasYellowCard
                            }
                        }
                    }
                }
            """
            
            risultato_dettagli = esegui_query_sorare(query_dettagli, {"offerId": offerta_id})
            
            if not risultato_dettagli:
                print("❌ Impossibile recuperare i dettagli dell'offerta.")
                continue

            offer_data = risultato_dettagli.get("data", {}).get("offer", {})
            incoming_cards = offer_data.get("incomingCards", [])

            # Valutiamo l'idoneità (Limited, max 0.50€, niente restrizioni/X)
            offerta_idonea = True
            carte_idonee_ids = []

            if not incoming_cards:
                offerta_idonea = False
            
            for card in incoming_cards:
                card_id = card.get("id")
                rarita = card.get("rarity")
                token_info = card.get("erc721Token") or {}
                prezzo = token_info.get("price") or 0.0
                stats = card.get("singleCardStats") or {}
                has_red = stats.get("hasRedCard", False)
                
                if rarita != "limited" or prezzo > 0.50 or has_red:
                    offerta_idonea = False
                    break
                
                carte_idonee_ids.append(card_id)

            # Azione: Rifiuto se non idonea, Controproposta se idonea
            if not offerta_idonea:
                print("🚫 Offerta NON idonea. Rifiuto in corso...")
                mutazione = """
                    mutation RejectOffer($input: RejectOfferInput!) {
                        rejectOffer(input: $input) {
                            offer { id status }
                            errors { message }
                        }
                    }
                """
                risultato_azione = esegui_query_sorare(mutazione, {"input": {"offerId": offerta_id}})
                print(f"Risposta rifiuto: {risultato_azione}")
            else:
                num_carte = len(carte_idonee_ids)
                totale_euro = num_carte * 0.20
                print(f"✅ Offerta IDONEA! Invio controproposta: offro {totale_euro}€ ({num_carte} carte x 0.20€) rimuovendo Kulenovic.")
                
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
                    }
                }
                risultato_azione = esegui_query_sorare(mutazione_counter, variables)
                print(f"Risposta controproposta: {risultato_azione}")

            # ⏱️ Pausa di sicurezza di 3 secondi tra un'offerta e l'altra per evitare blocchi da parte di Sorare
            print("⏳ Pausa di sicurezza di 3 secondi...")
            time.sleep(3)

        except Exception as e:
            print(f"Errore durante l'elaborazione dell'offerta in coda: {e}")
        finally:
            offerta_queue.task_done()

# Avvia il worker in background
threading.Thread(target=processatore_offerte, daemon=True).start()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data:
        print(f"📥 Ricevuta nuova notifica webhook, messa in coda di sicurezza.")
        offerta_queue.put(data)
        return jsonify({"status": "success", "message": "Offerta messa in coda"}), 200
    return jsonify({"status": "error", "message": "Payload vuoto"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
