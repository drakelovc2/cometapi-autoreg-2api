#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recon — разведка страницы регистрации перед написанием реггера.

Одна команда вместо ручного разбора DevTools:

    python3 tools/recon.py https://service.example/signup
    python3 tools/recon.py URL --engine camoufox --headed --wait 8
    python3 tools/recon.py URL --proxy http://user:pass@host:port

Что снимает:
  * все формы и их поля (селектор, type, name, обязательность, плейсхолдер);
  * сетевые запросы: какие эндпоинты дёргаются при загрузке и отправке;
  * защиту: turnstile / recaptcha / hcaptcha / cloudflare challenge /
    datadome / perimeterx / kasada / akamai;
  * скрытые поля и input'ы, включая `input[name=code]`, `type=hidden`,
    contenteditable-ячейки OTP;
  * скриншот и HTML.

Пишет в data/recon/<host>-<ts>/:
  report.md        — читать глазами, здесь всё связно
  summary.json     — машинный итог
  skeleton.yaml    — заготовка адаптера под наш реггер
  requests.jsonl   — сетевые запросы
  page.png, page.html

Правило: селекторы брать ИЗ ОТЧЁТА, а не из памяти. Придуманный селектор —
самая частая причина «форма не заполнилась».
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import engines  # noqa: E402

STATIC_EXT = re.compile(
    r"\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|webp|avif|mp4|webm|wasm)"
    r"(?:$|\?)", re.I)

PROTECTION = {
    "cloudflare_turnstile": ("challenges.cloudflare.com/turnstile", "cf-turnstile", "turnstile"),
    "cloudflare_challenge": ("cdn-cgi/challenge-platform", "__cf_chl", "cf_chl_opt"),
    "hcaptcha": ("hcaptcha.com", "h-captcha"),
    "recaptcha": ("recaptcha.net", "google.com/recaptcha", "g-recaptcha"),
    "arkose": ("arkoselabs", "funcaptcha", "arkose"),
    "datadome": ("datadome", "dd_cookie"),
    "perimeterx": ("perimeterx", "px-cdn", "_px"),
    "kasada": ("kasada", "kpsdk"),
    "akamai": ("akamai", "bm-verify", "_abck"),
    "imperva": ("incapsula", "imperva", "_incap"),
    "aliyun_slider": ("aliyun", "nc_1_n1z", "nocaptcha", "scene"),
}

ENDPOINT_HINT = re.compile(
    r"(signup|sign-up|register|registration|create.?account|auth|login|"
    r"verify|verification|otp|code|email|user|account|onboard|invite|"
    r"sessions?|token|api/v\d)", re.I)

FORMS_JS = """() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return !!(r.width && r.height) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return '#' + CSS.escape(el.id);
    const name = el.getAttribute('name');
    if (name) {
      const t = el.tagName.toLowerCase();
      const same = document.querySelectorAll(t + '[name="' + CSS.escape(name) + '"]');
      return same.length === 1 ? t + '[name="' + CSS.escape(name) + '"]'
                               : t + '[name="' + CSS.escape(name) + '"]';
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      let part = cur.tagName.toLowerCase();
      const p = cur.parentElement;
      if (p) {
        const sibs = [...p.children].filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || null,
    name: el.getAttribute('name') || null,
    id: el.id || null,
    placeholder: el.getAttribute('placeholder') || null,
    autocomplete: el.getAttribute('autocomplete') || null,
    required: el.hasAttribute('required'),
    disabled: el.hasAttribute('disabled') || el.disabled === true,
    visible: vis(el),
    selector: cssPath(el),
    value_len: (el.value || '').length,
    aria_label: el.getAttribute('aria-label') || null,
    maxlength: el.getAttribute('maxlength') || null,
    inputmode: el.getAttribute('inputmode') || null,
  });
  const forms = [...document.querySelectorAll('form')].map((f, i) => ({
    index: i,
    id: f.id || null,
    action: f.getAttribute('action') || null,
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    selector: cssPath(f),
    visible: vis(f),
    fields: [...f.querySelectorAll('input, select, textarea, button, [contenteditable]')]
              .map(describe),
  }));
  const loose = [...document.querySelectorAll(
      'input:not(form input), select:not(form select), textarea:not(form textarea), ' +
      '[contenteditable]:not(form [contenteditable])')].map(describe);
  const codes = [...document.querySelectorAll(
      'input[autocomplete="one-time-code"], input[name*=code i], input[id*=code i], ' +
      'input[name*=otp i], input[maxlength="1"], input[inputmode="numeric"]')]
      .map(describe);
  const hidden = [...document.querySelectorAll('input[type=hidden]')].map(describe);
  const captcha = {
    turnstile: document.querySelectorAll('.cf-turnstile, [data-sitekey]').length,
    recaptcha: document.querySelectorAll('.g-recaptcha, textarea[name="g-recaptcha-response"]').length,
    hcaptcha: document.querySelectorAll('.h-captcha, textarea[name="h-captcha-response"]').length,
    aliyun: document.querySelectorAll('#nc_1_wrapper, .nc-container, [class*=nc_1]').length,
  };
  const sitekeys = [...document.querySelectorAll('[data-sitekey]')]
      .map(e => e.getAttribute('data-sitekey'));
  const iframes = [...document.querySelectorAll('iframe')].map(f => f.src || '');
  return {
    url: location.href, title: document.title,
    forms, loose, codes, hidden, captcha, sitekeys,
    iframes: iframes.filter(Boolean).slice(0, 30),
    bodyText: (document.body ? document.body.innerText : '').slice(0, 4000),
  };
}"""


