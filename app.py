import json
import logging
import os
import platform
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from flask import Flask, Response, render_template, request, jsonify

# ---------------------------------------------------------------------------
# Logging — structured output to stdout + rotating file
# ---------------------------------------------------------------------------
_log_file = f"artifact_eraser_{time.strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("artifact_eraser")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB max request body

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------
JOB_QUEUES: dict[str, queue.Queue] = {}
JOB_REPORTS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()

MAX_CONCURRENT_JOBS = 3
MAX_PATHS = 50
MAX_PATH_LEN = 32767  # Windows extended-length path limit

# ---------------------------------------------------------------------------
# Execution waves
# Wave 1: sequential — secure wipe must complete before filesystem journal ops
# Wave 2: parallel   — independent artifact categories
# Wave 3: sequential — app_artifacts must be last (dangling-ref verification pass)
# ---------------------------------------------------------------------------
WAVE_1 = ["secure_wipe", "filesystem"]
WAVE_2 = ["os_logs", "shell_history", "network_artifacts", "memory_artifacts"]
WAVE_3 = ["app_artifacts"]
ALL_CATEGORIES = WAVE_1 + WAVE_2 + WAVE_3


# ---------------------------------------------------------------------------
# Admin detection
# ---------------------------------------------------------------------------
def _is_admin() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ADMIN_WARNINGS: list[str] = []
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
    ]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _validate_paths(raw: list) -> tuple[list[str], list[str]]:
    """Return (valid_paths, error_messages). Deduplicates and normalises."""
    valid: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    for p in raw[:MAX_PATHS]:
        p = str(p).strip()
        if not p:
            continue
        if len(p) > MAX_PATH_LEN:
            errors.append(f"Path too long: {p[:80]}…")
            continue
        if not os.path.isabs(p):
            errors.append(f"Relative path rejected (must be absolute): {p}")
            continue
        norm = os.path.normpath(p)
        if norm in seen:
            continue
        seen.add(norm)
        valid.append(norm)

    return valid, errors


# ---------------------------------------------------------------------------
# Post-wipe second pass  (covers artifacts our own deletion created)
# ---------------------------------------------------------------------------
def _post_wipe_cleanup(paths: list[str], reporter) -> None:
    if platform.system() != "Windows":
        return

    import glob
    import subprocess

    def _run(cmd: list) -> tuple[int, str, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    # Re-clear USN journal — captures file-overwrite and deletion entries we wrote
    drives = {
        os.path.splitdrive(p)[0].upper().rstrip("\\") + "\\"
        for p in paths if os.path.splitdrive(p)[0]
    } or {"C:\\"}

    for drive in drives:
        letter = drive.rstrip("\\")
        reporter.running("filesystem", f"Post-wipe USN clear ({letter})")
        rc, _, err = _run(["fsutil", "usn", "deletejournal", "/D", letter])
        if rc == 0:
            _run(["fsutil", "usn", "createjournal", "m=0x800000", "a=0x100000", letter])
            reporter.ok("filesystem", f"Post-wipe USN clear ({letter})")
        else:
            reporter.warn("filesystem", f"Post-wipe USN clear ({letter})", err or "Requires elevation")

    # Re-check event logs for new deletion artifacts
    from cleaner.os_logs import _log_contains_path, _CHECKED_LOGS
    basenames = [os.path.basename(p) for p in paths]
    for log_name in _CHECKED_LOGS:
        artifact = f"Post-wipe Event Log check: {log_name}"
        reporter.running("os_logs", artifact)
        if _log_contains_path(log_name, basenames):
            rc, _, err = _run(["wevtutil", "cl", log_name])
            if rc == 0:
                reporter.ok("os_logs", artifact, "Deletion artifacts found and cleared")
            else:
                reporter.warn("os_logs", artifact, err or "Requires elevation")
        else:
            reporter.skip("os_logs", artifact, "No new deletion artifacts found")

    # Remove python.exe from BAM/DAM (records our tool's execution path)
    reporter.running("os_logs", "Post-wipe: python.exe from BAM/DAM")
    try:
        import winreg
        bam_base = r"SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
        deleted = 0
        try:
            bam_key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, bam_base, access=winreg.KEY_ALL_ACCESS
            )
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(bam_key, i)
                    try:
                        sid_key = winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE,
                            bam_base + "\\" + sid,
                            access=winreg.KEY_ALL_ACCESS,
                        )
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
        except Exception:
            pass
        reporter.ok("os_logs", "Post-wipe: python.exe from BAM/DAM", f"Removed {deleted} entry(s)")
    except ImportError:
        reporter.skip("os_logs", "Post-wipe: python.exe from BAM/DAM", "winreg not available")

    # Remove python prefetch entries
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


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------
def _run_job(job_id: str, paths: list[str], categories: list[str]) -> None:
    from cleaner import CLEANERS
    from cleaner.base import Status, StatusReporter

    q = JOB_QUEUES[job_id]
    reporter = StatusReporter(q)
    start = time.time()

    log.info("Job %s started | paths=%d categories=%s", job_id, len(paths), categories)

    wave1 = [c for c in WAVE_1 if c in categories]
    wave2 = [c for c in WAVE_2 if c in categories]
    wave3 = [c for c in WAVE_3 if c in categories]
    unknown = [c for c in categories if c not in ALL_CATEGORIES]

    def _run_cat(cat: str) -> None:
        if cat not in CLEANERS:
            return
        try:
            CLEANERS[cat].run(paths, reporter)
        except Exception as exc:
            reporter.emit(cat, "Unhandled error", Status.ERROR, str(exc))
            log.exception("Unhandled error in category %s (job %s)", cat, job_id)

    # Wave 1 — sequential
    for cat in wave1:
        _run_cat(cat)

    # Wave 2 — parallel (independent categories, shared thread-safe queue)
    if wave2:
        workers = min(len(wave2), 4)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wave2") as ex:
            futs = {ex.submit(_run_cat, cat): cat for cat in wave2}
            for fut in as_completed(futs):
                if exc := fut.exception():
                    log.error("Wave-2 %s raised: %s", futs[fut], exc)

    # Wave 3 — sequential (dangling-ref check must be last artifact op)
    for cat in wave3:
        _run_cat(cat)

    for cat in unknown:
        _run_cat(cat)

    # Second pass — cover artifacts our own wipe process created
    try:
        _post_wipe_cleanup(paths, reporter)
    except Exception as exc:
        reporter.emit("filesystem", "Post-wipe cleanup error", Status.ERROR, str(exc))
        log.exception("Post-wipe cleanup error (job %s)", job_id)

    elapsed = round(time.time() - start, 2)
    log.info("Job %s completed in %.2fs", job_id, elapsed)

    # Persist report for export endpoint
    with JOB_LOCK:
        if job_id in JOB_REPORTS:
            JOB_REPORTS[job_id].update({
                "end_ts": time.time(),
                "elapsed_s": elapsed,
                "events": [e.to_dict() for e in reporter.get_events()],
            })

    q.put({"done": True, "elapsed": elapsed})

    # Auto-clean after 1 hour
    def _cleanup() -> None:
        with JOB_LOCK:
            JOB_QUEUES.pop(job_id, None)
            JOB_REPORTS.pop(job_id, None)

    threading.Timer(3600, _cleanup).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/check_admin")
