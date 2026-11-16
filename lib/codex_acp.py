"""Project policy in front of the upstream codex-acp process.

The adapter owns ACP and all app-server translation. This module only constrains
inputs at the host boundary and selects its native legacy model/effort catalog.
"""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import tomllib

from codex_service import API_KEY, BINARY, STATE, CodexError, selected_profile

ADAPTER = '/opt/ai-codex-acp/bin/codex-acp'
EXECUTORS = Path('/run/ai-codex-exec')
MAX_LINE = 16 * 1024 * 1024
SESSION = re.compile(r'[A-Za-z0-9-]{1,100}')
SETUP = {'session/new', 'session/load', 'session/resume', 'session/fork'}
METHODS = SETUP | {'initialize', 'session/prompt', 'session/cancel', 'session/list',
                   'session/set_model', 'session/set_mode', 'session/set_config_option'}


def workspace_path(value):
    if not isinstance(value, str) or len(value) > 4096 or '\\' in value or '\x00' in value or '\n' in value:
        raise ValueError('Недействительная рабочая папка.')
    path = PurePosixPath(value)
    if '..' in path.parts or str(path) != value:
        raise ValueError('Недействительная рабочая папка.')
    if value == '/workspace/repo' or value.startswith('/workspace/repo/'):
        return value
    if re.fullmatch(r'/workspace/worktrees/[a-fA-F0-9-]{36}/repo(?:/[^\n]+)?', value):
        return value
    raise ValueError('Выберите репозиторий или worktree этого проекта.')


