import os
from datetime import datetime
from zoneinfo import ZoneInfo


TIMEZONE = os.getenv("TZ", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(TIMEZONE)


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_iso() -> str:
    return local_now().isoformat()


def local_fromtimestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=LOCAL_TZ)
