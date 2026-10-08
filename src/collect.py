"""Barre los transcripts de Claude Code de todos los usuarios y los guarda en SQLite.

Uso: python collect.py [--root C:\\Users] [--db tokens.db]
Idempotente: se puede ejecutar cada pocos minutos. Cada respuesta se guarda una vez
(clave user+message.id) conservando el registro con mayor output_tokens.
"""
import argparse, json, os, sqlite3, stat, sys, logging
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage(
  id TEXT PRIMARY KEY, user TEXT, project TEXT, session TEXT, model TEXT, ts TEXT,
  input INT, output INT, cache_create INT, cache_read INT,
  side INT, cache_5m INT, cache_1h INT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS attrib(
  id TEXT PRIMARY KEY, user TEXT, session TEXT, ts TEXT, cat TEXT, name TEXT, calls INT, out_tok REAL, in_tok REAL);
"""
UPSERT = """
INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET input=excluded.input, output=excluded.output,
  cache_create=excluded.cache_create, cache_read=excluded.cache_read, ts=excluded.ts,
  side=excluded.side, cache_5m=excluded.cache_5m, cache_1h=excluded.cache_1h
WHERE excluded.output >= usage.output"""  # >=: reprocesar rellena las columnas nuevas de filas antiguas


def is_link(p):
    """Symlink, junction u otro reparse point: SYSTEM no debe seguirlos (un usuario podría apuntar al perfil de otro)."""
    try:
        st = os.lstat(p)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def transcripts(root):
    """(usuario, ruta) de cada .jsonl en <root>/<usuario>/.claude/projects, sin atravesar enlaces."""
    for user in sorted(os.listdir(root)):
        base = os.path.join(root, user, ".claude", "projects")
        if any(is_link(p) for p in (os.path.join(root, user), os.path.dirname(base), base)):
            continue
        for d, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [x for x in dirs if not is_link(os.path.join(d, x))]
            for f in files:
                p = os.path.join(d, f)
                if f.endswith(".jsonl") and not is_link(p):
                    yield user, p


MAX_LINE = 16 * 1024 * 1024  # caracteres; la línea real más larga medida ronda 1 MB


def lines(f, limit=MAX_LINE):
    """Líneas de hasta `limit` caracteres. Las más largas se descartan leyéndolas por trozos, así que la memoria
    queda acotada aunque un usuario escriba una línea de varios GB (se pierde esa línea: un uso o una marca de prompt)."""
    while line := f.readline(limit):
        if len(line) == limit and not line.endswith("\n"):
            while (rest := f.readline(limit)) and not rest.endswith("\n"):
                pass
            logging.warning("línea de más de %d caracteres descartada en %s", limit, getattr(f, "name", "?"))
            continue
        yield line


def toint(v):
    """Contador de tokens del transcript (lo controla el usuario): entero >= 0 o 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def txt(v, n=300):
    return str(v)[:n] if v else "?"


def stamp(v):
    """ISO con zona horaria o '' (un ts sin zona rompería las comparaciones del reporte)."""
    try:
        return v if isinstance(v, str) and len(v) < 40 and datetime.fromisoformat(v.replace("Z", "+00:00")).tzinfo else ""
    except ValueError:
        return ""


def default_root():
    return r"C:\Users" if os.name == "nt" else "/home"


PROMPT, TEXT = ("Sistema", "prompt / contexto inicial"), ("Sistema", "texto y razonamiento")


def categorize(name, inp):
    """(categoría, nombre) de una llamada a herramienta."""
    name = txt(name, 200)
    if name.startswith("mcp__plugin_"):
        return "Plugin", name.split("__")[1]
    if name.startswith("mcp__"):
        return "MCP", name.split("__")[1]
    if name == "Skill":
        sk = txt((inp or {}).get("skill"), 200)
        return ("Plugin", sk) if ":" in sk else ("Skill", sk)
    if name in ("Task", "Agent"):
        return "Subagente", txt((inp or {}).get("subagent_type") or "general", 200)
    return "Herramienta", name


def parse_file(path, user):
    """-> (filas usage, filas attrib). Una respuesta ocupa varias líneas con el mismo message.id: se unen sus
    herramientas y se queda el uso con más output_tokens. La salida de cada respuesta se reparte entre sus
    herramientas; la entrada nueva (input+cache_create) entre las herramientas de la respuesta anterior,
    cuyos resultados entraron al contexto (estimación)."""
    msgs, order = {}, []  # message.id -> datos; order: ids y marcas de prompt de usuario
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in lines(f):
            try:
                d = json.loads(line)
                m = d.get("message") or {}
                content = m.get("content")
                if d.get("type") == "user":
                    if isinstance(content, str) or any(b.get("type") == "text" for b in content or [] if isinstance(b, dict)):
                        order.append(None)  # prompt del usuario (no un tool_result)
                    continue
                u = m.get("usage")
                if d.get("type") != "assistant" or not u:
                    continue
                key = m.get("id") or d.get("requestId") or d.get("uuid")
                if not key or not isinstance(key, str):
                    continue
                if key not in msgs:
                    msgs[key] = {"tools": [], "side": bool(d.get("isSidechain")), "best": None}
                    order.append(key)
                x = msgs[key]
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        t = categorize(b.get("name") or "?", b.get("input"))
                        if t not in x["tools"]:
                            x["tools"].append(t)
                o = toint(u.get("output_tokens"))
                cd = u.get("cache_creation") or {}
                if x["best"] is None or o >= x["best"][0]:
                    x["best"] = (o, (f"{user}:{key[:200]}", user, txt(d.get("cwd")), txt(d.get("sessionId"), 100),
                                     txt(m.get("model"), 100), stamp(d.get("timestamp")),
                                     toint(u.get("input_tokens")), o, toint(u.get("cache_creation_input_tokens")),
                                     toint(u.get("cache_read_input_tokens")), int(bool(d.get("isSidechain"))),
                                     toint(cd.get("ephemeral_5m_input_tokens")), toint(cd.get("ephemeral_1h_input_tokens"))))
            except (ValueError, AttributeError, TypeError):
                continue  # línea corrupta o formato inesperado
    usage, att, prev = [], {}, PROMPT_LIST
    for key in order:
        if key is None:
            prev = PROMPT_LIST
            continue
        x = msgs[key]
        row = x["best"][1]
        usage.append(row)
        _, _, _, sess, _, ts, i, o, cc = row[:9]
        for kind, tools, amount in (("o", x["tools"] or [("Subagente", "texto y razonamiento") if x["side"] else TEXT], o),
                                    ("i", prev, i + cc)):
            for cat, name in tools:
                r = att.setdefault(f"{user}:{key}:{kind}:{cat}:{name}", [user, sess, ts, cat, name, 0, 0.0, 0.0])
                r[5] += 1 if kind == "o" else 0
                r[6 if kind == "o" else 7] += amount / len(tools)
        prev = x["tools"] or [TEXT]
    return usage, [(k, *v) for k, v in att.items()]


PROMPT_LIST = [PROMPT]


def collect(root, db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    if "side" not in [r[1] for r in db.execute("PRAGMA table_info(usage)")]:  # BD anterior: migrar y reprocesar todo
        for c in ("side", "cache_5m", "cache_1h"):
            db.execute(f"ALTER TABLE usage ADD COLUMN {c} INT")
        db.execute("DELETE FROM files")
    if db.execute("PRAGMA user_version").fetchone()[0] < 1:  # v1: campos validados; reprocesar lo ya guardado
        db.execute("DELETE FROM files")
        db.execute("PRAGMA user_version=1")
    seen = dict(db.execute("SELECT path, mtime FROM files"))
    n = 0
    for user, path in transcripts(root):
        try:
            mtime = os.path.getmtime(path)
            if seen.get(path) == mtime:
                continue
            usage, att = parse_file(path, user)
            db.executemany(UPSERT, usage)
            db.executemany("INSERT OR REPLACE INTO attrib VALUES(?,?,?,?,?,?,?,?,?)", att)
            db.execute("INSERT OR REPLACE INTO files VALUES(?,?)", (path, mtime))
            n += 1
        except OSError as e:
            logging.warning("no se pudo leer %s: %s", path, e)
    db.commit()
    db.close()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=default_root())
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tokens.db"))
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)  # data/ no viene en el repo
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.info("archivos procesados: %d", collect(a.root, a.db))
