#!/usr/bin/env bash
# cometapi2api — OpenAI-совместимый мост к пулу ключей CometAPI (127.0.0.1:12063).
#   bash start.sh          — запустить
#   bash start.sh --stop   — погасить
#   bash start.sh --status — проверить
#   bash start.sh --restart— погасить и запустить
#
# Пул ключей не здесь: банк читается hot-reload из
#   data/accounts.jsonl
# Логи: bridge.log (внутри server.py, с ротацией); data/console.log — только
# старт/падения, перезаписывается при каждом запуске.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=$(python3 -c "import json;print(json.load(open('${DIR}/config.json'))['port'])" 2>/dev/null || echo 12063)

listening() { ss -tlnH "sport eq :${PORT}" 2>/dev/null | grep -q .; }

case "${1:-}" in
  --stop)
    if listening; then
      fuser -k -n tcp "$PORT" >/dev/null 2>&1 || true
      sleep 1
    fi
    if listening; then echo "still listening on :$PORT"; exit 1; fi
    echo "stopped :$PORT"
    exit 0
    ;;
  --status)
    if listening; then
      echo "running on 127.0.0.1:$PORT"
      curl -s --max-time 3 "http://127.0.0.1:${PORT}/health" || true
      echo
    else
      echo "not running"
    fi
    exit 0
    ;;
  --restart)
    bash "$DIR/start.sh" --stop
    # провал ниже — обычный запуск (кейс без аргумента)
    ;;
esac

if listening; then
  echo "already running on 127.0.0.1:$PORT"
  exit 0
fi

cd "$DIR" || exit 1
mkdir -p data
chmod 700 data 2>/dev/null || true

nohup env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
  python3 -u server.py > data/console.log 2>&1 &
sleep 1

if listening; then
  echo "started on 127.0.0.1:$PORT (log: $DIR/bridge.log)"
else
  echo "failed to start; see $DIR/data/console.log and $DIR/bridge.log"
  exit 1
fi