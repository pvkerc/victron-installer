"""Version-bound installer. Never calls a Home Assistant control service."""
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request
import zipfile

DOMAIN = "victron_economische_diagnose"
REPO = "pvkerc/victron-updates"
VERSION = re.compile(r"0\.[0-9]+\.[0-9]+(?:rc[0-9]+)?")
TERMINAL = {"installed", "restored", "aborted"}
MAX_PACKAGE = 2 * 1024 * 1024

class Refused(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("Onverwachte omleiding bij ophalen; er is niets geinstalleerd.")

def request_json(url, token, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json",
        "Content-Type": "application/json", "User-Agent": "Victron-Installer/0.1"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            raw = r.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise Refused("Antwoord is te groot.")
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        # Do not include request headers, tokens, response bodies or private URLs.
        raise Refused("Verbinding of antwoord niet bevestigd (" + type(e).__name__ + ").") from None

class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        if not self.token:
            raise Refused("Vul eerst een GitHub-token met alleen leestoegang in bij de appconfiguratie.")
        return request_json("https://api.github.com/repos/" + REPO + path, self.token)

    def contents(self, path, commit):
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise Refused("Ongeldige bronversie.")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
            raise Refused("Ongeldig pakketpad.")
        r = self.get("/contents/" + path + "?ref=" + commit)
        if r.get("encoding") != "base64" or not isinstance(r.get("content"), str):
            raise Refused("Bestand ontbreekt of is te groot.")
        return base64.b64decode(r["content"].replace("\n", ""), validate=True)

    def proposal(self):
        commit = self.get("/commits/main")["sha"]
        runs = self.get("/actions/runs?head_sha=" + commit + "&event=push&per_page=100")
        matches = [r for r in runs.get("workflow_runs", [])
                   if r.get("path") == ".github/workflows/verify.yml"
                   and r.get("head_sha") == commit and r.get("head_branch") == "main"]
        latest = max(matches, key=lambda r: r["id"], default={})
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise Refused("De controles van deze GitHub-versie zijn nog niet geslaagd.")
        catalog = json.loads(self.contents("updates/catalog.json", commit))
        if catalog.get("schema") != 1:
            raise Refused("Onbekende pakketlijst.")
        item = catalog["current"]
        validate_item(item)
        return dict(item, commit=commit)

def validate_item(item):
    if not isinstance(item, dict) or not VERSION.fullmatch(str(item.get("version", ""))):
        raise Refused("Ongeldige pakketversie.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
        raise Refused("Controlesom ontbreekt.")
    if item.get("path") != "packages/victron-" + item["version"] + ".zip":
        raise Refused("Onverwacht pakketpad.")
    if (not isinstance(item.get("from_versions"), list) or not item["from_versions"]
            or not all(VERSION.fullmatch(str(v)) for v in item["from_versions"])):
        raise Refused("Ondersteunde vorige versies ontbreken.")

class Supervisor:
    def __init__(self, token):
        self.token = token

    def call(self, path, body=None, timeout=30):
        r = request_json("http://supervisor" + path, self.token, body, timeout)
        if path.startswith("/core/api/"):
            return r
        if r.get("result") != "ok":
            raise Refused("Supervisor heeft de opdracht niet bevestigd.")
        return r.get("data", {})

    def state(self, entity):
        return self.call("/core/api/states/" + entity)

    def stop(self):
        # Synchronous stop: a successful response confirms Core has stopped.
        self.call("/core/stop", {}, timeout=180)

    def start(self):
        self.call("/core/start", {}, timeout=600)

    def check(self):
        self.call("/core/check", {}, timeout=300)

    def backup(self, version):
        r = self.call("/backups/new/partial", {
            "name": "Voor Victron " + version, "homeassistant": True,
            "homeassistant_exclude_database": False, "location": ".local",
            "background": False}, timeout=900)
        slug = r.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise Refused("Geen voltooide Home Assistant-back-up bevestigd.")
        info = self.call("/backups/" + slug + "/info")
        if not info.get("homeassistant"):
            raise Refused("De back-up bevat Home Assistant niet.")
        return slug

def current_version(path):
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))["version"]

def digest_tree(path):
    if path.is_symlink() or not path.is_dir():
        raise Refused("Integratiemap ontbreekt of is een koppeling.")
    result = {}
    for f in sorted(path.rglob("*")):
        if f.is_symlink():
            raise Refused("Koppeling in integratiemap.")
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
            result[f.relative_to(path).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    if "manifest.json" not in result:
        raise Refused("Manifest ontbreekt.")
    return result

def numeric(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        n = float(value)
        if not math.isfinite(n):
            raise ValueError()
        return n
    except (ValueError, TypeError):
        raise Refused("Laadlimiet is niet bevestigd.") from None

def safe_state(api):
    def state(entity):
        r = api.state(entity)
        if r.get("entity_id") != entity:
            raise Refused("Onverwacht statusantwoord.")
        return r
    if state("input_boolean.victron_laadstop")["state"] != "on":
        raise Refused("Zet Victron laadstop aan.")
    if state("switch.ess_force_charge")["state"] != "off":
        raise Refused("Force Charge is niet bevestigd uit.")
    if state("input_boolean.victron_nacht_laadlimiet_actief")["state"] != "off":
        raise Refused("Nachteigenaarschap is niet vrijgegeven.")
    limit = numeric(state("number.gx_device_ess_max_charge_current")["state"])
    original = numeric(state("input_number.victron_nacht_originele_laadstroomlimiet")["state"])
    if abs(limit - original) > 0.001:
        raise Refused("De oorspronkelijke laadlimiet is niet hersteld.")
    report = state("sensor.victron_b_regelstatus")
    attrs = report.get("attributes", {})
    # Require a new report after requesting a fresh control cycle, not old sensor data.
    from datetime import datetime, timezone
    reported = report.get("last_reported", report.get("last_updated", ""))
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(reported)).total_seconds()
    except (ValueError, TypeError):
        raise Refused("Actualiteit van de regelstatus is niet bevestigd.") from None
    if not 0 <= age <= 90:
        raise Refused("Wacht op een verse regelstatus.")
    if attrs.get("ingeschakeld") is not False:
        raise Refused("Stop eerst profiel B met de bestaande actie Stop bescherming.")
    p1 = attrs.get("p1_bescherming", {})
    if (p1.get("toestemming_actief") is not False
            or p1.get("actieve_ingreep") is not False
            or p1.get("schrijver_gereserveerd") is not False
            or "lease" not in p1 or p1["lease"] is not None
            or "schrijfopdracht_onbevestigd" not in p1
            or p1["schrijfopdracht_onbevestigd"] is not None):
        raise Refused("Stop P1 gecontroleerd en handel open herstel af.")
    recovery = attrs.get("ketenherstel", {})
    if (recovery.get("onbevestigd") is not False
            or recovery.get("wachtende_verzoeken") != 0):
        raise Refused("Ketenherstel is nog niet bevestigd.")
    return attrs.get("versie")

def unpack_package(data, item, dest):
    validate_item(item)
    if len(data) > MAX_PACKAGE or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise Refused("Het pakket komt niet overeen met de goedgekeurde versie.")
    prefix = "custom_components/" + DOMAIN + "/"
    dest.mkdir()
    expected = None
    names = set()
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for e in archive.infolist():
            name = e.filename
            total += e.file_size
            if (total > 8 * 1024 * 1024 or e.is_dir() or name in names
                    or "\\" in name or ":" in name or "\x00" in name
                    or ".." in PurePosixPath(name).parts or name.startswith("/")
                    or stat.S_ISLNK(e.external_attr >> 16)):
                raise Refused("Onveilige pakketinhoud.")
            names.add(name)
            content = archive.read(e)
            if name == "checksums.json":
                expected = json.loads(content)
                continue
            if not name.startswith(prefix):
                raise Refused("Het pakket probeert buiten de Victron-integratie te schrijven.")
            relative = name[len(prefix):]
            if not relative or relative.startswith("/") or PurePosixPath(relative).as_posix() != relative:
                raise Refused("Ongeldig bestandspad.")
            f = dest / relative
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
            if f.suffix == ".py":
                compile(content, str(f), "exec")
    if not isinstance(expected, dict) or digest_tree(dest) != expected:
        raise Refused("Bestandscontrole mislukt.")
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("domain") != DOMAIN or manifest.get("version") != item["version"]:
        raise Refused("Manifest en goedgekeurde versie verschillen.")

def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)

class Installer:
    def __init__(self, root, data, api, github):
        self.root, self.data, self.api, self.github = Path(root), Path(data), api, github
        self.data.mkdir(parents=True, exist_ok=True)
        self.current = self.root / "custom_components" / DOMAIN
        self.lock = threading.Lock()
        self.plan = None
        self.journal = self.data / "transaction.json"
        self.progress = {"phase": "ready", "message": "Kies Controleren op updates."}
        if self.journal.exists():
            j = json.loads(self.journal.read_text())
            self.progress = j
            if j["phase"] not in TERMINAL:
                self.progress["message"] = "Onderbroken installatie. Geen automatische hervatting. Kies Vorige versie terugzetten."

    def save(self, phase, message, **extra):
        self.progress = dict(self.progress, phase=phase, message=message, **extra)
        atomic_json(self.journal, self.progress)

    def blocked(self):
        return self.journal.exists() and self.progress["phase"] not in TERMINAL

    def prepare(self):
        with self.lock:
            if self.blocked():
                raise Refused("Handel de vorige installatie eerst af.")
            proposal = self.github.proposal()
            old = current_version(self.current)
            if old == proposal["version"]:
                self.plan = None
                return {"installed": old, "up_to_date": True}
            if old not in proposal["from_versions"]:
                raise Refused("Deze update ondersteunt de huidige versie " + old + " niet.")
            self.plan = dict(proposal, old=old, old_digest=digest_tree(self.current),
                             approval=secrets.token_urlsafe(32), expires=time.monotonic() + 900)
            return {"installed": old, "version": proposal["version"],
                    "notes": str(proposal.get("notes", "")), "approval": self.plan["approval"]}

    def approve(self, approval):
        if not self.lock.acquire(blocking=False):
            raise Refused("Er loopt al een bewerking.")
        try:
            p = self.plan
            if (not p or not secrets.compare_digest(str(approval), p["approval"])
                    or time.monotonic() > p["expires"] or self.blocked()):
                raise Refused("Goedkeuring ontbreekt of is verlopen. Controleer opnieuw.")
            # Consume the approval before any asynchronous work or mutation.
            self.plan = None
            return dict(p)
        finally:
            self.lock.release()

    def install(self, p):
        with self.lock:
            if self.blocked():
                raise Refused("Handel de vorige installatie eerst af.")
            if digest_tree(self.current) != p["old_digest"] or current_version(self.current) != p["old"]:
                raise Refused("De huidige bestanden zijn gewijzigd. Controleer opnieuw.")
            if safe_state(self.api) != p["old"]:
                raise Refused("De draaiende versie verschilt van de bestanden.")
            workspace = self.root / ".victron_updates"
            if workspace.is_symlink():
                raise Refused("Werkmap mag geen koppeling zijn.")
            workspace.mkdir(exist_ok=True)
            tx = secrets.token_hex(12)
            work = workspace / tx
            work.mkdir()
            stage = work / "next"
            unpack_package(self.github.contents(p["path"], p["commit"]), p, stage)
            self.api.check()
            backup = self.api.backup(p["version"])
            # Re-read safety AFTER the potentially long backup, immediately before stopping.
            if safe_state(self.api) != p["old"] or digest_tree(self.current) != p["old_digest"]:
                raise Refused("Toestand veranderde tijdens de voorbereiding.")
            self.progress = {}
            self.save("stopping", "Home Assistant wordt gestopt.", transaction=tx,
                      old=p["old"], version=p["version"], old_digest=p["old_digest"],
                      supervisor_backup=backup, commit=p["commit"])
            try:
                self.api.stop()
                self.save("stopped", "Home Assistant is gestopt; bestanden worden geplaatst.")
                os.replace(self.current, work / "previous")
                self.save("old_saved", "Vorige component is bewaard.")
                os.replace(stage, self.current)
                self.save("placed", "Bestanden zijn gecontroleerd en geplaatst.")
            except Exception:
                # Only this pre-start failure can be rolled back automatically.
                # A stop timeout is ambiguous: do not swap any files without stop acknowledgement.
                if self.progress["phase"] != "stopping":
                    self._restore_files(work)
                    self.save("starting_old", "Vorige bestanden hersteld; Home Assistant wordt gestart.")
                    self.api.start()
                    self.wait_ready(p["old"])
                    self.save("restored", "Installatie afgebroken; vorige versie is bevestigd.")
                else:
                    self.save("attention", "Stop niet bevestigd. Bestanden niet vervangen; controleer Home Assistant.")
                raise
            self.save("starting", "Home Assistant wordt gestart; laadstop blijft aan.")
            self.api.start()
            self.wait_ready(p["version"])
            self.api.check()
            self.save("installed", "Versie " + p["version"] + " is geladen. Laadstop blijft aan; hervat de regeling bewust na controle.")

    def _restore_files(self, work):
        previous = work / "previous"
        if previous.exists():
            if digest_tree(previous) != self.progress["old_digest"]:
                raise Refused("Back-upbestanden zijn gewijzigd. Gebruik de Home Assistant-back-up.")
            if self.current.exists():
                # Preserve the failed version as evidence; never delete user files.
                failed = work / ("replaced-" + secrets.token_hex(4))
                os.replace(self.current, failed)
            os.replace(previous, self.current)
        if not self.current.exists() or digest_tree(self.current) != self.progress["old_digest"]:
            raise Refused("Vorige bestanden niet bevestigd. Gebruik de Home Assistant-back-up.")

    def restore(self):
        with self.lock:
            if not self.journal.exists() or "transaction" not in self.progress:
                raise Refused("Geen vorige installatie beschikbaar.")
            # Recovery is explicit and still requires live proof of disabled controllers.
            # If Core cannot start, use Supervisor's full HA backup instead of guessing.
            safe_state(self.api)
            tx = self.progress["transaction"]
            if not re.fullmatch(r"[a-f0-9]{24}", tx):
                raise Refused("Ongeldig herstelregister.")
            work = self.root / ".victron_updates" / tx
            self.save("restore_stopping", "Home Assistant wordt gestopt voor herstel.")
            self.api.stop()
            self.save("restore_stopped", "Vorige component wordt teruggezet.")
            self._restore_files(work)
            self.save("restore_starting", "Home Assistant wordt gestart met de vorige versie.")
            self.api.start()
            self.wait_ready(self.progress["old"])
            self.api.check()
            self.save("restored", "Vorige versie bevestigd. Laadstop blijft aan.")

    def wait_ready(self, version):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                config = self.api.call("/core/api/config")
                if config.get("state") == "RUNNING" and safe_state(self.api) == version:
                    return
            except Refused:
                pass
            time.sleep(3)
        raise Refused("Herstart of veilige versiecontrole niet bevestigd. Controleer de appstatus en de Home Assistant-back-up.")
