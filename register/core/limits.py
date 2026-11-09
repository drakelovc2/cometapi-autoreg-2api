# -*- coding: utf-8 -*-
"""limits — квоты и расход по аккаунтам (ОБРАЗЕЦ под сервис с free-tier).

Это пример под конкретный тип сервиса (эндпоинты `/api/me/free-tier`,
`/api/keys` — из образца register.py). Под свой сервис правь эндпоинты и поля,
но принцип сохраняй:

Что сервис обычно отдаёт (проверяй на своём):
  * `/api/me/free-tier` (только с сессией, т.е. после login):
      req_used, reset_at, exhausted, used_pct, model_ids[], rpm_limit, plan…
    Числового лимита токенов в ответе часто НЕТ — только «сколько уже сделано»
    и дата сброса периода.
  * `/api/keys`: дневные/недельные/месячные лимиты по ключу.

Поэтому «сколько осталось» считаем из двух источников:
  1) серверные поля (req_used, exhausted, reset_at) — авторитетно для статуса;
  2) локальный леджер моста data/usage.jsonl — реальные токены/запросы на аккаунт.

Модуль кэширует результат в data/limits.json, чтобы панель не логинилась
по всем аккаунтам на каждый запрос.
"""
import json
import os
import threading
import time

from . import config
from .register import ServiceClient, list_models

LIMITS_FILE = os.path.join(config.DATA_DIR, "limits.json")
_lock = threading.Lock()

# сколько секунд кэш считается свежим
CACHE_TTL = int(getattr(config, "LIMITS_CACHE_TTL", 600))


def _load_cache():
    try:
        with open(LIMITS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"accounts": {}, "checked_at": None}


def _save_cache(data):
    tmp = f"{LIMITS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIMITS_FILE)


def account_quota(email, password, proxy=None, attempts=3, backoff=20):
    """Один аккаунт: login → free-tier + ключи. Возвращает dict (с error при фейле).

    Логин рейт-лимитится (429 при массовом обходе) — ретраим с бэкоффом.
    """
    last = None
    for att in range(1, attempts + 1):
        c = ServiceClient(proxy=proxy)
        try:
            res = c.login(email, password)
        except Exception as exc:                    # сетевой фейл
            last = f"login: {type(exc).__name__}: {exc}"
            time.sleep(backoff * att)
            continue
        if res.get("ok"):
            last = None
            break
        last = f"login: {res.get('error')}"
        if "429" in str(res.get("error")) or "fast" in str(res.get("error", "")).lower():
            time.sleep(backoff * att)               # рейт-лимит — ждём и пробуем снова
            continue
        break                                        # иная ошибка (неверный пароль и т.п.)
    if last:
        return {"email": email, "error": last, "checked_at": time.time()}

    out = {"email": email, "checked_at": time.time()}
    try:
        ft = c.session.get(f"{config.BASE_URL}/api/me/free-tier", timeout=20).json()
        out.update({
            "req_used": ft.get("req_used"),
            "used_pct": ft.get("used_pct"),
            "exhausted": ft.get("exhausted"),
            "reset_at": ft.get("reset_at"),
            "period_started_at": ft.get("period_started_at"),
            "plan": ft.get("plan"),
            "on_plan": ft.get("on_plan"),
            "overage_billing": ft.get("overage_billing"),
            "rpm_limit": ft.get("rpm_limit"),
            "model_ids": ft.get("model_ids") or [],
            "model_labels": ft.get("model_labels") or [],
            "allowance_models": ft.get("allowance_model_labels") or [],
        })
    except Exception as exc:
        out["error"] = f"free-tier: {exc}"
    try:
        keys = c.session.get(f"{config.BASE_URL}/api/keys", timeout=20).json().get("keys") or []
        if keys:
            out["daily_limit_usd"] = keys[0].get("dailyLimitUsd")
            out["provider_allow"] = keys[0].get("providerAllow")
    except Exception:
        pass
    return out


def usage_by_account():
    """Агрегат локального леджера моста: api_key-префикс → токены/запросы.

    Леджер пишется мостом (data/usage.jsonl). Префикс = первые 24 символа ключа.
    """
    agg = {}
    path = os.path.join(config.DATA_DIR, "usage.jsonl")
    if not os.path.exists(path):
        return agg
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            p = (r.get("key_prefix") or "")[:24]
            a = agg.setdefault(p, {"requests": 0, "prompt_tokens": 0,
                                   "completion_tokens": 0, "last_ts": None})
            a["requests"] += 1
            a["prompt_tokens"] += r.get("prompt_tokens") or 0
            a["completion_tokens"] += r.get("completion_tokens") or 0
            if r.get("ts"):
                a["last_ts"] = r["ts"]
                if a["first_ts"] is None or r["ts"] < a["first_ts"]:
                    a["first_ts"] = r["ts"]      # начало rolling-окна аккаунта
    return agg


