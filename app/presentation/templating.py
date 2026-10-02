from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from fastapi.templating import Jinja2Templates

from app.application.use_cases._permissions import can_view_credentials
from app.presentation.reconcile import environment_row_version, list_key, live_class, row_version

templates = Jinja2Templates(directory="app/presentation/templates")


def _as_tz(dt: datetime, tz_name: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    try:
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M (%Z)")
    except OverflowError:
        return "—"


templates.env.filters["as_tz"] = _as_tz


def _toyaml(value: dict | None) -> str:
    if not value:
        return ""
    return yaml.dump(value, default_flow_style=False, allow_unicode=True).rstrip()


templates.env.filters["toyaml"] = _toyaml

# One owner-or-admin credentials rule (#478), shared by booking_row.html and the credentials route.
templates.env.globals["can_view_credentials"] = can_view_credentials

# Page row reconciliation (#497): every row render path emits the same version and list key.
templates.env.globals["row_version"] = row_version
templates.env.globals["environment_row_version"] = environment_row_version
templates.env.globals["list_key"] = list_key
templates.env.globals["live_class"] = live_class
