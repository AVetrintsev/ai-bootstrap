Status: needs-info

# 03: Проверить Codex с изолированным исполнителем

**What to build:** На временном проекте проверить сквозную работу Codex по подписке ChatGPT с авторизованным процессом вне песочницы и исполнителем внутри неё. Результат определяет реализацию для портала.

**Blocked by:** Рабочий профиль mihomo для выхода к ChatGPT и источникам установки, административный доступ для применения профиля и установки rootless-рантайма, подтверждение входа владельцем в браузере.

- [ ] На целевых версиях Codex, OpenHands Agent Canvas и Agent Server с ACP воспроизведён вход по подписке с подтверждением в браузере.
- [ ] Из Canvas и терминала проверены запуск shell, чтение и запись файлов рабочей копии и вызов MCP-инструмента. Наблюдаемый результат подтверждает, что операции выполняются в песочнице проекта.
- [ ] Проверено, что из песочницы нельзя получить учётные данные ChatGPT. При отключённом исполнителе команда завершается явной ошибкой, выполнения на VM не происходит.
- [ ] Зафиксированы версии, воспроизводимый сценарий, наблюдаемые результаты и выбранная схема. Если критерий не выполнен, зафиксировано, что блокирует внедрение и какие безопасные варианты проверены.

## Comments

### 2026-11-08 — предварительная проверка

На VM подтверждены SSH по ключу и Ubuntu 26.04.1 LTS на `x86_64`. Codex, Docker и Podman отсутствуют. Попытка создать пользовательское пространство имён через `unshare --user --map-root-user --mount true` завершилась ошибкой записи в `uid_map`. `sudo -n true` потребовал интерактивного входа. HTTPS-запросы с VM к `auth.openai.com`, `chatgpt.com`, `registry.npmjs.org` и `ghcr.io` завершились ошибкой TLS с HTTP-кодом `000`. На VM действует проверочный профиль mihomo из приёмки локального HTTP в задаче № 2 (`7838948`).

