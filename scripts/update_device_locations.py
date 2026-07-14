#!/usr/bin/env python3
"""Refresh device locations from SNMP location, with identity-based fallback."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, "/app")

from app import database, ssh_backup  # noqa: E402


def fallback_location(identity: str) -> str:
    location = (identity or "").strip()
    location = re.sub(r"^(Router|RO)\.BTS\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^Router\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^RO\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^SW\d*\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"\.(CRS\d+|RB\d+|4011|10G|BLKNG)$", "", location, flags=re.IGNORECASE)
    location = re.sub(r"[-_.]+", " ", location).strip()
    return location.upper() or (identity or "").strip()


def update_one(router: dict) -> tuple[str, str, str]:
    try:
        password = ssh_backup.decrypt_password(router["password_encrypted"])
        info = ssh_backup.detect_device_info(router["ip"], router["port"], router["username"], password, timeout=6)
        if not info.get("success"):
            return ("fail", router["ip"], info.get("error", "unknown"))

        identity = info.get("identity") or router.get("identity") or router.get("name")
        location = info.get("location") or fallback_location(identity)
        database.update_router(
            router["id"],
            name=identity or router["name"],
            ip=router["ip"],
            port=router["port"],
            username=router["username"],
            password_encrypted=router["password_encrypted"],
            device_type=router["device_type"],
            location=location,
            enabled=router["enabled"],
        )
        if identity:
            database.set_router_identity(router["id"], identity)
        return ("ok", router["ip"], location)
    except Exception as exc:
        return ("fail", router["ip"], str(exc))


def main() -> int:
    routers = database.list_routers()
    ok = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(update_one, router) for router in routers]
        for future in as_completed(futures):
            status, _ip, _detail = future.result()
            if status == "ok":
                ok += 1
            else:
                failed += 1
    print({"updated_from_snmp_or_identity": ok, "failed_preserved_fallback": failed, "total": len(routers)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
