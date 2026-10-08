"""Genera report.html (autocontenido) desde tokens.db. Uso: python report.py [--db tokens.db] [--out report.html]"""
import argparse, json, os, re, sqlite3
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/ -> raíz del proyecto

DAILY = """SELECT substr(ts,1,10) d, user, project, model, SUM(input), SUM(output), SUM(cache_create), SUM(cache_read), COUNT(*)
FROM usage GROUP BY 1,2,3,4"""
SESSIONS = """SELECT session, user, project, MIN(ts), MAX(ts), SUM(input), SUM(output), SUM(cache_create), SUM(cache_read)
FROM usage GROUP BY session, user"""

ATTRIB = """SELECT substr(ts,1,10), user, cat, name, SUM(calls), SUM(out_tok), SUM(in_tok) FROM attrib GROUP BY 1,2,3,4"""


def has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def otel_data(db):
    """Datos OTel agregados por día/usuario (el usuario sale de cruzar session.id con los transcripts) y
    las sesiones que tienen algún dato OTel."""
    if not (has(db, "otel_events") and has(db, "otel_metrics")):
        return [], [], set()
    owner = dict(db.execute("SELECT session, user FROM usage GROUP BY session"))
    api, tools = {}, {}
    for ts, sess, ev, model, qs, i, o, cr, cc, tool, ok, ms, skill, plugin, mcp in db.execute(
            "SELECT ts, session, event, model, query_source, input, output, cache_read, cache_create, tool_name,"
            " success, duration_ms, skill, plugin, mcp_server FROM otel_events"):
        d, u = ts[:10], owner.get(sess, "?")
        if ev == "api_request":
            r = api.setdefault((d, u, model, qs), [0, 0, 0, 0, 0])
            for k, v in enumerate((1, i, o, cr, cc)):
                r[k] += v or 0
        elif ev == "tool_result":
            r = tools.setdefault((d, u, tool, skill, plugin, mcp), [0, 0, 0.0])
            r[0] += 1
            r[1] += ok == "true"
            r[2] += ms or 0
    seen = {r[0] for r in db.execute("SELECT DISTINCT session FROM otel_events UNION SELECT DISTINCT session FROM otel_metrics")}
    return [[*k, *v] for k, v in api.items()], [[*k, *v] for k, v in tools.items()], seen


def windows(db, now):
    """Cuenta única compartida: los bloques de 5h se calculan con los mensajes de TODOS los usuarios mezclados
    (el bloque nace con el primer mensaje y dura 5h). Se reparte lo consumido en cada bloque entre usuarios.
    Cuenta input+output+cache_create (el cache leído casi no pesa en los límites)."""
    rows = []
    for u, ts, n in db.execute("SELECT user, ts, input+output+cache_create FROM usage WHERE ts!='' ORDER BY ts"):
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if t.tzinfo:  # sin zona no se puede comparar con el resto
            rows.append((t, u, n))
    blocks = []  # [inicio, {usuario: tokens}]
    for t, u, n in rows:
        if blocks and t < blocks[-1][0] + timedelta(hours=5):
            blocks[-1][1][u] = blocks[-1][1].get(u, 0) + n
        else:
            blocks.append([t, {u: n}])
    users = {u: {"cur": 0, "prev": 0, "week": 0} for _, u, _ in rows}
    if not blocks:
        return {"cur": None, "prev": None, "week": 0, "users": users}
    live = blocks[-1][0] + timedelta(hours=5) > now
    cur, prev = (blocks[-1], blocks[-2] if len(blocks) > 1 else None) if live else (None, blocks[-1])
    for key, blk in (("cur", cur), ("prev", prev)):
        for u, n in (blk[1] if blk else {}).items():
            users[u][key] = n
    for t, u, n in rows:
        if t >= now - timedelta(days=7):
            users[u]["week"] += n
    iso = lambda b: b and {"start": b[0].isoformat(), "end": (b[0] + timedelta(hours=5)).isoformat(), "tokens": sum(b[1].values())}
    return {"cur": iso(cur), "prev": iso(prev), "week": sum(d["week"] for d in users.values()), "users": users}


