import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import json, os, sqlite3, subprocess, tempfile
from collect import collect, is_link, lines


def line(mid, out, typ="assistant"):
    return json.dumps({"type": typ, "cwd": "/p", "sessionId": "s1", "timestamp": "2026-01-01T10:00:00Z",
                       "message": {"id": mid, "model": "m", "usage": {"input_tokens": 5, "output_tokens": out}}})


def test_dedup_idempotent_corrupt():
    root = tempfile.mkdtemp()
    d = os.path.join(root, "ana", ".claude", "projects", "p")
    os.makedirs(d)
    with open(os.path.join(d, "s.jsonl"), "w") as f:
        f.write("\n".join([line("a", 2), line("a", 1093), line("a", 7), "{roto", line("b", 10, "user"), line("c", 4)]))
    db = os.path.join(root, "t.db")
    collect(root, db)
    collect(root, db)
    rows = dict(sqlite3.connect(db).execute("SELECT id, output FROM usage"))
    assert rows == {"ana:a": 1093, "ana:c": 4}, rows


def test_attrib():
    root = tempfile.mkdtemp()
    d = os.path.join(root, "ana", ".claude", "projects", "p")
    os.makedirs(d)

    def asst(mid, out, inp, cc, tools):
        return json.dumps({"type": "assistant", "sessionId": "s", "timestamp": "t", "message": {"id": mid, "model": "m",
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cc},
            "content": [{"type": "tool_use", "name": n, "input": i} for n, i in tools] or [{"type": "text", "text": "x"}]}})
    lines = [json.dumps({"type": "user", "message": {"content": "hola"}}),
             asst("a", 100, 10, 90, [("Bash", {}), ("mcp__slack__post", {})]),   # entrada 100 -> prompt
             json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
             asst("b", 40, 20, 180, [("Skill", {"skill": "tdd"})]),              # entrada 200 -> Bash y slack (100 c/u)
             asst("c", 8, 0, 50, [])]                                              # entrada 50 -> tdd
    open(os.path.join(d, "s.jsonl"), "w").write("\n".join(lines))
    db = os.path.join(root, "t.db")
    collect(root, db)
    collect(root, db)
    r = {(c, n): (o, i) for c, n, o, i in sqlite3.connect(db).execute("SELECT cat, name, SUM(out_tok), SUM(in_tok) FROM attrib GROUP BY 1,2")}
    assert r[("Herramienta", "Bash")] == (50, 100) and r[("MCP", "slack")] == (50, 100), r
    assert r[("Skill", "tdd")] == (40, 50) and r[("Sistema", "prompt / contexto inicial")] == (0, 100), r
    assert r[("Sistema", "texto y razonamiento")] == (8, 0), r


def test_untrusted_fields():
    root = tempfile.mkdtemp()
    d = os.path.join(root, "ana", ".claude", "projects", "p")
    os.makedirs(d)
    bad = {"type": "assistant", "cwd": "x" * 5000, "sessionId": "s", "timestamp": "2026-01-01T10:00:00",  # sin zona
           "message": {"id": "a", "model": "m", "usage": {"input_tokens": "abc", "output_tokens": -5, "cache_read_input_tokens": 7}}}
    open(os.path.join(d, "s.jsonl"), "w").write(json.dumps(bad))
    db = os.path.join(root, "t.db")
    collect(root, db)
    r = sqlite3.connect(db).execute("SELECT ts, input, output, cache_read, length(project) FROM usage").fetchall()
    assert r == [("", 0, 0, 7, 300)], r


def test_skip_junction():
    root = tempfile.mkdtemp()
    victim = os.path.join(root, "bob", ".claude", "projects", "p")
    os.makedirs(victim)
    open(os.path.join(victim, "s.jsonl"), "w").write(line("x", 1))
    os.makedirs(os.path.join(root, "eve", ".claude", "projects"))
    link = os.path.join(root, "eve", ".claude", "projects", "robado")
    if os.name != "nt" or subprocess.run(["cmd", "/c", "mklink", "/J", link, victim], capture_output=True).returncode:
        return  # sin junctions en esta plataforma
    assert is_link(link)
    db = os.path.join(root, "t.db")
    collect(root, db)
    assert [r[0] for r in sqlite3.connect(db).execute("SELECT id FROM usage")] == ["bob:x"]


def test_long_line():
    import io
    nl = chr(10)
    f = io.StringIO(nl.join(["ok1", "x" * 25, "y" * 30, "ok2", "z" * 10, "fin"]))
    # 10 caracteres + salto ya supera el límite de 10: también se descarta
    assert list(lines(f, 10)) == ["ok1" + nl, "ok2" + nl, "fin"]


if __name__ == "__main__":
    test_dedup_idempotent_corrupt()
    test_attrib()
    test_untrusted_fields()
    test_skip_junction()
    test_long_line()
    print("ok")
