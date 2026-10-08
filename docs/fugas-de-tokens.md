# Fugas de tokens en Claude Code: qué medir con `tokens.db`

Consultado el 2026-10-07. Fuentes: docs oficiales (code.claude.com, platform.claude.com) y el código de `collect.py` / `otel_receiver.py`.
Todo umbral sin cita es **heurística propia** y está marcado `[H]`.

Abreviaturas de fuentes:
- **[COST]** https://code.claude.com/docs/en/costs
- **[PC-CC]** https://code.claude.com/docs/en/prompt-caching
- **[PC-API]** https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching
- **[OTEL]** https://code.claude.com/docs/en/monitoring-usage
- **[TOOLS]** https://code.claude.com/docs/en/tools-reference
- **[MCP]** https://code.claude.com/docs/en/mcp
- **[PERM]** https://code.claude.com/docs/en/permissions
- **[ENV]** https://code.claude.com/docs/en/env-vars
- **[CTX]** https://code.claude.com/docs/en/context-window

---

## 1. Cómo funciona el prompt caching (base de las reglas)

| Hecho | Cita |
|---|---|
| Claude Code reenvía el contexto completo en cada request (system prompt, CLAUDE.md, todos los mensajes y tool results). El caché reutiliza el **prefijo** idéntico; un cambio en el prefijo recomputa todo lo posterior. | [PC-CC] "How the cache is organized" |
| TTL por defecto de la API: 5 min. Opción de 1 h. Cada acierto (hit) **renueva** el TTL. | [PC-API] |
| Campos de uso: `input_tokens` (tras el último breakpoint), `cache_creation_input_tokens`, `cache_read_input_tokens`; input total = suma de los tres. Hay desglose `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`. | [PC-API] "Usage Response Fields" |
| TTL en Claude Code: conversación principal = **1 h** con suscripción dentro del uso del plan; **5 min** con API key, proveedor cloud o usage credits. Subagentes, workflows, compactación y títulos = **5 min** salvo que se configure. Se controla con `promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL` y `subagentPromptCacheTtl` (v2.1.242+). | [PC-CC] "Which TTL each request gets" |
| El caché es **por modelo**: `/model` hace que la siguiente request no tenga hits. También lo invalidan: cambiar el set de definiciones de herramientas (MCP sin tool search, deny de una herramienta entera), compactar, acumulación de imágenes, actualizar Claude Code, `/effort` en modelos antiguos y fast mode. | [PC-CC] "Actions that invalidate the cache" |
| El caché está acotado a máquina+directorio: sesiones en directorios distintos no comparten caché. | [PC-CC] "Cache scope" |
| Los subagentes empiezan su propio caché (no leen el del padre). | [PC-CC] "Subagents and the cache" |
| Tras un descanso mayor al TTL, el primer mensaje reprocesa todo el contexto. `/compact` en frío es lo más caro. | [COST] "Why usage climbs in a long session"; [PC-CC] "Compacting the conversation" |

Consecuencia: la lectura de caché **no es gratis**; en una sesión larga el contexto se relee en cada turno ([COST]: "so a one-line question in a session that has been open all day still draws usage for the whole conversation").

---

## 2. Qué datos tenemos y qué trae cada columna

Esquema real (`collect.py`, `otel_receiver.py`):
- `usage(id, user, project, session, model, ts, input, output, cache_create, cache_read)`. **No hay columna `isSidechain`** (el colector solo la usa para etiquetar `attrib`), así que las respuestas de subagentes van mezcladas con las principales en `usage`.
- `attrib(id, user, session, ts, cat, name, calls, out_tok, in_tok)`. `out_tok` reparte la salida de la respuesta entre sus herramientas; `in_tok` reparte `input+cache_create` de la respuesta **siguiente** entre las herramientas de la anterior. Es una **estimación**, no el tamaño real del resultado.
- `otel_events(ts, session, event, model, query_source, input, output, cache_read, cache_create, tool_name, success, duration_ms, skill, plugin, mcp_server)`.

Definiciones usadas:
- `ctx = input + cache_create + cache_read` (tamaño del prompt de esa respuesta) [PC-API].
- `hit = cache_read / ctx`.

