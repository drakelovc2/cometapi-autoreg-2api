#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cometapi2api — OpenAI-совместимый мост к пулу ключей CometAPI.

Источник ключей — локальный файл data/accounts.jsonl.
Файл читается hot-reload по mtime: новый ключ, дописанный снаружи,
подхватывается без рестарта (в том числе посреди запроса — пул перечитывается
перед каждой попыткой).

Пул:
  * стратегия least_used (по умолчанию) или round_robin;
  * 401 → ключ dead; 403 → dead, но 403/400/429 с текстом про квоту → пауза;
  * 402 / insufficient quota → ключ на паузу, тот же запрос идёт следующим;
  * 429 → короткий кулдаун, после r429_to_exhausted подряд → длинная пауза;
  * 5xx / сеть / таймаут → ретрай, после fails_to_dead → dead;
  * не больше max_attempts (3) попыток на один запрос;
  * сырые ключи не уходят клиенту и не пишутся в логи — только маска.

Эндпоинты:
  POST /v1/chat/completions   OpenAI chat (stream и non-stream)
  POST /v1/responses          OpenAI Responses API (stream и non-stream)
  POST /v1/embeddings         OpenAI embeddings
  GET  /v1/models             каталог апстрима (кэш models_cache_sec)
  GET  /v1/balance            usd_left по ключам из банка (без апстрима)
  GET  /health                200 всегда, апстрим не трогает
  GET  /admin/status          пул, маскированные ключи, счётчики, dead
  POST /admin/reload          перечитать банк; {"reset_dead": true} — снять dead/паузы

Файлы состояния:
  data/state.json     статусы ключей; только sha256-отпечатки, без сырых ключей
  data/usage.jsonl    леджер запросов (ключ — маска)
  data/models_cache.json
  bridge.log          лог с ротацией (RotatingFileHandler)

Запуск: bash start.sh   (порт из config.json, по умолчанию 12063)
"""
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

VERSION = "1.0"
STARTED_AT = time.time()

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "state.json")
USAGE_LOG = os.path.join(DATA, "usage.jsonl")
MODELS_CACHE = os.path.join(DATA, "models_cache.json")


def _load_cfg():
    path = os.path.join(ROOT, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CFG = _load_cfg()
BR = CFG.get("bridge") or {}

HOST = CFG.get("host") or "127.0.0.1"
PORT = int(CFG.get("port", 12063))
UPSTREAM = (CFG.get("upstream") or "https://api.cometapi.com/v1").rstrip("/")
BANK_FILE = os.path.normpath(os.path.join(ROOT, CFG.get("accounts_file")
                                          or "data/accounts.jsonl"))
CLIENT_KEYS = [str(k) for k in (CFG.get("client_keys") or []) if str(k).strip()]
MAX_BODY = 64 * 1024 * 1024

# ── настройки ротации ───────────────────────────────────────────────────────
STRATEGY = str(BR.get("strategy") or "least_used").lower()
MAX_ATTEMPTS = int(BR.get("max_attempts", 3))
RETRY_BASE_SEC = float(BR.get("retry_base_sec", 0.6))
RETRY_MAX_SLEEP = float(BR.get("retry_max_sleep", 5.0))
COOLDOWN_429_SEC = float(BR.get("cooldown_429_sec", 30))
COOLDOWN_5XX_SEC = float(BR.get("cooldown_5xx_sec", 10))
FAILS_TO_DEAD = int(BR.get("fails_to_dead", 4))
R429_TO_EXHAUSTED = int(BR.get("r429_to_exhausted", 2))
EXHAUSTED_PARK_SEC = float(BR.get("exhausted_park_min", 60)) * 60
BANK_POLL_SEC = float(BR.get("bank_poll_sec", 5))
MIN_USD_LEFT = float(BR.get("min_usd_left", 0.0))
CONNECT_TIMEOUT = float(BR.get("upstream_connect_timeout", 20))
READ_TIMEOUT = float(BR.get("upstream_read_timeout", 600))
MODELS_CACHE_SEC = float(BR.get("models_cache_sec", 600))

LOG_FILE = os.path.join(ROOT, (CFG.get("log") or {}).get("file") or "bridge.log")
LOG_MAX_BYTES = int((CFG.get("log") or {}).get("max_bytes") or 5 * 1024 * 1024)
LOG_BACKUPS = int((CFG.get("log") or {}).get("backup_count") or 5)

log = logging.getLogger("cometapi2api")


def setup_logging(foreground=False):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                                              backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if foreground or os.environ.get("COMETAPI2API_FOREGROUND"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.propagate = False

    def _hook(exc_type, exc, tb):
        log.error("uncaught: %s", "".join(traceback.format_exception(exc_type, exc, tb)).strip())

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)


# ── маскирование и прочее ───────────────────────────────────────────────────

def mask(key):
    """Маска для логов/статуса: sk-enE…rDdS."""
    if not key:
        return "?"
    if len(key) <= 12:
        return key[:4] + "…"
    return key[:8] + "…" + key[-4:]


def mask_ascii(key):
    """Маска для HTTP-заголовков (latin-1): sk-enE..rDdS."""
    if not key:
        return "?"
    if len(key) <= 12:
        return key[:4] + ".."
    return key[:8] + ".." + key[-4:]


def fp_of(key):
    """Стабильный отпечаток ключа: sha256, первые 16 hex. Сырого ключа не содержит."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


_raw_keys = []
_raw_lock = threading.Lock()


def _set_raw_keys(keys):
    with _raw_lock:
        _raw_keys[:] = sorted(set(keys), key=len, reverse=True)


