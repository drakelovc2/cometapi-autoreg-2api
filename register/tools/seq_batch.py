#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Последовательный добор аккаунтов: адаптивная пауза под антифрод CometAPI."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core.register import register_one                # noqa: E402
from core.results import save_account, load_accounts  # noqa: E402

INDICES = [16] + list(range(18, 33))
PAUSE_MIN = 90          # быстрее не имеет смысла: антифрод начинает резать
PAUSE_START = 90        # оператор попросил сразу максимальную скорость
PAUSE_MAX = 600
PAUSE_STEP_DOWN = 30    # после успеха ускоряемся
PAUSE_STEP_UP = 180     # после блока тормозим
PAUSE_ERR = 45


def done_idx():
    return {r.get("pool_index") for r in load_accounts()
            if r.get("pool_index") is not None and r.get("auth") != "manual_key"}


def main():
    pause = PAUSE_START
    for idx in INDICES:
        if idx in done_idx():
            print(f"[seq] idx {idx} уже в банке — пропуск", flush=True)
            continue
        for attempt in (1, 2, 3):
            print(f"[seq] ===== idx {idx} попытка {attempt} =====", flush=True)
            try:
                rec = register_one(index=idx)
            except Exception as e:
                print(f"[seq] EXC idx {idx}: {type(e).__name__}: {str(e)[:160]}",
                      flush=True)
                time.sleep(PAUSE_ERR)
                continue
            if rec.get("error"):
                msg = str(rec["error"])
                print(f"[seq] FAIL idx {idx}: {msg[:240]}", flush=True)
                if ("security check" in msg or "сессия не появилась" in msg
                        or "Castle" in msg or "blocked" in msg or "429" in msg):
                    pause = min(PAUSE_MAX, pause + PAUSE_STEP_UP)
                    print(f"[seq] блок → пауза {pause}s", flush=True)
                    time.sleep(pause)
                else:
                    time.sleep(PAUSE_ERR)
                continue
            save_account(rec)
            b = rec.get("balance") or {}
            print(f"[seq] OK idx {idx} {rec.get('github_username')} "
                  f"{rec.get('api_key', '')[:9]}… quota={b.get('quota')} "
                  f"usd={b.get('usd_left')}", flush=True)
            pause = max(PAUSE_MIN, pause - PAUSE_STEP_DOWN)
            print(f"[seq] следующая пауза {pause}s", flush=True)
            time.sleep(pause)
            break
        else:
            print(f"[seq] idx {idx} сдался после 3 попыток", flush=True)
    print("[seq] done", flush=True)


if __name__ == "__main__":
    main()
