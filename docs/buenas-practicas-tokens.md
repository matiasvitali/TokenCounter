# Buenas prácticas para ahorrar tokens: comportamientos del usuario y cómo detectarlos

Consultado el 2026-10-07. Complementa `docs/fugas-de-tokens.md` (reglas R1-R14): aquí el foco es **qué hace el usuario** y qué debería hacer en su lugar; las fugas ya descritas allí se referencian como `R#` y no se repiten.
Fuentes: docs oficiales (code.claude.com, platform.claude.com) y el código de `collect.py`, `otel_receiver.py` y `report.py`.
Todo umbral o afirmación sin cita es **heurística propia** y está marcado `[H]`.

Abreviaturas de fuentes:
- **[COST]** https://code.claude.com/docs/en/costs
- **[BP]** https://code.claude.com/docs/en/best-practices
- **[PC-CC]** https://code.claude.com/docs/en/prompt-caching
- **[PC-API]** https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- **[PRICE]** https://platform.claude.com/docs/en/about-claude/pricing
- **[RL]** https://platform.claude.com/docs/en/api/rate-limits
- **[VISION]** https://platform.claude.com/docs/en/build-with-claude/vision
- **[MODEL]** https://code.claude.com/docs/en/model-config
- **[SUB]** https://code.claude.com/docs/en/sub-agents
- **[MEM]** https://code.claude.com/docs/en/memory
- **[CTX]** https://code.claude.com/docs/en/context-window
- **[MCP]** https://code.claude.com/docs/en/mcp
- **[TOOLS]** https://code.claude.com/docs/en/tools-reference
- **[PERM]** https://code.claude.com/docs/en/permissions
- **[ENV]** https://code.claude.com/docs/en/env-vars
- **[OTEL]** https://code.claude.com/docs/en/monitoring-usage

---

## 0. Datos disponibles (actualización respecto a `fugas-de-tokens.md`)

`fugas-de-tokens.md` §2 dice que `usage` no tiene `isSidechain`. **Ya no es así**: `collect.py` (líneas 10-13 y 84) guarda ahora:

- `usage(id, user, project, session, model, ts, input, output, cache_create, cache_read, side, cache_5m, cache_1h)`. `side=1` = respuesta de subagente (`isSidechain`); `cache_5m`/`cache_1h` = `cache_creation.ephemeral_5m/1h_input_tokens` [PC-API].
- `attrib(id, user, session, ts, cat, name, calls, out_tok, in_tok)`. Las filas `cat='Sistema', name='prompt / contexto inicial'` se crean una por cada respuesta que sigue a un **prompt del usuario** (`collect.py` líneas 59-62 y 92-101), así que `COUNT(*)` de esas filas por sesión ≈ nº de prompts del usuario (estimación `[H]`).
- `otel_events(ts, session, event, model, query_source, input, output, cache_read, cache_create, cost_usd, tool_name, success, duration_ms, skill, plugin, mcp_server)`; solo se guardan `api_request`, `api_error` y `tool_result` (`otel_receiver.py` línea 21, `KEEP`). **No** se guardan `effort`, `speed`, `tool_result_size_bytes`, `error_type`, `status_code`, `attempt` ni el evento `user_prompt`, aunque Claude Code los emite [OTEL] "Events".
- `otel_metrics(ts, name, value, session, model, type, query_source, skill, plugin, mcp_server)`.

`ctx = input + cache_create + cache_read` (prompt completo de la respuesta) [RL] "Cache-aware ITPM". Se reutiliza la vista `t` de `fugas-de-tokens.md` §2 (añadiendo `WHERE side=0` para el hilo principal).

Muestra real de la `tokens.db` local (2026-10-07, solo para calibrar, no es un umbral): 82 sesiones, 54 con `MAX(ctx)>150k` y 22 con `>300k` (máx. 567k); **contexto de la primera respuesta del hilo principal: mediana ≈ 73k tokens** (el ejemplo oficial de arranque suma ~8k [CTX]); 102 de 632 respuestas de subagente en Opus.

---

## 1. Criterio de severidad

Precios de referencia por MTok [PRICE] "Model pricing": Opus 5.5 $4 entrada / $20 salida / $0.20 lectura de caché; Sonnet 5.5 $2 / $10 / $0.10; Sonnet 5 $2 / $10 / $0.20; Haiku 5.5 $0.10 / $0.50 / $0.01 (≤100k de prompt); Fable 5.1 $10 / $50 / $0.25. Escritura de caché = 1.25x (5 min) o 2x (1 h) la entrada base [PRICE] "Prompt caching".

| Severidad | Criterio (impacto estimado) |
|---|---|
| **CRÍTICO** | Escala con el tamaño del contexto **en cada turno** o **multiplica el precio unitario ≥2x** de forma sostenida. Puede por sí solo superar el 10 % del uso, que es el umbral con el que `/usage` marca un "behavior flag" [COST] "Plan usage breakdown". Ej.: 500k de contexto × 100 turnos = 50 M de lectura de caché (≈ $10 en Opus 5.5) solo por releer historia. |
| **MEDIO** | Coste **puntual pero grande** (reconstrucción completa del caché, un resultado de 10k-25k tokens, un /compact en frío) o recurrente pero acotado. `[H]` típicamente 1-10 % del uso. |
| **BAJO** | `[H]` < 1 % del uso o solo aplica en escenarios concretos (API propia, Batch, límites del plan); beneficio indirecto (calidad, menos reintentos). |

---

## 2. Catálogo de malas prácticas y recomendaciones

Formato: **Qué hace mal** · **Por qué cuesta** (cita) · **Recomendación** (comando/setting) · **Señal de detección** (tablas/columnas y umbral, o qué dato falta).

### CRÍTICO

