"""Installer proofs use temporary folders and a fake Supervisor; no HA connection."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "victron_installer"))
import engine
from engine import DOMAIN, Installer, Refused, GitHub, safe_state, unpack_package
from app import App, handler_for

def package(version="0.9.0rc14", extra=None):
    files = {"manifest.json": json.dumps({"domain": DOMAIN, "version": version}).encode(),
             "__init__.py": b"VALUE = 14\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr("custom_components/" + DOMAIN + "/" + name, data)
        z.writestr("checksums.json", json.dumps({n: hashlib.sha256(b).hexdigest() for n,b in files.items()}))
        if extra:
            for name, data in extra:
                z.writestr(name, data)
    data = buf.getvalue()
    return data, {"version": version, "from_versions": ["0.9.0rc13"],
                  "path": "packages/victron-" + version + ".zip",
                  "sha256": hashlib.sha256(data).hexdigest(), "commit": "a" * 40}

class API:
    def __init__(self, root):
        self.root = root
        self.events = []
        self.running = True
        self.bad_check = False
        self.bad_backup = False
        self.bad_stop = False
        self.mutate_during_backup = False
        self.states = {}
        for entity, value in {
            "input_boolean.victron_laadstop": "on", "switch.ess_force_charge": "off",
            "input_boolean.victron_nacht_laadlimiet_actief": "off",
            "number.gx_device_ess_max_charge_current": "-1",
            "input_number.victron_nacht_originele_laadstroomlimiet": "-1"
        }.items():
            self.states[entity] = {"entity_id": entity, "state": value}

    def state(self, entity):
        if not self.running:
            raise Refused("Offline")
        if entity in self.states:
            return self.states[entity]
        version = engine.current_version(self.root / "custom_components" / DOMAIN)
        return {"entity_id": entity, "state": "UIT",
                "last_reported": datetime.now(timezone.utc).isoformat(),
                "attributes": {"versie": version, "ingeschakeld": False,
                    "p1_bescherming": {"toestemming_actief": False, "actieve_ingreep": False,
                        "schrijver_gereserveerd": False, "lease": None, "schrijfopdracht_onbevestigd": None},
                    "ketenherstel": {"onbevestigd": False, "wachtende_verzoeken": 0}}}

    def call(self, path, body=None, timeout=30):
        assert path == "/core/api/config", "No hardware or service endpoints permitted in tests"
        if not self.running:
            raise Refused("Offline")
        return {"state": "RUNNING"}

    def stop(self):
        self.events.append("stop")
        if self.bad_stop:
            raise Refused("Unknown stop result")
        self.running = False

    def start(self):
        self.events.append("start")
        self.running = True

    def check(self):
        assert self.running, "Supervisor config check executes inside the running Core container"
        self.events.append("check")
        if self.bad_check:
            raise Refused("Invalid config")

    def backup(self, version):
        self.events.append("backup")
        if self.bad_backup:
            raise Refused("Backup failed")
        if self.mutate_during_backup:
            self.states["input_boolean.victron_laadstop"]["state"] = "off"
        return "backup123"

class Source:
    def __init__(self):
        self.data, self.item = package()
        self.calls = []

    def proposal(self):
        return dict(self.item)

    def contents(self, path, commit):
        self.calls.append((path, commit))
        assert commit == "a" * 40
        return self.data

@pytest.fixture
def setup(tmp_path):
    current = tmp_path / "ha" / "custom_components" / DOMAIN
    current.mkdir(parents=True)
    (current / "manifest.json").write_text(json.dumps({"domain": DOMAIN, "version": "0.9.0rc13"}))
    (current / "__init__.py").write_text("VALUE = 13\n")
    root = tmp_path / "ha"
    api = API(root)
    src = Source()
    installer = Installer(root, tmp_path / "data", api, src)
    return installer, api, src

def approve(installer):
    p = installer.prepare()
    return installer.approve(p["approval"])

def test_version_bound_install_backup_and_explicit_restore(setup):
    i, api, src = setup
    before = engine.digest_tree(i.current)
    p = approve(i)
    # Main moving after approval does not change the approved source commit or bytes.
    src.item["commit"] = "b" * 40
    i.install(p)
    assert src.calls == [(p["path"], "a" * 40)]
    assert engine.current_version(i.current) == "0.9.0rc14"
    assert i.progress["phase"] == "installed"
    assert api.events == ["check", "backup", "stop", "start", "check"]
    assert i.progress["supervisor_backup"] == "backup123"
    i.restore()
    assert engine.digest_tree(i.current) == before
    assert i.progress["phase"] == "restored"

@pytest.mark.parametrize("field,value", [
    ("input_boolean.victron_laadstop", "off"),
    ("switch.ess_force_charge", "on"),
    ("input_boolean.victron_nacht_laadlimiet_actief", "on"),
    ("number.gx_device_ess_max_charge_current", "70"),
    ("number.gx_device_ess_max_charge_current", "unavailable"),
])
def test_unsafe_state_blocks_without_mutation(setup, field, value):
    i, api, _ = setup
    before = engine.digest_tree(i.current)
    p = approve(i)
    api.states[field]["state"] = value
    with pytest.raises(Refused):
        i.install(p)
    assert before == engine.digest_tree(i.current)
    assert not api.events

@pytest.mark.parametrize("kind", ["backup", "safety_changed", "config"])
def test_failed_preparation_preserves_current_files(setup, kind):
    i, api, _ = setup
    before = engine.digest_tree(i.current)
    p = approve(i)
    api.bad_backup = kind == "backup"
    api.bad_check = kind == "config"
    api.mutate_during_backup = kind == "safety_changed"
    with pytest.raises(Refused):
        i.install(p)
    assert before == engine.digest_tree(i.current)
    assert "stop" not in api.events

def test_stop_timeout_never_swaps_and_blocks_new_install(setup):
    i, api, _ = setup
    before = engine.digest_tree(i.current)
    p = approve(i)
    api.bad_stop = True
    with pytest.raises(Refused):
        i.install(p)
    assert before == engine.digest_tree(i.current)
    assert i.progress["phase"] == "attention"
    with pytest.raises(Refused):
        i.prepare()

def test_swap_error_restores_before_restart(setup, monkeypatch):
    i, api, _ = setup
    before = engine.digest_tree(i.current)
    p = approve(i)
    original = engine.os.replace
    def replace(source, target):
        if Path(source).name == "next":
            raise OSError("Injected failure")
        original(source, target)
    monkeypatch.setattr(engine.os, "replace", replace)
    with pytest.raises(OSError):
        i.install(p)
    assert before == engine.digest_tree(i.current)
    assert i.progress["phase"] == "restored"
    assert api.running

def test_tampered_previous_backup_not_restored(setup):
    i, api, _ = setup
    i.install(approve(i))
    backup = i.root / ".victron_updates" / i.progress["transaction"] / "previous"
    (backup / "__init__.py").write_text("tampered")
    with pytest.raises(Refused):
        i.restore()
    assert engine.current_version(i.current) == "0.9.0rc14"
    assert i.progress["phase"] == "restore_stopped"

def test_approval_wrong_expired_single_use_and_local_changes(setup):
    i, api, _ = setup
    p = i.prepare()
    with pytest.raises(Refused):
        i.approve("wrong")
    i.plan["expires"] = time.monotonic() - 1
    with pytest.raises(Refused):
        i.approve(p["approval"])
    p = approve(i)
    with pytest.raises(Refused):
        i.approve(p["approval"])
    (i.current / "__init__.py").write_text("changed")
    with pytest.raises(Refused):
        i.install(p)
    assert not api.events

def test_restart_does_not_resume_unfinished_transaction(setup):
    i, api, src = setup
    i.save("stopped", "Injected power loss", transaction="c" * 24)
    another = Installer(i.root, i.data, api, src)
    assert not api.events
    assert another.blocked()
    with pytest.raises(Refused):
        another.prepare()

@pytest.mark.parametrize("extra", [
    [("../configuration.yaml", b"bad")],
    [("custom_components/other/__init__.py", b"bad")],
    [("custom_components/" + DOMAIN + "/../../configuration.yaml", b"bad")],
    [("/configuration.yaml", b"bad")],
    [("custom_components/" + DOMAIN + "/manifest.json", b"duplicate")],
    [("custom_components/" + DOMAIN + "/new.py", b"tampered")],
])
def test_package_rejects_path_escape_unexpected_files_and_duplicates(tmp_path, extra):
    data, item = package(extra=extra)
    with pytest.raises(Refused):
        unpack_package(data, item, tmp_path / "stage")
    assert not (tmp_path / "configuration.yaml").exists()

def test_package_checksum_and_symlink_rejected(tmp_path):
    data, item = package()
    with pytest.raises(Refused):
        unpack_package(data + b"x", item, tmp_path / "stage1")
    b = io.BytesIO(data)
    with zipfile.ZipFile(b, "a") as z:
        link = zipfile.ZipInfo("custom_components/" + DOMAIN + "/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/config")
    item["sha256"] = hashlib.sha256(b.getvalue()).hexdigest()
    with pytest.raises(Refused):
        unpack_package(b.getvalue(), item, tmp_path / "stage2")

@pytest.mark.parametrize("change", ["p1", "enabled", "lease", "pending", "recovery", "stale"])
def test_incomplete_or_active_diagnostic_is_not_safe(setup, change):
    _, api, _ = setup
    report = api.state("sensor.victron_b_regelstatus")
    attrs = report["attributes"]
    if change == "p1": attrs["p1_bescherming"]["toestemming_actief"] = True
    if change == "enabled": attrs["ingeschakeld"] = True
    if change == "lease": attrs["p1_bescherming"]["lease"] = {}
    if change == "pending": attrs["p1_bescherming"].pop("schrijfopdracht_onbevestigd")
    if change == "recovery": attrs["ketenherstel"]["wachtende_verzoeken"] = 1
    if change == "stale": report["last_reported"] = (datetime.now(timezone.utc)-timedelta(seconds=200)).isoformat()
    api.states["sensor.victron_b_regelstatus"] = report
    with pytest.raises(Refused):
        safe_state(api)

def test_source_requires_successful_matching_push_workflow(monkeypatch):
    data, item = package()
    content = base64.b64encode(json.dumps({"schema": 1, "current": item}).encode()).decode()
    run = {"id": 1, "path": ".github/workflows/verify.yml", "head_sha": "a" * 40,
           "head_branch": "main", "status": "completed", "conclusion": "success"}
    def get(self, path):
        if path == "/commits/main": return {"sha": "a" * 40}
        if path.startswith("/actions/runs"): return {"workflow_runs": [dict(run)]}
        return {"encoding": "base64", "content": content}
    monkeypatch.setattr(GitHub, "get", get)
    source = GitHub("not-a-real-token")
    assert source.proposal()["commit"] == "a" * 40
    run["conclusion"] = "failure"
    with pytest.raises(Refused):
        source.proposal()
    run["conclusion"] = "success"
    run["head_sha"] = "b" * 40
    with pytest.raises(Refused):
        source.proposal()

def test_ingress_rejects_non_proxy_and_missing_identity(setup):
    i, _, _ = setup
    handler = handler_for(App(i))
    h = object.__new__(handler)
    h.client_address = ("127.0.0.1", 1234)
    h.headers = {"X-Remote-User-Id": "user"}
    assert not h.allowed()
    h.client_address = ("172.30.32.2", 1234)
    h.headers = {}
    assert not h.allowed()
    h.headers = {"X-Remote-User-Id": "user"}
    assert h.allowed()
