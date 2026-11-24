# Автоматическая сборка CLI

Один существующий `.github/workflows/build-cli.yml` репозитория `tenstepsbeforedecay/slipgate-tgws-cli` отвечает за проверку и выпуск. Для его сопровождения нужны права записи содержимого и workflow.

## Установка

В подготовленном `mirror-patch` пути уже соответствуют зеркалу. Перенесите `build-cli.yml` в `.github/workflows/`, а следующие файлы в `scripts/`: `release.py`, `build-ci.py`, `generate-capabilities.py`, `generate-routing.py`, `make-routing-contracts.py`, `routing-contracts.json`, `capabilities-overlay.json`, `manifest-validator.cjs`, `validate-manifest.cjs`, `test_release.py`. Этот документ переносится в `docs/cli-release.md`.

Откройте draft PR, проверьте diff и объедините после ревью. Сам push ветки/PR не запускает сборку и не публикует release. Секреты не добавляются. `contents: write` нужен доверенным задачам `resolve` и `publish`: GitHub скрывает drafts в List releases от токена без push access, поэтому одного `contents: read` недостаточно для надёжного восстановления. `actions: read` нужен только чтению сохранённого артефакта при публикации. Сборка upstream получает только `contents: read` и не получает write-token. Уведомления в Telegram/issues удалены: это не обязательная часть сборки и требует лишних разрешений/секретов; причина сбоя остаётся в summary и artifact.

Расписание раз в два часа получает стабильный latest upstream, разрешает annotated/lightweight tag до точного commit и пропускает существующий опубликованный release. Ручной запуск допускается только из default branch. Поля `force`, `--clobber`, замены опубликованных exe/sha отсутствуют. Первый scheduled run при существующем `v1.12.0` ничего не пересобирает.

## Проверка без публикации

После разрешённого merge откройте существующий `build-cli` в Actions, выберите default branch и ручной режим `mode=validate` (значение по умолчанию); `tag` — стабильный upstream tag либо пусто для latest. Запуск из другой ветки и неизвестный mode не допускают выполнения задач. Push/PR сборку не запускает; поэтому порядок внедрения: локальные проверки → PR/review → merge без обхода policies → ручной validate → проверка Windows logs и artifact. До получения успешных logs реальная CI-сборка не считается проверенной.

Отдельный `validate-resolve` имеет только `contents: read`, делает GET запросы к upstream и разрешает tag до exact commit, не обращаясь к releases зеркала. Даже уже опубликованный tag собирается для проверки. Нормальный resolver с write-token и задача publish пропускаются в этом режиме. Дополнительно `release.py` запрещает release-фазы при `RUN_MODE=validate`, а validation API отклоняет любые методы кроме GET до сети.

Общая задача `build` на `windows-2025` без write-token выполняет настоящий PyInstaller build, `--help`, bind-smoke с завершением собственного дерева PID, проверку PE x64, schema identity/digest и production manifest validator через `build-ci.py`. Unknown semantics остаются блокирующей ошибкой. Результат сохраняется только как Actions artifact `cli-validation` (30 дней), не как release asset. Этот artifact не имеет имени `cli-staging` и не используется восстановлением публикации. Ошибка оставляет диагностику `review-required`; release/tag/notes/assets не меняются. Скачайте artifact при необходимости, проверьте успешность всех Windows steps и неизменность списка release assets до/после validation.

Для ручного штатного выпуска требуется явно выбрать `mode=release`; расписание продолжает штатный выпуск без ручных inputs и сохраняет published-release no-op. Не используйте release вместо validate для демонстрации сборки. Наличие validate в локальном комплекте не означает, что он уже внедрён или запускался в GitHub.

Для переноса изменений нужны права Contents/Workflows/Actions write на `tenstepsbeforedecay/slipgate-tgws-cli`. Не передавайте токены в чат.

## Совместимость старых Slipgate

Имя `TgWsProxy_windows.exe`, архитектура x64, консольный вход `proxy.tg_ws_proxy.main`, флаги запуска, 32-hex secret и теги сохранены. Исторический updater сначала выбирает asset точным регулярным выражением `^TgWsProxy_windows\.exe$`; дополнительные JSON не участвуют в выборе. Старые assets не скачиваются для замены и никогда не изменяются. Старые версии продолжают получать один прежний exe; новый updater дополнительно получает `capabilities.json` с совпадающим SHA256. Исторические releases без schema остаются доступными.

