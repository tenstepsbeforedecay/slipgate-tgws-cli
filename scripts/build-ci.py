"""Run only on an isolated Windows GitHub runner; never on the user's PC."""
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from packaging.requirements import Requirement


def run(args, cwd=None, timeout=600):
    result = subprocess.run(args, cwd=cwd, timeout=timeout, text=True, capture_output=True)
    if result.returncode:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        result.check_returncode()
    return result


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform != 'win32':
        raise RuntimeError('Сборка/исполнение upstream разрешены только на Windows CI')
    root = Path('upstream').resolve()
    commit = run(['git', '-C', str(root), 'rev-parse', 'HEAD']).stdout.strip()
    if commit != os.environ['SOURCE_COMMIT']:
        raise ValueError('Checkout не совпадает с exact commit')
    dependencies = []
    if (root / 'requirements.txt').is_file():
        dependencies.extend(x.strip() for x in (root / 'requirements.txt').read_text().splitlines() if x.strip() and not x.strip().startswith('#'))
    if (root / 'pyproject.toml').is_file():
        dependencies.extend(tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['dependencies'])
    filtered = []
    for text in dependencies:
        dep = Requirement(text)
        if dep.url:
            raise ValueError('URL dependency требует проверки: ' + dep.name)
        if re.match(r'^(pystray|pillow|customtkinter|pyperclip|pyobjc)(-|$)', dep.name, re.I):
            continue
        filtered.append(text)
    if not filtered:
        raise ValueError('Не определены зависимости upstream')
    filtered.extend(['certifi==2026.2.25', 'packaging==25.0', 'pyinstaller==6.16.0'])
    # A constraints conflict stops the release instead of silently replacing
    # upstream's dependency contract. Wheels only: no downloaded build scripts.
    req = root / 'requirements-cli.txt'
    req.write_text('\n'.join(sorted(set(filtered))), encoding='utf-8')
    print(run([sys.executable, '-m', 'pip', 'install', '--only-binary=:all:', '--retries', '3', '--timeout', '30',
               '--report', str(root / 'pip-report.json'), '-r', str(req)]).stdout)
    (root / '_cli.py').write_text("from proxy.tg_ws_proxy import main\nif __name__ == '__main__':\n    main()\n", encoding='utf-8')
    flags = ['--onefile', '--console', '--name', 'TgWsProxy_windows', '--collect-all', 'certifi']
    for name in ['proxy', 'cryptography', 'httpx', 'h2']:
        flags += ['--collect-submodules', name]
    for name in ['proxy', 'proxy.tg_ws_proxy', 'proxy.bridge', 'proxy.config', 'proxy.balancer', 'proxy.fake_tls', 'proxy.raw_websocket', 'proxy.stats', 'proxy.utils', 'proxy.cf_h2', 'proxy.h2_transport', 'utils.logging_setup']:
        flags += ['--hidden-import', name]
    for name in ['pystray', 'customtkinter', 'PIL', 'tkinter']:
        flags += ['--exclude-module', name]
    print(run([sys.executable, '-m', 'PyInstaller', *flags, '_cli.py'], root, 1200).stdout)
    exe = root / 'dist/TgWsProxy_windows.exe'
    result = run([str(exe), '--help'], root, 45)
    if not re.search(r'usage:\s*TgWsProxy', result.stdout + result.stderr):
        raise ValueError('Нет штатного --help')
    print('PASS: CLI --help')
    # Only our own process tree is stopped, never by image name.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    smoke_args = ['--host', '127.0.0.1', '--port', str(port), '--secret', '00112233445566778899aabbccddeeff',
                  '--dc-ip', '2:149.154.167.220', '--dc-ip', '4:149.154.167.220', '--buf-kb', '256', '--pool-size', '4',
                  '--cfproxy-domain', 'example.com', '--fake-tls-domain', 'example.com', '--no-cfproxy', '--log-file', 'smoke.log', '-v']
    with (root / 'smoke-output.txt').open('w') as log:
        process = subprocess.Popen([str(exe), *smoke_args], cwd=root, stdout=log, stderr=log)
        try:
            listening = False
            for _ in range(30):
                if process.poll() is not None:
                    break
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                        listening = True
                        break
                except OSError:
                    time.sleep(1)
            if not listening:
                raise ValueError('Smoke: CLI не открыл локальный порт')
            print('PASS: CLI local TCP bind')
        finally:
            subprocess.run(['taskkill.exe', '/F', '/T', '/PID', str(process.pid)], timeout=20, capture_output=True)
            process.wait(timeout=20)
    bundle = Path('bundle')
    bundle.mkdir(exist_ok=True)
    binary = exe.read_bytes()
    (bundle / exe.name).write_bytes(binary)
    gen = load('generate-capabilities')
    sources = {str(p.relative_to(root)).replace('\\', '/'): p.read_text(encoding='utf-8-sig')
               for base in ['proxy', 'ui'] for p in (root / base).rglob('*') if p.suffix in ['.py', '.json'] and p.stat().st_size < 1024*1024}
    source = sources['proxy/tg_ws_proxy.py']
    locales = gen.source_locales(sources)
    schema = gen.generate(source, json.loads(Path(__file__).with_name('capabilities-overlay.json').read_text(encoding='utf-8')), os.environ['TAG'], commit, binary, locales)
    gen.presentation(schema['fields'], sources, locales)
    review = [f['flag'] for f in schema['fields'] if f.get('unsupported') or f['type'] == 'unknown']
    if review:
        raise ValueError('Нужна семантическая проверка: ' + ', '.join(review))
    (bundle / 'capabilities.json').write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
    load('make-routing-contracts').validate(sources)
    routing = load('generate-routing').generate({'commit': commit, 'assetDigest': hashlib.sha256(binary).hexdigest()}, sources, schema)
    (bundle / 'routing.json').write_text(json.dumps(routing, ensure_ascii=False), encoding='utf-8')
    (bundle / 'build-info.json').write_text(json.dumps({'source': commit, 'python': sys.version, 'dependencies': run([sys.executable, '-m', 'pip', 'freeze']).stdout,
        'pipReport': json.loads((root / 'pip-report.json').read_text())}), encoding='utf-8')
    load('release').validate_bundle({name: (bundle / name).read_bytes() for name in load('release').NAMES}, {'tag': os.environ['TAG'], 'commit': commit})
    print('PASS: PE x64, schema identity/digest, reviewed routing, complete bundle')
    print(run(['node', str(Path(__file__).with_name('validate-manifest.cjs')), str(bundle / 'capabilities.json')]).stdout)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        Path('review-required.txt').write_text(str(error), encoding='utf-8')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
                summary.write('Нужна проверка: ' + str(error) + '\nПубликация заблокирована.\n')
        raise