#### BP-01. Sesión "cajón de sastre": no hacer `/clear` entre tareas no relacionadas
- **Qué hace mal**: encadena tareas distintas (y preguntas laterales) en la misma sesión durante horas.
- **Por qué cuesta**: "Claude Code sends your full conversation with every request [...] a one-line question in a session that has been open all day still draws usage for the whole conversation" [COST] "Why usage climbs in a long session". "Stale context wastes tokens on every subsequent message" [COST] "Manage context proactively". Patrón de fallo "The kitchen sink session" [BP] "Avoid common failure patterns". Las sesiones largas sin limpiar son, junto con Opus por defecto, la causa habitual de gasto inesperado [COST] "When a developer asks about a limit".
- **Recomendación**: `/clear` al cambiar de tarea (`/rename` antes, `/resume` para volver); `/btw` para preguntas que no deben quedar en el contexto ("The answer never enters conversation history") [BP] "Manage context aggressively"; `/clear` "costs nothing" frente a `/compact` [COST]; `/context` para ver qué ocupa.
- **Señal**: = R1. `usage` (`side=0`) por `session`: `MAX(ctx) > 150000 AND COUNT(*) > 40`. Complemento `[H]`: nº de prompts del usuario por sesión (`attrib` `name='prompt / contexto inicial'`) ≥ 30 con `MAX(ctx) > 150k` ⇒ varias tareas en una sesión.

#### BP-02. Dejar crecer el contexto hasta el auto-compact de los modelos de 1M
- **Qué hace mal**: usa modelos con ventana de 1M (Sonnet 5/5.5, Opus 4.6+, Fable, Haiku 5.5) sin ajustar el umbral de auto-compact, así que el contexto llega a cientos de miles de tokens.
- **Por qué cuesta**: "Models running with a native 1M window compact before the window fills, at about 967K tokens by default" [MODEL] "Auto-compact window". El 1M no tiene recargo por token ("A 900k-token request is billed at the same per-token rate as a 9k-token request") [PRICE] "Long context pricing", pero cada turno relee todo (BP-01) y el rendimiento cae al llenarse ("LLM performance degrades as context fills") [BP].
- **Recomendación**: `/autocompact 200k` (o `"autoCompactWindow": 200000` en settings, o `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000`) [MODEL]; `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` para compactar antes [ENV]; `/compact <foco>` en cortes naturales [CTX] "When your context fills up".
- **Señal**: `usage` `side=0`: `MAX(ctx) > 300000` por sesión `[H]`. En la BD local hay 22 sesiones así. Hoy la regla `ctx` de `findings()` solo mira `>150k` y no distingue este caso.

#### BP-03. Opus (o Fable) como modelo por defecto para todo
- **Qué hace mal**: deja Opus/Fable fijo para tareas rutinarias (lecturas, ediciones pequeñas, preguntas).
- **Por qué cuesta**: Opus 5.5 cuesta 2x Sonnet 5.5 en entrada y salida; Fable 5.1, 5x [PRICE]. "Sonnet handles most coding tasks well and costs less than Opus. Reserve Opus for complex architectural decisions or multi-step reasoning" [COST] "Choose the right model". Además "A switch to Opus also applies to the subagents that inherit your session's model" [COST].
- **Recomendación**: `/model sonnet` al inicio o default en `/config` (`"model": "sonnet"`); `haiku` para tareas simples ("Choose Haiku for simple tasks") [PRICE] "Cost optimization strategies"; reservar `opus`/`fable` para tareas difíciles.
- **Señal**: = R7. `usage` `side=0`: % de respuestas Opus/Fable con `output < 300 AND ctx < 30000` > 50 % (mín. 20). Complemento `[H]`: % de tokens de entrada+salida del usuario en Opus/Fable > 50 %.

#### BP-04. Subagentes y compañeros de equipo que heredan Opus; equipos grandes
- **Qué hace mal**: lanza subagentes (incluido el Explore integrado) o agent teams sin fijar un modelo barato.
- **Por qué cuesta**: los subagentes sin `model` usan el del hilo principal; el Explore integrado usa "the main conversation's model" y `CLAUDE_CODE_SUBAGENT_MODEL` "doesn't change the built-in Explore or Plan subagents" [SUB] "Choose a model". Cada subagente "send[s] its own requests, which count toward the same usage limits" [SUB]; sus requests usan TTL de 5 min [PC-CC] "Subagents and the cache". "Agent teams use approximately 7x more tokens than standard sessions when teammates run in plan mode" [COST] "Manage agent team costs".
- **Recomendación**: `model: haiku` (o `sonnet`) en el frontmatter de cada subagente; definir un subagente propio llamado `Explore` con `model: haiku` [SUB]; para todos: `"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}` [SUB] "Run every subagent on one model"; equipos pequeños, Sonnet para compañeros, cerrarlos al terminar [COST] "Agent team token costs".
- **Señal**: `usage` `side=1`: `% de filas con model LIKE '%opus%' OR '%fable%'` > 30 % (mín. 20) `[H]`. Peso total de subagentes: = R10 (`attrib cat='Subagente'` > 10 % del usuario). Agent teams no se distinguen de subagentes con los datos actuales (faltaría `query_source`/`agent.name` de OTEL por respuesta).

#### BP-05. Volver a una sesión grande tras una pausa mayor que el TTL del caché
- **Qué hace mal**: deja una sesión de 100k+ abierta, vuelve horas/días después y sigue en ella (o la reanuda con `--resume`).
- **Por qué cuesta**: "your first message after a break longer than the cache lifetime misses the cache and reprocesses your full context" [COST] "Why usage climbs". TTL: 1 h en suscripción dentro del plan; 5 min con usage credits, API key o nube [PC-CC] "Which TTL each request gets".
- **Recomendación**: cerrar con `/clear` o empezar sesión nueva con una nota de estado; aceptar "resume from a summary" en Pro/Max [COST]; con API key o usage credits y pausas de 5-60 min, `"promptCacheTtl": "1h"` [PC-CC] "Choose the TTL yourself".
- **Señal**: = R4/R13. `usage` `side=0`: `gap_s > TTL AND cache_create > 0.5*ctx AND cache_create >= 20000`. TTL por sesión según `cache_5m` vs `cache_1h` (ya lo hace `findings()`).