def scrub(text):
    """Вырезает любые известные сырые ключи из текста (защита от эха апстрима)."""
    if not text:
        return text
    s = str(text)
    with _raw_lock:
        for raw in _raw_keys:
            if raw and raw in s:
                s = s.replace(raw, mask(raw))
    return s


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return None


def _boolish(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "да")
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── классификация ошибок апстрима ───────────────────────────────────────────
# kind: ok | dead | exhausted | retry | client_error
QUOTA_HINTS = (
    "insufficient quota", "insufficient_quota", "quota is not enough",
    "not enough quota", "exceeded your current quota", "exceeded quota",
    "quota exceeded", "insufficient balance", "insufficient_balance",
    "insufficient credit", "not enough balance", "no credit", "billing",
    # CometAPI: преконсьюм квоты при маленьком остатке — это про квоту
    # (до предоплаты), а не бан ключа. Ошибка пре-резерва — exhausted,
    # ключ уходит на паузу, а не в dead навсегда.
    "pre-consume", "pre_consume", "combined remaining quota",
    "余额", "额度", "欠费",
)
AUTH_HINTS = (
    "invalid token", "invalid api key", "invalid_api_key", "incorrect api key",
    "token has expired", "token expired", "key expired", "unauthorized",
    "banned", "revoked", "deactivated", "deleted",
)
TRANSIENT_HINTS = (
    "rate limit", "rate_limit", "too many requests", "overloaded", "over capacity",
    "temporarily", "timeout", "try again", "upstream", "server error", "unavailable",
)
CLIENT_HINTS = (
    "invalid request", "invalid_request", "invalid model", "model not found",
    "does not exist", "unsupported", "context length", "context_length",
    "bad request", "missing required", "validation",
)
# CometAPI (one-api) на неизвестную/недоступную модель отвечает 500
# «no available channel for group default and model ...» — это про модель,
# а не про ключ: ротация и штраф ключа тут только жгут попытки.
MODEL_HINTS = (
    "no available channel", "no available channels", "无可用渠道",
    "no available distributor", "model_not_found", "model not found",
)


def _has(text, hints):
    return any(h in text for h in hints)


def classify_text(text, default="retry"):
    """Смысл ошибки по тексту. default — когда ничего не узнали."""
    t = (text or "").lower()
    # Квота важнее прочих: CometAPI бьет ERR-предоплатой до фактической
    # транзакции — такое нельзя помечать dead, это пауза.
    if _has(t, QUOTA_HINTS):
        return "exhausted"
    if _has(t, AUTH_HINTS):
        return "dead"
    if _has(t, MODEL_HINTS):
        return "client_error"
    if _has(t, TRANSIENT_HINTS):
        return "retry"
    if _has(t, CLIENT_HINTS):
        return "client_error"
    return default


def classify(status, text):
    """Классифицирует HTTP-ответ апстрима. Ключ не трогаем на ошибках запроса."""
    t = (text or "").lower()
    if _has(t, MODEL_HINTS):
        return "client_error"
    if status == 200:
        return "ok"
    if status == 401:
        return "dead"
    if status == 403:
        # 403 у CometAPI — это и «ключ забанен», и «кончилась квота».
        if _has(t, QUOTA_HINTS):
            return "exhausted"
        if _has(t, TRANSIENT_HINTS):
            return "retry"
        return "dead"
    if status == 402:
        return "exhausted"
    if status == 429:
        return "exhausted" if _has(t, QUOTA_HINTS) else "retry"
    if status in (400, 422):
        return "exhausted" if _has(t, QUOTA_HINTS) else "client_error"
    if status in (408, 409, 425):
        return "retry"
    if status >= 500:
        return "retry"
    if status == 404:
        return "client_error"       # нет такой модели/маршрута — смена ключа не поможет
    return "client_error"


def _err_text(obj, raw=""):
    try:
        e = obj.get("error")
        if isinstance(e, dict):
            parts = [str(e[k]) for k in ("type", "code", "message") if e.get(k)]
            return " ".join(parts)[:600]
        if e:
            return str(e)[:600]
        if obj.get("message"):
            return str(obj["message"])[:600]
    except (AttributeError, TypeError):
        pass
    return (raw or "")[:600]


def clean_error_text(text):
    """Достаёт человекочитаемое message из JSON-ошибки апстрима.

    Клиенту не нужно видеть вложенный JSON строкой: OpenAI-клиенты показывают
    error.message, и там должен быть текст, а не экранированные кавычки.
    """
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
        except ValueError:
            return t
        if isinstance(obj, dict):
            e = obj.get("error")
            if isinstance(e, dict):
                for k in ("message", "code", "type"):
                    if e.get(k):
                        return str(e[k])[:600]
            elif e:
                return str(e)[:600]
            if obj.get("message"):
                return str(obj["message"])[:600]
    return t


class UpstreamError(Exception):
    def __init__(self, kind, status, text, retry_after=None):
        super().__init__(text)
        self.kind = kind
        self.status = status
        self.text = text or ""
        self.retry_after = retry_after


# ── ключ ────────────────────────────────────────────────────────────────────

