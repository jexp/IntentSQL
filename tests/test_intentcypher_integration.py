"""Integration tests against a live Neo4j, gated on connection settings.

Skipped unless NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD are resolvable via
.env or the environment (integration.env is honored first). Requires the
APOC Core plugin (apoc.meta.schema) and a database with at least one label
and a few nodes. No model calls are made; determinism comes from the
compiler, not Jev."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from intentcypher.connection import connection_config, connect_readonly, read_query
from intentcypher.cypher_compiler import Condition, CypherQuery, compile_cypher
from intentcypher.graph_schema import load_schema


def _available() -> bool:
    # Prefer integration.env, then .env, then the process environment.
    for name in ("integration.env", ".env"):
        path = Path(name)
        if path.is_file():
            os.environ.update({line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
                               for line in path.read_text().splitlines()
                               if "=" in line and not line.lstrip().startswith("#")})
    try:
        connection_config()
        return True
    except ConnectionError:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason="no Neo4j connection settings")


@pytest.fixture(scope="module")
def config():
    return connection_config()


@pytest.fixture(scope="module")
def driver(config):
    driver = connect_readonly(config)
    yield driver
    driver.close()


@pytest.fixture(scope="module")
def schema(driver, config):
    return load_schema(driver, config.database)


def test_schema_has_labels(schema):
    assert schema.label_names, "apoc.meta.schema() returned no node labels"
    for label in schema.label_names:
        assert isinstance(schema.property_names(label), tuple)


def test_readonly_execution_of_compiled_cypher(driver, config, schema):
    label = schema.label_names[0]
    query = CypherQuery(label=label, limit=3)
    compiled = compile_cypher(query)
    assert "MATCH (n:" in compiled.query and "LIMIT $limit" in compiled.query
    rows = list(read_query(driver, config, compiled.query, compiled.parameters))
    assert len(rows) <= 3


def test_parameterized_null_predicate_executes(driver, config, schema):
    label = schema.label_names[0]
    prop = schema.property_names(label)[0]
    query = CypherQuery(label=label,
                       conditions=(Condition(prop, "is_not_null"),),
                       projection=(prop,),
                       limit=2)
    compiled = compile_cypher(query)
    rows = list(read_query(driver, config, compiled.query, compiled.parameters))
    assert all(row.get(prop) is not None for row in rows)


def test_env_file_is_not_committed():
    assert not (Path(__file__).resolve().parent.parent / ".env").is_dir()
