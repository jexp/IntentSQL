"""Joined predicates remain qualified to real inspected columns."""

import sqlite3
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.join_fields import QualifiedField
from intentsql.skills.join_predicate import (joined_predicate_candidates,
                                              resolve_join_predicate_column,
                                              resolve_join_predicate_columns)
from intentsql.skills.schema import Column


class JoinPredicateTests(unittest.TestCase):
    def test_one_choice_returns_qualified_field(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"column": {
            "choice": "c1", "probabilities": {"c1": .9}}}}, 200, 30, 300)
        result = resolve_join_predicate_column("rows above 10", {
            "left": (Column("id", "INT", False, True),),
            "right": (Column("score", "REAL", True, False),)}, client)
        self.assertEqual(result.field, QualifiedField("right", "score"))

    def test_multiple_qualified_predicates_share_one_batched_call(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "c0": {"noul": .05}, "c1": {"noul": .94},
            "c2": {"noul": .92},
        }}, 200, 30, 300)
        result = resolve_join_predicate_columns("bounded joined rows", {
            "left": (Column("id", "INT", False, True),
                     Column("year", "INT", True, False)),
            "right": (Column("category", "TEXT", True, False),)}, client,
            candidate_evidence={
                "left.id": {"type": "INT"},
                "left.year": {"request_numbers_in_range": (2020,)},
                "right.category": {"related_stored_values": ({"value": "open"},)},
            })
        self.assertEqual(result.fields, (
            QualifiedField("left", "year"), QualifiedField("right", "category")))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 1)

    def test_uncertain_join_role_gets_bounded_followup_with_established_state(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"c0": {"noul": .55},
                                    "c1": {"noul": .85}}}, 100, 20, 100),
            CallResult({"answers": {"c0": {"noul": .9}}}, 40, 10, 100),
        ]
        result = resolve_join_predicate_columns("rows in a period for a region", {
            "facts": (Column("period", "INT", True, False),),
            "places": (Column("region", "TEXT", True, False),)}, client)
        self.assertEqual(result.fields, (
            QualifiedField("facts", "period"), QualifiedField("places", "region")))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 2)
        follow_state = client.call.call_args.args[0]
        self.assertEqual(follow_state["established_where_columns"],
                         ("places.region",))

    def test_shared_observed_value_is_bound_to_one_qualified_column(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"c0": {"noul": .52},
                                    "c1": {"noul": .76}}}, 100, 20, 100),
            CallResult({"answers": {"column": {"choice": "c1"}}}, 40, 10, 100),
        ]
        evidence = {"children.city": {"related_stored_values": (
                        {"value": "Cambridge"},)},
                    "parents.name": {"related_stored_values": (
                        {"value": "Cambridge"},)}}
        result = resolve_join_predicate_columns(
            "children of the parent called Cambridge", {
                "children": (Column("city", "TEXT", True, False),),
                "parents": (Column("name", "TEXT", True, False),)}, client,
            candidate_evidence=evidence)
        self.assertEqual(result.fields, (QualifiedField("parents", "name"),))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 2)

    def test_unfamiliar_join_candidates_use_ranges_and_observed_values(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE observations (id INTEGER PRIMARY KEY, place_id INTEGER,
                                       period INTEGER, amount INTEGER);
            CREATE TABLE places (id INTEGER PRIMARY KEY, region TEXT,
                                 founded INTEGER);
            INSERT INTO observations VALUES (1, 1, 2021, 9000), (2, 2, 2022, 8000);
            INSERT INTO places VALUES (1, 'USA', 1900), (2, 'CAN', 1950);
        """)
        candidates = joined_predicate_candidates(
            conn, "Show observations from 2020 through 2023 outside the USA", {
                "observations": (Column("id", "INTEGER", False, True),
                                 Column("place_id", "INTEGER", True, False),
                                 Column("period", "INTEGER", True, False),
                                 Column("amount", "INTEGER", True, False)),
                "places": (Column("id", "INTEGER", False, True),
                           Column("region", "TEXT", True, False),
                           Column("founded", "INTEGER", True, False)),
            }, (2020, 2023))
        self.assertIn("observations.period", candidates)
        self.assertIn("places.region", candidates)
        self.assertNotIn("observations.id", candidates)
        self.assertNotIn("places.id", candidates)
        self.assertNotIn("places.founded", candidates)
        self.assertEqual(candidates["observations.period"]["observed_range"], (2021, 2022))

    def test_numeric_range_alone_does_not_hide_or_invent_a_text_filter(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE observations (id INTEGER PRIMARY KEY, amount INTEGER);
            CREATE TABLE places (id INTEGER PRIMARY KEY, region TEXT);
            INSERT INTO observations VALUES (1, 10), (2, 40);
            INSERT INTO places VALUES (1, 'USA'), (2, 'CAN');
        """)
        grounded = joined_predicate_candidates(
            conn, "outside the USA", {
                "observations": (Column("id", "INTEGER", False, True),
                                 Column("amount", "INTEGER", True, False)),
                "places": (Column("id", "INTEGER", False, True),
                           Column("region", "TEXT", True, False)),
            }, ())
        self.assertIn("places.region", grounded)
        self.assertNotIn("observations.amount", grounded)
        fallback = joined_predicate_candidates(
            conn, "a perfect recorded amount", {
                "observations": (Column("id", "INTEGER", False, True),
                                 Column("amount", "INTEGER", True, False)),
                "places": (Column("id", "INTEGER", False, True),
                           Column("region", "TEXT", True, False)),
            }, ())
        self.assertEqual(fallback["observations.amount"]["observed_range"], (10, 40))

    def test_ungrounded_multi_select_narrows_to_one_column(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"c0": {"noul": .71}, "c1": {"noul": .68},
                                    "c2": {"noul": .53}}}, 80, 10, 1),
            CallResult({"answers": {"column": {
                "choice": "c0", "confidence": .9,
                "probabilities": {"c0": .8, "c1": .15, "none": .05}}}}, 40, 8, 1),
        ]
        result = resolve_join_predicate_columns(
            "Which schools had a perfect graduation rate?", {
                "rates": (Column("graduated", "NUMERIC", True, False),
                          Column("dropped", "NUMERIC", True, False)),
                "schools": (Column("name", "TEXT", True, False),)},
            client,
            candidate_evidence={
                "rates.graduated": {"type": "NUMERIC", "observed_range": (4, 100)},
                "rates.dropped": {"type": "NUMERIC", "observed_range": (0, 75)},
                "schools.name": {"type": "TEXT"},
            },
            established_outputs=("schools.name",))
        self.assertEqual(result.fields, (QualifiedField("rates", "graduated"),))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 2)
        criteria = client.call.call_args_list[1].args[1]["column"]["criteria"]
        described = [item.get("column") for item in criteria.values()
                     if isinstance(item, dict)]
        self.assertEqual(described, ["rates.graduated", "rates.dropped"])
        self.assertIn("none", criteria)
        task = client.call.call_args_list[1].args[0]["task"]
        self.assertNotIn("without a request literal", task)
        self.assertIn("single column", task)

    def test_ungrounded_choice_fails_closed_when_confidence_is_low(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"c0": {"noul": .7}, "c1": {"noul": .66}}}, 40, 8, 1),
            CallResult({"answers": {"column": {"choice": "c0", "confidence": .4}}}, 20, 4, 1),
        ]
        result = resolve_join_predicate_columns(
            "records with a perfect score", {
                "facts": (Column("score", "NUMERIC", True, False),
                          Column("penalty", "NUMERIC", True, False))},
            client, candidate_evidence={
                "facts.score": {"observed_range": (0, 100)},
                "facts.penalty": {"observed_range": (0, 20)},
            })
        self.assertEqual(result.fields, ())
        self.assertEqual(result.status, "ambiguous")

    def test_independent_text_operands_survive_candidate_pruning(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE patrons (region TEXT, tier TEXT, surname TEXT);
            INSERT INTO patrons VALUES ('P.R.', 'Active', 'Rico');
        """)
        candidates = joined_predicate_candidates(
            conn, "Show patrons from Puero Rico that are Active", {
                "patrons": (Column("region", "TEXT", True, False),
                            Column("tier", "TEXT", True, False),
                            Column("surname", "TEXT", True, False))})
        self.assertIn("patrons.region", candidates)
        self.assertIn("patrons.tier", candidates)
        self.assertNotIn("patrons.surname", candidates)


if __name__ == "__main__":
    unittest.main()
