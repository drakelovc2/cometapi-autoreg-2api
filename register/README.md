# cometapi-register

Регистратор аккаунтов **CometAPI** (`www.cometapi.com`) через GitHub OAuth.
Один прогон = один GitHub-аккаунт из пула → сессия CometAPI → ключ `sk-…` →
живой `chat/completions`. Новому аккаунту начисляется **750 000 единиц квоты ≈ $1.50**.

Панель: `http://127.0.0.1:9935`.

## Установка

```bash
cp config.example.json config.json
# gh_pool_file → путь к вашему пулу GitHub-аккаунтов
pip install -r requirements.txt
./start.sh                 # или: bash start.sh --no-browser
```

Для Camoufox один раз скачивается браузер:

```bash
python3 -m camoufox fetch
```

## Формат пула GitHub

`gh_pool_file` — текстовый файл, по строке на аккаунт:

```
email:password:TOTP_SECRET:recovery_codes:username
```

- `TOTP_SECRET` — base32-секрет 2FA (pyotp).
- `recovery_codes` — необязательное поле, можно пустым.
- `pool_min_index` — с какого индекса начинать (если первые строки заняты
  другими реггерами).
- Битые/пустые строки пропускаются.

## Флоу (что происходит внутри)

1. `GET /console/login` — React SPA, ставит session-cookie.
2. `POST /api/oauth/pre-check` с Castle-токеном — **обязательный клик** по
   «Continue with GitHub». Без него новый аккаунт получает
   `blocked by security check`.
3. GitHub: `login` + `password` → `input[name=app_otp]` (TOTP) → сессия.
4. Consent: две кнопки `name=authorize`; жать только `value=1`.
5. Callback `/console/oauth/github?code&state` — SPA сама обменивает code.
6. `GET /api/organization/` → id организации.
7. `POST /api/token/` + `X-Organization-Id` → полный ключ в `data.key`;
   для `/v1` = `"sk-" + data.key`.
8. `GET /api/organization/detail?id=<org>` → `quota` (бонус приходит асинхронно,
   первый запрос может отдать 0).
9. Проверка: `POST https://api.cometapi.com/v1/chat/completions`,
   `gpt-4o-mini-2024-07-18` (~2 единицы квоты).

## Панель

- сводка: аккаунты, живые ключи, последний прогон;
- запуск N аккаунтов в фоне и мягкий стоп;
- перепроверка ключей (`test_key`);
- живой `/v1/models` по первому ключу;
- хвост логов и состояние пула моста.

## CLI

```bash
python3 -c "import sys;sys.path.insert(0,'.');from core import register;print(register.register_one(index=0))"
```

Полезные функции: `register_one`, `test_key`, `list_models`, `free_models`,
`ServiceClient`, `load_pool`, `pick_account`.

## Данные

| Файл | Что внутри |
|---|---|
| `data/accounts.jsonl` | аккаунты: email, ключ, org/user id, quota, usd_left |
| `data/accounts.txt` | combo `email\|\|sk-…` |
| `data/profiles/<login>/` | постоянный профиль Camoufox (повторный прогон не жжёт TOTP) |
| `data/panel.log` | лог панели |

Всё это в `.gitignore`.

## Грабли (проверено)

- `/api/oauth/state` режется по IP: ~3–5 запросов → 429 на десятки минут.
  Лечится только сменой IP; поэтому по умолчанию `dc_rotating`.
- Клик по GitHub до инициализации Castle SDK → `window.open(null)` →
  вкладка `/console/null`. Ждать кнопку и ~4 секунды.
- `button[name=authorize][value='1']` в HTML `disabled` — Playwright-клик
  всё равно проходит; чистый POST с `authorize=1` тоже работает.
- TOTP на границе 30-секундного окна флапает — ждать свежее окно. Много
  подряд отправок кода → GitHub `too many codes` → бэкофф 90 секунд.
- `/api/organization` без слэша → `301`; `/api/token/` без
  `X-Organization-Id` → `400 organization context required`.
- `user.quota` всегда `0`; реальный баланс — `org.quota`.
- `gpt-oss-20b-free` в каталоге с ценой 0, но живой запрос отдаёт
  `503 no available channel` — ключ им не проверяем.

## Ограничения

Только собственные/разрешённые GitHub-аккаунты. Прокси, пул и ключи — ваши;
в репозитории их нет. Панель слушает `127.0.0.1`.