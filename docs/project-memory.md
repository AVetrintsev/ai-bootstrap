# Память проекта

Codex и Qwen используют MCP-сервер `graphiti` из Canvas и терминала.
Он предоставляет `add_memory`, `get_episodes`, `get_episode`,
`search_memory_facts` и `delete_episode`. Запись становится доступна другим
клиентам после завершения `add_memory`; успешный ответ означает сохранение на диск.

В защищённый YAML конфигурации добавьте:

```yaml
project_memory:
  model: qwen3.8-flash
  embedding_model: openai/text-embedding-3-small
  embedding_dimensions: 1536
```

`model` — имя уже настроенной модели в `model_gateway.models`. Graphiti
использует её провайдера и ключ на доверенной стороне VM. Провайдер должен
поддерживать OpenAI-совместимые chat completions с JSON Schema и embeddings;
пример рассчитан на существующий OpenRouter. Embeddings используются для
поиска по смыслу. Эти обращения оплачиваются через API провайдера и не входят
в подписку Codex или квоту запросов агента в шлюзе.

Примените обычную команду `sudo ./install.sh --config /etc/ai-bootstrap/config.yaml`.
При отсутствии секции память отключена. Установщик сохраняет имеющиеся данные
при обновлении и отключении; удаление проекта через платформу удаляет его память.
Смена модели embeddings или размерности для уже заполненной памяти требует
отдельной миграции; не меняйте эти поля при обычной повторной установке.

Для каждого проекта доверенная служба `ai-bootstrap-memory@PROJECT.service`
запускает Graphiti 0.31.2 и отдельный FalkorDB Lite 0.11.0.
Снимок базы находится в `/var/lib/ai-memory/PROJECT/memory.rdb`.
MCP-сокет в `/run/ai-bootstrap-git/PROJECT/memory.sock` доступен только учётной
записи соответствующей песочницы. Служба проверяет UID соединения и определяет
проект из своего имени, а не из аргументов инструмента. Неизвестные поля,
включая `group_id`, `group_ids` и `project_id`, отклоняются. Доступ по UUID
также ограничен отдельной базой проекта.

FalkorDB слушает только закрытый Unix-сокет на VM, без TCP-порта. Каталог базы
и служебный ключ не монтируются в контейнер. Ключ поступает службе через
systemd `LoadCredential`; при повторной установке обновляется его копия из
действующего ключа провайдера. Ошибки API не возвращают агенту служебные данные.

Запись ограничена 4000 символами, выборка — 20 результатами, обработка —
240 секундами. Общая память между проектами и подтверждение публикации владельцем
относятся к задаче №13 и здесь не включены.

## Проверки на VM

Автоматические проверки используют настоящий Graphiti/FalkorDB с тестовым
HTTP-провайдером моделей. Нужны зависимости `/opt/ai-memory` и MCP SDK для
проверки совместимости протокола. Пример отдельного окружения:

```sh
python3 -m venv --system-site-packages /tmp/memory-tests
/tmp/memory-tests/bin/pip install 'graphiti-core[falkordblite]==0.31.2' \
  falkordblite==0.11.0 jsonschema==4.26.0 mcp==1.26.0
GRAPHITI_TELEMETRY_ENABLED=false /tmp/memory-tests/bin/python -m unittest discover -s tests -v
```

Приёмка установленной платформы с двумя существующими проектами:

```sh
sudo python3 tests/accept_project_memory.py gh-FIRST gh-SECOND ./install.sh
sudo python3 tests/accept_project_agents.py gh-FIRST codex memory
sudo python3 tests/accept_project_agents.py gh-FIRST qwen memory
sudo BROWSER_PYTHON tests/accept_canvas.py gh-FIRST CHROMIUM codex memory
sudo BROWSER_PYTHON tests/accept_canvas.py gh-FIRST CHROMIUM qwen memory
```

Первый сценарий проверяет обмен памятью, поиск фактов, подмену проекта и UUID,
перезапуск и повторную установку, недоступность ключа и прямого TCP-подключения
к FalkorDB. Терминальные сценарии проверяют завершённые вызовы одного агента
на запись и другого на чтение. Canvas проверяет завершённые MCP-вызовы в событиях
браузера и записанную память независимым клиентом. Временные записи удаляются.

Основание: [Graphiti MCP](https://help.getzep.com/graphiti/getting-started/mcp-server),
[FalkorDB Lite](https://github.com/FalkorDB/falkordblite),
[embeddings OpenRouter](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings).