ADVICE = {
    "ctx": "Usa /clear al cambiar de tarea y /compact en cortes naturales; /context muestra qué ocupa el contexto.",
    "swap": "Elige el modelo al inicio; para cambiar de modelo en una tarea grande haz /clear antes de /model (cada modelo tiene su propio caché).",
    "pref": "No conectes ni quites servidores MCP ni plugins a mitad de sesión; desactiva los que no uses con /mcp antes de empezar.",
    "hit": "Evita cambiar modelo, MCP o plugins a mitad de sesión; /usage muestra la causa probable de cada fallo de caché.",
    "idle": "Si vas a volver tras más de una hora, cierra la tarea con /clear o reanuda desde el resumen que ofrece Claude Code.",
    "model": "Elige Sonnet (o Haiku en subagentes) con /model para tareas simples; reserva Opus para decisiones complejas.",
    "big": "Lee por rangos (offset/limit), acota Grep, y bloquea carpetas pesadas con Read(...) en permissions.deny (.claudeignore no tiene efecto).",
    "sub": "Usa un modelo pequeño en los subagentes (model: haiku) y delega solo lo voluminoso.",
    "out": "Baja el esfuerzo con /effort en tareas simples y usa plan mode antes de implementar.",
    "err": "Corrige los permisos o comandos que fallan (permissions.allow, CLAUDE.md con los comandos correctos).",
    "idle5": "Configura \"promptCacheTtl\": \"1h\" (o CLAUDE_CODE_PROMPT_CACHE_TTL=1h): la escritura cuesta 2x pero se amortiza con dos lecturas.",
    "huge": " Con modelos de 1M el auto-compact no salta hasta ~967k: usa /autocompact 200k (o \"autoCompactWindow\": 200000).",
    "boot": "Ejecuta /context: deja CLAUDE.md en menos de 200 líneas, pasa flujos especializados a skills, desactiva con /mcp los servidores que no uses y no fuerces ENABLE_TOOL_SEARCH=false.",
    "cold": "Compacta (/compact <foco>) mientras trabajas, no al volver de una pausa; si no necesitas continuidad usa /clear.",
    "bg": "Usa /loop solo en sesiones ligeras, CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0 y revisa la fila Loops de /usage.",
    "submodel": "Pon model: haiku o sonnet en tus subagentes, o CLAUDE_CODE_SUBAGENT_MODEL=haiku (el Explore integrado usa el modelo de la sesión).",
    "explore": "Acota el pedido (archivo, escenario, qué es 'terminado') o pide 'usa subagentes para investigar X' para que la exploración no llene tu contexto.",
    "noplan": "Para cambios en varios archivos o con enfoque incierto, pasa a plan mode (Shift+Tab) antes de implementar.",
    "haiku": "Compacta antes de 100k (/autocompact 100k) o da a los subagentes Haiku tareas acotadas: Haiku 5.5 cobra 5x por encima de 100k.",
    "limit": "Revisa /usage; aplaza tareas pesadas (subagentes, sesiones grandes) hasta el próximo bloque.",
}
SEV = {"crítica": 0, "media": 1, "baja": 2}


