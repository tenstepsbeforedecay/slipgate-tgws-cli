"""Read exact upstream source as AST data, NEVER import or execute it.

The semantic overlay is reviewed by the mirror maintainer. Unknown flags are
reported, but remain unsupported until their semantics/labels are reviewed.
Run after compiling the binary, before uploading either release asset.
"""
import argparse
import ast
import hashlib
import json
import re
from pathlib import Path


def literal(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [literal(x) for x in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -literal(node.operand)
    raise ValueError('non-literal argparse metadata')


def source_locales(sources):
    locales = {lang: json.loads(sources.get('ui/i18n/' + lang + '.json', '{}')) for lang in ['ru', 'en']}
    # Before localization (1.6.6–1.7.3), these literal UI labels lived in
    # settings_form/ctk_tray_ui. Read the literals, never import the UI module.
    if not locales['ru']:
        keys = {'dc_inner': 'section.dc', 'cf_cb': 'label.cf_enable', 'cf_custom_cb': 'label.cf_custom_domain',
                'cf_worker_lbl': 'label.cfworker_domains', 'verbose_cb': 'label.verbose', 'cf_prio_cb': 'label.cfproxy_priority'}
        for name in ['ui/ctk_tray_ui.py', 'ui/settings_form.py']:
            if name not in sources:
                continue
            for node in ast.walk(ast.parse(sources[name])):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.value.args) > 3:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in keys and isinstance(node.value.args[3], ast.Constant) and isinstance(node.value.args[3].value, str):
                            locales['ru'][keys[target.id]] = node.value.args[3].value
                if isinstance(node, ast.Tuple) and len(node.elts) >= 3 and all(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in node.elts[:2]):
                    label, key = (n.value for n in node.elts[:2])
                    if key in ['buf_kb', 'pool_size']:
                        locales['ru']['label.' + key] = label
    return locales


def presentation(fields, sources, locales):
    # Sections are taken from the actual GUI constructor and captions. This
    # maps sections, not the desktop's physical row/column layout.
    text = sources.get('ui/settings_form.py', sources.get('ui/ctk_tray_ui.py', ''))
    sections = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            name = call.func.id if isinstance(call.func, ast.Name) else ''
            if name not in ['_config_section', 'create_config_section'] or len(call.args) < 4:
                continue
            title = call.args[3]
            if isinstance(title, ast.Call) and title.args and isinstance(title.args[0], ast.Constant):
                key = title.args[0].value
                label = {lang: locales.get(lang, {}).get(key, key) for lang in ['ru', 'en']}
            elif isinstance(title, ast.Constant):
                label = {'ru': title.value, 'en': title.value}
            else:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    sections[target.id] = label
    groups = {'dcIp': 'dc_inner', 'cfproxy': 'cf_inner', 'h2': 'cf_inner',
              'cfproxyPriority': 'cf_inner', 'cfproxyUserDomain': 'cf_inner',
              'cfproxyWorkerDomain': 'cf_worker_inner', 'verbose': 'log_inner',
              'disableSecure': 'log_inner', 'bufKb': 'log_inner', 'poolSize': 'log_inner'}
    for field in fields:
        parent = groups.get(field['id'])
        # Require the literal GUI label to exist; CLI presence alone is not UI evidence.
        if parent in sections and field.get('upstreamLabelKey'):
            field['group'] = {'id': parent, 'label': sections[parent]}
        if field['id'] == 'cfproxyPriority':
            if 'Пробовать CF-прокси раньше прямого TCP-подключения' not in text:
                raise ValueError('cfproxy priority semantics changed')
            field['label'] = {'ru': 'CF-прокси раньше прямого TCP', 'en': 'CF proxy before direct TCP'}


def generate(source, overlay, version, commit, binary, locales=None, binary_digest=None):
    tree = ast.parse(source)
    reviewed = {f['flag']: f for f in overlay['fields']}
    fields = []
    seen = set()
    defaults_dc = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == 'args' and t.attr == 'dc_ip' for t in node.targets):
            if isinstance(node.value, ast.List) and node.value.elts:
                defaults_dc = literal(node.value)
    # Only the CLI main is inspected; helpers/GUI parsers cannot authorize flags.
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    for node in ast.walk(main):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'add_argument':
            continue
        try:
            flags = [literal(a) for a in node.args]
            flag = next(f for f in flags if isinstance(f, str) and f.startswith('--'))
        except (ValueError, StopIteration):
            raise ValueError('dynamic/short-only argparse declaration requires review') from None
        if flag in seen:
            raise ValueError('duplicate CLI flag')
        seen.add(flag)
        if re.search(r'path|file|exec|config|output|directory|command|script', flag) or flag in ['--host', '--port', '--secret', '--log-max-mb', '--log-backups', '--cfproxy-h2-media']:
            continue
        kw = {k.arg: k.value for k in node.keywords}
        if None in kw:
            raise ValueError('dynamic argparse keywords require review: ' + flag)
        try:
            action = literal(kw['action']) if 'action' in kw else 'store'
            default = literal(kw['default']) if 'default' in kw else False if action == 'store_true' else True if action == 'store_false' else None
            choices = literal(kw['choices']) if 'choices' in kw else None
            name = kw.get('type')
            type_name = name.id if isinstance(name, ast.Name) else 'str' if name is None else None
            field_type = 'boolean' if action in ['store_true', 'store_false'] else 'list' if action == 'append' else 'enum' if choices else {'int': 'integer', 'float': 'number', 'str': 'string'}.get(type_name, 'unknown')
            f = dict(reviewed.get(flag, {}))
            known = bool(f)
            if known and f['type'] == 'boolean' and action != ('store' if flag == '--cfproxy-priority' else 'store_true'):
                raise ValueError('reviewed boolean action changed: ' + flag)
            if flag in ['--cfproxy-domain', '--cfproxy-worker-domain'] and action == 'store' and type_name == 'str':
                field_type = 'list'
                default = [default] if default else []
                f['maxItems'] = 1
            if flag == '--cfproxy-priority' and type_name == '_parse_bool' and known:
                field_type = 'boolean'
                f['booleanValue'] = True
            if not known:
                parts = flag[2:].split('-')
                f = {'id': parts[0] + ''.join(p.title() for p in parts[1:]), 'semantics': 1, 'flag': flag, 'label': {'ru': 'Параметр ' + flag, 'en': 'Option ' + flag}, 'unsupported': True}
                # Only elementary literal argparse contracts can be automated.
                # Post-parse transformations/custom parsers require review.
                dest = literal(kw['dest']) if 'dest' in kw else flag[2:].replace('-', '_')
                transformed = any(isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and
                    any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and
                        t.value.id == 'args' and t.attr == dest
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
                    for n in ast.walk(main))
                simple = (action in ['store', 'append', 'store_true', 'store_false'] and
                          type_name in ['str', 'int', 'float'] and field_type != 'unknown' and
                          not transformed and not any(k in kw for k in ['nargs', 'const', 'required']) and
                          (default is not None or action == 'append'))
                if simple:
                    f.pop('unsupported')
            label_key = f.get('upstreamLabelKey')
            if label_key and locales:
                f['label'] = {lang: locales.get(lang, {}).get(label_key, f['label'][lang]) for lang in ['ru', 'en']}
            # CLI-only captions are reviewed metadata, independent of the exact
            # machine-readable flag. Future manifests can supply these too.
            elif not label_key and not known:
                f['label'] = {'ru': flag, 'en': flag}
            if known and field_type != f['type']:
                f['unsupported'] = True
            f['type'] = field_type
            if flag == '--dc-ip':
                if defaults_dc is None:
                    raise ValueError('dc-ip semantics require review')
                bare = 'nargs' in kw and literal(kw['nargs']) == '?' and 'const' in kw and literal(kw['const']) is None
                f['bareEmpty'] = bare
                default = defaults_dc
            elif field_type == 'list' and default is None:
                default = []
            if field_type == 'boolean' and not f.get('booleanValue'):
                when = f.get('flagWhen', action == 'store_true')
                f['flagWhen'] = when
                # An inverse UI switch (cfproxy/h2) maps --no-* to false.
                default = not default if when is False and action == 'store_true' else default
            f['default'] = default
            if choices:
                f['choices'] = choices
            fields.append(f)
        except (ValueError, TypeError):
            raise ValueError('argparse metadata changed; reviewed overlay required: ' + flag) from None
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('exact upstream commit required')
    return {'schemaVersion': 1, 'repo': 'tenstepsbeforedecay/slipgate-tgws-cli', 'platform': 'win32', 'arch': 'x64', 'version': version.removeprefix('v'), 'upstreamCommit': commit,
            'sourceSha256': hashlib.sha256(source.encode()).hexdigest(), 'binarySha256': binary_digest or hashlib.sha256(binary).hexdigest(), 'fields': fields}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory')
    parser.add_argument('--verify', action='store_true')
    for arg in ['source', 'overlay', 'version', 'commit', 'binary', 'output']:
        parser.add_argument('--' + arg)
    args = parser.parse_args()
    if args.inventory:
        inventory_file = Path(args.inventory)
        inventory = json.loads(inventory_file.read_text(encoding='utf-8-sig'))
        if not inventory['pages'] or inventory['pages'][-1]['count'] != 0:
            raise ValueError('release pagination incomplete')
        overlay = json.loads(Path(args.overlay).read_text(encoding='utf-8-sig'))
        matrix = {}
        for release in inventory['releases']:
            directory = inventory_file.parent / release['version']
            sources = {}
            for info in release['sources']:
                raw = (directory / info['file']).read_text(encoding='utf-8-sig')
                if hashlib.sha256(raw.encode()).hexdigest() != info['sha256']:
                    raise ValueError('source fixture digest mismatch: ' + info['file'])
                sources[info['path']] = raw
            source = sources['proxy/tg_ws_proxy.py']
            locales = source_locales(sources)
            schema = generate(source, overlay, release['tag'], release['commit'], None, locales, release['assetDigest'])
            presentation(schema['fields'], sources, locales)
            matrix[release['version']] = schema
            cli = []
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
                    cli.append(ast.get_source_segment(source, node))
            release['cliDeclarations'] = cli
            release['schemaFlags'] = [f['flag'] for f in schema['fields']]
            release['bareDcIp'] = next(f['bareEmpty'] for f in schema['fields'] if f['id'] == 'dcIp')
            print(release['version'] + ': ' + ', '.join(release['schemaFlags']))
        if args.verify:
            if json.loads(Path(args.output).read_text(encoding='utf-8-sig')) != matrix:
                raise ValueError('bundled matrix differs from exact source generation')
        else:
            Path(args.output).write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            inventory_file.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return
    if not all([args.source, args.overlay, args.version, args.commit, args.binary, args.output]):
        parser.error('source, overlay, version, commit, binary and output are required')
    root = Path(args.source).parent.parent
    sources = {name: (root / name).read_text(encoding='utf-8-sig') for name in ['ui/i18n/ru.json', 'ui/i18n/en.json', 'ui/ctk_tray_ui.py', 'ui/settings_form.py'] if (root / name).is_file()}
    locales = source_locales(sources) if sources else None
    result = generate(Path(args.source).read_text(encoding='utf-8-sig'), json.loads(Path(args.overlay).read_text(encoding='utf-8-sig')), args.version, args.commit, Path(args.binary).read_bytes(), locales)
    presentation(result['fields'], sources, locales or {})
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if len(data.encode()) > 128 * 1024:
        raise ValueError('schema exceeds limit')
    Path(args.output).write_text(data + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