### MEDIO

#### BP-06. Cambiar modelo, effort o fast mode en mitad de una tarea
- **Qué hace mal**: `/model`, `/effort` o `/fast` con el contexto ya grande; usa `opusplan` y alterna plan mode a menudo.
- **Por qué cuesta**: "Each model has its own cache"; `opusplan` hace que "each plan-mode toggle is a model switch"; cambiar effort invalida el caché "on most models" (no en Opus 5.5, Sonnet 5.5, Haiku 5.5 y Fable 5.1 con API key o suscripción); fast mode añade una cabecera que es parte de la clave de caché [PC-CC] "Actions that invalidate the cache".
- **Recomendación**: "Pick your model and effort level at the top of a session" [PC-CC]; `/clear` antes de `/model`.
- **Señal**: modelo = R5 (`model <> prev_model AND cache_create > 0.5*ctx AND ctx > 50000`, `side=0`). Effort y fast mode: **no detectable** (faltan `effort` y `speed` de `api_request` [OTEL] en `otel_events`).

#### BP-07. Conectar/quitar MCP, plugins con MCP o denegar una herramienta entera a mitad de sesión
- **Por qué cuesta**: invalida todo el caché cuando las herramientas se cargan por adelantado (sin tool search, `ANTHROPIC_BASE_URL` no oficial, `ENABLE_TOOL_SEARCH=false`) [PC-CC] "Connecting or removing an MCP server", "Denying an entire tool". También el primer turno tras actualizar Claude Code [PC-CC] "Upgrading Claude Code".
- **Recomendación**: configurar MCP/plugins **antes** de empezar; `/reload-plugins` respeta el aviso de re-lectura completa [PC-CC]; `DISABLE_AUTOUPDATER=1` para elegir cuándo actualizar [PC-CC].
- **Señal**: = R6 (`cache_create > 0.5*ctx AND gap_s < 300 AND model = prev_model`, ≥2 por sesión).

#### BP-08. CLAUDE.md largo, imports que "lo ordenan", memoria y skills que inflan el arranque
- **Qué hace mal**: CLAUDE.md de cientos de líneas con flujos especializados; parte el archivo en `@imports` creyendo que ahorra; acumula reglas sin `paths:`.
- **Por qué cuesta**: CLAUDE.md "is loaded into context at session start" y lo especializado está presente "even when you're doing unrelated work" [COST] "Move instructions from CLAUDE.md to skills". "Imports [...] don't reduce its context cost, because imported files also load at launch" (hasta 4 saltos) [MEM] "Import additional files". "Longer files consume more context and reduce adherence" [MEM]. Auto memory carga las primeras 200 líneas o 25KB de `MEMORY.md` en cada sesión [MEM] "Auto memory". Cada subagente/compañero vuelve a cargar CLAUDE.md [COST] "Agent team token costs".
- **Recomendación**: < 200 líneas por CLAUDE.md [COST][MEM]; mover flujos a skills (cargan bajo demanda) [COST]; reglas con `paths:` en `.claude/rules/` [MEM] "Path-specific rules"; comentarios `<!-- -->` para notas humanas (se eliminan antes de inyectar) [MEM]; `claudeMdExcludes` para CLAUDE.md ajenos en monorepos [MEM]; `/doctor` propone recortes [BP]; `/context` para medir.
- **Señal**: `usage` `side=0`, **primera fila de cada sesión**: `ctx` (arranque: system prompt + herramientas + CLAUDE.md + memoria + skills). Umbral `[H]`: mediana por usuario > 40000 (ejemplo oficial ≈ 8k [CTX]; en esta VM la mediana es ≈ 73k). No separa CLAUDE.md de herramientas/MCP/skills: el desglose solo lo da `/context` en la máquina [CTX] "Check your own session". **No está en `findings()`**.

#### BP-09. MCP donde basta una CLI; servidores sin usar; tool search desactivado o `alwaysLoad`
- **Por qué cuesta**: "Tools like `gh`, `aws`, `gcloud`, and `sentry-cli` are still more context-efficient than MCP servers" [COST] "Reduce MCP server overhead". Con tool search (por defecto) solo cargan nombres; con `ENABLE_TOOL_SEARCH=false`, un gateway propio o `alwaysLoad: true` se cargan todos los esquemas ("each upfront tool consumes context") [MCP] "Configure tool search", "Exempt a server from deferral". Resultados MCP: aviso a 10,000 tokens, límite `MAX_MCP_OUTPUT_TOKENS` 25,000 por defecto [MCP].
- **Recomendación**: `/mcp` para desactivar lo que no se usa [COST]; preferir CLIs [BP] "Use CLI tools"; no poner `alwaysLoad` salvo herramientas de cada turno [MCP]; si se usa gateway que reenvía `tool_reference`, `ENABLE_TOOL_SEARCH=true` [MCP].
- **Señal**: resultados = R9 / regla `big` (`attrib cat='MCP'`, `SUM(in_tok)/SUM(calls) > 10000`). Esquemas cargados por adelantado: indirecta vía contexto de arranque (BP-08). El servidor concreto con más peso en arranque: **no detectable** (faltaría el desglose de `/context`).

