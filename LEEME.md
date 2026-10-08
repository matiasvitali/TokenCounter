# TokenCounter

Controla cuántos tokens de Claude Code gasta cada usuario de la VM Windows, cuándo y en qué proyecto, sesión y modelo. Genera un reporte HTML.

## Cómo funciona

1. `src/collect.py` lee los transcripts de Claude Code de cada usuario (`C:\Users\*\.claude\projects\**\*.jsonl`) y los guarda en `data/tokens.db` (SQLite).
2. `src/report.py` genera `report.html` a partir de esa base.
3. Una tarea programada corre ambos como SYSTEM al arrancar la VM y cada 10 minutos.

Claude Code borra sus transcripts a los 30 días, pero `tokens.db` conserva el histórico desde la primera pasada.

## Estructura

```
TokenCounter/
├── install.cmd / uninstall.cmd       doble clic: instalan o desinstalan (piden permisos de administrador)
├── LEEME.md                         esta guía
├── config/limits.json               límites de tokens de tu plan (ver más abajo)
├── src/                             código: collect.py, report.py, otel_receiver.py, managed_settings.py
├── scripts/                         install.ps1, uninstall.ps1
├── tests/                           pruebas (test_collect.py, test_otel.py, test_report.py)
├── docs/                            documentación técnica y un reporte de ejemplo (ejemplo.html)
└── data/                            se crea al usarlo: tokens.db y el report.html local (no se versiona)
```

## Requisitos

- Windows con PowerShell y acceso de administrador.
- Python 3.8 o superior, instalado **para todos los usuarios** (por ejemplo en `C:\Program Files`). La tarea corre como SYSTEM: si un usuario pudiera modificar la carpeta de Python, podría ejecutar código como SYSTEM. Por eso el instalador se niega a usar un Python instalado en un perfil de usuario, el de la Microsoft Store o uno cuya carpeta puedan modificar Usuarios, Usuarios autenticados, Todos o INTERACTIVE. No hay que instalar paquetes.

## Instalación

1. Copia esta carpeta a una ruta fija, por ejemplo `C:\TokenCounter`. No la dejes dentro de un perfil de usuario.
2. Haz doble clic en `install.cmd` y acepta el aviso de administrador. Para desinstalar, usa `uninstall.cmd`.
   Si prefieres hacerlo a mano, abre PowerShell **como administrador** y ejecuta:
   ```powershell
   cd C:\TokenCounter
   .\scripts\install.ps1
   ```
   Si PowerShell bloquea el script, usa `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1`.
3. Si `python` no está en el PATH del administrador, indica la ruta:
   ```powershell
   .\scripts\install.ps1 -Python "C:\Program Files\Python312\python.exe"
   ```

Opciones: `-ReportDir` cambia la carpeta del reporte (por defecto `C:\TokenReports`). `-Share` publica además esa carpeta en red, y `-ShareName` cambia el nombre del recurso (por defecto `TokenReports`).

El instalador hace esto:
- Comprueba que Python no pueda ser modificado por usuarios normales (ver Requisitos).
- Deja la carpeta del programa (código y `data\tokens.db`, que contiene los datos de todos) accesible **solo para SYSTEM y Administradores**. Los usuarios normales no pueden leerla ni modificarla.
- Crea `C:\TokenReports`, con escritura para SYSTEM y Administradores y lectura para todos los usuarios.
- Solo con `-Share`: comparte esa carpeta como `\\<NOMBRE-VM>\TokenReports`, de solo lectura para `BUILTIN\Users`. Si la VM está en un dominio, eso incluye a Domain Users.
- Registra la tarea programada `TokenCounter` y la ejecuta una vez. Python corre en modo aislado (`-I`): ignora las variables `PYTHON*` y los paquetes del usuario.

## Verificar que funciona

1. Espera un minuto y abre `C:\TokenReports\report.html` en el navegador.
2. Comprueba que aparecen **todos** los usuarios:
   ```powershell
   python -c "import sqlite3;print(sqlite3.connect(r'C:\TokenCounter\data\tokens.db').execute('select user,count(*) from usage group by 1').fetchall())"
   ```
   Si falta alguno, SYSTEM no puede leer su `.claude`. Da permiso de lectura a SYSTEM en `C:\Users\<usuario>\.claude`.
3. Reinicia la VM y confirma que `report.html` se vuelve a actualizar (mira la hora en "Generado").

## Ver el reporte

