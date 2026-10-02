"""Local UI and API for tracing constrained System One database programs."""

from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import shutil
import sqlite3
from intentsql.database import connect, readonly_uri
import threading
import time
import uuid
from urllib.parse import urlsplit
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import __version__, mutations, read_engine as engine
from .jev_client import JevClient
from .semantic_read import run_read
from .request_routing import explicit_request_operation

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
_DEFAULT_WORKSPACE = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "IntentSQL"
                      if os.name == "nt" else
                      Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "intentsql")
_LEGACY_WORKSPACE = Path.home() / ".local/share/intentsql"
if not _LEGACY_WORKSPACE.exists():
    _LEGACY_WORKSPACE = Path.home() / ".local/share/jevql"
WORKSPACE = Path(os.environ.get("INTENTSQL_WORKSPACE") or
                 os.environ.get("JEVQL_WORKSPACE") or
                 (_LEGACY_WORKSPACE if _LEGACY_WORKSPACE.exists() and not _DEFAULT_WORKSPACE.exists()
                  else _DEFAULT_WORKSPACE)).expanduser()
DB_DIR = WORKSPACE / "databases"
BACKUP_DIR = WORKSPACE / "backups"
CONFIG_FILE = WORKSPACE / "connection.json"
PROVIDERS = {
    "jev": {"name": "Jev", "url": "https://api.typesafe.ai/v1/systemone",
            "model": "jev-latest", "requires_key": True},
    "openjev": {"name": "OpenJEV", "url": "https://api.codiv.ai/v1/systemone",
                "model": "openjev-latest", "requires_key": True},
    "laya": {"name": "Laya local", "url": "http://127.0.0.1:7861/v1/systemone",
             "model": "", "requires_key": False},
    "custom": {"name": "Custom System One", "url": "", "model": "",
               "requires_key": False},
}
RUN_LOCK = threading.Lock()
PENDING: dict[str, dict[str, Any]] = {}
UNDO: dict[str, dict[str, Any]] = {}
MAX_INSPECT_ROWS = 500
MAX_INSPECT_SECONDS = 5.0

app = FastAPI(title="IntentSQL", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])


@app.middleware("http")
async def local_asset_cache(request, call_next):
    # A website opened alongside this local app must not configure it or spend its key.
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def prepare_workspace() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for source in DATA_DIR.glob("*.db"):
        target = DB_DIR / source.name
        if not target.exists():
            shutil.copy2(source, target)


def _bundled_database_names() -> set[str]:
    return {path.name for path in DATA_DIR.glob("*.db")}


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("index.html", "style.css", "app.js", "favicon.svg"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


def database_path(database: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.db", database):
        raise HTTPException(400, "Invalid database name")
    prepare_workspace()
    path = DB_DIR / database
    if path.is_symlink() or path.resolve().parent != DB_DIR.resolve():
        raise HTTPException(400, "Database must be a file in the local workspace")
    if not path.is_file():
        raise HTTPException(404, "Database not found")
    return path


def _connection_store() -> dict[str, Any]:
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "Connection settings could not be read. Repair or remove connection.json in your IntentSQL workspace.") from exc
    if not isinstance(saved, dict):
        raise HTTPException(500, "Invalid connection settings. Remove connection.json in your IntentSQL workspace.")
    if isinstance(saved.get("profiles"), dict):
        return {"active": saved.get("active", "jev"), "profiles": saved["profiles"]}
    # The previous release stored one endpoint. Keep its key with that endpoint.
    url = saved.get("url", "")
    provider = next((name for name, template in PROVIDERS.items()
                     if name != "custom" and url == template["url"]), "custom") if url else "jev"
    return {"active": provider, "profiles": {provider: saved} if saved else {}}


def _profile(provider: str, store: dict[str, Any]) -> dict[str, Any]:
    template = PROVIDERS[provider]
    saved = store["profiles"].get(provider, {})
    if not isinstance(saved, dict):
        raise HTTPException(500, "Invalid provider profile in connection.json.")
    result = {"provider": provider, "name": template["name"],
              "url": saved.get("url", template["url"]),
              "model": saved.get("model", template["model"]),
              "api_key": saved.get("api_key", ""),
              "requires_key": template["requires_key"]}
    if provider == "jev":
        result["url"] = saved.get("url") or os.environ.get("SYSTEM_ONE_URL") or result["url"]
        result["model"] = saved.get("model") or os.environ.get("SYSTEM_ONE_MODEL") or result["model"]
        result["api_key"] = saved.get("api_key") or os.environ.get("SYSTEM_ONE_API_KEY") or result["api_key"]
    return result


def connection_settings() -> dict[str, Any]:
    store = _connection_store()
    provider = store["active"] if store["active"] in PROVIDERS else "jev"
    return _profile(provider, store)


def configure_engine() -> dict[str, Any]:
    config = connection_settings()
    engine.SYSTEM_ONE_URL = config["url"]
    engine.SYSTEM_ONE_MODEL = config["model"]
    engine.SYSTEM_ONE_API_KEY = config["api_key"]
    return config


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False, allow_nan=False))