class Key:
    """Один ключ CometAPI + его состояние в ротации."""

    def __init__(self, raw, rec=None, st=None):
        self.raw = raw
        self.fp = fp_of(raw)
        self.label = mask(raw)
        self.usd_left = None
        self.key_check = None
        self.test_model = None
        self.bank_live = None
        self.apply_record(rec or {})

        st = st or {}
        self.status = st.get("status", "alive")          # alive | exhausted | dead
        self.cooldown_until = _num(st.get("cooldown_until")) or 0.0
        self.park_until = _num(st.get("park_until")) or 0.0
        self.park_reason = st.get("park_reason")
        self.dead_reason = st.get("dead_reason")
        self.fails = int(st.get("fails") or 0)            # подряд 5xx/сеть
        self.r429 = int(st.get("r429") or 0)              # подряд 429
        self.ok = int(st.get("ok") or 0)
        self.fail = int(st.get("fail") or 0)
        self.uses = int(st.get("uses") or 0)              # выдач из пула (least-used)
        self.rotations = int(st.get("rotations") or 0)    # уходов на следующий ключ
        self.last_err = st.get("last_err")
        self.last_status = st.get("last_status")
        self.last_used = _num(st.get("last_used")) or 0.0

    # ── банк ──
    def apply_record(self, rec):
        usd = rec.get("usd_left")
        if usd is None:
            bal = rec.get("balance")
            if isinstance(bal, dict):
                usd = bal.get("usd_left")
            elif isinstance(bal, (int, float)):
                usd = bal
        if usd is not None:
            self.usd_left = _num(usd)
        self.key_check = (str(rec.get("key_check"))[:300]
                          if rec.get("key_check") is not None else self.key_check)
        self.test_model = rec.get("test_model") or self.test_model
        if "key_live" in rec:
            self.bank_live = _boolish(rec.get("key_live"))

    def apply_bank_hints(self, min_usd=None, park_sec=None):
        """Мягкие сигналы банка: явный dead в банке и usd_left<=0."""
        min_usd = MIN_USD_LEFT if min_usd is None else min_usd
        park_sec = EXHAUSTED_PARK_SEC if park_sec is None else park_sec
        bank_dead, why = False, None
        if self.bank_live is False:
            bank_dead, why = True, "bank: key_live=false"
        m = re.search(r"HTTP\s+(401|403)", self.key_check or "", re.I)
        if m:
            bank_dead, why = True, "bank: key_check HTTP %s" % m.group(1)
        if bank_dead:
            if self.status != "dead":
                self.status = "dead"
                self.dead_reason = "bank"
                self.last_err = why
            return
        if self.status == "dead" and self.dead_reason == "bank":
            self.revive()
        if self.usd_left is not None and self.usd_left <= min_usd:
            if self.status == "alive" or (self.status == "exhausted"
                                          and self.park_reason != "bank_low"):
                self.park(park_sec, "bank_low")
        elif self.status == "exhausted" and self.park_reason == "bank_low":
            self.revive()

    # ── состояние ──
    def usable(self):
        if self.status == "dead":
            return False
        now = time.time()
        if self.status == "exhausted":
            return now >= self.park_until
        return now >= self.cooldown_until

    def cool(self, sec, err=None):
        self.cooldown_until = max(self.cooldown_until, time.time() + max(0.0, sec))
        if err:
            self.last_err = scrub(err)[:200]

    def park(self, sec, reason=None):
        self.status = "exhausted"
        self.park_until = max(self.park_until, time.time() + max(0.0, sec))
        if reason:
            self.park_reason = reason

    def mark_dead(self, reason=None):
        if self.status != "dead":
            self.fail += 1
        self.status = "dead"
        self.dead_reason = "runtime"
        if reason:
            self.last_err = scrub(reason)[:200]

    def revive(self):
        self.status = "alive"
        self.cooldown_until = 0.0
        self.park_until = 0.0
        self.park_reason = None
        self.dead_reason = None
        self.fails = 0
        self.r429 = 0

    def on_ok(self):
        self.fails = 0
        self.r429 = 0
        self.ok += 1
        self.last_status = 200
        self.last_used = time.time()
        if self.status != "alive":
            self.revive()
        self.last_err = None
        self.cooldown_until = 0.0
        self.park_until = 0.0
        self.park_reason = None

    def on_429(self, err=None):
        self.r429 += 1
        self.fail += 1
        self.last_status = 429
        self.cool(COOLDOWN_429_SEC, err)
        if self.r429 >= R429_TO_EXHAUSTED:
            self.park(EXHAUSTED_PARK_SEC, "429")

    def on_5xx(self, err=None):
        self.fails += 1
        self.fail += 1
        self.last_status = 500
        if self.fails >= FAILS_TO_DEAD:
            self.status = "dead"
            self.dead_reason = "runtime"
            if err:
                self.last_err = scrub(err)[:200]
        else:
            self.cool(COOLDOWN_5XX_SEC, err)

    def state(self):
        """То, что пишем на диск: без raw, только fp."""
        return {"status": self.status, "cooldown_until": self.cooldown_until,
                "park_until": self.park_until, "park_reason": self.park_reason,
                "dead_reason": self.dead_reason, "fails": self.fails,
                "r429": self.r429, "ok": self.ok, "fail": self.fail,
                "uses": self.uses, "rotations": self.rotations,
                "last_err": self.last_err, "last_status": self.last_status,
                "last_used": self.last_used}

    def public(self):
        now = time.time()
        d = self.state()
        d.update({"id": self.fp, "key": self.label,
                  "usd_left": self.usd_left, "key_live": self.bank_live,
                  "test_model": self.test_model,
                  "key_check": (self.key_check or "")[:120] or None,
                  "usable": self.usable(),
                  "cooldown_left_sec": max(0, int(self.cooldown_until - now)),
                  "park_left_sec": max(0, int(self.park_until - now)),
                  "last_used_at": _iso(self.last_used) if self.last_used else None})
        d.pop("cooldown_until", None)
        d.pop("park_until", None)
        return d


# ── пул ─────────────────────────────────────────────────────────────────────

def _record_from_line(line):
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    key = str(rec.get("api_key") or "").strip()
    if not key or len(key) < 20:
        return None
    return rec


