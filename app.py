import json
import platform
import queue
import threading
import uuid
from flask import Flask, Response, render_template, request, jsonify

app = Flask(__name__)

JOB_QUEUES: dict = {}
JOB_LOCK = threading.Lock()


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


def _run_job(job_id: str, paths: list, categories: list) -> None:
    from cleaner import CLEANERS

    q = JOB_QUEUES[job_id]
    from cleaner.base import StatusReporter
    reporter = StatusReporter(q)

    for cat in categories:
        if cat in CLEANERS:
            try:
                CLEANERS[cat].run(paths, reporter)
            except Exception as e:
                from cleaner.base import Status
                reporter.emit(cat, "Unhandled error", Status.ERROR, str(e))

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
