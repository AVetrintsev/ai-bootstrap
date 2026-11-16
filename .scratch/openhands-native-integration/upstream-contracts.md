# Закреплённые upstream-контракты для внедрения

Исследовано 2026-11-16 по официальным исходникам. Это инструкция для реализации, не отчёт об успешной интеграционной проверке: тесты здесь не запускались. Все runtime-проверки выполняет основной агент только на целевой Linux VM.

## Версии и решение

| Компонент | Закреплённая версия / commit | Назначение |
|---|---|---|
| Codex ACP | `@agentclientprotocol/codex-acp@2.6.0`, `f3208913693bd4d5171d6c6b9be5ec1ff96c6431` | Готовый переводчик ACP ↔ app-server |
| Codex | `0.166.0`, `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` | Уже закреплённый движок и remote executor |
| SDK / Agent Server | `1.62.0`, `504a88cab0fb48ee5226238ad7b8f1d865fa84a8` | Уже закреплённый ACP client, profiles, worktrees |
| Canvas | `1.39.0`, `5aed815f5384c48bccacf1e40a8619653d488ad0` | Уже закреплённый штатный frontend |
| Qwen Code | `0.27.2`, `f42fe0f3251172c1174d00382ece522eafb3c6c2` | Уже закреплённый ACP engine |

