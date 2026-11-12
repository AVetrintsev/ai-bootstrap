"""Private Codex login control and project-scoped ACP bridge.

Only this trusted service sees ChatGPT credentials. The sandbox can send ACP
messages, never app-server RPC, configuration, credentials, or host commands.
"""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time

STATE = Path('/var/lib/ai-codex')
BINARY = '/opt/ai-codex/bin/codex'
API_KEY = Path('/etc/ai-bootstrap/codex-api.key')
PROJECT = re.compile(r'gh-[1-9][0-9]{0,18}')
MESSAGES = {
    'login_required': 'Требуется повторный вход в ChatGPT через портал /codex.',
    'limit_reached': 'Лимит подписки исчерпан. Дождитесь обновления лимита или явно выберите платный API в портале /codex.',
    'executor_unavailable': 'Исполнитель проекта недоступен. Выполнение остановлено.',
    'unavailable': 'Codex недоступен. Повторите попытку позже.',
}


class CodexError(Exception):
    def __init__(self, kind='unavailable'):
        self.kind = kind
        super().__init__(MESSAGES[kind])


def error_kind(error):
    """Use structured protocol codes; never expose arbitrary upstream errors."""
    info = error.get('codexErrorInfo') or error.get('data', {}).get('codexErrorInfo')
    if info == 'usageLimitExceeded':
        return 'limit_reached'
    if info == 'unauthorized':
        return 'login_required'
    if isinstance(info, dict):
        if any(isinstance(value, dict) and value.get('httpStatusCode') == 401 for value in info.values()):
            return 'login_required'
    return 'unavailable'


def selected_profile(state):
    path = state / 'profile'
    value = path.read_text().strip() if path.exists() else 'chatgpt'
    if value not in ('chatgpt', 'api'):
        raise CodexError()
    return value


def save(path, value):
    temporary = path.with_suffix('.new')
    temporary.write_text(value)
    temporary.chmod(0o600)
    temporary.replace(path)


