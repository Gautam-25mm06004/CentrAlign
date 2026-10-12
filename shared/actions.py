"""Versioned business-action schemas (declared in each company profile) used by the authorization service, the browser
request guard and the sandbox validators. Each field has a type that defines its
canonical form, so "3,68,160.00" and "368160" are the same amount, while any real
change of value produces a different digest.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def _load_profile_schemas() -> dict:
    """Company profiles declare the schemas of their protected write actions."""
    import yaml
    from shared.config import COMPANIES
    out = {}
    for prof in sorted(COMPANIES.glob("*/profile.yaml")):
        d = yaml.safe_load(prof.read_text(encoding="utf-8")) or {}
        out.update(d.get("action_schemas") or {})
    return out


ACTION_SCHEMAS: dict[str, dict[str, str]] = _load_profile_schemas()


class FieldError(ValueError):
    pass


# Accepted input grammars. Values are validated, never silently reinterpreted: "-6" is not "6",
# "1.5" is not an integer, "1e3" is not an amount.
_CURRENCY = re.compile(r"^\s*(?:₹|rs\.?|inr)?\s*", re.I)
_MONEY = re.compile(r"^(\d+(?:,\d+)*)(?:\.(\d{1,2}))?$")   # 368160 · 3,68,160 · 3,68,160.00
_QTY3 = re.compile(r"^\d+(?:\.\d{1,3})?$")                   # 6 · 6.000 (non-negative, max 3 decimals)
_INT = re.compile(r"^-?\d+$")


def money_to_paise(v) -> int:
    """Non-negative rupee amount (optional ₹ / Rs / INR prefix, digit grouping, up to 2 decimals) -> paise."""
    s = _CURRENCY.sub("", str(v if v is not None else "")).strip()
    m = _MONEY.match(s)
    if not m:
        raise FieldError(f"not a valid rupee amount: {v!r} (use digits, optional commas and up to 2 decimals)")
    rupees = int(m.group(1).replace(",", ""))
    paise = int((m.group(2) or "0").ljust(2, "0"))
    return rupees * 100 + paise


def paise_to_str(p: int) -> str:
    return f"{Decimal(p) / 100:.2f}"


def fmt_inr(p: int) -> str:
    """Indian digit grouping, e.g. 76464000 paise -> ₹7,64,640.00"""
    neg = p < 0
    p = abs(p)
    rupees, paise = divmod(p, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{'-' if neg else ''}₹{s}.{paise:02d}"


def normalize_field(ftype: str, v) -> str:
    if v is None:
        v = ""
    if ftype == "money":
        return str(money_to_paise(v))
    if ftype == "qty3":
        s = str(v).strip()
        if not _QTY3.match(s):
            raise FieldError(f"not a valid quantity: {v!r} (non-negative, at most 3 decimals)")
        return str(Decimal(s).quantize(Decimal("0.001")))
    if ftype == "int":
        s = str(v).strip()
        s = s[:-1].strip() if s.endswith("%") else s
        if not _INT.match(s):
            raise FieldError(f"not a whole number: {v!r}")
        return str(int(s))
    if ftype == "id":
        return str(v).strip().upper()
    if ftype == "label":  # descriptive text compared without case/spacing differences (e.g. a steel grade)
        return " ".join(str(v).split()).casefold()
    if ftype.startswith("enum:"):
        allowed = ftype[5:].split(",")
        s = str(v).strip()
        if s not in allowed:
            raise FieldError(f"{v!r} not one of {allowed}")
        return s
    return " ".join(str(v).split())  # text


NON_BUSINESS_FIELDS = {"csrf_token", "reason"}  # transport/audit only, never part of the digest


def business_fields_from_form(action: str, items: list[tuple[str, str]]) -> dict:
    """Pick business fields from submitted form pairs. Duplicate business keys are rejected."""
    schema = ACTION_SCHEMAS[action]
    out: dict[str, str] = {}
    for k, v in items:
        if k in NON_BUSINESS_FIELDS:
            continue
        if k not in schema:
            raise FieldError(f"unknown submitted field {k!r}")
        if k in out:
            raise FieldError(f"duplicate submitted field {k!r}")
        out[k] = v
    return normalize(action, out)


def normalize(action: str, raw: dict) -> dict:
    """Return canonical business fields. Unknown or missing fields are errors."""
    schema = ACTION_SCHEMAS[action]
    extra = set(raw) - set(schema)
    if extra:
        raise FieldError(f"unknown business fields: {sorted(extra)}")
    missing = set(schema) - set(raw)
    if missing:
        raise FieldError(f"missing business fields: {sorted(missing)}")
    return {k: normalize_field(t, raw[k]) for k, t in schema.items()}