#### BP-10. Leer archivos enteros, Grep sin acotar y confiar en `.claudeignore`
- **Por qué cuesta**: Read devuelve el archivo desde el principio hasta el límite de tokens y solo entonces pagina [TOOLS] "Read tool behavior". "If your project has a `.claudeignore` file, it has no effect, so move its entries into `Read` deny rules" [PERM]. Cada lectura queda en la historia y se relee cada turno (BP-01). Ejemplo oficial: leer `auth.ts` = 2.4k tokens [CTX].
- **Recomendación**: pedir rangos (`offset`/`limit`) y `Grep` con `head_limit` [TOOLS]; `permissions.deny: ["Read(./node_modules/**)", "Read(./dist/**)", "Read(./*.log)"]` [PERM]; `@archivo` concreto en vez de describir dónde está [BP] "Provide rich content"; plugins de code intelligence ("A single 'go to definition' call replaces [...] a grep followed by reading multiple candidate files") [COST]; `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` para fijar el límite [ENV].
- **Señal**: = R8 / regla `big`: `attrib cat='Herramienta' AND name IN ('Read','Grep','Glob')`, `SUM(in_tok)/SUM(calls) > 10000` (mín. 3) y > 10 % del usuario. Lecturas repetidas del mismo archivo o de rutas que deberían estar denegadas: **no detectable** (faltan `tool_parameters`/`tool_result_size_bytes` [OTEL], que además exigen `OTEL_LOG_TOOL_DETAILS=1`).

#### BP-11. Salidas de Bash enormes (tests, builds, logs) sin filtrar
- **Por qué cuesta**: Bash devuelve inline hasta ~30,000 caracteres por defecto (≈ 7.5k tokens con 1 token ≈ 4 caracteres [PRICE] FAQ); lo que excede se guarda en archivo con vista previa [TOOLS] "Output limits". "Instead of Claude reading a 10,000-line log file [...] a hook can grep for `ERROR` [...] reducing context from tens of thousands of tokens to hundreds" [COST] "Offload processing to hooks and skills".
- **Recomendación**: hook `PreToolUse` con `matcher: "Bash"` que filtra la salida de tests (ejemplo oficial con `grep -A 5 -E '(FAIL|ERROR|error:)' | head -100`) [COST]; "Prefer running single tests" en CLAUDE.md [BP]; delegar suites/logs a un subagente ("report only the failing tests") [SUB] "Isolate high-volume operations"; no subir `bashOutputMaxChars`/`BASH_MAX_OUTPUT_LENGTH` sin necesidad [TOOLS][ENV].
- **Señal**: regla `big` con `name IN ('Bash','PowerShell')`. En esta VM Bash promedia ≈ 1.1k tokens/llamada (sin alerta). Qué comando produjo la salida: **no detectable** (falta `tool_parameters`).

#### BP-12. Prompts vagos y exploración sin acotar en el hilo principal
- **Qué hace mal**: "mejora este código", "investiga X" sin alcance; Claude lee cientos de archivos.
- **Por qué cuesta**: "Vague requests like 'improve this codebase' trigger broad scanning" [COST] "Write specific prompts". Patrón "The infinite exploration" [BP].
- **Recomendación**: nombrar archivo, escenario y criterio de éxito [BP] "Provide specific context in your prompts"; "use subagents to investigate X" para que la exploración quede fuera del contexto principal [BP] "Use subagents for investigation"; una skill "codebase-overview" para no re-explorar [COST].
- **Señal** `[H]`: por sesión, `attrib cat='Herramienta' AND name IN ('Read','Grep','Glob')` con `SUM(calls) >= 60`, `MAX(ctx) > 150000` en `usage side=0` y **ninguna** llamada `cat='Subagente'`. Limitación: `attrib` mezcla herramientas del hilo principal y de subagentes. La vaguedad del prompt en sí: **no detectable** (requiere `OTEL_LOG_USER_PROMPTS=1`, sensible).

#### BP-13. Corregir una y otra vez en la misma sesión; reintentos
- **Por qué cuesta**: "If you've corrected Claude more than twice on the same issue in one session, the context is cluttered with failed approaches. Run `/clear` and start fresh with a more specific prompt" [BP] "Course-correct early and often".
- **Recomendación**: Esc para parar; `/rewind` (o Esc Esc) en vez de seguir: además reutiliza un prefijo ya cacheado [PC-CC] "Rewinding the conversation"; dar a Claude una verificación (tests, build) [BP] "Give Claude a way to verify its work"; probar incrementalmente [COST].
- **Señal**: fallos de herramientas = R12 (`otel_events` `tool_result`, `success='false'` > 15 % con ≥ 20). Correcciones del usuario: **no detectable** con fiabilidad. Proxy `[H]`: muchos prompts (`attrib` `prompt / contexto inicial`) con pocas ediciones nuevas. Faltaría el evento `user_prompt` (`prompt_length`) [OTEL] y marcar `/rewind`.

#### BP-14. Esfuerzo/razonamiento alto en tareas sencillas
- **Por qué cuesta**: "Thinking tokens are billed as output tokens, and the default budget can be tens of thousands of tokens per request" [COST] "Adjust extended thinking". La salida cuesta 5x la entrada en todos los modelos [PRICE]. "You are charged for all thinking tokens generated, even when collapsed" [MODEL].
- **Recomendación**: `/effort low|medium` (o `"effortLevel": "medium"`, `CLAUDE_CODE_EFFORT_LEVEL`) [MODEL]; Opus/Sonnet/Haiku 5.5 ya parten en `medium` [MODEL]; `MAX_THINKING_TOKENS` solo en modelos con presupuesto fijo [COST]; `effort` también se puede limitar con `maxEffortLevel` [ENV].
- **Señal**: = R11 (`usage` `AVG(output) > 4000`, mín. 20). El nivel de effort usado: **no detectable** (añadir `effort` de `api_request` a `otel_events`).

#### BP-15. Acumular capturas de pantalla e imágenes grandes
- **Por qué cuesta**: una imagen cuesta `⌈ancho/28⌉ × ⌈alto/28⌉` tokens; en modelos 4.7+ (alta resolución) hasta 4,784 por imagen ("up to roughly three times more visual tokens") [VISION] "Resolution and token cost". Siguen en la historia y se releen; al pasar el límite Claude Code borra un lote de las más antiguas y "the next request reprocesses the conversation from the earliest of those messages onward" [PC-CC] "Accumulating many images".
- **Recomendación**: recortar o reducir la captura antes de pegarla [VISION][TOOLS]; preferir lectura de texto de la página (texto/árbol de accesibilidad) a capturas cuando baste `[H]`; `/clear` tras sesiones de UI con muchas capturas.
- **Señal**: **no detectable directamente** (no se cuentan bloques `image`). Proxy `[H]`: `attrib cat='MCP'` de servidores de navegador (`Claude_Browser`, `claude-in-chrome`) con > 10 % del `in_tok` del usuario. Faltaría: que `collect.py` cuente bloques `type='image'` en mensajes de usuario y `tool_result`.

