"""Start everything (Windows, macOS, Linux):  python start.py

Seeds the sandboxes on first run, then starts every company's sandbox app (steel 8101, e-commerce 8102,
school 8103, read from the company profiles) and the operator console (8100). Press Ctrl+C to stop them all.
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import webbrowser

from shared.config import DATA, PORTS, app_db

PY = sys.executable


def up(port: int) -> bool:
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/login"
                                                                          if port != PORTS["console"] else
                                                                          f"http://127.0.0.1:{port}/", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    DATA.mkdir(exist_ok=True)
    if any(not app_db(a).exists() for a in PORTS if a != "console") or "--reset" in sys.argv:
        subprocess.run([PY, "-m", "scripts.seed"], check=True)
    procs = []
    services = [(f"apps.{app}.app", f"{app}.log") for app in PORTS if app != "console"]
    for mod, log in services + [("console.server", "console.log")]:
        procs.append(subprocess.Popen([PY, "-m", mod], stdout=open(DATA / log, "a", encoding="utf-8"),
                                      stderr=subprocess.STDOUT))
    for _ in range(40):
        if all(up(p) for p in PORTS.values()):
            break
        time.sleep(0.5)
    else:
        print("Services did not start; see data/*.log")
    url = f"http://127.0.0.1:{PORTS['console']}"
    print(f"\nOperator console:  {url}")
    for app, port in PORTS.items():
        if app != "console":
            print(f"{app} sandbox app:   http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.\n")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("A service stopped unexpectedly; see data/*.log")
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