Новый release становится публичным только после проверки полного набора `exe`, `capabilities.json`, `routing.json`, `build-info.json`: PE x64, manifest identity/defaults через снимок production validator, SHA256 каждого загруженного asset из API и независимого скачивания. До этого он draft и не виден анонимному updater. Ошибка проверки оставляет предыдущий публичный release рабочим.

## Восстановление

Перед draft сохраняется неизменяемый artifact `cli-staging` (30 дней). В draft notes записаны repository, exact source commit/tag, commit workflow, run ID и digest/size каждого файла. Автор должен быть `github-actions[bot]`. После потери ответа POST повторный запуск перечитывает состояние, не повторяет запись вслепую. Пустой `starter` разрешено удалять только внутри собственного draft с точным marker и исходным набором файлов. Завершённые assets не перезаписываются.

Повторите весь workflow с тем же tag на том же commit default branch либо повторите исходный run. Resolver найдёт собственный draft, прочитает run ID и возьмёт оригинальный `cli-staging`, не пересобирая exe. Неполные загрузки дозагружаются; полный digest readback повторяется перед publish. Не меняйте notes draft вручную. Другой автор, изменённые notes/source/workflow, чужие assets, несовпадающие bytes либо истёкший artifact останавливают восстановление для ручного разбора владельцем. Автоматического удаления релизов/тегов или force-rebuild нет. После изменения default branch сначала безопаснее восстановить исходный run; новый commit не присваивает себе старый draft.

## Будущие версии и границы

AST извлекает literal bool/int/float/string/enum/list и локализацию из exact source без импорта/исполнения upstream для extraction. Простые новые параметры автоматизируются; неизвестный type, вычисляемый default, динамические kwargs, преобразование нового значения после argparse, изменившаяся проверенная семантика либо unsafe option блокируют выпуск и дают `review-required.txt`. Reviewed overlay сохраняет смысл сложных DC/доменов/зависимостей. Все будущие протоколы без изменений поддержать невозможно.

`routing.json` автоматически извлекается как дополнительный проверяемый asset для ревью/следующего обновления Slipgate. Текущий production `tgwsRoutingForSchema` читает ТОЛЬКО baked matrix с совпадением digest/commit: **он не скачивает этот asset и диагностика серверов новой версии автоматически не появится**. Сетевые маршруты самого exe остаются родными upstream и не заменяются матрицей. Доставка динамической диагностики требует отдельного изменения production, в этот patch не включена.

Python 3.13.17 x64, pip 25.3, packaging 25.0, PyInstaller 6.16.0 и certifi 2026.2.25 зафиксированы; SHA Actions проверены через официальный GitHub API. Зависимости upstream читаются из requirements/pyproject без смены их ограничений; используются только wheels, URL dependencies останавливают сборку. Конфликт pin с upstream прекращает release. Transitive dependencies upstream разрешаются pip в рамках upstream constraints и фиксируются в `build-info.json` (freeze/report): это аудит и повторное использование исходного staging artifact, **не обещание побитово воспроизводимой пересборки**.

GitHub/загрузки ограничены тайм-аутами/размерами, GET — пятью попытками; rate-limit соблюдает Retry-After/reset и прекращает попытки при ожидании сверх бюджета. Публикация сериализована concurrency; после неоднозначного ответа мутаций состояние сверяется заново. Сбои GitHub, upstream, антивирусов или сети не исключены; безопасное поведение — не публиковать непроверенное обновление.

## Проверки

В зеркале: `python -m unittest discover -s scripts -p test_release.py -v`, `actionlint .github/workflows/build-cli.yml`. Сборка/`--help`/портовый smoke выполняются только на изолированном Windows CI; helper отказывает локальному запуску. Снимок `manifest-validator.cjs` получен из текущего `src/shared/tgws-capabilities.ts` без встроенных matrices, которые не нужны при проверке нового manifest; при изменении production контракта обновите снимок и тесты вместе.

Первоисточники: [права и drafts REST](https://docs.github.com/en/rest/releases/releases), [защита Actions](https://docs.github.com/en/actions/reference/security/secure-use), [rate limit/retries](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).
