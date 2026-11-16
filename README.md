# sand3d-mcp

MCP-сервер для открытого каталога 3D-объектов [Sand3D](https://huggingface.co/spaces/Sandflow/sand3d-explorer) от Sandflow. В каталоге около 100 тысяч объектов. У каждого есть GLB, рендер и короткая Python-программа, которая этот объект строит. Лицензия CC BY 4.0.

Сервер подключается к любому MCP-клиенту (Claude Code, Claude Desktop, Cursor и другие). Модель ищет объект по описанию, выбирает его глазами по контактному листу с рендерами и скачивает в папку проекта.

Сервер неофициальный и с Sandflow не связан.

## Что умеет

| Инструмент | Что делает |
|---|---|
| `search_objects` | Поиск по смыслу (английское описание сравнивается с рендерами) или по словам. Фильтры по датасету, семейству, шарнирам. Возвращает список и контактный лист с номерами |
| `find_similar` | Похожие по виду на выбранный объект, тоже с листом |
| `get_object` | Карточка объекта с лицензией, атрибуцией, ссылкой на страницу и его программа |
| `download_object` | Скачивает `model.glb`, `program.py`, `render.png`, `meta.json` в указанную папку. По желанию `model.usda` и помощник `s3d-joints.js` для three.js |
| `list_datasets` | Датасеты и семейства со счетчиками |
| `sand3d_format_guide` | Справка по устройству GLB и шарнирам. То же доступно ресурсом `sand3d://guide` |

Все инструменты только читают каталог. Пишет на диск один `download_object`, и только в папку, которую назвали в запросе.

## Установка

Нужен [uv](https://docs.astral.sh/uv/) или Python 3.10 и новее. Отдельно ставить ничего не нужно, `uvx` сам соберет окружение при первом запуске.

Проверка, что сервер запускается.

```bash
uvx --from git+https://github.com/siliconbag/sand3d-mcp sand3d-mcp
```

Сервер молча ждет команд на stdin, выход по Ctrl+C. Из локальной копии репозитория запуск такой.

```bash
uvx --from /path/to/sand3d-mcp sand3d-mcp
# или
pip install /path/to/sand3d-mcp && sand3d-mcp
```

## Подключение

### Claude Code

```bash
claude mcp add sand3d -- uvx --from git+https://github.com/siliconbag/sand3d-mcp sand3d-mcp
```

Флаг `--scope user` подключит сервер во всех проектах. Относительные пути в `download_object` считаются от папки, где запущен Claude Code.

### Claude Desktop

Файл `claude_desktop_config.json` (Settings, Developer, Edit Config).

```json
{
  "mcpServers": {
    "sand3d": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/siliconbag/sand3d-mcp", "sand3d-mcp"]
    }
  }
}
```

Если Claude Desktop не находит `uvx`, укажи полный путь (его покажет `which uvx`). Рабочая папка у Desktop не определена, поэтому для скачивания называй абсолютный путь.

### Cursor

Файл `.cursor/mcp.json` в проекте или `~/.cursor/mcp.json` для всех проектов.

```json
{
  "mcpServers": {
    "sand3d": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/siliconbag/sand3d-mcp", "sand3d-mcp"]
    }
  }
}
```

## Примеры запросов к модели

- «Найди в Sand3D старый красный трактор и скачай лучший вариант в `assets/sand3d`»
- «Нужна настольная лампа с подвижными частями для three.js сцены. Положи модель и помощник для шарниров в `public/models`»
- «Покажи, какие семейства есть в датасете simready»
- «Найди что-нибудь похожее на этот объект, но в другом стиле»
- «Открой программу этого объекта и скажи, какие у него параметры»

Смысловой поиск понимает английский. Модель сама переводит запрос, но точнее всего работает описание внешнего вида («wooden rocking chair», «yellow forklift»).

## Что лежит в папке после скачивания

- `model.glb`, объект в метрах, ось Y вверх, корень со scale 0.001
- `program.py`, программа на библиотеке sand3d, которая строит объект
- `render.png`, рендер 512x512 на прозрачном фоне
- `meta.json`, имя, датасет, семейство, лицензии, строка атрибуции, габариты, детали и шарниры
- `model.usda`, если попросили или если у объекта нет GLB
- `s3d-joints.js`, если попросили помощник для three.js (`collectJoints`, `setJoint`, `setJoint01`, `fitToHeight`)

## Лицензии

Каталог Sand3D выпущен Sandflow под CC BY 4.0. У исходных моделей свои лицензии, они записаны в `meta.json` в полях `source_license` и `license_status`. При публикации ролика, сайта или игры с объектом указывай строку `attribution` из `meta.json`.

Код этого сервера распространяется по лицензии MIT, см. [LICENSE](LICENSE).

## Бережно к чужому сервису

Сервер ходит в публичный API спейса Sandflow, который работает на одном CPU. Десятки объектов это нормально. Для массовой выгрузки бери открытые датасеты `Sandflow/sand3d-objaverse`, `-cad`, `-articraft`, `-simready`, `-automate` на Hugging Face (parquet с программами, рендерами и USDA). Потолок выдачи 200 объектов за запрос, по умолчанию 12. Контактный лист рисует не больше 24 рендеров.

## Ограничения

- Все зависит от спейса Sandflow. Если его остановят или поменяют API, сервер перестанет работать.
- Готовые GLB отдает только спейс, в открытых датасетах их нет.
- Если спейс уснул, первый запрос ждет загрузки каталога до 4 минут. Некоторые клиенты обрывают вызов раньше, тогда просто повтори его через минуту. Время ожидания задается переменной `SAND3D_WAKE_SECONDS`.
- Соединение со спейсом иногда рвется сериями по несколько секунд. Сервер повторяет запрос примерно 40 секунд, потом возвращает понятную ошибку.
- Пересборка объекта из `program.py` требует пакета `sand3d` и Blender (`bpy`), сервер ее не делает.

## English

Unofficial MCP server (stdio) for the open [Sand3D](https://huggingface.co/spaces/Sandflow/sand3d-explorer) 3D object catalog by Sandflow (about 100k objects with GLB, render and generating Python program, CC BY 4.0). Tools: `search_objects` (meaning or keyword search with a numbered contact sheet image), `find_similar`, `get_object`, `download_object`, `list_datasets`, `sand3d_format_guide`.

```bash
# Claude Code
claude mcp add sand3d -- uvx --from git+https://github.com/siliconbag/sand3d-mcp sand3d-mcp
```

Claude Desktop or Cursor config:

```json
{ "mcpServers": { "sand3d": { "command": "uvx", "args": ["--from", "git+https://github.com/siliconbag/sand3d-mcp", "sand3d-mcp"] } } }
```

Please be gentle with the service, it is a single-CPU space: dozens of objects are fine, use the Hugging Face datasets `Sandflow/sand3d-*` for bulk work. Credit the `attribution` string from `meta.json` when you publish. Code is MIT licensed.
