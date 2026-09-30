import unittest
from unittest.mock import Mock
import tempfile
from pathlib import Path

from intentsql.decision_context import DecisionClient, ReadState, Stage
from intentsql.database import connect
from intentsql.jev_client import CallResult
from intentsql.semantic_read import run_read


class DecisionContextTests(unittest.TestCase):
    def test_child_snapshot_includes_only_relevant_established_facts(self):
        transport = Mock()
        client = DecisionClient(transport, ReadState("measurements", projection=("label",),
            ranking=(("magnitude", "ASC"),), predicates=(("zone", "=", "east"),)))
        client.advance(Stage.VALUE, predicate_column="mass", comparison=">")
        client.call({"request": "request", "condition_column": "mass", "comparison": ">"}, {"value": {}})
        sent = transport.call.call_args.args[0]
        established = sent["decision_context"]["established"]
        self.assertEqual(established["source"], "measurements")
        self.assertEqual(established["ranking"], (("magnitude", "ASC"),))
        self.assertEqual(established["predicates"], (("zone", "=", "east"),))
        self.assertEqual(established["projection"], ("label",))
        client.advance(Stage.VALUE, predicate_column="other")
        self.assertEqual(established["predicate_column"], "mass")

    def test_contradictory_children_never_reach_provider(self):
        transport = Mock()
        client = DecisionClient(transport, ReadState("items"))
        with self.assertRaises(ValueError):
            client.advance(Stage.OUTPUT, source="different")
        client.advance(Stage.VALUE, predicate_column="x", comparison="=")
        for local in ({"condition_column": "y"}, {"comparison": "CONTAINS"}):
            with self.subTest(local=local), self.assertRaises(ValueError):
                client.call(local, {"value": {}})
        transport.call.assert_not_called()

    def test_coverage_can_challenge_established_state(self):
        transport = Mock()
        client = DecisionClient(transport, ReadState("items"))
        client.advance(Stage.COVERAGE)
        local = {"request": "original", "compiled_plan": {"outputs": ["x"]}}
        client.call(local, {"coverage": {}})
        self.assertEqual(transport.call.call_args.args[0], local)

    def test_ranking_is_established_before_predicate_children(self):
        transport = Mock()
        seen = []

        def answer(state, questions):
            seen.append((state, questions))
            if "output" in questions or "quantity" in questions:
                values = {key: {"noul": 0} for key in (
                    "filters", "ordering", "distinct", "grouping", "relationship",
                    "having", "expression", "windowing")}
                values.update(output={"choice": "fields"}, quantity={"choice": "none"},
                              filters={"noul": .95})
            elif "entity_rows" in questions:
                values = {"c0": {"noul": 0}, "c1": {"noul": 1}, "c2": {"noul": 0},
                          "c3": {"noul": 0}, "output_scope": {"choice": "selected_fields"}, "entity_rows": {"noul": 0},
                          "entity_label": {"choice": "none"}}
            elif "cardinality" in questions:
                values = {"cardinality": {"choice": "top_one", "confidence": .95}}
            elif "target" in questions:
                values = {"target": {"choice": "c3"}, "direction": {"choice": "ASC"},
                          "direction_explicit": {"noul": 1}, "secondary_target": {"choice": "none"},
                          "secondary_direction": {"choice": "ASC"}}
            elif "columns" in state and all(q["type"] == "noul" for q in questions.values()):
                self.assertEqual(state["decision_context"]["established"]["ranking"], (("magnitude", "ASC"),))
                values = {key: {"noul": .99 if name == "area" else .01}
                          for key, name in zip(questions, state["columns"])}
            elif "operator" in questions:
                self.assertEqual(state["decision_context"]["established"]["predicate_column"], "area")
                values = {"operator": {"choice": "=", "probabilities": {"=": 1}}}
            elif "coverage" in questions:
                self.assertNotIn("decision_context", state)
                values = {"coverage": {"choice": "complete"}}
            else:
                raise AssertionError(tuple(questions))
            return CallResult({"answers": values}, 20, 4, 1)

        transport.call.side_effect = answer
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE records (id INTEGER, label TEXT, area TEXT, magnitude REAL)")
                conn.executemany("INSERT INTO records VALUES (?,?,?,?)", [
                    (1, "outside", "West", 1), (2, "selected", "East", 5), (3, "other", "East", 9)])
            result = run_read(path, "Return the label of the record with the lowest magnitude in East", transport)
        self.assertEqual(result["rows"], [("selected",)])
        self.assertEqual(result["program"]["filters"], [{"column": "area", "operator": "=", "value": "East", "table": None}])
        self.assertFalse(any("value" in questions for _, questions in seen))

    def test_grouped_range_is_grounded_after_group_and_measure(self):
        """A missed router hint cannot erase exact range evidence."""
        transport = Mock()
        binding_contexts = []

        def choose_column(criteria, name):
            return next(key for key, value in criteria.items()
                        if isinstance(value, dict) and value.get("column") == name)

        def answer(state, questions):
            keys = set(questions)
            values = {}
            if keys == {"output"}:
                values = {"output": {"choice": "other", "confidence": .99}}
            elif "quantity" in keys and "grouping" in keys:
                values = {name: {"noul": 0} for name in (
                    "filters", "ordering", "distinct", "grouping", "relationship",
                    "having", "expression", "windowing")}
                values.update(output={"choice": "other"},
                              quantity={"choice": "none"})
                values["grouping"] = {"noul": .99}
                values["ordering"] = {"noul": .99}
            elif keys == {"shape", "stored_row_extremum"}:
                values = {"shape": {"choice": "grouped"},
                          "stored_row_extremum": {"noul": 0}}
            elif keys == {"group"}:
                values = {"group": {"choice": choose_column(
                    questions["group"]["criteria"], "year")}}
            elif keys == {"function", "target", "extremum"}:
                values = {"function": {"choice": "SUM"},
                          "extremum": {"choice": "neither"},
                          "target": {"choice": choose_column(
                              questions["target"]["criteria"], "HR")}}
            elif keys == {"having", "ordering", "ordering_basis", "threshold_fact"}:
                values = {"having": {"noul": 0}, "ordering": {"noul": 1},
                          "ordering_basis": {"choice": "measure"}, "threshold_fact": {"choice": "none"}}
            elif keys == {"filter"}:
                established = state["decision_context"]["established"]
                self.assertEqual(established["groups"], ("year",))
                self.assertEqual(established["aggregate"], ("SUM", "HR"))
                self.assertEqual(
                    [item[2] for item in established["literal_facts"]],
                    [1998, 2000])
                self.assertEqual(state["established_group_keys"], ("year",))
                self.assertEqual(state["established_computed_measure"],
                                 "SUM of HR per group")
                filter_contexts.append(state)
                values = {"filter": {"noul": 0}}
            elif keys == {"target", "direction", "tie"}:
                values = {"target": {"choice": "measure"},
                          "direction": {"choice": "DESC"},
                          "tie": {"choice": "none"}}
            elif "columns" in state and all(q["type"] == "noul" for q in questions.values()):
                binding_contexts.append(state)
                values = {key: {"noul": .99 if name == "year" else .01}
                          for key, name in zip(questions, state["columns"])}
            elif "relationship" in keys and all(key.startswith("v") or key == "relationship"
                                                 for key in keys):
                values = {"relationship": {"choice": "other"},
                          **{key: {"noul": 0} for key in keys if key.startswith("v")}}
            elif keys == {"operator"}:
                values = {"operator": {"choice": "BETWEEN",
                                         "probabilities": {"BETWEEN": 1}}}
            elif keys == {"cardinality"}:
                values = {"cardinality": {"choice": "all", "confidence": .99}}
            elif keys == {"coverage"}:
                values = {"coverage": {"choice": "complete"}}
            else:
                raise AssertionError((state, questions))
            return CallResult({"answers": values}, 10, 2, 1)

        transport.call.side_effect = answer
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performances.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE performances (year INTEGER, HR INTEGER)")
                conn.executemany("INSERT INTO performances VALUES (?, ?)", [
                    (1998, 10), (1998, 20), (1999, 55), (2000, 15), (2000, 25),
                    (2001, 500)])
            result = run_read(path,
                "For performances from 1998 through 2000, return year and total home runs, grouped by year, highest total first.",
                transport)
        self.assertEqual(result["sql"],
            'SELECT "year", SUM("HR") AS "sum_HR" FROM "performances" '
            'WHERE "year" BETWEEN ? AND ? GROUP BY "year" '
            'ORDER BY "sum_HR" DESC, "year" ASC')
        self.assertEqual(result["params"], [1998, 2000])
        self.assertEqual(result["rows"], [(1999, 55), (2000, 40), (1998, 30)])
        self.assertEqual(len(binding_contexts), 1)
        self.assertEqual(
            [item["value"] for item in binding_contexts[0]["exact_request_operands"]],
            [1998, 2000])