Las consultas usan SQLite con funciones de ventana. Base común:

```sql
CREATE TEMP VIEW t AS
SELECT *, input+cache_create+cache_read AS ctx,
  LAG(ts)    OVER w AS prev_ts,
  LAG(model) OVER w AS prev_model,
  LAG(input+cache_create+cache_read) OVER w AS prev_ctx,
  (julianday(ts)-julianday(LAG(ts) OVER w))*86400 AS gap_s
FROM usage WINDOW w AS (PARTITION BY user, session ORDER BY ts);
```

---

## 3. Patrones, cálculo, umbral y recomendación

### R1. Contexto grande releído en cada turno (sesión larga sin `/clear` ni `/compact`)
- **Cálculo**: por sesión, `AVG(cache_read)`, `MAX(ctx)`, nº de respuestas y `SUM(cache_read)` como parte del total de tokens de entrada.
- **Umbral**: `[H]` sesión con `MAX(ctx) > 150k` y más de 40 respuestas, o `SUM(cache_read)` > 10 M por sesión. Fuente de la lógica: [COST] ("Long context", "Clear between tasks: stale context wastes tokens on every subsequent message"). [COST] marca como "behavior flag" lo que supera **10 %** del uso reciente: úsalo como umbral para "esta sesión/usuario concentra X % de los tokens".
- **Recomendación**: `/clear` al cambiar de tarea (con `/rename` antes y `/resume` después), `/compact <instrucciones>` en cortes naturales, `/context` para ver qué ocupa el contexto, `/rewind` en vez de seguir por un camino equivocado. Fuente: [COST] "Manage context proactively", [PC-CC] tip de `/compact`.

### R2. Ausencia de `/clear` o `/compact` (no cae nunca el contexto)
- **Cálculo**: `ctx < 0.5*prev_ctx` en filas consecutivas ⇒ hubo `/clear`, `/compact` o `/rewind` (en `/clear` normalmente cambia `session`). Contar caídas por sesión.
- **Umbral**: `[H]` sesión con `MAX(ctx) > 150k` y **cero** caídas. El auto-compact existe como red de seguridad ([COST] "A context or auto-compact warning"), por lo que llegar a él es la señal.
- **Recomendación**: igual que R1; adelantar auto-compact con `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (p. ej. 50) o `autoCompactWindow` / `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100000-1000000). Fuente: [ENV].

### R3. Hit ratio bajo / `cache_create` repetido
- **Cálculo**: por sesión excluyendo la primera respuesta, `SUM(cache_read)*1.0/SUM(ctx)`; y recuento de filas con `cache_create > 0.5*ctx` (reconstrucción casi completa).
- **Umbral**: `[H]` hit < 80 % o más de 3 reconstrucciones por sesión. Referencia cualitativa: "if creation stays high turn after turn, something is changing in your prefix" [PC-CC] "Check cache performance"; el ejemplo de [COST] muestra 91 % (solo ilustrativo).
- **Recomendación**: revisar R4-R6 (causas). El propio `/usage` muestra `Prompt cache (main)` con aciertos, fallos y "likely cause" (v2.1.251+/2.1.260+) [COST]. Fijar modelo y esfuerzo al inicio [PC-CC] tip.

### R4. Expiración del caché por inactividad
- **Cálculo**: `gap_s > TTL AND cache_create > 0.5*ctx` en la fila posterior a la pausa. `TTL` = 3600 (suscripción, hilo principal) o 300 (API key / usage credits / subagentes). Tokens evitables ≈ `cache_create` de esa fila.
- **Umbral**: `[H]` ≥ 20k tokens re-escritos tras una pausa, y ≥ 3 eventos/semana por usuario. El mecanismo está en [COST] "Cache misses" y [PC-CC] "Cache lifetime".
- **Recomendación**: si se va a volver tras horas, `/clear` o cerrar la tarea en vez de reanudar un contexto enorme; en Pro/Max Claude Code ofrece "resume from a summary" al reanudar sesiones grandes ([COST]); en API key, `promptCacheTtl: "1h"` solo si hay pausas frecuentes de 5-60 min. Evitar `/loop` y tareas programadas sobre contextos grandes ([COST] "Scheduled tasks").

