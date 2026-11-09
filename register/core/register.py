# -*- coding: utf-8 -*-
"""cometapi-register — ядро: GitHub OAuth → CometAPI → API-ключ → живая проверка.

Живая разведка 04.11.2026 (camoufox + снятые сетевые запросы). CometAPI —
форк new-api (console — React SPA на /console/, API аккаунта — /api/*,
OpenAI-совместимый шлюз — https://api.cometapi.com/v1).

Цепочка (ОДИН браузерный контекст на аккаунт + requests на API аккаунта):

  1. GET  www.cometapi.com/console/login          → SPA, ставит session-cookie
  2. fetch /api/oauth/state                        → state (привязан к session)
       ⚠ сервер режет по IP: 429 → бэкофф, а лучше прокси (ISP BRD проходит)
  3. github.com/login → login + password           → /sessions/two-factor/app
     POST app_otp = pyotp.TOTP(secret).now()       → github.com/ (сессия)
  4. GET  github.com/login/oauth/authorize?client_id=Ov23li07lGot0Z7F8WDv
          &state=…&scope=user:email
       страница consent: ДВЕ кнопки name=authorize — Cancel(0) и Authorize(1).
       Жмём ТОЛЬКО value=1 (или уже авторизовано → сразу 302).
  5. → www.cometapi.com/console/oauth/github?code=…&state=…
       SPA сама делает GET /api/oauth/github?code&state и ставит сессию.
       ⚠ Чистый requests на этот GET получает «blocked by security check»
       (Castle/fingerprint) — поэтому шаг делается браузером, а не HTTP.
  6. GET  /api/organization                        → org id (у нового юзера своя)
  7. POST /api/token/ + X-Organization-Id          → data.key (полный ключ!)
       В списке /api/token/ ключ маскирован, полный отдаётся ТОЛЬКО при создании.
       API-ключ для /v1 = "sk-" + data.key.
  8. GET  /api/organization/detail?id=<org>        → quota (знак-бонус ~750000
       единиц ≈ $1.50), used_quota, request_count.
  9. Проверка живости — отдельный шаг: POST api.cometapi.com/v1/chat/completions
       дешёвой моделью (gpt-4o-mini-2024-07-18, ~2 единицы квоты за запрос).

Контракт шаблона (импортируют panel/server.py и core/limits.py):
    register_one(), test_key(), list_models(), free_models(), ServiceClient.

Секреты: креды GitHub-пула читаются ТОЛЬКО из файла, указанного в
config.json → gh_pool_file, пароли/TOTP/ключи НЕ логируются,
в консоль и отчёты уходят только маски. config.json в .gitignore.
"""
import json
import os
import re
import string
import time
from urllib.parse import urlparse

import requests

from . import config

# ── константы флоу (из ЖИВОГО recon 04.11.2026, а не из головы) ───────────
BASE = "https://www.cometapi.com"          # console + API аккаунта (new-api)
API = "https://api.cometapi.com"           # OpenAI-совместимый шлюз
GITHUB = "https://github.com"

CLIENT_ID = "Ov23li07lGot0Z7F8WDv"         # GitHub OAuth App «comet-api-login»
REDIRECT_PATH = "/console/oauth/github"    # зарегистрирован в настройках OAuth App
SCOPE = "user:email"

# Дешёвая модель для проверки ключа: ~2 единицы квоты (~$0.000005) за запрос.
# Голый free-роут gpt-oss-20b-free в каталоге есть, но отдаёт 503
# «no available channel» — проверять им нельзя.
DEFAULT_TEST_MODEL = "gpt-4o-mini-2024-07-18"
KEY_TEST_FALLBACKS = ["gpt-5-nano", "gpt-4.1-nano-2025-04-14", "hy4"]

# Сколько единиц квоты в $1 (new-api QuotaPerUnit). Проверено расчётом:
# 13 prompt + 5 completion у gpt-4o-mini ($0.15/$0.60 за 1M, ratio 0.8)
# стоили 2 единицы → ~500000 ед./$1.
QUOTA_PER_USD = 500000.0


