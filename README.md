# cometapi-autoreg-2api

Авторег аккаунтов **CometAPI** (`www.cometapi.com`) через GitHub OAuth + OpenAI-совместимый
мост к пулу их ключей. Новому аккаунту CometAPI начисляет **750 000 единиц квоты ≈ $1.50**
(курс 500 000 ед./$1); карта и почта не нужны — вход через GitHub.

```
cometapi-autoreg-2api/
├── register/   ← реггер + панель (:9935)
│   ├── core/   ← GitHub OAuth → CometAPI → ключ, прокси, банк
│   ├── panel/  ← веб-панель
│   └── tools/  ← recon, mock-сайт, последовательный батч
└── bridge/     ← мост (:12063), пул ключей с hot-reload
```

## Цепочка

1. `register/` берёт GitHub-аккаунт из пула, логинится в браузере (Camoufox),
   проходит TOTP и consent → OAuth-callback CometAPI → сессия.
2. Через API аккаунта создаёт ключ `sk-…` и проверяет его живым
   `POST /v1/chat/completions`.
3. Ключи пишутся в `register/data/accounts.jsonl`.
4. `bridge/` читает этот же файл (hot-reload по mtime) и раздаёт
   `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, `/v1/models`
   с ротацией и обработкой 401/402/429.

## Быстрый старт

```bash
# 0) GitHub-пул: по строке на аккаунт
#    email:password:TOTP_SECRET:recovery_codes:username
cp gitaccs.example.txt gitaccs.txt   # и заполни своими аккаунтами

# 1) реггер
cd register
cp config.example.json config.json    # gh_pool_file, прокси, порт
pip install -r requirements.txt
./start.sh                            # панель http://127.0.0.1:9935

# 2) мост
cd ../bridge
cp config.example.json config.json
pip install -r requirements.txt
./start.sh                            # http://127.0.0.1:12063
```

## Подключение инструментов к мосту

```bash
export OPENAI_BASE_URL=http://127.0.0.1:12063/v1
export OPENAI_API_KEY=sk-...          # любой непустой, если client_keys пуст
```

```bash
curl -s http://127.0.0.1:12063/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini-2024-07-18","messages":[{"role":"user","content":"hi"}]}'
```

## Как это работает (проверено живьём)

- **CometAPI — форк new-api.** Консоль — React SPA на `/console/`, API аккаунта — `/api/*`,
  OpenAI-шлюз — `https://api.cometapi.com/v1`.
- **GitHub OAuth обязателен.** Клик «Continue with GitHub» запускает
  `POST /api/oauth/pre-check {castle_request_token}` → `{"allowed":true}`, и только после
  этого открывается GitHub. Прямой переход на `authorize` без клика новый аккаунт
  получает `blocked by security check` (HTTP 200, `success:false`). Существующий —
  логинится нормально, поэтому легко принять за «работает».
- **TOTP GitHub.** После пароля — `input[name=app_otp]`. Кнопка Verify в HTML `disabled`,
  Enter по полю работает. На границе 30-секундного окна код флапает — ждать свежее окно.
- **Consent.** На странице две кнопки `name=authorize`: Cancel `value=0` и Authorize
  `value=1`. Жать только `1`.
- **Callback.** `redirect_uri` в authorize не передаётся — он зарегистрирован в OAuth App
  (`https://www.cometapi.com/console/oauth/github`). SPA сама делает
  `GET /api/oauth/github?code&state` и ставит session-cookie.
- **Ключ.** `POST /api/token/` + заголовок `X-Organization-Id` → полный ключ в `data.key`;
  в `GET /api/token/` он навсегда маскирован. Ключ для `/v1` = `"sk-" + data.key`.
- **Квота.** `GET /api/organization/detail?id=<org>` → `quota`. Знак-бонус 750 000 единиц
  начисляется **асинхронно**: первый запрос может отдать `quota=0`, через пару секунд —
  750000. Карта не нужна.
- **Рейт-лимит на IP.** `/api/oauth/state` режется по IP (~3–5 запросов → 429 на десятки
  минут). Поэтому `proxy_strategy=dc_rotating`: свежий IP на каждый аккаунт. Статичный
  ISP выжигается за 2–3 аккаунта.
- **Профили.** `data/profiles/<login>` — постоянный профиль Camoufox на аккаунт:
  повторный прогон пропускает GitHub-логин и не жжёт TOTP.

## Конфиг GitHub-пула

`register/config.json → gh_pool_file` — файл, по строке на аккаунт:

```
email:password:TOTP_SECRET:recovery_codes:username
```

`pool_min_index` задаёт, с какого индекса начинать (если часть строк уже занята другими
реггерами). Пустые и битые строки пропускаются. PAT-файл (`gh_pats_file`) не нужен для
OAuth — оставьте пустым.

## Мост

- `POST /v1/chat/completions` — OpenAI chat, stream и non-stream.
- `POST /v1/responses` — OpenAI Responses API.
- `POST /v1/embeddings` — эмбеддинги.
- `GET /v1/models`, `GET /v1/balance`, `GET /health`, `GET /admin/status`,
  `POST /admin/reload`.
- Стратегия `least_used`, ретраи, кулдауны 429/5xx, парковка исчерпанных ключей.

## Ограничения

- Только для собственных/разрешённых аккаунтов и тестов. Не абузь сервисы.
- GitHub-аккаунты, прокси и почта — ваши; в репозитории их нет.
- Мост и панель слушают `127.0.0.1` — наружу не выставляйте без авторизации.

## Лицензия

MIT + условие добросовестного использования — см. [LICENSE](LICENSE).