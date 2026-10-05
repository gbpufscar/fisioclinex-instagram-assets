"""Eligibility barriers based on confirmed feed time, never planned slots."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

STORY_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def initial_story_not_before(published_at: datetime) -> datetime:
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("confirmed feed timestamp must be timezone-aware")
    local = published_at.astimezone(STORY_TIMEZONE)
    evening = datetime.combine(local.date(), time(18), tzinfo=STORY_TIMEZONE)
    if local <= evening:
        return evening
    # Add elapsed hours in UTC, including historical Brazilian DST transitions.
    return (published_at.astimezone(timezone.utc) + timedelta(hours=6)).astimezone(STORY_TIMEZONE)
