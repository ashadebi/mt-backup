"""SSH backup engine: connect to MikroTik via paramiko, run /export, save .rsc

Folder layout: BACKUP_DIR/{ip} - {sanitized_name}/{YYYY-MM-DD.daily.rsc}
"""
import os
import re
import time
from datetime import datetime
from pathlib import Path
from cryptography.fernet import Fernet
import paramiko

BACKUP_DIR = Path(os.environ.get("MT_BACKUP_DIR", "/app/backups"))
KNOWN_HOSTS = Path(os.environ.get("MT_DATA_DIR", "/app/data")) / "ssh_known_hosts"


def _fernet() -> Fernet:
    key = os.environ.get("MT_FERNET_KEY", "").encode()
    if not key:
        raise RuntimeError("MT_FERNET_KEY not set in environment")
    return Fernet(key)


def decrypt_password(encrypted_b64: str) -> str:
    return _fernet().decrypt(encrypted_b64.encode()).decode()


def encrypt_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def sanitize_name(name: str) -> str:
    """Sanitize router name for filesystem use. Keep alphanumeric, dot, dash, underscore.
    Replace anything else with single underscore. Strip leading/trailing dots/dashes/underscores."""
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or '').strip())
    s = s.strip('._-')
    return s or 'router'


def router_dir(router: dict) -> Path:
    """Per-router backup directory: {BACKUP_DIR}/{ip} - {sanitized_name}"""
    ip = router["ip"]
    name = sanitize_name(router.get("name", "router"))
    return BACKUP_DIR / f"{ip} - {name}"


def parse_dir_name(dir_name: str) -> dict:
    """Parse '{ip} - {name}' or legacy '{ip}' into dict with ip and name."""
    parts = dir_name.split(" - ", 1)
    return {"ip": parts[0], "name": parts[1] if len(parts) > 1 else ""}


def filename_for(now: datetime, router_name: str = "") -> str:
    """Return basename (no path) for backup at given time.

    Naming matches Bos's bash script convention:
    - {Identity}.YYYY-MM-DD.monthly.rsc       (day 1 of month)
    - {Identity}.YYYY-MM-DD.weekly.rsc        (Sunday)
    - {Identity}.YYYY-MM-DD.1800.daily.rsc    (afternoon/evening)
    - {Identity}.YYYY-MM-DD.daily.rsc         (morning)

    router_name is sanitized for filesystem use; empty means no prefix.
    """
    dow = now.weekday()
    dom = now.day
    hour = now.hour
    date = now.strftime("%Y-%m-%d")
    if dom == 1:
        suffix = "monthly"
    elif dow == 6:
        suffix = "weekly"
    elif hour >= 12:
        suffix = "1800.daily"
    else:
        suffix = "daily"
    prefix = sanitize_name(router_name) if router_name else ""
    if prefix and prefix != "router":
        return f"{prefix}.{date}.{suffix}.rsc"
    return f"{date}.{suffix}.rsc"


def test_connection(ip: str, port: int, username: str, password: str, timeout: int = 10) -> dict:
    """SSH connectivity test. Returns success, version, identity, output."""
    client = paramiko.SSHClient()
    if KNOWN_HOSTS.exists():
        client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            ip, port=port, username=username, password=password,
            timeout=timeout, look_for_keys=False, allow_agent=False,
            banner_timeout=timeout, auth_timeout=timeout,
        )
        # /system resource print → version
        stdin, stdout, stderr = client.exec_command("/system resource print", timeout=timeout)
        out_res = stdout.read().decode("utf-8", errors="replace")
        # /system identity print → name
        stdin, stdout, stderr = client.exec_command("/system identity print", timeout=timeout)
        out_id = stdout.read().decode("utf-8", errors="replace")
        client.close()

        version = ""
        for line in out_res.splitlines():
            if line.strip().startswith("version:"):
                version = line.split(":", 1)[1].strip()
                break
        identity = ""
        for line in out_id.splitlines():
            if line.strip().startswith("name:"):
                identity = line.split(":", 1)[1].strip()
                break
        return {
            "success": True, "version": version, "identity": identity,
            "output": out_res[:500],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "version": "", "identity": ""}