Los usuarios abren `C:\TokenReports\report.html` (o `\\<NOMBRE-VM>\TokenReports\report.html` si se instaló con `-Share`). El menú del encabezado separa el reporte en secciones (Resumen, Límites, Gasto, OpenTelemetry, Recomendaciones, Sesiones; la sección queda en la URL como `#resumen`, etc.) y los filtros de usuario y fechas del encabezado aplican a todas. El estilo sigue `docs/DESIGN.md` (Huly: tema oscuro, acento azul iris y coral, controles en píldora; usa fuentes del sistema: el reporte no hace ninguna petición externa y su Content-Security-Policy bloquea cualquier conexión de red). Muestra:
- Totales de entrada, salida, caché y número de llamadas.
- Tokens por día.
- Desglose por usuario, modelo y proyecto.
- Las últimas 50 sesiones.

Todos los usuarios ven el consumo de los demás.

## Porcentaje de la suscripción (`config/limits.json`)

Todos los usuarios de la VM comparten **una sola cuenta de Claude**, así que hay un único límite por periodo y todos se miden contra él. Arriba del reporte hay:

- **Tarjeta "Cuenta compartida":** el consumo total de la cuenta en el bloque de 5 h actual, el bloque de 5 h anterior y la semana, como porcentaje del límite.
- **Una tarjeta por usuario ("aporte a la cuenta"):** cuánto consumió ese usuario en los mismos periodos, como porcentaje del **mismo límite**. Los porcentajes de los usuarios suman el de la cuenta.

La barra pasa a amarillo al 70 % y a rojo al 90 %.

- **Bloque de 5 h:** se calcula con los mensajes de todos los usuarios mezclados. Empieza con el primer mensaje de cualquiera y dura 5 horas. Si el último bloque ya terminó, no hay bloque actual y el anterior es ese.
- **Semana:** los últimos 7 días móviles. No coincide con el día de reinicio que usa Anthropic.
- **Qué se cuenta:** entrada + salida + caché escrito. El caché leído no se suma.
- **Los límites son tuyos:** Anthropic no publica los límites en tokens ni vienen en los transcripts. Edita `config/limits.json` con los valores de tu plan. Los que trae son solo de ejemplo. Sin límite configurado, la barra muestra solo los tokens.
  ```json
  { "five_hour": 5000000, "weekly": 50000000 }
  ```

Para calibrar, compara el porcentaje que muestra Claude Code con `/usage` en un momento dado y ajusta el límite hasta que coincida.

## En qué se gastan los tokens

El reporte tiene dos paneles, con fuentes distintas:

**1. "En qué se gastan los tokens" (siempre disponible, es una estimación).** Sale de los transcripts. Reparte los tokens por tipo: herramientas nativas (`Bash`, `Read`, `Edit`…), MCP, Skills, Plugins y Subagentes.
- La **salida** de cada respuesta se reparte entre las herramientas que llamó. Sin herramientas va a "texto y razonamiento".
- La **entrada nueva** (entrada + caché escrito) se atribuye a las herramientas de la respuesta anterior, porque sus resultados fueron lo último que entró al contexto. Si lo anterior fue un mensaje del usuario, va a "prompt / contexto inicial".
- El **contexto re-leído** (caché leído) aparece en la tarjeta "Cache leído". No se reparte por herramienta.
- Los transcripts **no** traen cuánto del contexto es prompt del sistema, definiciones de herramientas, skills cargados o CLAUDE.md. Eso solo se ve en vivo con `/context`.

**2. "Detalle exacto (OpenTelemetry)" (solo si el receptor está activo).** Claude Code envía eventos por cada llamada a la API y cada herramienta a un receptor local (`src/otel_receiver.py`, puerto 4318 de la propia VM). Muestra el número de llamadas, el origen de cada llamada (principal, subagente o auxiliar, que los transcripts no muestran) y, por herramienta, usos, porcentaje de éxito y duración. No incluye el skill, plugin o MCP: eso requeriría `OTEL_LOG_TOOL_DETAILS`, que envía detalles de las herramientas (comandos, rutas) y está desactivado por seguridad. La sección "Gasto" sí los muestra, a partir de los transcripts.
- Es opcional: el instalador lo activa salvo que uses `-NoOtel`.
- Registra tareas `TokenCounterOtel` (receptor siempre activo y con reinicio automático) y escribe `C:\Program Files\ClaudeCode\managed-settings.json` con las variables `OTEL_*`. Si el archivo ya existía, **se fusiona** y se guarda una copia `.bak-tokencounter`. El desinstalador quita solo lo que puso.
- **Solo cubre las sesiones de Claude Code iniciadas después de activarlo.** Las que ya estaban abiertas no envían nada, y si el receptor está apagado los eventos se pierden.
- El usuario de cada evento se deduce cruzando el `session.id` con los transcripts. Por eso aparece tras la siguiente pasada del colector.
- Si faltan datos, el reporte lo avisa: "Falta información de OpenTelemetry para N de M sesiones". La tabla de sesiones tiene una columna "OTel" (sí/no).
- Privacidad: solo se guardan nombres de herramienta, modelo, tokens, éxito y duración. Los prompts, respuestas y parámetros de herramientas **no** se guardan, aunque lleguen al receptor.
- Cualquier usuario de la VM podría enviar datos falsos al puerto del receptor. No hay forma de exigir un secreto porque `managed-settings.json` es legible por todos. El receptor limita cada envío a 5 MB, corta las conexiones lentas (15 s) y trunca los textos a 200 caracteres.

