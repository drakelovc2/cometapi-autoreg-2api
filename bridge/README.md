# cometapi2api — мост к пулу ключей CometAPI

OpenAI-совместимый шлюз к ключам CometAPI (`https://api.cometapi.com/v1`).
Ключи не хранятся в конфиге: банк читается из `../register/data/accounts.jsonl`
с hot-reload по mtime.

Порт по умолчанию: `12063`, слушает `127.0.0.1`.

## Запуск

```bash
cp config.example.json config.json
pip install -r requirements.txt
./start.sh            # --stop | --status | --restart
```

## Эндпоинты

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI chat, stream и non-stream |
| POST | `/v1/responses` | OpenAI Responses API |
| POST | `/v1/embeddings` | эмбеддинги |
| GET | `/v1/models` | каталог апстрима (кэш `models_cache_sec`) |
| GET | `/v1/balance` | `usd_left` по ключам из банка, без апстрима |
| GET | `/health` | 200 всегда |
| GET | `/admin/status` | состояние пула и ключей |
| POST | `/admin/reload` | перечитать конфиг и банк |

Anthropic `/v1/messages` этот мост **не** проксирует — CometAPI отдаёт
OpenAI-формат.

## Ротация

Секция `bridge` в конфиге:

- `strategy: least_used` — берём ключ с наименьшим числом запросов;
- `max_attempts` — сколько ключей пробовать на один запрос;
- `cooldown_429_sec`, `cooldown_5xx_sec` — пауза ключа после ошибок;
- `fails_to_dead` — после скольких фатальных ошибок ключ помечается мёртвым;
- `r429_to_exhausted`, `exhausted_park_min` — парковка исчерпанных ключей;
- `bank_poll_sec` — период проверки mtime банка;
- `min_usd_left` — не брать ключи с остатком ниже порога.

`client_keys` — если пусто, клиентский ключ не проверяется (мост слушает только
localhost). Для доступа извне заполните список и не выставляйте порт в интернет
без TLS/авторизации.

## Подключение

```bash
export OPENAI_BASE_URL=http://127.0.0.1:12063/v1
export OPENAI_API_KEY=sk-...
```

## Ограничения

Только собственные/разрешённые ключи. Мост не обходит лимиты CometAPI.