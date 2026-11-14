"""Owner activation, browser registration, and session checks behind Nginx."""

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
from html import escape
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from io import BytesIO
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
from threading import BoundedSemaphore
import time
from urllib.parse import parse_qs, quote

from cryptography.fernet import Fernet
import qrcode
from qrcode.image.svg import SvgPathFillImage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared_memory_client import OWNER_SOCKET, request as shared_memory_request

MEMORY_STYLE = '.memory{max-width:72ch;margin:1.5rem auto;padding:0 1rem}.memory pre{white-space:pre-wrap;overflow-wrap:anywhere}.memory article{margin:2rem 0}.memory button{padding:.6rem 1rem}.memory input:focus-visible,.memory button:focus-visible,.memory a:focus-visible{outline:2px solid;outline-offset:3px}'
MEMORY_STYLE_HASH = base64.b64encode(hashlib.sha256(MEMORY_STYLE.encode()).digest()).decode()

STATE = Path("/var/lib/ai-portal")
KEY = Path("/etc/ai-bootstrap/portal.key")
OWNER_PREFIX = "/_owner/"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
BROWSER_ACTION = re.compile(r"/browsers/([0-9a-f]{64})/(rename|revoke)")
BROWSER_NAME_LIMIT = 80
TOTP_DIGITS = 6
TOTP_PERIOD = 30
CODE = re.compile(rf"[0-9]{{{TOTP_DIGITS}}}")
HOUR = 60 * 60
ACTIVATION_LIFETIME = 24 * HOUR
SESSION_LIFETIME = 30 * ACTIVATION_LIFETIME
FAILURE_LIMIT = 3
STREAM_CHECK_INTERVAL = 1
# Gunicorn runs four threads per worker. Reserve capacity for portal requests.
STREAM_SLOTS = BoundedSemaphore(3)

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    secret BLOB NOT NULL,
    activation BLOB,
    activation_expires INTEGER NOT NULL,
    registration BLOB NOT NULL,
    last_counter INTEGER
);
CREATE TABLE IF NOT EXISTS failures (
    occurred_at INTEGER NOT NULL,
    ip TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT 'Браузер',
    registered_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL
);
"""


def digest(token):
    return hashlib.sha256(token.encode("ascii")).digest()


def totp(secret, counter):
    mac = hmac.digest(secret, counter.to_bytes(8, "big"), "sha1")
    offset = mac[-1] & 15
    value = int.from_bytes(mac[offset:offset + 4], "big") & 0x7fffffff
    return f"{value % (10 ** TOTP_DIGITS):0{TOTP_DIGITS}d}"


class OwnerAccess:
    def __init__(self, state, key, clock):
        self.database = state / "portal.sqlite3"
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.clock = clock

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA cache_size = -2048")
        return db

    def initialize(self):
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "registered_at" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT 'Браузер'")
                db.execute("ALTER TABLE sessions ADD COLUMN registered_at INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE sessions ADD COLUMN last_active_at INTEGER NOT NULL DEFAULT 0")
                # Before sliding expiration, expiry was always registration plus 30 days.
                db.execute(
                    "UPDATE sessions SET registered_at = expires_at - ?, last_active_at = expires_at - ?",
                    (SESSION_LIFETIME, SESSION_LIFETIME),
                )

    def provision(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = int(self.clock())
            owner = db.execute("SELECT * FROM owner WHERE id = 1").fetchone()
            if owner and owner["activation"] is None:
                return None
            if owner and owner["activation_expires"] > now:
                return OWNER_PREFIX + self.cipher.decrypt(owner["activation"]).decode("ascii")
            return self.issue_activation(db, now)

    def issue_activation(self, db, now):
        activation = secrets.token_urlsafe(32)
        db.execute(
            """INSERT OR REPLACE INTO owner (id, secret, activation, activation_expires, registration)
               VALUES (1, ?, ?, ?, ?)""",
            (
                self.cipher.encrypt(secrets.token_bytes(20)),
                self.cipher.encrypt(activation.encode("ascii")),
                now + ACTIVATION_LIFETIME,
                self.cipher.encrypt(secrets.token_urlsafe(32).encode("ascii")),
            ),
        )
        return OWNER_PREFIX + activation

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM failures")
            return self.issue_activation(db, int(self.clock()))

    def registration_kind(self, owner, token, now):
        if owner is None:
            return None
        if owner["activation"] is not None:
            if now < owner["activation_expires"] and hmac.compare_digest(
                token, self.cipher.decrypt(owner["activation"])
            ):
                return "activation"
            return None
        if hmac.compare_digest(token, self.cipher.decrypt(owner["registration"])):
            return "registration"
        return None

    def registration_page(self, token):
        with closing(self.connect()) as db:
            owner = db.execute("SELECT * FROM owner WHERE id = 1").fetchone()
            kind = self.registration_kind(owner, token, int(self.clock()))
            if kind == "activation":
                return kind, self.cipher.decrypt(owner["secret"])
            return kind, None

    def rotate_registration(self):
        with closing(self.connect()) as db, db:
            token = secrets.token_urlsafe(32)
            updated = db.execute(
                "UPDATE owner SET registration = ? WHERE id = 1 AND activation IS NULL",
                (self.cipher.encrypt(token.encode("ascii")),),
            )
            return OWNER_PREFIX + token if updated.rowcount else None

    def register(self, token, code, ip):
        # Serialize lookup, rate limits, TOTP consumption and session issuance across workers.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = int(self.clock())
            owner = db.execute("SELECT * FROM owner WHERE id = 1").fetchone()
            if self.registration_kind(owner, token, now) is None:
                return HTTPStatus.NOT_FOUND, None

            db.execute("DELETE FROM failures WHERE occurred_at <= ?", (now - HOUR,))
            failures = db.execute(
                "SELECT COUNT(ip = ? OR NULL), COUNT(*) FROM failures", (ip,)
            ).fetchone()
            if any(count >= FAILURE_LIMIT for count in failures):
                return HTTPStatus.TOO_MANY_REQUESTS, None

            counter = now // TOTP_PERIOD
            secret = self.cipher.decrypt(owner["secret"])
            last_counter = owner["last_counter"]
            # A code from the previous step may still be in transit. If two
            # adjacent steps share a code, neither may have been used already.
            matches = [
                step for step in (counter, counter - 1)
                if hmac.compare_digest(totp(secret, step), code)
            ]
            if matches and (last_counter is None or min(matches) > last_counter):
                session = secrets.token_urlsafe(32)
                db.execute(
                    "UPDATE owner SET activation = NULL, last_counter = ? WHERE id = 1",
                    (matches[0],),
                )
                db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
                db.execute(
                    """INSERT INTO sessions (token_hash, expires_at, registered_at, last_active_at)
                       VALUES (?, ?, ?, ?)""",
                    (digest(session), now + SESSION_LIFETIME, now, now),
                )
                return HTTPStatus.SEE_OTHER, session

            db.execute("INSERT INTO failures VALUES (?, ?)", (now, ip))
            return HTTPStatus.BAD_REQUEST, None

    @staticmethod
    def active_session(db, token_hash, now):
        return db.execute(
            "SELECT 1 FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone() is not None

    def has_session(self, token):
        if not TOKEN.fullmatch(token):
            return False
        with closing(self.connect()) as db:
            return self.active_session(db, digest(token), int(self.clock()))

    @staticmethod
    def renew_session(db, token_hash, now):
        db.execute(
            """UPDATE sessions SET last_active_at = MAX(last_active_at, ?),
               expires_at = MAX(expires_at, ?) WHERE token_hash = ?""",
            (now, now + SESSION_LIFETIME, token_hash),
        )

    def dashboard(self, token_hash, *, renew):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if renew else "BEGIN")
            now = int(self.clock())
            if not self.active_session(db, token_hash, now):
                return None
            if renew:
                self.renew_session(db, token_hash, now)
            owner = db.execute("SELECT registration FROM owner WHERE id = 1").fetchone()
            link = OWNER_PREFIX + self.cipher.decrypt(owner["registration"]).decode("ascii")
            browsers = db.execute(
                """SELECT * FROM sessions WHERE expires_at > ?
                   ORDER BY registered_at, token_hash""",
                (now,),
            ).fetchall()
            return link, browsers

    def change_browser(self, token_hash, browser_hash, action, name):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = int(self.clock())
            if not self.active_session(db, token_hash, now):
                return False, False
            if action == "revoke":
                changed = db.execute(
                    "DELETE FROM sessions WHERE token_hash = ? AND expires_at > ?",
                    (browser_hash, now),
                )
            else:
                changed = db.execute(
                    "UPDATE sessions SET name = ? WHERE token_hash = ? AND expires_at > ?",
                    (name, browser_hash, now),
                )
            if not changed.rowcount:
                return False, False
            current_revoked = action == "revoke" and token_hash == browser_hash
            if not current_revoked:
                self.renew_session(db, token_hash, now)
            return True, not current_revoked


def session_cookie(header):
    try:
        cookie = SimpleCookie(header).get("ai_session")
    except CookieError:
        return ""
    return cookie.value if cookie else ""


def submitted_form(environ, *, max_fields, max_length):
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return {}
    try:
        length = int(environ.get("CONTENT_LENGTH", "0"))
        if not 0 < length <= max_length:
            return {}
        fields = parse_qs(environ["wsgi.input"].read(length).decode("ascii"),
                          max_num_fields=max_fields, errors="strict")
    except (ValueError, UnicodeError):
        return {}
    return {name: values[0] for name, values in fields.items() if len(values) == 1}


def foreground_navigation(environ):
    return (
        environ.get("HTTP_SEC_FETCH_MODE") == "navigate"
        and environ.get("HTTP_SEC_FETCH_DEST") == "document"
        and not environ.get("HTTP_SEC_PURPOSE")
        and not environ.get("HTTP_PURPOSE")
    )


def csrf_token(session):
    return hmac.digest(session.encode("ascii"), b"browser management", "sha256").hex()


def github_request(action, **fields):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect("/run/ai-bootstrap-github.sock")
        connection.sendall(json.dumps({"action": action, **fields}).encode() + b"\n")
        with connection.makefile("rb") as reply:
            return json.loads(reply.readline(2 * 1024 * 1024))


def codex_request(action, *, socket_path="/run/ai-bootstrap-codex.sock", **fields):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(socket_path)
        connection.sendall(json.dumps({"action": action, **fields}).encode() + b"\n")
        with connection.makefile("rb") as reply:
            return json.loads(reply.readline(65536))


def respond(start_response, status, body="", content_type="text/plain; charset=utf-8", headers=()):
    start_response(
        f"{status.value} {status.phrase}",
        [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'none'; img-src data:; form-action 'self'; "
                                        "frame-ancestors 'none'; base-uri 'none'; "
                                        f"style-src 'sha256-{MEMORY_STYLE_HASH}'"),
            ("X-Content-Type-Options", "nosniff"),
            *headers,
        ],
    )
    return [body.encode("utf-8")]


def html_page(title, content):
    return f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><h1>{title}</h1>{content}</html>
"""


