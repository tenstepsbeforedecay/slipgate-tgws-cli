"""CI release state machine. No upstream code is imported or executed here."""
import argparse
import hashlib
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

REPO = 'tenstepsbeforedecay/slipgate-tgws-cli'
UPSTREAM = 'Flowseal/tg-ws-proxy'
NAMES = {'TgWsProxy_windows.exe': 128 * 1024 * 1024, 'capabilities.json': 128 * 1024,
         'routing.json': 128 * 1024, 'build-info.json': 1024 * 1024}
PREFIX = '<!-- slipgate-cli-staging-v1\n'


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl)
        if host.scheme != 'https' or not (host.hostname == 'api.github.com' or host.hostname == 'release-assets.githubusercontent.com'):
            raise ValueError('Недоверенный redirect загрузки')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if host.hostname != 'api.github.com':
            redirected.remove_header('Authorization')
        return redirected


def tag_value(tag):
    if not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', tag, flags=re.ASCII) or len(tag) > 64:
        raise ValueError('Требуется стабильный числовой тег')
    return tag


def sha(data):
    return hashlib.sha256(data).hexdigest()


class API:
    def __init__(self, token, sleep=time.sleep, opener=None, read_only=False):
        self.token, self.sleep = token, sleep
        self.read_only = read_only
        self.opener = opener or urllib.request.build_opener(SafeRedirect()).open

    def call(self, path, method='GET', body=None, binary=False, limit=2*1024*1024):
        if self.read_only and method != 'GET':
            raise ValueError('Validation API: read-only')
        url = path if path.startswith('https://') else 'https://api.github.com/' + path
        host = urllib.parse.urlsplit(url)
        if host.hostname not in ['api.github.com', 'uploads.github.com'] or host.scheme != 'https' or host.username:
            raise ValueError('Недоверенный API URL')
        headers = {'Authorization': 'Bearer ' + self.token, 'User-Agent': 'slipgate-cli-ci',
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if body is not None:
            headers['Content-Type'] = 'application/octet-stream' if isinstance(body, bytes) else 'application/json'
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
        # GET is retried here. Mutations are reconciled by the state machine;
        # never blindly repeat a POST after a lost response.
        for attempt in range(5):
            try:
                with self.opener(urllib.request.Request(url, body, headers, method=method), timeout=40) as response:
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise ValueError('Ответ превышает предел размера')
                    return data if binary else json.loads(data) if data else None
            except urllib.error.HTTPError as error:
                error.close()
                if method == 'GET' and error.code == 404:
                    return None
                if method != 'GET' or error.code not in [403, 429, 500, 502, 503, 504] or attempt == 4:
                    raise
                delay = max(60 * 2**attempt if error.code in [403, 429] else 2**attempt,
                            float(error.headers.get('Retry-After', 0)),
                            float(error.headers.get('X-RateLimit-Reset', 0)) - time.time()
                            if error.headers.get('X-RateLimit-Remaining') == '0' else 0)
                if delay > 120:
                    raise RuntimeError('Лимит API: повторите после сброса квоты') from error
                self.sleep(delay)
            except (OSError, TimeoutError):
                if method != 'GET' or attempt == 4:
                    raise
                self.sleep(2**attempt)

    def pages(self, path):
        result = []
        for page in range(1, 1001):
            items = self.call(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            if not isinstance(items, list):
                raise ValueError('Неверная страница API')
            result.extend(items)
            if not items:
                return result
        raise ValueError('Превышен безопасный предел пагинации')


def upstream(api, requested):
    tag = tag_value(requested) if requested else None
    release = api.call(f'repos/{UPSTREAM}/releases/' + ('tags/' + tag if tag else 'latest'))
    if not release or release['draft'] or release['prerelease']:
        raise ValueError('Нет стабильного upstream release')
    actual = tag_value(release['tag_name'])
    if tag and tag != actual:
        raise ValueError('Несовпадение тега релиза')
    ref = api.call(f'repos/{UPSTREAM}/git/ref/tags/{actual}')['object']
    for _ in range(8):
        if ref['type'] == 'commit':
            break
        if ref['type'] != 'tag' or not re.fullmatch('[a-f0-9]{40}', ref['sha']):
            raise ValueError('Неверный объект тега')
        ref = api.call(f'repos/{UPSTREAM}/git/tags/{ref["sha"]}')['object']
    if ref['type'] != 'commit' or not re.fullmatch('[a-f0-9]{40}', ref['sha']):
        raise ValueError('Не удалось разрешить exact commit')
    return {'tag': actual, 'commit': ref['sha']}


def find_release(api, tag):
    matches = [r for r in api.pages(f'repos/{REPO}/releases') if r['tag_name'] == tag]
    if len(matches) > 1:
        raise ValueError('Неоднозначный релиз')
    return matches[0] if matches else None


def ownership(release, plan, workflow):
    body = release.get('body') or ''
    if not release['draft'] or release.get('author', {}).get('login') != 'github-actions[bot]' or not body.startswith(PREFIX) or not body.endswith('\n-->'):
        raise ValueError('Чужой draft: ручная проверка владельца обязательна')
    marker = json.loads(body[len(PREFIX):-4])
    if marker['repo'] != REPO or marker['source'] != plan or marker['workflow'] != workflow or not re.fullmatch(r'[1-9][0-9]{0,19}', marker['run']):
        raise ValueError('Draft принадлежит другому source/workflow')
    return marker


def validate_bundle(files, plan):
    if set(files) != set(NAMES):
        raise ValueError('Неполный набор assets')
    for name, data in files.items():
        if not 0 < len(data) <= NAMES[name]:
            raise ValueError('Неверный размер: ' + name)
    exe = files['TgWsProxy_windows.exe']
    if len(exe) < 256 or exe[:2] != b'MZ':
        raise ValueError('Нет PE заголовка')
    offset = struct.unpack_from('<I', exe, 60)[0]
    if offset + 96 > len(exe) or exe[offset:offset+4] != b'PE\0\0' or struct.unpack_from('<H', exe, offset+4)[0] != 0x8664 or struct.unpack_from('<H', exe, offset+24)[0] != 0x20b:
        raise ValueError('Требуется PE x64')
    schema = json.loads(files['capabilities.json'])
    if any(schema.get(k) != v for k, v in {'schemaVersion': 1, 'repo': REPO, 'platform': 'win32', 'arch': 'x64', 'version': plan['tag'].removeprefix('v'), 'upstreamCommit': plan['commit'], 'binarySha256': sha(exe)}.items()):
        raise ValueError('Несовпадение schema identity/digest')
    if not re.fullmatch('[a-f0-9]{64}', schema.get('sourceSha256', '')):
        raise ValueError('Нет source digest')
    fields = schema.get('fields')
    if not isinstance(fields, list) or len(fields) > 64:
        raise ValueError('Неверный список полей')
    ids, flags = set(), set()
    for f in fields:
        if f.get('unsupported') or f.get('type') not in ['boolean', 'integer', 'number', 'enum', 'string', 'list'] or f['id'] in ids or f['flag'] in flags:
            raise ValueError('Требуется проверка типа/конфликта: ' + str(f.get('flag')))
        ids.add(f['id'])
        flags.add(f['flag'])
    routing = json.loads(files['routing.json'])
    if routing['upstreamCommit'] != plan['commit'] or routing['binarySha256'] != sha(exe):
        raise ValueError('Несовпадение routing identity')
    return {name: {'sha256': sha(data), 'size': len(data)} for name, data in files.items()}


def publish(api, plan, workflow, run, files, sleep=time.sleep):
    hashes = validate_bundle(files, plan)
    marker = {'repo': REPO, 'source': plan, 'workflow': workflow, 'run': run, 'assets': hashes}
    body = PREFIX + json.dumps(marker, sort_keys=True) + '\n-->'
    # After any ambiguous mutation, reread state. Missing/partial uploads can
    # only be removed inside this exact owned draft with this exact bundle.
    for attempt in range(5):
        release = find_release(api, plan['tag'])
        if release and not release['draft']:
            return 'existing-published'
        try:
            if release is None:
                release = api.call(f'repos/{REPO}/releases', 'POST', {'tag_name': plan['tag'], 'target_commitish': workflow,
                    'name': 'CLI ' + plan['tag'], 'body': body, 'draft': True, 'prerelease': False})
            owned = ownership(release, plan, workflow)
            if owned != marker:
                raise ValueError('Нужен исходный CI artifact из run ' + owned['run'])
            assets = api.pages(f'repos/{REPO}/releases/{release["id"]}/assets')
            if len({a['name'] for a in assets}) != len(assets) or any(a['name'] not in files for a in assets):
                raise ValueError('Чужие или повторяющиеся assets в draft')
            for name, data in files.items():
                asset = next((a for a in assets if a['name'] == name), None)
                if asset and asset['state'] == 'starter' and asset['size'] == 0:
                    api.call(f'repos/{REPO}/releases/assets/{asset["id"]}', 'DELETE')
                    asset = None
                if not asset:
                    time.sleep(1)
                    api.call(f'https://uploads.github.com/repos/{REPO}/releases/{release["id"]}/assets?name={name}', 'POST', data)
            # Required independent readback, including actual downloaded bytes.
            assets = api.pages(f'repos/{REPO}/releases/{release["id"]}/assets')
            if len(assets) != len(files) or {a['name'] for a in assets} != set(files):
                raise ValueError('Неполный набор после загрузки')
            for asset in assets:
                expected = hashes[asset['name']]
                if asset['state'] != 'uploaded' or asset['size'] != expected['size'] or asset.get('digest') != 'sha256:' + expected['sha256']:
                    raise ValueError('Digest/size/state mismatch: ' + asset['name'])
                raw = api.call(f'repos/{REPO}/releases/assets/{asset["id"]}', binary=True, limit=NAMES[asset['name']])
                if raw is None or sha(raw) != expected['sha256']:
                    raise ValueError('Readback digest mismatch: ' + asset['name'])
            current = api.call(f'repos/{REPO}/releases/{release["id"]}')
            if ownership(current, plan, workflow) != marker:
                raise ValueError('Draft изменился во время проверки')
            published = api.call(f'repos/{REPO}/releases/{release["id"]}', 'PATCH', {'draft': False, 'make_latest': 'legacy'})
            if not published or published['draft'] or published['tag_name'] != plan['tag']:
                raise ValueError('GitHub не подтвердил публикацию полного release')
            return 'published'
        except (OSError, TimeoutError) as error:
            if attempt == 4:
                raise RuntimeError('Публикация прервана; сохранён recoverable draft') from error
            delay = 2**attempt
            if isinstance(error, urllib.error.HTTPError):
                if error.code in [403, 429]:
                    delay = max(60 * 2**attempt, float(error.headers.get('Retry-After', 0)),
                                float(error.headers.get('X-RateLimit-Reset', 0)) - time.time()
                                if error.headers.get('X-RateLimit-Remaining') == '0' else 0)
                    if delay > 120:
                        raise RuntimeError('Лимит API: draft сохранён, повторите позже') from error
                elif error.code not in [409, 422, 500, 502, 503, 504]:
                    raise
            sleep(delay)
    raise RuntimeError('Не удалось завершить публикацию')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['resolve', 'publish', 'validate'])
    args = parser.parse_args()
    mode = os.environ.get('RUN_MODE', 'release')
    if mode not in ['release', 'validate'] or (args.phase == 'validate') != (mode == 'validate'):
        raise ValueError('Недопустимый режим/фаза')
    api = API(os.environ['GH_TOKEN'], read_only=mode == 'validate')
    if os.environ.get('GITHUB_REPOSITORY') != REPO:
        raise ValueError('Неверный repository')
    workflow = os.environ['GITHUB_SHA']
    if not re.fullmatch('[a-f0-9]{40}', workflow):
        raise ValueError('Неверный workflow commit')
    if args.phase in ['resolve', 'validate']:
        plan = upstream(api, os.environ.get('REQUESTED_TAG', ''))
        # Validation ignores target releases entirely: even an already mirrored
        # tag must build, and no draft/recovery state is read or changed.
        release = find_release(api, plan['tag']) if args.phase == 'resolve' else None
        recover = ownership(release, plan, workflow)['run'] if release and release['draft'] else ''
        out = {**plan, 'skip': str(bool(release and not release['draft'])).lower(), 'recover': recover}
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(''.join(f'{k}={v}\n' for k, v in out.items()))
        print(json.dumps(out))
    else:
        plan = {'tag': tag_value(os.environ['TAG']), 'commit': os.environ['SOURCE_COMMIT']}
        if upstream(api, plan['tag']) != plan:
            raise ValueError('Upstream tag изменился после сборки')
        files = {name: (Path('bundle') / name).read_bytes() for name in NAMES}
        run = os.environ.get('RECOVER_RUN') or os.environ['GITHUB_RUN_ID']
        print(publish(api, plan, workflow, run, files))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        message = 'Требуется проверка: ' + str(error)
        print(message)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(message + '\nПредыдущие опубликованные assets не изменены.\n')
        raise
