import re

from app.domain.exceptions import EnvironmentTooLargeError

_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_var_names(vars_: dict) -> None:
    """Raise ValueError if any key doesn't match [a-zA-Z_][a-zA-Z0-9_]*."""
    for key in vars_:
        if not _VAR_NAME_RE.match(key):
            raise ValueError(f"invalid var name '{key}': must match [a-zA-Z_][a-zA-Z0-9_]*")


def validate_environment_size(item_count: int, limit: int) -> None:
    """Raise EnvironmentTooLargeError if a blueprint's `item_count` exceeds `limit` (#497).

    An environment gets one child per blueprint item, so this bounds every environment's children —
    the invariant page reconciliation's bounded per-environment child read relies on.
    """
    if item_count > limit:
        raise EnvironmentTooLargeError(
            f"a blueprint may have at most {limit} items (ENVIRONMENT_MAX_CHILDREN); this one has {item_count}"
        )