### R5. Cambio de modelo a mitad de sesión (cache por modelo)
- **Cálculo**: `model <> prev_model` en misma sesión y `cache_read = 0` o `cache_create > 0.5*ctx`.
- **Umbral**: `[H]` ≥ 2 cambios por sesión con `ctx > 50k`. Mecanismo oficial: [PC-CC] "Switching models" (incluye `opusplan`, fallback automático, skills con `model:` en frontmatter).
- **Advertencia de falso positivo**: los subagentes (otro modelo, otra sesión de caché) comparten `session` con el hilo principal y no hay `isSidechain` en `usage`, por lo que se verán como "cambios de modelo". Cruzar con `attrib.cat='Subagente'` o usar `otel_events.query_source`.
- **Recomendación**: elegir modelo al inicio; cambiar entre tareas con `/clear`, no con `/model` en pleno contexto grande. Claude Code ya pide confirmación mientras el caché está caliente [PC-CC].

### R6. Prefijo inestable (herramientas/MCP/plugins cambian)
- **Cálculo**: reconstrucción masiva en una fila sin pausa (`gap_s < TTL`) y sin cambio de modelo: `cache_create > 0.5*ctx AND gap_s < 300 AND model = prev_model`.
- **Umbral**: `[H]` ≥ 2 por sesión. Causas oficiales: servidor MCP conectado/eliminado sin tool search, deny de herramienta completa, plugin con MCP, muchas imágenes, fast mode, upgrade de Claude Code [PC-CC].
- **Recomendación**: no añadir/quitar servidores MCP ni plugins con MCP a mitad de sesión; `/mcp` para desactivar los que no se usan **antes** de empezar [COST]; `DISABLE_AUTOUPDATER=1` para controlar cuándo se actualiza [PC-CC].

### R7. Modelo caro para tareas simples
- **Cálculo**: por usuario y modelo, nº de respuestas y `AVG(output)`; filtrar respuestas Opus/Fable con `output < 300` y `ctx < 30k` (tareas triviales) o con `attrib` solo de Read/Grep/Glob/Bash.
- **Umbral**: `[H]` > 50 % de las respuestas Opus/Fable (mínimo 20) son cortas. Docs: "Sonnet handles most coding tasks well and costs less than Opus. Reserve Opus for complex architectural decisions" [COST] "Choose the right model".
- **Recomendación**: `/model` al inicio o `/config` para default; `model: haiku` en subagentes simples [COST]; `opusplan` solo si se acepta que cada cambio de modo es un cambio de modelo (R5) [PC-CC].

### R8. Resultados de herramientas enormes (Read completo, Bash largo, Grep amplio)
- **Cálculo**: `SELECT user, name, SUM(in_tok)/SUM(calls) AS tok_por_llamada FROM attrib WHERE cat='Herramienta' GROUP BY user, name`. Con OTEL (si se guardara `tool_result_size_bytes`) sería exacto [OTEL].
- **Umbral**: `[H]` > 10k tokens estimados por llamada en promedio, o una herramienta con > 25 % del `in_tok` total del usuario. Referencias oficiales de tamaño: advertencia MCP a 10,000 tokens, límite por defecto 25,000 tokens [MCP]; Bash devuelve inline hasta ~30,000 caracteres [TOOLS] "Output limits"; 1 token ≈ 4 caracteres [PRICE] FAQ.
- **Recomendación**:
  - Leer rangos (`offset`/`limit`) y pedir a Claude `Grep` acotado con `head_limit` ([TOOLS]).
  - `Read(./ruta/**)` en `permissions.deny` para `node_modules`, `dist`, binarios, logs (`.claudeignore` **no tiene efecto**, hay que migrarlo a reglas `Read` deny) [PERM].
  - Hook PreToolUse que filtra la salida de tests/logs (ejemplo oficial con `grep ... | head -100`) [COST] "Offload processing to hooks and skills".
  - Reducir `bashOutputMaxChars` (hasta 128000, por defecto ~30000) o `BASH_MAX_OUTPUT_LENGTH` [TOOLS][ENV]; `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` para el límite de Read [ENV]; `MAX_MCP_OUTPUT_TOKENS` para MCP [MCP].
  - Delegar salidas voluminosas a un subagente [COST]. Plugins de code intelligence para evitar lecturas de archivos al navegar símbolos [COST].

