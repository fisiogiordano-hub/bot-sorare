import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SORARE_JWT_TOKEN = os.getenv("SORARE_JWT_TOKEN", "")
KULENOVIC_ID = os.getenv("KULENOVIC_ID", "")
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
    print(f"⚡ Elaborazione istantanea offerta {offerta_id} per la carta ID: {kul_id}")
    
    # Query per analizzare i dettagli dell'offerta specifica
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
        print(f"✅ Trovate {num_carte} carte idonee! Invio controproposta immediata: tolgo Kulenovic e invio {totale_euro}€.")
        
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

@app.route('/', methods=['GET'])
def home():
    return "Bot Sorare Webhook operativo e in ascolto!"

@app.route('/webhook', methods=['POST'])
def webhook_offerta():
    if not KULENOVIC_ID:
        return jsonify({"status": "error", "message": "KULENOVIC_ID non configurato"}), 500
        
    dati = request.get_json(silent=True) or {}
    print(ricevuto := f"📥 Webhook ricevuto: {dati}")
    
    # Estrae l'ID dell'offerta dal payload del webhook (supportando vari formati standard)
    offerta_id = dati.get("offerId") or dati.get("id") or dati.get("data", {}).get("offerId")
    
    if offerta_id:
        # Esegue immediatamente l'elaborazione senza attese
        elabora_offerta_specifica(offerta_id, KULENOVIC_ID)
        return jsonify({"status": "success", "message": "Offerta elaborata istantaneamente"}), 200
    else:
        return jsonify({"status": "ignored", "message": "Nessun offerId valido nel payload"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
