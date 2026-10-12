"""One tiny Gemini call to confirm the key/model work. It is a real request, so it is recorded in the model-call
ledger (run id 'setup-check') and counts toward the daily free-tier allocation like any other call."""
import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.config import load_env_file
load_env_file()
from operator_core import store  # noqa: E402

store.init()
m, k = os.environ.get("GEMINI_MODEL"), os.environ.get("GEMINI_API_KEY")
if not m or not k:
    print("GEMINI_NOT_CONFIGURED: set GEMINI_API_KEY and GEMINI_MODEL in .env (offline tests work without them)")
    sys.exit(2)
body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the single word: ready"}]}],
        "generationConfig": {"maxOutputTokens": 20}}
req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": k})
cid = store.record_call("setup-check", "dispatched", "setup")
ok = False
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    u = d.get("usageMetadata", {})
    store.update_call(cid, status="ok", input_tokens=u.get("promptTokenCount", 0),
                      output_tokens=u.get("candidatesTokenCount", 0))
    print("GEMINI_OK", m, json.dumps(u))
    ok = True
except urllib.error.HTTPError as e:
    text = e.read().decode()[:600]
    store.update_call(cid, status=f"http_{e.code}", error=text[:500])
    print("GEMINI_HTTP_ERROR", e.code, text)
except Exception as e:
    store.update_call(cid, status="error", error=repr(e)[:300])
    print("GEMINI_ERROR", repr(e)[:400])
if not os.environ.get("GEMINI_RPD"):
    print("WARNING: GEMINI_RPD is not set, so the daily free-tier protection is OFF.")
sys.exit(0 if ok else 1)
