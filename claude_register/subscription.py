"""Date normalization for timestamps explicitly reported by a subscription API."""
from datetime import date, datetime, timezone


def expiry_date(value) -> str:
    if not isinstance(value, str):
        return ""
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    return ""
