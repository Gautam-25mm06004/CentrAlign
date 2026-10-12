"""Trusted read-only access to sandbox records (checker / verifier / reconciliation only).
The agent's browser is blocked from these endpoints and never sees the key."""
from __future__ import annotations

import json
import urllib.request

from shared.config import app_origin, load_secrets

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(company: str, path: str):
    req = urllib.request.Request(app_origin(company) + path,
                                 headers={"X-Trusted-Key": load_secrets()["trusted_key"]})
    with _NO_PROXY.open(req, timeout=10) as r:
        return json.loads(r.read())


def snapshot(company: str) -> dict:
    return _get(company, "/api/trusted/snapshot")


def idempotency(company: str, key: str) -> dict | None:
    return _get(company, f"/api/trusted/idempotency/{key}")


def set_faults(company: str, faults: dict) -> dict:
    req = urllib.request.Request(app_origin(company) + "/__faults", data=json.dumps(faults).encode(), method="POST",
                                 headers={"X-Fault-Key": load_secrets()["fault_key"], "Content-Type": "application/json"})
    with _NO_PROXY.open(req, timeout=10) as r:
        return json.loads(r.read())
