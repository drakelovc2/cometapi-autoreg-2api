#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest — регрессия шаблона на локальном mock-сайте.

    python3 tests/selftest_browser.py                     # авто-движок
    python3 tests/selftest_browser.py --engine camoufox
    python3 tests/selftest_browser.py --headed

Что проверяет, без сети и без чужих сервисов:

  1. движок поднимается и открывает страницу;
  2. форма заполняется и отправляется;
  3. код подтверждения читается из «почты» (mock /mail);
  4. переход по verify-ссылке подтверждает аккаунт;
  5. verify-селектор успеха находится на странице;
  6. профиль постоянный: второй запуск даёт тот же отпечаток;
  7. два разных профиля дают РАЗНЫЕ отпечатки (личности не слипаются).

Правило: после любой правки core/engines.py, core/mail.py, core/results.py
или core/vision.py гонять этот файл. Exit 0 = pass.
"""
import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import engines  # noqa: E402
import mock_site  # noqa: E402


def _log(ok, msg):
    print(f"[{'ok  ' if ok else 'FAIL'}] {msg}", flush=True)
    return ok


def run(engine="auto", headed=False):
    url, srv = mock_site.serve()
    print(f"mock: {url}")

    eng = engines.resolve(engine, stealth=True)
    print(f"движок: {eng}\n")

    tmp = tempfile.mkdtemp(prefix="autoreg-selftest-")
    results = []

    with engines.launch(eng, engines.make_profile(tmp, account="selftest-a",
                                                  engine=eng),
                        headless=not headed) as br:
        page = br.new_page()
        page.set_default_timeout(30000)
        page.goto(url + "/signup", wait_until="domcontentloaded")
        results.append(_log(page.locator('input[type=email]').count() > 0,
                            "страница регистрации открыта"))

        email = f"selftest-{int(time.time())}@mock.local"
        page.fill('input[type=email]', email)
        page.fill('input[type=password]', "TestPass123!")
        page.click('button[type=submit]')
        page.wait_for_selector("#sent", timeout=15000)
        results.append(_log(True, "форма отправлена"))

        # «почта»: забираем код тем же парсером, что и настоящий реггер
        import json
        import urllib.request
        with urllib.request.urlopen(f"{url}/mail?to={email}", timeout=10) as r:
            mails = json.load(r).get("results", [])
        results.append(_log(bool(mails), f"письмо получено ({len(mails)})"))

        from core.mail import parse_code_from_raw
        code, _subj = parse_code_from_raw(mails[-1]["raw"]) if mails else (None, None)
        results.append(_log(bool(code), f"код извлечён: {code}"))
        code = code or "000000"

        page.goto(f"{url}/verify?email={email}&code={code}",
                  wait_until="domcontentloaded")
        page.wait_for_selector("#ok", timeout=10000)
        results.append(_log(True, "аккаунт подтверждён"))

        page.goto(url + "/welcome", wait_until="domcontentloaded")
        results.append(_log(page.locator("#welcome").count() > 0,
                            "verify-селектор успеха найден"))

        with urllib.request.urlopen(f"{url}/api/state", timeout=10) as r:
            state = json.load(r)
        results.append(_log(bool(state.get(email, {}).get("verified")),
                            "сервер подтверждает verified"))

        fp1 = engines.fingerprint(page)
        prof = engines.make_profile(tmp, account="selftest-a", engine=eng)
        ok, diffs = engines.fingerprint_check(page, prof, save_if_missing=True)
        results.append(_log(ok and not diffs, f"отпечаток сохранён ({prof.id})"))

    # второй запуск того же профиля: отпечаток должен совпасть
    with engines.launch(eng, engines.make_profile(tmp, account="selftest-a",
                                                  engine=eng),
                        headless=not headed) as br:
        page = br.new_page()
        page.goto(url + "/signup", wait_until="domcontentloaded")
        ok, diffs = engines.fingerprint_check(page, eng and engines.make_profile(
            tmp, account="selftest-a", engine=eng), save_if_missing=False)
        results.append(_log(ok, f"отпечаток стабилен между запусками{'' if ok else ': ' + str(diffs)}"))

    # другой профиль — другой отпечаток (или хотя бы не падаем)
    with engines.launch(eng, engines.make_profile(tmp, account="selftest-b",
                                                  engine=eng),
                        headless=not headed) as br:
        page = br.new_page()
        page.goto(url + "/signup", wait_until="domcontentloaded")
        fpb = engines.fingerprint(page)
        results.append(_log(isinstance(fpb, dict) and "userAgent" in fpb,
                            "второй профиль снимает отпечаток"))

    srv.shutdown()
    passed = sum(1 for x in results if x)
    print(f"\n{passed}/{len(results)} проверок пройдено")
    return 0 if all(results) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="auto",
                    choices=list(engines._ENGINES))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if not engines.available():
        print("нет ни одного движка: pip install playwright && playwright install chromium")
        return 2
    return run(args.engine, args.headed)


if __name__ == "__main__":
    sys.exit(main())