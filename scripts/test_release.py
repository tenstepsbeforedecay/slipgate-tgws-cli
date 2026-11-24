import importlib.util
import unittest
import io
import json
import struct
import os
import tempfile
import re
import urllib.error
from unittest.mock import patch
from pathlib import Path
import release as r

PLAN = {'tag': 'v2.0.0', 'commit': 'a' * 40}
WORKFLOW = 'b' * 40


def bundle():
    binary = bytearray(512)
    binary[:2] = b'MZ'
    struct.pack_into('<I', binary, 60, 128)
    binary[128:132] = b'PE\0\0'
    struct.pack_into('<H', binary, 132, 0x8664)
    struct.pack_into('<H', binary, 152, 0x20b)
    binary = bytes(binary)
    schema = {'schemaVersion': 1, 'repo': r.REPO, 'version': '2.0.0', 'upstreamCommit': PLAN['commit'],
              'binarySha256': r.sha(binary), 'sourceSha256': 'c'*64, 'platform': 'win32', 'arch': 'x64', 'fields': []}
    routing = {'upstreamCommit': PLAN['commit'], 'binarySha256': r.sha(binary)}
    return {'TgWsProxy_windows.exe': binary, 'capabilities.json': json.dumps(schema).encode(),
            'routing.json': json.dumps(routing).encode(), 'build-info.json': b'{}'}


class FakeAPI:
    def __init__(self):
        self.release = None
        self.assets = []
        self.data = {}
        self.mutations = []
        self.fail_upload = False
        self.corrupt = False
        self.race = False
        self.lost_publish = False

    def pages(self, path):
        return list(self.assets) if '/assets' in path else [self.release] if self.release else []

    def call(self, path, method='GET', body=None, binary=False, limit=None):
        if method != 'GET':
            self.mutations.append((method, path))
        if method == 'POST' and path.endswith('/releases'):
            self.release = {**body, 'id': 1, 'author': {'login': 'github-actions[bot]'}}
            if self.race:
                self.release['draft'] = False
                raise OSError('Concurrent publish')
            return self.release.copy()
        if method == 'POST' and '?name=' in path:
            name = path.split('?name=')[1]
            if self.fail_upload:
                self.fail_upload = False
                raise OSError('Lost upload')
            asset = {'id': max(self.data, default=0)+1, 'name': name, 'state': 'uploaded', 'size': len(body), 'digest': 'sha256:' + r.sha(body)}
            self.assets.append(asset)
            self.data[asset['id']] = body
            return asset
        if method == 'GET' and binary:
            return b'corrupted' if self.corrupt else self.data[int(path.rsplit('/', 1)[1])]
        if method == 'GET':
            return self.release.copy()
        if method == 'PATCH':
            self.release.update(body)
            if self.lost_publish:
                self.lost_publish = False
                raise OSError('Lost publish response')
            return self.release.copy()
        if method == 'DELETE':
            self.assets = [a for a in self.assets if str(a['id']) != path.rsplit('/', 1)[1]]


def publish(api, files=None):
    with patch('release.time.sleep'):
        return r.publish(api, PLAN, WORKFLOW, '123', files or bundle(), sleep=lambda _: None)