def load_bank(path):
    """Читает банк: JSONL, по одной записи на строку. Битые строки пропускает.

    Один и тот же api_key в нескольких строках — берём последнюю (банк может
    переписывать запись), порядок первого появления сохраняется.
    """
    records = []
    index = {}
    bad = 0
    for k in (CFG.get("keys") or []):
        if isinstance(k, str) and len(k.strip()) >= 20:
            rec = {"api_key": k.strip()}
            key = rec["api_key"]
            if key in index:
                records[index[key]] = rec
            else:
                index[key] = len(records)
                records.append(rec)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                rec = _record_from_line(line)
                if rec is None:
                    bad += 1
                    continue
                key = str(rec["api_key"]).strip()
                if key in index:
                    records[index[key]] = rec
                else:
                    index[key] = len(records)
                    records.append(rec)
    except OSError:
        pass
    return records, bad


def _load_state_file():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("keys") or {}
        return keys if isinstance(keys, dict) else {}
    except (OSError, ValueError):
        return {}


class Pool:
    def __init__(self):
        self._lock = threading.Lock()
        self._reload_lock = threading.Lock()
        self.keys = []
        self._rr = 0
        self._mtime = -1.0
        self._loaded = False
        self._bad_lines = 0
        self._reloaded_at = 0.0
        self._reload_count = 0
        self._dirty = False
        self._state_from_disk = _load_state_file()

    # ── загрузка ──
    def reload(self, force=False):
        try:
            mtime = os.path.getmtime(BANK_FILE)
        except OSError:
            mtime = 0.0
        with self._reload_lock:
            if not force and self._loaded and mtime == self._mtime:
                return {"changed": False, "total": len(self.keys),
                        "mtime": _iso(mtime), "reloads": self._reload_count}
            records, bad = load_bank(BANK_FILE)
            if not records and self.keys and not force:
                # Банк на мгновение пуст (переписывается) — пул не выбрасываем.
                self._mtime = mtime
                log.warning("bank is empty/broken; keeping %d keys in pool", len(self.keys))
                return {"changed": False, "total": len(self.keys),
                        "warning": "bank empty; pool kept", "bad_lines": bad,
                        "mtime": _iso(mtime), "reloads": self._reload_count}
            with self._lock:
                old = {k.fp: k for k in self.keys}
                new = []
                added = []
                for rec in records:
                    raw = str(rec["api_key"]).strip()
                    fp = fp_of(raw)
                    k = old.get(fp)
                    if k is None:
                        k = Key(raw, rec, self._state_from_disk.get(fp))
                        added.append(k)
                    else:
                        k.apply_record(rec)
                    k.apply_bank_hints()
                    new.append(k)
                new_fps = {k.fp for k in new}
                removed = [k for k in self.keys if k.fp not in new_fps]
                self.keys = new
                self._mtime = mtime
                self._loaded = True
                self._bad_lines = bad
                self._reloaded_at = time.time()
                self._reload_count += 1
                self._dirty = True
            _set_raw_keys([k.raw for k in new])
            info = {"changed": True, "total": len(new), "added": len(added),
                    "removed": len(removed), "bad_lines": bad,
                    "mtime": _iso(mtime), "reloads": self._reload_count}
            if added or removed or force or bad:
                log.info("bank reload: total=%d added=%d removed=%d bad_lines=%d mtime=%s",
                         len(new), len(added), len(removed), bad, _iso(mtime) or "?")
            return info

    # ── сохранение состояния ──
    def mark_dirty(self):
        with self._lock:
            self._dirty = True

    def save(self, force=False):
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {"saved_at": int(time.time()), "version": VERSION,
                       "keys": {k.fp: k.state() for k in self.keys}}
            self._dirty = False
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log.warning("state save failed: %s", e)

    # ── выбор ключа ──
    def pick(self, exclude):
        with self._lock:
            cand = [k for k in self.keys if k.usable() and k.fp not in exclude]
            if not cand:
                return None
            if STRATEGY == "round_robin":
                self._rr = (self._rr + 1) % len(cand)
                chosen = cand[self._rr]
            else:  # least_used
                chosen = min(cand, key=lambda k: (k.uses, k.last_used, k.fp))
            chosen.uses += 1
            chosen.last_used = time.time()
            return chosen

    def any_key(self, exclude):
        """Все в кулдауне/паузе — берём живого (не dead), кто раньше освободится."""
        with self._lock:
            cand = [k for k in self.keys if k.status != "dead" and k.fp not in exclude]
            if not cand:
                return None
            cand.sort(key=lambda k: max(k.cooldown_until, k.park_until))
            chosen = cand[0]
            chosen.uses += 1
            chosen.last_used = time.time()
            return chosen

    def models_candidates(self, limit=2):
        with self._lock:
            cand = [k for k in self.keys if k.usable()]
        return cand[:limit]

    # ── статистика ──
    def stats(self):
        now = time.time()
        with self._lock:
            return {
                "total": len(self.keys),
                "usable": sum(1 for k in self.keys if k.usable()),
                "alive": sum(1 for k in self.keys if k.status == "alive"),
                "exhausted": sum(1 for k in self.keys if k.status == "exhausted"),
                "dead": sum(1 for k in self.keys if k.status == "dead"),
                "cooling": sum(1 for k in self.keys
                               if k.status == "alive" and k.cooldown_until > now),
                "usd_left_sum": round(sum(k.usd_left or 0 for k in self.keys), 4),
            }

    def bank_info(self):
        try:
            mtime = os.path.getmtime(BANK_FILE)
        except OSError:
            mtime = 0.0
        return {"path": os.path.basename(BANK_FILE), "exists": os.path.exists(BANK_FILE),
                "mtime": _iso(mtime), "mtime_age_sec": (int(time.time() - mtime)
                                                        if mtime else None),
                "bad_lines": self._bad_lines, "reloaded_at": _iso(self._reloaded_at),
                "reloads": self._reload_count}

    def snapshot_keys(self):
        with self._lock:
            return [k.public() for k in self.keys]

    def reset_dead(self):
        n = 0
        with self._lock:
            for k in self.keys:
                if k.status != "alive":
                    k.revive()
                    n += 1
            self._dirty = True
        return n