class ServiceError(Exception):
    def __init__(self, message, retryable=False, ip_burned=False):
        super().__init__(message)
        self.retryable = retryable
        self.ip_burned = ip_burned


def _step(msg, on_step=None, **fields):
    text = msg
    if fields:
        text += " " + json.dumps(fields, ensure_ascii=False)
    if on_step:
        on_step(text)
    else:
        print("  " + text, flush=True)


def mask(s, head=8, tail=4):
    """Маска для логов/отчётов. Полный ключ НИКОГДА не уходит в консоль."""
    if not s:
        return ""
    s = str(s)
    return s[:head] + "…" + s[-tail:] if len(s) > head + tail + 2 else s[:head] + "…"


# ── пул GitHub-аккаунтов ──────────────────────────────────────────────────

def load_pool(path=None):
    """gitaccs.txt: email:password:TOTP_SECRET:recovery_codes:username.

    Читаем ТОЛЬКО из Python (пароли со спецсимволами). Строки с неполным
    числом полей пропускаем. PAT-файл all_pats.txt НЕ используется:
    PAT — только проверка живости, для OAuth не нужен.
    """
    path = path or config.get("gh_pool_file")
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) != 5:
                continue
            email, password, totp, recovery, username = parts
            if not (username and totp and password):
                continue
            out.append({"email": email, "password": password,
                        "totp": totp.strip().upper().replace(" ", ""),
                        "recovery": recovery, "username": username,
                        "pool_index": i})
    return out


def used_usernames():
    from . import results
    try:
        return {a.get("github_username") for a in results.load_accounts()
                if a.get("github_username")}
    except Exception:
        return set()


def pick_account(index=None):
    """Свободный GitHub-аккаунт из пула.

    ЖЕЛЕЗНОЕ ПРАВИЛО: индексы 0 (piapi), 1-2 (wavespeed) заняты — их не жжём.
    Нижняя граница берётся из config.pool_min_index (по умолчанию 3).
    """
    min_idx = int(config.get("pool_min_index", 3) or 0)
    pool = [a for a in load_pool() if a["pool_index"] >= min_idx]
    if index is not None:
        exact = [a for a in pool if a["pool_index"] == index]
        return exact[0] if exact else None
    used = used_usernames()
    free = [a for a in pool if a["username"] not in used]
    return free[0] if free else None


# ── HTTP-сессия к CometAPI (cookie ставит браузер) ────────────────────────

def _proxy_dict(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def _effective_proxy(proxy=None):
    """Прокси под CometAPI: свежий DC-IP на аккаунт (session-ротация BRD).

    Причина: /api/oauth/state лимитируется по IP — после нескольких запросов
    отдаёт 429 на десятки минут. Статичный ISP-IP выжигается за 2-3 аккаунта,
    да и одиночный DC-IP тоже. Поэтому proxy_strategy="dc_rotating" берёт
    СЛУЧАЙНУЮ разрешённую DC-зону и случайный -session-<id>: у зоны
    datacenter_proxy3 пул ~20 адресов (проверено: state 200, pre-check allowed).
    gh_static (opt_in, pay-per-GB) в список по умолчанию НЕ входит.
    Стратегия "external" — использовать переданный панелью proxy как есть.
    """
    strat = (config.get("proxy_strategy") or "external").lower()
    if strat == "dc_rotating":
        import random
        zones = list(config.BRD_ZONES or [])
        allow = config.get("dc_zones_allow") or []
        if allow:
            zones = [z for z in zones if z.get("zone") in allow]
        else:
            zones = [z for z in zones
                     if "static" not in str(z.get("zone"))
                     and not str(z.get("zone")).startswith("gh_")]
        if zones:
            z = random.choice(zones)
            sess = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            cust = config.get("brd_customer") or ""
            host = config.get("brd_host") or "brd.superproxy.io:44445"
            zone = str(z["zone"])
            if z.get("country"):
                zone += f"-country-{z['country']}"
            return (f"http://brd-customer-{cust}-zone-{zone}-session-{sess}"
                    f":{z['password']}@{host}")
    if proxy:
        return proxy
    try:
        from . import proxy as proxy_mod
        rot = proxy_mod.make_rotator()
        if len(rot):
            return rot.next()
    except Exception:
        pass
    return None


def _session(proxy=None, cookies=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": config.get("ua") or config.UA,
        "Accept": "application/json",
        "Origin": BASE,
        "Referer": BASE + "/console/",
    })
    if proxy:
        s.proxies = _proxy_dict(proxy)
    if cookies:
        s.cookies.update(cookies)
    return s


