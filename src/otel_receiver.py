"""Receptor OTLP/HTTP-JSON mínimo para la telemetría de Claude Code. Guarda eventos y métricas en tokens.db.

Uso: python otel_receiver.py [--port 4318] [--db tokens.db] [--dump crudo.jsonl]
Claude Code debe exportar con OTEL_EXPORTER_OTLP_PROTOCOL=http/json (lo configura install.ps1).
Solo guarda atributos de una lista blanca: nunca prompts, respuestas ni parámetros de herramientas.
"""
import argparse, json, logging, os, sqlite3, sys, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = """
CREATE TABLE IF NOT EXISTS otel_events(
  ts TEXT, session TEXT, event TEXT, model TEXT, query_source TEXT,
  input INT, output INT, cache_read INT, cache_create INT, cost_usd REAL,
  tool_name TEXT, success TEXT, duration_ms REAL, skill TEXT, plugin TEXT, mcp_server TEXT);
CREATE INDEX IF NOT EXISTS otel_events_session ON otel_events(session);
CREATE TABLE IF NOT EXISTS otel_metrics(
  ts TEXT, name TEXT, value REAL, session TEXT, model TEXT, type TEXT, query_source TEXT,
  skill TEXT, plugin TEXT, mcp_server TEXT);
CREATE INDEX IF NOT EXISTS otel_metrics_session ON otel_metrics(session);
"""
KEEP = {"api_request", "api_error", "tool_result"}
MAX_BODY = 5 * 1024 * 1024  # cualquier usuario local puede enviar: acotar memoria
lock = threading.Lock()


def val(v):
    """Valor OTLP {"stringValue":..|"intValue":..|"doubleValue":..|"boolValue":..} -> Python."""
    for k in ("stringValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    return None


def attrs(lst):
    """Strings truncados: los atributos vienen de un puerto local sin autenticar."""
    return {a["key"]: v[:200] if isinstance(v := val(a.get("value", {})), str) else v for a in lst or []}


def iso(nano):
    try:
        return datetime.fromtimestamp(int(nano) / 1e9, timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_logs(doc):
    for rl in doc.get("resourceLogs", []):
        res = attrs(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for r in sl.get("logRecords", []):
                a = {**res, **attrs(r.get("attributes"))}
                ev = str(a.get("event.name") or val(r.get("body", {})) or "").replace("claude_code.", "")
                if ev not in KEEP:  # hooks, plugin_loaded, etc. son ruido (22 eventos por sesión)
                    continue
                yield (iso(r.get("timeUnixNano")), a.get("session.id"), ev,
                       a.get("model"), a.get("query_source"), num(a.get("input_tokens")), num(a.get("output_tokens")),
                       num(a.get("cache_read_tokens")), num(a.get("cache_creation_tokens")), num(a.get("cost_usd")),
                       a.get("tool_name"), None if a.get("success") is None else str(a.get("success")),
                       num(a.get("duration_ms")), a.get("skill.name"), a.get("plugin.name"), a.get("mcp_server.name"))


def parse_metrics(doc):
    for rm in doc.get("resourceMetrics", []):
        res = attrs(rm.get("resource", {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                for dp in (m.get("sum") or m.get("gauge") or {}).get("dataPoints", []):
                    a = {**res, **attrs(dp.get("attributes"))}
                    v = dp.get("asDouble", dp.get("asInt"))
                    if v is None:
                        continue
                    yield (iso(dp.get("timeUnixNano")), m.get("name", "").replace("claude_code.", ""), float(v),
                           a.get("session.id"), a.get("model"), a.get("type"), a.get("query_source"),
                           a.get("skill.name"), a.get("plugin.name"), a.get("mcp_server.name"))


def store(db_path, path, doc):
    if path == "/v1/logs":
        sql, rows = "INSERT INTO otel_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(parse_logs(doc))
    elif path == "/v1/metrics":
        sql, rows = "INSERT INTO otel_metrics VALUES(?,?,?,?,?,?,?,?,?,?)", list(parse_metrics(doc))
    else:
        return 0
    with lock:
        db = sqlite3.connect(db_path, timeout=30)
        db.executemany(sql, rows)
        db.commit()
        db.close()
    return len(rows)


def make_handler(db_path, dump):
    class H(BaseHTTPRequestHandler):
        timeout = 15  # una conexión lenta no retiene el hilo para siempre

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.send_error(413 if n > MAX_BODY else 400)
                return
            body = self.rfile.read(n)
            try:
                doc = json.loads(body)
                if dump:
                    with lock, open(dump, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"path": self.path, "doc": doc}) + "\n")
                store(db_path, self.path, doc)
            except Exception as e:  # nunca devolver error a Claude Code por un payload raro
                logging.warning("payload ignorado en %s: %s", self.path, e)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    return H


def init_db(db_path):
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")  # convive con collect.py escribiendo a la vez
    db.executescript(SCHEMA)
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tokens.db"))
    ap.add_argument("--dump", help="guarda además cada payload crudo (solo para depurar)")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)  # data/ no viene en el repo
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    init_db(a.db)
    logging.info("escuchando en 127.0.0.1:%d", a.port)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.db, a.dump))
    srv.daemon_threads = True
    srv.serve_forever()
