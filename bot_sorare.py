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
