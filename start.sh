#!/usr/bin/env bash
set -e

echo "Starting FXBankBot..."

# Запускаем FastAPI (uvicorn)
exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}

