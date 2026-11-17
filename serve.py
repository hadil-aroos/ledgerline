"""Production server for the team (Waitress, multi-threaded).

  python serve.py                 -> listens on all network interfaces, port 8080
  set PORT=9000 & python serve.py -> another port

Other PCs on the same network open  http://<this-computer's-IP>:8080
"""
import os
import socket

from waitress import serve

from app import app

port = int(os.environ.get("PORT", 8080))
host = os.environ.get("HOST", "0.0.0.0")
try:
    ip = socket.gethostbyname(socket.gethostname())
except OSError:
    ip = "this-computer"
print(f"Ledgerline running. On this PC: http://127.0.0.1:{port}   Other PCs: http://{ip}:{port}")
serve(app, host=host, port=port, threads=8)