def _protection_from_text(blob):
    low = (blob or "").lower()
    hits = []
    for name, needles in PROTECTION.items():
        if any(n.lower() in low for n in needles):
            hits.append(name)
    return hits


def _useful(url):
    if not url or url.startswith(("data:", "blob:", "about:")):
        return False
    if STATIC_EXT.search(url):
        return False
    return True


def recon(url, engine="auto", headed=False, wait=6.0, proxy=None,
          out_root=None, profile_root=None):
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_") or "site"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(out_root or os.path.join(ROOT, "data", "recon"),
                       f"{host}-{stamp}")
    os.makedirs(out, exist_ok=True)

    prof_root = profile_root or os.path.join(ROOT, "data", "profiles")
    os.makedirs(prof_root, exist_ok=True)
    profile = engines.make_profile(prof_root, account=f"recon-{host}",
                                   engine=engine if engine != "auto" else "camoufox")

    requests = []
    console = []

    eng = engines.resolve(engine, stealth=True)
    print(f"[recon] {url}\n[recon] движок: {eng} → {out}")

    with engines.launch(eng, profile, proxy=proxy, headless=not headed) as br:
        page = br.new_page()
        page.set_default_timeout(90000)

        def on_req(req):
            if _useful(req.url):
                try:
                    plen = len(req.post_data or "")
                except Exception:
                    plen = -1          # бинарное тело: декодировать нельзя
                requests.append({"method": req.method, "url": req.url,
                                 "resource": req.resource_type,
                                 "post_len": plen})

        def on_resp(resp):
            for rec in reversed(requests[-3:]):
                if rec["url"] == resp.url and "status" not in rec:
                    rec["status"] = resp.status
                    break

        def on_console(msg):
            if msg.type in ("error", "warning"):
                console.append(f"{msg.type}: {msg.text[:200]}")

        page.on("request", on_req)
        page.on("response", on_resp)
        page.on("console", on_console)

        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(int(wait * 1000))

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
        except Exception:
            pass

        data = page.evaluate(FORMS_JS)
        html = page.content()
        try:
            page.screenshot(path=os.path.join(out, "page.png"), full_page=True)
        except Exception:
            pass
        with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as f:
            f.write(html)

    # ── защита ──
    haystack = " ".join([data.get("url", ""), html[:200000]] +
                        [r["url"] for r in requests] +
                        [i for i in data.get("iframes", [])])
    prot = sorted(set(_protection_from_text(haystack)) |
                  {k for k, v in (data.get("captcha") or {}).items() if v})

    endpoints = sorted({r["url"] for r in requests
                        if ENDPOINT_HINT.search(r["url"])})[:60]

    summary = {
        "url": url, "final_url": data.get("url"), "title": data.get("title"),
        "engine": eng, "at": stamp,
        "protection": prot,
        "sitekeys": data.get("sitekeys") or [],
        "forms": data.get("forms") or [],
        "loose_fields": data.get("loose") or [],
        "code_fields": data.get("codes") or [],
        "hidden_fields": data.get("hidden") or [],
        "endpoints": endpoints,
        "requests_total": len(requests),
        "console": console[:40],
        "iframes": data.get("iframes") or [],
        "body_text": (data.get("bodyText") or "")[:2500],
    }

    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "requests.jsonl"), "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "skeleton.yaml"), "w", encoding="utf-8") as f:
        f.write(_skeleton(summary))
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_report(summary))

    return out, summary


def _yaml_str(value):
    """Строка для YAML. Селекторы содержат кавычки — экранируем, иначе
    `target: "input[name="email"]"` разваливает документ."""
    s = str(value)
    if "'" not in s:
        return "'" + s + "'"
    return '"' + s.replace('"', '\\"') + '"'