def _api(session, path, method="GET", payload=None, org=None):
    """Вызов API аккаунта new-api. Возвращает (status, json|text)."""
    headers = {}
    if org:
        headers["X-Organization-Id"] = str(org)
    r = session.request(method, BASE + path, json=payload, headers=headers,
                        timeout=40, allow_redirects=False)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_text": r.text[:250]}


# ── GitHub-логин и OAuth (в браузере) ────────────────────────────────────

def _browser_proxy(proxy):
    if not proxy:
        return None
    from . import proxy as proxy_mod
    return proxy_mod.browser_proxy_dict(proxy)


_TZ_BY_COUNTRY = {
    "nl": "Europe/Amsterdam", "us": "America/New_York", "gb": "Europe/London",
    "de": "Europe/Berlin", "fr": "Europe/Paris", "pl": "Europe/Warsaw",
    "ca": "America/Toronto", "sg": "Asia/Singapore", "jp": "Asia/Tokyo",
}


def _browser_timezone(proxy):
    """Часовой пояс под страну выхода прокси (без geoip-extra camoufox).

    Нужен, чтобы timezone браузера не противоречил IP: geoip требует
    `pip install camoufox[geoip]`, поэтому страну берём из строки BRD-зоны
    (`-country-nl`) и подставляем в camoufox config.
    """
    default = config.get("browser_timezone") or "Europe/Amsterdam"
    if not proxy:
        return default
    m = re.search(r"-country-([a-z]{2})", str(proxy))
    if not m:
        return default
    return _TZ_BY_COUNTRY.get(m.group(1), default)


