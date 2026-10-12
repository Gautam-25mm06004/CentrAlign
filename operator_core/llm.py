"""Model adapter: Gemini (free tier, REST via the standard library) behind one metered interface.

Every attempt — including retries and repairs — is written to the model-call ledger before
dispatch. There is no paid fallback: exhausting free capacity raises QuotaExhausted and the
runtime performs a controlled handoff.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from operator_core import store

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class QuotaExhausted(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class ModelError(Exception):
    pass


@dataclass
class ModelTurn:
    calls: list[dict]
    text: str = ""
    usage: dict = field(default_factory=dict)
    finish: str = ""


def pacific_day_start() -> float:
    """Gemini daily request quotas reset at midnight Pacific time."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def daily_allocation() -> int | None:
    """GEMINI_RPD x GEMINI_EVAL_SHARE, or None when GEMINI_RPD is not set (protection off, reported loudly)."""
    rpd = os.environ.get("GEMINI_RPD")  # observed free-tier requests/day from AI Studio
    return int(int(rpd) * float(os.environ.get("GEMINI_EVAL_SHARE", 0.9))) if rpd else None


class Budget:
    """Per-run cap (the run's own call_cap, e.g. the remainder of an evaluation suite's allocation, else
    MAX_MODEL_CALLS_PER_RUN) and a daily allocation counted over REAL provider requests only: the offline
    scripted client never consumes free-tier quota and is excluded."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        run = store.get_run(run_id) or {}
        env_cap = int(os.environ.get("MAX_MODEL_CALLS_PER_RUN", 90))
        self.per_run = min(env_cap, run["call_cap"]) if run.get("call_cap") is not None else env_cap
        self.daily_allocation = daily_allocation()

    def admit(self):
        used_run = len(store.calls(self.run_id))
        if used_run >= self.per_run:
            raise QuotaExhausted("run_call_cap", f"{used_run}/{self.per_run} model calls used by this run")
        if self.daily_allocation is not None:
            used_today = len(store.calls(since=pacific_day_start(), provider_only=True))
            if used_today >= self.daily_allocation:
                raise QuotaExhausted("daily_allocation", f"{used_today}/{self.daily_allocation} calls today")

    def status(self) -> dict:
        return {"run_calls": len(store.calls(self.run_id)), "run_cap": self.per_run,
                "today_calls": len(store.calls(since=pacific_day_start(), provider_only=True)),
                "daily_allocation": self.daily_allocation}


def to_declarations(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        d = {"name": t["name"], "description": t["description"]}
        params = dict(t["parameters"])
        if params.get("properties"):
            if not params.get("required"):
                params.pop("required", None)
            d["parameters"] = params
        out.append(d)
    return out


class GeminiModel:
    kind = "gemini"

    def __init__(self, run_id: str, model: str | None = None):
        self.run_id = run_id
        self.model = model or os.environ.get("GEMINI_MODEL", "")
        self.key = os.environ.get("GEMINI_API_KEY", "")
        if not self.model or not self.key:
            raise ModelError("Set GEMINI_MODEL and GEMINI_API_KEY in .env (free-tier key from Google AI Studio).")
        self.max_output = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", 2048))
        self.thinking = os.environ.get("GEMINI_THINKING", "budget:512")  # budget:N | level:low | off
        self.budget = Budget(run_id)
        if self.budget.daily_allocation is None:
            store.emit(run_id, "budget_warning", {"message": "Daily free-tier protection is OFF because GEMINI_RPD is "
                                                  "not set. Set it in .env to the model's requests-per-day limit."})

    def _gen_config(self):
        cfg = {"temperature": 0.2, "maxOutputTokens": self.max_output}
        if self.thinking.startswith("budget:"):
            cfg["thinkingConfig"] = {"thinkingBudget": int(self.thinking.split(":")[1])}
        elif self.thinking.startswith("level:"):
            cfg["thinkingConfig"] = {"thinkingLevel": self.thinking.split(":")[1]}
        return cfg

    def generate(self, system: str, user: str, tools: list[dict], kind: str = "step") -> ModelTurn:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"functionDeclarations": to_declarations(tools)}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": self._gen_config(),
        }
        minute_waits, transient = 0, 0
        while True:
            self.budget.admit()
            self._pace()
            cid = store.record_call(self.run_id, "dispatched", kind)
            try:
                data = self._post(body)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                store.update_call(cid, status=f"http_{e.code}", error=text[:500])
                if e.code == 429:
                    if "PerDay" in text or "per day" in text.lower():
                        raise QuotaExhausted("provider_daily_quota", text[:300]) from e
                    if minute_waits < 2:
                        minute_waits += 1
                        time.sleep(min(60, _retry_delay(text) or 20))
                        continue
                    raise QuotaExhausted("provider_rate_limit", text[:300]) from e
                if e.code == 400 and "thinking" in text.lower() and self.thinking != "off":
                    self.thinking = "off"  # model does not accept this thinking config; retry without it
                    body["generationConfig"] = self._gen_config()
                    continue
                if e.code >= 500 and transient < 2:
                    transient += 1
                    time.sleep(3 * transient)
                    continue
                raise ModelError(f"Gemini HTTP {e.code}: {text[:300]}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                store.update_call(cid, status="network_error", error=str(e)[:300])
                if transient < 2:
                    transient += 1
                    time.sleep(3 * transient)
                    continue
                raise ModelError(f"Gemini unreachable: {e}") from e
            usage = data.get("usageMetadata", {})
            store.update_call(cid, status="ok", input_tokens=usage.get("promptTokenCount", 0),
                              output_tokens=usage.get("candidatesTokenCount", 0),
                              thinking_tokens=usage.get("thoughtsTokenCount", 0))
            cand = (data.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                     for p in parts if "functionCall" in p]
            text = " ".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
            return ModelTurn(calls=calls, text=text, usage=usage, finish=cand.get("finishReason", ""))

    def _pace(self):
        """Stay under the free tier's requests-per-minute limit (GEMINI_RPM) without spending calls."""
        rpm = float(os.environ.get("GEMINI_RPM", 0) or 0)
        if rpm <= 0:
            return
        recent = [c["ts"] for c in store.calls(since=time.time() - 60, provider_only=True)]
        if len(recent) >= rpm:
            wait = 60 - (time.time() - min(recent)) + 0.5
            if wait > 0:
                store.emit(self.run_id, "rate_pacing", {"wait_s": round(wait, 1), "rpm": rpm})
                time.sleep(wait)

    def _post(self, body):
        req = urllib.request.Request(API.format(model=self.model), data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
        with urllib.request.urlopen(req, timeout=90) as r:  # honours HTTPS_PROXY if set
            return json.loads(r.read())


def _retry_delay(text: str) -> float | None:
    try:
        for d in json.loads(text)["error"].get("details", []):
            if "retryDelay" in d:
                return float(d["retryDelay"].rstrip("s"))
    except Exception:  # noqa: BLE001
        return None
    return None
