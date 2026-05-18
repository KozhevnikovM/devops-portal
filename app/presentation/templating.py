from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/presentation/templates")


def _as_tz(dt: datetime, tz_name: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M (%Z)")


templates.env.filters["as_tz"] = _as_tz
