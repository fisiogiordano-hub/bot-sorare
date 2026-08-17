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

# Credenziali prese dalle variabili d'ambiente di Render
SORARE_EMAIL = os.getenv("SORARE_EMAIL")
SORARE_PASSWORD = os.getenv("SORARE_PASSWORD")

# Variabile globale per salvare il token in memoria
jwt_token = None

def ottieni_token_jwt():
    """Effettua il login automatico su Sorare usando email e password per ottenere il JWT."""
    global jwt_token
    if not SORARE_EMAIL or not SORARE_PASSWORD:
        print("⚠️ ERRORE: SORARE_EMAIL o SORARE_PASSWORD non configurati nelle variabili d'ambiente!")
        return None

    query_signin = """
        mutation SignIn($input: SignInInput!) {
            signIn(input: $input) {
                currentUser {
                    jwtToken(duration: TWO_WEEKS) {
                        token
                        expiredAt
                    }
                }
                errors {
                    message
                }
            }
        }
    """
    
    variables = {
        "input": {
            "email": SORARE_EMAIL,
            "password": SORARE_PASSWORD
        }
    }

    try:
        print("Tentativo di accesso a Sorare in corso...")
        response = requests.post(SORARE_API_URL, json={"query": query_signin, "variables": variables}, timeout=10)
        data = response.json()
        
        errors = data.get("data", {}).get("signIn", {}).get("errors", [])
        if errors:
            print(f"❌ Errore durante il login su Sorare: {errors}")
            return None
            
        token = data.get("data", {}).get("signIn", {}).get("currentUser", {}).get("jwtToken", {}).get("token")
        if token:
            print("✅ Login riuscito! Token JWT ottenuto con successo.")
            jwt_token = token
            return token
        else:
            print(f"❌ Impossibile estrarre il token dalla risposta: {data}")
            return None
    except Exception as e:
        print(f"❌ Eccezione durante la richiesta di login: {e}")
        return None

# Eseguiamo il login immediatamente all'avvio del modulo (così Gunicorn lo legge subito)
ottieni_token_jwt()

def esegui_query_sorare(query, variables=None):
    global jwt_token
    if not jwt_token:
        ottieni_token_jwt()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}"
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
    return "La sentinella di San Drino Kulenovic è attiva e vigile!"

# Coda per gestire le offerte in ordine cronologico
offerta_queue = Queue()

def processatore_offerte():
    while True:
        offerta = offerta_queue.get()
        try:
            print(f"Elaborazione offerta in corso: {offerta}")
            
            offerta_id = offerta.get("payload", {}).get("id") or offerta.get("id")
            
            if not offerta_id:
                print("⚠️ Impossibile trovare l'ID dell'offerta nel payload ricevuto.")
                continue

            print(f"Tentativo di accettare l'offerta ID: {offerta_id}")

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
