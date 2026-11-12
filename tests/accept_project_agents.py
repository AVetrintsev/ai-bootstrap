"""Run on the target VM: both terminal agents use the installed project tools."""
import json
import os
from pathlib import Path
import re
import queue
import secrets
import subprocess
import sys
import threading
import time

if os.geteuid() != 0 or len(sys.argv) != 3 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
    sys.exit('Usage: sudo python3 tests/accept_project_agents.py PROJECT qwen|codex')
project, agent = sys.argv[1:]
if agent not in ('qwen', 'codex'):
    sys.exit('Expected qwen or codex')
symbol = 'terminal_tools_' + secrets.token_hex(8)
fixture = '/workspace/repo/' + symbol + '.ts'
marker = fixture + '.result'


def run(*command, **kwargs):
    kwargs.setdefault('stdin', subprocess.DEVNULL)
    return subprocess.run(['ai-sandbox', 'shell', project, *command],
                          check=True, text=True, capture_output=True, **kwargs)


def prompt_agent(prompt, log):
    # ACP is exposed by both terminal binaries and includes completed tool output.
    # Human-readable CLI banners only prove that a tool was started.
    process = subprocess.Popen(['ai-sandbox', 'shell', project, agent, '--acp'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
    messages = queue.Queue()
    calls = {}
    def read():
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put({'error': 'Agent exited'})
    threading.Thread(target=read, daemon=True).start()
    def send(message):
        process.stdin.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
        process.stdin.flush()
    def request(identity, method, params):
        send({'id': identity, 'method': method, 'params': params})
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            assert 'error' not in message, message
            if message.get('method') == 'session/request_permission':
                option = next(o for o in message['params']['options'] if o['kind'] == 'allow_once')
                send({'id': message['id'], 'result': {'outcome': {'outcome': 'selected', 'optionId': option['optionId']}}})
            update = message.get('params', {}).get('update', {})
            if update.get('sessionUpdate') in ('tool_call', 'tool_call_update'):
                calls.setdefault(update['toolCallId'], {}).update(update)
            if message.get('id') == identity and 'result' in message:
                return message['result']
        raise AssertionError('Agent timed out')
    try:
        request(1, 'initialize', {'protocolVersion': 1, 'clientCapabilities': {}})
        session = request(2, 'session/new', {'cwd': '/workspace/repo', 'mcpServers': []})['sessionId']
        request(3, 'session/prompt', {'sessionId': session, 'prompt': [{'type': 'text', 'text': prompt}]})
        return calls.values()
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=15)


def has_symbol(value):
    if isinstance(value, str):
        try:
            return has_symbol(json.loads(value))
        except ValueError:
            # Qwen can concatenate structuredContent and text as two JSON lines.
            return (any(has_symbol(line) for line in value.splitlines())
                    if len(value.splitlines()) > 1 else value == symbol)
    if isinstance(value, dict):
        return not value.get('isError') and any(has_symbol(v) for k, v in value.items() if k not in ('query', 'search_term'))
    return isinstance(value, list) and any(has_symbol(v) for v in value)


try:
    run('python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
        fixture, f'export function {symbol}() {{ return 42; }}\n')
    time.sleep(5)
    prompt = (f'Acceptance test. Call Serena initial_instructions, then get_symbols_overview on {symbol}.ts. '
              f'Call CodeGraphContext find_code with query {symbol}; both tools must find the function. '
              f'Then use shell: cd /workspace/repo && rg {symbol} {symbol}.ts '
              f"&& ast-grep run --lang typescript --pattern 'function $NAME() {{ $$$BODY }}' {symbol}.ts "
              f"&& printf '%s' '{symbol}' > '{marker}'. Do not change other files. Reply DONE.")
    log = Path('/tmp') / (symbol + '-' + agent + '.log')
    with log.open('w') as output:
        calls = prompt_agent(prompt, output)
    assert run('cat', marker).stdout == symbol, f'Search commands did not complete; see {log}'
    for tool in ('get_symbols_overview', 'find_code'):
        assert any(tool in call.get('title', '') and call.get('status') == 'completed'
                   and has_symbol(call.get('content')) for call in calls), f'No successful {tool} result; see {log}'
    print(f'OK {agent} terminal called Serena, CodeGraphContext, ripgrep and ast-grep; {log}')
finally:
    run('rm', '-f', '--', fixture, marker)
