"""Graph schema facts from apoc.meta.schema(); no language interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from neo4j import Driver


@dataclass(frozen=True)
class Property:
    name: str
    types: tuple[str, ...]
    mandatory: bool


@dataclass(frozen=True)
class RelEntry:
    """One directed, typed edge between two inspected labels."""
    rel_type: str
    start_label: str
    end_label: str
    count: int

    def involves(self, label: str) -> bool:
        return label in (self.start_label, self.end_label)

    def other_label(self, label: str) -> str:
        if label == self.start_label:
            return self.end_label
        if label == self.end_label:
            return self.start_label
        raise ValueError("label is not part of this inspected relationship")


class GraphSchema:
    def __init__(self, labels: dict[str, tuple[Property, ...]],
                 rels: tuple[RelEntry, ...]) -> None:
        self._labels = labels
        self._rels = rels

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._labels))

    def properties_for(self, label: str) -> tuple[Property, ...]:
        if label not in self._labels:
            raise ValueError("label is not in the inspected graph")
        return self._labels[label]

    def property_names(self, label: str) -> tuple[str, ...]:
        return tuple(prop.name for prop in self.properties_for(label))

    def neighbors(self, label: str) -> tuple[RelEntry, ...]:
        """Directed edges adjacent to one inspected label."""
        return tuple(rel for rel in self._rels if rel.involves(label))

    def __iter__(self) -> Iterator[RelEntry]:
        return iter(self._rels)


def load_schema(driver: Driver, database: str | None = None) -> GraphSchema:
    """Call apoc.meta.schema() and convert it into typed schema facts."""
    from neo4j import RoutingControl
    records, _, _ = driver.execute_query(
        "CALL apoc.meta.schema()",
        database_=database, routing_=RoutingControl.READ)
    if not records:
        raise RuntimeError("apoc.meta.schema() returned no schema")
    # apoc returns one column, typically named after the procedure's yield list
    raw = records[0].data()
    meta = next(iter(raw.values())) if len(raw) == 1 else raw.get("value")
    if not isinstance(meta, dict):
        raise RuntimeError("apoc.meta.schema() returned an unexpected shape")

    labels: dict[str, tuple[Property, ...]] = {}
    rels: list[RelEntry] = []
    for name, entry in meta.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "node":
            props = entry.get("properties") or {}
            labels[name] = tuple(
                Property(prop, tuple(sorted({str(t) for t in
                                             (info.get("types") or [info.get("type")])
                                             if info.get("types") or info.get("type")})),
                         bool(info.get("existence", False)))
                for prop, info in sorted(props.items())
                if isinstance(info, dict))
            for rel_type, rel_info in (entry.get("relationships") or {}).items():
                if not isinstance(rel_info, dict):
                    continue
                direction = rel_info.get("direction")
                others = rel_info.get("labels") or []
                for other in others:
                    # A rel entry proves a legal directed edge; it does not
                    # establish that the request needs the traversal.
                    if direction == "out":
                        rels.append(RelEntry(rel_type, name, other,
                                             int(rel_info.get("count") or 0)))
                    elif direction == "in":
                        rels.append(RelEntry(rel_type, other, name,
                                             int(rel_info.get("count") or 0)))
    # Deduplicate edges seen from both endpoints, keeping any observed count.
    unique: dict[tuple[str, str, str], RelEntry] = {}
    for rel in rels:
        key = (rel.rel_type, rel.start_label, rel.end_label)
        if key not in unique or rel.count > unique[key].count:
            unique[key] = rel
    return GraphSchema(labels, tuple(unique.values()))


__all__ = ["GraphSchema", "Property", "RelEntry", "load_schema"]