### R9. Herramientas MCP con definiciones pesadas / servidores MCP con mucho peso
- **Cálculo**: `attrib WHERE cat IN ('MCP','Plugin')`: `SUM(in_tok)` y `SUM(calls)` por `name` (servidor); servidores con `in_tok` alto y pocas llamadas.
  Para el peso fijo de definiciones: tamaño de la **primera** respuesta de cada sesión, `cache_create + input + cache_read` donde sea la fila mínima por `ts`.
- **Umbral**: `[H]` primera respuesta con `ctx > 30k` (el arranque típico del ejemplo oficial suma ~8k tokens: system 4.2k, memoria 0.7k, CLAUDE.md 2.1k, skills 0.45k, MCP diferido 0.12k [CTX]); servidor con > 10 % de `in_tok` del usuario y < 5 llamadas.
- **Nota**: por defecto las definiciones MCP están **diferidas** (solo nombres en contexto) [COST][MCP], así que el gasto visible es el de sus **resultados**, no el de sus esquemas. Con `ANTHROPIC_BASE_URL` personalizado o `ENABLE_TOOL_SEARCH=false` se cargan por adelantado [MCP][CTX].
- **Recomendación**: `/mcp` para desactivar servidores sin uso; preferir CLIs (`gh`, `aws`, etc.) a MCP [COST]; `/context` para inspeccionar; CLAUDE.md < 200 líneas y mover flujos especializados a skills [COST].

### R10. Subagentes y equipos con mucho gasto
- **Cálculo**: `attrib WHERE cat='Subagente'`: `SUM(out_tok+in_tok)` / total del usuario. Mejor con OTEL: `SUM(tokens) WHERE query_source='subagent'`.
- **Umbral**: > 10 % del uso reciente (mismo umbral de flag de [COST] "Plan usage breakdown"); `[H]` > 30 % es sospechoso. Los equipos de agentes usan ~7x tokens de una sesión estándar en plan mode [COST].
- **Recomendación**: modelo pequeño en subagentes (`model: haiku` en su frontmatter, o un único modelo para todos), equipos pequeños, cerrar compañeros que terminaron, prompts de spawn cortos [COST] "Agent team token costs" / "Delegate verbose operations". Recordar que sus requests usan TTL de 5 min por defecto (`subagentPromptCacheTtl`) [PC-CC].

### R11. Salida / razonamiento excesivo
- **Cálculo**: `AVG(output)` y `SUM(output)` como parte del total del usuario; filas de `attrib` con `name='texto y razonamiento'`.
- **Umbral**: `[H]` `AVG(output) > 4000` por respuesta o `output` > 40 % de los tokens del usuario. El thinking se factura como output y su presupuesto por defecto "can be tens of thousands of tokens per request" [COST] "Adjust extended thinking".
- **Recomendación**: `/effort` (o `/model`) para bajar esfuerzo en tareas simples; `MAX_THINKING_TOKENS` solo en modelos con presupuesto fijo (en los adaptativos se ignora) [COST]; Prompts específicos y plan mode (Shift+Tab) antes de implementar; Esc / `/rewind` para corregir pronto [COST].

### R12. Errores y reintentos de herramientas (solo con OTEL)
- **Cálculo**: `SELECT session, tool_name, SUM(success='false')*1.0/COUNT(*) FROM otel_events WHERE event='tool_result' GROUP BY 1,2`; `event='api_error'` por sesión. Los eventos `tool_result` traen `success`, `duration_ms`, `error_type` y `api_error` trae `attempt` [OTEL].
- **Umbral**: `[H]` tasa de fallo > 15 % con ≥ 20 llamadas; ≥ 3 `api_error` por sesión.
- **Recomendación**: corregir permisos, rutas o comandos que fallan repetidamente (reglas `permissions.allow` y CLAUDE.md con comandos correctos); plan mode y verificación incremental [COST].

