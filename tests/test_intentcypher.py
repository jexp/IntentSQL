"""Unit tests for IntentCypher: schema loading, compiler, and the skill graph
with a scripted JevClient — no live Neo4j, no real model."""

from __future__ import annotations

import pytest

from intentsql.jev_client import CallResult
from intentcypher.cypher_compiler import (Condition, CypherQuery, RelHop,
                                          compile_cypher)
from intentcypher.graph_schema import GraphSchema, Property, RelEntry
from intentcypher.skills import (explicitly_named_label, legal_operators,
                                 resolve_coverage, resolve_label,
                                 resolve_ordering, resolve_predicate)
from intentcypher import semantic_cypher


class ScriptedJev:
    """Routes pre-scripted answers by question name; records every payload."""

    def __init__(self, script: dict[str, list[str]] | list[str]) -> None:
        self.script = ({k: list(v) for k, v in script.items()}
                       if isinstance(script, dict) else {"__ordered__": list(script)})
        self.payloads: list[dict] = []
        self.calls = 0

    def call(self, state, questions):
        self.calls += 1
        self.payloads.append({"state": state, "questions": questions})
        question = next(iter(questions))
        if question in self.script:
            selected = self.script[question].pop(0)
        else:
            selected = self.script["__ordered__"].pop(0)
        answer = {"choice": selected, "probabilities": {selected: 1.0},
                  "confidence": 0.98}
        return CallResult({"answers": {question: answer}},
                          10, 5, 12)


def sample_schema() -> GraphSchema:
    return GraphSchema(
        labels={
            "Company": (Property("name", ("STRING",), True),
                       Property("founded", ("INTEGER",), False),
                       Property("hq", ("STRING",), False)),
            "Person": (Property("name", ("STRING",), True),
                       Property("age", ("INTEGER",), False)),
        },
        rels=(RelEntry("FOUNDED", "Person", "Company", 12),
              RelEntry("EMPLOYS", "Company", "Person", 340)),
    )


# --- schema --------------------------------------------------------------

def test_label_names_sorted_and_neighbors_directed():
    schema = sample_schema()
    assert schema.label_names == ("Company", "Person")
    neighbors = schema.neighbors("Company")
    assert len(neighbors) == 2
    founded = next(rel for rel in neighbors if rel.rel_type == "FOUNDED")
    assert founded.start_label == "Person" and founded.end_label == "Company"
    assert founded.other_label("Company") == "Person"
    with pytest.raises(ValueError):
        founded.other_label("Movie")


def test_property_names_and_unknown_label_rejects():
    schema = sample_schema()
    assert schema.property_names("Company") == ("name", "founded", "hq")
    with pytest.raises(ValueError):
        schema.property_names("Movie")


# --- compiler ------------------------------------------------------------

def test_compile_single_label_filter_order_limit():
    query = CypherQuery(
        label="Company",
        projection=("name",),
        conditions=(Condition("founded", ">", 1999),),
        order_by=(("founded", "ASC"),),
        limit=5,
    )
    compiled = compile_cypher(query)
    assert compiled.query == (
        "MATCH (n:`Company`)\n"
        "WHERE n.`founded` > $p0\n"
        "RETURN n.`name` AS `name`\n"
        "ORDER BY n.`founded` ASC\n"
        "LIMIT $limit")
    assert compiled.parameters == {"p0": 1999, "limit": 5}


def test_compile_one_hop_outgoing_and_null_check():
    query = CypherQuery(
        label="Company",
        rel_hop=RelHop("EMPLOYS", "out", "Person"),
        rel_projection=("name",),
        conditions=(Condition("hq", "is_null"),),
    )
    compiled = compile_cypher(query)
    assert compiled.query == (
        "MATCH (n:`Company`)-[:`EMPLOYS`]->(m:`Person`)\n"
        "WHERE n.`hq` IS NULL\n"
        "RETURN n, m.`name` AS `other_name`")
    assert compiled.parameters == {}


def test_compile_aggregate_with_grouping_and_order():
    query = CypherQuery(
        label="Company",
        aggregate=("count", "*"),
        group_by="hq",
        order_by=(("count_all", "DESC"),),
    )
    compiled = compile_cypher(query)
    assert "RETURN n.`hq` AS `hq`, count(n) AS count_all" in compiled.query
    assert "ORDER BY `count_all` DESC" in compiled.query


def test_compiler_rejects_unsupported_shapes():
    with pytest.raises(ValueError):
        Condition("name", "like", "x")
    with pytest.raises(ValueError):
        CypherQuery(label="Company", aggregate=("median", "founded"))
    with pytest.raises(ValueError):
        CypherQuery(label="Company", aggregate=("sum", "founded"),
                    rel_hop=RelHop("EMPLOYS", "out", "Person"))
    with pytest.raises(ValueError):
        CypherQuery(label="Company", group_by="hq")


def test_identifier_quoting_doubles_backticks():
    from intentcypher.cypher_compiler import quote_identifier
    assert quote_identifier("we`ird") == "`we``ird`"


# --- skills --------------------------------------------------------------

