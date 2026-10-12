#!/usr/bin/env bash
# Restart both sandbox apps in the background (Linux/macOS helper for development).
cd "$(dirname "$0")/.."
for p in $(pgrep -f "[a]pps\.(steel|ecommerce|school)\.app"); do kill $p; done
sleep 1
nohup python3 -m apps.steel.app > data/steel.log 2>&1 &
nohup python3 -m apps.ecommerce.app > data/ecom.log 2>&1 &
nohup python3 -m apps.school.app > data/school.log 2>&1 &
sleep 2