### R13. Sesiones que se reanudan viejas y pesadas
- **Cálculo**: primera fila tras `gap_s > 24h` con `prev_ctx > 100k`; tokens evitables = `cache_create` re-escrito.
- **Umbral**: `[H]` ≥ 100k tokens re-escritos al reanudar. Mecanismo y alternativa: [COST], [PC-CC] "Resuming a session" y "resume from a summary".
- **Recomendación**: aceptar el resumen al reanudar o abrir sesión nueva con una nota breve de estado.

### R14. Llamadas automáticas con contexto completo
- **Cálculo**: filas con `gap_s` regular (± 5 %) y `output` muy bajo (`< 50`) sobre `ctx` alto; con OTEL, `query_source='auxiliary'`.
- **Umbral**: `[H]` ≥ 10 respuestas/día con `output < 50` y `ctx > 50k`. Fuentes: tareas programadas, mensajes entre sesiones, check-ins de goal y prompt suggestions reenvían el contexto completo [COST] "Why usage climbs".
- **Recomendación**: `/loop` solo sobre sesiones ligeras; `crossSessionInbound: hold`; `CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0`; desactivar prompt suggestions [COST].

---

## 4. Qué NO se puede detectar con los datos actuales

1. **Qué archivo/comando/consulta causó un resultado grande**. `attrib.in_tok` es una estimación repartida a partes iguales entre las herramientas de la respuesta previa (`collect.py`); no hay tamaño real ni ruta. Con OTEL se tienen `tool_result_size_bytes` y, con `OTEL_LOG_TOOL_DETAILS=1`, `tool_parameters` [OTEL], pero `otel_events` no almacena esas columnas hoy.
2. **Lecturas repetidas del mismo archivo** (requiere `tool_parameters`, R8) y **calidad del prompt** (requiere `OTEL_LOG_USER_PROMPTS=1`, sensible).
3. **Si la escritura de caché fue 5 min o 1 h**: `usage` no guarda `cache_creation.ephemeral_*` [PC-API]. Tampoco se sabe si la cuenta estaba en plan o en usage credits, que cambia el TTL [PC-CC].
4. **Separar hilo principal de subagentes en `usage`** (falta `isSidechain`; las reglas R5/R6/R13 pueden dar falsos positivos). OTEL sí tiene `query_source`.
5. **Causa exacta de un fallo de caché**. Solo lo reporta `/usage` ("likely cause") en la máquina del usuario [COST]; aquí solo se infiere.
6. **Esfuerzo (`effort`), fast mode y tokens de thinking por separado**: `effort` y `speed` existen en OTEL [OTEL]; el thinking va dentro de `output`.
7. **Peso real de las definiciones MCP/CLAUDE.md/skills**: con tool search están diferidas; solo se ve un total de arranque (R9). `/context` lo desglosa dentro de la sesión [COST].
8. **Uso en límites del plan (suscripción)**. `/usage` muestra barras de plan y la atribución por skill/subagente/plugin/MCP solo en la máquina local [COST].
9. **Consumo fuera de esta VM** (otros dispositivos, claude.ai). `/usage` también es local [COST].
10. **Cuenta compartida**: el reparto por usuario es por usuario de SO (directorio de `~/.claude/projects`); si dos personas usan el mismo usuario de SO, no se distinguen.
11. **Ahorro real de una recomendación** (contrafactual): solo estimable.

---

## 5. Tabla lista para implementar

`TTL` = 3600 o 300 según plan. Todas las consultas parten de la vista `t` de la sección 2.