def _skeleton(s):
    """Заготовка описания флоу под наш реггер (не адаптер их формата)."""
    fields = []
    for f_ in s["forms"]:
        for el in f_["fields"]:
            if el["tag"] == "button":
                continue
            if el["type"] == "hidden":
                continue
            fields.append(el)
    if not fields:
        fields = s["loose_fields"][:12]

    def guess(el):
        blob = " ".join(str(el.get(k) or "") for k in ("name", "id", "type",
                                                       "placeholder", "aria_label"))
        low = blob.lower()
        if re.search(r"mail", low):
            return "email"
        if re.search(r"pass", low):
            return "password"
        if re.search(r"code|otp|otp", low):
            return "otp"
        if re.search(r"user|login|nick", low):
            return "username"
        if re.search(r"name", low):
            return "full_name"
        return None

    lines = [
        "# Заготовка от recon — проверь селекторы глазами, не доверяй слепо.",
        f"# источник: {s['url']}",
        f"mode: {'browser' if s['forms'] else 'protocol'}",
        "engine: auto",
        f"# защита: {', '.join(s['protection']) or 'не найдена'}",
        "",
        "steps:",
        f"  - goto: {_yaml_str(s['url'])}",
    ]
    for el in fields[:10]:
        role = guess(el)
        if not el.get("selector"):
            continue
        lines.append("  - fill:")
        lines.append(f"      target: {_yaml_str(el['selector'])}")
        lines.append(f"      value: '{'{{' + (role or 'value') + '}}'}'")
    if s["code_fields"]:
        lines.append("  - otp:")
        lines.append("      timeout: 180")
        el = s["code_fields"][0]
        if el.get("selector"):
            lines.append("  - fill:")
            lines.append(f"      target: {_yaml_str(el['selector'])}")
            lines.append("      value: '{{otp}}'")
    if s["protection"]:
        lines.insert(3, f"captcha:\n  provider: TODO  # из {s['protection']}")
    lines += [
        "",
        "# Кандидаты в эндпоинты (проверять запросом, не гадать):",
    ]
    for e in s["endpoints"][:15]:
        lines.append(f"#   {e}")
    return "\n".join(lines) + "\n"


def _report(s):
    out = [f"# recon: {s['url']}", "",
           f"- финальный URL: `{s['final_url']}`",
           f"- заголовок: `{s['title']}`",
           f"- движок: `{s['engine']}`",
           f"- запросов снято: {s['requests_total']}", ""]

    out += ["## Защита", ""]
    if s["protection"]:
        for p in s["protection"]:
            out.append(f"- **{p}**")
        if s["sitekeys"]:
            out.append(f"- sitekey: `{', '.join(s['sitekeys'][:5])}`")
    else:
        out.append("не найдена")
    out.append("")

    out += ["## Формы", ""]
    if not s["forms"]:
        out.append("форм нет — возможно, SPA: смотри `loose_fields` и эндпоинты.")
    for f_ in s["forms"]:
        out.append(f"### Форма #{f_['index']} `{f_.get('selector')}` "
                   f"({f_['method'].upper()} {f_.get('action') or '—'})")
        out.append("")
        out.append("| тег | type | name | селектор | обяз. | видим |")
        out.append("|---|---|---|---|---|---|")
        for el in f_["fields"]:
            out.append(f"| {el['tag']} | {el.get('type') or ''} | "
                       f"{el.get('name') or ''} | `{el.get('selector')}` | "
                       f"{'да' if el['required'] else ''} | "
                       f"{'да' if el['visible'] else 'нет'} |")
        out.append("")

    if s["loose_fields"]:
        out += ["## Поля вне форм", ""]
        for el in s["loose_fields"][:20]:
            out.append(f"- `{el.get('selector')}` type={el.get('type')} "
                       f"name={el.get('name')} visible={el['visible']}")
        out.append("")

    if s["code_fields"]:
        out += ["## Поля кода / OTP", ""]
        for el in s["code_fields"][:10]:
            out.append(f"- `{el.get('selector')}` autocomplete={el.get('autocomplete')} "
                       f"maxlength={el.get('maxlength')}")
        out.append("")

    if s["hidden_fields"]:
        out += ["## Скрытые поля", ""]
        for el in s["hidden_fields"][:15]:
            out.append(f"- `{el.get('selector')}` name={el.get('name')}")
        out.append("")

    out += ["## Эндпоинты-кандидаты", ""]
    for e in s["endpoints"][:40]:
        out.append(f"- `{e}`")
    out.append("")

    if s["iframes"]:
        out += ["## iframe", ""]
        for i in s["iframes"][:10]:
            out.append(f"- `{i}`")
        out.append("")

    if s["console"]:
        out += ["## Консоль", ""]
        for c in s["console"][:15]:
            out.append(f"- {c}")
        out.append("")

    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Разведка страницы регистрации")
    ap.add_argument("url")
    ap.add_argument("--engine", default="auto",
                    choices=list(engines._ENGINES))
    ap.add_argument("--headed", action="store_true", help="видимое окно")
    ap.add_argument("--wait", type=float, default=6.0, help="секунд после загрузки")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--out", default=None, help="каталог для отчёта")
    args = ap.parse_args()

    try:
        out, summary = recon(args.url, engine=args.engine, headed=args.headed,
                             wait=args.wait, proxy=args.proxy, out_root=args.out)
    except Exception as exc:  # noqa: BLE001
        print(f"[recon] ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\n[recon] готово: {out}")
    print(f"  защита:   {', '.join(summary['protection']) or 'нет'}")
    print(f"  форм:     {len(summary['forms'])}  "
          f"(полей OTP: {len(summary['code_fields'])})")
    print(f"  эндпоинтов: {len(summary['endpoints'])}")
    print(f"\n  читать: {os.path.join(out, 'report.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())