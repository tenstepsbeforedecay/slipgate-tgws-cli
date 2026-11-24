const fs = require('node:fs')
const api = require('./manifest-validator.cjs')
const schema = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
api.validateTgwsManifest(schema, schema.version, schema.binarySha256)
for (const field of schema.fields) {
  if (!api.supportedTgwsField(field, schema)) throw new Error('Нужна проверка: ' + field.flag)
}
console.log('Production validator: OK')
