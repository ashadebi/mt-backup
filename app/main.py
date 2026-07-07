"""MikroTik Backup Panel — FastAPI app"""
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Form, Request, HTTPException, Depends
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, database, ssh_backup

app = FastAPI(title="MikroTik Backup Panel")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("MT_SECRET_KEY", secrets.token_hex(32)),
    session_cookie="mt_backup_session",
    max_age=86400 * 7,
    same_site="lax",
    https_only=False,  # set True behind reverse proxy
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Redirect HTML requests to /login on 401, otherwise return JSON."""
    if exc.status_code == 401:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)



@app.on_event("startup")
def on_startup():
    database.init_db()


# ---- Helpers ----

def _ctx(request: Request, **extra) -> dict:
    base = {
        "request": request,
        "session_user": auth.current_user(request),
        "current_year": datetime.now().year,
    }
    base.update(extra)
    return base


def _read_loadavg() -> tuple:
    try:
        return os.getloadavg()
    except Exception:
        return (0.0, 0.0, 0.0)


def _read_meminfo() -> dict:
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip().split()[0]  # value in kB
    except Exception:
        pass
    return info


def _disk_usage() -> dict:
    try:
        u = shutil.disk_usage("/")
        return {
            "total_gb": round(u.total / 1024**3, 1),
            "used_gb": round(u.used / 1024**3, 1),
            "free_gb": round(u.free / 1024**3, 1),
            "percent": int(100 * u.used / u.total) if u.total else 0,
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0}


def get_system_stats() -> dict:
    load = _read_loadavg()
    mem = _read_meminfo()
    total_mb = int(mem.get("MemTotal", 0)) // 1024
    avail_mb = int(mem.get("MemAvailable", 0)) // 1024
    used_mb = total_mb - avail_mb
    return {
        "cpu_load_1": load[0],
        "cpu_load_5": load[1],
        "cpu_load_15": load[2],
        "mem_used_mb": used_mb,
        "mem_total_mb": total_mb,
        "mem_percent": int(100 * used_mb / total_mb) if total_mb else 0,
        "disk": _disk_usage(),
    }


# ---- Routes ----

@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    return HTMLResponse(content="ok", status_code=200)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", _ctx(request, error=None))


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.verify_login(username, password)
    if user:
        return auth.login_response(request, user)
    return templates.TemplateResponse(
        "login.html", _ctx(request, error="Username atau password salah"),
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    return auth.logout_response(request)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(auth.require_login)):
    stats = database.get_stats()
    sys_stats = get_system_stats()
    routers = database.list_routers()
    logs = database.list_logs(limit=10)
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, stats=stats, sys_stats=sys_stats, routers=routers, logs=logs, user=user),
    )


# ---- Routers CRUD ----

@app.get("/routers", response_class=HTMLResponse)
def routers_list(request: Request, user=Depends(auth.require_login)):
    routers = database.list_routers()
    return templates.TemplateResponse(
        "routers.html", _ctx(request, routers=routers, user=user),
    )


@app.get("/routers/new", response_class=HTMLResponse)
def router_new_get(request: Request, user=Depends(auth.require_login)):
    return templates.TemplateResponse(
        "router_form.html", _ctx(request, router=None, error=None, user=user),
    )


@app.post("/routers/new")
def router_new_post(
    request: Request,
    ip: str = Form(...),
    port: int = Form(2282),
    username: str = Form(...),
    password: str = Form(...),
    enabled: int = Form(1),
    user=Depends(auth.require_admin),
):
    # Auto-add known host (one-time, best-effort)
    ssh_backup.add_known_host(ip, port)

    # Auto-detect from router (identity, snmp location, model -> device_type)
    info = ssh_backup.detect_device_info(ip, port, username, password, timeout=8)
    if not info.get("success"):
        return templates.TemplateResponse(
            "router_form.html",
            _ctx(request, error=f"Tidak bisa konek ke router: {info.get('error', 'unknown')}",
                 user=user, detected=None, ip=ip, port=port, username=username),
            status_code=400,
        )

    # Fallback name if identity empty
    name = info.get("identity") or f"Router-{ip.split('.')[-1]}"
    location = info.get("location", "")
    device_type = info.get("device_type", "router")

    try:
        encrypted = ssh_backup.encrypt_password(password)
    except Exception as e:
        return templates.TemplateResponse(
            "router_form.html",
            _ctx(request, error=f"Encryption failed: {e}", user=user,
                 detected=info, ip=ip, port=port, username=username),
            status_code=500,
        )
    new_id = database.create_router(
        name=name, ip=ip, port=port, username=username, password_encrypted=encrypted,
        device_type=device_type, location=location, enabled=enabled,
    )
    # Save auto-detected identity explicitly
    if info.get("identity"):
        database.set_router_identity(new_id, info["identity"])
    return RedirectResponse(url=f"/routers/{new_id}", status_code=303)


@app.get("/routers/{router_id}", response_class=HTMLResponse)
def router_detail(router_id: int, request: Request, user=Depends(auth.require_login)):
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    # Don't expose encrypted password
    router.pop("password_encrypted", None)
    logs = database.list_logs(limit=50, router_id=router_id)
    files = ssh_backup.list_router_files(router)
    qp = request.query_params
    return templates.TemplateResponse(
        "router_detail.html",
        _ctx(request, router=router, logs=logs, files=files, user=user, qp=qp,
             backup_dir=str(ssh_backup.router_dir(router))),
    )


@app.get("/routers/{router_id}/edit", response_class=HTMLResponse)
def router_edit_get(router_id: int, request: Request, user=Depends(auth.require_login)):
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    router.pop("password_encrypted", None)
    return templates.TemplateResponse(
        "router_form.html", _ctx(request, router=router, error=None, user=user),
    )


@app.post("/routers/{router_id}/edit")
def router_edit_post(
    router_id: int,
    request: Request,
    name: str = Form(...),
    ip: str = Form(...),
    port: int = Form(2282),
    username: str = Form(...),
    password: str = Form(""),  # empty = keep current
    device_type: str = Form("router"),
    location: str = Form(""),
    enabled: int = Form(1),
    user=Depends(auth.require_admin),
):
    current = database.get_router(router_id)
    if not current:
        raise HTTPException(status_code=404, detail="Router not found")
    if password:
        try:
            encrypted = ssh_backup.encrypt_password(password)
        except Exception as e:
            return templates.TemplateResponse(
                "router_form.html",
                _ctx(request, router=current, error=f"Encryption failed: {e}", user=user),
                status_code=500,
            )
    else:
        encrypted = current["password_encrypted"]
    database.update_router(
        router_id, name=name, ip=ip, port=port, username=username, password_encrypted=encrypted,
        device_type=device_type, location=location, enabled=enabled,
    )
    return RedirectResponse(url=f"/routers/{router_id}", status_code=303)


@app.post("/routers/{router_id}/delete")
def router_delete(router_id: int, request: Request, user=Depends(auth.require_admin)):
    database.delete_router(router_id)
    return RedirectResponse(url="/routers", status_code=303)


@app.post("/routers/{router_id}/delete-all-backups")
def router_delete_all_backups(router_id: int, request: Request, user=Depends(auth.require_admin)):
    """Delete ALL .rsc backup files for this router. Router entry is preserved."""
    from pathlib import Path
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    backup_root = Path(ssh_backup.BACKUP_DIR)
    router_folder = backup_root / f"{router['ip']} - {ssh_backup.sanitize_name(router['name'])}"
    deleted = 0
    if router_folder.exists() and router_folder.is_dir():
        for f in router_folder.glob("*.rsc"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    return RedirectResponse(url=f"/routers/{router_id}?bulk_deleted={deleted}", status_code=303)


@app.post("/routers/{router_id}/test")
def router_test(router_id: int, request: Request, user=Depends(auth.require_admin)):
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    try:
        password = ssh_backup.decrypt_password(router["password_encrypted"])
    except Exception as e:
        return JSONResponse({"success": False, "error": f"decrypt failed: {e}"}, status_code=500)
    result = ssh_backup.test_connection(router["ip"], router["port"], router["username"], password)
    if result.get("success") and result.get("identity"):
        database.set_router_identity(router_id, result["identity"])
    return JSONResponse(result)


@app.get("/routers/{router_id}/diff", response_class=HTMLResponse)
def router_diff(
    router_id: int,
    request: Request,
    a: str = "",
    b: str = "",
    user=Depends(auth.require_login),
):
    """Show unified diff between two backup files (a vs b)."""
    import difflib
    from pathlib import Path
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    backup_root = Path(ssh_backup.BACKUP_DIR)
    router_folder = backup_root / f"{router['ip']} - {ssh_backup.sanitize_name(router['name'])}"
    files = sorted(
        [f for f in router_folder.glob("*.rsc")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    ) if router_folder.exists() else []
    file_names = [f.name for f in files]

    error = None
    diff_lines = []
    stats = {"added": 0, "removed": 0}
    file_a_meta = None
    file_b_meta = None
    content_a = ""
    content_b = ""

    if a and b:
        path_a = router_folder / a
        path_b = router_folder / b
        # Security: ensure no path traversal
        if not (str(path_a.resolve()).startswith(str(router_folder.resolve())) and
                str(path_b.resolve()).startswith(str(router_folder.resolve()))):
            error = "Invalid file path"
        elif not path_a.exists() or not path_b.exists():
            error = f"File not found: {a if not path_a.exists() else b}"
        else:
            content_a = path_a.read_text(encoding="utf-8", errors="replace")
            content_b = path_b.read_text(encoding="utf-8", errors="replace")
            file_a_meta = {"name": a, "size": path_a.stat().st_size,
                           "mtime": datetime.fromtimestamp(path_a.stat().st_mtime).isoformat()}
            file_b_meta = {"name": b, "size": path_b.stat().st_size,
                           "mtime": datetime.fromtimestamp(path_b.stat().st_mtime).isoformat()}
            lines_a = content_a.splitlines(keepends=True)
            lines_b = content_b.splitlines(keepends=True)
            raw = list(difflib.unified_diff(
                lines_a, lines_b,
                fromfile=a, tofile=b, n=3,
            ))
            for line in raw:
                tag = ""
                if line.startswith("+") and not line.startswith("+++"):
                    tag = "add"
                    stats["added"] += 1
                elif line.startswith("-") and not line.startswith("---"):
                    tag = "del"
                    stats["removed"] += 1
                elif line.startswith("@@"):
                    tag = "hunk"
                elif line.startswith("---") or line.startswith("+++"):
                    tag = "header"
                diff_lines.append({"raw": line.rstrip(), "tag": tag})

    return templates.TemplateResponse(
        "diff.html",
        _ctx(request,
             router=router, files=file_names,
             a=a, b=b, error=error,
             diff_lines=diff_lines, stats=stats,
             file_a=file_a_meta, file_b=file_b_meta,
             user=user),
    )

# ---- Users management (admin only) ----

@app.get("/users", response_class=HTMLResponse)
def users_list(request: Request, user=Depends(auth.require_admin)):
    users = database.list_users()
    return templates.TemplateResponse(
        "users.html", _ctx(request, users=users, user=user),
    )


@app.get("/users/new", response_class=HTMLResponse)
def user_new_get(request: Request, user=Depends(auth.require_admin)):
    return templates.TemplateResponse(
        "user_form.html", _ctx(request, u=None, error=None, user=user),
    )


@app.post("/users/new")
def user_new_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
    enabled: int = Form(1),
    user=Depends(auth.require_admin),
):
    import bcrypt as _bcrypt
    if role not in ("admin", "viewer"):
        role = "viewer"
    password_hash = _bcrypt.hashpw(password[:72].encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode()
    if database.get_user_by_username(username):
        return templates.TemplateResponse(
            "user_form.html",
            _ctx(request, u=None, error=f"Username '{username}' sudah ada", user=user),
            status_code=400,
        )
    try:
        database.create_user(username, password_hash, role, int(enabled))
    except Exception as e:
        return templates.TemplateResponse(
            "user_form.html",
            _ctx(request, u=None, error=f"Gagal: {e}", user=user),
            status_code=500,
        )
    return RedirectResponse(url="/users", status_code=303)


@app.post("/users/{user_id}/toggle")
def user_toggle(request: Request, user_id: int, user=Depends(auth.require_admin)):
    u = database.get_user(user_id)
    if u:
        database.update_user(user_id, enabled=0 if u["enabled"] else 1)
    return RedirectResponse(url="/users", status_code=303)


@app.post("/users/{user_id}/delete")
def user_delete(request: Request, user_id: int, user=Depends(auth.require_admin)):
    database.delete_user(user_id)
    return RedirectResponse(url="/users", status_code=303)


@app.post("/users/{user_id}/reset-password")
def user_reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    user=Depends(auth.require_admin),
):
    import bcrypt as _bcrypt
    password_hash = _bcrypt.hashpw(password[:72].encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode()
    database.update_user_password(user_id, password_hash)
    return RedirectResponse(url="/users", status_code=303)


# ---- Backup view (no download) ----

@app.get("/backups/view", response_class=HTMLResponse)
def backup_view(
    request: Request,
    ip: str = "",
    filename: str = "",
    user=Depends(auth.require_login),
):
    """Display .rsc backup content inline in browser."""
    from pathlib import Path as _P
    if not ip or not filename:
        raise HTTPException(status_code=422, detail="Missing ip or filename")
    router = None
    for r in database.list_routers():
        if r["ip"] == ip:
            router = r
            break
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    backup_root = _P(ssh_backup.BACKUP_DIR)
    router_folder = backup_root / f"{router['ip']} - {ssh_backup.sanitize_name(router['name'])}"
    file_path = router_folder / filename
    try:
        file_resolved = file_path.resolve()
        folder_resolved = router_folder.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not str(file_resolved).startswith(str(folder_resolved)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not file_resolved.exists() or not file_resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    content = file_resolved.read_text(encoding="utf-8", errors="replace")
    size = file_resolved.stat().st_size
    mtime_iso = datetime.fromtimestamp(file_resolved.stat().st_mtime).isoformat()
    lines = []
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            cls = "rsc-comment"
        elif stripped.startswith("/"):
            cls = "rsc-section"
        elif "=" in line and not line.startswith("/"):
            cls = "rsc-set"
        else:
            cls = "rsc-default"
        lines.append({"num": i, "text": line, "cls": cls})
    return templates.TemplateResponse(
        "view_backup.html",
        _ctx(request,
             lines=lines, line_count=len(lines),
             filename=filename, ip=ip,
             router=router, size=size, mtime=mtime_iso, user=user),
    )





@app.post("/routers/{router_id}/backup")
def router_backup_now(router_id: int, request: Request, user=Depends(auth.require_admin)):
    router = database.get_router(router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    result = ssh_backup.run_backup(router)
    status = "success" if result["success"] else "failed"
    database.log_backup(router_id, result["filename"], status, result["size"], result.get("error"))
    database.set_last_backup(router_id, status)
    if result.get("identity"):
        database.set_router_identity(router_id, result["identity"])
    if result["success"]:
        return RedirectResponse(url=f"/routers/{router_id}?backup=ok&file={result['filename']}", status_code=303)
    return RedirectResponse(
        url=f"/routers/{router_id}?backup=fail&error={result.get('error', '')[:200]}",
        status_code=303,
    )


# ---- Backups list / download ----

@app.get("/backups", response_class=HTMLResponse)
def backups_list(request: Request, ip: str | None = None, user=Depends(auth.require_login)):
    files = ssh_backup.list_all_backup_files(filter_ip=ip)
    routers = database.list_routers()
    selected_ip = ip
    return templates.TemplateResponse(
        "backups.html",
        _ctx(request, files=files, routers=routers, selected_ip=selected_ip, user=user),
    )


@app.post("/backups/delete")
def backup_delete(
    request: Request,
    ip: str = "",
    filename: str = "",
    user=Depends(auth.require_admin),
):
    """Delete a single backup file. Accepts ip/filename from query params."""
    # Modal sends via query params; allow form body as fallback
    if not ip:
        ip = request.query_params.get("ip", "")
    if not filename:
        filename = request.query_params.get("filename", "")
    if not ip or not filename:
        raise HTTPException(status_code=422, detail="Missing ip or filename")
    """Delete a single backup file."""
    from pathlib import Path
    # Find router by IP (security: only allow deleting from known routers)
    router = None
    for r in database.list_routers():
        if r["ip"] == ip:
            router = r
            break
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")
    backup_root = Path(ssh_backup.BACKUP_DIR)
    router_folder = backup_root / f"{router['ip']} - {ssh_backup.sanitize_name(router['name'])}"
    file_path = router_folder / filename
    # Security: ensure no path traversal
    try:
        file_resolved = file_path.resolve()
        folder_resolved = router_folder.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not str(file_resolved).startswith(str(folder_resolved)):
        raise HTTPException(status_code=400, detail="Invalid path")
    deleted = False
    if file_resolved.exists() and file_resolved.is_file():
        file_resolved.unlink()
        deleted = True
    # Log to audit (best-effort)
    try:
        database.log_backup(router["id"], filename, "deleted", 0, None)
    except Exception:
        pass
    return RedirectResponse(url=f"/routers/{router['id']}?deleted={filename if deleted else 'fail'}", status_code=303)


@app.get("/backups/download")
def backup_download(ip: str, filename: str, request: Request, user=Depends(auth.require_login)):
    # Search all dirs matching IP (handles folder rename gracefully)
    path = ssh_backup.find_backup_path_by_ip(ip, filename)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        path=str(path), media_type="application/octet-stream",
        filename=filename,
    )


# ---- Cron trigger endpoint (for systemd timer or manual) ----

@app.post("/api/backup/run")
def api_backup_run(request: Request, x_cron_token: str | None = None):
    """Triggered by external cron. Requires X-Cron-Token matching env."""
    expected = os.environ.get("MT_CRON_TOKEN", "")
    if not expected or x_cron_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    routers = database.list_routers()
    results = []
    for r in routers:
        if not r.get("enabled"):
            continue
        result = ssh_backup.run_backup(r)
        status = "success" if result["success"] else "failed"
        database.log_backup(r["id"], result["filename"], status, result["size"], result.get("error"))
        database.set_last_backup(r["id"], status)
        if result.get("identity"):
            database.set_router_identity(r["id"], result["identity"])
        results.append({
            "router_id": r["id"], "ip": r["ip"], "name": r["name"],
            "identity": result.get("identity", ""),
            "status": status, "filename": result["filename"],
            "size": result["size"], "error": result.get("error"),
        })
    return JSONResponse({"ran_at": datetime.utcnow().isoformat(), "results": results})