def _stats_with_cost(usage: dict[str, Any], provider: str) -> dict[str, Any]:
    result = dict(usage)
    result["cost_usd"] = (
        (int(result.get("input_tokens") or 0) * engine.JEV_INPUT_USD_PER_MTOK
         + int(result.get("output_tokens") or 0) * engine.JEV_OUTPUT_USD_PER_MTOK) / 1_000_000
        if provider == "jev" else None
    )
    return result


@app.get("/")
def home() -> HTMLResponse:
    version = _asset_version()
    document = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
        "__ASSET_VERSION__", version)
    return HTMLResponse(document, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ready": True, "version": __version__}


@app.get("/api/connection")
def get_connection() -> dict[str, Any]:
    store = _connection_store()
    active = store["active"] if store["active"] in PROVIDERS else "jev"
    profiles = {name: _profile(name, store) for name in PROVIDERS}
    public = lambda profile: {key: profile[key] for key in
                              ("provider", "name", "url", "model", "requires_key")}
    return {**public(profiles[active]), "has_key": bool(profiles[active]["api_key"]),
            "presets": PROVIDERS,
            "profiles": {name: {**public(profile), "has_key": bool(profile["api_key"])}
                         for name, profile in profiles.items()}}


class ConnectionInput(BaseModel):
    provider: str = "jev"
    url: str = Field(max_length=2000)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=2000)


@app.post("/api/connection")
def save_connection(body: ConnectionInput) -> dict[str, Any]:
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "Unknown provider")
    body.url = body.url.strip()
    try:
        parsed = urlsplit(body.url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "Enter a valid endpoint URL and port (1–65535).") from exc
    if port is not None and not 1 <= port <= 65535:
        raise HTTPException(400, "Port must be between 1 and 65535.")
    local_http = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if (not (parsed.scheme == "https" or local_http) or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment):
        raise HTTPException(400, "Use HTTPS or a local HTTP endpoint")
    if PROVIDERS[body.provider]["requires_key"] and not body.model.strip():
        raise HTTPException(400, "This provider requires a model identifier")
    store = _connection_store()
    prior = store["profiles"].get(body.provider, {})
    store["active"] = body.provider
    store["profiles"][body.provider] = {
        "url": body.url.strip(), "model": body.model.strip(),
        "api_key": body.api_key.strip() or prior.get("api_key", "")}
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(store, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)
    return get_connection()


@app.get("/api/databases")
def databases() -> list[dict[str, Any]]:
    prepare_workspace()
    bundled = _bundled_database_names()
    return [{"id": path.name, "label": path.stem.replace("_", " ").title(),
             "size_bytes": path.stat().st_size, "deletable": path.name not in bundled}
            for path in sorted(DB_DIR.glob("*.db"))]


