# Выбор движка оркестрации и переиспользуемых компонентов

Дата: 2026-11-16. Исследование первичного кода, без установки, запуска движков и тестов. Версии зафиксированы через GitHub API: Agentico `ea4f9206382d00d3a6fb659159a307a4182ff6a8`; Orchestrator.inc `cb4e9bf16694ec3f530793961728761be6bafe92`. Все ссылки ниже привязаны к этим commits. «Факт» означает найденный контракт/код; «вывод» — оценку соответствия нашей платформе; «не подтверждено» — отсутствие доказательства в проверенном срезе, а не доказанное отсутствие во всём продукте.

## Решение по двум готовым coding orchestrators

**Вывод: ни один из двух продуктов без изменений не закрывает все согласованные требования.** Agentico предлагает более готовый процесс inquiry → plan → implement → review, но его встроенная модель child features ограничена одним уровнем и одним активным потомком. Orchestrator.inc существенно ближе как готовый headless runtime для Codex/Qwen, однако его доказанный контракт — управление сессиями, worktrees, chat и PR lifecycle; произвольное дерево задач и нашу policy-модель он не предоставляет в проверенных контрактах.

Для существующего Canvas разумнее переиспользовать **подходы и отдельные компоненты Orchestrator.inc к CLI transport/recovery**, а из Agentico — **контракты фаз и проверки артефактов**. Если выбирать целый дополнительный сервис только для исполнения workers, первым кандидатом будет Orchestrator.inc daemon, но это отдельный вариант интеграции с переносом владения сессиями и worktrees, а не подключаемая библиотека. Внедрять одновременно собственный supervisor и AO supervisor без единого владельца процесса нельзя.

## Agentico: факты и границы