Для следующего прогона выбраны Codex `0.166.0`, Agent Canvas `1.37.0`, Agent Server `1.60.1` и `@agentclientprotocol/codex-acp` `2.6.0`. Agent Server взят из конфигурации выпуска Canvas, адаптер объявляет зависимость от Codex `^0.166.0`. На стенде нужно закрепить и записать фактически использованный бинарник Codex, поскольку диапазон зависимости допускает более новую версию. [Конфигурация выпуска Canvas](https://github.com/OpenHands/OpenHands/blob/v1.37.0/config/defaults.json) · [Манифест адаптера](https://github.com/agentclientprotocol/codex-acp/blob/v2.6.0/package.json)

#### Что воспроизвелось локально

Проверка прошла на Windows, Codex `0.166.0` и Node.js `24.19.0`. Для app-server и exec-server использованы разные пустые каталоги `CODEX_HOME`, временная рабочая копия и отдельный loopback-порт. Для app-server заданы `OPENAI_BASE_URL=http://127.0.0.1:1/v1`, `CODEX_BASE_URL=http://127.0.0.1:1` и фиктивный `OPENAI_API_KEY=local-probe-placeholder`. Вход и успешные запросы к модели в этот прогон не входили.

1. В рабочей копии создать `marker.txt` с содержимым `project-probe`. Запустить `codex exec-server --listen ws://127.0.0.1:<порт>` из этой копии. Подключиться по WebSocket, отправить `initialize` с `clientName`, затем уведомление `initialized`.
2. Вызвать `fs/readFile` для `marker.txt` и `fs/writeFile` для нового `output.txt`, используя абсолютные `file:` URI и `dataBase64` для записи. Оба файла содержали `project-probe`. Через `process/start` запустить `cmd.exe /d /c "type marker.txt"` с `cwd` рабочей копии. Исполнитель вернул маркер и событие `process/exited` с кодом `0`. [Протокол exec-server 0.166.0](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/exec-server/README.md)
3. В каталоге app-server создать `environments.toml` со следующим содержимым, подставив порт.

   ```toml
   default = "project"
   include_local = false

   [[environments]]
   id = "project"
   url = "ws://127.0.0.1:<порт>"
   connect_timeout_sec = 2
   ```

4. В `config.toml` добавить `[mcp_servers.probe]` с командой запуска тестового stdio MCP-сервера, `environment_id = "project"` и явным абсолютным `cwd` рабочей копии. Инструмент `marker` должен создать `mcp-called.txt` и вернуть содержимое `marker.txt`. Запустить `codex app-server --listen stdio:// --strict-config`, выполнить `initialize` с `capabilities.experimentalApi = true`, затем `initialized` и `thread/start` с `cwd` рабочей копии. Запрос `mcpServer/tool/call` с полученным `threadId`, `server = "probe"` и `tool = "marker"` вернул `project-probe`. При первом запуске без явного `cwd` Codex вернул `executor stdio server requires an explicit cwd`. [Конфигурация среды в Codex 0.166.0](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/exec-server/src/environment_toml.rs) · [Настройка среды для MCP](https://github.com/openai/codex/issues/51481)
5. Удалить `mcp-called.txt`, остановить исполнитель и повторить вызов с тем же `threadId`. После 25 секунд попыток восстановления получена ошибка `-32603` с сообщением `Transport closed`. Тестовый файл не появился.

Для воспроизведения шагов 4 и 5 сохранить следующий минимальный MCP-сервер как `probe.mjs` в рабочей копии. В `[mcp_servers.probe]` задать `command` абсолютным путём к Node.js, а `args` массивом из абсолютного пути к файлу.

```js
import { readFileSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

for await (const line of createInterface({ input: process.stdin })) {
    const { id, method, params } = JSON.parse(line);
    if (id === undefined) continue;

    let result;
    switch (method) {
        case 'initialize':
            result = {
                protocolVersion: params.protocolVersion,
                capabilities: { tools: {} },
                serverInfo: { name: 'probe', version: '1' },
            };
            break;
        case 'tools/list':
            result = { tools: [{ name: 'marker', inputSchema: { type: 'object' } }] };
            break;
        case 'tools/call':
            writeFileSync('mcp-called.txt', 'called');
            result = { content: [{ type: 'text', text: readFileSync('marker.txt', 'utf8') }] };
            break;
        default:
            console.log(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Unknown method' } }));
            continue;
    }
    console.log(JSON.stringify({ jsonrpc: '2.0', id, result }));
}
```

Этот прогон подтвердил транспорт, файловые операции, запуск shell через протокол исполнителя и отказ отдельного MCP-вызова. Оба процесса Windows работали под одним пользователем. Граница изоляции, отсутствие запуска shell на VM при отказе и маршрутизация инструментов в ходе модельной сессии остаются критериями приёмки на Ubuntu.

#### Сценарий приёмки на VM

1. Подключить рабочий профиль mihomo с явными маршрутами для установки компонентов и входа Codex. Проверить исходящий HTTPS с VM и установить rootless-рантайм. Сохранить запрет на непредусмотренный исходящий трафик.
2. Запустить на VM доверенный процесс Codex под отдельной непривилегированной учётной записью. Выполнить `codex login --device-auth` и подтвердить вход в браузере владельца. Проверить `codex login status` и сохранить только тип входа. Файлы авторизации хранить в закрытом каталоге доверенного процесса. [Вход Codex на сервере](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
3. Запустить `exec-server` в контейнере временного проекта. Монтировать рабочую копию и нужные инструменты. Исключить из окружения контейнера каталог авторизации, переменные с токенами, сокеты Docker и SSH-агента, а также доступ к управляющим портам. Подключить исполнитель через закрытый WebSocket, настроить его единственной средой с `include_local = false`. Размещать ACP-адаптер и авторизованный app-server на доверенной стороне. Стандартный пример OpenHands сохраняет `CODEX_AUTH_JSON` там, где запускается адаптер, поэтому запуск такого Agent Server внутри песочницы проекта нарушил бы границу. [Хранение учётных данных в OpenHands 1.37.0](https://github.com/OpenHands/OpenHands/blob/v1.37.0/docs/ACP_AGENTS.md#L164-L173)
4. Из Canvas через ACP и из терминала в песочнице выполнить одну и ту же задачу с shell, чтением и записью рабочей копии и вызовом stdio MCP. Использовать разные маркеры на VM и в контейнере, чтобы различать место выполнения. Отдельно проверить действия, которые ACP-клиент делегирует Agent Server. Проверить Codex TUI с `--remote` через посредник, ограничивающий запросы сессией и средой проекта. Держать полный API доверенного app-server недоступным для процессов контейнера. Проверить, что авторизация остаётся на доверенной стороне и запросы на операции с файлами или процессами VM отклоняются. [Удалённый режим Codex CLI](https://learn.chatgpt.com/docs/cli/reference) · [Файловые RPC app-server 0.166.0](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server/src/request_processors/fs_processor.rs#L50-L86)
5. В песочнице проверить недоступность синтетического файла-маркера в каталоге авторизации через shell, доступные API и делегированные файловые операции. Проверить отсутствие доступа к самому каталогу и отсутствие переменных с учётными данными. Не читать и не записывать в журнал значения реальных токенов. Затем остановить исполнитель и повторить операции из обеих точек входа в открытых и новых сессиях. Зафиксировать ошибки и убедиться, что маркеры shell, записи и MCP на VM остались прежними.

Кандидат для портала — доверенный процесс Codex с подпиской и отдельный исполнитель на проект. Внедрение зависит от подтверждения обоих клиентских путей и отказа без перехода на выполнение на VM.
