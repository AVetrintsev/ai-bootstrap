"""Exercise installed project tools on the target VM through their public interfaces."""
import os
import re
import subprocess
import sys


def run(project, *command):
    return subprocess.run(['ai-sandbox', 'shell', project, *command],
                          check=True, capture_output=True, text=True, timeout=180).stdout


if __name__ == '__main__':
    if os.geteuid() != 0 or len(sys.argv) != 2 or not re.fullmatch(r'gh-[1-9][0-9]{0,18}', sys.argv[1]):
        sys.exit('Usage: sudo python3 tests/accept_project_tools.py PROJECT')
    project = sys.argv[1]
    assert 'ripgrep' in run(project, 'rg', '--version')
    assert 'ast-grep' in run(project, 'ast-grep', '--version')
    print('OK project search commands are installed')