POOL = Pool()


# ── счётчики ────────────────────────────────────────────────────────────────

class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = {"requests": 0, "ok": 0, "errors": 0, "client_errors": 0,
                     "retries": 0, "no_keys": 0, "by_status": {},
                     "by_endpoint": {}}

    def inc(self, name, n=1):
        with self._lock:
            self.data[name] = self.data.get(name, 0) + n

    def status(self, code, endpoint=None):
        with self._lock:
            bs = self.data["by_status"]
            bs[str(code)] = bs.get(str(code), 0) + 1
            if endpoint:
                be = self.data["by_endpoint"]
                be[endpoint] = be.get(endpoint, 0) + 1

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.data))


COUNTERS = Counters()
_usage_lock = threading.Lock()


def log_usage(**fields):
    fields["ts"] = _iso(time.time())
    line = scrub(json.dumps(fields, ensure_ascii=False))
    try:
        with _usage_lock:
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


# ── каталог моделей ─────────────────────────────────────────────────────────

STATIC_MODELS = [
    "gpt-4o-mini-2024-07-18", "gpt-4o-mini", "gpt-4o", "gpt-4.1",
    "gpt-4.1-mini", "gpt-5", "gpt-5-mini", "o3-mini",
    "claude-sonnet-4-20250514", "deepseek-chat", "gemini-2.5-flash",
    "text-embedding-3-small", "text-embedding-3-large",
]

_models = {"data": None, "ts": 0.0, "count": 0}


