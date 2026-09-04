from __future__ import annotations

import json
import os
import socket
import ssl
from pathlib import Path
from urllib.parse import urlparse


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ARCHIVE_ROOT / ".env"


def load_neon_value() -> str:
    for key in ("NEON_DATABASE_KEY", "DATABASE_URL"):
        if os.environ.get(key):
            return os.environ[key].strip()
    if not ENV_PATH.is_file():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"NEON_DATABASE_KEY", "DATABASE_URL"}:
            return value.strip()
    return ""


def classify(value: str) -> str:
    if value.startswith(("postgres://", "postgresql://")):
        return "postgres_dsn"
    if value.startswith("napi_"):
        return "neon_api_key"
    if value:
        return "present_other"
    return "missing"


def tcp_tls_probe(dsn: str) -> dict[str, object]:
    parsed = urlparse(dsn)
    host = parsed.hostname
    port = parsed.port or 5432
    if not host:
        return {"status": "invalid_dsn", "check": "parse", "ok": False}
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=host):
            return {"status": "ok", "check": "tls_socket", "ok": True, "port": port}


def scrub_exception(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if "permission denied" in lowered:
        return "connection_blocked_by_local_network_policy"
    if "timeout" in lowered:
        return "connection_timed_out"
    if "could not translate host name" in lowered or "name or service not known" in lowered:
        return "dns_resolution_failed"
    if "authentication failed" in lowered:
        return "authentication_failed"
    return exc.__class__.__name__


def main() -> int:
    value = load_neon_value()
    key_type = classify(value)
    result: dict[str, object] = {
        "env_path": str(ENV_PATH),
        "present": bool(value),
        "key_type": key_type,
        "ok": False,
    }
    if key_type == "missing":
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if key_type != "postgres_dsn":
        result["status"] = "present_but_not_dsn"
        print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        import psycopg  # type: ignore
    except Exception:
        psycopg = None
    if psycopg is not None:
        try:
            with psycopg.connect(value, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    row = cur.fetchone()
            if not row or row[0] != 1:
                raise RuntimeError("Neon SELECT 1 returned an unexpected result")
            result["ok"] = True
            result["status"] = "ok"
            result["check"] = "psycopg_select_1"
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            result["status"] = "connection_failed"
            result["check"] = "psycopg_select_1"
            result["error_code"] = scrub_exception(exc)
            print(json.dumps(result, ensure_ascii=False))
            return 1
    try:
        import psycopg2  # type: ignore
    except Exception:
        psycopg2 = None
    if psycopg2 is not None:
        try:
            with psycopg2.connect(value, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    row = cur.fetchone()
            if not row or row[0] != 1:
                raise RuntimeError("Neon SELECT 1 returned an unexpected result")
            result["ok"] = True
            result["status"] = "ok"
            result["check"] = "psycopg2_select_1"
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            result["status"] = "connection_failed"
            result["check"] = "psycopg2_select_1"
            result["error_code"] = scrub_exception(exc)
            print(json.dumps(result, ensure_ascii=False))
            return 1
    try:
        probe = tcp_tls_probe(value)
    except Exception as exc:
        result["status"] = "connection_failed"
        result["check"] = "tls_socket"
        result["error_code"] = scrub_exception(exc)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    result.update(probe)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
