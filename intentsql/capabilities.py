"""Closed deterministic predicate capabilities; no provider-supplied code.

The semantic choice set and SQL validation use this same registry. Retrieval
may supply operands, but only a semantic decision can select an operation.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PredicateCapability:
    name: str
    description: str
    arity: int | None  # None denotes a nonempty set
    types: tuple[str, ...] = ('numeric', 'text', 'date')


PREDICATES = {item.name: item for item in (
    PredicateCapability('=', 'equal to the complete value', 1),
    PredicateCapability('!=', 'not equal to the complete value', 1),
    PredicateCapability('>', 'strictly greater', 1),
    PredicateCapability('>=', 'greater or equal', 1),
    PredicateCapability('<', 'strictly less', 1),
    PredicateCapability('<=', 'less or equal', 1),
    PredicateCapability('BETWEEN', 'inside an inclusive interval', 2),
    PredicateCapability('IN', 'one of the listed values', None),
    PredicateCapability('IS NULL', 'missing value', 0),
    PredicateCapability('IS NOT NULL', 'present value', 0),
    PredicateCapability('CONTAINS', 'contains a substring', 1, ('text',)),
    PredicateCapability('PREFIX', 'begins with a substring', 1, ('text',)),
    PredicateCapability('SUFFIX', 'ends with a substring', 1, ('text',)),
)}


def comparison_choices(semantic_type: str) -> dict[str, str]:
    # Set operands are bound by the alternatives skill before scalar comparison.
    return {name: item.description for name, item in PREDICATES.items()
            if semantic_type in item.types and name != 'IN'}


def validate_operand(operator: str, value) -> None:
    if operator not in PREDICATES:
        raise ValueError('Unsupported comparison operator')
    arity = PREDICATES[operator].arity
    scalar = lambda item: isinstance(item, (str, int, float)) and not isinstance(item, bool)
    if arity == 0:
        valid = value is None
    elif arity == 1:
        valid = scalar(value)
    else:
        valid = (isinstance(value, tuple) and bool(value) and
                 (arity is None or len(value) == arity) and all(scalar(item) for item in value))
    if not valid:
        raise ValueError('Comparison has unresolved or invalid operands')
