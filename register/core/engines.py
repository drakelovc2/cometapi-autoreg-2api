# -*- coding: utf-8 -*-
"""Движки браузера: один интерфейс на camoufox / patchright / playwright / invisible.

Зачем прослойка. Разные антибот-системы проходят разные движки: Turnstile
стабильно проваливается под стоковым Chromium, а под camoufox (Gecko с
подменой отпечатка) проходит. Вместо того чтобы переписывать реггер под
каждый сайт, движок выбирается флагом.

    from . import engines
    eng = engines.resolve("auto", stealth=True)      # → "camoufox"
    with engines.launch(eng, profile_dir=prof, proxy=px, headless=False) as ctx:
        page = ctx.new_page()

Профиль. Один аккаунт = один постоянный `user_data_dir`. Cookies и
localStorage переживают перезапуск, поэтому регистрация и последующая
проверка идут из одной «личности». Профили между аккаунтами и сервисами
НЕ переиспользуются.

`fingerprint_check()` снимает живые параметры браузера и сравнивает с
сохранённым снимком: расхождение означает, что под тем же профилем поднялся
другой отпечаток (сменился UA, часовой пояс, WebGL), и партию гонять рано.
"""
import json
import os
import random
import shutil
import string

_ENGINES = ("auto", "camoufox", "patchright", "playwright", "invisible")

# Порядок авто-выбора: от самого устойчивого к антиботу к самому простому.
# stealth=True начинается с camoufox; stealth=False — с лёгкого Chromium.
_AUTO_ORDER_STEALTH = ("camoufox", "patchright", "playwright")
_AUTO_ORDER_PLAIN = ("patchright", "playwright", "camoufox")


def _has(module):
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def available():
    """Какие движки реально стоят в текущем интерпретаторе."""
    out = []
    if _has("camoufox"):
        out.append("camoufox")
    if _has("patchright"):
        out.append("patchright")
    if _has("playwright"):
        out.append("playwright")
    if _has("invisible_playwright"):
        out.append("invisible")
    return out


def resolve(name="auto", stealth=False):
    """Имя движка → конкретный установленный. Бросает, если ставить нечего.

    `auto` перебирает порядок, зависящий от stealth, и берёт первый
    доступный. Явно названный, но не установленный движок — это ошибка,
    а не молчаливая подмена: иначе реггер «работает», но не тем движком,
    который нужен для прохождения защиты.
    """
    name = (name or "auto").strip().lower()
    if name not in _ENGINES:
        raise ValueError(f"неизвестный движок: {name} (есть: {', '.join(_ENGINES)})")
    have = available()
    if name != "auto":
        if name not in have:
            raise RuntimeError(
                f"движок {name} не установлен (доступны: {', '.join(have) or 'нет'}). "
                f"camoufox: pip install camoufox; patchright: pip install patchright; "
                f"invisible: pip install invisible-playwright")
        return name
    order = _AUTO_ORDER_STEALTH if stealth else _AUTO_ORDER_PLAIN
    for cand in order:
        if cand in have:
            return cand
    raise RuntimeError("ни один движок браузера не установлен "
                       "(playwright/camoufox/patchright)")


