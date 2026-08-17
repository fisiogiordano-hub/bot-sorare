import time
import threading
import os
import requests
from queue import Queue
from flask import Flask, request, jsonify

# Inizializzazione Flask per tenere sveglio il bot su Render
app = Flask(__name__)

# Configurazione API Sorare GraphQL
SORARE_API_URL = "https://api.sorare.com/graphql"
# Prende il token dalle variabili d'ambiente di Render
SORARE_TOKEN = os.getenv("SORARE_JWT_TOKEN")

@app.route('/')
def home():
    return "La sentinella di San Drino Kulenovic è attiva e vigile!"

# Coda per gestire le offerte in ordine cronologico
offerta_queue = Queue()

def esegui_query_sorare(query, variables=None):
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

def processatore_offerte():
    while True:
        offerta = offerta_queue.get()
        try:
            print(f"Elaborazione offerta in corso: {offerta}")
            
            # Estraiamo l'ID dell'offerta dal payload del webhook
            # (Adattato sullo standard comune dei webhook di Sorare)
            offerta_id = offerta.get("payload", {}).get("id") or offerta.get("id")
            
            if not offerta_id:
                print("⚠️ Impossibile trovare l'ID dell'offerta nel payload ricevuto.")
                continue

            print(f"Tentativo di accettare l'offerta ID: {offerta_id}")

            # Mutazione GraphQL di Sorare per accettare un'offerta
            # (Nota: assicurati che il nome della mutazione corrisponda allo schema attuale di Sorare)
            mutazione_accetta = """
                mutation AcceptOffer($input: AcceptOfferInput!) {
                    acceptOffer(input: $input) {
                        offer {
                            id
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
                    "offerId": offerta_id
                }
            }

            risultato = esegui_query_sorare(mutazione_accetta, variables)
            
            if risultato:
                print(f"Risposta da Sorare per l'offerta {offerta_id}: {risultato}")
            else:
                print(f"❌ Fallito l'invio della mutazione per l'offerta {offerta_id}")

            time.sleep(1)
        except Exception as e:
            print(f"Errore durante l'elaborazione dell'offerta: {e}")
        finally:
            offerta_queue.task_done()

# Avvia il worker in background per la coda
threading.Thread(target=processatore_offerte, daemon=True).start()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data:
        print(f"Ricevuta nuova notifica webhook: {data}")
        offerta_queue.put(data)
        return jsonify({"status": "success", "message": "Offerta ricevuta e messa in coda"}), 200
    return jsonify({"status": "error", "message": "Payload vuoto"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
