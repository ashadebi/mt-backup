#!/usr/bin/env python3
"""Standalone cron script — run by /etc/cron.d/mt-backup at 08:00 and 18:00 daily.

Same logic as api/backup/run but as a CLI tool. No auth needed (cron has filesystem access).
"""
import os
import sys
from datetime import datetime
from pathlib import Path

# Make /app importable when running inside container
sys.path.insert(0, "/app")

from app import database, ssh_backup  # noqa: E402


def main():
    started = datetime.now()
    print(f"[{started.isoformat()}] mt-backup cron started")

    # 1. Cleanup old backups first
    deleted = ssh_backup.cleanup_old_backups()
    print(f"  cleanup: removed {deleted} old .rsc files")

    # 2. Get all enabled routers
    routers = database.list_routers()
    if not routers:
        print("  no routers configured, exiting")
        return 0

    print(f"  found {len(routers)} router(s) to back up")

    success = 0
    failed = 0
    for r in routers:
        if not r.get("enabled"):
            print(f"  - {r['ip']} ({r['name']}): SKIPPED (disabled)")
            continue
        print(f"  - {r['ip']} ({r['name']}): ", end="", flush=True)
        result = ssh_backup.run_backup(dict(r), now=started)
        status = "success" if result["success"] else "failed"
        database.log_backup(r["id"], result["filename"], status, result["size"], result.get("error"))
        database.set_last_backup(r["id"], status)
        if result.get("identity"):
            database.set_router_identity(r["id"], result["identity"])
        if result["success"]:
            id_str = f" identity={result['identity']}" if result.get("identity") else ""
            print(f"OK ({result['filename']}, {result['size']} bytes){id_str}")
            success += 1
        else:
            print(f"FAILED — {result.get('error', '')[:200]}")
            failed += 1

    print(f"[{datetime.now().isoformat()}] done: {success} success, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