def make_profile(root, account=None, engine="camoufox", locale="en-US",
                 timezone=None, proxy_country=None):
    """Постоянный профиль под один аккаунт: каталог + снимок отпечатка.

    `account` — стабильный ключ (email / label). Без него генерится случайный,
    чтобы профиль нельзя было случайно переиспользовать.
    """
    key = (account or ("anon_" + "".join(random.choices(string.ascii_lowercase, k=10))))
    safe = "".join(c if c.isalnum() or c in "-_@." else "_" for c in key)[:80]
    path = os.path.join(root, safe)
    os.makedirs(path, exist_ok=True)
    meta_path = os.path.join(path, "profile.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
    meta.setdefault("account", key)
    meta["engine"] = engine
    meta.setdefault("locale", locale)
    if timezone:
        meta.setdefault("timezone", timezone)
    elif proxy_country:
        # грубая, но полезная привязка: часы не должны противоречить IP
        meta.setdefault("timezone", _tz_for_country(proxy_country))
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return Profile(path, meta)


_COUNTRY_TZ = {
    "us": "America/New_York", "gb": "Europe/London", "de": "Europe/Berlin",
    "nl": "Europe/Amsterdam", "fr": "Europe/Paris", "pl": "Europe/Warsaw",
    "ua": "Europe/Kyiv", "ru": "Europe/Moscow", "tr": "Europe/Istanbul",
    "sg": "Asia/Singapore", "jp": "Asia/Tokyo", "kz": "Asia/Almaty",
    "ca": "America/Toronto", "au": "Australia/Sydney", "br": "America/Sao_Paulo",
}


def _tz_for_country(country):
    return _COUNTRY_TZ.get((country or "").lower(), "America/New_York")


class Profile:
    def __init__(self, path, meta):
        self.path = path
        self.meta = meta

    @property
    def id(self):
        return self.meta.get("account") or os.path.basename(self.path)

    @property
    def engine(self):
        return self.meta.get("engine")

    def set(self, **kw):
        self.meta.update({k: v for k, v in kw.items() if v is not None})
        with open(os.path.join(self.path, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=1)

    def snapshot_path(self):
        return os.path.join(self.path, "fingerprint.json")

    def load_snapshot(self):
        try:
            with open(self.snapshot_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_snapshot(self, data):
        with open(self.snapshot_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def wipe(self):
        shutil.rmtree(self.path, ignore_errors=True)

    def __repr__(self):
        return f"<Profile {self.id} engine={self.engine} {self.path}>"


# ── запуск ────────────────────────────────────────────────────────────────

def _proxy_dict(proxy):
    """Строка прокси → конфиг для движков."""
    if not proxy:
        return None
    from .proxy import normalize
    url = normalize(proxy)
    if not url:
        return None
    from urllib.parse import unquote, urlsplit
    u = urlsplit(url)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


class BrowserContext:
    """Обёртка над контекстом любого движка — единый API для реггера."""

    def __init__(self, engine, ctx, closer):
        self.engine = engine
        self.ctx = ctx
        self._closer = closer

    def new_page(self):
        return self.ctx.new_page()

    def pages(self):
        return self.ctx.pages

    def close(self):
        try:
            self._closer()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def launch(engine, profile, proxy=None, headless=True, locale=None,
           timezone=None, slow_mo=None, extra_args=None):
    """Поднять движок с постоянным профилем. Возвращает BrowserContext.

    Все движки приводятся к одному виду: у нас есть `ctx.new_page()`,
    cookies и storage лежат в `profile.path`.
    """
    engine = resolve(engine or "auto", stealth=(engine == "camoufox"))
    px = _proxy_dict(proxy)
    loc = locale or (profile.meta.get("locale") if profile else None) or "en-US"
    tz = timezone or (profile.meta.get("timezone") if profile else None)

    if engine == "camoufox":
        return _launch_camoufox(profile, px, headless, loc, tz, extra_args)
    if engine in ("patchright", "playwright"):
        return _launch_chromium(engine, profile, px, headless, loc, tz,
                                slow_mo, extra_args)
    if engine == "invisible":
        return _launch_invisible(profile, px, headless, loc, tz)
    raise RuntimeError(f"движок {engine} не поддержан")


def _geoip_available():
    """Установлен ли extra `camoufox[geoip]` (maxminddb)."""
    try:
        from camoufox.geolocation import geoip_allowed  # type: ignore
        geoip_allowed()
        return True
    except Exception:
        return False


def _launch_camoufox(profile, px, headless, locale, timezone, extra_args):
    from camoufox.sync_api import Camoufox  # type: ignore

    # Отпечаток ОБЯЗАН быть стабильным: camoufox по умолчанию генерирует
    # новый на каждый запуск, и «один аккаунт = одна личность» разваливается
    # (проверено selftest'ом: UA, платформа и WebGL менялись между запусками).
    # Поэтому первый запуск выбирает реальный preset из комплекта camoufox,
    # сохраняет его в профиль, а дальше поднимаемся с ним же.
    preset = profile.meta.get("camoufox_preset")
    if not preset:
        preset = _new_camoufox_preset(profile.meta.get("fp_os") or "windows")
        if preset:
            profile.set(camoufox_preset=preset)

    opts = {
        "headless": headless,
        "humanize": True,
        "locale": locale,
        "persistent_context": True,
        "user_data_dir": profile.path,
    }
    if preset:
        opts["fingerprint_preset"] = preset
    if timezone:
        # В camoufox часовой пояс — это prefs (`config`), а не аргумент
        # запуска: `timezone=` уходит в launch_persistent_context и падает.
        cfg = dict(opts.get("config") or {})
        cfg.setdefault("timezone", timezone)
        opts["config"] = cfg
    if px:
        opts["proxy"] = px
        # geoip согласует часовой пояс/локаль с выходным IP. Он требует
        # extra `camoufox[geoip]`; если его нет — не падаем, а полагаемся
        # на timezone, переданный вызывающим (см. `_tz_for_country`).
        if _geoip_available():
            opts["geoip"] = True
        else:
            opts["i_know_what_im_doing"] = True
    if extra_args:
        opts["args"] = list(extra_args)

    cm = Camoufox(**opts)
    ctx = cm.__enter__()

    def _close():
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass

    return BrowserContext("camoufox", ctx, _close)


def _new_camoufox_preset(os_name="windows"):
    """Реальный (не синтетический) preset устройства из комплекта camoufox."""
    try:
        from camoufox.fingerprints import get_random_preset  # type: ignore
        return get_random_preset(os=os_name)
    except Exception:
        return None


def _launch_chromium(engine, profile, px, headless, locale, timezone,
                     slow_mo, extra_args):
    if engine == "patchright":
        from patchright.sync_api import sync_playwright  # type: ignore
    else:
        from playwright.sync_api import sync_playwright  # type: ignore

    pw = sync_playwright().start()
    args = ["--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check"]
    if extra_args:
        args.extend(extra_args)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=profile.path,
        headless=headless,
        args=args,
        locale=locale,
        timezone_id=timezone,
        slow_mo=slow_mo,
        proxy=px,
    )

    def _close():
        try:
            ctx.close()
        finally:
            pw.stop()

    return BrowserContext(engine, ctx, _close)


def _launch_invisible(profile, px, headless, locale, timezone):
    from invisible_playwright.sync_api import sync_playwright  # type: ignore

    pw = sync_playwright().start()
    launch_kw = {"headless": headless, "locale": locale,
                 "user_data_dir": profile.path}
    if timezone:
        launch_kw["timezone_id"] = timezone
    if px:
        launch_kw["proxy"] = px
    ctx = pw.chromium.launch_persistent_context(**launch_kw)

    def _close():
        try:
            ctx.close()
        finally:
            pw.stop()

    return BrowserContext("invisible", ctx, _close)


# ── отпечаток ─────────────────────────────────────────────────────────────

FP_JS = """() => {
  const nav = navigator, scr = screen;
  const gl = (() => {
    try {
      const c = document.createElement('canvas');
      const g = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!g) return null;
      const dbg = g.getExtension('WEBGL_debug_renderer_info');
      return dbg ? {
        vendor: g.getParameter(dbg.UNMASKED_VENDOR_WEBGL),
        renderer: g.getParameter(dbg.UNMASKED_RENDERER_WEBGL),
      } : { vendor: g.getParameter(g.VENDOR), renderer: g.getParameter(g.RENDERER) };
    } catch (e) { return null; }
  })();
  return {
    userAgent: nav.userAgent,
    platform: nav.platform,
    language: nav.language,
    languages: (nav.languages || []).join(','),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    timezoneOffset: new Date().getTimezoneOffset(),
    screen: scr.width + 'x' + scr.height + '@' + (scr.colorDepth || 0),
    hardwareConcurrency: nav.hardwareConcurrency,
    deviceMemory: nav.deviceMemory || null,
    webdriver: !!nav.webdriver,
    webgl: gl,
    touchPoints: nav.maxTouchPoints,
    vendor: nav.vendor,
  };
}"""

# Поля, которые обязаны совпадать у одного профиля между запусками.
# `screen` и `webdriver` тоже важны, но screen меняется при смене окна —
# его сравниваем отдельно и мягко.
FP_STRICT = ("userAgent", "platform", "language", "languages", "timezone",
             "hardwareConcurrency", "vendor", "touchPoints")


def fingerprint(page):
    """Снять живой отпечаток страницы."""
    return page.evaluate(FP_JS)


def fingerprint_check(page, profile, save_if_missing=True):
    """Сравнить живой отпечаток с сохранённым в профиле.

    Возвращает (ok, diffs). Первый вызов сохраняет снимок и возвращает True:
    сравнивать не с чем. Дальше любое расхождение по строгим полям — провал.
    """
    live = fingerprint(page)
    saved = profile.load_snapshot()
    if not saved:
        if save_if_missing:
            profile.save_snapshot(live)
        return True, {}
    diffs = {}
    for key in FP_STRICT:
        if key in saved and live.get(key) != saved.get(key):
            diffs[key] = (saved.get(key), live.get(key))
    if saved.get("webgl") != live.get("webgl"):
        diffs["webgl"] = (saved.get("webgl"), live.get("webgl"))
    return (not diffs), diffs