def findings(db):
    """Puntos a mejorar por usuario. Umbrales heurísticos de docs/fugas-de-tokens.md y docs/buenas-practicas-tokens.md ([H]: calibrar con datos propios).
    Fila: [usuario, regla, severidad (crítica/media/baja), título, detalle, tokens implicados, consejo, tokens evitables (estimación, 0 si no se puede estimar)]. Las reglas de sesión usan solo el hilo principal (side=0): los subagentes tienen su propio caché."""
    out, sess = [], {}
    cols = {r[1] for r in db.execute("PRAGMA table_info(usage)")}  # BD anterior a collect.py nuevo: sin side/cache_5m/cache_1h
    c = lambda n: f"COALESCE({n},0)" if n in cols else "0"
    main = f"{c('side')}=0"
    for u, s, m, ts, i, o, cc, cr, c5, c1 in db.execute(
            f"SELECT user, session, model, ts, input, output, cache_create, cache_read, {c('cache_5m')}, {c('cache_1h')}"
            f" FROM usage WHERE ts!='' AND {main} ORDER BY user, session, ts"):
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if not t.tzinfo:
            continue
        sess.setdefault((u, s), []).append((t, m or "", i, o, cc, cr, c5, c1))
    per = {}  # usuario -> acumuladores
    for (u, s), rs in sess.items():
        a = per.setdefault(u, {**{k: [0, 0, 0] for k in ("big", "hit", "idle", "exp", "out", "swap", "pref", "cold")},
                               "huge": 0, "idle5": 0, "boot": [], "bg": {}})
        ctx = [r[2] + r[4] + r[5] for r in rs]
        if ctx[0]:
            a["boot"].append(ctx[0])
        if max(ctx) > 150000 and len(rs) > 40:
            a["big"][0] += 1; a["big"][1] += sum(r[5] for r in rs); a["big"][2] += sum(max(0, r[5] - 150000) for r in rs)  # evitable: lectura por encima de 150k de contexto
        a["huge"] += max(ctx) > 300000
        hit = len(rs) >= 10 and sum(ctx[1:]) and sum(r[5] for r in rs[1:]) / sum(ctx[1:]) < 0.8
        done = set()  # reconstrucciones ya contadas en idle/swap/pref/cold, para no duplicar en hit
        ttl = 300 if sum(r[6] for r in rs) > sum(r[7] for r in rs) else 3600  # TTL del caché según cómo se escribió
        sw = pf = 0
        for k in range(1, len(rs)):
            gap, rebuilt = (rs[k][0] - rs[k - 1][0]).total_seconds(), rs[k][4] > 0.5 * ctx[k]
            if gap > ttl and ctx[k - 1] > 100000 and ctx[k] < 0.5 * ctx[k - 1] and rs[k][5] < 0.5 * ctx[k]:
                a["cold"][0] += 1; a["cold"][1] += ctx[k - 1]; a["cold"][2] += ctx[k - 1]; done.add(k)  # compactación en frío: releyó todo sin caché
            elif gap > ttl and rebuilt and rs[k][4] >= 20000:
                a["idle"][0] += 1; a["idle"][1] += rs[k][4]; a["idle"][2] += rs[k][4]; done.add(k)
                a["idle5"] += ttl == 300 and gap < 3600
            elif rebuilt and ctx[k] > 50000:
                if rs[k][1] != rs[k - 1][1]:
                    sw += 1; a["swap"][1] += rs[k][4]; a["swap"][2] += rs[k][4]; done.add(k)
                elif gap < 300:
                    pf += 1; a["pref"][1] += rs[k][4]; a["pref"][2] += rs[k][4]; done.add(k)
            if rs[k][3] < 50 and ctx[k] > 50000 and gap > 120:  # respuesta mínima sobre contexto grande: /loop, /goal, mensajes entre sesiones
                d = a["bg"].setdefault(rs[k][0].date(), [0, 0]); d[0] += 1; d[1] += ctx[k]
        a["swap"][0] += sw >= 2; a["pref"][0] += pf >= 2
        if hit:
            a["hit"][0] += 1; a["hit"][1] += sum(r[4] for r in rs[1:]); a["hit"][2] += sum(rs[k][4] for k in range(1, len(rs)) if k not in done)
        for r, c in zip(rs, ctx):
            if any(x in r[1].lower() for x in ("opus", "fable")):
                a["exp"][0] += 1; a["exp"][1] += (r[3] < 300 and c < 30000)
            a["out"][0] += 1; a["out"][1] += r[3]; a["out"][2] += max(0, r[3] - 4000)

    def add(u, rule, sev, title, detail, tok, av=0, advice=None):
        out.append([u, rule, sev, title, detail, tok, advice or ADVICE[rule], int(av)])

    for u, a in per.items():
        if a["big"][0]:
            huge = f" {a['huge']} superaron 300k." if a["huge"] else ""
            add(u, "ctx", "crítica", "Contexto grande releído en cada turno", f"{a['big'][0]} sesiones con más de 150k de contexto y más de 40 respuestas, sin limpiar.{huge}",
                a["big"][1], a["big"][2], ADVICE["ctx"] + (ADVICE["huge"] if a["huge"] else ""))
        if a["hit"][0]:
            add(u, "hit", "media", "Caché poco aprovechado", f"{a['hit'][0]} sesiones donde menos del 80 % del contexto se leyó de caché.", a["hit"][1], a["hit"][2])
        if a["idle"][0] >= 3:
            p5 = a["idle5"] * 2 > a["idle"][0]  # mayoría de pausas de 5-60 min con caché de 5 min
            add(u, "idle", "crítica", "Caché perdido por inactividad", f"{a['idle'][0]} reanudaciones tras superar el TTL del caché que reescribieron más de 20k tokens."
                + (" La mayoría fueron pausas de 5 a 60 minutos con caché de 5 min." if p5 else ""), a["idle"][1], a["idle"][2], ADVICE["idle5"] if p5 else None)
        if a["cold"][0] >= 2:
            add(u, "cold", "media", "Compactación en frío", f"{a['cold'][0]} compactaciones tras superar el TTL del caché: el resumen releyó todo el contexto sin caché.", a["cold"][1], a["cold"][2])
        if a["swap"][0]:
            add(u, "swap", "media", "Cambios de modelo con contexto grande", f"{a['swap'][0]} sesiones con 2 o más cambios de modelo que reconstruyeron el caché (más de 50k de contexto).", a["swap"][1], a["swap"][2])
        if a["pref"][0]:
            add(u, "pref", "media", "Prefijo del prompt inestable", f"{a['pref'][0]} sesiones con 2 o más reconstrucciones del caché sin pausa ni cambio de modelo (MCP, plugins o herramientas cambiando).", a["pref"][1], a["pref"][2])
        b = sorted(a["boot"])
        if len(b) >= 5 and b[len(b) // 2] > 40000:
            add(u, "boot", "media", "Contexto de arranque pesado", f"La primera respuesta de tus sesiones parte de {b[len(b) // 2] // 1000}k tokens de mediana (referencia oficial ≈ 8k). Cada sesión y subagente lo vuelve a pagar.",
                sum(b), sum(max(0, x - 8000) for x in b))
        bg = [v for v in a["bg"].values() if v[0] >= 10]
        if bg:
            add(u, "bg", "media", "Llamadas automáticas con contexto completo", f"{len(bg)} días con 10 o más respuestas mínimas (menos de 50 tokens) sobre más de 50k de contexto tras una pausa: probable /loop, /goal o mensajes entre sesiones.",
                sum(v[1] for v in bg), sum(v[1] for v in bg))
        n, short = a["exp"][:2]
        if n >= 20 and short / n > 0.5:
            add(u, "model", "crítica", "Modelo caro en respuestas cortas", f"{short} de {n} respuestas de Opus/Fable fueron cortas (menos de 300 de salida, contexto menor de 30k).", 0)
        if a["out"][0] >= 20 and a["out"][1] / a["out"][0] > 4000:
            add(u, "out", "media", "Salida (o razonamiento) alta", f"Promedio de {a['out'][1] // a['out'][0]} tokens de salida por respuesta.", a["out"][1], a["out"][2])
    if "side" in cols:
        for u, n, exp, tk in db.execute("SELECT user, COUNT(*), SUM(model LIKE '%opus%' OR model LIKE '%fable%'), SUM(CASE WHEN model LIKE '%opus%' OR model LIKE '%fable%'"
                                        " THEN input+cache_create+cache_read+output ELSE 0 END) FROM usage WHERE side=1 GROUP BY user"):
            if n >= 20 and exp / n > 0.3:
                add(u, "submodel", "crítica", "Subagentes en modelo caro", f"{exp} de {n} respuestas de subagentes usaron Opus/Fable.", tk)
    for u, n, tk in db.execute("SELECT user, COUNT(*), SUM(input+cache_create+cache_read) FROM usage WHERE model LIKE '%haiku-5-5%'"
                               " AND input+cache_create+cache_read > 100000 GROUP BY user HAVING COUNT(*) >= 5"):
        add(u, "haiku", "baja", "Haiku 5.5 con prompts de más de 100k", f"{n} respuestas de Haiku 5.5 con más de 100k de contexto, facturadas a 5x.", tk)
    if has(db, "attrib"):
        tot = {u: x or 0 for u, x in db.execute("SELECT user, SUM(out_tok+in_tok) FROM attrib GROUP BY user")}
        for u, cat, n, calls, tk in db.execute("SELECT user, cat, name, SUM(calls), SUM(in_tok) FROM attrib WHERE cat IN ('Herramienta','MCP','Plugin') GROUP BY user, cat, name"):
            if calls >= 3 and tk / calls > 10000 and tk > 0.1 * tot.get(u, 1):
                add(u, "big", "media", f"Resultados enormes de {n}", f"{calls} llamadas con unos {tk // calls} tokens de entrada estimados cada una.", tk, tk - 10000 * calls)
        for u, tk in db.execute("SELECT user, SUM(out_tok+in_tok) FROM attrib WHERE cat='Subagente' GROUP BY user"):
            share = tk / tot[u] if tot.get(u) else 0
            if share > 0.1:
                add(u, "sub", "crítica" if share > 0.3 else "media", "Subagentes con gran parte del gasto", f"{share:.0%} de los tokens atribuidos del usuario.", tk)
        ex = {}
        for u, s in db.execute(f"""SELECT a.user, a.session FROM attrib a
            WHERE a.cat='Herramienta' AND a.name IN ('Read','Grep','Glob')
              AND NOT EXISTS (SELECT 1 FROM attrib b WHERE b.session=a.session AND b.cat='Subagente' AND b.calls>0)
              AND (SELECT MAX(input+cache_create+cache_read) FROM usage x WHERE x.session=a.session AND {main}) > 150000
            GROUP BY a.user, a.session HAVING SUM(a.calls) >= 60"""):
            ex[u] = ex.get(u, 0) + 1
        for u, n in ex.items():
            if n >= 2:
                add(u, "explore", "media", "Exploración sin acotar en el hilo principal", f"{n} sesiones con 60 o más lecturas/búsquedas (Read/Grep/Glob) y más de 150k de contexto, sin subagentes.", 0)
        for u, n in db.execute("""SELECT user, COUNT(*) FROM (SELECT user, session FROM attrib GROUP BY user, session
            HAVING SUM(CASE WHEN name IN ('Edit','Write') THEN calls ELSE 0 END) >= 60 AND SUM(CASE WHEN name='ExitPlanMode' THEN calls ELSE 0 END) = 0)
            GROUP BY user HAVING COUNT(*) >= 3"""):
            add(u, "noplan", "baja", "Cambios grandes sin plan mode", f"{n} sesiones con 60 o más ediciones sin pasar por plan mode.", 0)
    if has(db, "otel_events"):
        owner = dict(db.execute("SELECT session, user FROM usage GROUP BY session"))
        st = {}
        for sess_id, tool, ok in db.execute("SELECT session, tool_name, success FROM otel_events WHERE event='tool_result'"):
            r = st.setdefault((owner.get(sess_id, "?"), tool), [0, 0]); r[0] += 1; r[1] += ok != "true"
        for (u, tool), (n, bad) in st.items():
            if n >= 20 and bad / n > 0.15 and u != "?":
                add(u, "err", "media", f"Fallos frecuentes en {tool}", f"{bad} de {n} llamadas fallaron ({bad / n:.0%}).", 0)
    p = plan_data(db)
    for key, lim, frac, label in (("cur", p["limits"]["five_hour"], 0.9, "El bloque de 5 h actual"), ("week", p["limits"]["weekly"], 0.8, "La semana")):
        used = p["week"] if key == "week" else (p["cur"] or {}).get("tokens", 0)
        if lim and used > frac * lim:
            for u, d in p["users"].items():
                if d[key]:
                    add(u, "limit", "media", "Cerca del límite del plan", f"{label} va por el {used / lim:.0%} del límite configurado (tu aporte: {d[key]:,} tokens).", d[key])
    return sorted(out, key=lambda r: (SEV[r[2]], -r[7], -r[5]))


PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Uso de tokens</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:">
<style>
:root{--bg:#040506;--card:#07080a;--well:#111214;--badge:#1b1c1e;--ring:#363739;--smoke:#6a6b6c;--gr:#9c9c9d;--st:#9c9c9d;--mist:#e6e6e6;--bone:#fff;--mid:#454647;--ir:#e6e6e6;--or:#ff6363;--ember:#452324;
--key:rgba(255,255,255,.05) 0 1px 0 0 inset,rgba(255,255,255,.25) 0 0 0 1px,rgba(0,0,0,.2) 0 -1px 0 0 inset;
--sans:'Inter',ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:'Geist Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--t:.15s cubic-bezier(.4,0,.2,1)}
*{box-sizing:border-box}[hidden]{display:none!important}
html{color-scheme:dark}body{margin:0;min-height:100vh;background:var(--bg) radial-gradient(ellipse 60% 420px at 50% 0,rgba(4,63,150,.55),rgba(6,18,37,.2) 60%,transparent) no-repeat;color:var(--bone);font:400 14px/1.5 var(--sans);font-feature-settings:"calt","kern","liga","ss03"}
header{position:fixed;top:16px;left:16px;bottom:16px;z-index:5;width:220px;overflow-y:auto;display:flex;flex-direction:column;gap:24px;padding:16px;border:1px solid var(--ring);border-radius:8px;background:rgba(4,5,6,.6);backdrop-filter:blur(48px);-webkit-backdrop-filter:blur(48px)}
.brand{font:500 13px/1.25 var(--sans);display:flex;align-items:center;flex-wrap:wrap;gap:8px}.brand small{flex-basis:100%}
.brand::before{content:"";width:10px;height:10px;background:var(--or);transform:rotate(45deg);border-radius:2px}.brand small{color:var(--smoke);font:400 12px var(--mono);letter-spacing:.2px}
nav{display:flex;flex-direction:column;gap:2px;flex:1}
nav a{color:var(--gr);text-decoration:none;font-weight:500;font-size:13px;padding:8px 12px;border-radius:8px;transition:color var(--t),background var(--t)}
nav a:hover,nav a[aria-current]{color:var(--bone)}nav a[aria-current]{background:rgba(255,255,255,.05)}
.flt{display:flex;flex-direction:column;gap:8px}
select,input{background:rgba(255,255,255,.05);color:var(--bone);border:1px solid transparent;border-radius:8px;padding:6px 12px;font:400 13px var(--sans);transition:border-color var(--t)}
select option{background:var(--well)}select:hover,input:hover{border-color:var(--ring)}select:focus-visible,input:focus-visible{outline:0;border-color:var(--gr)}
body{padding-left:252px}main{width:100%;max-width:1200px;margin:0 auto;padding:80px 32px 120px}
h1{font:400 56px/1.17 var(--sans);letter-spacing:.22px;margin:0 0 48px}
.eb{font:500 11px/1 var(--sans);letter-spacing:.8px;text-transform:uppercase;color:var(--gr);margin-bottom:16px}
h2{font:500 14px/1.4 var(--sans);letter-spacing:.14px;color:var(--gr);margin:0 0 16px}h2 small{font:400 12px/1.4 var(--sans);color:var(--smoke);letter-spacing:0;margin-left:4px}
.card{background:var(--card);box-shadow:var(--key);border-radius:16px;padding:24px;margin-bottom:24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.kpi{background:var(--card);box-shadow:var(--key);border-radius:16px;padding:24px}.kpi span{display:block;font-size:12px;color:var(--gr);margin-bottom:16px}
.kpi b{display:block;font:500 32px/1.15 var(--sans)}.kpi svg{display:block;width:100%;height:40px;margin-top:16px}
svg polyline{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px}.grid>.card{margin-bottom:0}
.grid.wide{grid-template-columns:1fr}
.row{display:grid;grid-template-columns:minmax(80px,32%) 1fr auto;gap:16px;align-items:center;margin:8px 0}
.row i{display:block;height:8px;border-radius:9999px}.row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row span:last-child{font:400 12px var(--mono);color:var(--gr)}
.days{display:flex;align-items:flex-end;gap:2px;height:160px}.days div{flex:1;background:var(--mid);border-radius:3px 3px 0 0;min-height:1px;transition:background var(--t)}.days div:hover{background:var(--mist)}
.m{margin:16px 0}.m>div{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.m b{font:500 24px/1.15 var(--sans)}.m small{color:var(--gr);font-size:12px}
.m u{display:block;height:6px;background:rgba(255,255,255,.1);border-radius:9999px;overflow:hidden;margin:8px 0}.m u i{display:block;height:100%;border-radius:9999px;background:var(--mist)}
.m.w u i,.m.x u i{background:var(--or)}.m em{font:500 11px var(--sans);font-style:normal;color:var(--bone);background:var(--ember);border-radius:6px;padding:2px 6px;margin-left:8px}
.pls{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}.pls .card{margin:0}.pls h2{color:var(--bone)}
.acct{border-radius:20px;margin-bottom:24px}.acct .m{margin:0}
.acct .tri{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:32px}
.warn{background:var(--card);box-shadow:var(--key);border-radius:16px;padding:16px;margin-bottom:16px}.warn::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--gr);margin-right:8px;vertical-align:middle}
.rc{background:var(--well);border-radius:8px;padding:16px;margin-bottom:8px}.rc p{margin:8px 0;color:var(--mist)}.rc small{color:var(--gr)}.rc .av{color:var(--bone);font-weight:500}.rc .rt{font-weight:500;font-size:16px;margin-top:8px}
.rc.hi{background:var(--ember)}.rc.lo{background:transparent;box-shadow:var(--key)}.tg.lo{background:transparent;color:var(--gr);box-shadow:var(--key)}
.tg{font:400 11px var(--mono);letter-spacing:.5px;text-transform:uppercase;border-radius:6px;padding:2px 6px;background:var(--badge);color:var(--bone)}.tg.hi{background:rgba(255,99,99,.2)}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--badge);white-space:nowrap}
th{font-size:12px;font-weight:500;color:var(--gr)}td.n,th.n{text-align:right}
@media(max-width:800px){body{padding-left:0}header{position:static;width:auto;margin:16px 16px 0}nav{flex-direction:row;flex-wrap:wrap}main{padding:48px 16px 80px}h1{font-size:36px}}
</style></head><body>
<header><div class="brand">TokenCounter <small id="gen"></small></div><nav id="nav"></nav>
<div class="flt"><select id="u"></select><input type="date" id="f"><input type="date" id="t"></div></header>
<main>
<section id="resumen" data-t="Resumen"><div class="eb">Uso de tokens de Claude Code</div><h1>Resumen</h1>
<div class="kpis" id="k"></div>
<div class="card"><h2>Tokens por día (entrada + salida)</h2><div class="days" id="days"></div></div>
<div class="grid wide"><div class="card"><h2>Por usuario</h2><div id="bu"></div></div>
<div class="card"><h2>Por modelo</h2><div id="bm"></div></div>
<div class="card"><h2>Por proyecto</h2><div id="bp"></div></div></div></section>
<section id="limites" data-t="Límites"><div class="eb">Cuenta compartida</div><h1>Límites del plan</h1><div id="plan"></div></section>
<section id="gasto" data-t="Gasto"><div class="eb">Atribución estimada</div><h1>En qué se gastan los tokens</h1>
<div class="card"><h2>Estimado desde los transcripts <small>La salida se reparte entre las herramientas que llamó cada respuesta; la entrada nueva, entre las herramientas cuyos resultados entraron al contexto.</small></h2>
<div class="grid"><div id="ac"></div><div class="scroll"><table id="at"></table></div></div></div></section>
<section id="telemetria" data-t="OpenTelemetry"><div class="eb">Datos exactos</div><h1>Detalle OpenTelemetry</h1><div class="card"><div id="otel"></div></div></section>
<section id="recomendaciones" data-t="Recomendaciones"><div class="eb">Histórico completo · umbrales heurísticos (docs/fugas-de-tokens.md, docs/buenas-practicas-tokens.md)</div><h1>Recomendaciones por usuario</h1><div class="eb" style="margin-top:-16px;text-transform:none">Los tokens evitables son una estimación de lo que no se habría gastado siguiendo cada recomendación; no es una garantía.</div><div id="rec"></div></section>
<section id="sesiones" data-t="Sesiones"><div class="eb">Últimas 50</div><h1>Sesiones recientes</h1><div class="card scroll"><table id="s"></table></div></section>
</main>
<script>
const D=__DAILY__,S=__SESSIONS__,GEN="__GEN__",P=__PLAN__,AT=__ATTRIB__,OA=__OAPI__,OT=__OTOOLS__,FX=__FINDINGS__;
// D: [fecha,user,proyecto,modelo,in,out,cache_create,cache_read,llamadas]  S: [sesion,user,proyecto,ini,fin,in,out,cc,cr]
const $=id=>document.getElementById(id),fmt=n=>n>=1e6?(n/1e6).toFixed(2)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":""+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const users=[...new Set(D.map(r=>r[1]))].sort();
$("u").innerHTML='<option value="">Todos los usuarios</option>'+users.map(u=>`<option>${esc(u)}</option>`).join("");
$("gen").textContent="Generado "+GEN;
function bars(el,map){const e=[...map].sort((a,b)=>b[1]-a[1]).slice(0,8),m=Math.max(1,...e.map(x=>x[1]));
 $(el).innerHTML=e.map(([k,v],i)=>`<div class="row"><span title="${esc(k)}">${esc(k)}</span><i style="width:${v/m*100}%;background:var(${i?"--gr":"--bone"})"></i><span>${fmt(v)}</span></div>`).join("")||"Sin datos"}
