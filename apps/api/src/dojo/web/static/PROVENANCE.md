# Происхождение вендоренных ассетов

Файлы в этом каталоге взяты из апстрима без изменений и лежат в репозитории
намеренно: штатная страница `/docs` FastAPI подтягивает их с `cdn.jsdelivr.net`,
из-за чего документация API не открывается без интернета. Критерий приёмки №2
требует полной работы офлайн после первичной установки.

| Файл | Источник | Версия |
|---|---|---|
| `swagger-ui-bundle.js` | https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js | 5.18.2 |
| `swagger-ui.css` | https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css | 5.18.2 |
| `htmx.min.js` | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | 2.0.4 |

Контрольные суммы SHA-256:

```
c50b94bbc4f02394326fb7aed1f4fb693b3677f4b3d3344e0d6131808cbf281f  swagger-ui-bundle.js
8f33d996025317049d4a9864f421eab2b2a247872f388026fa94c654913259e7  swagger-ui.css
e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447  htmx.min.js
```

Проверить:

```bash
sha256sum apps/api/src/dojo/web/static/swagger-ui-*
```

Каталог исключён из всех pre-commit хуков (верхнеуровневый `exclude` в
`.pre-commit-config.yaml`): файлы должны совпадать с первоисточником побайтно,
иначе сверка по контрольной сумме теряет смысл. `end-of-file-fixer` уже
однажды дописал им перевод строки.

## Обновление версии

```bash
VER=<новая версия>
for f in swagger-ui-bundle.js swagger-ui.css; do
  curl -fsSL "https://cdn.jsdelivr.net/npm/swagger-ui-dist@$VER/$f" \
    -o "apps/api/src/dojo/web/static/$f"
done
sha256sum apps/api/src/dojo/web/static/swagger-ui-*   # обновить таблицу выше
make test-int                                          # проверить, что /docs жив
```