class AppServer:
    def __init__(self, home, *, binary=BINARY, api_key=None):
        self.home, self.binary, self.api_key = home, binary, api_key
        self.pending = {}
        self.sequence = 0
        self.events = asyncio.Queue(maxsize=1024)
        self.process = None
        self.reader = None

    async def start(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'CODEX_HOME': str(self.home),
               'TOKIO_WORKER_THREADS': '2', 'RAYON_NUM_THREADS': '2'}
        if self.api_key is not None:
            env['OPENAI_API_KEY'] = self.api_key
        self.process = await asyncio.create_subprocess_exec(
            self.binary, 'app-server', '--listen', 'stdio://', '--strict-config',
            '-c', 'cli_auth_credentials_store="file"',
            '-c', 'forced_login_method="' + ('api' if self.api_key is not None else 'chatgpt') + '"',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=str(self.home), env=env,
            limit=4 * 1024 * 1024, start_new_session=True)
        self.reader = asyncio.create_task(self.read())
        await self.call('initialize', {'clientInfo': {'name': 'ai_bootstrap', 'version': '1'},
                                      'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized'})
        return self

    async def send(self, message):
        if self.process is None or self.process.returncode is not None:
            raise CodexError()
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        await self.process.stdin.drain()

    async def call(self, method, params=None, timeout=25):
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({'id': key, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        except (OSError, asyncio.TimeoutError):
            raise CodexError() from None
        finally:
            self.pending.pop(key, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if 'id' in message and 'method' not in message:
                    future = self.pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(CodexError(error_kind(message['error'])))
                        else:
                            future.set_result(message.get('result', {}))
                elif 'id' in message:
                    # There is no path for a client to approve a host action or supply auth.
                    await self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported server request'}})
                else:
                    self.events.put_nowait(message)
        except (ValueError, OSError, asyncio.QueueFull):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexError())
            with suppress(asyncio.QueueFull):
                self.events.put_nowait({'method': 'bridge/closed'})

    async def close(self):
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


class LoginControl:
    def __init__(self, state=STATE, *, binary=BINARY, api_key=API_KEY):
        self.state, self.binary, self.api_key = state, binary, api_key
        self.server = None
        self.login = None
        self.lock = asyncio.Lock()

    async def connect(self):
        if not self.server or self.server.process.returncode is not None:
            if self.server:
                await self.server.close()
            self.server = AppServer(self.state / 'auth', binary=self.binary)
            await self.server.start()
        while not self.server.events.empty():
            message = self.server.events.get_nowait()
            if message['method'] == 'account/login/completed':
                self.login = None
        if self.login and self.login['expires'] < time.time():
            self.login = None

    async def handle(self, request):
        async with self.lock:
            try:
                action = request.get('action')
                if action not in ('status', 'login', 'profile'):
                    return {'error': 'Неизвестное действие.'}
                if action == 'profile':
                    profile = request.get('profile')
                    if profile not in ('api', 'chatgpt') or (profile == 'api' and not self.api_key.is_file()):
                        return {'error': 'Платный API не настроен.'}
                    save(self.state / 'profile', profile + '\n')
                await self.connect()
                if action == 'login' and not self.login:
                    result = await self.server.call('account/login/start', {'type': 'chatgptDeviceCode'})
                    self.login = {'userCode': result['userCode'], 'expires': time.time() + 900}
                account = (await self.server.call('account/read', {'refreshToken': True})).get('account')
                status = 'login_required'
                if account and account.get('type') == 'chatgpt':
                    limits = await self.server.call('account/rateLimits/read')
                    status = 'limit_reached' if limits.get('ordinaryUsageAllowed') is False else 'ready'
                    if (limits.get('rateLimits') or {}).get('rateLimitReachedType'):
                        status = 'limit_reached'
                return {'status': status, 'profile': selected_profile(self.state),
                        'api_configured': self.api_key.is_file(), 'login': self.login}
            except CodexError as error:
                return {'status': error.kind, 'profile': selected_profile(self.state),
                        'api_configured': self.api_key.is_file(), 'login': self.login}
            except (OSError, ValueError, KeyError):
                return {'error': MESSAGES['unavailable']}

    async def close(self):
        if self.server:
            await self.server.close()


class ProjectSession:
    def __init__(self, project, writer, *, state=STATE, binary=BINARY, api_key=API_KEY):
        self.project, self.writer, self.state, self.binary, self.api_key = project, writer, state, binary, api_key
        self.server = None
        self.session = None
        self.turn = None
        self.profile = None
        self.model = None

    async def send(self, message):
        self.writer.write(json.dumps({'jsonrpc': '2.0', **message}).encode() + b'\n')
        await self.writer.drain()

    async def update(self, update):
        await self.send({'method': 'session/update', 'params': {'sessionId': self.session, 'update': update}})

    async def executor(self):
        try:
            await self.server.call('environment/info', {'environmentId': 'project'}, timeout=10)
        except CodexError:
            raise CodexError('executor_unavailable') from None

    async def connect(self, params, resume=False):
        if self.server or params.get('cwd') != '/workspace/repo' or params.get('mcpServers'):
            raise ValueError('Требуется /workspace/repo без дополнительных MCP-серверов.')
        self.profile = selected_profile(self.state)
        home = self.state / 'projects' / self.project / self.profile
        key = self.api_key.read_text().strip() if self.profile == 'api' else None
        self.server = AppServer(home, binary=self.binary, api_key=key)
        await self.server.start()
        await self.executor()
        account = (await self.server.call('account/read')).get('account')
        if self.profile == 'chatgpt' and (not account or account.get('type') != 'chatgpt'):
            raise CodexError('login_required')
        args = {'cwd': '/workspace/repo', 'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}
        if resume:
            identifier = params.get('sessionId', '')
            if not re.fullmatch(r'[a-zA-Z0-9-]{1,100}', identifier) or not (home / 'sessions-allowed' / identifier).is_file():
                raise ValueError('Разговор не принадлежит этому проекту и способу оплаты.')
            args['threadId'] = identifier
        result = await self.server.call('thread/resume' if resume else 'thread/start', args)
        self.session = result['thread']['id']
        self.model = result['model']
        allowed = home / 'sessions-allowed'
        allowed.mkdir(exist_ok=True, mode=0o700)
        (allowed / self.session).touch(mode=0o600)
        return {'sessionId': self.session, 'modes': {'currentModeId': 'project', 'availableModes': [
            {'id': 'project', 'name': 'Песочница проекта', 'description': 'Команды и файлы только в контейнере проекта.'}]}}

    async def prompt(self, params):
        if not self.server or params.get('sessionId') != self.session:
            raise ValueError('Неизвестный разговор.')
        if selected_profile(self.state) != self.profile:
            raise ValueError('Способ оплаты изменён. Создайте новый разговор.')
        parts = params.get('prompt', [])
        if not parts or any(p.get('type') != 'text' or not isinstance(p.get('text'), str) for p in parts):
            raise ValueError('Поддерживаются текстовые сообщения.')
        await self.executor()
        result = await self.server.call('turn/start', {'threadId': self.session,
            'input': [{'type': 'text', 'text': p['text'], 'text_elements': []} for p in parts]})
        self.turn = result['turn']['id']
        try:
            async with asyncio.timeout(600):
                while True:
                    event = await self.server.events.get()
                    method, values = event['method'], event.get('params', {})
                    if method == 'bridge/closed':
                        raise CodexError()
                    if values.get('threadId') != self.session:
                        continue
                    if method == 'item/agentMessage/delta':
                        await self.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': values['delta']}})
                    elif method in ('item/started', 'item/completed'):
                        item = values['item']
                        if item['type'] in ('commandExecution', 'fileChange', 'mcpToolCall'):
                            content = item.get('aggregatedOutput') or json.dumps(item.get('changes') or item.get('result') or {}, ensure_ascii=False)
                            update = {'sessionUpdate': 'tool_call' if method == 'item/started' else 'tool_call_update',
                                      'toolCallId': item['id'], 'status': 'in_progress' if method == 'item/started' else ('failed' if item.get('status') == 'failed' else 'completed'),
                                      'title': item.get('command') or item.get('tool') or 'Изменение файлов',
                                      'kind': {'commandExecution': 'execute', 'fileChange': 'edit', 'mcpToolCall': 'other'}[item['type']],
                                      'content': [{'type': 'content', 'content': {'type': 'text', 'text': content}}]}
                            await self.update(update)
                    elif method == 'turn/completed' and values['turn']['id'] == self.turn:
                        turn = values['turn']
                        if turn['status'] == 'failed':
                            raise CodexError(error_kind(turn.get('error') or {}))
                        await self.executor()
                        return {'stopReason': 'cancelled' if turn['status'] == 'interrupted' else 'end_turn'}
        finally:
            self.turn = None

    async def handle(self, message):
        key, method, params = message.get('id'), message.get('method'), message.get('params') or {}
        try:
            if method == 'initialize':
                result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True},
                          'agentInfo': {'name': 'ai-bootstrap-codex', 'version': '1'}, 'authMethods': []}
            elif method in ('session/new', 'session/load'):
                result = await self.connect(params, method == 'session/load')
            elif method == 'session/prompt':
                result = await self.prompt(params)
            elif method == 'session/cancel':
                if self.turn and params.get('sessionId') == self.session:
                    await self.server.call('turn/interrupt', {'threadId': self.session, 'turnId': self.turn})
                return
            elif method == 'session/set_mode' and params.get('modeId') == 'project' and params.get('sessionId') == self.session:
                result = {}
            else:
                raise ValueError('Этот метод недоступен из песочницы.')
            if key is not None:
                await self.send({'id': key, 'result': result})
        except (ValueError, CodexError, OSError, KeyError, asyncio.TimeoutError) as error:
            kind = error.kind if isinstance(error, CodexError) else 'unavailable'
            if key is not None:
                await self.send({'id': key, 'error': {'code': -32000, 'message': str(error) if isinstance(error, (CodexError, ValueError)) else MESSAGES[kind], 'data': {'kind': kind}}})

    async def close(self):
        if self.server:
            await self.server.close()


async def serve(listener, project=None, *, state=STATE, binary=BINARY, api_key=API_KEY):
    control = LoginControl(state, binary=binary, api_key=api_key) if project is None else None
    slots = asyncio.Semaphore(2)

    async def client(reader, writer):
        session = ProjectSession(project, writer, state=state, binary=binary, api_key=api_key) if project else None
        active = None
        try:
            async with slots:
                while line := await asyncio.wait_for(reader.readline(), 1800):
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        break
                    if control:
                        result = await control.handle(message)
                        writer.write(json.dumps(result).encode() + b'\n')
                        await writer.drain()
                        break
                    if active and not active.done():
                        if message.get('method') == 'session/cancel':
                            await session.handle(message)
                        elif message.get('id') is not None:
                            await session.send({'id': message['id'], 'error': {'code': -32000, 'message': 'Дождитесь завершения текущего действия.'}})
                    else:
                        active = asyncio.create_task(session.handle(message))
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            if active:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            if session:
                await session.close()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(client, sock=listener, limit=1024 * 1024)
    try:
        async with server:
            await server.serve_forever()
    finally:
        if control:
            await control.close()


if __name__ == '__main__':
    project = sys.argv[1] if len(sys.argv) > 1 else None
    if project and not PROJECT.fullmatch(project):
        sys.exit('Invalid project')
    os.umask(0o077)
    asyncio.run(serve(socket.socket(fileno=3), project))
