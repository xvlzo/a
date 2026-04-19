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


def _clipboard_history(reporter: StatusReporter) -> None:
    artifact = "Clipboard history"
    reporter.running(CAT, artifact)
    clip_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Clipboard")
    deleted = 0
    if os.path.isdir(clip_dir):
        for root, dirs, files in os.walk(clip_dir):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                    deleted += 1
                except Exception:
                    pass
    # Disable clipboard history via registry
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Policies\Microsoft\Windows\System",
                             access=winreg.KEY_ALL_ACCESS)
        winreg.SetValueEx(key, "AllowClipboardHistory", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
    except Exception:
        pass
    reporter.ok(CAT, artifact, f"Deleted {deleted} clipboard item(s); history disabled via policy")


def _gpu_telemetry(reporter: StatusReporter) -> None:
    artifact = "GPU driver telemetry"
    reporter.running(CAT, artifact)
    deleted = 0
    paths_to_clear = [
        # NVIDIA
        os.path.expandvars(r"%LOCALAPPDATA%\NVIDIA\NvBackend\ApplicationOntology"),
        os.path.expandvars(r"%PROGRAMDATA%\NVIDIA Corporation\Downloader"),
        r"C:\ProgramData\NVIDIA Corporation\NvTelemetry",
        # AMD
        os.path.expandvars(r"%LOCALAPPDATA%\AMD\CN"),
        r"C:\ProgramData\AMD\CN",
        # Intel
        os.path.expandvars(r"%LOCALAPPDATA%\Intel\ShaderCache"),
    ]
    for d in paths_to_clear:
        if os.path.isdir(d):
            for f in glob.glob(os.path.join(d, "**", "*.log"), recursive=True):
                try:
                    os.remove(f)
                    deleted += 1
                except Exception:
                    pass
            for f in glob.glob(os.path.join(d, "**", "*.db"), recursive=True):
                try:
                    os.remove(f)
                    deleted += 1
                except Exception:
                    pass

    # NVIDIA telemetry registry — disable future logging
    try:
        import winreg
        for nv_key in [r"SOFTWARE\NVIDIA Corporation\NvTelemetry",
                       r"SYSTEM\CurrentControlSet\Services\NvTelemetryContainer"]:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, nv_key,
                                     access=winreg.KEY_ALL_ACCESS)
                winreg.SetValueEx(key, "EnableTelemetry", 0, winreg.REG_DWORD, 0)
                winreg.CloseKey(key)
            except Exception:
                pass
    except ImportError:
        pass

    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} GPU telemetry file(s)")
    else:
        reporter.skip(CAT, artifact, "No GPU telemetry files found")


def _temp_artifacts(paths: list, reporter: StatusReporter) -> None:
    """Clear %TEMP% and %TMP% entries that could be extraction artifacts."""
    artifact = "Temp extraction artifacts"
    reporter.running(CAT, artifact)
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    stems = {os.path.splitext(b)[0] for b in basenames_lower}
    temp_dirs = list({os.path.expandvars("%TEMP%"), os.path.expandvars("%TMP%"),
                      r"C:\Windows\Temp"})
    deleted = 0
    for d in temp_dirs:
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            el = entry.lower()
            if any(b in el for b in basenames_lower) or any(s in el for s in stems):
                fpath = os.path.join(d, entry)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                    elif os.path.isdir(fpath):
                        import shutil
                        shutil.rmtree(fpath, ignore_errors=True)
                    deleted += 1
                except Exception:
                    pass
    if deleted:
        reporter.ok(CAT, artifact, f"Removed {deleted} temp item(s)")
    else:
        reporter.skip(CAT, artifact, "No matching temp artifacts found")


def _etw_logs(reporter: StatusReporter) -> None:
    """Clear ETW kernel-file circular log buffers (WDI diagnostic logs)."""
    artifact = "ETW kernel diagnostic logs (WDI/PerfLogs)"
    reporter.running(CAT, artifact)
    etw_dirs = [
        r"C:\Windows\System32\WDI\LogFiles",
        r"C:\Windows\System32\LogFiles\WMI",
        r"C:\Windows\Logs\WMI",
        r"C:\PerfLogs",
    ]
    deleted = 0
    for d in etw_dirs:
        if os.path.isdir(d):
            for f in glob.glob(os.path.join(d, "**", "*.etl"), recursive=True):
                try:
                    os.remove(f)
                    deleted += 1
                except Exception:
                    pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} ETW log file(s)")
    else:
        reporter.skip(CAT, artifact, "No ETW log files found")