def check_admin():
    return jsonify({"is_admin": _is_admin(), "warnings": ADMIN_WARNINGS})


@app.post("/erase")
def erase():
    data = request.get_json(force=True)
    raw_paths: list = [str(p) for p in data.get("paths", []) if str(p).strip()]
    categories: list[str] = [
        str(c) for c in data.get("categories", []) if isinstance(c, str)
    ]

    if not raw_paths:
        return jsonify({"error": "No paths provided"}), 400
    if not categories:
        return jsonify({"error": "No categories selected"}), 400

    paths, errors = _validate_paths(raw_paths)
    if not paths:
        return jsonify({"error": "No valid paths after validation", "details": errors}), 400

    with JOB_LOCK:
        if len(JOB_QUEUES) >= MAX_CONCURRENT_JOBS:
            return jsonify({"error": f"Too many concurrent jobs (max {MAX_CONCURRENT_JOBS})"}), 429

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()

    with JOB_LOCK:
        JOB_QUEUES[job_id] = q
        JOB_REPORTS[job_id] = {
            "job_id": job_id,
            "paths": paths,
            "categories": categories,
            "start_ts": time.time(),
            "end_ts": None,
            "elapsed_s": None,
            "validation_errors": errors,
            "events": [],
        }

    threading.Thread(
        target=_run_job, args=(job_id, paths, categories), daemon=True, name=f"job-{job_id[:8]}"
    ).start()

    log.info("Job %s queued | valid_paths=%s | skipped_errors=%d", job_id, paths, len(errors))
    return jsonify({"job_id": job_id, "validation_errors": errors})


@app.get("/status/<job_id>")
def status(job_id: str):
    try:
        uuid.UUID(job_id, version=4)
    except ValueError:
        return jsonify({"error": "Invalid job ID"}), 400

    def _generate():
        with JOB_LOCK:
            q = JOB_QUEUES.get(job_id)
        if q is None:
            yield f"data: {json.dumps({'error': 'Unknown or expired job'})}\n\n"
            return
        while True:
            try:
                event = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if isinstance(event, dict):
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("done"):
                    break
            else:
                yield f"data: {json.dumps(event.to_dict())}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/report/<job_id>")
def report(job_id: str):
    try:
        uuid.UUID(job_id, version=4)
    except ValueError:
        return jsonify({"error": "Invalid job ID"}), 400

    with JOB_LOCK:
        r = JOB_REPORTS.get(job_id)
    if r is None:
        return jsonify({"error": "Report not found or expired (1-hour TTL)"}), 404

    return jsonify(r)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Artifact Eraser v1.0 starting — http://127.0.0.1:5000")
    log.info("Log file: %s", _log_file)
    log.info("Admin: %s", _is_admin())
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
