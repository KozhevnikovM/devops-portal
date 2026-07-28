import re

_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_var_names(vars_: dict) -> None:
    """Raise ValueError if any key doesn't match [a-zA-Z_][a-zA-Z0-9_]*."""
    for key in vars_:
        if not _VAR_NAME_RE.match(key):
            raise ValueError(f"invalid var name '{key}': must match [a-zA-Z_][a-zA-Z0-9_]*")