Codex ACP 2.6.0 объявляет зависимость `@openai/codex:^0.166.0`; старый SDK install preset 2.2.2 объявлял `^0.160.1`. Для внешнего доверенного сервиса нужен прямой pin 2.6.0 и `CODEX_PATH`, а не установка через устаревший preset. Это соответствие версий исходников, не доказательство runtime-совместимости remote environment. [package.json](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/package.json), [SDK install catalog](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-sdk/openhands/sdk/settings/acp_install_catalog.py), [Codex tag](https://github.com/openai/codex/releases/tag/rust-v0.166.0).

## Запуск и граница доверия Codex

Запускать установленный `codex-acp` без аргументов, stdio — newline JSON-RPC. Адаптер сам запускает app-server. Управляемый environment:

```text
CODEX_PATH=/opt/ai-codex/bin/codex
CODEX_HOME=/var/lib/ai-codex/projects/gh-N/chatgpt
HOME=<доверенный home сервиса>
CODEX_CONFIG={"developer_instructions":""}
```

Для API-профиля остаётся отдельный host home и управляемая конфигурация провайдера. Не наследовать environment ACP-клиента. Не задавать `DEFAULT_AUTH_REQUEST`, `APP_SERVER_LOGS`, непроверенный `MODEL_PROVIDER`, `CODEX_CONFIG` из клиента. `CODEX_PATH` исключает незаметный выбор bundled Codex. Адаптер наследует свой environment при spawn; конфигурация JSON применяется к сессиям, а `hooks` из неё выделяется в startup `app-server -c hooks=…`. [entrypoint](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/index.ts), [spawn](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexJsonRpcConnection.ts), [hook config](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexHookConfig.ts), [logger](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/Logger.ts).

Сохранить `environments.toml` с `default="project"`, `include_local=false`, управляемым executor socket. `newSession` передаёт app-server `thread/start` с `cwd`, `config`, `modelProvider` и при необходимости `developerInstructions`; собственного environment override адаптер туда не добавляет. Поэтому он совместим с default-selection по конструкции, но доказательством служит проверка на VM: shell/edit в контейнере и отказ при потере executor. Не монтировать auth home внутрь контейнера ради удобства адаптера. [thread start/resume/config](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexAcpClient.ts).

### Тонкий фильтр, без второго переводчика

Прокси остаётся двунаправленной передачей JSON-RPC, включая response IDs, server requests, cancel и notifications. Не переводить инструменты, изображения, план, reasoning, history, token usage или ошибки app-server заново. Контроль сосредоточить на внешних входах и минимальной совместимости model picker:

- Разрешать только реализуемые операции ACP, проверять session ownership и project/profile. Для `session/new/load/resume/fork/list` проверять cwd; sessionId возвращённого new/fork сохранять в доверенном реестре. Старые сессии мигрировать из доверенного проектного хранилища. `session/list/batch_get/subscribe` не должны обходить фильтр принадлежности.
- Запретить `authenticate`, `logout`, `authentication/logout`, `auth/login`, `auth/logout`, `providers/*`, `_codex/hooks/*`, произвольные расширения и client config. В init-ответе не рекламировать запрещённые возможности. Список зарегистрированных методов берётся из upstream router, а не угадывается по старому adapter API.
- Отвергать непустые входящие `mcpServers`, произвольные `additionalDirectories`, `_meta` с AIR additional roots/custom instructions и чужой `systemPrompt`; доверенные MCP задаются host config с `environment_id="project"`.
- Не доверять только RPC denylist: адаптер обрабатывает `/logout` внутри `session/prompt`, а `/mcp reconnect` может запустить OAuth. Разбор: только первый text block, `trim()`, `/`, затем `trim()`, первый whitespace-delimited token lower-case. Блокировать соответствующие команды и не показывать их в available commands. Обычный `/mcp` — чтение статуса; допустимость решается отдельно.
- Проверять только безопасный `session/set_config_option` allowlist (если нужен); modes не должны изменять trust boundary, provider, host execution или billing. Server permission requests должны сохранять прежнюю политику проекта.
- Никакого скрытого перехода ChatGPT → API при истечении подписки. Перед стартом/возобновлением и после ошибки проверять установленный режим учётной записи прежним trusted control plane.

Источники: [полный router v1/v2](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/AcpAgentRouter.ts), [slash commands](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexCommands.ts), [MCP reconnect/OAuth](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/mcp/McpCommand.ts), [AIR metadata](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/AirExtension.ts).

### Очистка прежних общих правил при resume

Одного удаления `developer_instructions` из файла недостаточно для сохранённого thread. В доверенном environment явно задать `CODEX_CONFIG={"developer_instructions":""}`. В **top-level params** каждого `session/new`, `session/load`, `session/resume`, `session/fork`, после удаления клиентского `_meta/systemPrompt`, вставлять:

```json
{
  "cwd": "/workspace/repo",
  "mcpServers": [],
  "systemPrompt": {
    "mode": "append",
    "content": [{"type": "text", "text": ""}]
  }
}
```

Для load/resume/fork здесь дополнительно остаётся проверенный `sessionId`; cwd может быть разрешённым worktree. Это **не** `_meta`. Массив content должен иметь один text block: пустой массив отвергается. Пустая строка допустима. `resolveSessionInstructions` использует пустую строку из config как baseline; resume/load/fork включают `developerInstructions:""`, потому что проверяют наличие `instructions.text`, а не truthiness. Так старые platform rules заменяются явно без изменения upstream adapter. Проверить на реальном сохранённом thread. [system prompt contract](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/ClientSystemPrompt.ts), [resolution and resume](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexAcpClient.ts), [fork](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/SessionFork.ts).

### Файлы, hooks, клиентские callbacks

- `resource_link` превращается в текстовую ссылку; adapter не читает соответствующий host file. Embedded resource text/blob остаётся inline. Для image adapter принимает URI с `http:`, `https:`, `data:`; `file:` не используется, берётся inline data. Если не нужен host-side URL fetch, фильтр принимает только проверенные inline изображения/data, отвергая внешние image URI. Это не требует собственного image translator. [buildPromptItems](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexAcpClient.ts).
- `SessionIndexWorktrees` читает host `.git` для агрегации сессий соседних worktree, но при отсутствующем cwd возвращает `[cwd]`. Для обычного prompt этот механизм не нужен; не создавать host mirror проекта ради него. AIR file-change reporting делает host canonicalization; не включать AIR capabilities/metadata. История/имена/watchers используют доверенный `CODEX_HOME`. [worktree index](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/SessionIndexWorktrees.ts), [AIR file report](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/AgentFileChangeReport.ts), [home watcher](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexHomeWatcher.ts).
- **Hooks требуют отдельного ограничения.** В Codex 0.166.0 local hooks выполняются на host; executor hooks — отдельный путь. `include_local=false` само по себе не обещает запрета local hooks. Не разрешать client hooks/trust, не помещать hooks в trusted config, проверить отсутствие host выполнения repository hook на VM. [host hook cwd](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/core/src/hook_runtime.rs#L263), [hook trust API адаптера](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexHookTrust.ts).
- `configRead`, `listPermissionProfiles`, `skillsList` вызываются через app-server с cwd. Их работоспособность при удалённом cwd нельзя вывести из Node fs-проверок. Приёмка нужна на установленном remote executor. Клиентские fs/terminal callbacks, если adapter их запросит, направляются в sandbox ACP client; host proxy не реализует для них filesystem API. Не подменять это host чтением.

## Модель и reasoning без собственного Canvas UI

Codex ACP 2.6.0 уже формирует legacy `models.availableModels` как декартово произведение реального `model/list` и `supportedReasoningEfforts`: ID `model[effort]`, label с effort. Пример синтаксиса `gpt-5.2[high]`, но каталог не хардкодить. Одновременно он отдаёт `configOptions` с base model и отдельным thought_level. [ModelId](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/ModelId.ts), [createModelState](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/CodexAcpServer.ts#L3507), [config options](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/ModelConfigOption.ts).

SDK 1.62 сначала выбирает `configOptions.id=="model"`; если его нет, использует legacy `models` с `via_config_option=False`. В этом режиме `_apply_acp_model` передаёт ID как есть в `session/set_model`, а встроенный `_ACPClientConnection` сохраняет этот RPC даже после удаления его из ACP Python library. Родной Codex router регистрирует данный legacy метод. Значит выбранная совместимость: удалить только `configOptions` entries с `id=="model"` **или** `category=="model"` **или** `category=="thought_level"` (дополнительно `id=="reasoning_effort"`), сохранив исходный upstream `models`; повторять для session setup responses и `config_options_update`, чтобы механизм не переключался посреди сессии. Не генерировать aliases и не разбирать model[effort] самим. [SDK выбор механизма](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-sdk/openhands/sdk/agent/acp_contracts.py#L296), [SDK apply/legacy RPC](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-sdk/openhands/sdk/agent/acp_agent.py#L667), [Codex router](https://github.com/agentclientprotocol/codex-acp/blob/f3208913693bd4d5171d6c6b9be5ec1ff96c6431/src/AcpAgentRouter.ts).

Обычные Agent Profiles поддерживают `acp_model` и `acp_effort`. Canvas ACP save form 1.39 показывает model/command/args, но не самостоятельный effort selector. API-seeded profiles можно использовать как пресеты новых разговоров; это не заменяет mid-session combo picker. Не применять JetBrains clientInfo spoof: adapter отключает configOptions лишь для конкретного старого IDE, публичного environment switch для этого нет. [SDK settings](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-sdk/openhands/sdk/settings/model.py#L1884), [Canvas form](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/routes/agent-settings.tsx#L291).

### Qwen: штатный `/effort` предпочтительнее aliases

Qwen 0.27.2 предоставляет `/effort` в `interactive`, `non_interactive` **и `acp`**. Без аргумента он сообщает текущее значение и доступные tiers; `/effort <tier>` применяет настройку со следующего prompt, сохраняет `model.reasoningEffort`, если execution policy не запретила persistence. Это уже работающий upstream путь через чат/terminal без Canvas JS. Для `qwen3.8-max` и `qwen3.8-flash` поддержаны **low, medium, xhigh**, default xhigh; qwen3.5/3.6/3.7 в этом manifest — toggle-only. Не выдавать `high` за поддержанный уровень 3.8. [команда](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/packages/cli/src/ui/commands/effort-command.ts), [модельные возможности](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/packages/cli/src/acp-integration/model-configuration.ts#L52).

Если необходим именно model picker с вариантами, `modelProviders.openai[]` позволяет name/id и полный `generationConfig`. Для OpenAI Chat wire native `getRequestModel` поддерживает `generationConfig.extra_body.model` / `samplingParams.model`, поэтому alias может ссылаться на реальный wire model без gateway mapping. Например:

```json
{
  "id": "qwen3.8-max[xhigh]",
  "name": "Qwen 3.8 Max (xhigh)",
  "envKey": "OPENAI_API_KEY",
  "baseUrl": "<project-gateway>/v1",
  "generationConfig": {
    "extra_body": {"model": "qwen3.8-max", "reasoning_effort": "xhigh"}
  }
}
```

Это запасной вариант, не выбранная рекомендация: `singleAttempt`/isolated requests намеренно не применяют override model; поведение side requests нужно отдельно доказать на wire. На Responses API model override через extra_body не действует. Нельзя дублировать один id+protocol+baseUrl ради effort — registry отбросит дубликат. Полный model-specific generationConfig должен быть в provider entry: нижний `model.generationConfig` не наследуется. Шлюзу достаточно прозрачной пересылки корректных параметров и сокрытия provider credentials, без alias translator и без лимитов бюджета. [model providers](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/docs/users/configuration/model-providers.md), [request model](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/packages/core/src/core/request-model.ts), [wire pipeline](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/packages/core/src/core/openaiContentGenerator/pipeline.ts), [model types](https://github.com/QwenLM/qwen-code/blob/f42fe0f3251172c1174d00382ece522eafb3c6c2/packages/core/src/models/types.ts).

## Нативный вход Canvas без browser-storage API

`/conversations` — **HomeChatLauncher**, то есть создание нового разговора с workspace/profile/worktree. `/` лишь добавляет pinned-home routing поверх того же home. Поэтому занятый порталом `/` не мешает штатному входу `/conversations`; новый host или frontend fork не нужен. `/settings/backends` отсутствует: Manage backends — modal из backend selector, также показывается автоматически при недоступном backend. [router](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/routes.ts), [home](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/routes/home.tsx), [bootstrap/recovery](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/root.tsx), [launcher](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/components/features/home/home-chat-launcher.tsx).

Выбранный простой путь: портал даёт `/conversations` и данные для штатного Add backend: Host `<origin>/agent/gh-N`, API key `portal-session` (не provider secret), понятное имя проекта. Форма проверяет связь и автоматически выбирает добавленный backend. На следующих заходах пользователь выбирает проект в штатном selector и начинает новый conversation/worktree. Использование самим upstream собственного localStorage остаётся штатным; интеграция туда больше не пишет. [Self-hosting Add backend](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/docs/SELF_HOSTING.md), [форма](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/components/features/backends/backend-form-modal.tsx), [add/select](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/contexts/active-backend-context.tsx#L124).

Ограничение: Add backend создаёт случайный `generateId()`, а `?backend=` выбирает только уже зарегистрированный ID. Поэтому `?backend=gh-N` нельзя продолжать генерировать после перехода на родную форму. `/launch` занимается plugins/message/automation и **не регистрирует backend**. Прямой `/conversations/<id>` работает с заранее выбранным правильным backend; сам Canvas добавляет корректные backend params в собственные ссылки. Портал не знает браузерный ID без нарушения выбранного ограничения, поэтому его обычная кнопка ведёт в home. [URL selection](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/api/backend-registry/url-selection.ts), [launch](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/routes/launch.tsx).

Есть `VITE_BASE_PATH`, но это build-time basename вместе с Vite base, не runtime URL registration. Также есть default seed `VITE_BACKEND_BASE_URL` + injected session API key; он создаёт один `default-local`, поэтому не решает несколько проектов на одном origin без новых договорённостей. Для выбранного `/conversations` всё это не требуется. [router build config](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/react-router.config.ts), [default backend](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/api/backend-registry/default-backend.ts), [deployment config](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/api/agent-server-config.ts).

## Worktree: пути, создание, удаление

Agent Server принимает `worktree:true`; `worktree_options` содержит `base:default_branch|head|ref`, необязательный `ref`, `branch_prefix`. Установить `OH_CONVERSATION_WORKTREE_ROOT=/workspace/worktrees` на персистентном томе проекта. Тогда для source `/workspace/repo` создаётся `/workspace/worktrees/<conversation UUID>/repo`; рабочая папка внутри вложенного source сохраняет относительный суффикс. Branch: `<branch_prefix><conversation UUID>`. Не использовать default `/tmp/conversation-worktrees`, если /tmp контейнера пропадает при пересоздании. [request schema](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-sdk/openhands/sdk/conversation/request.py#L199), [server config](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-agent-server/openhands/agent_server/config.py#L287), [planning/create](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-agent-server/openhands/agent_server/conversation_service.py#L220).

При непригодном Git repo или пустой истории default/head создание worktree может вернуть `None`, и работа продолжится в исходном workspace. Нельзя обещать изоляцию пустого репозитория по одной галочке. Default branch путь пытается fetch origin, при ошибке использует известные refs. Host policy допускает только `/workspace/repo` и подтверждённые worktree данного проекта; одной строковой проверки prefix недостаточно — нужна проверка canonical path / Git registration на executor стороне. `podman exec --workdir /workspace/repo` задаёт cwd запуска exec-server, но не запрещает protocol requests с другим разрешённым cwd; необходимость его изменения определяется runtime-проверкой, не предположением. [worktree planner](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-agent-server/openhands/agent_server/conversation_service.py#L151).

Удаление штатного conversation закрывает event service и удаляет worktree только когда нет другого conversation, ссылающегося на него: forks делят исходный worktree. Дочерние conversations orphaned, не каскадно удалены. Upstream сознательно избегает глобального `git worktree prune`, чтобы не удалить регистрации соседних контейнеров. Не добавлять второй worktree manager/cleanup; проверить штатный lifecycle и не удалять общий source. [delete lifecycle](https://github.com/OpenHands/software-agent-sdk/blob/504a88cab0fb48ee5226238ad7b8f1d865fa84a8/openhands-agent-server/openhands/agent_server/conversation_service.py#L1851).

## Минимальная приёмка Linux

1. ACP initialize/new/prompt/load/cancel, image, реальный shell/edit через executor; обе billing-конфигурации без fallback.
2. Полный model[effort] каталог в Canvas, смена в том же thread, restart/resume сохраняет комбинацию; config-options notification не возвращает UI к base-model механизму.
3. Сохранённый Codex thread с прежними общими правилами получает пустые developerInstructions при resume; repository AGENTS.md продолжает обрабатываться самим движком.
4. Недоверенные cwd/session/auth/logout/provider/config/MCP/meta/hooks запросы отвергаются; slash `/logout` также. Secrets не появляются в ответах, stderr или app-server.log.
5. Executor down → prompt не выполняется на host. Репозиторный hook и fs/image URI не создают обхода host credentials.
6. Два conversations/worktrees одного проекта: разные cwd/branches, корректные Git и MCP, перезапуск, удаление одного не затрагивает другой. Пустой repo имеет явно проверенное поведение.
7. Qwen `/effort` в Canvas и terminal меняет wire request без своего UI; native modelProviders каталог не требует gateway alias. Gateway не добавляет quotas/max_tokens и не расходует provider secret наружу.
8. Чистый браузер: портал → `/conversations` → штатный Add backend → выбор проекта → новая задача/worktree; повторный вход и две вкладки используют штатный backend selector.