function spark(R,i){const m=new Map();R.forEach(r=>m.set(r[0],(m.get(r[0])||0)+r[i<4?i+4:8]));
 const v=[...m].sort((a,b)=>a[0]<b[0]?-1:1).map(x=>x[1]);if(v.length<2)return"";
 const mx=Math.max(1,...v),h=v.length>>1,up=v.slice(h).reduce((a,b)=>a+b,0)>=v.slice(0,h).reduce((a,b)=>a+b,0);
 return`<svg viewBox="0 0 100 40" preserveAspectRatio="none"><polyline stroke="var(${up?"--ir":"--or"})" points="${v.map((y,k)=>(k/(v.length-1)*100).toFixed(1)+","+(38-y/mx*36).toFixed(1)).join(" ")}"/></svg>`}
function draw(){const u=$("u").value,f=$("f").value,t=$("t").value;
 const ok=d=>(!u||d[1]==u)&&(!f||d[0]>=f)&&(!t||d[0]<=t+"~");
 const R=D.filter(ok),tot=R.reduce((a,r)=>[a[0]+r[4],a[1]+r[5],a[2]+r[6],a[3]+r[7],a[4]+r[8]],[0,0,0,0,0]);
 $("k").innerHTML=[["Entrada",tot[0]],["Salida",tot[1]],["Cache escrito",tot[2]],["Cache leído",tot[3]],["Llamadas",tot[4]]]
  .map(([l,v],i)=>`<div class="kpi"><span>${l}</span><b>${fmt(v)}</b>${spark(R,i)}</div>`).join("");
 const g=(i,v)=>{const m=new Map();R.forEach(r=>m.set(r[i],(m.get(r[i])||0)+v(r)));return m},io=r=>r[4]+r[5];
 bars("bu",g(1,io));bars("bm",g(3,io));bars("bp",g(2,io));
 const dm=g(0,io),ds=[...dm.keys()].sort(),mx=Math.max(1,...dm.values());
 $("days").innerHTML=ds.map(d=>`<div title="${esc(d)}: ${fmt(dm.get(d))}" style="height:${dm.get(d)/mx*100}%"></div>`).join("");
 const SS=S.filter(s=>(!u||s[1]==u)&&(!f||s[4].slice(0,10)>=f)&&(!t||s[4].slice(0,10)<=t)).sort((a,b)=>b[4]<a[4]?-1:1).slice(0,50);
 $("s").innerHTML="<tr><th>Último uso</th><th>Usuario</th><th>Proyecto</th><th class=n>Entrada</th><th class=n>Salida</th><th class=n>Cache</th><th>OTel</th></tr>"+
  SS.map(s=>`<tr><td>${esc(s[4].replace("T"," ").slice(0,16))}</td><td>${esc(s[1])}</td><td>${esc(s[2])}</td><td class=n>${fmt(s[5])}</td><td class=n>${fmt(s[6])}</td><td class=n>${fmt(s[7]+s[8])}</td><td>${s[9]?"sí":"no"}</td></tr>`).join("");extras(ok,SS,u)}
