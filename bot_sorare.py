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

# Qui sotto andranno le funzioni di controllo e gestione delle offerte
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
            # Qui inseriremo la logica di accettazione/rifiuto tramite GraphQL
            time.sleep(1)
        except Exception as e:
            print(f"Errore durante l'elaborazione dell'offerta: {e}")
        finally:
            offerta_queue.task_done()

# Avvia il worker in background per la coda
threading.Thread(target=processatore_offerte, daemon=True).start()
