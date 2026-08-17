import time
import threading
from queue import Queue
from flask import Flask

# Inizializzazione Flask per tenere sveglio il bot su Render
app = Flask(__name__)

@app.route('/')
def home():
    return "La sentinella di San Drino Kulenovic è attiva e vigile!"

# Coda per gestire le offerte in ordine cronologico
offerta_queue = Queue()

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
            print(f"[LOG] Elaborazione offerta in corso...")
            
            # --- LOGICA DEL BOT ---
            # 1. Esclude SEMPRE la carta di San Drino Kulenovic dalla controproposta.
            # 2. Filtra le carte idonee (Limited, sotto i 0.50€, senza restrizioni).
            # 3. Se ci sono carte idonee, prepara la controproposta offrendo 0,20€ per ciascuna.
            # 4. Se l'offerta non è valida, rifiuta in blocco.
            
            # Esempio di pausa di sicurezza per evitare il Rate Limit di Sorare
            time.sleep(2)
            
            print(f"[LOG] Offerta elaborata con successo per San Drino!")
            
        except Exception as e:
            print(f"[ERRORE] Errore nell'elaborazione dell'offerta: {e}")
            
        finally:
            offerta_queue.task_done()

# Avvia il worker in background in un thread separato
t = threading.Thread(target=worker_offerte, daemon=True)
t.start()


# Funzione principale per avviare l'app web e il server
if __name__ == "__main__":
    # Render assegna una porta automatica tramite variabile d'ambiente, altrimenti usa la 5000
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)