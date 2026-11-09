# -*- coding: utf-8 -*-
"""Атомарная запись результатов: JSONL (полные данные) + combo txt.

Пишем СРАЗУ после каждого аккаунта — падение на 90-м не теряет 89.
combo-формат: email|password|api_key
"""
import contextlib
import fcntl
import json
import os
import threading
import time

from . import config

_lock = threading.RLock()
# Резервные имена для параллельных писателей (несколько процессов реггера):
# каждый пишет свой tmp, чтобы не перетирать чужой посреди записи.
_tmp_seq = 0


def _tmp_name(path):
    """Уникальный tmp в той же папке: os.replace атомарен только в пределах ФС."""
    global _tmp_seq
    with _lock:
        _tmp_seq += 1
        return f"{path}.tmp.{os.getpid()}.{_tmp_seq}"


@contextlib.contextmanager
def _file_lock(path):
    """Межпроцессный лок на банк: параллельные воркеры не теряют чужие строки."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _atomic_append(path, line):
    tmp = _tmp_name(path)
    with _file_lock(path):
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(existing + line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def save_account(account: dict):
    """account: {email, password, api_key, free_tier_enabled, created_at, ...}

    Идемпотентно по email: повторное сохранение (resume, ретрай) заменяет
    прежнюю строку, а не плодит дубли — иначе один аккаунт считается
    дважды и в панели, и в выгрузке.

    Чтение и запись идут под ОДНИМ локом: при параллельных воркерах
    read-modify-write вне лока терял чужие строки.
    """
    os.makedirs(os.path.dirname(config.OUTPUT_JSONL), exist_ok=True)
    email = account.get("email")
    line = json.dumps(account, ensure_ascii=False) + "\n"
    tmp = _tmp_name(config.OUTPUT_JSONL)
    with _file_lock(config.OUTPUT_JSONL):
        existing = []
        if email and os.path.exists(config.OUTPUT_JSONL):
            with open(config.OUTPUT_JSONL, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if rec.get("email") != email:
                        existing.append(rec)
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in existing:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.OUTPUT_JSONL)
    combo = f"{account.get('email','')}|{account.get('password','')}|{account.get('api_key','')}"
    _atomic_append(config.OUTPUT_COMBO, combo + "\n")


def load_accounts():
    if not os.path.exists(config.OUTPUT_JSONL):
        return []
    out = []
    with open(config.OUTPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


# ── two-phase: pending ────────────────────────────────────────────────────
# Случай: сервис отвечает «письмо отправлено», но само письмо приходит
# через 20–60 минут либо почта упёрлась в лимит. Держать процесс живым всё
# это время бессмысленно — аккаунт пишется в pending вместе со снимком почты
# (baseline), а `verify` позже берёт ЕГО и ждёт только письма, пришедшие
# после регистрации.

def _pending_path():
    return getattr(config, "OUTPUT_PENDING",
                   os.path.join(os.path.dirname(config.OUTPUT_JSONL), "pending.jsonl"))


def save_pending(account: dict):
    """Записать аккаунт как ожидающий подтверждения.

    `mail_baseline` (см. MailClient.baseline) обязателен — без него verify
    не отличит старое письмо от нового.
    """
    rec = dict(account)
    rec.setdefault("status", "pending")
    rec["pending_at"] = rec.get("created_at") or time.time()
    _atomic_append(_pending_path(), json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_pending():
    path = _pending_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    # Ждём любой незавершённый статус: реггер может записать
    # pending_verify (письмо не пришло), registered_unverified (без почты),
    # key_unverified (ключ создан, но не ответил).
    return [r for r in out
            if str(r.get("status", "pending")).startswith("pending")
            or r.get("status") in ("registered_unverified", "key_unverified", None)]


def resolve_pending(email, account=None, status="verified"):
    """Пометить pending как verified (переложив в accounts.jsonl) или dropped."""
    path = _pending_path()
    if not os.path.exists(path):
        return False
    kept, found = [], False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("email") == email and (
                    str(rec.get("status", "pending")).startswith("pending")
                    or rec.get("status") in ("registered_unverified",
                                             "key_unverified", None)):
                found = True
                if status == "verified":
                    merged = dict(rec)
                    merged.update(account or {})
                    merged["status"] = "verified"
                    merged["verified_at"] = time.time()
                    save_account(merged)
                continue
            kept.append(rec)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return found
