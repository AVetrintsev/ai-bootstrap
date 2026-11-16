"""Exercise installed owner access on a disposable VM (resets its owner).

Run as root: python3 tests/accept_portal_lifecycle.py BASE_URL STATE_DIR --reset-owner
BASE_URL can point through an SSH tunnel. STATE_DIR receives private credentials
for subsequent acceptance. Only expiry timestamps are advanced directly in SQLite;
all access decisions, registration, recovery and renewal use installed interfaces.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from http.cookiejar import LWPCookieJar
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Barrier
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request
import os


class Browser:
    def __init__(self, base):
        self.base = base
        self.cookies = LWPCookieJar()
        self.client = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path, form=None, foreground=False):
        headers = {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'} if foreground else {}
        request = Request(self.base + path, headers=headers,
                          data=None if form is None else urlencode(form).encode())
        try:
            with self.client.open(request, timeout=15) as response:
                return response.status, response.read().decode()
        except HTTPError as error:
            return error.code, error.read().decode()

    def token_hash(self):
        token = next(cookie.value for cookie in self.cookies if cookie.name == 'ai_session')
        assert token is not None
        return hashlib.sha256(token.encode()).digest()


def owner_command(command):
    output = subprocess.check_output(['runuser', '-u', 'ai-portal', '--', 'python3', '-I',
        '/usr/local/libexec/ai-bootstrap/portal.py', command, base], text=True).strip()
    return urlsplit(output).path


def code(secret):
    digest = hmac.new(base64.b32decode(secret), (int(time.time()) // 30).to_bytes(8, 'big'), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f'{(int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff) % 1000000:06d}'


def activate(path):
    browser = Browser(base)
    status, page = browser.request(path)
    assert status == 200
    match = re.search(r'<code>([A-Z2-7]+)</code>', page)
    assert match, 'Activation did not offer a TOTP secret'
    secret = match.group(1)
    assert browser.request(path, {'code': code(secret)})[0] == 200
    assert browser.request(path)[0] == 404, 'Activation link remained valid'
    return browser, secret


def registration(browser):
    status, page = browser.request('/')
    assert status == 200
    match = re.search(r'href="([^"]+)">Зарегистрировать браузер', page)
    assert match, 'Dashboard did not offer browser registration'
    return match.group(1)


def session_state(browser):
    with sqlite3.connect(database) as db:
        return db.execute('SELECT expires_at,last_active_at FROM sessions WHERE token_hash=?',
                          (browser.token_hash(),)).fetchone()


if __name__ == '__main__':
    if os.geteuid() != 0 or len(sys.argv) != 4 or sys.argv[3] != '--reset-owner':
        sys.exit('Use on a disposable VM: sudo python3 accept_portal_lifecycle.py BASE_URL STATE_DIR --reset-owner')
    base, state_dir = sys.argv[1:3]
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path:
        sys.exit('Expected a loopback HTTP base URL without a path')
    database = '/var/lib/ai-portal/portal.sqlite3'
    os.umask(0o077)
    state = Path(state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)

    path = owner_command('recover')
    assert owner_command('activation') == path, 'Retry replaced a pending activation'
    with sqlite3.connect(database) as db:
        db.execute('UPDATE owner SET activation_expires=? WHERE id=1', (int(time.time()) - 1,))
    assert Browser(base).request(path)[0] == 404, 'Expired activation was accepted'
    replacement = owner_command('activation')
    assert replacement != path
    actor, secret = activate(replacement)
    print('PASS activation expiry, retry stability, replacement and one-time use', flush=True)

    link = registration(actor)
    now = int(time.time())
    with sqlite3.connect(database) as db:
        db.execute('UPDATE sessions SET expires_at=?,last_active_at=? WHERE token_hash=?',
                   (now + 60, now - 60, actor.token_hash()))
    before = session_state(actor)
    assert actor.request('/api/status')[0] == 200
    assert actor.request('/')[0] == 200
    assert session_state(actor) == before, 'Background request renewed the session'
    assert actor.request('/', foreground=True)[0] == 200
    renewed = session_state(actor)
    assert renewed[0] >= now + 30 * 86400 and renewed[1] >= now
    print('PASS background requests do not renew; foreground navigation renews for 30 days', flush=True)

    outsider = Browser(base)
    # Invalid but well-formed codes reach the persistent registration limiter.
    wrong = f'{(int(code(secret)) + 12345) % 1000000:06d}'
    for _ in range(3):
        time.sleep(1.1)
        assert outsider.request(link, {'code': wrong})[0] == 400
    subprocess.run(['systemctl', 'restart', 'ai-bootstrap-portal.service'], check=True)
    assert outsider.request(link, {'code': code(secret)})[0] == 429
    assert actor.request('/projects')[0] == 200, 'Registration limit blocked an existing browser'
    print('PASS registration limit survives service restart; existing session remains usable', flush=True)
    # Advance only the failure window; do not change the VM clock.
    with sqlite3.connect(database) as db:
        db.execute('UPDATE failures SET occurred_at=?', (int(time.time()) - 3601,))
        last_counter = db.execute('SELECT last_counter FROM owner WHERE id=1').fetchone()[0]
    while int(time.time()) // 30 <= last_counter:
        time.sleep(0.5)
    contenders = [Browser(base), Browser(base)]
    current = code(secret)
    barrier = Barrier(2)

    def submit(browser):
        barrier.wait(timeout=10)
        return browser.request(link, {'code': current})[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, contenders))
    assert sorted(results) == [200, 400], 'One TOTP registered more than one browser'
    enrolled = contenders[results.index(200)]
    print('PASS parallel submission of one TOTP registers exactly one browser', flush=True)

    stream = enrolled.client.open(base + '/api/test-stream', timeout=10)
    assert stream.readline().strip() == b'data: ready'
    with sqlite3.connect(database) as db:
        db.execute('UPDATE sessions SET expires_at=? WHERE token_hash=?',
                   (int(time.time()) - 1, enrolled.token_hash()))
    assert enrolled.request('/projects')[0] == 404
    stream.read()
    stream.close()
    print('PASS inactivity expiry rejects requests and closes an already-open stream', flush=True)

    recovery = owner_command('recover')
    assert actor.request('/projects')[0] == 404
    assert actor.request(link)[0] == 404
    actor, recovered_secret = activate(recovery)
    assert recovered_secret != secret and registration(actor) != link
    subprocess.run(['systemctl', 'restart', 'ai-bootstrap-portal.service'], check=True)
    assert actor.request('/projects')[0] == 200
    actor.cookies.save(str(state / 'cookies'), ignore_discard=True)
    (state / 'totp-secret').write_text(recovered_secret)
    print('PASS recovery revokes old access, replaces TOTP and registration, and survives restart', flush=True)
