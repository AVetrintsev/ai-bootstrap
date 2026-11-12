"""Run on the target VM: both terminal agents use the installed project tools."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
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


try:
    run('python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
        fixture, f'export function {symbol}() {{ return 42; }}\n')
    time.sleep(5)
    prompt = (f'Acceptance test. Call Serena initial_instructions, then get_symbols_overview on {symbol}.ts. '
              f'Call CodeGraphContext find_code with query {symbol}; both tools must find the function. '
              f'Then use shell: cd /workspace/repo && rg {symbol} {symbol}.ts '
              f"&& ast-grep run --lang typescript --pattern 'function $NAME() {{ $$$BODY }}' {symbol}.ts "
              f"&& printf '%s' '{symbol}' > '{marker}'. Do not change other files. Reply DONE.")
    command = (['codex', 'exec', prompt] if agent == 'codex' else
               ['qwen', '-p', prompt, '--approval-mode', 'yolo', '--output-format', 'stream-json'])
    result = run(*command, timeout=300)
    output = result.stdout + result.stderr
    log = Path('/tmp') / (symbol + '-' + agent + '.log')
    log.write_text(output)
    assert run('cat', marker).stdout == symbol, f'Search commands did not complete; see {log}'
    if agent == 'codex':
        for tool in ('get_symbols_overview', 'find_code'):
            assert '[Codex] ' + tool in result.stderr, f'No {tool} call; see {log}'
    else:
        used = set()
        def inspect(value):
            if isinstance(value, dict):
                if value.get('type') == 'tool_use':
                    used.add(value.get('name', value.get('tool_name', '')))
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        for line in result.stdout.splitlines():
            try:
                inspect(json.loads(line))
            except ValueError:
                continue
        for tool in ('get_symbols_overview', 'find_code', 'run_shell_command'):
            assert any(name.endswith(tool) for name in used), f'No {tool} call; see {log}'
    print(f'OK {agent} terminal called Serena, CodeGraphContext, ripgrep and ast-grep; {log}')
finally:
    run('rm', '-f', '--', fixture, marker)
