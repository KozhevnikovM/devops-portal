"""Form-field types shared by the browser routes."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Form
from pydantic import BeforeValidator


def _blank_to_none(value: Any) -> Any:
    return None if value == "" else value


# An optional UUID form field where blank means "not provided" (#525): a <select>'s
# "no specific choice" option submits `field=""`, which is treated like omitting the field.
# A non-empty value must still be a valid UUID (422 otherwise).
#
# Use it as `field: OptionalFormUUID = None`, never `= Form(None)` — a `Form()` default
# replaces the one carried here and silently drops the blank-to-None validator.
OptionalFormUUID = Annotated[UUID | None, Form(), BeforeValidator(_blank_to_none)]
