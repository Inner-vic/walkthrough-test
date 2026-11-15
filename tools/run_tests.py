"""Run offline suites in separate processes so similarly named Skill modules cannot collide."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 11):
        print(json.dumps({'status': 'unsupported_interpreter', 'required': 'CPython 3.11'}))
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('portable', 'full'), default='full')
    args = parser.parse_args()
    common = ROOT / 'workspace/.agents/skills/chassis-business-routing/scripts'
    skill = ROOT / 'workspace/runtime/wtr096-hardening/skill/scripts'
    suites = [('input', common, 'test_natural_input.py'), ('repository', ROOT / 'tests', 'test_*.py')]
    if args.suite == 'full':
        suites += [('routing', common, 'test_walkthrough_new_dispatch.py'), ('walkthrough', skill, 'test_*.py')]
    else:
        suites += [('analyzer-contract', skill, 'test_analyzer_contract.py')]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    for name, folder, pattern in suites:
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-m', 'unittest', 'discover',
                                 '-s', str(folder), '-p', pattern], cwd=ROOT, env=env)
        if result.returncode:
            print(json.dumps({'status': 'failed', 'suite': name}))
            return result.returncode
    print(json.dumps({'status': 'passed', 'suite': args.suite, 'suite_groups': len(suites)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