def _wait(page, predicate, timeout=60, interval=1.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        # sync API: pump the dispatcher so page.url/navigation events refresh
        page.wait_for_timeout(int(interval * 1000))
    return False


def _gh_logged_in(page):
    """Залогинен ли GitHub — по DOM, а не по URL (URL врёт во время редиректов)."""
    try:
        return bool(page.evaluate(
            "()=>{const m=document.querySelector('meta[name=user-login]');"
            "return !!(m&&m.content);}"))
    except Exception:
        return False


def _gh_has_otp(page):
    try:
        return bool(page.query_selector("input[name=app_otp]"))
    except Exception:
        return False


def _gh_login(page, acct, on_step=None, navigate=True):
    """Вход в GitHub: login + password, затем TOTP (app_otp).

    navigate=False — НЕ уходим на /login: страница уже может быть
    github.com/login?client_id=…&return_to=<authorize>. Тогда после логина
    GitHub сам вернёт на authorize и подставит state.

    429 «too many codes» — GitHub лимитирует ЧИСЛО отправок кода, поэтому
    бэкофф 90с по умолчанию и не больше gh_totp_attempts попыток.
    """
    if navigate and not page.query_selector("#login_field") \
            and not _gh_has_otp(page) and not _gh_logged_in(page):
        page.goto(GITHUB + "/login", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1200)
    if _gh_logged_in(page):
        _step("GitHub: сессия уже жива (профиль)", on_step)
        return
    if page.query_selector("#login_field"):
        page.fill("#login_field", acct["username"])
        page.fill("#password", acct["password"])
        page.keyboard.press("Enter")
        # ждём именно появления OTP-поля (URL в процессе редиректа врёт)
        _wait(page, lambda: _gh_has_otp(page) or _gh_logged_in(page), timeout=60)
    attempts = int(config.get("gh_totp_attempts", 3) or 3)
    backoff = float(config.get("gh_totp_backoff_sec", 90) or 90)
    for i in range(attempts):
        if not _gh_has_otp(page):
            break
        body = (page.inner_text("body") or "").lower()
        if "too many codes" in body or "rate limit" in body:
            _step(f"GitHub: 429 «too many codes», бэкофф {int(backoff)}с", on_step)
            time.sleep(backoff)
            if not _gh_has_otp(page):
                break
            continue
        page.wait_for_selector("input[name=app_otp]", timeout=30000)
        # TOTP на границе окна флапает: дожидаемся свежих 30 секунд
        rem = 30 - (time.time() % 30)
        if rem < 8:
            time.sleep(rem + 1)
        page.fill("input[name=app_otp]", _totp(acct["totp"]))
        page.wait_for_timeout(500)
        # кнопка Verify в HTML disabled (включается JS): Enter по полю —
        # рабочий путь, JS-клик по форме — дубль на случай иной вёрстки.
        try:
            page.press("input[name=app_otp]", "Enter")
        except Exception:
            page.keyboard.press("Enter")
        try:
            page.evaluate("document.querySelector('button[type=submit]')?.click()")
        except Exception:
            pass
        _wait(page, lambda: not _gh_has_otp(page), timeout=45)
        if _gh_has_otp(page):
            body = (page.inner_text("body") or "").replace("\n", " ")[:200]
            _step(f"GitHub 2FA: попытка {i + 1} не принята — {body}", on_step)
        else:
            _step(f"GitHub 2FA: попытка {i + 1} ок", on_step)
            break
        time.sleep(backoff if i + 1 < attempts else 0)
    if _gh_has_otp(page):
        raise ServiceError("GitHub: 2FA не принят (TOTP/лимит кодов)", retryable=True)
    if "suspended" in (page.url or "").lower():
        raise ServiceError("GitHub-аккаунт SUSPENDED")
    _step("GitHub: сессия получена", on_step)


def _totp(secret):
    import pyotp
    return pyotp.TOTP(secret).now()


def _start_oauth_click(ctx, page, on_step=None):
    """Нажать «Continue with GitHub» на /console/login.

    ВАЖНО (набито 04.11.2026): новый аккаунт CometAPI создаётся ТОЛЬКО если
    перед OAuth клик по кнопке пройдёт серверный pre-check:
        POST /api/oauth/pre-check {"castle_request_token": <Castle token>}
        → {"allowed": true}
    Токен Castle генерит браузерный SDK; без него новый юзер получает
    {"message":"blocked by security check"} от /api/oauth/github
    (а существующий — логинится нормально).

    Возвращает страницу GitHub (новая вкладка/popup или сам login-таб).
    До 3 кликов: при pre-check 429/неготовом Castle приложение открывает
    window.open(null) → /console/null, это не ошибка GitHub.
    """
    tries = int(config.get("github_click_tries", 3) or 3)
    for attempt in range(1, tries + 1):
        try:
            page.get_by_role("button", name="Continue with GitHub").click(timeout=30000)
        except Exception as exc:
            raise ServiceError(f"OAuth: не нажалась кнопка GitHub ({str(exc)[:100]})",
                               retryable=True)
        _step(f"OAuth: клик по GitHub #{attempt} (Turnstile/Castle в браузере)", on_step)
        end = time.time() + float(config.get("github_open_timeout_sec", 45) or 45)
        while time.time() < end:
            for p in list(ctx.pages):
                try:
                    if "github.com" in (p.url or ""):
                        _step("OAuth: GitHub открыт", on_step,
                              path=urlparse(p.url).path[:40])
                        return p
                except Exception:
                    continue
            try:
                if "github.com" in (page.url or ""):
                    return page
            except Exception:
                pass
            # sync API: time.sleep would block the dispatcher, so page.url /
            # ctx.pages stay stale until the timeout; pump the event loop.
            page.wait_for_timeout(700)
        _step("OAuth: GitHub не открылся (pre-check/Castle), повтор клика", on_step)
        page.wait_for_timeout(3000)
    raise ServiceError("OAuth: GitHub не открылся после кликов "
                       "(pre-check/Castle не пропустил)", retryable=True)


def _gh_approve(page, on_step=None):
    """Подтвердить consent (authorize=1) и дождаться callback CometAPI.

    Если приложение уже авторизовано ранее, GitHub показывает страницу
    «You are being redirected…» БЕЗ кнопки Authorize и сам уводит на
    redirect_uri. Это не ошибка: просто ждём callback. Раньше такой случай
    падал с «нет кнопки Authorize» и аккаунт сгорал зря.
    """
    page.wait_for_timeout(1500)
    try:
        on_authorize = ("cometapi.com" not in page.url
                        and "/login/oauth/authorize" in page.url)
    except Exception:
        on_authorize = False          # навигация уже началась — значит уводит
    if on_authorize:
        try:
            btn = page.query_selector("button[name=authorize][value='1']")
            if not btn:
                btn = page.query_selector("button[name=authorize]:not([value='0'])")
        except Exception:
            btn = None                # execution context destroyed = редирект пошёл
        if btn:
            try:
                page.click("button[name=authorize][value='1']",
                           no_wait_after=True, timeout=20000)
            except Exception:
                try:
                    btn.click()
                except Exception:
                    page.keyboard.press("Enter")
            _step("OAuth: consent → authorize=1", on_step)
        else:
            # Уже авторизовано — GitHub сам редиректит, кнопки нет by design.
            _step("OAuth: consent не требуется (приложение уже авторизовано)",
                  on_step)
    ok = _wait(page, lambda: "cometapi.com" in page.url,
               timeout=float(config.get("callback_timeout_sec", 75) or 75))
    if not ok:
        raise ServiceError(f"OAuth: callback не пришёл ({page.url[:120]})",
                           retryable=True)
    _step("OAuth: callback получен", on_step, path=urlparse(page.url).path)


def _browser_session_alive(page):
    """Жива ли CometAPI-сессия в этом браузере (для повторного прогона)."""
    try:
        res = page.evaluate(
            """async()=>{try{const r=await fetch('/api/user/self',{credentials:'include'});
                 return [r.status];}catch(e){return [0];}}""")
        return res and res[0] == 200
    except Exception:
        return False


def _wait_comet_session(ctx, on_step=None, timeout=90):
    """После callback SPA обменивает code на сессию. Ждём /api/user/self 200.

    Проверяем ВСЕ страницы контекста: popup после редиректа может закрыться,
    а сессия — уже стоять.
    """
    end = time.time() + timeout
    while time.time() < end:
        for p in list(ctx.pages):
            try:
                if _browser_session_alive(p):
                    return True
            except Exception:
                continue
        time.sleep(2)
    return False


def _oauth_browser(acct, proxy=None, on_step=None, profile_dir=None):
    """Полный OAuth в браузере. Возвращает cookies cometapi (name→value).

    Браузер обязателен: /api/oauth/github проверяет fingerprint/Castle и
    Turnstile-сессию; чистый requests получает «blocked by security check»
    даже с валидными cookie.
    """
    from camoufox.sync_api import Camoufox

    opts = dict(headless=bool(config.get("headless", True)),
                locale="en-US", os="windows", humanize=True,
                # timezone ставим сами из -country-<cc> прокси (geoip-extra нет)
                i_know_what_im_doing=True)
    if proxy:
        opts["proxy"] = _browser_proxy(proxy)
        # geoip-extra не установлен → страну берём из -country-<cc> в строке
        opts["config"] = {"timezone": _browser_timezone(proxy)}
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        opts["persistent_context"] = True
        opts["user_data_dir"] = profile_dir

    with Camoufox(**opts) as br:
        ctx = br                      # persistent_context → сразу BrowserContext
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        oauth_seen = []
        page.on("response", lambda r: oauth_seen.append((r.status, r.url))
                if "/api/oauth/" in r.url else None)
        page.goto(BASE + "/console/login", wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)

        if _browser_session_alive(page):
            _step("CometAPI: сессия уже жива (профиль), OAuth пропущен", on_step)
        else:
            # ждём кнопку и даём Castle SDK (660 КБ) инициализироваться: клик
            # раньше готовности SDK даёт pre-check без токена → window.open(null)
            try:
                page.wait_for_selector("text=Continue with GitHub", timeout=45000)
            except Exception:
                raise ServiceError(
                    f"CometAPI: login-страница не отрисовалась ({page.url[:100]})",
                    retryable=True)
            page.wait_for_timeout(4000)
            gh = _start_oauth_click(ctx, page, on_step)
            _gh_login(gh, acct, on_step, navigate=False)
            _gh_approve(gh, on_step)
            if not _wait_comet_session(ctx, on_step,
                                       timeout=int(config.get("oauth_wait_sec", 90) or 90)):
                raise ServiceError("CometAPI: сессия не появилась после OAuth "
                                   f"(security check/Castle? url={gh.url[:100]}, "
                                   f"oauth={oauth_seen[-2:]})", retryable=True)
            _step("CometAPI: сессия получена", on_step)

        cookies = {c["name"]: c["value"] for c in ctx.cookies()
                   if "cometapi" in (c.get("domain") or "")}
    if not cookies.get("session"):
        raise ServiceError("CometAPI: нет session-cookie после OAuth", retryable=True)
    return cookies


def get_org(session, on_step=None):
    status, data = _api(session, "/api/organization/")
    if status != 200 or not data.get("success"):
        raise ServiceError(f"/api/organization: HTTP {status} {str(data)[:150]}")
    orgs = data.get("data") or []
    if not orgs:
        raise ServiceError("/api/organization: список пуст (OAuth не создал org?)")
    org = next((o for o in orgs if o.get("state") == "active"), orgs[0])
    return org["id"]


def get_balance(session, org):
    """quota/used_quota организации (знак-бонус новичка) + user."""
    out = {"org_id": org}
    status, data = _api(session, f"/api/organization/detail?id={org}", org=org)
    if status == 200 and data.get("success"):
        d = data.get("data") or {}
        quota = d.get("quota")
        out.update({
            "quota": quota,
            "used_quota": d.get("used_quota"),
            "request_count": d.get("request_count"),
            "usd_left": round(quota / QUOTA_PER_USD, 4) if isinstance(quota, (int, float)) else None,
            "usd_used": (round(d.get("used_quota") / QUOTA_PER_USD, 6)
                         if isinstance(d.get("used_quota"), (int, float)) else None),
            "state": d.get("state"),
        })
    status, data = _api(session, "/api/user/self")
    if status == 200 and data.get("success"):
        u = data.get("data") or {}
        out["user"] = {k: u.get(k) for k in
                       ("id", "username", "email", "quota", "used_quota",
                        "request_count", "github_id", "register_source")}
    return out


def create_api_key(session, org, name=None, on_step=None):
    """POST /api/token/ → полный ключ. В списке ключ маскирован навсегда.

    Один аккаунт = один ключ; дубликаты с тем же именем не мешают.
    """
    name = name or config.get("key_name") or "comet-reg"
    payload = {
        "name": name,
        "remain_quota": 0,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
        "group": "",
    }
    status, data = _api(session, "/api/token/", "POST", payload, org=org)
    if status != 200 or not data.get("success"):
        raise ServiceError(f"/api/token/: HTTP {status} {str(data)[:180]}")
    d = data.get("data") or {}
    raw = d.get("key")
    if not raw:
        raise ServiceError(f"/api/token/: нет key в ответе ({str(data)[:150]})")
    api_key = raw if raw.startswith("sk-") else "sk-" + raw
    _step("API-ключ создан", on_step, prefix=mask(api_key), key_id=d.get("id"),
          org=d.get("organization_id"))
    return api_key, d


def list_tokens(session, org):
    status, data = _api(session, "/api/token/?p=0&size=20", org=org)
    if status == 200 and data.get("success"):
        return (data.get("data") or {}).get("items") or []
    return []


# ── публичный контракт ────────────────────────────────────────────────────

def list_models(api_key, proxy=None):
    """Список моделей по ключу → [id, ...]. Пусто при ошибке."""
    try:
        r = requests.get(API + "/v1/models",
                         headers={"Authorization": f"Bearer {api_key}",
                                  "User-Agent": config.get("ua") or config.UA},
                         proxies=_proxy_dict(proxy), timeout=40)
        if r.ok:
            return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        pass
    return []


def free_models(api_key=None, proxy=None):
    """Бесплатных рабочих моделей сейчас нет.

    В каталоге числится gpt-oss-20b-free (input=output=0), но живой запрос
    отдаёт 503 «no available channel for group default» — на 04.11.2026
    канала нет. Поэтому возвращаем пусто: проверка идёт дешёвой платной
    моделью (2 единицы квоты из знак-бонуса).
    """
    return []


def test_key(api_key, model=None, proxy=None):
    """Живой chat/completions по ключу → (ok, model, detail).

    Один короткий запрос (max_tokens=16). Стоимость ~$0.000005.
    401/403 → ключ мёртв; 402/429 → нет средств/лимит; 503 → нет канала.
    """
    models = [model] if model else [config.get("key_test_model") or DEFAULT_TEST_MODEL]
    models += [m for m in (config.get("key_test_fallbacks") or KEY_TEST_FALLBACKS)
               if m not in models]
    last = None
    for m in models:
        try:
            r = requests.post(
                API + "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "User-Agent": config.get("ua") or config.UA},
                json={"model": m,
                      "messages": [{"role": "user", "content": "Say hi in 3 words"}],
                      "max_tokens": 16},
                proxies=_proxy_dict(proxy), timeout=60)
        except Exception as exc:
            return False, m, str(exc)[:150]
        if r.ok:
            try:
                txt = r.json()["choices"][0]["message"]["content"]
            except Exception:
                txt = ""
            return True, m, f"HTTP 200: {str(txt)[:120]}"
        detail = f"HTTP {r.status_code}: {r.text[:160]}"
        last = (m, detail)
        # ключ мёртв / нет баланса — другие модели не помогут
        if r.status_code in (401, 402, 403):
            return False, m, detail
        if r.status_code != 503 and r.status_code != 404:
            return False, m, detail
    m, detail = last or (models[0], "нет ответа")
    return False, m, detail


class ServiceClient:
    """Совместимость с core/limits.py (account_quota).

    У CometAPI НЕТ /api/me/free-tier и /api/keys (это контракт другого
    сервиса из шаблона): квота живёт в организации (`/api/organization/`),
    а ключи — в `/api/token/`. Поэтому login здесь честно отказывает, чтобы
    фон-сбор квот не поднимал по браузеру на каждый аккаунт. Баланс же
    пишется в `data/accounts.jsonl` при регистрации (`quota`, `usd_left`),
    а панель перепроверяет ключи через test_key.
    """

    def __init__(self, proxy=None, email=None, on_step=None):
        self.proxy = proxy
        self.email = email
        self.on_step = on_step
        self.session = None
        self.cookies = None

    def login(self, email, password=None):
        return {"ok": False,
                "error": "CometAPI: квота — в организации (org.quota), "
                         "/api/me/free-tier отсутствует; см. КОНТЕКСТ.md"}

    def close(self):
        if self.session:
            self.session.close()


# ── полный флоу одного аккаунта ───────────────────────────────────────────

def register_one(email=None, password=None, proxy=None, mail=None,
                 on_step=None, index=None, gh_account=None):
    """Один GitHub-аккаунт → CometAPI-аккаунт + ключ + баланс + live-проверка.

    email/password/mail оставлены в сигнатуре для совместимости с шаблоном:
    у CometAPI вход только через GitHub OAuth, своей почты не требуется.
    gh_account/index — ручной выбор аккаунта из пула (pool_min_index соблюдается).
    Возвращает dict; при ошибке — {"error": ...}. Секреты НЕ логируем.
    """
    if gh_account is None:
        gh_account = pick_account(index)
    if not gh_account:
        return {"error": "в пуле не осталось свободных GitHub-аккаунтов "
                         f"(pool_min_index={config.get('pool_min_index', 3)})"}

    rec = {"github_username": gh_account["username"],
           "github_email": gh_account["email"],
           "email": gh_account["email"],
           "pool_index": gh_account["pool_index"],
           "auth": "github_oauth",
           "password": "",
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    started = time.time()

    # прокси: см. _effective_proxy — для CometAPI по умолчанию свежий DC-IP
    proxy = _effective_proxy(proxy)
    if proxy:
        rec["proxy"] = mask(proxy, head=12, tail=6)

    profile_dir = None
    if config.get("use_profiles", True):
        safe = "".join(c if c.isalnum() or c in "-_@." else "_"
                       for c in gh_account["username"])[:60]
        profile_dir = os.path.join(config.DATA_DIR, "profiles", safe)

    try:
        _step(f"1/6 GitHub-логин + OAuth: {gh_account['username']}"
              f" (индекс {gh_account['pool_index']})", on_step)
        cookies = _oauth_browser(gh_account, proxy, on_step, profile_dir)

        _step("2/6 сессия CometAPI", on_step)
        session = _session(proxy, cookies)
        org = get_org(session, on_step)
        _step("3/6 организация", on_step, org_id=org)

        _step("4/6 баланс", on_step)
        bal = get_balance(session, org)
        uid = (bal.get("user") or {})
        rec.update({
            "user_id": uid.get("id"),
            "username": uid.get("username") or gh_account["username"],
            "email": uid.get("email") or gh_account["email"],
            "github_id": uid.get("github_id"),
            "org_id": org,
            "balance": bal,
            "quota": bal.get("quota"),
            "used_quota": bal.get("used_quota"),
            "usd_left": bal.get("usd_left"),
        })
        _step("баланс", on_step, quota=bal.get("quota"),
              used=bal.get("used_quota"), usd_left=bal.get("usd_left"))

        _step("5/6 создаю API-ключ", on_step)
        api_key, key_meta = create_api_key(session, org, on_step=on_step)
        rec.update({
            "api_key": api_key,
            "key_prefix": api_key[:10],
            "key_id": key_meta.get("id"),
            "key_masked": mask(api_key),
            "organization_id": key_meta.get("organization_id"),
        })

        _step("6/6 живой chat/completions", on_step)
        ok, model, detail = test_key(api_key, proxy=proxy)
        rec["key_live"] = bool(ok)
        rec["key_check"] = f"{model}: {detail}"
        rec["test_model"] = model
        _step("проверка ключа", on_step, ok=ok, model=model, detail=detail[:160])

        # Знак-бонус начисляется асинхронно: у нового аккаунта первый
        # /api/organization/detail может вернуть quota=0, а через пару секунд
        # уже 750000. Опрашиваем до bonus_wait_sec, затем берём финальный баланс.
        bal2 = None
        deadline = time.time() + float(config.get("bonus_wait_sec", 15) or 0)
        while True:
            try:
                bal2 = get_balance(session, org)
            except Exception:
                bal2 = None
            if bal2 and isinstance(bal2.get("quota"), (int, float)) and bal2["quota"] > 0:
                break
            if time.time() >= deadline:
                break
            time.sleep(3)
        if bal2:
            rec["balance_after"] = {
                "quota": bal2.get("quota"),
                "used_quota": bal2.get("used_quota"),
                "request_count": bal2.get("request_count"),
                "usd_left": bal2.get("usd_left"),
            }
            if (not rec.get("quota")) and bal2.get("quota"):
                rec["quota"] = bal2.get("quota")
                rec["usd_left"] = bal2.get("usd_left")
                rec["balance"] = bal2
        # для панели: «free_tier_enabled» здесь = есть бесплатный знак-бонус
        rec["free_tier_enabled"] = bool(isinstance(rec.get("quota"), (int, float))
                                        and rec["quota"] > 0)
        rec["free_usd"] = rec.get("usd_left")

        rec["oauth"] = {"client_id": CLIENT_ID, "redirect": BASE + REDIRECT_PATH,
                        "scope": SCOPE, "authorize_value": 1}
        rec["engine"] = "camoufox+requests"
        rec["elapsed_sec"] = round(time.time() - started, 1)
        if not ok:
            rec["error"] = f"ключ создан, но chat/completions не прошёл: {detail}"
        return rec
    except ServiceError as exc:
        rec["error"] = str(exc)[:250]
        if exc.retryable:
            rec["retryable"] = True
        if exc.ip_burned:
            rec["ip_burned"] = True
        return rec
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
        return rec
