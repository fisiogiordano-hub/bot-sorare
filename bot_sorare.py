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
    Processa le offerte in arrivo: verifica la presenza di Kulenovic,
    valuta le carte dell'altro manager, rifiuta se non idonee o 
    invia controproposta togliendo Kulenovic e offrendo 0,20€ per carta.
    """
    while True:
        offerta = offerta_queue.get()
        if offerta is None:
            break
        
        try:
            print(f"[LOG] Elaborazione offerta in corso: {offerta}")
            
            offerta_id = offerta.get("id")
            
            if not offerta_id:
                print("[ERRORE] ID offerta non trovato nel payload del webhook.")
                continue

            # Query GraphQL estesa per leggere sia il lato nostro (viewerSide) che quello dell'offerente (senderSide)
            # Nota: la struttura dei campi offerta/senderSide/viewerSide riflette lo schema standard Sorare GraphQL
            query_dettagli = """
            query($id: ID!) {
              offer(id: $id) {
                id
                viewerSide {
                  assets {
                    __typename
                    ... on Card {
                      slug
                      name
                      rarity
                    }
                  }
                }
                senderSide {
                  assets {
                    __typename
                    ... on Card {
                      slug
                      name
                      rarity
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
            
            # Variabili di controllo
            contiene_kulenovic = False
            carte_validita_ok = True
            numero_carte_da_comprare = 0
            
            if risultato and "data" in risultato and risultato["data"]["offer"]:
                offer_data = risultato["data"]["offer"]
                
                # 1. Controlla se nel nostro lato (viewerSide) è presente Kulenovic
                viewer_assets = offer_data.get("viewerSide", {}).get("assets", [])
                for asset in viewer_assets:
                    if asset.get("__typename") == "Card":
                        nome_carta = asset.get("name", "")
                        if "Kulenovic" in nome_carta:
                            contiene_kulenovic = True
                
                # 2. Se non c'è Kulenovic, ignoriamo completamente l'offerta
                if not contiene_kulenovic:
                    print(f"[LOG] Offerta ignorata: non è indirizzata alla carta di Kulenovic.")
                    continue
                
                print(f"[LOG] Offerta intercettata su Kulenovic! Analizzo le carte dell'altro manager...")
                
                # 3. Analizziamo le carte offerte dall'altra parte (senderSide)
                sender_assets = offer_data.get("senderSide", {}).get("assets", [])
                for asset in sender_assets:
                    if asset.get("__typename") == "Card":
                        is_locked = asset.get("isLocked", False)
                        restrizioni = asset.get("restrictions", [])
                        
                        # Se la carta ha blocchi o restrizioni (es. X rossa/blu), non è idonea
                        if is_locked or len(restrizioni) > 0:
                            carte_validita_ok = False
                        else:
                            numero_carte_da_comprare += 1

            # --- DECISIONE FINALE ---
            if not carte_validita_ok or numero_carte_da_comprare == 0:
                print(f"[LOG] Offerta non idonea (Carte valide: {numero_carte_da_comprare}, Restrizioni ok: {carte_validita_ok}). Rifiuto immediato...")
                
                # MUTAZIONE PER RIFIUTARE L'OFFERTA (rejectOffer)
                mutation_reject = """
                mutation($input: RejectOfferInput!) {
                  rejectOffer(input: $input) {
                    errors {
                      message
                    }
                  }
                }
                """
                res_reject = esegui_query_graphql(mutation_reject, {"input": {"offerId": offerta_id}})
                print(f"[LOG] Risultato rifiuto: {res_reject}")
                
            else:
                prezzo_totale = numero_carte_da_comprare * 0.20
                print(f"[LOG] Offerta idonea! Trovate {numero_carte_da_comprare} carte valide. Invio controproposta a {prezzo_totale}€ (rimuovendo Kulenovic)...")
                
                # NOTA: Per fare la controproposta (counterOffer) su Sorare, 
                # dovrai strutturare il payload con le carte dell'altro utente e l'importo in denaro (wei/EUR), 
                # assicurandoti di NON includere la tua carta di Kulenovic.
                
                # Esempio di struttura mutazione counterOffer (da adattare al tuo schema esatto di mutazione):
                mutation_counter = """
                mutation($input: CounterOfferInput!) {
                  counterOffer(input: $input) {
                    errors {
                      message
                    }
                  }
                }
                """
                # Qui andranno passati gli ID delle carte dell'offerta e l'offerta in denaro
                # input_data = { "offerId": offerta_id, "amount": prezzo_totale, ... }
                # res_counter = esegui_query_graphql(mutation_counter, {"input": input_data})
                
            # Pausa di sicurezza per evitare il Rate Limit di Sorare
            time.sleep(2)
            print(f"[LOG] Elaborazione completata.")
            
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
        
        # Risponde subito a Sorare confermando la ricezione
        return jsonify({"status": "successo", "messaggio": "Offerta messa in coda"}), 200

    except Exception as e:
        print(f"[ERRORE WEBHOOK]: {e}")
        return jsonify({"status": "errore", "dettaglio": str(e)}), 500

# Funzione principale per avviare l'app web e il server
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
