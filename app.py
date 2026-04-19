import json
import platform
import queue
import threading
import uuid
from flask import Flask, Response, render_template, request, jsonify

app = Flask(__name__)

JOB_QUEUES: dict = {}
JOB_LOCK = threading.Lock()

# Enforced execution order — secure_wipe must finish before filesystem,
# filesystem must clear USN/VSS before os_logs checks event logs, etc.
CATEGORY_ORDER = [
    "secure_wipe",
    "filesystem",
    "os_logs",
    "shell_history",
    "browser_history",
    "network_artifacts",
    "app_artifacts",
    "memory_artifacts",
]


def _is_admin() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ADMIN_WARNINGS = []
if platform.system() == "Windows" and not _is_admin():
    ADMIN_WARNINGS = [
        "VSS deletion (vssadmin requires elevation)",
        "USN Journal deletion (fsutil /D requires elevation)",
        "Amcache.hve modification (requires PcaSvc stop + admin)",
        "SRUM database deletion (requires SruMSvc stop + admin)",
        "HKLM registry keys (BAM/DAM, ShimCache, AppCompatFlags)",
        "Prefetch folder access (requires admin)",
        "Event Log clearing (requires admin)",
        "Firewall log deletion (requires admin)",
        "Hibernation disable (requires admin)",
        "Page file settings (requires admin)",
    ]


def _post_wipe_cleanup(paths: list, reporter) -> None:
    """
    Second pass after all cleaners:
    Re-clear USN journal and re-check event logs to cover deletion artifacts our
    own wipe process created (file overwrites + deletion generate new USN/audit entries).
    Also scrub our own process (python.exe) from BAM/DAM.
    """
    if platform.system() != "Windows":
        return

    from cleaner.base import Status
    import subprocess, os, glob

    def _run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    # --- Re-clear USN journal (captures our own file overwrites + deletion) ---
    drives = {os.path.splitdrive(p)[0].upper().rstrip("\\") + "\\" for p in paths if os.path.splitdrive(p)[0]}
    if not drives:
        drives = {"C:\\"}
    for drive in drives:
        letter = drive.rstrip("\\")
        reporter.running("filesystem", f"Post-wipe USN clear ({letter})")
        rc, _, err = _run(["fsutil", "usn", "deletejournal", "/D", letter])
        if rc == 0:
            _run(["fsutil", "usn", "createjournal", "m=0x800000", "a=0x100000", letter])
            reporter.ok("filesystem", f"Post-wipe USN clear ({letter})")
        else:
            reporter.warn("filesystem", f"Post-wipe USN clear ({letter})", err or "Requires elevation")

    # --- Re-check event logs for new deletion artifacts ---
    from cleaner.os_logs import _log_contains_path, _CHECKED_LOGS
    basenames = [os.path.basename(p) for p in paths]
    for log in _CHECKED_LOGS:
        artifact = f"Post-wipe Event Log check: {log}"
        reporter.running("os_logs", artifact)
        if _log_contains_path(log, basenames):
            rc, _, err = _run(["wevtutil", "cl", log])
            if rc == 0:
                reporter.ok("os_logs", artifact, "Deletion artifacts found and cleared")
            else:
                reporter.warn("os_logs", artifact, err or "Requires elevation")
        else:
            reporter.skip("os_logs", artifact, "No new deletion artifacts found")

    # --- Remove python.exe from BAM/DAM (records our tool's execution) ---
    reporter.running("os_logs", "Post-wipe: python.exe from BAM/DAM")
    try:
        import winreg
        bam_base = r"SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
        deleted = 0
        try:
            bam_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, bam_base, access=winreg.KEY_ALL_ACCESS)
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(bam_key, i)
                    sid_key_path = bam_base + "\\" + sid
                    try:
                        sid_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sid_key_path,
                                                  access=winreg.KEY_ALL_ACCESS)
                        to_del = []
                        j = 0
                        while True:
                            try:
                                name, _, _ = winreg.EnumValue(sid_key, j)
                                if "python" in name.lower():
                                    to_del.append(name)
                                j += 1
                            except OSError:
                                break
                        for name in to_del:
                            winreg.DeleteValue(sid_key, name)
                            deleted += 1
                        winreg.CloseKey(sid_key)
                    except Exception:
                        pass
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(bam_key)
        except (FileNotFoundError, Exception):
            pass
        reporter.ok("os_logs", "Post-wipe: python.exe from BAM/DAM", f"Removed {deleted} entry(s)")
    except ImportError:
        reporter.skip("os_logs", "Post-wipe: python.exe from BAM/DAM", "winreg not available")

    # --- Remove python prefetch entry ---
    reporter.running("os_logs", "Post-wipe: python.exe Prefetch")
    pf_dir = r"C:\Windows\Prefetch"
    deleted = 0
    if os.path.isdir(pf_dir):
        for pf in glob.glob(os.path.join(pf_dir, "PYTHON*.pf")):
            try:
                os.remove(pf)
                deleted += 1
            except Exception:
                pass
    if deleted:
        reporter.ok("os_logs", "Post-wipe: python.exe Prefetch", f"Removed {deleted} file(s)")
    else:
        reporter.skip("os_logs", "Post-wipe: python.exe Prefetch", "No python prefetch found")


def _run_job(job_id: str, paths: list, categories: list) -> None:
    from cleaner import CLEANERS
    from cleaner.base import StatusReporter

    q = JOB_QUEUES[job_id]
    reporter = StatusReporter(q)

    # Enforce execution order regardless of checkbox order in request
    ordered = [c for c in CATEGORY_ORDER if c in categories]
    # Include any unknown categories at the end (future-proofing)
    ordered += [c for c in categories if c not in ordered]

    for cat in ordered:
        if cat in CLEANERS:
            try:
                CLEANERS[cat].run(paths, reporter)
            except Exception as e:
                from cleaner.base import Status
                reporter.emit(cat, "Unhandled error", Status.ERROR, str(e))

    # Post-wipe second pass: cover deletion artifacts our own tool created
    try:
        _post_wipe_cleanup(paths, reporter)
    except Exception as e:
        from cleaner.base import Status
        reporter.emit("filesystem", "Post-wipe cleanup error", Status.ERROR, str(e))

    q.put(None)  # sentinel

    def _cleanup():
        with JOB_LOCK:
            JOB_QUEUES.pop(job_id, None)

    threading.Timer(300, _cleanup).start()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/check_admin")
def check_admin():
    return jsonify({"is_admin": _is_admin(), "warnings": ADMIN_WARNINGS})


@app.post("/erase")
def erase():
    data = request.get_json(force=True)
    paths = [p.strip() for p in data.get("paths", []) if p.strip()]
    categories = data.get("categories", [])

    if not paths:
        return jsonify({"error": "No paths provided"}), 400
    if not categories:
        return jsonify({"error": "No categories selected"}), 400

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    with JOB_LOCK:
        JOB_QUEUES[job_id] = q

    t = threading.Thread(target=_run_job, args=(job_id, paths, categories), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.get("/status/<job_id>")
def status(job_id: str):
    def _generate():
        with JOB_LOCK:
            q = JOB_QUEUES.get(job_id)
        if q is None:
            yield f"data: {json.dumps({'error': 'Unknown job'})}\n\n"
            return
        while True:
            try:
                event = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event is None:
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            yield f"data: {json.dumps(event.to_dict())}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
