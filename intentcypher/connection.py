"""Neo4j connection: .env / environment / programmatic, read-only access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError

_ENV_KEYS = ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE")


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, comments with #, no interpolation."""
    values: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return values
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), value.strip().strip("'\""))
    return values


def connection_config(env: Mapping[str, str] | None = None,
                      uri: str | None = None, username: str | None = None,
                      password: str | None = None,
                      database: str | None = None) -> "Neo4jConfig":
    """Resolve connection details: explicit arguments first, then .env, then process env."""
    file_env = load_env_file()
    source: Mapping[str, str] = {**file_env, **dict(os.environ if env is None else env)}
    missing = [key for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
               if not (source.get(key) or "").strip()]
    if missing and not (uri and username and password):
        raise ConnectionError(
            f"Missing Neo4j connection settings: {', '.join(missing)}. "
            "Provide them via a .env file (NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, "
            "NEO4J_DATABASE), environment variables, or explicit arguments.")
    return Neo4jConfig(
        uri=uri or source["NEO4J_URI"],
        username=username or source["NEO4J_USERNAME"],
        password=password or source["NEO4J_PASSWORD"],
        database=database or source.get("NEO4J_DATABASE") or None,
    )


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    username: str
    password: str
    database: str | None = None


def connect_readonly(config: Neo4jConfig) -> Driver:
    """Open a driver; reads route to followers. Reads never write."""
    driver = GraphDatabase.driver(config.uri, auth=(config.username, config.password))
    driver.verify_connectivity()
    return driver


def read_query(driver: Driver, config: Neo4jConfig, query: str,
                parameters: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Execute a read through the read routing policy."""
    from neo4j import RoutingControl
    records, _, _ = driver.execute_query(
        query, parameters or {}, database_=config.database,
        routing_=RoutingControl.READ)
    return (dict(record) for record in records)


__all__ = ["Neo4jConfig", "connect_readonly",
           "connection_config", "load_env_file", "read_query", "Neo4jError",
           "apply_env_file"]


def apply_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Export .env values into os.environ without overriding real variables.

    Used for System One settings (SYSTEM_ONE_URL/MODEL/API_KEY/RETRIES) that
    JevClient reads from the process environment."""
    applied = {}
    for key, value in load_env_file(path).items():
        if key not in os.environ and value:
            os.environ[key] = value
            applied[key] = key
    return applied
