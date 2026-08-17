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

def esegui_query_graphql(query, variables=None):
    """
    Funzione helper per inviare richieste GraphQL a Sorare usando il token di autenticazione.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SORARE_TOKEN}" if SORARE_TOKEN else ""
    }
    payload = {
        "query": query,
        "variables": variables or {}
    }
    
    try:
        response = requests.post(SORARE_API_URL, json=payload, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        print(f"[ERRORE API] Impossibile contattare Sorare: {e}")
        return None

def worker_offerte():
    """
    Processa le offerte in arrivo una alla volta, in modo ordinato,
    applicando i filtri, escludendo San Drino e rispondendo in pochi secondi.
    """
    while True:
        offerta = offerta_queue.get()
        if offerta is None:
            break
        
        try:
            print(f"[LOG] Elaborazione offerta in corso: {offerta}")
            
            # Estrae l'ID dell'offerta dal webhook ricevuto
            # (Nota: a seconda del payload del webhook di Sorare, l'id potrebbe trovarsi in percorsi diversi, es. offerta.get('id'))
            offerta_id = offerta.get("id")
            
            if not offerta_id:
                print("[ERRORE] ID offerta non trovato nel payload del webhook.")
                continue

            # --- LOGICA DEL BOT & FILTRI ---
            ha_san_drino = False
            ha_restrizioni = False  # Controllo per le carte con la X rossa e blu
            tutte_sotto_soglia = True
            
            # Esempio di query GraphQL per verificare i dettagli dell'offerta (da adattare al vostro schema esatto)
            query_dettagli = """
            query($id: ID!) {
              offer(id: $id) {
                viewerSide {
                  assets {
                    __typename
                    ... on Card {
                      slug
                      name
                      rarity
                      price
                      isLocked
                      restrictions {
                        id
                      }
                    }
                  }
                }
              }
            }
            """
            
            risultato = esegui_query_graphql(query_dettagli, {"id": offerta_id})
            
            if risultato and "data" in risultato and risultato["data"]["offer"]:
                assets = risultato["data"]["offer"]["viewerSide"]["assets"]
                
                for asset in assets:
                    if asset.get("__typename") == "Card":
                        nome_giocatore = asset.get("name", "")
                        prezzo = asset.get("price", 0) or 0
                        is_locked = asset.get("isLocked", False)
                        restrizioni = asset.get("restrictions", [])
                        
                        # 1. Esclude San Drino Kulenovic
                        if "San Drino Kulenovic" in nome_giocatore:
                            ha_san_drino = True
                        
                        # 2. Controlla restrizioni o blocchi (X rossa e blu / isLocked)
                        if is_locked or len(restrizioni) > 0:
                            ha_restrizioni = True
                            
                        # 3. Filtro prezzo (sotto i 0.50€)
                        if prezzo > 0.50:
                            tutte_sotto_soglia = False

            # Decisione finale basata sui filtri
            if ha_san_drino or ha_restrizioni or not tutte_sotto_soglia:
                print(f"[LOG] Offerta non idonea (San Drino: {ha_san_drino}, Restrizioni X: {ha_restrizioni}, Prezzo OK: {tutte_sotto_soglia}). Rifiuto in corso...")
                # TODO: Inserire qui la mutazione GraphQL per rifiutare l'offerta (rejectOffer)
            else:
                print(f"[LOG] Offerta idonea! Invio controproposta a 0,20€ per carta...")
                # TODO: Inserire qui la mutazione GraphQL per la controproposta (counterOffer)
            
            # Pausa di sicurezza per evitare il Rate Limit di Sorare
            time.sleep(2)
            
            print(f"[LOG] Offerta elaborata con successo!")
            
        except Exception as e:
            print(f"[ERRORE] Errore nell'elaborazione dell'offerta: {e}")
            
        finally:
            offerta_queue.task_done()

# Avvia il worker in background in un thread separato
t = threading.Thread(target=worker_offerte, daemon=True)
t.start()


# --- ROTTA WEBHOOK PER SORARE ---
@app.route('/webhook', methods=['POST'])
def webhook_sorare():
    """
    Riceve il segnale in tempo reale da Sorare quando arriva un'offerta.
    """
    try:
        dati_offerta = request.json
        
        if not dati_offerta:
            return jsonify({"status": "errore", "messaggio": "Nessun dato ricevuto"}), 400
        
        print(f"[WEBHOOK] Ricevuta nuova offerta in tempo reale!")
        
        # Mette l'offerta in coda per essere elaborata dal worker
        offerta_queue.put(dati_offerta)
        
        # Risponde subito a Sorare confermando la ricezione (evita timeout)
        return jsonify({"status": "successo", "messaggio": "Offerta messa in coda"}), 200

    except Exception as e:
        print(f"[ERRORE WEBHOOK]: {e}")
        return jsonify({"status": "errore", "dettaglio": str(e)}), 500


# Funzione principale per avviare l'app web e il server
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
