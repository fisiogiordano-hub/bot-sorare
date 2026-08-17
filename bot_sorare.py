import os
import queue
import threading
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

offerta_queue = queue.Queue()

SORARE_JWT_TOKEN = os.getenv("SORARE_JWT_TOKEN", "")
KULENOVIC_CARD_ID = os.getenv("KULENOVIC_CARD_ID", "")
SORARE_API_URL = "https://api.sorare.com/graphql"

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
        print(f"❌ Eccezione durante la richiesta HTTP a Sorare: {e}")
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data:
        print(f"📥 Nuova notifica webhook ricevuta e messa in coda.")
        offerta_queue.put(data)
        return jsonify({"status": "success", "message": "Offerta in coda"}), 200
    return jsonify({"status": "error", "message": "Payload vuoto"}), 400

def processatore_offerte():
    while True:
        offerta = offerta_queue.get()
        try:
            print(f"🔄 Elaborazione offerta in corso...")
            
            offerta_id = offerta.get("payload", {}).get("id") or offerta.get("id")
            if not offerta_id:
                print("⚠️ Impossibile trovare l'ID dell'offerta.")
                continue

            query_dettagli = """
                Query GetOfferDetails($offerId: String!) {
                    offer(id: $offerId) {
                        id
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
                        outgoingCards {
                            id
                        }
                    }
                }
            """
            
            risultato_dettagli = esegui_query_sorare(query_dettagli, {"offerId": offerta_id})
            if not risultato_dettagli:
                print("❌ Impossibile recuperare i dettagli dell'offerta.")
                continue

            offer_data = risultato_dettagli.get("data", {}).get("offer", {})
            
            outgoing_cards = offer_data.get("outgoingCards", [])
            kulu_richiesto = any(card.get("id") == KULENOVIC_CARD_ID for card in outgoing_cards)

            if not kulu_richiesto:
                print("⏭️ Offerta ignorata: manca la richiesta di Kulenovic (nessun segnale).")
                continue

            print("🎯 Segnale Kulenovic rilevato! Analizzo le carte in arrivo...")

            incoming_cards = offer_data.get("incomingCards", [])
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
                print("🚫 Nessuna carta idonea trovata nell'offerta. Rifiuto in corso...")
                mutazione_reject = """
                    mutation RejectOffer($input: RejectOfferInput!) {
                        rejectOffer(input: $input) {
                            offer { id status }
                            errors { message }
                        }
                    }
                """
                risultato = esegui_query_sorare(mutazione_reject, {"input": {"offerId": offerta_id}})
                print(f"Risultato rifiuto: {risultato}")
            else:
                num_carte = len(carte_idonee_ids)
                totale_euro = num_carte * 0.20
                print(f"✅ Trovate {num_carte} carte idonee! Invio controproposta: tengo solo le valide, tolgo Kulenovic e offro {totale_euro}€ ({num_carte} x 0.20€).")
                
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
                risultato = esegui_query_sorare(mutazione_counter, variables)
                print(f"Risultato controproposta: {risultato}")

            time.sleep(1)

        except Exception as e:
            print(f"❌ Errore critico nel processamento dell'offerta: {e}")
        finally:
            offerta_queue.task_done()

threading.Thread(target=processatore_offerte, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
