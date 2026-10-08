import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import os, sqlite3, tempfile
import otel_receiver as o


def A(**kw):
    return [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}} for k, v in kw.items()]


LOGS = {"resourceLogs": [{"resource": {"attributes": A(**{"service.name": "claude-code"})}, "scopeLogs": [{"logRecords": [
    {"timeUnixNano": "1790000000000000000", "body": {"stringValue": "claude_code.api_request"}, "attributes": A(**{
        "event.name": "api_request", "session.id": "s1", "model": "m", "query_source": "main", "input_tokens": "2" and 2,
        "output_tokens": 85, "cache_read_tokens": 0, "cache_creation_tokens": 30458, "cost_usd": "0.12", "user.email": "x@y.z"})},
    {"timeUnixNano": "1790000001000000000", "attributes": A(**{
        "event.name": "tool_result", "session.id": "s1", "tool_name": "Read", "success": "true", "duration_ms": "10",
        "tool_input": "SECRETO"})},
    {"attributes": A(**{"event.name": "hook_registered", "session.id": "s1"})}]}]}]}
METRICS = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{"metrics": [
    {"name": "claude_code.token.usage", "sum": {"dataPoints": [
        {"asInt": "89", "timeUnixNano": "1790000000000000000", "attributes": A(**{"type": "output", "model": "m", "session.id": "s1"})}]}}]}]}]}


def test_store():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    o.init_db(db)
    assert o.store(db, "/v1/logs", LOGS) == 2  # el hook se descarta
    assert o.store(db, "/v1/metrics", METRICS) == 1
    c = sqlite3.connect(db)
    ev = c.execute("SELECT event, session, output, cache_create, cost_usd, tool_name, success FROM otel_events ORDER BY ts").fetchall()
    assert ev == [("api_request", "s1", 85.0, 30458.0, 0.12, None, None), ("tool_result", "s1", None, None, None, "Read", "true")], ev
    assert c.execute("SELECT name, value, type FROM otel_metrics").fetchall() == [("token.usage", 89.0, "output")]
    assert "SECRETO" not in str(c.execute("SELECT * FROM otel_events").fetchall())  # lista blanca


def test_managed_settings():
    import json, managed_settings as m
    d = tempfile.mkdtemp()
    f = os.path.join(d, "managed-settings.json")
    m.apply(f)  # no existía: lo crea
    assert json.load(open(f))["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    m.remove(f)
    assert not os.path.exists(f) and not os.path.exists(f + ".tokencounter-created")
    json.dump({"permissions": {"deny": ["x"]}, "env": {"MIO": "1"}}, open(f, "w"))  # existía: se conserva lo ajeno
    m.apply(f)
    m.remove(f)
    assert json.load(open(f)) == {"permissions": {"deny": ["x"]}, "env": {"MIO": "1"}}
    assert os.path.exists(f + ".bak-tokencounter")


def test_limits():
    import http.client, threading
    from http.server import ThreadingHTTPServer
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    o.init_db(db)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), o.make_handler(db, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    c.putrequest("POST", "/v1/logs")
    c.putheader("Content-Length", str(o.MAX_BODY + 1))
    c.endheaders()  # no se envía el cuerpo: debe rechazarse sin leerlo
    assert c.getresponse().status == 413
    srv.shutdown()
    big = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": A(**{
        "event.name": "tool_result", "session.id": "s", "tool_name": "x" * 10000})}]}]}]}
    o.store(db, "/v1/logs", big)
    assert sqlite3.connect(db).execute("SELECT length(tool_name) FROM otel_events").fetchone() == (200,)


def test_managed_legacy():
    import json, managed_settings as m
    f = os.path.join(tempfile.mkdtemp(), "managed-settings.json")
    json.dump({"env": {"OTEL_LOG_TOOL_DETAILS": "1"}}, open(f, "w"))  # instalación anterior
    m.apply(f)
    assert "OTEL_LOG_TOOL_DETAILS" not in json.load(open(f))["env"]


if __name__ == "__main__":
    test_managed_settings()
    test_limits()
    test_managed_legacy()
    test_store()
    print("ok")