def add_known_host(ip: str, port: int) -> bool:
    """Add host key to known_hosts (one-time per host)."""
    try:
        transport = paramiko.Transport((ip, port))
        transport.connect()
        key = transport.get_remote_server_key()
        transport.close()
        KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{ip}]:{port} {key.get_name()} {key.get_base64()}\n"
        with KNOWN_HOSTS.open("a") as f:
            f.write(line)
        return True
    except Exception:
        return False


def _extract_identity(output: str) -> str:
    """Parse /system identity print output for `name: <identity>`."""
    for line in output.splitlines():
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_field(output: str, key: str) -> str:
    """Extract field value from MikroTik print output: 'key: value' -> 'value'."""
    for line in output.splitlines():
        s = line.strip()
        if s.startswith(key):
            return s.split(":", 1)[1].strip()
    return ""


def detect_device_info(ip: str, port: int, username: str, password: str, timeout: int = 10) -> dict:
    """SSH connect, fetch identity, snmp location, routerboard model, version.

    Returns dict:
        success: bool
        error: str (if fail)
        identity: str    # /system identity print -> name
        location: str    # /system snmp print -> location
        contact: str     # /system snmp print -> contact
        model: str       # /system routerboard print -> model (fallback: resource platform)
        version: str     # /system resource print -> version
        device_type: str # 'router' or 'switch' (auto-detected from model: CRS* = switch)
    """
    result = {
        "success": False, "error": "", "identity": "", "location": "",
        "contact": "", "model": "", "version": "", "device_type": "router",
    }
    client = paramiko.SSHClient()
    if KNOWN_HOSTS.exists():
        client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            ip, port=port, username=username, password=password,
            timeout=timeout, look_for_keys=False, allow_agent=False,
            banner_timeout=timeout, auth_timeout=timeout,
        )
    except Exception as e:
        result["error"] = f"SSH connect failed: {e}"
        return result

    def _run(cmd: str) -> str:
        try:
            _, stdout, _ = client.exec_command(cmd, timeout=timeout)
            return stdout.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    try:
        result["identity"] = _extract_field(_run("/system identity print"), "name:")
        snmp_out = _run("/system snmp print")
        result["location"] = _extract_field(snmp_out, "location:")
        result["contact"] = _extract_field(snmp_out, "contact:")
        res_out = _run("/system resource print")
        result["version"] = _extract_field(res_out, "version:")
        rb_out = _run("/system routerboard print")
        model_rb = _extract_field(rb_out, "model:")
        model_plat = _extract_field(res_out, "platform:")
        result["model"] = model_rb or model_plat
        # CRS* = switch
        if (result["model"] or "").upper().startswith("CRS"):
            result["device_type"] = "switch"
        result["success"] = True
    except Exception as e:
        result["error"] = f"Command failed: {e}"
    finally:
        try:
            client.close()
        except Exception:
            pass
    return result



def run_backup(router: dict, now: datetime | None = None) -> dict:
    """Run /export on a router, save to router_dir(router)/{filename}.

    Returns: success, filename, size, error, identity (auto-detected from /system identity print)
    """
    now = now or datetime.now()
    host = router["ip"]
    port = int(router["port"])
    username = router["username"]
    try:
        password = decrypt_password(router["password_encrypted"])
    except Exception as e:
        return {"success": False, "filename": "", "size": 0, "error": f"decrypt failed: {e}", "identity": ""}

    filename = filename_for(now)
    out_dir = router_dir(router)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    client = paramiko.SSHClient()
    if KNOWN_HOSTS.exists():
        client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            host, port=port, username=username, password=password,
            timeout=15, look_for_keys=False, allow_agent=False,
            banner_timeout=15, auth_timeout=15,
        )
        # /system identity print — get identity (best-effort, may fail on some setups)
        identity = ""
        try:
            stdin, stdout, stderr = client.exec_command("/system identity print", timeout=10)
            out_id = stdout.read().decode("utf-8", errors="replace")
            identity = _extract_identity(out_id)
        except Exception:
            pass

        # Re-compute filename with identity prefix now that we have it
        filename = filename_for(now, identity)

        # /export — main backup
        stdin, stdout, stderr = client.exec_command("/export", timeout=60)
        content = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        client.close()
        if err.strip() and not content.strip():
            return {"success": False, "filename": filename, "size": 0, "error": err.strip()[:500], "identity": identity}
        out_path = out_dir / filename
        out_path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "filename": filename,
            "size": len(content.encode("utf-8")),
            "error": None,
            "identity": identity,
            "path": str(out_path),
        }
    except Exception as e:
        return {"success": False, "filename": filename, "size": 0, "error": str(e)[:500], "identity": ""}