const SV={"crítica":0,"media":1,"baja":2},CL={"crítica":"hi","media":"md","baja":"lo"};
function rec(u){const by=new Map();
 FX.filter(r=>!u||r[0]==u).forEach(r=>by.set(r[0],[...(by.get(r[0])||[]),r]));
 $("rec").innerHTML=[...by].sort().map(([n,l])=>{l.sort((a,b)=>SV[a[2]]-SV[b[2]]||b[7]-a[7]||b[5]-a[5]);
  const hi=l.filter(r=>r[2]=="crítica").length,md=l.filter(r=>r[2]=="media").length,av=l.reduce((x,r)=>x+r[7],0);
  return`<div class="card rg"><h2><b>${esc(n)}</b> <small>${l.length} recomendación(es)${hi?` · <span class="tg hi">${hi} crítica(s)</span>`:""}${md?` · <span class="tg">${md} media(s)</span>`:""}${av?` · ${fmt(av)} tokens evitables`:""}</small></h2>`+
  l.map(r=>`<div class="rc ${CL[r[2]]}"><span class="tg ${CL[r[2]]}">prioridad ${r[2]}</span><div class="rt">${esc(r[3])}</div><p>${esc(r[4])}</p><p><b>Qué hacer:</b> ${esc(r[6])}</p><small>${r[5]?fmt(r[5])+" tokens implicados":""}${r[5]&&r[7]?" · ":""}${r[7]?`<b class="av">${fmt(r[7])} tokens evitables</b>`:""}</small></div>`).join("")+"</div>"}).join("")||'<div class="card">Sin recomendaciones: no se detectaron hábitos que gasten tokens de más.</div>'}
