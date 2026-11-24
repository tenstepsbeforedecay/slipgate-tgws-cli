"""Static routing inventory from pinned text; never execute upstream Python."""
import ast
import argparse
import hashlib
import json
from pathlib import Path


def literals(source):
    out = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    try:
                        out[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return out


def generate(release, sources, schema):
    values = {}
    for name in ['proxy/bridge.py', 'proxy/utils.py']:
        values.update(literals(sources.get(name, '')))
    dc_ips = values['DC_DEFAULT_IPS']
    text = sources.get('proxy/utils.py', '') + '\n' + sources['proxy/tg_ws_proxy.py']
    tree = ast.parse(text)
    ws_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ['ws_domains', '_ws_domains'])
    fn = ast.get_source_segment(text, ws_fn)
    if 'if dc == 203:' not in fn or 'dc = 2' not in fn:
        raise ValueError('review DC203 WS alias')
    templates = []
    for node in sorted(ast.walk(ws_fn), key=lambda n: getattr(n, 'lineno', 0)):
        if isinstance(node, ast.Return):
            items = []
            for item in node.value.elts:
                if not isinstance(item, ast.JoinedStr):
                    raise ValueError('review WS domain templates')
                items.append(''.join(v.value if isinstance(v, ast.Constant) else '{dc}' for v in item.values))
            templates.append(items)
    if len(templates) != 2:
        raise ValueError('review WS variants')
    normal, media = (templates if 'if not is_media' in fn else templates[::-1])
    config = literals(sources['proxy/config.py'])
    # Reviewed _dd algorithm in exact config; do not import it.
    encoded = config['_CFPROXY_ENC']
    if 'ord(c)' not in sources['proxy/config.py']:
        raise ValueError('review CF decoder')
    def decode(domain):
        p = domain[:-4]
        n = sum(c.isalpha() for c in p)
        return ''.join(chr((ord(c) - 97 - n) % 26 + 97) if c.isalpha() else c for c in p) + '.co.uk'
    bridge = sources['proxy/bridge.py']
    connectivity = sources.get('ui/connectivity.py', sources.get('ui/ctk_tray_ui.py', ''))
    cv = literals(connectivity)
    redirects = dict(item.split(':', 1) for item in next(f['default'] for f in schema['fields'] if f['id'] == 'dcIp'))
    return {
        'upstreamCommit': release['commit'], 'binarySha256': release['assetDigest'],
        'dcIps': dc_ips, 'testDcIps': values.get('DC_TEST_IPS', {}), 'redirects': redirects,
        'ws': {dc: {'normal': [s.format(dc=2 if dc == 203 else dc) for s in normal],
                    'media': [s.format(dc=2 if dc == 203 else dc) for s in media]} for dc in dc_ips},
        'wsTemplates': {'normal': normal, 'media': media}, 'wsAliases': {'203': 2},
        'wsPath': '/apiws', 'wsTestPath': '/apiws_test' if 'DC_TEST_IPS' in values else None,
        'tcpPort': 443, 'cfPorts': [443, 80] if any(f['id'] == 'disableSecure' for f in schema['fields']) else [443],
        'cfDomains': [decode(d) for d in encoded],
        'cfRefreshUrl': 'https://raw.githubusercontent.com/Flowseal/tg-ws-proxy/main/.github/cfproxy-domains.txt',
        'fallback': ['cf_worker', 'cf', 'tcp'] if "methods.append('cf_worker')" in bridge else ['cf', 'tcp'],
        'prioritySelectable': 'fallback_cfproxy_priority' in bridge,
        'frontingSni': 'sprinthost.ru' if 'sprinthost.ru' in ''.join(sources.values()) else None,
        'h2Path': '/api' if "'/api'" in sources.get('proxy/cf_h2.py', '') else None,
        'guiProbeDcs': cv.get('_CFPROXY_TEST_DCS', []),
        'guiWorkerProbeIps': cv.get('_CFWORKER_TEST_DST', {}),
        'workerQueryMedia': 'media' in bridge,
        'sourceDigests': {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in sources.items()}
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'tests/fixtures/tgws-upstream'
    inventory = json.loads((root / 'inventory.json').read_text(encoding='utf-8-sig'))
    schemas = json.loads((root.parents[3] / 'src/shared/tgws-bundled-capabilities.json').read_text(encoding='utf-8'))
    result = {}
    for release in inventory['releases']:
        sources = {}
        for entry in release['sources']:
            raw = (root / release['version'] / entry['file']).read_text(encoding='utf-8-sig')
            if hashlib.sha256(raw.encode()).hexdigest() != entry['sha256']:
                raise ValueError('source hash mismatch')
            sources[entry['path']] = raw
        result[release['version']] = generate(release, sources, schemas[release['version']])
    output = root.parents[3] / 'src/shared/tgws-bundled-routing.json'
    if args.verify:
        if json.loads(output.read_text(encoding='utf-8')) != json.loads(json.dumps(result)):
            raise ValueError('bundled routing differs from pinned source generation')
    else:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