#### BP-16. Tareas en segundo plano sobre contextos grandes
- **Por qué cuesta**: tareas programadas (`/loop`), mensajes entre sesiones, check-ins de `/goal` y prompt suggestions "sending your full context each time" [COST] "Why usage climbs" / "Background token usage".
- **Recomendación**: `/loop` solo en sesiones ligeras; `"crossSessionInbound": "hold"`; `CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0`; desactivar prompt suggestions [COST]; revisar la fila "Loops" de `/usage` [COST].
- **Señal**: = R14. `usage side=0`: `output < 50 AND ctx > 50000 AND gap_s > 120`, ≥ 10 por usuario y día `[H]`. Con OTEL, `query_source='auxiliary'` en métricas [OTEL]. **No está en `findings()`**.

#### BP-17. `/compact` en frío o dejar que el auto-compact salte en mitad de una tarea
- **Por qué cuesta**: "/compact reads the conversation it summarizes, so compacting a large context is itself a large request" [COST]; tras una pausa mayor que el TTL "the summarization request reprocesses the full history as uncached input" [PC-CC] "Compacting the conversation".
- **Recomendación**: `/compact <instrucciones>` en un corte natural y con el caché caliente; si se va a abandonar el camino, `/rewind` en vez de compactar; si no hace falta continuidad, `/clear` [PC-CC][COST]; instrucciones de compactación en CLAUDE.md (`# Compact instructions`) [COST].
- **Señal** `[H]`: `usage side=0` (vista `t`): `ctx < 0.5*prev_ctx AND prev_ctx > 100000 AND cache_read < 0.5*ctx AND gap_s > TTL` (caída por compactación tras pausa; el `cache_read` bajo la distingue de un `/rewind`). Coste ≈ `prev_ctx` sin caché. La petición de resumen en sí no aparece como fila propia en `usage`; con OTEL es `query_source='compact'` [OTEL]. **No está en `findings()`**.

#### BP-18. TTL de 5 min con pausas frecuentes de 5-60 min (API key o usage credits)
- **Por qué cuesta**: al pasar a usage credits, el hilo principal baja a 5 min de TTL [PC-CC] "Which TTL each request gets"; cada pausa > 5 min reescribe todo (1.25x). El TTL de 1 h cuesta 2x por escritura pero se amortiza "after two cache reads" [PRICE] "Prompt caching".
- **Recomendación**: `"promptCacheTtl": "1h"` (o `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`) si hay pausas de 5-60 min; `subagentPromptCacheTtl` para subagentes largos [PC-CC].
- **Señal**: `usage side=0` con `SUM(cache_5m) > SUM(cache_1h)` en la sesión **y** ≥ 3 reconstrucciones con `300 < gap_s < 3600`. `findings()` ya calcula el TTL (línea 110) y cuenta estas pausas en `idle`, pero el consejo no menciona `promptCacheTtl`.

#### BP-19. Fast mode encendido por defecto o a mitad de sesión
- **Por qué cuesta**: Opus 5.5 en fast mode cuesta $8/$40 por MTok frente a $4/$20 (2x) [PRICE] "Fast mode pricing"; activarlo con contexto grande provoca un fallo de caché facturado a tarifa fast [PC-CC] "Turning on fast mode".
- **Recomendación**: reservar `/fast` para momentos puntuales y activarlo al inicio de sesión, no tras horas de contexto [PC-CC].
- **Señal**: **no detectable** (falta `speed` de `api_request`/`token.usage` [OTEL] en `otel_events`).

### BAJO

#### BP-20. Plan mode mal usado (ausente en cambios grandes, presente en triviales)
- **Por qué cuesta**: plan mode evita "expensive re-work when the initial direction is wrong" [COST] "Work efficiently on complex tasks"; pero "also adds overhead. [...] If you could describe the diff in one sentence, skip the plan" [BP] "Explore first, then plan, then code". Con `opusplan` cada cambio de modo es cambio de modelo (BP-06).
- **Recomendación**: Shift+Tab a plan mode para cambios multiarchivo o enfoque incierto; directo para cambios pequeños.
- **Señal** `[H]`: sesiones con `attrib name IN ('Edit','Write')` `SUM(calls) >= 30` y ninguna llamada `name='ExitPlanMode'`; ≥ 3 sesiones por usuario. Con OTEL, el evento `permission_mode_changed` [OTEL] (no se guarda hoy).

#### BP-21. Hooks que inyectan mucho contexto (o no usar hooks para filtrar)
- **Por qué cuesta**: la salida de hooks entra en la conversación ("Context that hooks added earlier — Summarized with the rest of the conversation") [CTX] "What survives compaction"; un hook ruidoso en `PostToolUse`/`UserPromptSubmit` se paga en cada evento `[H]`. El uso correcto es lo contrario: filtrar antes de que llegue a Claude [COST].
- **Recomendación**: hooks silenciosos en éxito, salida corta en fallo `[H]`; usar hooks para preprocesar (BP-11).
- **Señal**: **no detectable** (`otel_receiver.py` descarta los eventos de hooks; no hay tamaño de salida de hook).

#### BP-22. Output styles extensos
- **Por qué cuesta**: el output style se carga antes del primer prompt [CTX] "What the timeline shows"; cambiarlo a mitad de sesión **no** invalida el caché [PC-CC] "Changing output style".
- **Recomendación**: estilos breves; cambiarlos sin miedo al caché.
- **Señal**: indirecta en el contexto de arranque (BP-08); el estilo concreto: **no detectable**.

