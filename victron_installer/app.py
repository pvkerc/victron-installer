"""Ingress-only web UI. No open host port and no unattended installation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from engine import GitHub, Installer, Refused, Supervisor

class App:
    def __init__(self, installer):
        self.installer = installer
        self.csrf = secrets.token_urlsafe(32)
        self.busy = False
        self.error = None
        self.gate = threading.Lock()

    def status(self):
        # Never expose local filesystem manifests or credentials to the page.
        p = self.installer.progress
        return {"busy": self.busy, "phase": p["phase"], "message": p["message"],
                "error": self.error, "csrf": self.csrf,
                "can_restore": bool(p.get("transaction")),
                "backup": p.get("supervisor_backup")}

    def run(self, operation, *args):
        with self.gate:
            if self.busy:
                raise Refused("Er loopt al een bewerking.")
            self.busy, self.error = True, None
        def worker():
            try:
                operation(*args)
            except Exception as e:
                self.error = str(e) if isinstance(e, Refused) else "Bewerking niet voltooid (" + type(e).__name__ + "). Controleer de status en bewaarde back-up."
            finally:
                self.busy = False
        threading.Thread(target=worker, daemon=True).start()

def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def allowed(self):
            # Trust identity headers only when the TCP peer IS the ingress proxy.
            return self.client_address[0] == "172.30.32.2" and bool(self.headers.get("X-Remote-User-Id"))

        def reply(self, code, data, kind="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"error": "Alleen beschikbaar via Home Assistant ingress."})
            if self.path == "/":
                return self.reply(200, Path(__file__).with_name("index.html").read_bytes(), "text/html")
            if self.path == "/status":
                return self.reply(200, app.status())
            return self.reply(404, {})

        def do_POST(self):
            if not self.allowed():
                return self.reply(403, {})
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), app.csrf):
                return self.reply(403, {})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096 or self.headers.get("Content-Type") != "application/json":
                    return self.reply(400, {})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    return self.reply(400, {})
                if self.path == "/prepare":
                    with app.gate:
                        if app.busy:
                            raise Refused("Er loopt al een bewerking.")
                        result = app.installer.prepare()
                    return self.reply(200, result)
                if self.path == "/install":
                    with app.gate:
                        if app.busy:
                            raise Refused("Er loopt al een bewerking.")
                        plan = app.installer.approve(body.get("approval"))
                    app.run(app.installer.install, plan)
                    return self.reply(202, {})
                if self.path == "/restore":
                    if body.get("confirm") != "HERSTEL_VORIGE_VERSIE":
                        return self.reply(400, {})
                    app.run(app.installer.restore)
                    return self.reply(202, {})
                return self.reply(404, {})
            except Refused as e:
                return self.reply(409, {"error": str(e)})
            except (ValueError, TypeError):
                return self.reply(400, {})
            except Exception:
                return self.reply(500, {"error": "Controle niet voltooid. Probeer opnieuw."})

        def log_message(self, format, *args):
            # Ingress URLs and secrets do not belong in request logs.
            pass
    return Handler

def main():
    options = json.loads(Path("/data/options.json").read_text())
    installer = Installer("/homeassistant", "/data", Supervisor(os.environ["SUPERVISOR_TOKEN"]),
                          GitHub(options.get("github_token", "")))
    server = ThreadingHTTPServer(("0.0.0.0", 8099), handler_for(App(installer)))
    server.serve_forever()

if __name__ == "__main__":
    main()
