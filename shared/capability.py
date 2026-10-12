"""Exact-action authorization capabilities.

Shared contract between the operator's authorization service (issuer) and the
sandbox apps (validators). A capability is bound to one action type, route,
case, canonical business payload digest, run, idempotency key and expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

HEADER = "X-Operator-Authorization"
CANON_VERSION = 1


def _norm_value(v) -> str:
    s = "" if v is None else str(v)
    return " ".join(s.split())  # collapse whitespace; case and digits preserved


def canonical_payload(fields: dict) -> str:
    """Versioned canonical form of business fields (sorted keys, normalized whitespace)."""
    clean = {k: _norm_value(v) for k, v in fields.items()}
    return json.dumps({"v": CANON_VERSION, "f": dict(sorted(clean.items()))}, separators=(",", ":"))


def payload_digest(fields: dict) -> str:
    return hashlib.sha256(canonical_payload(fields).encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(key: str, *, audience: str, method: str, route: str, action: str, case_id: str,
          digest: str, run_id: str, idempotency_key: str, ttl_s: int = 300) -> str:
    now = int(time.time())
    body = {
        "ver": 1, "jti": uuid.uuid4().hex, "iss": "operator-authz", "aud": audience,
        "method": method.upper(), "route": route, "action": action, "case": case_id,
        "digest": digest, "run": run_id, "idem": idempotency_key, "iat": now, "exp": now + ttl_s,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key.encode(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


class CapabilityError(Exception):
    pass


def verify(key: str, token: str | None, *, audience: str, method: str, route: str,
           action: str, fields: dict) -> dict:
    if not token or "." not in token:
        raise CapabilityError("missing authorization capability")
    raw_b64, sig_b64 = token.split(".", 1)
    try:
        raw = _unb64(raw_b64)
        sig = _unb64(sig_b64)
    except Exception as e:  # noqa: BLE001
        raise CapabilityError("malformed capability") from e
    expected = hmac.new(key.encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise CapabilityError("invalid capability signature")
    body = json.loads(raw)
    if body.get("exp", 0) < time.time():
        raise CapabilityError("capability expired")
    if body.get("aud") != audience:
        raise CapabilityError("capability addressed to another destination")
    if body.get("method") != method.upper() or body.get("route") != route:
        raise CapabilityError("capability bound to another route")
    if body.get("action") != action:
        raise CapabilityError(f"capability bound to action {body.get('action')}, request is {action}")
    if body.get("digest") != payload_digest(fields):
        raise CapabilityError("submitted business fields differ from the authorized values")
    return body
