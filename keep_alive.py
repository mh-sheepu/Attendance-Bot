# keep_alive.py
from flask import Flask
import threading
import time

app = Flask("")

start_time = time.time()  # Record when server starts

@app.route("/")
def home():
    return "Bot is alive!"

@app.route("/status")
def status():
    from datetime import timedelta

    uptime_seconds = int(time.time() - start_time)
    uptime_str = str(timedelta(seconds=uptime_seconds))

    return (
        f"<h2>Bot Status</h2>"
        f"<p>Uptime: {uptime_str}</p>"
        f"<p>Check <a href='/'>/</a> to see alive status</p>"
    )

def run():
    app.run(host="0.0.0.0", port=8080)

# Run in a separate thread so your bot.py can run at the same time
def keep_alive():
    t = threading.Thread(target=run)
    t.start()
