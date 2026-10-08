# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

TokenCounter mide cuántos tokens de Claude Code gasta cada usuario de una VM Windows compartida (una sola cuenta de Claude) y genera un reporte HTML. Solo stdlib de Python (3.8+), sin dependencias. Todo el texto de cara al usuario (código, comentarios, reporte, docs) está en español. `LEEME.md` es la documentación de usuario: mantenla al día cuando cambie el comportamiento.

## Comandos

```powershell
python src\collect.py [--root C:\Users] [--db data\tokens.db]     # transcripts -> tokens.db (idempotente)
python src\report.py [--db data\tokens.db] [--out data\report.html]
python src\otel_receiver.py [--port 4318] [--dump x.jsonl]        # receptor OTel en primer plano
python tests\test_collect.py   # cada test_*.py es un script con asserts; imprime "ok"
python tests\test_otel.py
python tests\test_report.py
```
Los tests no usan fixtures ni framework (también corren con `pytest tests\test_x.py::test_nombre`); cada uno añade `src/` a `sys.path`. Para correr uno solo sin pytest: `cd tests; python -c "import test_collect as t; t.test_attrib()"`.

## Estructura

`src/` código Python · `scripts/` install/uninstall.ps1 (calculan la raíz con `Split-Path $PSScriptRoot`) · `tests/` · `config/limits.json` · `docs/` (incluye `ejemplo.html`) · `data/` salidas locales (`tokens.db`, `report.html`; en `.gitignore`, los scripts la crean con `makedirs`). `install.cmd` / `uninstall.cmd` quedan en la raíz para el doble clic. Las rutas por defecto se calculan desde la raíz del proyecto (`ROOT` en `report.py`), no desde el directorio actual.

## Arquitectura

Tres procesos escriben/leen la misma `data/tokens.db` (SQLite, WAL):

1. **`collect.py`** (tarea `TokenCounter`, SYSTEM, cada 10 min): recorre `C:\Users\*\.claude\projects\**\*.jsonl`. El usuario es el nombre de la carpeta del perfil. Tabla `files` guarda el mtime para saltar archivos sin cambios. Los transcripts son entrada no confiable leída como SYSTEM: `transcripts()` no atraviesa reparse points (`is_link`) y `parse_file` valida campos (`toint`, `txt`, `stamp`).
   - `usage`: una fila por respuesta, clave `user:message.id`. Una respuesta ocupa varias líneas del transcript; se queda la de mayor `output_tokens` (el UPSERT usa `>=` para que reprocesar rellene columnas nuevas). `side` = subagente (`isSidechain`).
   - `attrib`: estimación de en qué se gastan tokens. La salida se reparte entre las herramientas de esa respuesta; la entrada nueva (input + cache_create) entre las herramientas de la respuesta **anterior** (sus resultados entraron al contexto). Categorías en `categorize()`: Herramienta, MCP, Plugin, Skill, Subagente, Sistema.
   - Migraciones: si falta una columna se hace `ALTER TABLE` y se borra `files` para reprocesar todo; `PRAGMA user_version` marca versiones de la validación (también reprocesa).
2. **`otel_receiver.py`** (tarea `TokenCounterOtel`, siempre activa): receptor OTLP/HTTP-JSON en 127.0.0.1:4318 → tablas `otel_events` / `otel_metrics`. Guarda solo atributos de lista blanca (nunca prompts ni parámetros). El usuario se deduce después cruzando `session` con `usage`. `managed_settings.py apply|remove` fusiona/quita las variables `OTEL_*` en `C:\Program Files\ClaudeCode\managed-settings.json` (con copia `.bak-tokencounter`).
3. **`report.py`**: un único HTML autocontenido. `PAGE` es una plantilla con marcadores `__DAILY__`, `__SESSIONS__`, `__ATTRIB__`, `__OAPI__`, `__OTOOLS__`, `__FINDINGS__`, `__PLAN__`, `__GEN__` que `build()` sustituye por JSON en **una sola pasada** (`re.sub`, con `js()` escapando `<>&`, porque los datos vienen de los usuarios). La página tiene una CSP sin red: no añadir recursos externos; el filtrado, gráficos y secciones (`#resumen`, etc.) son JS en el navegador. `windows()` calcula el bloque de 5 h y la semana contra `config/limits.json`; `findings()` produce las recomendaciones por usuario con umbrales heurísticos (ver `docs/fugas-de-tokens.md` y `docs/buenas-practicas-tokens.md` para las reglas, fuentes y lo no detectable). Estilo visual: `docs/DESIGN.md`.

`test_report.py` importa `SCHEMA` de `collect.py` para crear una BD en memoria: cambios de esquema afectan a ambos.

## Instalación

`scripts/install.ps1` / `scripts/uninstall.ps1` (como admin; `install.cmd` / `uninstall.cmd` se autoelevan para doble clic). El instalador rechaza un Python modificable por usuarios, restringe la carpeta del programa a SYSTEM/Admins, crea `C:\TokenReports` (lectura para usuarios), el recurso SMB solo con `-Share` y las tareas (con `python -I`), y salvo `-NoOtel` activa OTel. Las tareas corren como SYSTEM: ningún archivo que ejecuten o lean puede ser escribible por usuarios. `data/` son datos/salidas locales, no código.