function extras(ok,SS,u){rec(u);
 const A=AT.filter(ok),cat=new Map(),nm=new Map();
 A.forEach(r=>{const v=r[5]+r[6];cat.set(r[2],(cat.get(r[2])||0)+v);const k=r[2]+"|"+r[3],o=nm.get(k)||[r[2],r[3],0,0,0];o[2]+=r[4];o[3]+=r[5];o[4]+=r[6];nm.set(k,o)});
 bars("ac",cat);
 $("at").innerHTML="<tr><th>Tipo</th><th>Nombre</th><th class=n>Llamadas</th><th class=n>Salida</th><th class=n>Entrada est.</th></tr>"+
  [...nm.values()].sort((a,b)=>b[3]+b[4]-a[3]-a[4]).slice(0,15).map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td class=n>${fmt(r[2])}</td><td class=n>${fmt(r[3])}</td><td class=n>${fmt(r[4])}</td></tr>`).join("");
 const miss=S.filter(s=>(!u||s[1]==u)&&!s[9]&&(ok([s[4].slice(0,10),s[1]]))).length,tot=S.filter(s=>(!u||s[1]==u)&&ok([s[4].slice(0,10),s[1]])).length;
 const W=miss?`<div class="warn">Falta información de OpenTelemetry para ${miss} de ${tot} sesiones (anteriores a la activación o con el receptor apagado). Sus tokens solo se conocen por los transcripts.</div>`:"";
 const a=OA.filter(ok),t=OT.filter(ok);
 if(!a.length&&!t.length){$("otel").innerHTML=W||'<div class="warn">Sin datos de OpenTelemetry: el receptor no está activo o aún no hubo sesiones nuevas.</div>';return}
 const qs=new Map(),calls=a.reduce((x,r)=>x+r[4],0);
 a.forEach(r=>qs.set(r[3]||"?",(qs.get(r[3]||"?")||0)+r[5]+r[6]+r[8]));
 const g=new Map();t.forEach(r=>{const k=r[2]+"|"+(r[3]||r[4]||r[5]||"");const o=g.get(k)||[r[2],r[3]||r[4]||r[5]||"",0,0,0];o[2]+=r[6];o[3]+=r[7];o[4]+=r[8];g.set(k,o)});
 $("otel").innerHTML=W+`<div class="grid"><div><div class="m"><div><span>Llamadas a la API</span><b>${fmt(calls)}</b></div></div>
 <h2 style="margin-top:24px">Por origen de la llamada</h2><div id="oq"></div></div><div class="scroll"><table id="ot"></table></div></div>`;
 bars("oq",qs);
 $("ot").innerHTML="<tr><th>Herramienta</th><th>Skill / plugin / MCP</th><th class=n>Usos</th><th class=n>Éxito</th><th class=n>ms prom.</th></tr>"+
  [...g.values()].sort((x,y)=>y[2]-x[2]).slice(0,15).map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td class=n>${fmt(r[2])}</td><td class=n>${(r[3]/r[2]*100).toFixed(0)}%</td><td class=n>${Math.round(r[4]/r[2])}</td></tr>`).join("")}