def cleanup_old_backups(retention: dict | None = None) -> int:
    """Delete old .rsc files. retention: {daily, weekly, monthly} days."""
    retention = retention or {"daily": 7, "weekly": 30, "monthly": 365}
    now = time.time()
    deleted = 0
    if not BACKUP_DIR.exists():
        return 0
    for router_dir in BACKUP_DIR.iterdir():
        if not router_dir.is_dir():
            continue
        for f in router_dir.glob("*.rsc"):
            try:
                age_days = (now - f.stat().st_mtime) / 86400
                name = f.name
                if ".monthly." in name and age_days > retention["monthly"]:
                    f.unlink(); deleted += 1
                elif ".weekly." in name and age_days > retention["weekly"]:
                    f.unlink(); deleted += 1
                elif ".daily." in name and age_days > retention["daily"]:
                    f.unlink(); deleted += 1
            except Exception:
                pass
    return deleted


def _file_info(d: Path, f: Path, ip: str, name: str) -> dict:
    stat = f.stat()
    return {
        "ip": ip,
        "name": name,
        "dir_name": d.name,
        "filename": f.name,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "type": "monthly" if ".monthly." in f.name else (
            "weekly" if ".weekly." in f.name else "daily"
        ),
    }


def list_all_backup_files(filter_ip: str | None = None) -> list[dict]:
    """Walk all router dirs. Returns list with ip, name, dir_name, filename, size, mtime, type."""
    results = []
    if not BACKUP_DIR.exists():
        return results
    for d in sorted(BACKUP_DIR.iterdir()):
        if not d.is_dir():
            continue
        parsed = parse_dir_name(d.name)
        ip = parsed["ip"]
        name = parsed["name"]
        if filter_ip and ip != filter_ip:
            continue
        for f in sorted(d.glob("*.rsc"), reverse=True):
            try:
                results.append(_file_info(d, f, ip, name))
            except Exception:
                pass
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def list_router_files(router: dict) -> list[dict]:
    """List .rsc files for a specific router."""
    d = router_dir(router)
    if not d.exists():
        return []
    results = []
    for f in sorted(d.glob("*.rsc"), reverse=True):
        try:
            results.append(_file_info(d, f, router["ip"], router["name"]))
        except Exception:
            pass
    return results


def get_backup_path(router: dict, filename: str) -> Path | None:
    """Resolve a backup file path. Returns None if not found or unsafe."""
    if "/" in filename or ".." in filename or filename.startswith("."):
        return None
    d = router_dir(router)
    path = (d / filename).resolve()
    if not str(path).startswith(str(d.resolve())):
        return None
    if not path.exists() or not path.is_file():
        return None
    return path


def find_backup_path_by_ip(ip: str, filename: str) -> Path | None:
    """Search any router dir matching the IP for the file. Handles folder rename gracefully."""
    if "/" in filename or ".." in filename or filename.startswith("."):
        return None
    if not BACKUP_DIR.exists():
        return None
    for d in sorted(BACKUP_DIR.iterdir()):
        if not d.is_dir():
            continue
        parsed = parse_dir_name(d.name)
        if parsed["ip"] != ip:
            continue
        candidate = (d / filename).resolve()
        if not str(candidate).startswith(str(d.resolve())):
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    return None
