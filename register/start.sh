#!/bin/bash
# Панель реггера → http://127.0.0.1:<PORT>  (свой порт: впиши в autoreg/docs/PANEL-PORTS.md)
# Запуск:  bash start.sh               — поднимает панель и открывает её в браузере (xdg-open)
#          bash start.sh --no-browser  — то же, но без окна браузера (для тестов/скриптов)
# Вывод дублируется в data/panel.log; Ctrl+C гасит панель.
cd "$(dirname "$0")" || exit 1
export -n PYTHONHOME PYTHONPATH 2>/dev/null; unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH

NO_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --no-browser) NO_BROWSER=1 ;;
    -h|--help)
      echo "usage: bash start.sh [--no-browser]"
      exit 0 ;;
    *)
      echo "[!] неизвестный аргумент: $arg (см. bash start.sh --help)"
      exit 2 ;;
  esac
done

PORT=$(python3 -c "import json;print(json.load(open('config.json')).get('panel_port',9935))" 2>/dev/null || echo 9935)

# Если порт уже занят нашей старой панелью — гасим её (иначе «Address already in use»).
fuser -k -n tcp "$PORT" 2>/dev/null && sleep 1

mkdir -p data
PANEL_URL="http://127.0.0.1:$PORT"
echo "== панель реггера на 127.0.0.1:$PORT ($(date '+%Y-%m-%d %H:%M:%S')) ==" | tee -a data/panel.log

# Автооткрытие панели: ждём, пока сервер ответит, и открываем браузер.
# Сабшелл фоновый — передний план (exec панели) не трогаем.
if [[ "$NO_BROWSER" != "1" ]]; then
  (
    for i in $(seq 1 40); do
      env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
        curl -s -o /dev/null -m 1 "$PANEL_URL/api/summary" && break
      sleep 0.3
    done
    xdg-open "$PANEL_URL" >/dev/null 2>&1 || true
    echo "[реггер] браузер открыт: $PANEL_URL (--no-browser отключает)"
  ) &
fi

# -u: панель не буферизует stdout — лог в data/panel.log живой; tee дублирует вывод в файл
exec env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH \
  python3 -u panel/server.py --port "$PORT" > >(tee -a data/panel.log) 2>&1
