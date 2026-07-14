#!/usr/bin/env python3
"""Import routers and switches into the mt-backup dashboard from scan result files."""
import argparse
import os
import re
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, "/app")

from app import database, ssh_backup  # noqa: E402


ROUTER_RE = re.compile(r"^.*IP:\s+(\S+)\s+-\s+Identity:\s+(.+?)\s*$")
SWITCH_RE = re.compile(
    r"^.*IP:\s+(\S+)\s+-\s+Identity:\s+(.+?)\s+\(Router:\s+(.+?)\)\s*$"
)


def _read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def _location_from_identity(identity: str) -> str:
    location = (identity or "").strip()
    location = re.sub(r"^(Router|RO)\.BTS\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^Router\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^RO\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^SW\d*\.", "", location, flags=re.IGNORECASE)
    location = re.sub(r"\.(CRS\d+|RB\d+|4011|10G|BLKNG)$", "", location, flags=re.IGNORECASE)
    location = re.sub(r"[-_.]+", " ", location).strip()
    return location.upper() or (identity or "").strip()


def _import_router(line: str, password_encrypted: str, port: int, username: str) -> int | None:
    match = ROUTER_RE.match(line.strip())
    if not match:
        return None
    ip, identity = match.groups()
    identity = identity.strip()
    return database.upsert_router_by_ip(
        name=identity or ip,
        ip=ip,
        port=port,
        username=username,
        password_encrypted=password_encrypted,
        device_type="router",
        location=_location_from_identity(identity),
        enabled=1,
        identity=identity,
    )


def _import_switch(line: str, password_encrypted: str, port: int, username: str) -> int | None:
    match = SWITCH_RE.match(line.strip())
    if not match:
        return None
    ip, identity, parent_router = match.groups()
    identity = identity.strip()
    parent_router = parent_router.strip()
    return database.upsert_router_by_ip(
        name=identity or ip,
        ip=ip,
        port=port,
        username=username,
        password_encrypted=password_encrypted,
        device_type="switch",
        location=_location_from_identity(parent_router or identity),
        enabled=1,
        identity=identity,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routers", required=True, help="Path to router scan result file")
    parser.add_argument("--switches", required=True, help="Path to switch scan result file")
    parser.add_argument("--router-user", default=os.environ.get("MT_IMPORT_ROUTER_USER", ""))
    parser.add_argument("--router-pass", default=os.environ.get("MT_IMPORT_ROUTER_PASS", ""))
    parser.add_argument("--switch-user", default=os.environ.get("MT_IMPORT_SWITCH_USER", ""))
    parser.add_argument("--switch-pass", default=os.environ.get("MT_IMPORT_SWITCH_PASS", ""))
    parser.add_argument("--port", type=int, default=2282)
    args = parser.parse_args()
    if not args.router_user or not args.router_pass or not args.switch_user or not args.switch_pass:
        parser.error(
            "--router-user/--router-pass/--switch-user/--switch-pass or "
            "MT_IMPORT_ROUTER_USER/MT_IMPORT_ROUTER_PASS/"
            "MT_IMPORT_SWITCH_USER/MT_IMPORT_SWITCH_PASS is required"
        )

    database.init_db()
    router_password = ssh_backup.encrypt_password(args.router_pass)
    switch_password = ssh_backup.encrypt_password(args.switch_pass)

    router_ids = set()
    switch_ids = set()
    for line in _read_lines(args.routers):
        router_id = _import_router(line, router_password, args.port, args.router_user)
        if router_id is not None:
            router_ids.add(router_id)

    for line in _read_lines(args.switches):
        switch_id = _import_switch(line, switch_password, args.port, args.switch_user)
        if switch_id is not None:
            switch_ids.add(switch_id)

    print(f"imported_or_updated routers={len(router_ids)} switches={len(switch_ids)} total={len(router_ids) + len(switch_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