#### BP-23. Haiku 5.5 con prompts de más de 100k tokens
- **Por qué cuesta**: Haiku 5.5 pasa de $0.10/$0.50 a $0.50/$2.50 por MTok (5x) por encima de 100,000 tokens de prompt [PRICE] "Model pricing", "Long context pricing"; además "costs more on prompts longer than 100K tokens" [MODEL].
- **Recomendación**: `/compact` o `/autocompact 100k` en sesiones con Haiku; subagentes Haiku con tareas acotadas.
- **Señal**: `usage` `model LIKE '%haiku-5-5%' AND ctx > 100000`, ≥ 5 por usuario. (Hoy no hay filas Haiku en la BD local.)

#### BP-24. Ignorar los límites del plan y de la API
- **Por qué cuesta**: suscripción: ventana de 5 h y semanal compartida entre modelos ("can't restore access by switching models") [COST] "When a developer asks about a limit". API: solo `input_tokens + cache_creation_input_tokens` cuentan para ITPM; `cache_read` no ("With a 2,000,000 ITPM limit and an 80% cache hit rate, you could effectively process 10,000,000") [RL] "Cache-aware ITPM". TPM recomendados por usuario según tamaño del equipo (p. ej. 200k-300k para 1-5 usuarios) [COST] "Rate limit recommendations".
- **Recomendación**: mirar `/usage` (barras y behavior flags) [COST]; repartir el trabajo pesado fuera del bloque crítico; en API, mejorar el hit de caché antes de pedir más límite [RL].
- **Señal**: `report.py` `windows()` (líneas 43-71) ya calcula el bloque de 5 h y la semana contra `limits.json`, pero solo se muestra en la sección "Límites", no en `findings()`. Umbral `[H]`: bloque actual > 90 % de `five_hour` o semana > 80 % de `weekly`. Errores 429: **no detectable** (falta `status_code` de `api_error`).

#### BP-25. No usar procesamiento por lotes para trabajo masivo no interactivo
- **Por qué cuesta**: la Batch API tiene "a 50% discount on both input and output tokens" y se combina con caché [PRICE] "Batch processing", FAQ "How do discounts stack?". En Claude Code, `/batch <instrucción>` reparte en 5-30 subagentes y `claude -p` en bucle con `--allowedTools` [BP] "Fan out across files".
- **Recomendación**: para scripts propios contra la API, Batch API en lo no urgente; para migraciones masivas, `/batch` o `claude -p` con subagentes en modelo barato.
- **Señal**: **no detectable** (no se registra `app.entrypoint` ni si la sesión es `-p`; OTEL lo trae como atributo estándar [OTEL]).

#### BP-26. Apps propias contra la API con caché mal configurado
- **Por qué cuesta**: breakpoint sobre un bloque variable ⇒ "You pay for a fresh cache write on every request and never get a read"; prompts por debajo del mínimo cacheable (512-4,096 tokens según modelo) no se cachean sin error; cambiar `tool_choice`, imágenes, thinking o effort invalida niveles del caché; claves JSON con orden inestable rompen el prefijo [PC-API] "What invalidates the cache", "Best practices". Imágenes base64 reenviadas en cada turno: usar Files API [VISION].
- **Recomendación**: caché automático (`cache_control` en el nivel superior), breakpoint en el último bloque estable, revisar `cache_creation_input_tokens`/`cache_read_input_tokens` [PC-API].
- **Señal**: fuera del alcance de `tokens.db` (solo mide Claude Code).

---

## 3. Tabla cruzada: recomendación vs `findings()` (`report.py`)

Líneas de `report.py` a 2026-10-07.

| BP | Recomendación | ¿Lo detecta `findings()`? | Dónde |
|---|---|---|---|
| BP-01 | `/clear` entre tareas | **Sí** | regla `ctx`: líneas 106-107 (cálculo), 130-131 (mensaje) |
| BP-02 | `/autocompact` en modelos 1M | **Parcial** (umbral 150k fijo, no sugiere `/autocompact`) | 106, 130-131 |
| BP-03 | Sonnet/Haiku en vez de Opus/Fable | **Parcial** (solo respuestas cortas; no mide % del gasto en Opus) | `model`: 124-126, 140-142 |
| BP-04 | Modelo barato en subagentes / equipos | **Parcial** (peso total de subagentes, no su modelo) | `sub`: 150-153 |
| BP-05 | No volver a contextos grandes tras el TTL | **Sí** | `idle`: 110, 113-115, 134-135 |
| BP-06 | Fijar modelo/effort/fast al inicio | **Parcial** (solo cambio de modelo) | `swap`: 116-118, 121, 136-137 |
| BP-07 | No tocar MCP/plugins a mitad de sesión | **Sí** (inferido) | `pref`: 119-121, 138-139 |
| BP-08 | CLAUDE.md < 200 líneas, skills, `paths:` | **No** | — |
| BP-09 | CLI en vez de MCP, `/mcp` | **Parcial** (solo resultados MCP grandes) | `big`: 146-149 |
| BP-10 | Rangos en Read, `Read(...)` deny | **Parcial** (promedio por herramienta, no por archivo) | `big`: 146-149 |
| BP-11 | Hook de filtrado de Bash | **Parcial** (misma regla `big`) | 146-149 |
| BP-12 | Prompts específicos, subagentes para investigar | **No** | — |
| BP-13 | `/clear` tras 2 correcciones, `/rewind` | **Parcial** (solo fallos de herramientas vía OTEL) | `err`: 154-161 |
| BP-14 | `/effort` bajo en tareas simples | **Parcial** (salida media alta, sin effort) | `out`: 127, 143-144 |
| BP-15 | Menos capturas / recortarlas | **No** | — |
| BP-16 | Evitar `/loop`, check-ins, inbound sobre contexto grande | **No** | — |
| BP-17 | `/compact` en caliente o `/rewind` | **No** | — |
| BP-18 | `promptCacheTtl: "1h"` con pausas 5-60 min | **Parcial** (cuenta las pausas en `idle`, el consejo no menciona el TTL) | 110, 134-135, `ADVICE["idle"]` línea 79 |
| BP-19 | Fast mode puntual | **No** (sin datos) | — |
| BP-20 | Plan mode en cambios grandes | **No** | — |
| BP-21 | Hooks que filtran, no que inyectan | **No** (sin datos) | — |
| BP-22 | Output styles breves | **No** (sin datos) | — |
| BP-23 | Haiku 5.5 < 100k | **No** | — |
| BP-24 | Vigilar límites del plan | **No** en `findings()` (sí en la sección "Límites") | `windows()` 43-71 |
| BP-25 | Batch API / `/batch` | **No** (sin datos) | — |
| BP-26 | Caché en apps propias | **No aplica** | — |

