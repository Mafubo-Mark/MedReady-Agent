#!/bin/zsh
set -e

cd "$(dirname "$0")"

if ! python3 - <<'PY'
from pathlib import Path
path = Path('.env')
settings = {}
if path.exists():
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            settings[key.strip()] = value.strip()
if not settings.get('BAIDU_SEARCH_API_KEY') or not settings.get('DEEPSEEK_API_KEY'):
    raise SystemExit(1)
PY
then
  echo 'Please add your new Baidu and DeepSeek keys to .env, then double-click this file again.'
  echo 'Press Enter to close.'
  read
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo 'Creating the local Python environment...'
  python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import fastapi, uvicorn, requests, bs4, pydantic, streamlit, dotenv' 2>/dev/null; then
  echo 'Installing project packages (first launch only)...'
  .venv/bin/python -m pip install -r requirements.txt
fi

cleanup() {
  kill "$api_pid" "$ui_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

echo 'Starting MedReady...'
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir api --reload-dir core --reload-dir utils &
api_pid=$!
.venv/bin/python -m streamlit run frontend/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true &
ui_pid=$!
sleep 4
if ! kill -0 "$api_pid" 2>/dev/null || ! kill -0 "$ui_pid" 2>/dev/null; then
  echo 'A service could not start. Check whether ports 8000 or 8501 are already in use.'
  echo 'Press Enter to close.'
  read
  exit 1
fi

open 'http://127.0.0.1:8501'
echo 'MedReady is open. Keep this Terminal window open. Press Control+C here to stop both services.'
wait