| Regla | Cálculo SQL/Python | Umbral | Recomendación | Fuente |
|---|---|---|---|---|
| R1 Contexto grande releído | `SELECT user,session,COUNT(*) n,MAX(ctx) mx,SUM(cache_read) cr FROM t GROUP BY 1,2 HAVING mx>150000 AND n>40` | `[H]` ctx>150k y n>40; flag si >10 % del uso | `/clear` entre tareas, `/compact <foco>`, `/context` | [COST] |
| R2 Sin compactar/limpiar | por sesión `SUM(ctx<0.5*prev_ctx)=0` y `MAX(ctx)>150k` | `[H]` 0 caídas | `/compact` en cortes; `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` | [COST][ENV] |
| R3 Hit bajo | `SUM(cache_read)*1.0/SUM(ctx)` por sesión (sin 1ª fila) | `[H]` <80 % o >3 reconstrucciones | ver R4-R6; `/usage` línea "Prompt cache" | [PC-CC][COST] |
| R4 Caché expirado | `gap_s>TTL AND cache_create>0.5*ctx`; evitable `cache_create` | `[H]` ≥20k tokens, ≥3/semana | `/clear` si la pausa es larga; resume desde resumen; `promptCacheTtl` | [COST][PC-CC] |
| R5 Cambio de modelo | `model<>prev_model AND (cache_read=0 OR cache_create>0.5*ctx)` | `[H]` ≥2/sesión con ctx>50k (descartar subagentes) | elegir modelo al inicio; `/clear` antes de `/model` | [PC-CC] |
| R6 Prefijo inestable | `cache_create>0.5*ctx AND gap_s<300 AND model=prev_model` | `[H]` ≥2/sesión | no tocar MCP/plugins a mitad de sesión; `/mcp` antes | [PC-CC] |
| R7 Modelo caro, tarea simple | filas Opus/Fable con `output<300 AND ctx<30000` | `[H]` >50 % de las respuestas Opus/Fable (≥20) | `/model` Sonnet/Haiku; `model: haiku` en subagentes | [COST] |
| R8 Resultados enormes | `SELECT user,name,SUM(in_tok)/SUM(calls) FROM attrib WHERE cat='Herramienta' GROUP BY 1,2` | `[H]` >10k tok/llamada o >25 % del `in_tok` (ref. MCP: aviso 10k, límite 25k) | `Read` con rango, `Read(...)` deny, hook de filtrado, `bashOutputMaxChars`, subagente | [TOOLS][MCP][PERM][COST] |
| R9 MCP/plugin pesado | `attrib WHERE cat IN('MCP','Plugin')`: `SUM(in_tok)`, `SUM(calls)`; 1ª respuesta de sesión `ctx` | `[H]` 1ª `ctx`>30k; servidor >10 % `in_tok` y <5 llamadas | `/mcp` desactivar; preferir CLI; CLAUDE.md <200 líneas; skills | [COST][MCP][CTX] |
| R10 Subagentes | `SUM(in_tok+out_tok) WHERE cat='Subagente'` / total; OTEL `query_source='subagent'` | >10 % (flag oficial); `[H]` >30 % sospechoso | `model: haiku`, equipos pequeños, cerrar compañeros | [COST] |
| R11 Output/thinking alto | `AVG(output)`; `SUM(output)` / total del usuario | `[H]` avg>4000 o >40 % de los tokens | `/effort`, plan mode, prompts específicos, `/rewind` | [COST] |
| R12 Errores de herramientas | `otel_events` `tool_result`: `AVG(success='false')`; `api_error` por sesión | `[H]` >15 % con ≥20 llamadas; ≥3 api_error | corregir permisos/comandos; verificación incremental | [OTEL][COST] |
| R13 Reanudar sesión vieja | primera fila con `gap_s>86400 AND prev_ctx>100000`; `cache_create` | `[H]` ≥100k re-escritos | aceptar resume-from-summary o sesión nueva | [COST][PC-CC] |
| R14 Llamadas automáticas | `gap_s` regular y `output<50 AND ctx>50000`; OTEL `query_source='auxiliary'` | `[H]` ≥10/día | `/loop` ligero, `crossSessionInbound: hold`, `CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0` | [COST] |

### Notas de implementación
- Los umbrales `[H]` son punto de partida: calibrar con la mediana y el percentil 90 de la propia VM antes de alertar.
- Para ver el efecto de una regla, comparar los tokens de las filas marcadas con el total del usuario y priorizar por ahorro potencial.
- **Tokens evitables** (columna del reporte, estimación): R1 lectura de caché por encima de 150k de contexto; R3-R6 `cache_create` de las reconstrucciones (sin contar dos veces la misma fila); R8 tokens por encima de 10k por llamada; R11 salida por encima de 4000 por respuesta. R7, R10 y R12 no se estiman.
- Mejoras de datos que desbloquearían reglas (no implementadas): guardar `isSidechain` y `cache_creation.ephemeral_*` en `usage`; guardar `tool_result_size_bytes`, `effort`, `error_type` y `tool_parameters` en `otel_events`.