class Contract(unittest.TestCase):
    def test_workflow_mode_and_branch_gate_matrix(self):
        local = Path(__file__).with_name('build-cli.yml')
        text = (local if local.exists() else Path(__file__).parents[1] / '.github/workflows/build-cli.yml').read_text(encoding='utf-8')
        conditions = dict(re.findall(r'^  ([\w-]+):\n(?:    needs:.*\n)?    if: (.*)$', text, re.M))

        def enabled(job, context):
            expr = conditions[job].replace("format('refs/heads/{0}', github.event.repository.default_branch)", "'refs/heads/main'")
            expr = re.sub(r'(?:github|inputs|needs)\.[\w.-]+', lambda m: repr(context.get(m[0], '')), expr)
            expr = expr.replace('always()', 'True').replace('cancelled()', 'False')
            expr = re.sub(r'!(?!=)', ' not ', expr.replace('&&', ' and ').replace('||', ' or '))
            return eval(expr, {'__builtins__': {}})

        for event, mode, branch, release_enabled, validation_enabled in [
            ('schedule', '', 'main', True, False),
            ('workflow_dispatch', 'release', 'main', True, False),
            ('workflow_dispatch', 'validate', 'main', False, True),
            ('workflow_dispatch', 'unknown', 'main', False, False),
            ('workflow_dispatch', '', 'main', False, False),
            ('workflow_dispatch', 'validate', 'feature', False, False),
            ('workflow_dispatch', 'release', 'feature', False, False),
            ('pull_request', 'release', 'main', False, False)]:
            with self.subTest(event=event, mode=mode, branch=branch):
                ctx = {'github.event_name': event, 'inputs.mode': mode, 'github.ref': 'refs/heads/' + branch}
                self.assertEqual(enabled('resolve', ctx), release_enabled)
                self.assertEqual(enabled('validate-resolve', ctx), validation_enabled)
                ctx.update({'needs.resolve.result': 'success' if release_enabled else 'skipped',
                            'needs.validate-resolve.result': 'success' if validation_enabled else 'skipped',
                            'needs.resolve.outputs.skip': 'false', 'needs.build.result': 'success'})
                self.assertEqual(enabled('build', ctx), release_enabled or validation_enabled)
                self.assertEqual(enabled('publish', ctx), release_enabled)
                if release_enabled:
                    ctx['needs.resolve.outputs.skip'] = 'true'
                    self.assertFalse(enabled('build', ctx))
                    self.assertFalse(enabled('publish', ctx))

    def test_normal_resolver_preserves_published_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'output')
            env = {'RUN_MODE': 'release', 'GH_TOKEN': 'dummy', 'GITHUB_REPOSITORY': r.REPO,
                   'GITHUB_SHA': WORKFLOW, 'GITHUB_OUTPUT': output}
            api = FakeAPI()
            api.release = {'tag_name': PLAN['tag'], 'draft': False}
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['release.py', 'resolve']), patch('release.API', return_value=api), patch('release.upstream', return_value=PLAN):
                r.main()
            self.assertIn('skip=true\nrecover=\n', Path(output).read_text())
            self.assertEqual(api.mutations, [])

    def test_validation_resolves_published_tag_without_touching_mirror(self):
        class SourceOnly:
            def call(self, path):
                self_test.assertIn('repos/' + r.UPSTREAM + '/', path)
                return ({'tag_name': PLAN['tag'], 'draft': False, 'prerelease': False}
                        if '/releases/' in path else {'object': {'type': 'commit', 'sha': PLAN['commit']}})
        self_test = self
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'output')
            env = {'RUN_MODE': 'validate', 'GH_TOKEN': 'read-only', 'GITHUB_REPOSITORY': r.REPO,
                   'GITHUB_SHA': WORKFLOW, 'REQUESTED_TAG': PLAN['tag'], 'GITHUB_OUTPUT': output}
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['release.py', 'validate']), patch('release.API', return_value=SourceOnly()):
                r.main()
            self.assertEqual(Path(output).read_text(), f'tag={PLAN["tag"]}\ncommit={PLAN["commit"]}\nskip=false\nrecover=\n')

    def test_validation_and_unknown_modes_cannot_enter_release_phases(self):
        for mode, phase in [('validate', 'publish'), ('validate', 'resolve'), ('unknown', 'publish'), ('release', 'validate')]:
            with self.subTest(mode=mode, phase=phase), patch.dict(os.environ, {'RUN_MODE': mode}, clear=True), patch('sys.argv', ['release.py', phase]), patch('release.API') as api:
                with self.assertRaisesRegex(ValueError, 'режим'):
                    r.main()
                api.assert_not_called()

    def test_validation_api_rejects_mutations_before_network(self):
        for method in ['POST', 'PATCH', 'DELETE', 'PUT']:
            with self.subTest(method=method):
                with patch('urllib.request.OpenerDirector.open') as opener:
                    with self.assertRaisesRegex(ValueError, 'read-only'):
                        r.API('dummy', read_only=True).call('repos/' + r.REPO + '/releases', method, {})
                    opener.assert_not_called()

    def test_validation_workflow_has_read_only_resolver_and_shared_windows_build(self):
        local = Path(__file__).with_name('build-cli.yml')
        workflow = (local if local.exists() else Path(__file__).parents[1] / '.github/workflows/build-cli.yml').read_text(encoding='utf-8')
        self.assertIn('default: validate', workflow)
        resolver = workflow.split('  validate-resolve:\n')[1].split('  build:\n')[0]
        self.assertIn('contents: read', resolver)
        self.assertNotIn('contents: write', resolver)
        self.assertIn("inputs.mode == 'validate'", resolver)
        self.assertIn('github.event.repository.default_branch', resolver)
        self.assertIn('python scripts/release.py validate', resolver)
        build = workflow.split('  build:\n')[1].split('  publish:\n')[0]
        self.assertIn('needs: [resolve, validate-resolve]', build)
        self.assertIn('always()', build)  # Release resolver is intentionally skipped in validate mode.
        self.assertIn('windows-2025', build)
        self.assertIn('python scripts/build-ci.py', build)
        self.assertNotIn('contents: write', build)
        self.assertNotIn('GH_TOKEN:', build)
        self.assertIn('cli-validation', build)
        publish_job = workflow.split('  publish:\n')[1]
        self.assertIn("inputs.mode == 'release'", publish_job)
        self.assertIn("needs.resolve.result == 'success'", publish_job)

    def test_release_engine_exists(self):
        self.assertTrue(Path(__file__).with_name('release.py').is_file(), 'Нет безопасного механизма draft → publish')

    def test_staged_then_verified_then_published(self):
        api = FakeAPI()
        self.assertEqual(publish(api), 'published')
        self.assertFalse(api.release['draft'])
        self.assertEqual(len(api.assets), 4)
        self.assertEqual(api.mutations[-1][0], 'PATCH')

    def test_existing_published_is_byte_identical_no_op(self):
        api = FakeAPI()
        publish(api)
        before = json.dumps(api.release), dict(api.data), list(api.mutations)
        self.assertEqual(publish(api), 'existing-published')
        self.assertEqual((json.dumps(api.release), api.data, api.mutations), before)

    def test_partial_upload_reconciles(self):
        api = FakeAPI()
        api.fail_upload = True
        self.assertEqual(publish(api), 'published')
        self.assertEqual(len(api.assets), 4)

    def test_lost_create_and_upload_responses_reconcile_without_duplicates(self):
        for phase in ['create', 'upload']:
            class LostResponseAPI(FakeAPI):
                lost = False
                def call(self, path, method='GET', body=None, **kwargs):
                    result = super().call(path, method, body, **kwargs)
                    match = path.endswith('/releases') if phase == 'create' else '?name=' in path
                    if method == 'POST' and match and not self.lost:
                        self.lost = True
                        raise TimeoutError('Response lost after server accepted data')
                    return result
            api = LostResponseAPI()
            self.assertEqual(publish(api), 'published')
            self.assertEqual(len(api.assets), 4)
            self.assertEqual(len([m for m in api.mutations if m[0] == 'POST']), 5)

    def test_only_owned_empty_starter_can_be_replaced(self):
        api = FakeAPI()
        api.corrupt = True
        with self.assertRaises(ValueError):
            publish(api)
        api.corrupt = False
        api.assets[0].update(state='starter', size=0)
        self.assertEqual(publish(api), 'published')
        self.assertEqual(len([m for m in api.mutations if m[0] == 'DELETE']), 1)

    def test_network_get_retries_five_times(self):
        attempts, waits = [], []
        def opener(*args, **kwargs):
            attempts.append(1)
            raise TimeoutError('network')
        with self.assertRaises(TimeoutError):
            r.API('dummy', waits.append, opener).call('test')
        self.assertEqual(len(attempts), 5)
        self.assertEqual(waits, [1, 2, 4, 8])

    def test_own_draft_resumes_without_overwrite(self):
        api = FakeAPI()
        api.corrupt = True
        with self.assertRaisesRegex(ValueError, 'Readback'):
            publish(api)
        self.assertTrue(api.release['draft'])
        uploads = len(api.mutations)
        api.corrupt = False
        self.assertEqual(publish(api), 'published')
        self.assertEqual(len(api.mutations), uploads + 1)

    def test_foreign_draft_untouched(self):
        api = FakeAPI()
        api.release = {'tag_name': PLAN['tag'], 'draft': True, 'body': 'Ручные заметки', 'author': {'login': 'maintainer'}}
        with self.assertRaisesRegex(ValueError, 'Чужой draft'):
            publish(api)
        self.assertEqual(api.mutations, [])

    def test_concurrent_race_published_never_overwritten(self):
        api = FakeAPI()
        api.race = True
        self.assertEqual(publish(api), 'existing-published')
        self.assertEqual(len(api.mutations), 1)

    def test_lost_publish_response_reconciles(self):
        api = FakeAPI()
        api.lost_publish = True
        self.assertEqual(publish(api), 'existing-published')
        self.assertEqual(len([m for m in api.mutations if m[0] == 'PATCH']), 1)

    def test_changed_workflow_cannot_claim_draft(self):
        api = FakeAPI()
        api.corrupt = True
        with self.assertRaises(ValueError):
            publish(api)
        with self.assertRaisesRegex(ValueError, 'другому source/workflow'):
            r.ownership(api.release, PLAN, 'f'*40)

    def test_different_bundle_cannot_overwrite_owned_draft(self):
        api = FakeAPI()
        api.corrupt = True
        with self.assertRaises(ValueError):
            publish(api)
        files = bundle()
        files['build-info.json'] = b'{"different":true}'
        mutations = list(api.mutations)
        with self.assertRaisesRegex(ValueError, 'исходный CI artifact'):
            publish(api, files)
        self.assertEqual(mutations, api.mutations)

    def test_pe_and_identity_mismatches_block_before_create(self):
        for name in ['TgWsProxy_windows.exe', 'capabilities.json', 'routing.json']:
            with self.subTest(name=name):
                api, files = FakeAPI(), bundle()
                files[name] = b'{}'
                with self.assertRaises((ValueError, KeyError)):
                    publish(api, files)
                self.assertEqual(api.mutations, [])

    def test_unsupported_and_duplicate_fields_fail_closed(self):
        for fields in [[{'unsupported': True}], [{'type': 'unknown'}],
                       [{'id': 'x', 'flag': '--xx', 'type': 'boolean'}]*2]:
            files = bundle()
            schema = json.loads(files['capabilities.json'])
            schema['fields'] = fields
            files['capabilities.json'] = json.dumps(schema).encode()
            with self.assertRaises(ValueError):
                publish(FakeAPI(), files)

    def test_tag_injection_and_prerelease_blocked(self):
        for value in ['v2.0.0\nabc', 'v2.0.0;whoami', '../main', 'v2.0.0-rc1', 'v02.0.0', '${{ secrets.TOKEN }}']:
            with self.assertRaises(ValueError):
                r.tag_value(value)

    def test_old_updater_selects_same_exact_exe_among_json_assets(self):
        # The historic updater's first-choice regex is unchanged.
        import re
        names = list(bundle())
        selected = next(name for name in names if re.fullmatch(r'TgWsProxy_windows\.exe', name, re.I))
        self.assertEqual(selected, 'TgWsProxy_windows.exe')

    def test_rate_limit_and_network_retries_are_bounded(self):
        attempts, waits = [], []
        def opener(*args, **kwargs):
            attempts.append(1)
            raise urllib.error.HTTPError('https://api.github.com/test', 429, 'limit', {'Retry-After': '60'}, io.BytesIO())
        with self.assertRaisesRegex(RuntimeError, 'Лимит API'):
            r.API('dummy', waits.append, opener).call('test')
        self.assertEqual(len(attempts), 3)  # next exponential delay exceeds 120s budget
        self.assertEqual(waits, [60, 120])

    def test_mutations_not_blindly_retried(self):
        calls = []
        def opener(*args, **kwargs):
            calls.append(1)
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            r.API('dummy', lambda _: None, opener).call('test', 'POST', {})
        self.assertEqual(len(calls), 1)

    def test_pagination_reaches_empty_page(self):
        api = r.API('dummy')
        calls = []
        def call(path):
            calls.append(path)
            return [{'id': 1}] if len(calls) == 1 else []
        api.call = call
        self.assertEqual(api.pages('test'), [{'id': 1}])
        self.assertEqual(len(calls), 2)

    def test_missing_schema_never_creates_public_legacy_release(self):
        files = bundle()
        del files['capabilities.json']
        api = FakeAPI()
        with self.assertRaisesRegex(ValueError, 'Неполный'):
            publish(api, files)
        self.assertEqual(api.mutations, [])

    def test_asset_api_digest_mismatch_leaves_draft(self):
        api = FakeAPI()
        api.corrupt = True
        with self.assertRaises(ValueError):
            publish(api)
        api.corrupt = False
        api.assets[0]['digest'] = 'sha256:' + 'f'*64
        with self.assertRaisesRegex(ValueError, 'Digest'):
            publish(api)
        self.assertTrue(api.release['draft'])

    def test_redirect_strips_token_and_rejects_external_host(self):
        import urllib.request
        request = urllib.request.Request('https://api.github.com/test', headers={'Authorization': 'Bearer dummy'})
        redirected = r.SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/test')
        self.assertIsNone(redirected.get_header('Authorization'))
        with self.assertRaises(ValueError):
            r.SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://attacker.example/test')

    def test_exact_source_resolution_and_injection(self):
        class SourceAPI:
            def call(self, path):
                return {'tag_name': 'v2.0.0', 'draft': False, 'prerelease': False} if '/releases/' in path else {'object': {'type': 'commit', 'sha': PLAN['commit']}}
        self.assertEqual(r.upstream(SourceAPI(), ''), PLAN)
        with self.assertRaises(ValueError):
            r.upstream(SourceAPI(), 'v2.0.0\nBAD=1')

    def test_ast_elementary_contracts_and_unknown_types(self):
        spec = importlib.util.spec_from_file_location('generator', Path(__file__).with_name('generate-capabilities.py'))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        source = '''raise RuntimeError('must never execute')
def main():
    parser.add_argument('--new-bool', action='store_true')
    parser.add_argument('--new-int', type=int, default=3)
    parser.add_argument('--new-enum', choices=['a', 'b'], default='a')
    parser.add_argument('--new-string', default='hello')
    parser.add_argument('--new-list', action='append', type=str)
    parser.add_argument('--new-complex', type=custom_type, default='a')
'''
        result = gen.generate(source, {'fields': []}, PLAN['tag'], PLAN['commit'], b'data')
        self.assertEqual([f['type'] for f in result['fields'][:5]], ['boolean', 'integer', 'enum', 'string', 'list'])
        self.assertTrue(all(not f.get('unsupported') for f in result['fields'][:5]))
        self.assertTrue(result['fields'][5]['unsupported'])
        for declaration in ["parser.add_argument('--new-flag', default=dynamic())", "parser.add_argument('--new-flag', **dynamic)"]:
            with self.assertRaises(ValueError):
                gen.generate('def main():\n    ' + declaration, {'fields': []}, PLAN['tag'], PLAN['commit'], b'data')

    def test_reviewed_routing_contracts_on_all_19_snapshots(self):
        spec = importlib.util.spec_from_file_location('routing_contracts', Path(__file__).with_name('make-routing-contracts.py'))
        contracts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contracts)
        fixtures = Path(__file__).parents[1] / 'tests/fixtures/tgws-upstream'
        if not fixtures.exists():
            # Mirror ships contracts, not Slipgate's historical source inventory.
            self.assertTrue(Path(__file__).with_name('routing-contracts.json').is_file())
            return
        inventory = json.loads((fixtures / 'inventory.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(len(inventory['releases']), 19)
        for release in inventory['releases']:
            sources = {e['path']: (fixtures / release['version'] / e['file']).read_text(encoding='utf-8-sig') for e in release['sources']}
            contracts.validate(sources)
        changed = {**sources, 'proxy/config.py': sources['proxy/config.py'].replace('ord(c)', 'ord(c) + 1')}
        with self.assertRaisesRegex(ValueError, 'routing contract'):
            contracts.validate(changed)


if __name__ == '__main__':
    unittest.main()
