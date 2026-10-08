"""Activa/desactiva la telemetría OTel de Claude Code para todos los usuarios, fusionando (no pisando)
el managed-settings.json de la máquina. Uso: python managed_settings.py apply|remove [--file RUTA] [--port 4318]

apply: respalda el archivo original (una sola vez), añade las claves env de abajo y recuerda si lo creó.
remove: quita solo esas claves; si el archivo lo creó apply y queda vacío, lo borra.
"""
import argparse, json, os, shutil

DEFAULT = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ClaudeCode", "managed-settings.json")


def env(port):
    return {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json", "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}"}


# Claves que versiones anteriores añadían y ya no: se quitan al aplicar y al desinstalar.
# OTEL_LOG_TOOL_DETAILS enviaba detalles de herramientas (comandos, rutas) por el puerto local.
LEGACY = ("OTEL_LOG_TOOL_DETAILS",)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def apply(path, port=4318):
    doc, marker = load(path), path + ".tokencounter-created"
    if doc is None:
        doc = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(marker, "w").close()
    elif not os.path.exists(path + ".bak-tokencounter"):
        shutil.copy2(path, path + ".bak-tokencounter")
    e = doc.setdefault("env", {})
    for k in LEGACY:
        e.pop(k, None)
    e.update(env(port))
    save(path, doc)


def remove(path, port=4318):
    doc, marker = load(path), path + ".tokencounter-created"
    if doc is None:
        return
    for k in (*env(port), *LEGACY):
        doc.get("env", {}).pop(k, None)
    if not doc.get("env", True):
        doc.pop("env")
    if not doc and os.path.exists(marker):
        os.remove(path)
    else:
        save(path, doc)
    if os.path.exists(marker):
        os.remove(marker)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["apply", "remove"])
    ap.add_argument("--file", default=DEFAULT)
    ap.add_argument("--port", type=int, default=4318)
    a = ap.parse_args()
    {"apply": apply, "remove": remove}[a.action](a.file, a.port)