Resumen: **Sí** 3 (BP-01, BP-05, BP-07) · **Parcial** 11 · **No** 12 (de ellas 6 sin datos suficientes y 1 fuera de alcance).

---

## 4. Nuevas reglas propuestas para `findings()` (priorizadas)

Prioridad = (impacto en tokens × frecuencia observada en la BD local) / esfuerzo de implementación `[H]`. Todas usan solo el hilo principal (`side=0`) salvo indicación. Formato de fila de `findings()`: `[usuario, regla, severidad, título, detalle, tokens implicados, consejo, tokens evitables]`; severidades existentes: `alta`/`media` (añadir `baja` si se implementan las BAJO).

### P1. `boot` — Contexto de arranque pesado (BP-08, BP-09) · severidad `media`
- **Mensaje**: "Contexto de arranque pesado: la primera respuesta de tus sesiones parte de {mediana} tokens (referencia oficial ≈ 8k). Cada sesión y cada subagente lo vuelve a pagar."
- **Consejo**: "Ejecuta /context: deja CLAUDE.md en menos de 200 líneas, pasa flujos especializados a skills y reglas con `paths:`, desactiva con /mcp los servidores que no uses y evita `alwaysLoad`/`ENABLE_TOOL_SEARCH=false`."
- **Lógica**:
```sql
WITH f AS (SELECT user, session, MIN(ts) t FROM usage WHERE side=0 AND ts!='' GROUP BY user, session)
SELECT u.user, u.input+u.cache_create+u.cache_read AS ctx0
FROM f JOIN usage u ON u.user=f.user AND u.session=f.session AND u.ts=f.t AND u.side=0
WHERE u.input+u.cache_create+u.cache_read > 0;
```
  En Python: mediana de `ctx0` por usuario con ≥ 5 sesiones; alerta si > 40000 `[H]`. Tokens implicados = `SUM(ctx0)`; evitables ≈ `SUM(ctx0 - 8000)` (solo la primera escritura; no estima la relectura posterior, que es a 0.05-0.1x).
- **Por qué primero**: en esta VM la mediana es ≈ 73k (9x el ejemplo oficial), afecta a todas las sesiones y no hay ninguna regla que lo cubra.

### P2. Ampliar `ctx` — Contexto > 300k sin compactar (BP-02) · severidad `alta`
- **Mensaje**: "{n} sesiones superaron 300k tokens de contexto: con modelos de 1M el auto-compact no salta hasta ~967k."
- **Consejo**: "Usa /autocompact 200k (o \"autoCompactWindow\": 200000 en settings) y /compact <foco> en cortes naturales."
- **Lógica**: dentro del bucle de sesiones existente (líneas 103-107): `if max(ctx) > 300000: a["huge"][0] += 1; a["huge"][1] += sum(r[5] for r in rs); a["huge"][2] += sum(max(0, r[5]-200000) for r in rs)` (evitable: lectura por encima de 200k). Emitir como regla propia o subir la severidad de `ctx` con este detalle. 22 sesiones en la BD local.

### P3. `submodel` — Subagentes en modelo caro (BP-04) · severidad `media`
- **Mensaje**: "{caros} de {n} respuestas de subagentes usaron Opus/Fable."
- **Consejo**: "Pon `model: haiku` o `sonnet` en tus subagentes, define un `Explore` propio con `model: haiku` y/o usa CLAUDE_CODE_SUBAGENT_MODEL=haiku con CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1."
- **Lógica**:
```sql
SELECT user, COUNT(*) n,
  SUM(model LIKE '%opus%' OR model LIKE '%fable%') caros,
  SUM(CASE WHEN model LIKE '%opus%' OR model LIKE '%fable%' THEN input+cache_create+cache_read+output ELSE 0 END) tok
FROM usage WHERE side=1 GROUP BY user
HAVING n >= 20 AND caros*1.0/n > 0.3;
```
  Tokens evitables: no estimables en tokens (el ahorro es de precio, ~2x-40x según destino); dejar 0.

### P4. `explore` — Exploración sin acotar en el hilo principal (BP-12) · severidad `media`
- **Mensaje**: "{n} sesiones con más de 60 lecturas/búsquedas (Read/Grep/Glob) y más de 150k de contexto, sin usar subagentes."
- **Consejo**: "Acota el pedido (archivo, escenario, qué es 'terminado') o pide 'usa subagentes para investigar X' para que la exploración no se quede en tu contexto."
- **Lógica**:
```sql
SELECT a.user, a.session, SUM(a.calls) n
FROM attrib a
WHERE a.cat='Herramienta' AND a.name IN ('Read','Grep','Glob')
  AND NOT EXISTS (SELECT 1 FROM attrib s WHERE s.session=a.session AND s.cat='Subagente' AND s.calls>0)
  AND (SELECT MAX(input+cache_create+cache_read) FROM usage u WHERE u.session=a.session AND u.side=0) > 150000
GROUP BY a.user, a.session HAVING n >= 60;
```
  Alerta si ≥ 2 sesiones por usuario `[H]`.

