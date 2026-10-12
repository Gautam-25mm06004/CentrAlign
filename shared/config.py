"""Shared paths, ports and secrets.

Secrets live in data/secrets.json, created on first use by load_secrets(). They are read by
trusted processes only (sandbox apps, authorization service, checker, verifier).
Nothing in this module is ever placed in model context.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNS = ROOT / "runs"
COMPANIES = ROOT / "companies"

def _profile_ports() -> dict:
    """Each company profile declares its sandbox app key and port (no hard-coded company list)."""
    import yaml
    ports = {}
    for prof in sorted(COMPANIES.glob("*/profile.yaml")):
        d = yaml.safe_load(prof.read_text(encoding="utf-8")) or {}
        if d.get("app") and d.get("port"):
            ports[d["app"]] = int(os.environ.get(f"{d['app'].upper()}_PORT", d["port"]))
    return ports


PORTS = {"console": int(os.environ.get("CONSOLE_PORT", 8100)), **_profile_ports()}


def app_origin(company: str) -> str:
    return f"http://127.0.0.1:{PORTS[company]}"


def app_db(company: str) -> Path:
    return DATA / f"{company}.db"


SECRETS_FILE = DATA / "secrets.json"


def load_secrets() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    if not SECRETS_FILE.exists():
        regenerate_secrets()
    s = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    missing = [app for app in PORTS if app != "console" and app not in s["credentials"]]
    if missing:  # a newly onboarded company gets its own sandbox credentials
        for app in missing:
            s["credentials"][app] = {"username": f"{app}.agent", "password": secrets.token_urlsafe(10)}
        SECRETS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def regenerate_secrets() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    s = {
        # signs authorization capabilities (authorization service + sandbox validators)
        "capability_key": secrets.token_hex(32),
        # read-only trusted API used by checker/verifier only
        "trusted_key": secrets.token_hex(16),
        # test-only fault switches used by the evaluation harness only
        "fault_key": secrets.token_hex(16),
        # sandbox login credentials, injected by the credential provider
        "credentials": {app: {"username": f"{app}.agent", "password": secrets.token_urlsafe(10)}
                        for app in PORTS if app != "console"},
    }
    SECRETS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def load_env_file() -> None:
    """Minimal .env loader (no extra dependency)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