def enrollment_qr(secret):
    encoded = base64.b32encode(secret).decode("ascii")
    label = quote("ai-bootstrap:owner")
    uri = (
        f"otpauth://totp/{label}?secret={encoded}&issuer=ai-bootstrap"
        f"&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )
    output = BytesIO()
    qrcode.make(uri, image_factory=SvgPathFillImage).save(output)
    qr = base64.b64encode(output.getvalue()).decode("ascii")
    return (
        '<p>Отсканируйте QR-код в Google Authenticator или введите ключ вручную.</p>'
        f'<img src="data:image/svg+xml;base64,{qr}" width="256" height="256" alt="QR-код TOTP">'
        f"<p>Ключ для ручного ввода <code>{encoded}</code></p>"
    )


def browser_row(browser, current_hash, csrf):
    browser_hash = browser["token_hash"]
    path = f"/browsers/{browser_hash.hex()}"
    name = escape(browser["name"])
    current = " (текущий)" if browser_hash == current_hash else ""
    dates = [
        datetime.fromtimestamp(browser[field], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for field in ("registered_at", "last_active_at", "expires_at")
    ]
    hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
    return f"""<tr><th scope="row">{name}{current}</th>
<td>{dates[0]}</td><td>{dates[1]}</td><td>{dates[2]}</td>
<td><form method="post" action="{path}/rename">{hidden}
<label>Новое имя <input name="name" value="{name}" maxlength="{BROWSER_NAME_LIMIT}" required></label>
<button>Переименовать</button></form>
<form method="post" action="{path}/revoke">{hidden}
<button>Отозвать сессию</button></form></td></tr>"""


class Portal:
    def __init__(self, state, key, *, clock=time.time, secure=False, codex_socket="/run/ai-bootstrap-codex.sock",
                 shared_memory_socket=OWNER_SOCKET):
        self.owner = OwnerAccess(state, key, clock)
        self.secure = secure
        self.codex_socket = codex_socket
        self.shared_memory_socket = shared_memory_socket

    def __call__(self, environ, start_response):
        path = environ["PATH_INFO"]
        method = environ["REQUEST_METHOD"]
        session = session_cookie(environ.get("HTTP_COOKIE", ""))
        if path == "/_session":
            status = HTTPStatus.NO_CONTENT if self.owner.has_session(session) else HTTPStatus.FORBIDDEN
            return respond(start_response, status)

        if path.startswith(OWNER_PREFIX):
            token = path.removeprefix(OWNER_PREFIX)
            if TOKEN.fullmatch(token):
                return self.registration(environ, start_response, token.encode("ascii"))
        elif TOKEN.fullmatch(session):
            if method == "GET" and path == "/":
                return self.dashboard(environ, start_response, session)
            action = BROWSER_ACTION.fullmatch(path)
            if method == "POST" and action:
                return self.change_browser(environ, start_response, session, *action.groups())
            if path in ("/projects", "/projects/add", "/projects/delete", "/projects/canvas", "/github/connect"):
                if self.owner.has_session(session):
                    return self.projects(environ, start_response, session, path, method)
            if path in ("/codex", "/codex/login", "/codex/profile") and self.owner.has_session(session):
                return self.codex(environ, start_response, session, path, method)
            if path in ("/memory", "/memory/approve") and self.owner.has_session(session):
                return self.shared_memory(environ, start_response, session, path, method)
            if method == "GET" and self.owner.has_session(session):
                if path == "/api/status":
                    return respond(start_response, HTTPStatus.OK, '{"status":"ok"}\n', "application/json")
                if path == "/api/test-stream":
                    return self.stream(start_response, session)

        return respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")

    def stream(self, start_response, session):
        if not STREAM_SLOTS.acquire(blocking=False):
            yield from respond(start_response, HTTPStatus.SERVICE_UNAVAILABLE, "Stream limit reached\n")
            return
        try:
            if not self.owner.has_session(session):
                yield from respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")
                return
            start_response("200 OK", [
                ("Content-Type", "text/event-stream"),
                ("Cache-Control", "no-store"),
            ])
            yield b"data: ready\n\n"
            while True:
                time.sleep(STREAM_CHECK_INTERVAL)
                # Nginx checks only the initial request. Poll SQLite so revocation
                # from another worker or the admin command closes this connection.
                if not self.owner.has_session(session):
                    return
                yield b": keepalive\n\n"
        finally:
            STREAM_SLOTS.release()

    def cookie(self, session, lifetime=SESSION_LIFETIME):
        value = f"ai_session={session}; Path=/; Max-Age={lifetime}; HttpOnly; SameSite=Strict"
        return "Set-Cookie", value + ("; Secure" if self.secure else "")

    def dashboard(self, environ, start_response, session):
        renew = foreground_navigation(environ)
        token_hash = digest(session)
        dashboard = self.owner.dashboard(token_hash, renew=renew)
        if dashboard is None:
            return respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")
        link, browsers = dashboard
        csrf = csrf_token(session)
        rows = "".join(browser_row(browser, token_hash, csrf) for browser in browsers)
        page = html_page("ai-bootstrap", f"""
<p><a href="/projects">Проекты</a></p>
<p><a href="/codex">Подключение Codex</a></p>
<p><a href="/memory">Общая память</a></p>
<p><a href="{link}">Зарегистрировать браузер</a></p>
<h2>Зарегистрированные браузеры</h2>
<table><thead><tr><th>Имя</th><th>Регистрация</th><th>Последняя активность</th>
<th>Срок действия</th><th>Управление</th></tr></thead><tbody>{rows}</tbody></table>""")
        headers = (self.cookie(session),) if renew else ()
        return respond(start_response, HTTPStatus.OK, page, "text/html; charset=utf-8", headers)

    def shared_memory(self, environ, start_response, session, path, method):
        if (method, path) not in (("GET", "/memory"), ("POST", "/memory/approve")):
            return respond(start_response, HTTPStatus.NOT_FOUND)
        csrf = csrf_token(session)
        back = '<p><a href="/memory">К предложениям</a> · <a href="/">Главная</a></p>'
        try:
            if method == 'POST':
                form = submitted_form(environ, max_fields=4, max_length=1024)
                if not hmac.compare_digest(form.get('csrf', '').encode(), csrf.encode()):
                    return respond(start_response, HTTPStatus.FORBIDDEN, 'Недействительная форма. Обновите страницу.\n')
                if (set(form) != {'csrf', 'uuid', 'revision', 'confirm'} or form['confirm'] != 'yes'
                        or not re.fullmatch(r'[0-9a-f-]{36}', form['uuid'])
                        or not re.fullmatch(r'[0-9a-f]{64}', form['revision'])):
                    return respond(start_response, HTTPStatus.BAD_REQUEST, 'Подтвердите публикацию показанного текста.\n')
                shared_memory_request('approve_proposal', socket_path=self.shared_memory_socket,
                                      uuid=form['uuid'], revision=form['revision'])
                content = '<p>Запись опубликована. Теперь она доступна Codex и Qwen во всех проектах.</p>' + back
                title = 'Запись опубликована'
            else:
                query = environ.get('QUERY_STRING', '')
                if len(query) > 100:
                    return respond(start_response, HTTPStatus.BAD_REQUEST)
                try:
                    values = parse_qs(query, max_num_fields=1, strict_parsing=True)
                    after = int(values.get('after', ['0'])[0])
                    if set(values) - {'after'} or not 0 <= after <= 9223372036854775807:
                        raise ValueError
                except ValueError:
                    return respond(start_response, HTTPStatus.BAD_REQUEST)
                result = shared_memory_request('list_proposals', socket_path=self.shared_memory_socket, after=after)
                content = '<p><a href="/">Главная</a></p><p>Проверьте точный текст. После публикации его увидят оба агента во всех проектах.</p>'
                for item in result['proposals']:
                    content += f'''<article aria-labelledby="proposal-{item['uuid']}">
<h2 id="proposal-{item['uuid']}"><span>{escape(item['name'])}</span></h2>
<p>Предложение из проекта <strong>{escape(item['project'])}</strong></p>
<pre><code>{escape(item['content'])}</code></pre>
<form method="post" action="/memory/approve">
<input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="uuid" value="{item['uuid']}">
<input type="hidden" name="revision" value="{item['revision']}">
<button name="confirm" value="yes">Опубликовать этот текст</button></form></article>'''
                if not result['proposals']:
                    content += '<p>Предложений для публикации нет. Агент может предложить запись во время работы над проектом.</p>'
                if result['next_after'] is not None:
                    content += f'<p><a href="/memory?after={result["next_after"]}">Следующие предложения</a></p>'
                if after:
                    content += '<p><a href="/memory">К первым предложениям</a></p>'
                title = 'Общая память'
        except (OSError, ValueError):
            return respond(start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                html_page('Общая память', '<p>Не удалось выполнить действие. Проверьте доступность памяти и повторите попытку.</p>' + back),
                'text/html; charset=utf-8')
        page = html_page(title, f'<style>{MEMORY_STYLE}</style><main class="memory">{content}</main>')
        return respond(start_response, HTTPStatus.OK, page, 'text/html; charset=utf-8')

    def codex(self, environ, start_response, session, path, method):
        action = {("GET", "/codex"): "status", ("POST", "/codex/login"): "login",
                  ("POST", "/codex/profile"): "profile"}.get((method, path))
        if action is None:
            return respond(start_response, HTTPStatus.NOT_FOUND)
        csrf = csrf_token(session)
        fields = {}
        if method == "POST":
            form = submitted_form(environ, max_fields=3, max_length=1024)
            if not hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
                return respond(start_response, HTTPStatus.FORBIDDEN, "Недействительная форма.\n")
            if action == "profile":
                if form.get("profile") not in ("chatgpt", "api") or form.get("confirm") != "yes":
                    return respond(start_response, HTTPStatus.BAD_REQUEST, "Подтвердите выбор способа оплаты.\n")
                fields["profile"] = form["profile"]
        try:
            result = codex_request(action, socket_path=self.codex_socket, **fields)
        except (OSError, ValueError):
            result = {"error": "Служба Codex недоступна. Повторите попытку позже."}
        hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
        content = '<p><a href="/">Главная</a> · <a href="/projects">Проекты</a></p>'
        if "error" in result:
            content += f'<p role="alert">{escape(result["error"])}</p>'
            return respond(start_response, HTTPStatus.SERVICE_UNAVAILABLE, html_page("Codex", content), "text/html; charset=utf-8")
        labels = {"login_required": "Требуется вход в ChatGPT.", "limit_reached": "Лимит подписки исчерпан. Дождитесь обновления лимита или явно выберите платный API.",
                  "ready": "Подписка ChatGPT подключена.", "unavailable": "Не удалось проверить подписку. Повторите попытку позже."}
        content += f'<p>{labels.get(result.get("status"), labels["unavailable"])}</p>'
        profile = "Подписка ChatGPT" if result.get("profile", "chatgpt") == "chatgpt" else "Платный API"
        content += f'<p>Для новых разговоров: <strong>{profile}</strong>.</p>'
        login = result.get("login")
        if login:
            content += ('<p>Откройте <a href="https://auth.openai.com/codex/device" target="_blank" rel="noopener noreferrer">вход в ChatGPT</a> '
                        f'и введите код <strong>{escape(login["userCode"])}</strong>. Затем обновите эту страницу.</p>')
        content += f'<form method="post" action="/codex/login">{hidden}<button>Войти в ChatGPT</button></form>'
        content += '<p>Переключение применяется к новым разговорам. Автоматического перехода на платный API нет.</p>'
        options = [('chatgpt', 'Использовать подписку ChatGPT')]
        if result.get("api_configured"):
            options.append(('api', 'Использовать платный API'))
        for value, label in options:
            content += (f'<form method="post" action="/codex/profile">{hidden}'
                        f'<input type="hidden" name="profile" value="{value}">'
                        '<label><input type="checkbox" name="confirm" value="yes" required> Подтверждаю смену способа оплаты</label> '
                        f'<button>{label}</button></form>')
        return respond(start_response, HTTPStatus.OK, html_page("Codex", content), "text/html; charset=utf-8")

    def projects(self, environ, start_response, session, path, method):
        actions = {
            ("GET", "/projects"): "projects",
            ("GET", "/github/connect"): "app",
            ("POST", "/github/connect"): "connect",
            ("GET", "/projects/add"): "repositories",
            ("POST", "/projects/add"): "add",
            ("POST", "/projects/delete"): "delete",
            ("POST", "/projects/canvas"): "canvas",
        }
        action = actions.get((method, path))
        if action is None:
            return respond(start_response, HTTPStatus.NOT_FOUND)
        csrf = csrf_token(session)
        hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
        fields = {}
        if method == "POST":
            form = submitted_form(environ, max_fields=3, max_length=1024)
            if not hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
                return respond(start_response, HTTPStatus.FORBIDDEN, "Недействительная форма.\n")
            if action in ("add", "delete", "canvas"):
                repository = form.get("repository", "")
                if not re.fullmatch(r"[1-9][0-9]{0,18}", repository):
                    return respond(start_response, HTTPStatus.BAD_REQUEST, "Выберите репозиторий.\n")
                if action == "delete" and form.get("confirm") != "yes":
                    return respond(start_response, HTTPStatus.BAD_REQUEST, "Подтвердите удаление.\n")
                fields["repository"] = int(repository)
        back = '<p><a href="/projects">К проектам</a></p>'
        try:
            result = github_request(action, **fields)
        except (OSError, ValueError):
            result = {"error": "Служба проектов недоступна. Проверьте настройку платформы."}
        if "error" in result:
            return respond(start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                           html_page("Проекты", f'<p>{escape(result["error"])}</p>{back}'),
                           "text/html; charset=utf-8")
        if method == "POST":
            return respond(start_response, HTTPStatus.SEE_OTHER, headers=(("Location", "/projects"),))
        if action == "app":
            url = escape(result["install_url"])
            account = escape(result["account"])
            content = f"""<p>Аккаунт GitHub <strong>{account}</strong></p>
<p><a href="{url}" target="_blank" rel="noopener noreferrer">
Установить GitHub App</a></p>
<p>Разрешите приложению доступ ко всем или выбранным репозиториям, затем подтвердите подключение здесь.</p>
<form method="post">{hidden}<button>Подтвердить подключение</button></form>{back}"""
            title = "Подключить GitHub"
        elif action == "repositories":
            options = "".join(f'<option value="{repo["id"]}">{escape(repo["name"])}</option>'
                              for repo in result["repositories"])
            content = f"""<form method="post">{hidden}
<label>Приватный репозиторий <select name="repository" required>{options}</select></label>
<button>Добавить проект</button></form>{back}""" if options else f"<p>Нет доступных приватных репозиториев.</p>{back}"
            title = "Добавить проект"
        else:
            title = "Проекты"
            link = ('<a href="/projects/add">Добавить проект</a> · <a href="/github/connect">Подключение GitHub</a>'
                    if result["connected"] else '<a href="/github/connect">Подключить GitHub</a>')
            rows = "".join(self.project_row(project, hidden) for project in result["projects"])
            content = f'<p><a href="/">Главная</a></p><p>{link}</p><ul>{rows}</ul>'
        return respond(start_response, HTTPStatus.OK, html_page(title, content), "text/html; charset=utf-8")

    @staticmethod
    def project_row(project, hidden):
        states = {
            "creating": "Подготовка песочницы",
            "ready": "Готов",
            "failed": "Подготовка не завершена",
            "deleting": "Удаление",
            "delete_failed": "Удаление не завершено",
        }
        status = states[project["state"]]
        inputs = hidden + f'<input type="hidden" name="repository" value="{project["id"]}">'
        canvas = ""
        if project["state"] == "ready":
            connection = project.get("canvas", "pending")
            if connection == "ready":
                canvas = (f'<p><a href="/canvas/open/{escape(project["sandbox"])}">Открыть</a> '
                          f'· <a href="/canvas/open/{escape(project["sandbox"])}?agent=qwen">Qwen Code</a></p>')
            elif connection == "connecting":
                canvas = "<p>Подключение Canvas… Обновите страницу через минуту.</p>"
            else:
                label = "Подключение Canvas не завершено" if connection == "failed" else "Canvas ещё не подключён"
                canvas = (f'<p>{label}</p><form method="post" action="/projects/canvas">{inputs}'
                          '<button>Подключить Canvas</button></form>')
        retry = (f'<form method="post" action="/projects/add">{inputs}<button>Повторить добавление</button></form>'
                 if project["state"] == "failed" else "")
        delete = (f"""<form method="post" action="/projects/delete">{inputs}
<label><input type="checkbox" name="confirm" value="yes" required> Удалить рабочую копию и песочницу</label>
<button>Удалить проект</button></form>""" if project["state"] not in ("creating", "deleting") else "")
        return (f'<li><strong>{escape(project["name"])}</strong> · {status} '
                f'<code>{escape(project["sandbox"])}</code>{canvas}{retry}{delete}</li>')

    def change_browser(self, environ, start_response, session, browser_id, action):
        if not self.owner.has_session(session):
            return respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")
        form = submitted_form(environ, max_fields=2, max_length=2048)
        if not hmac.compare_digest(form.get("csrf", "").encode(), csrf_token(session).encode()):
            return respond(start_response, HTTPStatus.FORBIDDEN, "Недействительная форма.\n")

        name = ""
        if action == "rename":
            name = form.get("name", "").strip()
            if not 0 < len(name) <= BROWSER_NAME_LIMIT or not name.isprintable():
                return respond(start_response, HTTPStatus.BAD_REQUEST, "Укажите имя браузера.\n")
        browser_hash = bytes.fromhex(browser_id)
        current_hash = digest(session)
        changed, renewed = self.owner.change_browser(current_hash, browser_hash, action, name)
        if not changed:
            return respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")
        cookie = self.cookie(session) if renewed else self.cookie("", 0)
        return respond(start_response, HTTPStatus.SEE_OTHER, headers=(("Location", "/"), cookie))

    def registration(self, environ, start_response, token):
        method = environ["REQUEST_METHOD"]
        if method == "POST":
            code = submitted_form(environ, max_fields=1, max_length=256).get("code", "")
            status, session = self.owner.register(
                token, code if CODE.fullmatch(code) else "", environ["HTTP_X_PORTAL_CLIENT_IP"]
            )
            if session:
                return respond(start_response, status, headers=(("Location", "/"), self.cookie(session)))
            messages = {
                HTTPStatus.NOT_FOUND: "Страница недоступна.\n",
                HTTPStatus.BAD_REQUEST: "Код не принят. Вернитесь к форме и введите новый код.\n",
                HTTPStatus.TOO_MANY_REQUESTS: "Лимит попыток исчерпан. Повторите позже.\n",
            }
            return respond(start_response, status, messages[status])

        if method == "GET":
            kind, secret = self.owner.registration_page(token)
            if kind:
                title = "Активировать владельца" if secret else "Зарегистрировать браузер"
                intro = enrollment_qr(secret) if secret else ""
                form = f"""<form method="post">
<label>Шестизначный код <input name="code" type="text" inputmode="numeric"
autocomplete="one-time-code" pattern="{CODE.pattern}" maxlength="{TOTP_DIGITS}" required></label>
<button>Подтвердить</button></form>"""
                return respond(start_response, HTTPStatus.OK, html_page(title, intro + form),
                               "text/html; charset=utf-8")

        return respond(start_response, HTTPStatus.NOT_FOUND, "Not found\n")


def print_activation_url(owner, base_url):
    owner.initialize()
    path = owner.provision()
    if path:
        print(base_url + path)


def application(environ, start_response):
    # Only the Unix socket can reach this WSGI service. Nginx supplies the
    # scheme from its listener, never from a client-supplied header.
    portal = Portal(STATE, KEY.read_bytes(), secure=environ.get("HTTP_X_PORTAL_SCHEME") == "https")
    return portal(environ, start_response)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("activation").add_argument("base_url")
    commands.add_parser("recover").add_argument("base_url")
    commands.add_parser("rotate-registration").add_argument("base_url")
    args = parser.parse_args(argv)
    owner = OwnerAccess(STATE, KEY.read_bytes(), time.time)
    if args.command == "init":
        owner.initialize()
    elif args.command == "activation":
        print_activation_url(owner, args.base_url)
    elif args.command == "recover":
        owner.initialize()
        print(args.base_url + owner.recover())
    else:
        owner.initialize()
        path = owner.rotate_registration()
        if path is None:
            parser.exit(1, "Сначала активируйте владельца.\n")
        print(args.base_url + path)


if __name__ == "__main__":
    main()
