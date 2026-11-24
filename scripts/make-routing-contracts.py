"""Maintainer-only inventory from reviewed local snapshots, not release input."""
import ast
import hashlib
import json
from pathlib import Path


def fingerprint(node):
    # Stable between Python 3.11 and 3.14 (ast.dump changed empty-field output).
    def canonical(value):
        if isinstance(value, ast.AST):
            return {'node': type(value).__name__, **{key: canonical(item) for key, item in ast.iter_fields(value)
                    if key != 'type_params' and item is not None and item != []}}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value
    return hashlib.sha256(json.dumps(canonical(node), sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def contracts(sources):
    result = {}
    for name, text in sources.items():
        if name not in ['proxy/utils.py', 'proxy/tg_ws_proxy.py', 'proxy/config.py']:
            continue
        for node in ast.parse(text).body:
            if isinstance(node, ast.FunctionDef) and node.name in ['ws_domains', '_ws_domains', '_dd']:
                result[node.name] = fingerprint(node)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_S' for t in node.targets):
                result['_S'] = fingerprint(node.value)
    return result


def validate(sources):
    reviewed = json.loads(Path(__file__).with_name('routing-contracts.json').read_text())
    actual = contracts(sources)
    if not ('ws_domains' in actual or '_ws_domains' in actual):
        raise ValueError('Требуется проверка WS routing')
    if not all(key in actual for key in ['_dd', '_S']):
        raise ValueError('Требуется проверка CF routing')
    for key, digest in actual.items():
        if digest not in reviewed.get(key, []):
            raise ValueError('Изменился routing contract, требуется проверка: ' + key)


def main():
    root = Path(__file__).parents[1] / 'tests/fixtures/tgws-upstream'
    inventory = json.loads((root / 'inventory.json').read_text(encoding='utf-8-sig'))
    result = {}
    for release in inventory['releases']:
        sources = {e['path']: (root / release['version'] / e['file']).read_text(encoding='utf-8-sig') for e in release['sources']}
        for key, digest in contracts(sources).items():
            result.setdefault(key, set()).add(digest)
    Path(__file__).with_name('routing-contracts.json').write_text(json.dumps({k: sorted(v) for k, v in result.items()}, indent=2) + '\n')


if __name__ == '__main__':
    main()
