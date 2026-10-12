"""List Gemini models available to this key (model listing does not use generation quota)."""
import json, os, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.config import load_env_file
load_env_file()
k = os.environ["GEMINI_API_KEY"]
url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"x-goog-api-key": k}), timeout=60).read())
for m in d.get("models", []):
    if "generateContent" in m.get("supportedGenerationMethods", []):
        print(m["name"], "|", m.get("displayName"), "| in", m.get("inputTokenLimit"))
