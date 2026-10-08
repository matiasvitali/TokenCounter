import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import os, sqlite3, tempfile
from collect import SCHEMA
from report import build, findings


def test_findings_boot_and_bg():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    rows = []
    for s in range(5):  # 5 sesiones que arrancan con 60k de contexto -> boot
        rows.append((f"b{s}", "ana", "p", f"s{s}", "sonnet", f"2026-01-0{s + 1}T10:00:00Z", 1000, 500, 59000, 0, 0, 0, 59000))
    for k in range(12):  # 12 respuestas mínimas sobre 80k tras 3 min de pausa el mismo día -> bg
        rows.append((f"g{k}", "ana", "p", "sg", "sonnet", f"2026-02-01T{10 + k // 20:02d}:{k * 3:02d}:00Z", 10, 20, 0, 80000, 0, 0, 0))
    db.executemany("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    f = {r[1]: r for r in findings(db)}
    assert f["boot"][2] == "media" and f["boot"][7] == 5 * (60000 - 8000) + (80010 - 8000), f["boot"]
    assert f["bg"][7] == 11 * 80010, f["bg"]  # la primera fila no tiene pausa previa


def test_no_injection():
    d = tempfile.mkdtemp()
    path, out = os.path.join(d, "t.db"), os.path.join(d, "r.html")
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    evil = '__SESSIONS__</script><!--<img src=x onerror=alert(1)>'
    db.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("a", "ana", evil, "s", "m", "2026-01-01T10:00:00", 1, 2, 0, 0, 0, 0, 0))  # ts sin zona: no debe romper
    db.commit()
    db.close()
    build(path, out)
    html = open(out, encoding="utf-8").read()
    script = html.split("<script>")[1]
    assert html.count("</script>") == 1 and "<!--" not in script and "<img" not in script
    assert "__SESSIONS__" + chr(92) + "u003c/script" in script  # el dato llega literal, sin sustituir


if __name__ == "__main__":
    test_findings_boot_and_bg()
    test_no_injection()
    print("ok")