function meter(l,sub,v,lim){const p=lim?v/lim*100:null,c=p>=90?"x":p>=70?"w":"";
 return `<div class="m ${c}"><div><span>${l}${c=="x"?"<em>crítico</em>":""} <small>${sub||""}</small></span><b>${p==null?fmt(v):p.toFixed(1)+"%"}</b></div><u><i style="width:${Math.min(p||0,100)}%"></i></u><small>${fmt(v)}${lim?" de "+fmt(lim):" (sin límite configurado)"}</small></div>`}
const hm=s=>s?new Date(s).toLocaleString("es",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}):"";
function plan(){const u=$("u").value,L=P.limits,c=P.cur,p=P.prev,
 per=(k,x)=>meter(k=="cur"?"Bloque 5h actual":k=="prev"?"Bloque 5h anterior":"Semana (7 días)","",x[k],k=="week"?L.weekly:L.five_hour);
 $("plan").innerHTML='<div class="card acct"><h2>Cuenta compartida · todos los usuarios</h2><div class="tri">'+
  meter("Bloque 5h actual",c?hm(c.start)+" → "+hm(c.end):"sin bloque activo",c?c.tokens:0,L.five_hour)+
  meter("Bloque 5h anterior",p?hm(p.start)+" → "+hm(p.end):"",p?p.tokens:0,L.five_hour)+
  meter("Semana (7 días)","",P.week,L.weekly)+'</div></div><div class="pls">'+
  Object.keys(P.users).filter(x=>!u||x==u).sort().map(x=>{const d=P.users[x];
   return `<div class="card"><h2>${esc(x)} · aporte a la cuenta</h2>`+["cur","prev","week"].map(k=>per(k,d)).join("")+"</div>"}).join("")+"</div>"}
