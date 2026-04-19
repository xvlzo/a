import os
import glob
import subprocess
import platform
import time
import uuid
from .base import BaseCleaner, StatusReporter

CAT = "filesystem"


def _run(cmd: list) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _drives_from_paths(paths: list) -> set:
    drives = set()
    for p in paths:
        drive = os.path.splitdrive(p)[0]
        if drive:
            drives.add(drive.upper().rstrip("\\") + "\\")
    if not drives:
        drives.add("C:\\")
    return drives


def _usn_journal(paths: list, reporter: StatusReporter) -> None:
    drives = _drives_from_paths(paths)
    for drive in drives:
        letter = drive.rstrip("\\")
        artifact = f"USN Journal ({letter})"
        reporter.running(CAT, artifact)
        rc, _, err = _run(["fsutil", "usn", "deletejournal", "/D", letter])
        if rc == 0:
            _run(["fsutil", "usn", "createjournal", "m=0x800000", "a=0x100000", letter])
            reporter.ok(CAT, artifact)
        else:
            reporter.warn(CAT, artifact, err or "Requires elevation")


def _vss(reporter: StatusReporter) -> None:
    artifact = "Volume Shadow Copies"
    reporter.running(CAT, artifact)
    rc, _, err = _run(["vssadmin", "delete", "shadows", "/all", "/quiet"])
    if rc == 0:
        reporter.ok(CAT, artifact)
    else:
        reporter.warn(CAT, artifact, err or "Requires elevation or no shadows exist")


def _logfile_cycle(paths: list, reporter: StatusReporter) -> None:
    drives = _drives_from_paths(paths)
    for drive in drives:
        artifact = f"$LogFile cycle ({drive.rstrip(chr(92))})"
        reporter.running(CAT, artifact)
        tmp = os.path.join(drive, f"_artclean_{uuid.uuid4().hex}.tmp")
        try:
            chunk = b'\x00' * 1_048_576
            with open(tmp, "wb") as f:
                for _ in range(256):
                    f.write(chunk)
            os.remove(tmp)
            reporter.warn(CAT, artifact, "Best-effort online cycle done; full clear requires offline ntfsfix/WinPE")
        except Exception as e:
            reporter.warn(CAT, artifact, f"Cycle failed: {e}")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


def _dir_timestamps(paths: list, reporter: StatusReporter) -> None:
    artifact = "Parent directory timestamps"
    reporter.running(CAT, artifact)
    normalized = 0
    for path in paths:
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            continue
        try:
            siblings = [os.path.join(parent, f) for f in os.listdir(parent) if os.path.isfile(os.path.join(parent, f))]
            if siblings:
                oldest_mtime = min(os.path.getmtime(s) for s in siblings)
                os.utime(parent, (oldest_mtime, oldest_mtime))
            else:
                drive = os.path.splitdrive(parent)[0] + "\\"
                if os.path.isdir(drive):
                    ref_mtime = os.path.getmtime(drive)
                    os.utime(parent, (ref_mtime, ref_mtime))
            normalized += 1
        except Exception:
            pass
    if normalized:
        reporter.ok(CAT, artifact, f"Normalized {normalized} parent director{'ies' if normalized != 1 else 'y'}")
    else:
        reporter.skip(CAT, artifact, "No parent directories to normalize")


class FilesystemCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["USN Journal", "Volume Shadow Copies", "$LogFile cycle", "Parent directory timestamps"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _usn_journal(paths, reporter)
        _vss(reporter)
        _logfile_cycle(paths, reporter)
        _dir_timestamps(paths, reporter)
