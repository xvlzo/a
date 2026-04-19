import os
import glob
import subprocess
import platform
from .base import BaseCleaner, StatusReporter

CAT = "app_artifacts"


def _run(cmd: list, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _office_mru(paths: list, reporter: StatusReporter) -> None:
    artifact = "Microsoft Office MRU"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        apps = ["Word", "Excel", "PowerPoint", "Access", "Publisher", "Visio", "Project"]
        versions = ["14.0", "15.0", "16.0"]
        deleted = 0
        for version in versions:
            for app in apps:
                for mru_sub in ["File MRU", "Place MRU"]:
                    key_path = f"Software\\Microsoft\\Office\\{version}\\{app}\\{mru_sub}"
                    try:
                        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, access=winreg.KEY_ALL_ACCESS)
                        to_del = []
                        i = 0
                        while True:
                            try:
                                name, data, _ = winreg.EnumValue(key, i)
                                val = str(data).lower()
                                if any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower):
                                    to_del.append(name)
                                i += 1
                            except OSError:
                                break
                        for name in to_del:
                            winreg.DeleteValue(key, name)
                            deleted += 1
                        winreg.CloseKey(key)
                    except FileNotFoundError:
                        pass
        reporter.ok(CAT, artifact, f"Deleted {deleted} Office MRU entry(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _file_ext_handlers(paths: list, reporter: StatusReporter) -> None:
    artifact = "File extension handlers (OpenWithList)"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        exts = {os.path.splitext(p)[1].lower() for p in paths if os.path.splitext(p)[1]}
        deleted = 0
        for ext in exts:
            base_path = f"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\{ext}\\OpenWithList"
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_path, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        val = str(data).lower()
                        if any(b in val for b in basenames_lower):
                            to_del.append(name)
                        i += 1
                    except OSError:
                        break
                for name in to_del:
                    winreg.DeleteValue(key, name)
                    deleted += 1
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass
        reporter.ok(CAT, artifact, f"Deleted {deleted} handler entry(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _archive_tools(paths: list, reporter: StatusReporter) -> None:
    artifact = "Archive tool history (7-Zip / WinRAR)"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        deleted = 0

        # WinRAR
        for rar_key in [r"Software\WinRAR\ArcHistory", r"Software\WinRAR SFX"]:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rar_key, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        val = str(data).lower()
                        if any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower):
                            to_del.append(name)
                        i += 1
                    except OSError:
                        break
                for name in to_del:
                    winreg.DeleteValue(key, name)
                    deleted += 1
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass

        # 7-Zip recent files via config file
        sevenzip_ini = os.path.expandvars(r"%APPDATA%\7-Zip\7-zip.ini")
        if os.path.exists(sevenzip_ini):
            try:
                with open(sevenzip_ini, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                before = len(lines)
                lines = [l for l in lines if not (
                    any(b in l.lower() for b in basenames_lower) or
                    any(p in l.lower() for p in path_set_lower)
                )]
                with open(sevenzip_ini, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                deleted += before - len(lines)
            except Exception:
                pass

        reporter.ok(CAT, artifact, f"Deleted {deleted} entry(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _cortana_search(reporter: StatusReporter) -> None:
    artifact = "Cortana / Windows Search cache"
    reporter.running(CAT, artifact)
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Packages\Microsoft.Windows.Search_*\LocalState\DeviceSearchCache\*"
    )
    deleted = 0
    for f in glob.glob(pattern):
        try:
            os.remove(f)
            deleted += 1
        except Exception:
            pass
    # Also clear Windows Search app cache
    pattern2 = os.path.expandvars(
        r"%LOCALAPPDATA%\Packages\Microsoft.Windows.Search_*\LocalState\*"
    )
    for f in glob.glob(pattern2):
        if os.path.isfile(f):
            try:
                os.remove(f)
                deleted += 1
            except Exception:
                pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} cache file(s)")
    else:
        reporter.skip(CAT, artifact, "No Cortana/Search cache files found")


def _defender_history(paths: list, reporter: StatusReporter) -> None:
    artifact = "Windows Defender scan history"
    reporter.running(CAT, artifact)
    basenames_bytes = [os.path.basename(p).lower().encode() for p in paths]
    detection_dir = r"C:\ProgramData\Microsoft\Windows Defender\Scans\History\Service\DetectionHistory"
    deleted = 0
    if os.path.isdir(detection_dir):
        for root, dirs, files in os.walk(detection_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        content = f.read()
                    if any(b in content.lower() for b in basenames_bytes):
                        os.remove(fpath)
                        deleted += 1
                except Exception:
                    pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} detection history entry(s)")
    else:
        reporter.skip(CAT, artifact, "No matching Defender history entries found")


def _reliability_monitor(reporter: StatusReporter) -> None:
    artifact = "Reliability Monitor history"
    reporter.running(CAT, artifact)
    rel_dir = r"C:\ProgramData\Microsoft\Windows\Reliability"
    deleted = 0
    if os.path.isdir(rel_dir):
        for f in glob.glob(os.path.join(rel_dir, "*")):
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    deleted += 1
            except Exception:
                pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} file(s)")
    else:
        reporter.skip(CAT, artifact, "No Reliability Monitor files found")


def _smartscreen_telemetry(reporter: StatusReporter) -> None:
    artifact = "SmartScreen telemetry"
    reporter.running(CAT, artifact)
    _run(["wevtutil", "cl", "Microsoft-Windows-SmartScreen/Debug"])
    # Clear SmartScreen local store
    ss_path = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\SmartScreen")
    deleted = 0
    for f in glob.glob(os.path.join(ss_path, "*")):
        try:
            if os.path.isfile(f):
                os.remove(f)
                deleted += 1
        except Exception:
            pass
    reporter.ok(CAT, artifact, f"Event log cleared; {deleted} local SmartScreen file(s) removed")


class AppArtifactsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["Microsoft Office MRU", "File extension handlers (OpenWithList)",
                         "Archive tool history (7-Zip / WinRAR)", "Cortana / Windows Search cache",
                         "Windows Defender scan history", "Reliability Monitor history",
                         "SmartScreen telemetry"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _office_mru(paths, reporter)
        _file_ext_handlers(paths, reporter)
        _archive_tools(paths, reporter)
        _cortana_search(reporter)
        _defender_history(paths, reporter)
        _reliability_monitor(reporter)
        _smartscreen_telemetry(reporter)