$("u").addEventListener("change",plan);plan();
const SEC=[...document.querySelectorAll("main>section")];
$("nav").innerHTML=SEC.map(s=>`<a href="#${s.id}">${s.dataset.t}</a>`).join("");
function show(){const h=SEC.some(s=>s.id==location.hash.slice(1))?location.hash.slice(1):SEC[0].id;
 SEC.forEach(s=>s.hidden=s.id!=h);$("nav").querySelectorAll("a").forEach(a=>a.toggleAttribute("aria-current",a.getAttribute("href")=="#"+h));scrollTo(0,0)}
addEventListener("hashchange",show);show();
["u","f","t"].forEach(i=>$(i).onchange=draw);draw();
</script></body></html>"""


def plan_data(db):
    """Un único límite por periodo (la cuenta de Claude es una sola); cada usuario se mide contra ese mismo límite."""
    try:
        with open(os.path.join(ROOT, "config", "limits.json"), encoding="utf-8") as f:
            lim = json.load(f)
    except (OSError, ValueError):
        lim = {}
    return {**windows(db, datetime.now(timezone.utc)), "limits": {k: lim.get(k) for k in ("five_hour", "weekly")}}


def js(x):
    """JSON seguro dentro de <script>: sin <, > ni & literales (cubre </script> y <!--)."""
    return json.dumps(x).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build(db_path, out):
    db = sqlite3.connect(db_path)
    oa, ot, seen = otel_data(db)
    sessions = [[*r, int(r[0] in seen)] for r in db.execute(SESSIONS).fetchall()]
    attrib = db.execute(ATTRIB).fetchall() if has(db, "attrib") else []
    vals = {"DAILY": js(db.execute(DAILY).fetchall()), "SESSIONS": js(sessions), "ATTRIB": js(attrib), "OAPI": js(oa),
            "OTOOLS": js(ot), "FINDINGS": js(findings(db)), "PLAN": js(plan_data(db)),
            "GEN": datetime.now().strftime("%Y-%m-%d %H:%M")}
    # una sola pasada: un dato que contenga "__SESSIONS__" no se vuelve a sustituir (evita inyectar JS)
    html = re.sub(r"__(DAILY|SESSIONS|ATTRIB|OAPI|OTOOLS|FINDINGS|PLAN|GEN)__", lambda m: vals[m[1]], PAGE)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "tokens.db"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "report.html"))
    a = ap.parse_args()
    build(a.db, a.out)
