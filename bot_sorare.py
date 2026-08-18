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
    print(f"⚡ [INIZIO] Elaborazione offerta {offerta_id}")
    print(f"🔑 [DEBUG] ID Kulenovic usato dal bot in questo momento: '{kul_id}'")
    
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
        print(f"⚠️ Impossibile ottenere i dettagli per l'offerta {offerta_id}")
        return

    offerta = risultato.get("data", {}).get("offer")
    if not offerta:
        print(f"⚠️ L'offerta {offerta_id} non esiste o non è stata trovata.")
        return
        
    print(f"📄 Stato offerta corrente: {offerta.get('status')}")
    
    outgoing_cards = offerta.get("outgoingCards", [])
    print(f"📤 Carte in uscita dall'offerta: {[c.get('id') for c in outgoing_cards]}")
    
    kulu_richiesto = any(card.get("id") == kul_id for card in outgoing_cards)
    if not kulu_richiesto:
        print(f"⚠️ ATTENZIONE: La carta di Kulenovic ({kul_id}) NON corrisponde a nessuna delle carte in uscita di questa offerta!")
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
        
        campionato_coperto = all(comp.get("supported", False) for comp in competitions) if competitions else False
        
        if rarita == "limited" and prezzo <= 0.50 and campionato_coperto:
            carte_idonee_ids.append(card_id)
            print(f"✅ Carta idonea trovata: {card_id} (Prezzo: {prezzo}€)")
        else:
            print(f"⚠️ Carta scartata (ID: {card_id}, Rarità: {rarita}, Prezzo: {prezzo}€, Tutte le competizioni coperte: {campionato_coperto})")

    if not carte_idonee_ids:
        print(f"🚫 Nessuna carta idonea nell'offerta {offerta_id}. Rifiuto offerta...")
        mutazione_reject = """
            mutation RejectOffer($input: RejectOfferInput!) {
                rejectOffer(input: $input) {
                    offer { id status }
                    errors { message }
                }
            }
        """
        res_rej = esegui_query_sorare(mutazione_reject, {"input": {"offerId": offerta_id}})
        print(f"Risposta rifiuto: {res_rej}")
    else:
        num_carte = len(carte_idonee_ids)
        totale_euro = num_carte * 0.20
        print(f"✅ Trovate {num_carte} carte idonee! Invio controproposta rimuovendo Kulenovic e offrendo {totale_euro}€.")
        
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
    time.sleep(5)
    print("🔄 [DEBUG] Avvio ciclo di monitoraggio offerte Sorare...")
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
                viewer = risultato.get("data", {}).get("viewer")
                if viewer:
                    offerte = viewer.get("receivedOffers", {}).get("nodes", [])
                    for offerta in offerte:
                        offerta_id = offerta.get("id")
                        if offerta_id and offerta_id not in offerte_gia_gestite:
                            print(f"🔎 Nuova offerta rilevata: {offerta_id}")
                            offerte_gia_gestite.add(offerta_id)
                            threading.Thread(target=elabora_offerta_specifica, args=(offerta_id, KULENOVIC_ID)).start()
        except Exception as e:
            print(f"⚠️ Errore critico nel ciclo di monitoraggio: {e}")
        time.sleep(15)

t = threading.Thread(target=monitor_offerte, daemon=True)
t.start()

@app.route('/', methods=['GET'])
def home():
    return "Bot Sorare operativo e in esecuzione!"

@app.route('/test/<offerta_id>', methods=['GET'])
def test_offerta(offerta_id):
    print(f"🧪 Test manuale avviato via web per l'offerta: {offerta_id}")
    threading.Thread(target=elabora_offerta_specifica, args=(offerta_id, KULENOVIC_ID)).start()
    return jsonify({"status": "Test avviato", "offerta_id": offerta_id, "kul_id_usato": KULENOVIC_ID})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