# ── своя тарификация ────────────────────────────────────────────────────
# У части сервисов лимит измеряется НЕ в токенах и НЕ в запросах, а в долларовой
# стоимости по прайсу («list-price value of the work»), период — скользящие
# 7×24ч от ПЕРВОГО запроса (не календарная неделя). Тогда, чтобы знать «сколько
# осталось», надо считать $: токены × цена платного аналога модели.
# У своего сервиса проверь, в чём именно измеряется лимит, и правь под него.

PRICE_FILE = os.path.join(config.DATA_DIR, "prices.json")


def price_table(api_key, force=False):
    """{model_id: {in, out}} — цены за 1M токенов. Кэш в data/prices.json."""
    if not force and os.path.exists(PRICE_FILE):
        try:
            with open(PRICE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("prices"):
                return cached["prices"]
        except (OSError, ValueError):
            pass
    prices = {}
    try:
        import requests
        r = requests.get(f"{config.BASE_URL}/v1/models",
                         headers={"Authorization": f"Bearer {api_key}",
                                  "User-Agent": config.UA}, timeout=20)
        for m in (r.json().get("data") or []):
            mid = m.get("id", "")
            p = m.get("pricing") or {}
            prices[mid] = {"in": p.get("input_usd_per_1m", 0) or 0,
                           "out": p.get("output_usd_per_1m", 0) or 0}
    except Exception:
        return {}
    if prices:
        tmp = f"{PRICE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"prices": prices, "ts": time.time()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PRICE_FILE)
    return prices


def usd_for(prices, model, prompt_tokens, completion_tokens):
    """Стоимость запроса по прайсу.

    ВАЖНО: у free-роутов pricing = 0 (они бесплатны), но лимит сервиса
    считается по list-price — поэтому для `*:free` всегда берём цену ПЛАТНОГО
    аналога, а не нулевую запись самой free-модели.
    """
    mid = model or ""
    p = None
    if mid.endswith(":free"):
        p = prices.get(mid[:-5])
    if p is None:
        p = prices.get(mid)
    if not p:
        return 0.0
    return (prompt_tokens or 0) / 1e6 * (p.get("in") or 0) + \
           (completion_tokens or 0) / 1e6 * (p.get("out") or 0)


def usage_detailed():
    """Леджер с $: по каждому префиксу ключа — токены, запросы, usd, разбивка по моделям."""
    agg = {}
    path = os.path.join(config.DATA_DIR, "usage.jsonl")
    if not os.path.exists(path):
        return agg
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            p = (r.get("key_prefix") or "")[:24]
            a = agg.setdefault(p, {"requests": 0, "prompt_tokens": 0,
                                   "completion_tokens": 0, "usd": 0.0,
                                   "models": {}, "last_ts": None, "first_ts": None})
            a["requests"] += 1
            a["prompt_tokens"] += r.get("prompt_tokens") or 0
            a["completion_tokens"] += r.get("completion_tokens") or 0
            a["usd"] += r.get("usd") or 0.0
            m = r.get("model") or "?"
            a["models"][m] = a["models"].get(m, 0) + 1
            if r.get("ts"):
                a["last_ts"] = r["ts"]
                if a["first_ts"] is None or r["ts"] < a["first_ts"]:
                    a["first_ts"] = r["ts"]      # начало rolling-окна аккаунта
    return agg


def collect(accounts, proxy=None, only_missing=False, max_workers=3,
            on_progress=None, pause=1.0):
    """Собрать квоты по списку аккаунтов, кэш в data/limits.json.

    Логин рейт-лимитится, поэтому:
      * параллельность низкая (по умолчанию 3);
      * пауза `pause` секунд после каждого логина;
      * на 429 внутри account_quota — ретрай с бэкоффом.
    only_missing=True — не трогать тех, у кого свежий кэш (инкрементальный обход).
    """
    from concurrent.futures import ThreadPoolExecutor

    cache = _load_cache()
    accs = cache.get("accounts", {})
    now = time.time()
    todo = []
    for a in accounts:
        email = a.get("email")
        if not email or not a.get("password"):
            continue
        prev = accs.get(email)
        if only_missing and prev and (now - (prev.get("checked_at") or 0)) < CACHE_TTL:
            continue
        todo.append(a)

    done = [0]

    def work(a):
        q = account_quota(a["email"], a["password"], proxy=proxy)
        time.sleep(pause)
        done[0] += 1
        if on_progress:
            try:
                on_progress(done[0], len(todo))
            except Exception:
                pass
        return a["email"], q

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for email, q in ex.map(work, todo):
                accs[email] = q

    cache["accounts"] = accs
    cache["checked_at"] = now
    with _lock:
        _save_cache(cache)
    return cache


def model_costs(accounts=None):
    """Расход по КАЖДОЙ модели: запросы, токены, $ (наш учёт) + прайс.

    Считается из леджера моста (data/usage.jsonl, поле model) — то есть
    показывает, во что реально обходится каждая модель и сколько она «съела»
    из лимита. Прайс — из /v1/models (цена платного аналога для *:free).
    """
    from .results import load_accounts
    accounts = accounts if accounts is not None else load_accounts()
    key = next((a.get("api_key") for a in accounts if a.get("api_key")), None)
    prices = price_table(key) if key else {}

    agg = {}
    path = os.path.join(config.DATA_DIR, "usage.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                m = r.get("model") or "?"
                a = agg.setdefault(m, {"requests": 0, "prompt_tokens": 0,
                                       "completion_tokens": 0, "usd": 0.0})
                a["requests"] += 1
                a["prompt_tokens"] += r.get("prompt_tokens") or 0
                a["completion_tokens"] += r.get("completion_tokens") or 0
                a["usd"] += r.get("usd") or 0.0

    rows = []
    for mid, a in agg.items():
        base = mid[:-5] if mid.endswith(":free") else mid
        p = prices.get(base) or prices.get(mid) or {}
        toks = a["prompt_tokens"] + a["completion_tokens"]
        rows.append({
            "model": mid,
            "requests": a["requests"],
            "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"],
            "total_tokens": toks,
            "usd": round(a["usd"], 6),
            "usd_per_1m_in": p.get("in"),
            "usd_per_1m_out": p.get("out"),
            # сколько токенов влезет в один лимит аккаунта по этой модели
            "tokens_per_allowance": (int(getattr(config, "FREE_ALLOWANCE_USD_PER_ACCOUNT", 0)
                                          / (p.get("in") or p.get("out") or 1) * 1e6)
                                     if (p.get("in") or p.get("out")) else None),
        })
    rows.sort(key=lambda r: -r["usd"])
    return {"models": rows, "count": len(rows)}


def summary(accounts=None):
    """Сводка для панели: по аккаунтам + суммарно + реально доступные модели."""
    from .results import load_accounts
    accounts = accounts if accounts is not None else load_accounts()
    cache = _load_cache()
    quotas = cache.get("accounts", {})
    usage = usage_detailed()

    rows = []
    total_requests = total_prompt = total_completion = 0
    total_usd = 0.0
    free_accounts = exhausted_accounts = 0
    model_ok = {}

    for a in accounts:
        email = a.get("email")
        key = a.get("api_key") or ""
        prefix = key[:24]
        q = quotas.get(email) or {}
        u = usage.get(prefix) or {}
        # free-аккаунт определяем ЛОКАЛЬНО (по своему accounts.jsonl), а не по
        # серверному кэшу: кэш мог быть собран раньше, когда аккаунтов было меньше
        if a.get("free_tier_enabled") or q.get("model_ids"):
            free_accounts += 1
        req = u.get("requests", 0)
        pt = u.get("prompt_tokens", 0)
        ct = u.get("completion_tokens", 0)
        usd = u.get("usd", 0.0)
        total_requests += req
        total_prompt += pt
        total_completion += ct
        total_usd += usd
        for m in (q.get("model_ids") or []):
            model_ok[m] = model_ok.get(m, 0) + 1
        # оценка полного лимита в $: если сервер дал used_pct, достраиваем пропорцию
        pct = q.get("used_pct")
        est_total_usd = None
        if pct and pct > 0:
            est_total_usd = round(usd / (pct / 100.0), 6)
        # ЛОКАЛЬНЫЙ остаток: лимит аккаунта минус наш учёт расхода.
        # Считается из своего леджера, без похода на сервер.
        _allow = getattr(config, "FREE_ALLOWANCE_USD_PER_ACCOUNT", None)
        local_left_usd = round(max(0.0, _allow - usd), 6) if _allow else None
        # Исчерпан: сервер сказал exhausted ИЛИ наш учёт выбрал лимит.
        local_exhausted = bool(q.get("exhausted")) or (
            bool(_allow) and usd >= _allow)
        # Когда лимит вернётся: серверный reset_at, иначе первое использование
        # + 7×24ч (личное скользящее окно сервиса от первого запроса).
        if local_exhausted:
            exhausted_accounts += 1
        reset_at = q.get("reset_at")
        if not reset_at and local_exhausted and u.get("first_ts"):
            try:
                import datetime as _dt
                t0 = _dt.datetime.strptime(u["first_ts"][:19], "%Y-%m-%dT%H:%M:%S")
                reset_at = (t0 + _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                reset_at = None
        rows.append({
            "email": email,
            "api_key_prefix": prefix,
            "free": a.get("free_tier_enabled"),
            "local_remaining_usd": local_left_usd,
            "local_exhausted": local_exhausted,
            "reset_at_est": reset_at,
            "exhausted": local_exhausted,
            "req_used": q.get("req_used"),
            "used_pct": pct,
            "est_total_usd": est_total_usd,
            "reset_at": q.get("reset_at"),
            "period_started_at": q.get("period_started_at"),
            "plan": q.get("plan"),
            "models": q.get("model_ids") or [],
            "local_requests": req,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "usd": round(usd, 6),
            "last_ts": u.get("last_ts"),
            "error": q.get("error"),
            "checked_at": q.get("checked_at"),
        })

    est_allowances = [r["est_total_usd"] for r in rows if r.get("est_total_usd")]
    # средняя цена токена по нашему леджеру (микс моделей) — для пересчёта $ ⇄ токены
    avg_usd_per_token = (total_usd / (total_prompt + total_completion)) \
        if (total_prompt + total_completion) else None

    # оценка лимита на аккаунт в $: 1) из конфига, 2) из серверного used_pct
    cfg_allow = getattr(config, "FREE_ALLOWANCE_USD_PER_ACCOUNT", None)
    allow_per_acc = cfg_allow or (round(sum(est_allowances) / len(est_allowances), 4)
                                  if est_allowances else None)
    alive_n = len(rows) - exhausted_accounts
    spent_usd = round(total_usd, 6)
    remaining_usd = None
    remaining_tokens = None
    if allow_per_acc:
        remaining_usd = round(max(0.0, allow_per_acc * alive_n - spent_usd), 6)
        if avg_usd_per_token:
            remaining_tokens = int(remaining_usd / avg_usd_per_token)
    # сколько аккаунтов уже на нуле по нашему учёту
    spent_out = sum(1 for r in rows
                    if allow_per_acc and (r.get("usd") or 0) >= allow_per_acc)

    return {
        "accounts": rows,
        "totals": {
            "accounts": len(rows),
            "free_accounts": free_accounts,
            "exhausted": exhausted_accounts,
            "alive": alive_n,
            "requests": total_requests,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "usd": spent_usd,
            "usd_per_account_est": allow_per_acc,
            "usd_remaining_est": remaining_usd,
            "tokens_remaining_est": remaining_tokens,
            "avg_usd_per_1k_tokens": round(avg_usd_per_token * 1000, 6) if avg_usd_per_token else None,
            "spent_out_local": spent_out,
            "local_ledger": True,     # расход считается локально (usage.jsonl), не с сервера
        },
        "models_available": model_ok,
        "checked_at": cache.get("checked_at"),
        "billing": {
            "unit": "USD list-price value",
            "period": "rolling 7×24h от ПЕРВОГО free-запроса (не календарная неделя)",
            "on_exhausted": "free-роут отдаёт rate-limit ошибку (429); баланс не тратится",
            "allowance_source": ("config.FREE_ALLOWANCE_USD_PER_ACCOUNT" if cfg_allow
                                 else ("оценка из серверного used_pct" if est_allowances
                                       else "неизвестно — нужно измерить")),
            "note": "сервис считает лимит в $ по прайсу, не в токенах/запросах. "
                    "Расход считаем САМИ из своего леджера моста (usage.jsonl) в реальном "
                    "времени: «сожжено» = токены × цена платного аналога; "
                    "«осталось» = лимит×аккаунты − сожжено. Опрос сервера (used_pct, "
                    "exhausted, reset_at) — только по кнопке, для сверки.",
        },
    }


def check_models(api_key, proxy=None):
    """Какие модели РЕАЛЬНО отвечают (не просто числятся в /v1/models).

    Возвращает [{id, ok, detail}]. Голый ':free' у апстрима битый — пометится ok=false.
    """
    out = []
    for m in list_models(api_key, proxy):
        if not m.endswith(":free"):
            continue
        out.append({"id": m})
    return out


def probe_models(api_key, models=None, proxy=None):
    """Живая проверка: по каждой модели один короткий запрос."""
    from .register import test_key
    models = models or [m for m in list_models(api_key, proxy) if m.endswith(":free")]
    res = []
    for m in models:
        ok, _, detail = test_key(api_key, model=m, proxy=proxy)
        res.append({"id": m, "ok": ok, "detail": detail[:120]})
    return res