def test_explicitly_named_label_resolves_without_a_model_call():
    schema = sample_schema()
    result = resolve_label("List all companies founded after 1999", schema,
                           ScriptedJev([]))
    assert result.status == "resolved"
    assert result.value == "Company"
    assert result.jev_calls == 0


def test_resolve_label_uses_choice_and_validates_keys():
    schema = sample_schema()
    result = resolve_label("Who founded them?", schema, ScriptedJev({"label": ["l0"]}))
    assert result.value == "Company"
    bad = ScriptedJev({"label": ["l9"]})
    with pytest.raises(RuntimeError):
        resolve_label("Who?", schema, bad)


def test_legal_operators_by_observed_type():
    schema = sample_schema()
    company = schema.properties_for("Company")
    assert "contains" in legal_operators(company[2])      # name: STRING
    assert "contains" not in legal_operators(company[1])  # founded: INTEGER
    assert ">" in legal_operators(company[1])


def test_predicate_chain_with_single_matching_literal_is_mechanical():
    schema = sample_schema()
    from intentsql.skills.facts import extract_facts
    facts = extract_facts("companies founded after 1999")
    result = resolve_predicate("companies founded after 1999", schema,
                               "Company", facts, ScriptedJev({"predicate_column": ["c1"], "predicate_operator": [">"]}))
    assert result.status == "resolved"
    assert result.value == Condition("founded", ">", 1999)
    assert result.jev_calls == 2  # column + operator; operand was mechanical


def test_predicate_none_exits():
    schema = sample_schema()
    result = resolve_predicate("just list companies", schema, "Company",
                               (), ScriptedJev({"predicate_column": ["none"]}))
    assert result.status == "none"


def test_ordering_two_dependent_choices():
    schema = sample_schema()
    result = resolve_ordering("youngest companies first",
                               schema.properties_for("Company"),
                               ScriptedJev({"ordering": ["o1"], "ordering_direction": ["desc"]}))
    assert result.value == ("founded", "DESC")


def test_coverage_choice_from_closed_categories():
    result = resolve_coverage("five companies", {"source_label": "Company"},
                              ScriptedJev({"coverage": ["complete"]}))
    assert result.value == "complete"


# --- orchestration -------------------------------------------------------