class ImportDatabase(BaseModel):
    path: str


@app.post("/api/databases")
def import_database(body: ImportDatabase) -> dict[str, Any]:
    source = Path(body.path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 100 * 1024 * 1024:
        raise HTTPException(400, "Select an existing SQLite file smaller than 100 MB")
    with source.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise HTTPException(400, "File is not a SQLite database")
    prepare_workspace()
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", source.stem)[:48] or "database"
    target = DB_DIR / f"{name}.db"
    if target.exists():
        raise HTTPException(409, "A database with this name already exists")
    with connect(readonly_uri(source), uri=True) as src, connect(target) as dst:
        src.backup(dst)
    return {"id": target.name}


class DeleteDatabase(BaseModel):
    confirm: bool = False


@app.delete("/api/databases/{database}")
def delete_database(database: str, body: DeleteDatabase) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation is required")
    if database in _bundled_database_names():
        raise HTTPException(400, "Bundled examples cannot be deleted")
    if not RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Wait for the active request to finish")
    try:
        path = database_path(database)
        for token, item in list(PENDING.items()):
            if item.get("database") == database:
                PENDING.pop(token, None)
        for token, item in list(UNDO.items()):
            if item.get("database") == database:
                backup = item.get("backup")
                if backup:
                    Path(backup).unlink(missing_ok=True)
                UNDO.pop(token, None)
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        return {"deleted": True, "database": database}
    finally:
        RUN_LOCK.release()


@app.get("/api/schema/{database}")
def schema(database: str) -> dict[str, Any]:
    path = database_path(database)
    with connect(readonly_uri(path), uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        discovered = engine.inspect_schema(conn)
        tables = []
        for table in discovered.tables.values():
            count = conn.execute(f"SELECT COUNT(*) FROM {engine.qident(table.name)}").fetchone()[0]
            tables.append({"name": table.name, "rows": count,
                           "columns": [asdict(col) for col in table.columns],
                           "foreign_keys": [asdict(fk) for fk in table.foreign_keys]})
    return {"database": database, "tables": tables}


class InspectQueryInput(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)


_INSPECT_PREFIX = re.compile(r"^\s*(SELECT|WITH|EXPLAIN|PRAGMA)\b", re.IGNORECASE)
_DENIED_SQLITE_ACTIONS = frozenset(
    value for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_ATTACH",
        "SQLITE_DETACH", "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT",
    ) if (value := getattr(sqlite3, name, None)) is not None
)


@app.get("/api/inspect/{database}/overview")
def inspect_overview(database: str) -> dict[str, Any]:
    """Return a bounded recent-row preview for every table in the working copy."""
    path = database_path(database)
    with connect(readonly_uri(path), uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        discovered = engine.inspect_schema(conn)
        tables = []
        for table in discovered.tables.values():
            quoted = engine.qident(table.name)
            row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            primary_key = [column.name for column in table.columns if column.pk]
            order_columns = primary_key or ["rowid"]
            order_sql = ", ".join(
                (name if name == "rowid" else engine.qident(name)) + " DESC"
                for name in order_columns)
            rows = conn.execute(
                f"SELECT * FROM {quoted} ORDER BY {order_sql} LIMIT 5").fetchall()
            tables.append({
                "name": table.name,
                "row_count": row_count,
                "columns": [asdict(col) for col in table.columns],
                "foreign_keys": [asdict(fk) for fk in table.foreign_keys],
                "rows": rows,
                "preview_order": order_columns,
            })
    return json_safe({"database": database, "tables": tables, "preview_rows": 5})


@app.post("/api/inspect/{database}")
def inspect_query(database: str, body: InspectQueryInput) -> dict[str, Any]:
    """Execute one bounded, read-only SQL statement against the working copy."""
    sql = body.sql.strip()
    if not _INSPECT_PREFIX.match(sql):
        raise HTTPException(400, "Inspector accepts read-only SELECT, WITH, EXPLAIN, or PRAGMA queries only")
    path = database_path(database)
    started = time.monotonic()
    try:
        with connect(readonly_uri(path), uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")

            def authorize(action: int, _arg1: str | None, _arg2: str | None,
                          _db: str | None, _trigger: str | None) -> int:
                return sqlite3.SQLITE_DENY if action in _DENIED_SQLITE_ACTIONS else sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() - started > MAX_INSPECT_SECONDS else 0,
                10_000,
            )
            cursor = conn.execute(sql)
            if cursor.description is None:
                raise HTTPException(400, "Inspector query did not return a result set")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchmany(MAX_INSPECT_ROWS + 1)
            truncated = len(rows) > MAX_INSPECT_ROWS
            rows = rows[:MAX_INSPECT_ROWS]
    except HTTPException:
        raise
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "interrupted" in message.casefold():
            message = f"Query exceeded the {MAX_INSPECT_SECONDS:g}-second inspector limit"
        raise HTTPException(400, f"SQLite error: {message}") from exc
    return {
        "database": database,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "max_rows": MAX_INSPECT_ROWS,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


class RunInput(BaseModel):
    database: str
    prompt: str = Field(min_length=3, max_length=2000)
    mode: str = "auto"


@app.post("/api/run")
def run(body: RunInput) -> StreamingResponse:
    path = database_path(body.database)
    if body.mode not in ("auto", "read", "change"):
        raise HTTPException(400, "Mode must be auto, read or change")
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    started = time.monotonic()

    def send(event: dict[str, Any]) -> None:
        event["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        events.put(json_safe(event))

    def worker() -> None:
        if not RUN_LOCK.acquire(blocking=False):
            send({"kind": "error", "message": "Another run is in progress; wait for it to finish."})
            events.put(None)
            return
        client: JevClient | None = None
        config: dict[str, Any] | None = None
        try:
            # Deletion may have occurred between accepting the request and
            # acquiring the lock. Revalidate before opening any SQLite handle.
            database_path(body.database)
            engine.reset_run_state()
            config = configure_engine()

            def forward(event: dict[str, Any]) -> None:
                usage = event.get("stats") or (client.usage_stats() if client else engine.usage_stats())
                send({**event, "stats": _stats_with_cost(usage, config["provider"])})

            engine.EVENT_SINK = forward
            send({"kind": "status", "message": "Inspecting schema and resolving the request"})
            if config["requires_key"] and not engine.SYSTEM_ONE_API_KEY:
                raise ValueError(f"Add an API key for {config['name']} in Connection settings.")
            client = JevClient(
                url=engine.SYSTEM_ONE_URL, model=engine.SYSTEM_ONE_MODEL,
                api_key=engine.SYSTEM_ONE_API_KEY, on_event=forward)
            selected_mode = body.mode
            operation_hint = None
            if selected_mode == "auto":
                surfaced = explicit_request_operation(body.prompt)
                if surfaced:
                    selected_mode, operation_hint = surfaced
                    forward({"kind": "skill", "name": "Request operation",
                             "selected": operation_hint or selected_mode,
                             "source": "explicit_request_operation", "usage": None})
                else:
                    routing = client.call(
                        {"stage": "request operation", "user_request": body.prompt},
                        {"operation": {"type": "choice",
                                       "instructions": "Does the user request a read-only query or a stored-data change?",
                                       "criteria": {"read": "Read, inspect, list, count, summarize, or query data.",
                                                    "change": "Insert, update, or delete stored rows."}}})
                    selected_mode = str(routing.answers["operation"]["choice"]).lower()
                    if selected_mode not in {"read", "change"}:
                        raise ValueError("System One did not resolve whether this request reads or changes data.")
            if selected_mode == "read":
                payload = run_read(path, body.prompt, client,
                    on_step=lambda step: forward({"kind": "skill", **step}), on_event=forward)
            else:
                engine.stats["jev_calls"] = client.calls
                engine.stats["input_tokens"] = client.input_tokens
                engine.stats["output_tokens"] = client.output_tokens
                payload = mutations.plan_mutation(
                    path, body.prompt, reset_state=False, operation_hint=operation_hint)
                if payload["supported"]:
                    token = uuid.uuid4().hex
                    for prior_token, prior in list(PENDING.items()):
                        if prior["database"] == body.database:
                            PENDING.pop(prior_token, None)
                    PENDING[token] = {"database": body.database, "plan": payload}
                    payload["commit_token"] = token
            if "stats" in payload:
                provider_usage = (client.usage_stats() if selected_mode == "read"
                                  else engine.usage_stats())
                usage = {**payload["stats"], **provider_usage}
                payload["stats"] = _stats_with_cost(usage, config["provider"])
            else:
                payload["stats"] = _stats_with_cost(engine.usage_stats(), config["provider"])
            send({"kind": "result", "result": payload})
        except Exception as exc:
            provider = config["provider"] if config else ""
            usage = client.usage_stats() if client is not None else engine.usage_stats()
            send({"kind": "error", "message": str(exc),
                  "stats": _stats_with_cost(usage, provider)})
        finally:
            engine.EVENT_SINK = None
            RUN_LOCK.release()
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            try:
                event = events.get(timeout=10)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class CommitInput(BaseModel):
    token: str
    confirm: bool


@app.post("/api/commit")
def commit(body: CommitInput) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation is required")
    pending = PENDING.pop(body.token, None)
    if pending is None:
        raise HTTPException(404, "Preview expired; run the request again")
    with RUN_LOCK:
        path = database_path(pending["database"])
        backup = BACKUP_DIR / f"{body.token}.db"
        with connect(path) as src, connect(backup) as dst:
            src.backup(dst)
        try:
            affected = mutations.commit_mutation(path, pending["plan"])
        except Exception as exc:
            backup.unlink(missing_ok=True)
            raise HTTPException(409, str(exc)) from exc
        for token, old in list(UNDO.items()):
            if old["database"] == pending["database"]:
                old["backup"].unlink(missing_ok=True)
                UNDO.pop(token, None)
        UNDO[body.token] = {"database": pending["database"], "backup": backup,
                            "after_fingerprint": mutations.fingerprint(path)}
        return {"affected": affected, "undo_token": body.token,
                "table": (pending["plan"].get("program") or {}).get("table"),
                "schema": schema(pending["database"])}


class UndoInput(BaseModel):
    token: str


@app.post("/api/undo")
def undo(body: UndoInput) -> dict[str, Any]:
    item = UNDO.get(body.token)
    if item is None:
        raise HTTPException(404, "Undo snapshot not found")
    with RUN_LOCK:
        path = database_path(item["database"])
        if mutations.fingerprint(path) != item["after_fingerprint"]:
            raise HTTPException(409, "Database changed after commit; automatic undo is unsafe")
        with connect(item["backup"]) as src, connect(path) as dst:
            src.backup(dst)
        item["backup"].unlink(missing_ok=True)
        UNDO.pop(body.token, None)
        return {"restored": True, "schema": schema(item["database"])}


# --- IntentCypher: Neo4j read prototype -------------------------------------
# Translates the IntentCypher answer into the same terminal result shape the
# query-studio UI renders for SQLite reads. Neo4j connection details come from
# the request (browser-stored) or fall back to .env / process environment.


class CypherConnectionInput(BaseModel):
    uri: str = ""
    username: str = ""
    password: str = ""
    database: str = ""


class CypherAskInput(CypherConnectionInput):
    question: str = Field(min_length=3, max_length=2000)


def _cypher_config(body: CypherConnectionInput):
    from intentcypher.connection import apply_env_file, connection_config
    apply_env_file()
    return connection_config(
        env={}, uri=body.uri or None, username=body.username or None,
        password=body.password or None, database=body.database or None)


@app.get("/api/neo4j/status")
def neo4j_status() -> dict[str, Any]:
    """Report whether a Neo4j connection can be resolved. Never returns secrets."""
    try:
        config = _cypher_config(CypherConnectionInput())
    except Exception:
        return {"configured": False}
    return {"configured": True, "uri": config.uri, "database": config.database}


@app.post("/api/neo4j/schema")
def neo4j_schema(body: CypherConnectionInput) -> dict[str, Any]:
    from intentcypher.connection import connect_readonly
    from intentcypher.graph_schema import load_schema
    try:
        config = _cypher_config(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    with RUN_LOCK:
        try:
            driver = connect_readonly(config)
        except Exception as exc:
            raise HTTPException(400, f"Could not reach Neo4j: {exc}") from exc
        try:
            graph = load_schema(driver, config.database)
        except Exception as exc:
            raise HTTPException(400, f"Schema inspection failed: {exc}") from exc
        finally:
            driver.close()
    return {
        "labels": [{"name": label,
                    "properties": [{"name": prop.name, "types": list(prop.types),
                                    "mandatory": prop.mandatory}
                                   for prop in graph.properties_for(label)]}
                   for label in graph.label_names],
        "relationships": [{"type": rel.rel_type, "from": rel.start_label,
                           "to": rel.end_label, "count": rel.count}
                          for rel in graph],
    }


def _cypher_display(query: str, parameters: dict[str, Any]) -> tuple[str, list[Any]]:
    """Translate $name parameters into the UI's positional '?' display form."""
    order: list[str] = []
    display = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*",
                     lambda match: (order.append(match.group(0)[1:]) or "?"), query)
    return display, [parameters[name] for name in order]


@app.post("/api/cypher")
def ask_cypher(body: CypherAskInput) -> dict[str, Any]:
    from intentcypher.semantic_cypher import run_read as run_cypher_read
    try:
        config = _cypher_config(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    with RUN_LOCK:
        try:
            answer = run_cypher_read(body.question.strip(), config, JevClient())
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
    rows = answer.rows
    columns = list(rows[0]) if rows else []
    # Whole-node returns carry nested maps; the results table shows scalars.
    tabular = [[json.dumps(value, default=str, ensure_ascii=False)
                if isinstance(value, (dict, list)) else value
                for value in row.values()] for row in rows]
    display_sql, display_params = _cypher_display(answer.query, answer.parameters) \
        if answer.query else ("", [])
    plan = answer.plan or {}
    relationship = plan.get("relationship")
    outputs = plan.get("output")
    program = {
        "operation": "SELECT",
        "base_table": plan.get("source_label"),
        "joins": ([f"{relationship['type']} → {relationship['other_label']}"]
                  if relationship else []),
        "outputs": ["whole nodes"] if outputs == "whole nodes"
                   else list(outputs or []),
        "related_outputs": plan.get("related_output") or [],
        "filters": [f"{item['property']} {item['operator']}"
                    + ("" if item.get("value") is None else f" {json.dumps(item['value'], default=str)}")
                    for item in plan.get("filters") or []],
        "filter_connector": "AND",
        "order_by": [{"key": item["property"], "direction": item["direction"]}
                     for item in plan.get("ordering") or []],
        "limit": plan.get("limit"),
        "distinct": "UNSET",
        "language": "cypher",
    }
    return json_safe({
        "status": answer.status,
        "reason": answer.reason,
        "supported": answer.status == "answered",
        "columns": columns,
        "rows": tabular,
        "total_rows": len(rows),
        "truncated": False,
        "program": program,
        "sql": display_sql,
        "params": display_params,
        "cypher": answer.query,
        "parameters": answer.parameters,
        "stats": {"jev_calls": answer.jev_calls,
                  "input_tokens": answer.input_tokens,
                  "output_tokens": answer.output_tokens,
                  "usage_complete": True},
        "trace": answer.trace,
    })
