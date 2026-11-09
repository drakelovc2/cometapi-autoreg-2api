#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_site — локальная цель для регрессии шаблона. Сеть не нужна.

    python3 tools/mock_site.py --port 8765

Отдаёт минимальный, но честный флоу регистрации:

  GET  /signup          форма email + password
  POST /signup          создать аккаунт, «отправить» код (лежит в памяти)
  GET  /mail?to=EMAIL   JSON список писем для адреса (аналог apimail)
  GET  /verify?email=&code=   подтвердить
  GET  /welcome         страница успеха (её ищет verify-селектор)
  GET  /api/state       JSON состояние всех аккаунтов

Нужен для `tests/selftest_browser.py`: после любой правки движка, почты или
флоу прогоняем selftest и убеждаемся, что регистрация, чтение кода,
подтверждение и отпечаток профиля по-прежнему работают.
"""
import argparse
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {}   # email -> {"password", "code", "verified", "mails": [...]}


def _page(title, body):
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title>"
            f"</head><body><h1>{title}</h1>{body}</body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/signup"):
            return self._send(200, _page("Sign up", """
              <form id="signup" method="post" action="/signup">
                <input type="email" name="email" placeholder="you@example.com" required>
                <input type="password" name="password" required>
                <button type="submit">Create account</button>
              </form>"""))

        if u.path == "/mail":
            addr = (q.get("to") or [""])[0]
            rec = STATE.get(addr)
            return self._json({"results": rec["mails"] if rec else []})

        if u.path == "/verify":
            addr = (q.get("email") or [""])[0]
            code = (q.get("code") or [""])[0]
            rec = STATE.get(addr)
            if not rec:
                return self._send(404, _page("Not found", "<p>unknown account</p>"))
            if rec["code"] != code:
                return self._send(400, _page("Bad code", "<p id=err>invalid code</p>"))
            rec["verified"] = True
            return self._send(200, _page("Verified",
                                         "<p id=ok>Email verified</p>"
                                         "<a href='/welcome'>continue</a>"))

        if u.path == "/welcome":
            return self._send(200, _page("Welcome", "<p id=welcome>Welcome aboard</p>"))

        if u.path == "/api/state":
            return self._json(STATE)

        return self._send(404, _page("Not found", ""))

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "ignore") if n else ""

        if u.path != "/signup":
            return self._send(404, _page("Not found", ""))

        ctype = (self.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            try:
                data = json.loads(raw)
            except ValueError:
                return self._json({"error": "bad json"}, 400)
        else:
            data = {k: v[0] for k, v in parse_qs(raw).items()}

        email = (data.get("email") or "").strip()
        password = data.get("password") or ""
        if not email or "@" not in email or not password:
            return self._send(400, _page("Bad request", "<p id=err>email/password required</p>"))

        code = f"{random.randint(0, 999999):06d}"
        rec = STATE.setdefault(email, {"password": "", "code": "", "verified": False,
                                       "mails": []})
        rec["password"] = password
        rec["code"] = code
        rec["verified"] = False
        rec["mails"].append({
            "id": len(rec["mails"]) + 1,
            "to": email,
            "subject": "Verify your email",
            "raw": (f"From: Mock Service <no-reply@mock.local>\r\n"
                    f"To: {email}\r\n"
                    f"Subject: Verify your email\r\n\r\n"
                    f"Your verification code is {code}.\r\n"
                    f"Or open: http://127.0.0.1/mock/verify?email={email}&code={code}\r\n"),
        })
        return self._send(200, _page("Check your email",
                                     "<p id=sent>We sent a code to your inbox</p>"))


def serve(port=0):
    """Поднять mock-сайт в фоновом потоке. Возвращает (url, server)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"mock_site: http://127.0.0.1:{args.port}/signup")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()