## Recomendaciones por usuario

La pestaña "Recomendaciones" agrupa por usuario los hábitos que gastan tokens de más, ordenados por prioridad (coral = alta, azul = media). Cada recomendación indica los **tokens evitables**: una estimación de lo que no se habría gastado siguiendo el consejo (lecturas de caché por encima de 150k de contexto, caché reescrito sin necesidad, salida por encima de 4000 por respuesta, etc.; el detalle está en `docs/fugas-de-tokens.md`). Algunas recomendaciones no se pueden estimar y no muestran la cifra. Señala hábitos que gastan tokens de más, con el dato que lo sustenta y qué hacer. Mira el histórico completo (no respeta el filtro de fechas) y sí el filtro de usuario. Detecta:
- Contexto grande releído en cada turno (sesiones largas sin `/clear` ni `/compact`).
- Caché poco aprovechado y caché perdido tras pausas de más de 1 h.
- Cambios de modelo y prefijo inestable (MCP o plugins cambiando) que reconstruyen el caché.
- Opus/Fable en respuestas cortas.
- Salida o razonamiento alto.
- Herramientas con resultados enormes y subagentes con mucho del gasto.
- Fallos frecuentes de herramientas (solo con OpenTelemetry).

Solo analiza el hilo principal (los subagentes tienen su propio caché) y deduce el TTL del caché (5 min o 1 h) de cómo se escribió. Los umbrales son heurísticos (150k de contexto, 80 % de acierto de caché, etc.) y están en `src/report.py`, función `findings`. Calíbralos con los datos de tu VM. Las fuentes y lo que no se puede detectar están en `docs/fugas-de-tokens.md`. El reporte no muestra dinero, solo tokens.

## Uso manual

Sirve para probar o regenerar sin esperar a la tarea:
```powershell
python src\collect.py                          # actualiza data\tokens.db
python src\report.py --out C:\TokenReports\report.html   # sin --out escribe data\report.html
python src\otel_receiver.py                    # receptor OTel en primer plano (puerto 4318)
python tests\test_collect.py                   # deduplicación y atribución, debe imprimir "ok"
python tests\test_otel.py                      # receptor OTel y managed-settings, debe imprimir "ok"
python tests\test_report.py                    # recomendaciones y que el HTML no permita inyectar código, debe imprimir "ok"
```
`collect.py` admite `--root` (por defecto `C:\Users`) y `--db`; `report.py`, `--db` y `--out`.

## Administración

- Estado de la tarea: `Get-ScheduledTaskInfo -TaskName TokenCounter`
- Ejecutarla ya: `Start-ScheduledTask -TaskName TokenCounter`
- Desinstalar (PowerShell como administrador, desde la carpeta del programa):
  ```powershell
  .\scripts\uninstall.ps1          # quita las tareas, OTel y el recurso compartido; conserva data\ y el reporte
  .\scripts\uninstall.ps1 -Purge   # además borra data\ y C:\TokenReports, y restaura los permisos
  ```
  Con `-Purge` ya puedes borrar la carpeta `C:\TokenCounter`. Si instalaste con otro `-ReportDir` o `-ShareName`, pásalos también al desinstalar.

## Límites

- Mide **tokens**; el reporte no estima dinero.
- El usuario se identifica por su usuario de Windows, no por la cuenta de Anthropic.
- Claude Code no documenta oficialmente el formato de los transcripts. Si una versión nueva lo cambia, el colector ignora las líneas que no entiende y los totales pueden quedar incompletos.
- Solo cuenta lo que existe en disco. Si un usuario borra su carpeta `.claude` antes de la siguiente pasada, esas sesiones se pierden.
- Cada usuario controla sus transcripts: puede falsear su **propio** consumo, o poner el `session.id` de otra persona para quedarse con sus datos OTel. No puede escribir filas a nombre de otro usuario. El colector valida los campos (tokens enteros, textos truncados, fechas con zona horaria) y no sigue symlinks ni junctions, así que un usuario no puede hacer que se lean los transcripts de otro.
- El colector descarta las líneas de transcript de más de 16 millones de caracteres (las reales no pasan de ~1 MB), leyéndolas por trozos para no agotar la memoria. Si se descarta una línea, se pierde ese dato y queda un aviso en el log.
- Si el consumo parece bajo, subir `cleanupPeriodDays` en la configuración de Claude Code evita que se borren transcripts antes de ser leídos.
