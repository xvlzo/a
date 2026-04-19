import os
import glob
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
        deleted = 0
        errors = []
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
            from Registry import Registry
            _scrub_amcache(amcache_path, paths)
            reporter.ok(CAT, artifact)
        except ImportError:
            reporter.warn(CAT, artifact, "python-registry not installed; install with: pip install python-registry")
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

    def _check_and_delete(key):
        to_delete = []
        try:
            for i in range(key.values_number()):
                v = key.value(key.values()[i].name())
                vdata = str(v.value()).lower()
                if any(b in vdata for b in basenames) or any(p in vdata for p in path_set):
                    to_delete.append(v.name())
        except Exception:
            pass
        return to_delete

    def _walk(key):
        try:
            for subkey in key.subkeys():
                _walk(subkey)
        except Exception:
            pass

    _walk(reg.root())


def _srum(reporter: StatusReporter) -> None:
    artifact = "SRUM database"
    reporter.running(CAT, artifact)
    _run(["sc", "stop", "SruMSvc"])
    time.sleep(1)
    sru_dir = r"C:\Windows\System32\sru"
    deleted = 0
    errors = []
    for f in glob.glob(os.path.join(sru_dir, "SRUDB*")):
        try:
            os.remove(f)
            deleted += 1
        except Exception as e:
            errors.append(os.path.basename(f) + ": " + str(e))
    _run(["sc", "start", "SruMSvc"])
    if errors:
        reporter.warn(CAT, artifact, f"Deleted {deleted} file(s). Locked: {'; '.join(errors)}")
    elif deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} file(s)")
    else:
        reporter.skip(CAT, artifact, "SRUDB files not found")


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
                try:
                    i = 0
                    while True:
                        name, _, _ = winreg.EnumValue(key, i)
                        nl = name.lower()
                        if any(b in nl for b in basenames_lower) or any(p in nl for p in path_set_lower):
                            to_del.append(name)
                        i += 1
                except OSError:
                    pass
                for name in to_del:
                    winreg.DeleteValue(key, name)
                    deleted += 1
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass
            except Exception:
                pass
        reporter.ok(CAT, artifact, f"Deleted {deleted} value(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _event_logs(reporter: StatusReporter) -> None:
    logs = [
        "Security", "System", "Application",
        "Microsoft-Windows-PowerShell/Operational",
        "Microsoft-Windows-Windows Defender/Operational",
        "Microsoft-Windows-SmartScreen/Debug",
    ]
    for log in logs:
        artifact = f"Event Log: {log}"
        reporter.running(CAT, artifact)
        rc, _, err = _run(["wevtutil", "cl", log])
        if rc == 0:
            reporter.ok(CAT, artifact)
        else:
            reporter.warn(CAT, artifact, err or "Requires elevation or log not found")


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
                    if any(b in content.lower() for b in basenames_bytes) or any(p in content.lower() for p in path_bytes):
                        os.remove(fpath)
                        deleted += 1
                    elif fname.endswith((".hdmp", ".mdmp", ".wer", ".cab")):
                        os.remove(fpath)
                        deleted += 1
                except Exception:
                    pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} WER file(s)")
    else:
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
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} MSI log(s)")
    else:
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
    if deleted:
        reporter.ok(CAT, artifact, f"Removed {deleted} SDB shim(s)")
    else:
        reporter.skip(CAT, artifact, "No matching SDB files found")


class OsLogsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["Prefetch", "Amcache.hve / PCA", "SRUM database", "AppCompatFlags registry",
                         "BAM/DAM registry", "ShimCache (AppCompatCache)", "WER crash reports",
                         "MSI install logs", "SDB shim files"] + [f"Event Log: {l}" for l in ["Security", "System", "Application"]]:
                reporter.skip(CAT, name, "Windows only")
            return
        _prefetch(paths, reporter)
        _amcache(paths, reporter)
        _srum(reporter)
        _appcompat_flags(paths, reporter)
        _event_logs(reporter)
        _bam_dam(paths, reporter)
        _shimcache(reporter)
        _wer(paths, reporter)
        _msi_logs(paths, reporter)
        _sdb_shims(paths, reporter)
