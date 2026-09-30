"""Typed parent state and local decision contracts for the read graph.

Only established plan facts flow to children. Router hints are not facts, and
the final coverage reviewer remains independent of the construction contract.
"""
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any


class Stage(str, Enum):
    ROUTE = "branch routing"
    SHAPE = "result shape"
    RELATION = "relationship"
    OUTPUT = "output"
    ORDER = "ranking"
    FILTER = "predicate selection"
    COMPARISON = "predicate comparison"
    VALUE = "predicate operand"
    LOGIC = "predicate logic"
    QUANTITY = "result count"
    COVERAGE = "independent coverage"


@dataclass(frozen=True)
class ReadState:
    source: str
    operation: str = "SELECT"
    assignments: tuple[tuple[str, Any], ...] = ()
    literal_facts: tuple[tuple[str, str, Any], ...] = ()
    output: str | None = None
    projection: tuple[str, ...] = ()
    aggregate: tuple[str, str] | None = None
    groups: tuple[str, ...] = ()
    relation: str | None = None
    ranking: tuple[tuple[str, str], ...] = ()
    per_group: tuple[str, str, str] | None = None
    predicates: tuple[tuple[str, str, Any], ...] = ()
    result_limit: int | None = None
    predicate_table: str | None = None
    predicate_column: str | None = None
    comparison: str | None = None

    def evidence(self, stage: Stage) -> dict:
        # Keep the parent snapshot small and relevant. No transcript, scores,
        # SQL, or unrelated schema is repeated in child calls.
        keys = {
            Stage.ROUTE: ("source",),
            Stage.SHAPE: ("source", "output"),
            Stage.RELATION: ("source", "output", "aggregate", "groups"),
            Stage.OUTPUT: ("source", "output", "relation", "groups"),
            Stage.ORDER: ("source", "output", "projection", "aggregate", "groups"),
            Stage.FILTER: ("source", "literal_facts", "projection", "aggregate", "groups", "ranking", "per_group", "predicates", "result_limit"),
            Stage.COMPARISON: ("source", "literal_facts", "projection", "relation", "ranking", "per_group", "predicates", "result_limit", "predicate_table", "predicate_column"),
            Stage.VALUE: ("source", "literal_facts", "projection", "relation", "ranking", "per_group", "predicates", "result_limit", "predicate_table", "predicate_column", "comparison"),
            Stage.LOGIC: ("source", "predicates"),
            Stage.QUANTITY: ("source", "ranking", "predicates"),
            Stage.COVERAGE: (),
        }[stage]
        values = asdict(self)
        result = {key: values[key] for key in keys if values[key] not in (None, (), [])}
        if self.operation != "SELECT":
            result["operation"] = self.operation
            if self.assignments:
                result["new_values_not_conditions"] = self.assignments
        return result


class DecisionClient:
    """A per-run context adapter; transport/accounting remain in JevClient."""
    def __init__(self, client, state: ReadState):
        self.client = client
        self.state = state
        self.stage = Stage.ROUTE

    def advance(self, stage: Stage, **changes) -> None:
        if "source" in changes and changes["source"] != self.state.source:
            raise ValueError("A child decision cannot change the established source")
        self.state = replace(self.state, **changes)
        self.stage = stage

    def call(self, local: dict, questions: dict):
        if self.stage == Stage.COVERAGE:
            # Coverage must be able to challenge every earlier decision.
            return self.client.call(local, questions)
        if (self.state.predicate_column and self.stage in (Stage.COMPARISON, Stage.VALUE)
                and local.get("condition_column", self.state.predicate_column) != self.state.predicate_column):
            raise ValueError("Predicate child contradicts its established column")
        if (self.stage == Stage.VALUE and self.state.comparison
                and local.get("comparison", self.state.comparison) != self.state.comparison):
            raise ValueError("Operand child contradicts its established comparison")
        contract = {
            "step": self.stage.value,
            "established": self.state.evidence(self.stage),
            "scope": "Answer only this step from grounded choices; preserve established facts.",
        }
        if self.stage in (Stage.FILTER, Stage.COMPARISON, Stage.VALUE):
            contract["deferred"] = "Ignore output, ranking, and unrelated predicates."
        elif self.stage == Stage.OUTPUT:
            contract["deferred"] = "Select output fields only."
        elif self.stage == Stage.ORDER:
            contract["deferred"] = "Select ordering only."
        if self.stage in (Stage.COMPARISON, Stage.VALUE) and self.state.predicate_table:
            local = {**local, "condition_table": self.state.predicate_table}
        return self.client.call({**local, "decision_context": contract}, questions)

    def __getattr__(self, name):
        return getattr(self.client, name)
