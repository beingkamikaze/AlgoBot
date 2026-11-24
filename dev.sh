#!/usr/bin/env bash
# Starts TradingBot locally: backend on http://localhost:8100, web app on http://localhost:3100.
set -e
cd "$(dirname "$0")"
backend/.venv/bin/uvicorn --app-dir backend app.main:app --port 8100 --reload --reload-dir backend/app &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
npm --prefix web run dev
