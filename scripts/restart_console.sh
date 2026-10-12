#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for p in $(pgrep -f "[c]onsole\.serve[r]"); do kill $p; done
sleep 1
nohup python3 -m console.server > data/console.log 2>&1 &
sleep 2