class FakeDriver:
    """Stands in for the Neo4j driver: canned schema and canned rows."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict]] = []

    def close(self) -> None:
        pass

    def execute_query(self, query, parameters=None, **kwargs):
        self.executed.append((query, dict(parameters or {})))
        if query.strip() == "CALL apoc.meta.schema()":
            return _fake_schema_records(), None, None
        return [Row(row) for row in self.rows], None, None


class Row:
    def __init__(self, data: dict) -> None:
        self._data = data

    def data(self) -> dict:
        return self._data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


def _fake_schema_records() -> list[Row]:
    schema = {
        "Company": {
            "type": "node", "count": 20,
            "properties": {
                "name": {"type": "STRING", "existence": True},
                "founded": {"types": ["INTEGER"]},
                "hq": {"type": "STRING"},
            },
            "relationships": {
                "EMPLOYS": {"direction": "out", "labels": ["Person"], "count": 340},
            },
        },
        "Person": {
            "type": "node", "count": 90,
            "properties": {
                "name": {"type": "STRING", "existence": True},
                "age": {"types": ["INTEGER"]},
            },
            "relationships": {
                "EMPLOYS": {"direction": "in", "labels": ["Company"], "count": 340},
                "FOUNDED": {"direction": "out", "labels": ["Company"], "count": 12},
            },
        },
    }
    return [Row({"value": schema})]


def test_load_schema_from_apoc_shape(monkeypatch):
    driver = FakeDriver([])
    from intentcypher.graph_schema import load_schema
    graph = load_schema(driver)
    assert graph.label_names == ("Company", "Person")
    neighbors = graph.neighbors("Company")
    employs = next(rel for rel in neighbors if rel.rel_type == "EMPLOYS")
    assert (employs.start_label, employs.end_label) == ("Company", "Person")


def test_run_read_answers_with_parameterized_cypher(monkeypatch):
    driver = FakeDriver([{"name": "Neo4j"}, {"name": "Acme"}])
    monkeypatch.setattr(semantic_cypher, "connect_readonly", lambda config: driver)
    monkeypatch.setattr(semantic_cypher, "load_schema",
                        lambda drv, db=None: sample_schema())
    script = ScriptedJev({
        "relationship": ["none"], "second_traversal": ["none"],
        "fields": ["f0", "done"],
        "predicate_column": ["c1", "none"],  # founded > 1999, then loop ends
        "predicate_operator": [">"],
        "ordering": ["o1"], "ordering_direction": ["desc"],
        "coverage": ["complete"],
    })
    answer = semantic_cypher.run_read(
        "five companies founded after 1999, newest first",
        semantic_cypher.Neo4jConfig("bolt://x", "u", "p"), script)
    assert answer.status == "answered"
    assert answer.query == (
        "MATCH (n:`Company`)\n"
        "WHERE n.`founded` > $p0\n"
        "RETURN n.`name` AS `name`\n"
        "ORDER BY n.`founded` DESC\n"
        "LIMIT $limit")
    assert answer.parameters == {"p0": 1999, "limit": 5}
    assert answer.rows == [{"name": "Neo4j"}, {"name": "Acme"}]
    assert answer.jev_calls == script.calls


def test_run_read_refuses_on_coverage_gap(monkeypatch):
    driver = FakeDriver([])
    monkeypatch.setattr(semantic_cypher, "connect_readonly", lambda config: driver)
    monkeypatch.setattr(semantic_cypher, "load_schema",
                        lambda drv, db=None: sample_schema())
    script = ScriptedJev({
        "relationship": ["none"], "second_traversal": ["none"],
        "fields": ["f0", "done"],
        "predicate_column": ["none", "c2"],  # none first; repair retry on name
        "predicate_operator": ["contains"],
        "predicate_value": ["unspecified"],   # repair operand cannot be grounded
        "ordering": ["none"],
        "coverage": ["missing_filter"],
    })
    answer = semantic_cypher.run_read(
        "companies founded after 1999",
        semantic_cypher.Neo4jConfig("bolt://x", "u", "p"), script)
    assert answer.status == "refused"
    assert answer.reason
    # Coverage gates execution: no answer query was executed.
    assert not any("MATCH (n:" in q for q, _ in driver.executed)


def test_missing_filter_repair_recovers(monkeypatch):
    driver = FakeDriver([{"name": "Acme"}])
    monkeypatch.setattr(semantic_cypher, "connect_readonly", lambda config: driver)
    monkeypatch.setattr(semantic_cypher, "load_schema",
                        lambda drv, db=None: sample_schema())
    script = ScriptedJev({
        "relationship": ["none"], "second_traversal": ["none"],
        "fields": ["f0", "done"],
        "predicate_column": ["none", "c1"],  # missed first; repair retries
        "predicate_operator": [">"],          # operand 1999 binds mechanically
        "ordering": ["none"],
        "coverage": ["missing_filter", "complete"],
    })
    answer = semantic_cypher.run_read(
        "companies founded after 1999",
        semantic_cypher.Neo4jConfig("bolt://x", "u", "p"), script)
    assert answer.status == "answered"
    assert "WHERE n.`founded` > $p0" in answer.query
    assert answer.parameters == {"p0": 1999}
    assert answer.rows == [{"name": "Acme"}]


# --- web UI --------------------------------------------------------------

def test_web_ask_requires_a_body():
    from fastapi.testclient import TestClient
    from intentcypher.web import app
    client = TestClient(app)
    response = client.post("/api/ask", json={})
    assert response.status_code == 422  # missing question rejected by the model


def test_web_ask_without_connection_reports_error(monkeypatch, tmp_path):
    import importlib
    import intentcypher.connection as connection
    from fastapi.testclient import TestClient

    # Hermetic: no .env file and no NEO4J_* variables, regardless of the
    # developer machine's local .env.
    monkeypatch.setattr(connection, "load_env_file", lambda path=".env": {})
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    import intentcypher.web as web
    web = importlib.reload(web)
    client = TestClient(web.app)
    response = client.post("/api/ask", json={"question": "any question"})
    assert response.status_code == 400
    assert "Neo4j connection" in response.json()["reason"]


def test_web_index_serves_form():
    from fastapi.testclient import TestClient
    from intentcypher.web import app
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "IntentCypher" in response.text


def test_repair_replaces_vacuous_null_check(monkeypatch):
    driver = FakeDriver([{"name": "Elon Musk"}])
    monkeypatch.setattr(semantic_cypher, "connect_readonly", lambda config: driver)
    monkeypatch.setattr(semantic_cypher, "load_schema",
                        lambda drv, db=None: sample_schema())
    script = ScriptedJev({
        "relationship": ["none"], "second_traversal": ["none"],
        "fields": ["f0", "done"],
        # round 1 binds a vacuous null-check; round 2 ends the loop
        "predicate_column": ["c0", "none", "c0"],
        "predicate_operator": ["is_not_null", "="],  # repair retries with =
        "ordering": ["none"],
        "coverage": ["missing_filter", "complete"],
    })
    answer = semantic_cypher.run_read(
        "companies named Neo4j",
        semantic_cypher.Neo4jConfig("bolt://x", "u", "p"), script)
    assert answer.status == "answered"
    assert "WHERE n.`name` = $p0" in answer.query
    assert "IS NOT NULL" not in answer.query
    assert answer.parameters == {"p0": "Neo4j"}


def test_fields_selection_loop_picks_multiple_then_done():
    schema = sample_schema()
    from intentcypher.skills import resolve_fields
    result = resolve_fields("return the name and founding year of companies",
                             schema.properties_for("Company"),
                             ScriptedJev(["f0", "f1", "done"]))
    assert result.status == "resolved"
    assert result.value == ("name", "founded")  # f1 stays 'founded' in round 2
    assert result.jev_calls == 3


def test_fields_empty_selection_means_whole_node():
    schema = sample_schema()
    from intentcypher.skills import resolve_fields
    result = resolve_fields("just list companies",
                             schema.properties_for("Company"),
                             ScriptedJev(["done"]))
    assert result.value == ()