def _text_log_artifacts(paths: list, reporter: StatusReporter) -> None:
    """
    Scan Windows text-based log files for dropped path strings.
    Process Hacker and similar tools can find path references in CBS.log,
    DISM.log, debug logs, and AppX packaging logs even after the file is wiped.
    Lines containing target path are stripped; files are not deleted wholesale.
    """
    artifact = "Dropped strings in system text logs"
    reporter.running(CAT, artifact)
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}

    def _matches_line(line: str) -> bool:
        ll = line.lower()
        return any(b in ll for b in basenames_lower) or any(p in ll for p in path_set_lower)

    log_files = [
        r"C:\Windows\Logs\CBS\CBS.log",
        r"C:\Windows\Logs\DISM\dism.log",
        r"C:\Windows\Logs\AppxPackaging\OLE.log",
        r"C:\Windows\debug\NetSetup.LOG",
        r"C:\Windows\debug\PASSWD.LOG",
        r"C:\Windows\debug\wia\wiatrace.log",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\WER\ERC\*.log"),
    ]
    # Also scan C:\Windows\debug\*.log and C:\Windows\Logs\AppxPackaging\*.log
    for pattern in [
        r"C:\Windows\debug\*.log",
        r"C:\Windows\Logs\AppxPackaging\*.log",
        r"C:\Windows\Logs\*.log",
    ]:
        for f in glob.glob(pattern):
            if f not in log_files:
                log_files.append(f)

    cleaned = 0
    for log_path in log_files:
        # Handle glob patterns in the list
        expanded = glob.glob(log_path) if "*" in log_path else [log_path]
        for fpath in expanded:
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                before = len(lines)
                filtered = [l for l in lines if not _matches_line(l)]
                if len(filtered) < before:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.writelines(filtered)
                    cleaned += before - len(filtered)
            except Exception:
                pass

    if cleaned:
        reporter.ok(CAT, artifact, f"Stripped {cleaned} line(s) from system text logs")
    else:
        reporter.skip(CAT, artifact, "No path references found in system text logs")


def _dangling_registry_refs(paths: list, reporter: StatusReporter) -> None:
    """
    Verify no registry artifacts point to our (now-deleted) files.
    Dangling references (registry entry for a non-existent file) are a major
    screenshare red flag — Red Lotus and StormSS check for these explicitly.
    """
    artifact = "Dangling registry reference check"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        dangling = []

        keys_to_check = [
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"),
        ]

        def _scan_key_for_refs(hive, path):
            try:
                key = winreg.OpenKey(hive, path, access=winreg.KEY_READ)
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        val = (name + str(data)).lower()
                        if any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower):
                            dangling.append(f"{path}\\{name}")
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except Exception:
                pass

        for hive, path in keys_to_check:
            _scan_key_for_refs(hive, path)

        if dangling:
            reporter.warn(CAT, artifact,
                          f"ALERT: {len(dangling)} dangling reference(s) still found — "
                          f"re-run shell_history and os_logs categories. Keys: "
                          + "; ".join(dangling[:5]))
        else:
            reporter.ok(CAT, artifact, "No dangling registry references found")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


class AppArtifactsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["Microsoft Office MRU", "File extension handlers (OpenWithList)",
                         "Archive tool history (7-Zip / WinRAR)", "Cortana / Windows Search cache",
                         "Windows Defender scan history", "Reliability Monitor history",
                         "SmartScreen telemetry", "Clipboard history", "GPU driver telemetry",
                         "Temp extraction artifacts", "ETW kernel diagnostic logs (WDI/PerfLogs)",
                         "Dropped strings in system text logs", "Dangling registry reference check"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _office_mru(paths, reporter)
        _file_ext_handlers(paths, reporter)
        _archive_tools(paths, reporter)
        _cortana_search(reporter)
        _defender_history(paths, reporter)
        _reliability_monitor(reporter)
        _smartscreen_telemetry(reporter)
        _clipboard_history(reporter)
        _gpu_telemetry(reporter)
        _temp_artifacts(paths, reporter)
        _etw_logs(reporter)
        _text_log_artifacts(paths, reporter)
        _dangling_registry_refs(paths, reporter)  # always last — verification pass