### P5. `bg` — Llamadas automáticas con contexto completo (BP-16, = R14) · severidad `media`
- **Mensaje**: "{n} días con 10 o más respuestas mínimas (menos de 50 tokens) sobre contextos de más de 50k tras un rato de inactividad: probable /loop, check-ins de /goal o mensajes entre sesiones."
- **Consejo**: "Usa /loop solo en sesiones ligeras, CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0, \"crossSessionInbound\": \"hold\" y revisa la fila Loops de /usage."
- **Lógica** (vista `t` de `fugas-de-tokens.md` filtrada a `side=0`):
```sql
SELECT user, substr(ts,1,10) d, COUNT(*) n, SUM(ctx) tok
FROM t WHERE output < 50 AND ctx > 50000 AND gap_s > 120
GROUP BY 1,2 HAVING n >= 10;
```
  Tokens evitables ≈ `SUM(ctx)` de esas filas (lectura de caché + reescrituras).

### P6. `coldcompact` — Compactación en frío (BP-17) · severidad `media`
- **Mensaje**: "{n} compactaciones tras más de {TTL} de inactividad: el resumen releyó {tok} tokens sin caché."
- **Consejo**: "Compacta (/compact <foco>) mientras trabajas, no al volver; si no necesitas continuidad usa /clear, y para abandonar un camino /rewind."
- **Lógica**: en el bucle de pares consecutivos (líneas 112-120), además de lo existente: `if ctx[k] < 0.5*ctx[k-1] and ctx[k-1] > 100000 and rs[k][5] < 0.5*ctx[k] and gap > ttl: cold += 1; tok += ctx[k-1]`. Umbral `[H]`: ≥ 2 por usuario. Evitables ≈ `tok` (diferencia entre releer sin caché y con caché).

### P7. Ampliar `idle` — Sugerir `promptCacheTtl` cuando el TTL es 5 min (BP-18) · severidad `media`
- **Mensaje** (detalle añadido): "La mayoría de esas pausas duró entre 5 y 60 minutos con caché de 5 min."
- **Consejo** (si `ttl == 300` y ≥ 3 pausas con `300 < gap < 3600`): "Configura \"promptCacheTtl\": \"1h\" (o CLAUDE_CODE_PROMPT_CACHE_TTL=1h): la escritura cuesta 2x pero se amortiza con dos lecturas."
- **Lógica**: en la línea 114 contar por separado `300 < gap < 3600` cuando `ttl == 300`; cambiar el consejo de la fila `idle` según ese contador. Cambio mínimo, sin nueva consulta.

### P8. `limit` — Cerca del límite del plan (BP-24) · severidad `media`
- **Mensaje**: "El bloque de 5 h actual va por el {p}% del límite configurado (aporte de {usuario}: {x})."
- **Consejo**: "Revisa /usage; aplaza tareas pesadas (subagentes, /batch, sesiones grandes) hasta el próximo bloque."
- **Lógica**: reutilizar `plan_data(db)` / `windows(db, now)` (líneas 43-71 y 300-307): si `cur.tokens > 0.9*limits.five_hour` o `week > 0.8*limits.weekly` `[H]`, añadir una fila por usuario con su aporte. Sin SQL nuevo. Solo si `limits.json` tiene valores.

### P9. `noplan` — Cambios grandes sin plan mode (BP-20) · severidad `baja`
- **Mensaje**: "{n} sesiones con 30 o más ediciones sin pasar por plan mode."
- **Consejo**: "Para cambios en varios archivos o con enfoque incierto, Shift+Tab a plan mode antes de implementar; para cambios de una línea, no hace falta."
- **Lógica**:
```sql
SELECT user, session, SUM(CASE WHEN name IN ('Edit','Write') THEN calls ELSE 0 END) ed,
       SUM(CASE WHEN name='ExitPlanMode' THEN calls ELSE 0 END) pl
FROM attrib GROUP BY user, session HAVING ed >= 30 AND pl = 0;
```
  Alerta si ≥ 3 sesiones por usuario `[H]`.

### P10. `haikulong` — Haiku 5.5 con prompts > 100k (BP-23) · severidad `baja`
- **Mensaje**: "{n} respuestas de Haiku 5.5 con más de 100k de contexto, facturadas a 5x."
- **Consejo**: "Compacta antes de 100k (/autocompact 100k) o da a los subagentes Haiku tareas acotadas."
- **Lógica**:
```sql
SELECT user, COUNT(*) n, SUM(input+cache_create+cache_read) tok
FROM usage WHERE model LIKE '%haiku-5-5%' AND input+cache_create+cache_read > 100000
GROUP BY user HAVING n >= 5;
```

### Calibración con la BD local (2026-10-07, consultas ejecutadas)
| Regla | Resultado | Lectura |
|---|---|---|
| P1 | mediana de `ctx0` ≈ 73k | saltaría: es un hallazgo real |
| P2 | 22 sesiones > 300k | saltaría |
| P3 | 102 de 632 respuestas de subagente en Opus (16 %) | no salta con 30 %; ajustar si se quiere más sensibilidad |
| P4 | 9 sesiones (62-229 llamadas) | saltaría |
| P5 | 0 filas | sin uso de tareas en segundo plano |
| P9 | 25 sesiones | demasiado ruidosa: subir a ≥ 60 ediciones o dejarla informativa |

### Mejoras de datos que desbloquean reglas hoy "no detectables" (no implementadas)
1. `otel_receiver.py`: guardar `effort` y `speed` de `api_request` → BP-06 (effort), BP-14, BP-19. Guardar `status_code`/`attempt` de `api_error` → 429 en BP-24. Guardar `tool_result_size_bytes` y `error_type` de `tool_result` → tamaño real en BP-10/BP-11 [OTEL].
2. Añadir `user_prompt` a `KEEP` guardando solo `prompt_length` (nunca el texto) → prompts por sesión y BP-13 sin depender del proxy de `attrib` [OTEL].
3. `collect.py`: contar bloques `type='image'` por respuesta (mensajes de usuario y `tool_result`) → BP-15.
4. Guardar `app.entrypoint` de OTEL → distinguir `claude -p`/SDK de sesiones interactivas (BP-25) [OTEL].