def _read_models_cache():
    try:
        with open(MODELS_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
            return data
    except (OSError, ValueError):
        pass
    return None


def _static_models():
    return {"object": "list", "data": [{"id": m, "object": "model",
                                        "created": 0, "owned_by": "cometapi"}
                                       for m in STATIC_MODELS]}


def get_models(force=False):
    if not force and _models["data"] and time.time() - _models["ts"] < MODELS_CACHE_SEC:
        return _models["data"]
    for key in POOL.models_candidates(2):
        try:
            r = requests.get(f"{UPSTREAM}/models",
                             headers={"Authorization": f"Bearer {key.raw}"},
                             timeout=(CONNECT_TIMEOUT, 30))
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
                    _models["data"] = data
                    _models["ts"] = time.time()
                    _models["count"] = len(data["data"])
                    try:
                        with open(MODELS_CACHE, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False)
                    except OSError:
                        pass
                    return data
            elif r.status_code in (401, 403):
                # /models обычно не требует квоты: 401/403 сюда доходят
                # только от реально мёртвого ключа.
                key.mark_dead("models HTTP %d" % r.status_code)
                POOL.mark_dirty()
                log.warning("models: key %s dead (HTTP %d)", key.label, r.status_code)
        except requests.RequestException:
            continue
    if _models["data"]:
        return _models["data"]
    cached = _read_models_cache()
    if cached:
        _models["data"] = cached
        _models["ts"] = time.time() - MODELS_CACHE_SEC + 60   # почти протух
        _models["count"] = len(cached.get("data") or [])
        return cached
    return _static_models()


# ── запросы к апстриму ──────────────────────────────────────────────────────

def _safe_text(resp, limit=2000):
    try:
        return (resp.text or "")[:limit]
    except Exception:
        return ""


def _headers_for(key, stream):
    return {
        "Authorization": f"Bearer {key.raw}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "OpenAI/Python 1.0",
    }


def _retry_after(resp):
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def call_json(key, url, payload):
    r = requests.post(url, json=payload, headers=_headers_for(key, False),
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if r.status_code != 200:
        body = _safe_text(r)
        raise UpstreamError(classify(r.status_code, body), r.status_code, body,
                            _retry_after(r))
    try:
        data = r.json()
    except ValueError:
        raise UpstreamError("retry", r.status_code, "non-JSON response from upstream")
    if isinstance(data, dict) and data.get("error"):
        txt = _err_text(data)
        raise UpstreamError(classify_text(txt), 200, txt)
    return data


def call_stream_open(key, url, payload):
    """Открывает стрим и заглядывает в первый содержательный чанк.

    Возвращает ("json", data) — апстрим проигнорировал stream и отдал JSON,
    либо ("sse", resp, first_lines, iterator). Ошибку внутри SSE поднимаем
    как UpstreamError, чтобы клиент не получил 200 с ошибкой в теле.
    """
    r = requests.post(url, json=payload, headers=_headers_for(key, True),
                      stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if r.status_code != 200:
        body = _safe_text(r)
        ra = _retry_after(r)
        r.close()
        raise UpstreamError(classify(r.status_code, body), r.status_code, body, ra)

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in ctype:
        body = _safe_text(r)
        r.close()
        try:
            obj = json.loads(body)
        except ValueError:
            raise UpstreamError("retry", r.status_code, body or "non-SSE response")
        if isinstance(obj, dict) and obj.get("error"):
            txt = _err_text(obj)
            raise UpstreamError(classify_text(txt), 200, txt)
        return ("json", obj)

    it = r.iter_lines(decode_unicode=True)
    first = []
    last_event = ""
    try:
        for _ in range(8):
            try:
                line = next(it)
            except StopIteration:
                break
            if line is None:
                continue
            first.append(line)
            s = line.strip()
            if not s:
                continue
            if s.startswith("event:"):
                last_event = s[6:].strip()
                continue
            if not s.startswith("data:"):
                continue
            data = s[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                break
            if isinstance(obj, dict):
                if obj.get("error") or obj.get("type") == "error" or last_event == "error":
                    txt = _err_text(obj, data)
                    r.close()
                    raise UpstreamError(classify_text(txt, default="retry"), 200, txt)
            break
    except requests.RequestException as e:
        r.close()
        raise UpstreamError("retry", 0, "stream peek: %s: %s" % (type(e).__name__, e))
    return ("sse", r, first, it)


# ── HTTP-сервер ─────────────────────────────────────────────────────────────

PROXY_PATHS = {
    "/v1/chat/completions": "/chat/completions",
    "/v1/responses": "/responses",
    "/v1/embeddings": "/embeddings",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "cometapi2api/%s" % VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Базовый access-log выключен: свои строки пишем в bridge.log.
        pass

    # ── ответы ──
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, x-api-key, OpenAI-Beta")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code, obj, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, code, message, err_type="bridge_error", extra=None):
        body = {"error": {"message": scrub(message)[:800], "type": err_type}}
        if extra:
            body["error"].update(extra)
        self._json(code, body)

    # ── тело запроса ──
    def _read_exact(self, n):
        """rfile.read(n) может вернуть меньше байт — дочитываем до n."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _read_body(self):
        """Возвращает dict или None (слишком большое) или "BAD" (не JSON)."""
        try:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                chunks, total = [], 0
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        break
                    try:
                        size = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError:
                        return "BAD"
                    if size == 0:
                        while True:
                            t = self.rfile.readline(65537)
                            if t in (b"\r\n", b"\n", b""):
                                break
                        break
                    total += size
                    if total > MAX_BODY:
                        return None
                    chunks.append(self._read_exact(size))
                    self.rfile.read(2)
                raw = b"".join(chunks)
            else:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return None
                raw = self._read_exact(n) if n else b"{}"
        except (OSError, ValueError):
            return "BAD"
        if not raw:
            raw = b"{}"
        try:
            data = json.loads(raw)
        except ValueError:
            return "BAD"
        return data if isinstance(data, dict) else "BAD"

    def _client_ok(self):
        if not CLIENT_KEYS:
            return True
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            token = (self.headers.get("x-api-key") or "").strip()
        return any(hmac.compare_digest(token, k) for k in CLIENT_KEYS)

    # ── маршруты ──
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = (urllib.parse.urlparse(self.path).path.rstrip("/") or "/")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        POOL.reload()

        if path == "/health":
            return self._json(200, self._health())
        if path == "/admin/status":
            return self._json(200, self._admin_status())
        if path in ("/", "/v1"):
            return self._json(200, {
                "service": "cometapi2api", "version": VERSION,
                "upstream": UPSTREAM,
                "endpoints": sorted(PROXY_PATHS) + ["/v1/models", "/v1/balance",
                                                     "/health", "/admin/status",
                                                     "/admin/reload"],
                "pool": POOL.stats(), "bank": POOL.bank_info()})

        if not self._client_ok():
            return self._error(401, "invalid client key", "unauthorized")

        if path == "/v1/models":
            force = str(query.get("refresh", ["0"])[0]).lower() in ("1", "true", "yes")
            return self._json(200, get_models(force=force))
        if path == "/v1/balance":
            keys = POOL.snapshot_keys()
            return self._json(200, {
                "object": "balance_list", "currency": "USD",
                "usd_left": round(sum(k.get("usd_left") or 0 for k in keys), 4),
                "keys": [{"key": k["key"],
                          "usd_left": k.get("usd_left"), "status": k["status"],
                          "usable": k["usable"]} for k in keys]})
        return self._error(404, "not found", "not_found")

    def do_POST(self):
        path = (urllib.parse.urlparse(self.path).path.rstrip("/") or "/")
        if path == "/admin/reload":
            body = self._read_body()
            if body is None:
                self.close_connection = True
                return self._error(413, "body too large", "too_large")
            if body == "BAD":
                body = {}
            info = POOL.reload(force=True)
            reset = 0
            if isinstance(body, dict) and body.get("reset_dead"):
                reset = POOL.reset_dead()
            POOL.save(force=True)
            log.info("admin reload: total=%d added=%d reset=%d",
                     info.get("total", 0), info.get("added", 0), reset)
            info["reset"] = reset
            info["pool"] = POOL.stats()
            return self._json(200, info)

        if path not in PROXY_PATHS:
            return self._error(404, "not found", "not_found")
        if not self._client_ok():
            return self._error(401, "invalid client key", "unauthorized")

        payload = self._read_body()
        if payload is None:
            self.close_connection = True
            return self._error(413, "body too large", "too_large")
        if payload == "BAD":
            return self._error(400, "invalid JSON body", "bad_request")
        stream = bool(payload.get("stream")) and path != "/v1/embeddings"
        return self._proxy(path, payload, stream)

    # ── служебное ──
    def _health(self):
        stats = POOL.stats()
        return {"ok": True, "service": "cometapi2api", "version": VERSION,
                "uptime_sec": int(time.time() - STARTED_AT),
                "upstream": UPSTREAM,
                "pool": stats,
                "bank": POOL.bank_info(),
                "degraded": stats["usable"] == 0,
                "models_cached": _models["count"]}

    def _admin_status(self):
        stats = POOL.stats()
        keys = POOL.snapshot_keys()
        return {"ok": True, "service": "cometapi2api", "version": VERSION,
                "uptime_sec": int(time.time() - STARTED_AT),
                "strategy": STRATEGY,
                "upstream": UPSTREAM,
                "bank": POOL.bank_info(),
                "pool": stats,
                "counters": COUNTERS.snapshot(),
                "dead": [{"key": k["key"],
                          "reason": k.get("dead_reason"),
                          "last_err": k.get("last_err")}
                         for k in keys if k["status"] == "dead"],
                "keys": keys,
                "models": {"cached": _models["count"],
                           "cached_at": _iso(_models["ts"]) if _models["ts"] else None}}

    # ── проксирование ──
    def _proxy(self, path, payload, stream):
        endpoint = path
        suffix = PROXY_PATHS[path]
        url = UPSTREAM + suffix
        model = str(payload.get("model") or "?")
        req_id = uuid.uuid4().hex[:8]
        t0 = time.time()
        COUNTERS.inc("requests")
        tried = []
        last = None
        last_key = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            POOL.reload()
            key = POOL.pick(set(tried))
            if key is None:
                if not tried and not POOL.keys:
                    COUNTERS.inc("no_keys")
                    log.warning("req=%s %s model=%s no keys in pool",
                                req_id, endpoint, model)
                    return self._error(503, "no keys in pool", "no_keys",
                                       {"pool": POOL.stats()})
                key = POOL.any_key(set(tried))
                if key is None:
                    break
            tried.append(key.fp)
            last_key = key
            if attempt > 1:
                COUNTERS.inc("retries")

            try:
                if stream:
                    return self._do_stream(req_id, key, url, payload, model, t0,
                                           attempt, endpoint)
                data = call_json(key, url, payload)
                key.on_ok()
                POOL.mark_dirty()
                u = data.get("usage") or {}
                lat = int((time.time() - t0) * 1000)
                COUNTERS.inc("ok")
                COUNTERS.status(200, endpoint)
                log.info("req=%s %s model=%s key=%s attempt=%d status=200 lat=%dms "
                         "in=%s out=%s", req_id, endpoint, model, key.label, attempt, lat,
                         u.get("prompt_tokens") or u.get("input_tokens") or 0,
                         u.get("completion_tokens") or u.get("output_tokens") or 0)
                log_usage(req=req_id, endpoint=endpoint, model=model,
                          key=key.label, status=200, stream=False, attempt=attempt,
                          latency_ms=lat,
                          in_tok=u.get("prompt_tokens") or u.get("input_tokens") or 0,
                          out_tok=u.get("completion_tokens") or u.get("output_tokens") or 0)
                return self._json(200, data, {"X-Comet-Key": mask_ascii(key.raw),
                                              "X-Comet-Attempts": str(attempt)})

            except UpstreamError as e:
                last = e
                self._penalize(key, e)
                POOL.mark_dirty()
                lat = int((time.time() - t0) * 1000)
                log.warning("req=%s %s model=%s key=%s attempt=%d status=%s kind=%s "
                            "lat=%dms err=%s", req_id, endpoint, model, key.label,
                            attempt, e.status, e.kind, lat, scrub(e.text)[:200])
                log_usage(req=req_id, endpoint=endpoint, model=model, key=key.label,
                          status=e.status, kind=e.kind, retry=True, stream=stream,
                          attempt=attempt, latency_ms=lat, error=scrub(e.text)[:200])

                if e.kind == "client_error":
                    # Ошибка запроса/модели: ретрай не поможет, ключ не виноват.
                    COUNTERS.inc("client_errors")
                    COUNTERS.status(e.status or 400, endpoint)
                    code = e.status if 400 <= (e.status or 0) < 500 else 400
                    return self._error(code, clean_error_text(e.text)
                                       or "upstream rejected request",
                                       "invalid_request_error",
                                       {"attempts": len(tried)})

                key.rotations += 1
                POOL.mark_dirty()
                delay = min(RETRY_MAX_SLEEP, RETRY_BASE_SEC * (2 ** (attempt - 1)))
                if e.retry_after:
                    delay = max(delay, min(RETRY_MAX_SLEEP, e.retry_after))
                if e.kind != "dead" and attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue

            except requests.Timeout as e:
                last = UpstreamError("retry", 0, "timeout: %s" % e)
                key.on_5xx("timeout")
                key.rotations += 1
                POOL.mark_dirty()
                log.warning("req=%s %s model=%s key=%s attempt=%d timeout",
                            req_id, endpoint, model, key.label, attempt)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(RETRY_MAX_SLEEP,
                                   RETRY_BASE_SEC * (2 ** (attempt - 1))))
                continue

            except requests.RequestException as e:
                last = UpstreamError("retry", 0, "%s: %s" % (type(e).__name__, e))
                key.on_5xx(str(e))
                key.rotations += 1
                POOL.mark_dirty()
                log.warning("req=%s %s model=%s key=%s attempt=%d network=%s",
                            req_id, endpoint, model, key.label, attempt,
                            type(e).__name__)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(RETRY_MAX_SLEEP,
                                   RETRY_BASE_SEC * (2 ** (attempt - 1))))
                continue

            except Exception as e:  # неожиданное: не роняем соединение и не светим стек
                last = UpstreamError("retry", 0, "bridge error: %s: %s"
                                     % (type(e).__name__, e))
                key.rotations += 1
                POOL.mark_dirty()
                log.exception("req=%s %s model=%s key=%s attempt=%d unexpected error",
                              req_id, endpoint, model, key.label, attempt)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(RETRY_MAX_SLEEP,
                                   RETRY_BASE_SEC * (2 ** (attempt - 1))))
                continue

        # Все попытки исчерпаны.
        lat = int((time.time() - t0) * 1000)
        if last is None:
            code, msg, etype = 503, "no usable keys in pool", "no_keys"
            COUNTERS.inc("no_keys")
        else:
            msg = clean_error_text(last.text) or "upstream error"
            etype = "upstream_error"
            if last.kind == "exhausted" or (last.kind == "retry" and last.status == 429):
                code = 429
            elif last.kind == "client_error":
                code = last.status if 400 <= (last.status or 0) < 500 else 400
            else:
                code = 502
        COUNTERS.inc("errors")
        COUNTERS.status(code, endpoint)
        log.error("req=%s %s model=%s attempts=%d status=%d fail=%s",
                  req_id, endpoint, model, len(tried), code, scrub(msg)[:200])
        log_usage(req=req_id, endpoint=endpoint, model=model, status=code,
                  stream=stream, attempts=len(tried), latency_ms=lat,
                  error=scrub(msg)[:200])
        return self._error(code, msg, etype,
                           {"attempts": len(tried), "pool": POOL.stats()})

    def _penalize(self, key, e):
        if e.kind == "dead":
            key.mark_dead(e.text)
            key.last_status = e.status or 401
        elif e.kind == "exhausted":
            key.park(EXHAUSTED_PARK_SEC, "quota")
            key.last_status = e.status
        elif e.kind == "retry" and e.status == 429:
            key.on_429(e.text)
        elif e.kind == "retry":
            key.on_5xx(e.text)

    # ── стрим ──
    def _do_stream(self, req_id, key, url, payload, model, t0, attempt, endpoint):
        opened = call_stream_open(key, url, payload)
        if opened[0] == "json":
            # Апстрим отдал обычный JSON вместо SSE — отдаём как есть.
            data = opened[1]
            key.on_ok()
            POOL.mark_dirty()
            lat = int((time.time() - t0) * 1000)
            COUNTERS.inc("ok")
            COUNTERS.status(200, endpoint)
            log.info("req=%s stream-fallback-json model=%s key=%s attempt=%d "
                     "status=200 lat=%dms", req_id, model, key.label, attempt, lat)
            log_usage(req=req_id, endpoint=endpoint, model=model, key=key.label,
                      status=200, stream=False, attempt=attempt, latency_ms=lat)
            return self._json(200, data, {"X-Comet-Key": mask_ascii(key.raw),
                                          "X-Comet-Attempts": str(attempt)})

        _, r, first, it = opened
        key.on_ok()
        POOL.mark_dirty()
        start_lat = int((time.time() - t0) * 1000)
        log.info("req=%s stream-start model=%s key=%s attempt=%d ttfb=%dms",
                 req_id, model, key.label, attempt, start_lat)

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type",
                         r.headers.get("Content-Type") or "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Comet-Key", mask_ascii(key.raw))
        self.send_header("X-Comet-Attempts", str(attempt))
        self.end_headers()
        self.close_connection = True

        bytes_out = 0
        try:
            for line in first:
                data = (line + "\n").encode("utf-8")
                self.wfile.write(data)
                bytes_out += len(data)
            self.wfile.flush()
            for line in it:
                if line is None:
                    continue
                data = (line + "\n").encode("utf-8")
                self.wfile.write(data)
                bytes_out += len(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log.info("req=%s stream client disconnected after %d bytes",
                     req_id, bytes_out)
        except requests.RequestException as e:
            log.warning("req=%s stream broken: %s", req_id, type(e).__name__)
        finally:
            r.close()

        lat = int((time.time() - t0) * 1000)
        COUNTERS.inc("ok")
        COUNTERS.status(200, endpoint)
        log.info("req=%s stream-done model=%s key=%s attempt=%d lat=%dms bytes=%d",
                 req_id, model, key.label, attempt, lat, bytes_out)
        log_usage(req=req_id, endpoint=endpoint, model=model, key=key.label,
                  status=200, stream=True, attempt=attempt, latency_ms=lat,
                  bytes=bytes_out)


# ── фоновые нити ────────────────────────────────────────────────────────────

def bank_loop():
    time.sleep(1)
    while True:
        try:
            POOL.reload()
        except Exception:
            log.exception("bank reload failed")
        time.sleep(max(1.0, BANK_POLL_SEC))


def flush_loop():
    while True:
        time.sleep(2)
        try:
            POOL.save()
        except Exception:
            log.exception("state save failed")


def _shutdown(srv):
    def _stop(*_):
        log.info("shutdown signal received")
        POOL.save(force=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    foreground = "--foreground" in sys.argv or "-f" in sys.argv
    os.makedirs(DATA, exist_ok=True)
    setup_logging(foreground)

    info = POOL.reload(force=True)
    _set_raw_keys([k.raw for k in POOL.keys])
    log.info("start cometapi2api v%s on %s:%d upstream=%s bank=%s pool=%s",
             VERSION, HOST, PORT, UPSTREAM, os.path.basename(BANK_FILE),
             POOL.stats())
    if not POOL.keys:
        log.warning("bank has no keys yet: %s", os.path.basename(BANK_FILE))
    elif info.get("bad_lines"):
        log.warning("bank has %s bad lines", info.get("bad_lines"))

    threading.Thread(target=bank_loop, daemon=True, name="bank-watch").start()
    threading.Thread(target=flush_loop, daemon=True, name="state-flush").start()

    srv = Server((HOST, PORT), Handler)
    srv.daemon_threads = True
    _shutdown(srv)
    print("cometapi2api: http://%s:%d/v1  upstream=%s  pool=%s"
          % (HOST, PORT, UPSTREAM, POOL.stats()), flush=True)
    try:
        srv.serve_forever()
    finally:
        POOL.save(force=True)
        srv.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()