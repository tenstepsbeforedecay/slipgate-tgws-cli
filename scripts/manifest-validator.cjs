// Снимок production validator src/shared/tgws-capabilities.ts; bundled data исключены.
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TGWS_SCHEMA_LIMIT = exports.TGWS_CAPABILITY_REPO = void 0;
exports.tgwsOptionHasCustomValue = tgwsOptionHasCustomValue;
exports.tgwsRetainedOption = tgwsRetainedOption;
exports.tgwsFieldLabel = tgwsFieldLabel;
exports.tgwsRoutingForSchema = tgwsRoutingForSchema;
exports.tgwsVisibleFields = tgwsVisibleFields;
exports.tgwsDiagnosticPlan = tgwsDiagnosticPlan;
exports.supportedTgwsField = supportedTgwsField;
exports.validateTgwsValue = validateTgwsValue;
exports.validateTgwsManifest = validateTgwsManifest;
exports.bundledTgwsCapabilities = bundledTgwsCapabilities;
exports.legacyTgwsValues = legacyTgwsValues;
exports.compileTgwsOptions = compileTgwsOptions;
const bundled = {};
const routing = {};
function tgwsOptionHasCustomValue(field, value) {
    return JSON.stringify(value) !== JSON.stringify(field.default);
}
/** Display only: bundled, reviewed history never authorizes an executable flag. */
function tgwsRetainedOption(id, schema, language) {
    const lang = language.startsWith('ru') ? 'ru' : 'en';
    const history = Object.values(bundled).filter((s) => s.fields.some((f) => f.id === id && supportedTgwsField(f, s)));
    const field = schema?.fields.find((f) => f.id === id) ?? history[0]?.fields.find((f) => f.id === id);
    const caption = field?.label[lang];
    return {
        label: caption && caption !== field?.flag
            ? caption.endsWith(`(${id})`)
                ? caption
                : `${caption} (${id})`
            : id,
        versions: history.map((s) => s.version)
    };
}
/** Preserve exact GUI captions; CLI-only captions retain their separate flag. */
function tgwsFieldLabel(field, language) {
    const label = field.label[language.startsWith('ru') ? 'ru' : 'en'];
    return field.upstreamLabelKey || label === field.flag ? label : `${label} (${field.flag})`;
}
/** Endpoint metadata never authorizes a binary and requires exact attestation. */
function tgwsRoutingForSchema(schema) {
    if (!schema)
        return null;
    const entry = routing[schema.version];
    return entry &&
        entry.binarySha256 === schema.binarySha256 &&
        entry.upstreamCommit === schema.upstreamCommit
        ? entry
        : null;
}
function tgwsVisibleFields(schema) {
    return schema?.fields.filter((f) => supportedTgwsField(f, schema)) ?? [];
}
function tgwsDiagnosticPlan(snapshot) {
    const routes = tgwsRoutingForSchema(snapshot.schema);
    if (!routes || !snapshot.schema)
        return null;
    const test = snapshot.values.forceTestDc === true;
    const ips = test ? routes.testDcIps : routes.dcIps;
    const redirects = Object.fromEntries((snapshot.values.dcIp ?? []).map((item) => item.split(':', 2)));
    const targets = Object.entries({ ...ips, ...redirects }).map(([dc, address]) => {
        const hostname = routes.wsTemplates.normal[0].replace('{dc}', String(routes.wsAliases[dc] ?? dc));
        return {
            dc: Number(dc),
            handshakeDc: Number(dc) + (test ? 10000 : 0),
            address: redirects[dc] ?? address,
            hostname,
            path: (test ? routes.wsTestPath : routes.wsPath) ?? routes.wsPath,
            mode: redirects[dc] && hostname ? 'ws' : 'tcp',
            override: snapshot.overridden.includes('dcIp') && !!redirects[dc]
        };
    });
    const key = JSON.stringify([snapshot.schema.binarySha256, snapshot.values, targets]);
    return { key, version: snapshot.schema.version, targets };
}
exports.TGWS_CAPABILITY_REPO = 'tenstepsbeforedecay/slipgate-tgws-cli';
exports.TGWS_SCHEMA_LIMIT = 128 * 1024;
const own = (v, key) => Object.prototype.hasOwnProperty.call(v, key);
const supportedTypes = ['boolean', 'integer', 'number', 'string', 'enum', 'list'];
const protectedFlags = [
    '--host',
    '--port',
    '--secret',
    '--help',
    '--log-file',
    '--log-max-mb',
    '--log-backups',
    '--config',
    '--binary-path',
    '--executable',
    '--version'
];
function supportedTgwsField(field, schema) {
    return (!field.unsupported &&
        supportedTypes.includes(field.type) &&
        (!field.requires ||
            field.requires.every((d) => d.id !== field.id &&
                !!schema?.fields.some((f) => f.id === d.id && !f.unsupported && supportedTypes.includes(f.type)))));
}
function validateTgwsValue(field, value) {
    const fail = () => {
        throw new Error(`Invalid TgWs option: ${field.id}`);
    };
    const string = (v) => typeof v === 'string' &&
        v.length <= 2048 &&
        !/^[\s]*-/.test(v) &&
        !/[$`;&|<>]/.test(v) &&
        !Array.from(v).some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127);
    if (field.type === 'boolean') {
        if (typeof value !== 'boolean')
            fail();
    }
    else if (field.type === 'integer' || field.type === 'number') {
        if (typeof value !== 'number' ||
            !Number.isFinite(value) ||
            (field.type === 'integer' && !Number.isSafeInteger(value)) ||
            value < (field.min ?? -1e9) ||
            value > (field.max ?? 1e9))
            fail();
    }
    else if (field.type === 'list') {
        if (!Array.isArray(value) || value.length > (field.maxItems ?? 32) || !value.every(string))
            fail();
    }
    else if (field.type === 'string' || field.type === 'enum') {
        if (!string(value) || (field.type === 'enum' && !field.choices?.includes(value)))
            fail();
    }
    else
        fail();
    const items = Array.isArray(value) ? value : [value];
    for (const item of items) {
        if (field.format === 'domain' &&
            item !== '' &&
            (typeof item !== 'string' ||
                item.length > 253 ||
                !/^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z][a-zA-Z0-9-]{1,62}$/.test(item)))
            fail();
        if (field.format === 'dc' &&
            (typeof item !== 'string' ||
                !/^-?\d+:(?:\d{1,3}\.){3}\d{1,3}$/.test(item) ||
                item
                    .split(':')[1]
                    .split('.')
                    .some((n) => Number(n) > 255)))
            fail();
    }
    return value;
}
function validateTgwsManifest(value, version, digest) {
    if (!value || typeof value !== 'object' || JSON.stringify(value).length > exports.TGWS_SCHEMA_LIMIT)
        throw new Error('Invalid TgWs manifest');
    const s = value;
    if (s.schemaVersion !== 1 ||
        s.version !== version ||
        s.binarySha256 !== digest ||
        !/^[a-f0-9]{64}$/.test(digest) ||
        s.repo !== exports.TGWS_CAPABILITY_REPO ||
        s.platform !== 'win32' ||
        s.arch !== 'x64' ||
        !/^[a-f0-9]{40}$/.test(s.upstreamCommit ?? '') ||
        !Array.isArray(s.fields) ||
        s.fields.length > 64)
        throw new Error('TgWs manifest identity mismatch');
    const ids = new Set(), flags = new Set();
    for (const f of s.fields) {
        if (!f ||
            !/^[a-z][a-zA-Z0-9]{0,47}$/.test(f.id) ||
            ['constructor', 'prototype', '__proto__'].includes(f.id) ||
            ['host', 'port', 'secret', 'binaryPath', 'executable', 'path'].includes(f.id) ||
            !Number.isSafeInteger(f.semantics) ||
            f.semantics < 1 ||
            !/^--[a-z][a-z0-9-]{1,63}$/.test(f.flag) ||
            protectedFlags.some((p) => p.startsWith(f.flag) || f.flag === p) ||
            /^--(?:host|port|secret)-/.test(f.flag) ||
            /(?:path|file|exec|config|output|directory|command|script|secret|password|token|listen|bind)/i.test(f.flag) ||
            ids.has(f.id) ||
            flags.has(f.flag) ||
            own(f, 'aliases') ||
            !f.label ||
            typeof f.label.ru !== 'string' ||
            typeof f.label.en !== 'string' ||
            f.label.ru.length > 300 ||
            f.label.en.length > 300)
            throw new Error('Unsafe TgWs manifest field');
        ids.add(f.id);
        flags.add(f.flag);
        if (f.group &&
            (typeof f.group.id !== 'string' ||
                !/^[a-z][a-z0-9_]{0,47}$/.test(f.group.id) ||
                !f.group.label ||
                ['ru', 'en'].some((lang) => typeof f.group?.label[lang] !== 'string' || f.group.label[lang].length > 300)))
            throw new Error('Invalid TgWs presentation group');
        if (f.requires &&
            (!Array.isArray(f.requires) ||
                f.requires.length > 8 ||
                f.requires.some((d) => !d || typeof d.id !== 'string' || !own(d, 'equals'))))
            f.unsupported = true;
        const allowedKeys = [
            'id',
            'semantics',
            'flag',
            'type',
            'default',
            'label',
            'upstreamLabelKey',
            'min',
            'max',
            'choices',
            'flagWhen',
            'format',
            'bareEmpty',
            'maxItems',
            'booleanValue',
            'requires',
            'unsupported',
            'group'
        ];
        if (Object.keys(f).some((k) => !allowedKeys.includes(k)))
            f.unsupported = true;
        if ((f.format && !['domain', 'dc'].includes(f.format)) || (f.bareEmpty && f.id !== 'dcIp'))
            f.unsupported = true;
        if (typeof f.type !== 'string' ||
            f.type.length > 32 ||
            (f.flagWhen !== undefined && typeof f.flagWhen !== 'boolean') ||
            (f.bareEmpty !== undefined && typeof f.bareEmpty !== 'boolean'))
            throw new Error('Invalid TgWs field encoding');
        if (f.type === 'boolean' && !f.booleanValue && (f.flagWhen ?? true) === f.default)
            f.unsupported = true;
        if (f.maxItems !== undefined &&
            (!Number.isSafeInteger(f.maxItems) || f.maxItems < 1 || f.maxItems > 32))
            throw new Error('Invalid TgWs list limit');
        if (f.booleanValue !== undefined && typeof f.booleanValue !== 'boolean')
            throw new Error('Invalid TgWs boolean encoding');
        for (const n of [f.min, f.max])
            if (n !== undefined && (typeof n !== 'number' || !Number.isFinite(n)))
                throw new Error('Invalid TgWs bounds');
        if (f.min !== undefined && f.max !== undefined && f.min > f.max)
            throw new Error('Invalid TgWs bounds');
        if (f.choices &&
            (!Array.isArray(f.choices) ||
                f.choices.length > 64 ||
                f.choices.some((c) => typeof c !== 'string')))
            throw new Error('Invalid TgWs choices');
        if (supportedTgwsField(f, s))
            validateTgwsValue(f, f.default);
    }
    return s;
}
function bundledTgwsCapabilities(version) {
    const entry = bundled[version];
    return entry ? JSON.parse(JSON.stringify(entry)) : null;
}
/** Legacy [] meant defaults in Slipgate, while an explicit new [] means bare --dc-ip. */
function legacyTgwsValues(config) {
    const out = { ...config };
    if (Array.isArray(out.dcIp) && !out.dcIp.length)
        delete out.dcIp;
    for (const [id, toggle] of [
        ['cfproxyUserDomain', 'cfproxyUserDomainEnabled'],
        ['cfproxyWorkerDomain', 'cfproxyWorkerEnabled']
    ]) {
        if (out[toggle] === false)
            delete out[id];
        else if (typeof out[id] === 'string')
            out[id] = String(out[id])
                .split(/[,;\s]+/)
                .filter(Boolean);
    }
    return out;
}
function compileTgwsOptions(schema, legacy, overrides) {
    const args = [], unavailable = [];
    const values = legacyTgwsValues(legacy);
    const dependencyValue = (id) => {
        const field = schema.fields.find((f) => f.id === id);
        if (!field || !supportedTgwsField(field, schema))
            return undefined;
        const saved = own(overrides, id) ? overrides[id] : undefined;
        if (saved)
            return saved.semantics === field.semantics ? saved.value : field.default;
        return field.semantics === 1 && own(values, id) ? values[id] : field.default;
    };
    const knownIds = [
        'dcIp',
        'bufKb',
        'poolSize',
        'verbose',
        'cfproxy',
        'cfproxyPriority',
        'h2',
        'disableSecure',
        'cfproxyUserDomain',
        'cfproxyWorkerDomain',
        'fakeTlsDomain'
    ];
    for (const id of knownIds) {
        if (own(values, id) && !schema.fields.some((f) => f.id === id) && !unavailable.includes(id))
            unavailable.push(id);
    }
    for (const id of Object.keys(overrides))
        if (!schema.fields.some((f) => f.id === id))
            unavailable.push(id);
    for (const f of schema.fields) {
        const entry = own(overrides, f.id) ? overrides[f.id] : undefined;
        const value = entry ? entry.value : own(values, f.id) ? values[f.id] : f.default;
        if (!supportedTgwsField(f, schema) ||
            (entry && entry.semantics !== f.semantics) ||
            (!entry && own(values, f.id) && f.semantics !== 1)) {
            unavailable.push(f.id);
            continue;
        }
        if (f.requires?.some((d) => JSON.stringify(dependencyValue(d.id)) !== JSON.stringify(d.equals))) {
            if (entry || own(values, f.id))
                unavailable.push(f.id);
            continue;
        }
        if (Array.isArray(value) &&
            ((f.id === 'dcIp' && !value.length && !f.bareEmpty) || value.length > (f.maxItems ?? 32))) {
            unavailable.push(f.id);
            continue;
        }
        validateTgwsValue(f, value);
        if (JSON.stringify(value) === JSON.stringify(f.default))
            continue;
        if (f.type === 'boolean') {
            if (f.booleanValue)
                args.push(f.flag, String(value));
            else if (value === (f.flagWhen ?? true))
                args.push(f.flag);
        }
        else if (Array.isArray(value)) {
            if (!value.length && f.bareEmpty)
                args.push(f.flag);
            else
                for (const v of value)
                    args.push(f.flag, v);
        }
        else if (value !== '')
            args.push(f.flag, String(value));
    }
    return { args, unavailable };
}