def legacy_models(value):
    """Let SDK 1.62 select the adapter's own model[effort] legacy IDs."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key == 'configOptions' and isinstance(item, list):
                value[key] = [option for option in item if option.get('id') not in ('model', 'reasoning_effort')
                              and option.get('category') not in ('model', 'thought_level')]
            elif key == 'availableCommands' and isinstance(item, list):
                value[key] = [command for command in item if command.get('name', '').lower() not in ('logout', 'mcp')]
            else:
                legacy_models(item)
    elif isinstance(value, list):
        for item in value:
            legacy_models(item)
    return value


class ProjectAdapter:
    def __init__(self, project, writer, *, state=STATE, adapter=ADAPTER, binary=BINARY,
                 api_key=API_KEY, executors=EXECUTORS):
        self.project, self.writer = project, writer
        self.state, self.adapter, self.binary = Path(state), adapter, binary
        self.api_key, self.executors = Path(api_key), Path(executors)
        self.profile = selected_profile(self.state)
        self.home = self.state / 'projects' / project / self.profile
        self.allowed = self.home / 'sessions-allowed'
        self.process = None
        self.reader = None
        self.pending = {}
        self.callbacks = set()
        self.workspaces = {}
        self.write_lock = asyncio.Lock()

    async def send(self, message):
        async with self.write_lock:
            self.writer.write(json.dumps(message, ensure_ascii=False).encode() + b'\n')
            await self.writer.drain()

    async def start(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allowed.mkdir(exist_ok=True, mode=0o700)
        config = {'developer_instructions': '', 'forced_login_method': self.profile,
                  'approval_policy': 'never', 'sandbox_mode': 'danger-full-access',
                  'features': {'hooks': False}}
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'CODEX_HOME': str(self.home),
                       'CODEX_PATH': self.binary, 'CODEX_CONFIG': json.dumps(config),
                       'DISABLE_MCP_CONFIG_FILTERING': 'true',
                       'TOKIO_WORKER_THREADS': '2', 'RAYON_NUM_THREADS': '2'}
        if self.profile == 'api':
            environment['OPENAI_API_KEY'] = self.api_key.read_text().strip()
        self.process = await asyncio.create_subprocess_exec(self.adapter,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=self.home, env=environment, limit=MAX_LINE, start_new_session=True)
        self.reader = asyncio.create_task(self.read_backend())

    def session(self, value):
        if not isinstance(value, str) or not SESSION.fullmatch(value) or not (self.allowed / value).is_file():
            raise ValueError('Разговор не принадлежит этому проекту и способу оплаты.')
        return value

    def mcp_servers(self, cwd):
        """Native ACP overrides commands; trusted TOML keeps remote environment/cwd.

        Codex requires an explicit launch cwd for remote stdio. env --chdir then
        selects this conversation's workspace without modifying shared config.
        """
        path = self.home / 'config.toml'
        config = tomllib.loads(path.read_text()) if path.exists() else {}
        servers = []
        for name, settings in config.get('mcp_servers', {}).items():
            if settings.get('environment_id') != 'project' or settings.get('cwd') != '/workspace/repo':
                raise CodexError()
            servers.append({'name': name, 'command': '/usr/bin/env', 'args': [
                '--chdir', cwd, settings['command'], *settings.get('args', [])], 'env': []})
        return servers

    async def workspace(self, value):
        cwd = workspace_path(value)
        try:
            async with asyncio.timeout(15):
                reader, writer = await asyncio.open_unix_connection(str(self.executors / f'{self.project}.sock'))
                try:
                    writer.write(b'AI-WORKSPACE ' + json.dumps(cwd).encode() + b'\n')
                    await writer.drain()
                    result = json.loads(await reader.readline())
                    if result.get('cwd') != cwd:
                        raise ValueError('Рабочая папка не является репозиторием или worktree проекта.')
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
            raise CodexError('executor_unavailable') from None
        return cwd

    async def request(self, message):
        if not isinstance(message, dict) or type(message.get('id', 0)) not in (int, str):
            raise ValueError('Недействительный запрос.')
        if 'method' not in message:
            identity = message.get('id')
            if identity not in self.callbacks:
                raise ValueError('Неизвестный ответ клиентского инструмента.')
            self.callbacks.remove(identity)
            return message
        method, params = message['method'], message.get('params', {})
        if not isinstance(method, str) or method not in METHODS or not isinstance(params, dict):
            raise ValueError('Этот метод недоступен из песочницы.')
        if selected_profile(self.state) != self.profile:
            raise CodexError('profile_changed')
        if method == 'initialize':
            if self.process:
                raise ValueError('Соединение уже инициализировано.')
            capabilities = params.get('clientCapabilities') or {}
            if not isinstance(capabilities, dict):
                raise ValueError('Недействительные возможности клиента.')
            params = {'protocolVersion': params.get('protocolVersion', 1),
                      'clientInfo': {'name': 'ai-bootstrap', 'version': '2'},
                      'clientCapabilities': {key: capabilities[key] for key in ('fs', 'terminal') if key in capabilities}}
            await self.start()
        elif not self.process:
            raise ValueError('Сначала инициализируйте соединение.')
        elif method in SETUP:
            if params.get('mcpServers') or params.get('additionalDirectories'):
                raise ValueError('MCP запускаются в среде проекта через управляемую конфигурацию.')
            cwd = await self.workspace(params.get('cwd'))
            clean = {'cwd': cwd, 'mcpServers': self.mcp_servers(cwd),
                     'systemPrompt': {'mode': 'append', 'content': [{'type': 'text', 'text': ''}]}}
            if method != 'session/new':
                clean['sessionId'] = self.session(params.get('sessionId'))
            params = clean
        elif method == 'session/list':
            params = {key: params[key] for key in ('cursor',) if key in params}
        else:
            identity = self.session(params.get('sessionId'))
            if method == 'session/prompt':
                parts = params.get('prompt')
                if not isinstance(parts, list) or not parts or any(not isinstance(part, dict) for part in parts):
                    raise ValueError('Ожидается сообщение ACP.')
                for part in parts:
                    if part.get('type') == 'image' and part.get('uri') and (
                            not isinstance(part['uri'], str) or not part['uri'].startswith('data:')):
                        raise ValueError('Передайте изображение вложением, без внешнего URL.')
                first = parts[0].get('text', '') if parts[0].get('type') == 'text' else ''
                if not isinstance(first, str):
                    raise ValueError('Ожидается текст сообщения.')
                if re.match(r'^/\s*(logout|mcp)\b', first.strip(), re.IGNORECASE):
                    raise ValueError('Авторизация управляется через портал; MCP настроены в проекте.')
                cwd = self.workspaces.get(identity)
                if not cwd:
                    raise ValueError('Сначала откройте разговор.')
                await self.workspace(cwd)
                # AIR extensions and client configuration never reach the host.
                params = {'sessionId': identity, 'prompt': [
                    {key: value for key, value in part.items() if key != '_meta'} for part in parts]}
            else:
                fields = {'session/cancel': (), 'session/set_model': ('modelId',),
                          'session/set_mode': ('modeId',), 'session/set_config_option': ('configId', 'value')}[method]
                if method == 'session/set_config_option' and params.get('configId') not in ('model', 'reasoning_effort', 'mode'):
                    raise ValueError('Эта настройка управляется платформой.')
                params = {'sessionId': identity, **{key: params[key] for key in fields}}
        return {'jsonrpc': '2.0', **({'id': message['id']} if 'id' in message else {}),
                'method': method, 'params': params}

    async def read_backend(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                identity = message.get('id')
                if 'method' in message and identity is not None:
                    self.callbacks.add(identity)
                elif identity in self.pending:
                    method, params = self.pending.pop(identity)
                    result = message.get('result')
                    if isinstance(result, dict):
                        if method == 'initialize':
                            result['authMethods'] = []
                        if method in SETUP:
                            session = result.get('sessionId', params.get('sessionId'))
                            if isinstance(session, str) and SESSION.fullmatch(session):
                                self.workspaces[session] = params['cwd']
                                (self.allowed / session).touch(mode=0o600, exist_ok=True)
                    if 'error' in message:
                        # Never forward provider response details or host paths.
                        message['error'] = {'code': -32000, 'message': 'Codex отклонил запрос. Проверьте подключение и настройки.'}
                await self.send(legacy_models(message))
        except (ValueError, OSError, asyncio.LimitOverrunError):
            pass
        finally:
            self.writer.close()

    async def handle(self, message):
        try:
            forwarded = await self.request(message)
            identity = forwarded.get('id')
            if 'method' in forwarded and identity is not None:
                if identity in self.pending:
                    raise ValueError('Повторяющийся идентификатор запроса.')
                self.pending[identity] = (forwarded['method'], forwarded.get('params', {}))
            self.process.stdin.write(json.dumps(forwarded).encode() + b'\n')
            await self.process.stdin.drain()
        except (ValueError, KeyError, TypeError, OSError, CodexError) as error:
            identity = message.get('id') if isinstance(message, dict) else None
            text = str(error) if isinstance(error, (ValueError, CodexError)) else 'Адаптер Codex недоступен.'
            if identity is not None:
                if message.get('method') == 'session/prompt':
                    params = message.get('params')
                    session = params.get('sessionId') if isinstance(params, dict) else None
                    await self.send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                        'sessionId': session,
                        'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}})
                    await self.send({'jsonrpc': '2.0', 'id': identity, 'result': {'stopReason': 'refusal'}})
                else:
                    await self.send({'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32000, 'message': text}})

    async def close(self):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()


async def serve(listener, project, **options):
    slots = asyncio.Semaphore(2)
    async def client(reader, writer):
        adapter = ProjectAdapter(project, writer, **options)
        try:
            if slots.locked():
                message = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not isinstance(message, dict):
                    return
                await adapter.send({'jsonrpc': '2.0', 'id': message.get('id'), 'error': {
                    'code': -32000, 'message': 'Закройте один из открытых сеансов Codex.'}})
                return
            async with slots:
                while line := await asyncio.wait_for(reader.readline(), 1800):
                    await adapter.handle(json.loads(line))
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            await adapter.close()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    server = await asyncio.start_unix_server(client, sock=listener, limit=MAX_LINE)
    async with server:
        await server.serve_forever()
