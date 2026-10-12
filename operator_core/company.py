"""Load a company bundle: profile, policies, handbook and integration adapter."""
from __future__ import annotations

import importlib

import yaml

from shared.config import COMPANIES, app_origin


def load(company_id: str) -> dict:
    base = COMPANIES / company_id
    profile = yaml.safe_load((base / "profile.yaml").read_text(encoding="utf-8"))
    return {
        "id": company_id,
        "profile": profile,
        "policy": yaml.safe_load((base / "policies.yaml").read_text(encoding="utf-8")),
        "handbook": (base / "handbook.md").read_text(encoding="utf-8"),
        "adapter": importlib.import_module(f"companies.{company_id}.adapter"),
        "origin": app_origin(profile["app"]),
        "files_dir": (base / profile["files"]["directory"]) if profile.get("files") else None,
    }


def available() -> list[str]:
    return sorted(p.name for p in COMPANIES.iterdir() if (p / "profile.yaml").exists())