| Область | Что подтверждено |
|---|---|
| Headless/API | OpenAPI содержит создание features, действия, конфигурацию feature/runtime, sessions/transcript/output stream, prompts/permissions, recovery, supervisor messages/events. Это реальный API для собственного UI. [OpenAPI](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/api/openapi.yaml). |
| Workflow | `PipelineProfile` содержит medium/large/moonshot и фиксированную последовательность фаз. Medium начинает с Plan; large/moonshot включают KnowledgeBase, Inquire, Research, Design перед Plan. Это удовлетворяет части требования grilling до плана лишь в соответствующем профиле. [pipeline.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/feature/pipeline.go#L25). |
| Roadmap | Parser читает заголовки `Phase N`, имя, цель и stubs. Тип фазы — tracer-bullet/tdd-fill-in/collapsed; полей dependency edges в `RoadmapPhase` нет. Считать это arbitrary DAG оснований нет. [roadmap.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/agent/roadmap.go#L23). |
| Дерево | `ErrRefactorParentIsChild` прямо запрещает ребёнку становиться родителем; `ActiveChildExistsError` обеспечивает одного активного child. Child kinds — refactor, review-feedback, rebase. Это конкретное несовпадение с рекурсивной декомпозицией. [child.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/feature/child.go#L30). |
| CLI | Codex provider запускает `codex app-server`, принимает effort, проверяет `codex login status`, минимальная версия 0.153.2. Утверждать отсутствие зависимости от локального CLI/auth нельзя. В просмотренном `internal/llm` найдены Claude/Codex/OpenCode; готовый Qwen provider не найден. [provider.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/llm/codex/provider.go#L78). |
| Model/effort | YAML поддерживает рекомендации `provider:model` по phase role, явные phase models/effort и pipeline preferences. Это phase routing, не доказанный классификатор role × type × complexity и не автоматическая quality escalation. [config.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/config/config.go#L159). |

**Контракт Codex существенно шире нашего публичного wrapper.** Agentico передаёт developerInstructions в thread/start/resume, регистрирует dynamic tools `ask_user` и `complete_phase`; completion проходит координатор и проверки pending questions/tasks/artifacts. В документации API dynamic tools отмечен experimental; версия сама по себе не доказывает совместимость модели. Это хороший образец machine-readable завершения фазы, но его нельзя подключить к wrapper только initialize/new/load/prompt/cancel без нового адаптера. [Codex contract](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/docs/codex-contract.md).

**Dynamic config — частично готово.** `PairedConfigInput` содержит Models/Effort/checkpoints; изменения родителя и активного ребёнка записываются через durable intent и startup reconciliation. Это доказательство безопасного обновления пары records, но не требуемой общей семантики YAML revision → все pending nodes/new attempts при сохранении счётчиков. [child_config.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/feature/child_config.go#L35).

**Recovery — конкретные сценарии, не универсальная гарантия.** Перед выдачей recovery items код согласует orphan runs, child creation, config intents, discard/integration transactions; Resume повторно dispatch-ит текущую фазу. Git transaction reconciliation сравнивает old/candidate/current SHAs; неоднозначность оставляет attention. Это не exactly-once для произвольных команд агента. [recovery.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/orchestrator/recovery.go#L27).

Есть durable auto-resume после распознанного usage limit Claude/Codex с известным reset time; оно исключает child features и не заменяет общую retry/escalation policy. [auto_resume.go](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/internal/orchestrator/auto_resume.go#L29).

**Что переиспользовать:** structured phase completion, разделение inquiry/research/design/plan, артефакты проверки, идеи journal/reconciliation для интеграции веток. **Что не брать целиком:** внутреннюю модель feature/child как основу рекурсивного дерева. Главная причина — доказанные ограничения модели, а не отсутствие красивого UI.

## Orchestrator.inc: факты и границы

| Область | Что подтверждено |
|---|---|
| Сервис | Долгоживущий Go daemon, SQLite, REST/SSE, worktree на project session. TUI использует PTY/ConPTY/tmux; Chat — native protocol drivers. Primary listener `127.0.0.1:3001` без authentication; browser origins ограничены. Canvas нужен доверенный backend relay. [Architecture](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/docs/architecture.md). |
| API | Есть `/api/v1/sessions`, project config, conversations, input/approvals и другие daemon resources. Можно заменить frontend, сохранив daemon; API существует независимо от Electron UI. [OpenAPI](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/httpd/apispec/openapi.yaml). |
| Qwen | `qwenacp` запускает установленный пользователем `qwen --acp`, передаёт model и system prompt; approval modes default/auto-edit/auto/yolo. Runtime session option в этой binding — model; effort здесь не передаётся. Login/settings/updates остаются у установленного CLI. [Qwen driver](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/adapters/chatdriver/qwenacp/driver.go). |
| Qwen compatibility | Код требует минимум 0.16.0. Наш 0.27.2 проходит числовой порог, но это не заменяет проверку поведения конкретной версии. [version.go](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/adapters/chatdriver/qwenacp/version.go#L13). |
| Codex | Driver запускает app-server, получает model catalog, передаёт cwd/model/approval/sandbox/developerInstructions; effort через `model_reasoning_effort`. Есть native thread resume и persistent host. Полный driver не совместим с урезанным ACP wrapper проекта. [Codex driver](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/adapters/chatdriver/codexappserver/driver.go#L287). |
| Configuration | `ProjectConfig` — типизированный JSON в SQLite, названный преемником legacy YAML; defaults и worker/orchestrator/reviewer overrides применяются при spawn. Это поддержка выбора Codex как orchestrator, но не текущая YAML policy engine. [projectconfig.go](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/domain/projectconfig.go#L10). |
| Live edits | `SessionRecord.Model/Effort` фиксируются при spawn; effort специально сохраняется, чтобы project-default changes не меняли resume. Наша семантика «следующая попытка использует актуальную policy» требует собственной реализации. [session.go](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/domain/session.go#L393). |

**Workflow/reactions.** В domain есть session kinds worker/orchestrator. Проверенные domain/API не доказывают persistable recursive task tree с dependency edges и per-branch budgets. Lifecycle reactions направляют владельцу PR prompts для CI, review feedback и merge conflict; dedup signatures и attempts сохраняются. Это полезная operational automation, но не предотвращение конфликтов до начала работы и не смена model/effort по оценке качества. [reactions.go](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/backend/internal/lifecycle/reactions.go#L19).

**Recovery-компонент особенно полезен.** Detached per-session host владеет provider process; daemon присоединяется эксклюзивно, generation в SQLite ограничивает запись проекции. Codex replay buffer ограничен 32 MiB с backpressure; ACP хранит prompt events/ACK в журнале. Это защищает daemon replacement, включая forced daemon death. ADR отдельно исключает гарантию переживания host/machine death: после этого нужен native load/resume. Журнал/ACK проекции не делает внешние filesystem/Git/API effects exactly-once. Host наследует разрешённое окружение при запуске; credential data не записывается в descriptor. [ADR 0003](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/docs/adr/0003-persistent-chat-provider-host.md).

**Что переиспользовать:** протоколы `qwenacp`/`codexappserver`, дизайн exclusive provider ownership, журнал доставки/ACK, generation fences, восстановление permissions/input и durable PR reaction counters. Это Go-пакеты в `backend/internal`, связанные с domain/ports и другими пакетами AO. Поэтому реалистичная форма — небольшой vendor/fork с сохранением лицензий либо реализация наших адаптеров по контракту; «импортировать готовую независимую библиотеку» не подтверждено. Целый daemon можно подключить через API, но он станет владельцем своих sessions/worktrees/provider hosts и добавит второй state store.

## Совместимость с нынешней платформой

Исходные ограничения платформы заданы отдельно: Canvas + OpenHands AgentServer, Codex app-server на trusted host, exec-server в rootless Podman, credentials вне контейнера, wrapper с фиксированным `/workspace/repo`, без model/effort и с multi_agent=false.

Из этого следуют конкретные интеграционные работы независимо от выбора продукта:

1. Platform-owned dispatch должен выбирать CLI/model/effort до запуска attempt. Нынешний wrapper должен получить внутренний доверенный контракт выбора, не произвольные пользовательские флаги.
2. Native process launch в AO/Agentico не доказывает нашу границу host auth / container exec. Нужен отдельный launcher/transport, сохраняющий эту границу и выдающий каждой ветке нужный worktree как `/workspace/repo`.
3. Recursive tree, admission/dependency checks, разрешение пересечений изменений, serial integration и per-branch counters остаются обязанностью платформенного scheduler. Изолированный worktree сам по себе не исключает merge/semantic conflicts.
4. Ни для одного продукта не подтверждена готовая policy role/type/complexity → CLI/model/effort + escalation + live YAML revisions с сохранением всех счётчиков.

Это вывод о соответствии контрактов, а не результат runtime эксперимента. Проверка на целевой Ubuntu в этой research-only задаче не выполнялась.

## Лицензии

Оба корневых LICENSE — Apache-2.0. Раздел redistribution требует передавать лицензию, отмечать изменённые файлы, сохранять применимые notices/attribution и NOTICE при наличии. Agentico отдельно содержит NOTICE и лицензии включённых skills/agents; перенос этих материалов требует сохранять их собственную атрибуцию. Это описание текста, не юридическое заключение. [Agentico LICENSE](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/LICENSE.txt), [Agentico NOTICE](https://github.com/doordash-oss/agentic-orchestrator/blob/ea4f9206382d00d3a6fb659159a307a4182ff6a8/NOTICE.txt), [Orchestrator.inc LICENSE](https://github.com/OrchestratorInc/agent-orchestrator/blob/cb4e9bf16694ec3f530793961728761be6bafe92/LICENSE).

## Движки выполнения: DBOS, Temporal, LangGraph

Эти кандидаты предоставляют управление выполнением обычного кода. Их можно использовать для CLI-вызовов; они не требуют заменять Codex/Qwen собственным model API agent. Они также не включают готовые драйверы именно нашей платформы, правила модели по сложности, владение worktree и предотвращение конфликтов — это прикладной слой.

| Кандидат | Что берём готовым | Эксплуатация | Оценка для ai-bootstrap |
| --- | --- | --- | --- |
| DBOS OSS | Workflows/steps, checkpoints, durable очереди, дочерние вызовы, сообщения, retries и восстановление | Python-библиотека и БД; production-рекомендация — PostgreSQL, отдельного orchestration server нет | Основной кандидат для одной VM и нынешнего Python backend; нужно учитывать ресурсы ожидающих workflows |
| Temporal | Workflows/Activities, children, signals, timers, task queues, heartbeats, cancellation | Temporal Service + PostgreSQL + Python workers; UI отдельно | Более полный механизм длительного исполнения; больше сервисов и операционной работы |
| LangGraph OSS | State graph, ветвления/циклы, checkpoints, interrupts для ответа человека | Python-библиотека + persistent checkpointer | Подходит, если центральная сложность — граф диалога; вокруг длительных CLI и очереди задач останется больше собственного кода |

Фактические основания таблицы: [DBOS architecture](https://docs.dbos.dev/architecture), [Temporal Python SDK](https://github.com/temporalio/sdk-python), [Temporal deployment](https://docs.temporal.io/self-hosted-guide/deployment), [LangGraph repository](https://github.com/langchain-ai/langgraph), [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers). Последняя колонка — инженерная оценка, не измеренный benchmark.

### DBOS: минимальный состав сервисов с явными ограничениями

DBOS сохраняет inputs workflow и outputs steps. После рестарта на одном сервере находит незавершённые workflows; при повторном проходе завершённые steps возвращают сохранённые результаты, незавершённый может выполниться повторно. Workflow должен быть детерминированным, steps — выдерживать повторы. Межузловое recovery требует дополнительной координации. [Architecture](https://docs.dbos.dev/architecture).

Python quickstart использует SQLite по умолчанию, но рекомендует PostgreSQL для production. Поэтому базовая рекомендация здесь — DBOS OSS + PostgreSQL, без Conductor в минимальном составе. [Programming guide](https://docs.dbos.dev/python/programming-guide). Core Python-библиотека распространяется под MIT. [Repository/license](https://github.com/dbos-inc/dbos-transact-py).

Очереди имеют worker/global concurrency, rate limits и runtime reconfiguration. Глобальный лимит считает `PENDING` workflows; координаторы, ожидающие своих детей, не должны исчерпывать ту же ограниченную очередь, из которой запускаются дети. Для нашей архитектуры нужны отдельные очереди координации и исполнения, либо явный admission control исполнителей. [Queues](https://docs.dbos.dev/python/tutorials/queue-tutorial).

**Существенная граница OSS:** обычное `recv_async` позволяет durable ожидание ответа человека, но coroutine/state остаются в памяти; queued workflow продолжает занимать concurrency capacity. Выгрузка ожидания с освобождением ресурсов через `suspend=True` — Enterprise. Для неё нужны `suspend_mode=True`, `dbos[enterprise]`, а в production — Conductor. Это не включено в рекомендуемый минимальный OSS-стек. [Suspending workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial#suspending-workflows), [Prompting guide](https://docs.dbos.dev/python/prompting), [ветки core/enterprise в recv_async](https://github.com/dbos-inc/dbos-transact-py/blob/main/dbos/_dbos.py#L2070).

Отмена обычного workflow проверяется на границе step; для прерывания текущего async step нужен `preemptible`. Остановка Python coroutine сама по себе не доказывает остановку дочернего CLI-процесса. [Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial).

### Temporal: больше готового управления жизненным циклом

Python SDK предоставляет child workflows, signals/updates, timers и ожидание условий. Сеть, файловые операции и запуск CLI помещаются в Activities, поскольку workflow-код должен быть детерминированным. Heartbeats позволяют долгой Activity сообщать состояние и принимать отмену; heartbeat details можно передать следующей попытке. Всё это — строительные блоки адаптера CLI, а не готовое подключение Codex/Qwen. [Python SDK](https://github.com/temporalio/sdk-python).

Production-развёртывание включает отдельный Temporal Service и persistent database. `temporal server start-dev` и embedded server предназначены для разработки/тестирования; их нельзя автоматически считать production-архитектурой. [Self-hosted guide](https://docs.temporal.io/self-hosted-guide). Elasticsearch не обязателен: PostgreSQL поддерживает Visibility. [Visibility](https://docs.temporal.io/self-hosted-guide/visibility). Сервер имеет MIT-лицензию. [Server repository](https://github.com/temporalio/temporal).

**Оценка:** выбрать Temporal вместо DBOS, если уже сейчас важны многочисленные долгие ожидания, отдельные группы workers и развитое управление дочерними executions. Для одной VM он возможен, но добавляет обслуживаемую систему. Kubernetes сам по себе не является требованием; совместимость конкретного production-развёртывания с rootless Podman здесь не проверялась.

### LangGraph: библиотека графа, совместимая с CLI-only

LangGraph OSS можно использовать без LangChain; узлы могут выполнять обычный код, поэтому CLI-only его не исключает. Проект имеет MIT-лицензию. [Repository](https://github.com/langchain-ai/langgraph). Checkpointers сохраняют состояние графа; `interrupt()` ждёт ответа человека, продолжение использует тот же thread и повторно запускает узел с начала. [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers), [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).

**Оценка:** это разумный выбор для сложного графа общения и reasoning-стадий. Для нашей задачи он оставляет значительный слой фонового scheduling, владения CLI-процессами и worktrees. Не следует автоматически соединять LangGraph с DBOS или Temporal: сначала выбрать одного владельца состояния выполнения; отдельный agent graph добавлять только при конкретной необходимости.

## Что уже можно переиспользовать из OpenHands

Проверены SDK/Agent Server `v1.62.0` и Canvas commit `5aed815f5384c48bccacf1e40a8619653d488ad0` (1.39.0).

1. **Canvas и Custom ACP transport.** SDK запускает ACP agent и отображает его сообщения/tool calls. Поэтому третий профиль `orchestrator` может быть фасадом к платформенному workflow, сохранив основной разговор. Это предложенная интеграция на существующем контракте. [ACPAgent](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-sdk/openhands/sdk/agent/acp_agent.py#L1599).
2. **Разговоры и журнал событий.** Agent Server хранит состояние и связи parent/child conversations; `EventService.run()` запускает фоновую asyncio task. После рестарта RUNNING переводится в ERROR, активный goal — interrupted. Это полезное хранение разговоров, но не автоматическое восстановление дерева исполнения. [EventService](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-agent-server/openhands/agent_server/event_service.py#L1452), [parent relation](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-agent-server/openhands/agent_server/conversation_service.py#L1536).
3. **Native delegation не готов для наших ACP workers.** ACPAgent объявляет `supports_openhands_tools=False`, custom SDK tools отвергаются. Native TaskManager создаёт дочернего native agent с workspace родителя. Добавить TaskToolSet к существующему ACP-профилю и получить требуемое дерево нельзя. [ACP tools boundary](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-sdk/openhands/sdk/agent/acp_agent.py#L2321), [TaskManager](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-tools/openhands/tools/task/manager.py#L307).
4. **Grilling и дерево требуют адаптации представления.** Pinned ACP bridge отклоняет structured elicitation и не обрабатывает plan notification как гарантированный UI-план. Обычные текстовые вопросы/ответы и tool cards можно использовать; интерактивное рекурсивное дерево не появится автоматически. Canvas planner UI — отдельный workflow. [ACP callbacks](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-sdk/openhands/sdk/agent/acp_agent.py#L1784), [Canvas planner wrapper](https://github.com/OpenHands/OpenHands/blob/5aed815f5384c48bccacf1e40a8619653d488ad0/src/contexts/websocket-provider-wrapper.tsx#L70).

## Общий предел гарантий и практика исполнения

Ни workflow checkpoint, ни durable очередь не являются снимком памяти/PTY работающего CLI. При падении worker CLI может продолжить работу; при падении всей VM потребуется native session resume либо новая attempt. Повторный запуск без сверки способен создать двух исполнителей одного worktree. Это инженерное следствие описанных контрактов recovery.

Практика для нашего адаптера: durable task/attempt/run IDs, один владелец процесса, сохранённые session IDs, операция attach/reconcile перед повтором, подтверждение фактической остановки при cancel. Завершение фиксировать по структурированному результату и артефактам проверки. Для Git-интеграции хранить исходный/candidate/current commit и сериализовать запись в integration branch. Эти подходы можно заимствовать из проверенных компонентов AO и Agentico, сохраняя границу доверенного Codex bridge.

Свежий YAML читается при новом dispatch/attempt; принятое решение и версия правил сохраняются. Replay уже начатой attempt использует сохранённое решение, следующая attempt — актуальный YAML. Счётчики остаются состоянием задачи. Это реализация согласованной семантики, а не требование заморозить настройки на весь run.

## Рекомендация для выбора сейчас

**Основной вариант: существующий Canvas/Agent Server + DBOS OSS/PostgreSQL + платформенные адаптеры CLI.** Это инженерная рекомендация по текущим требованиям; владелец ещё не выбрал движок. Причины: Python backend и одна VM, уже готовые песочницы/auth/MCP, необходимость сохранить точный контракт дерева, YAML и интеграции веток. DBOS закрывает durable primitives; предметные правила остаются прозрачным кодом платформы.

```text
Canvas → Agent Server → Custom ACP orchestrator facade
                              ↓
                   workflow service: DBOS + PostgreSQL
                   ├── правила YAML / дерево / зависимости
                   ├── grilling / попытки / повышение / лимиты
                   └── allocation worktrees / serial integration
                              ↓
              доверенные адаптеры Codex и Qwen
```

**Граница переиспользования:**

- Берём готовым: workflow/queue/recovery primitives DBOS; существующие Canvas, Agent Server, Codex auth/remote executor, Qwen, песочницы и MCP-инструменты.
- Адаптируем по первичным контрактам: CLI transport, ownership/reconnect/journal из AO; typed phase completion, артефакты и Git reconciliation из Agentico. Их `internal` Go-код не является независимой Python-библиотекой; прямой перенос потребует выделения зависимостей и соблюдения лицензий.
- Пишем для платформы: recursive task model, правила маршрутизации/повышения, live YAML semantics, prevention of overlaps, интеграцию результатов и отображение дерева. Ни один изученный кандидат не подтверждает готовность всего этого сочетания.
- Существующие навыки grilling/research/разработки используем как инструкции стадий; workflow engine управляет их порядком, входами, результатами и остановками.

**Альтернативы с ясной ценой:** Temporal вместо DBOS — больше встроенного управления долгими executions и больше эксплуатации. Целый Orchestrator.inc daemon — больше готового CLI/session/worktree management, но второй крупный backend/state store и адаптация к платформенной авторизации; собственные tree/policy всё равно остаются. Agentico целиком не рекомендован из-за ограниченной child-модели и отсутствия готового Qwen provider. LangGraph уместен при приоритете conversational graph, но не уменьшает главную работу вокруг CLI lifecycle.

Это сравнение исходников и контрактов, не результат интеграционного запуска. Движки не устанавливались, платные вызовы моделей не выполнялись. Проверки установленных адаптеров и recovery следует выполнять только на целевой Linux VM, когда выбран вариант реализации.
