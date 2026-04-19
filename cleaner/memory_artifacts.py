import os
import glob
import subprocess
import platform
from .base import BaseCleaner, StatusReporter

CAT = "memory_artifacts"


def _run(cmd: list, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _hibernation(reporter: StatusReporter) -> None:
    artifact = "Hibernation file (hiberfil.sys)"
    reporter.running(CAT, artifact)
    rc, _, err = _run(["powercfg", "/hibernate", "off"])
    hiberfil = r"C:\hiberfil.sys"
    if rc == 0:
        if not os.path.exists(hiberfil):
            reporter.ok(CAT, artifact, "Hibernation disabled; hiberfil.sys deleted")
        else:
            reporter.warn(CAT, artifact, "powercfg succeeded but hiberfil.sys still present; may need reboot")
    else:
        reporter.warn(CAT, artifact, err or "Requires elevation")


def _pagefile(reporter: StatusReporter) -> None:
    artifact = "Page file (ClearPageFileAtShutdown)"
    reporter.running(CAT, artifact)
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
            access=winreg.KEY_ALL_ACCESS,
        )
        winreg.SetValueEx(key, "ClearPageFileAtShutdown", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        reporter.warn(CAT, artifact, "Pagefile will be zeroed on next shutdown — takes effect after reboot")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")
    except Exception as e:
        reporter.warn(CAT, artifact, str(e))


def _crash_dumps(reporter: StatusReporter) -> None:
    artifact = "Crash dumps / minidumps"
    reporter.running(CAT, artifact)
    dump_paths = [
        r"C:\Windows\Minidump\*.dmp",
        r"C:\Windows\MEMORY.DMP",
        os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps\*"),
        r"C:\Windows\LiveKernelReports\*.dmp",
        r"C:\Windows\LiveKernelReports\*.cab",
    ]
    deleted = 0
    for pattern in dump_paths:
        for f in glob.glob(pattern):
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    deleted += 1
            except Exception:
                pass

    # Disable future crash dumps
    try:
        import winreg
        # Disable WER dumps
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\Windows Error Reporting",
            access=winreg.KEY_ALL_ACCESS,
        )
        winreg.SetValueEx(key, "Disabled", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        # Set kernel dump type to none
        key2 = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\CrashControl",
            access=winreg.KEY_ALL_ACCESS,
        )
        winreg.SetValueEx(key2, "CrashDumpEnabled", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key2)
    except Exception:
        pass

    reporter.ok(CAT, artifact, f"Deleted {deleted} dump file(s); future dumps disabled")


def _wer_dumps(paths: list, reporter: StatusReporter) -> None:
    artifact = "WER memory dumps (.hdmp/.mdmp)"
    reporter.running(CAT, artifact)
    wer_dirs = [
        r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive",
        r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\WER"),
    ]
    deleted = 0
    for wer_dir in wer_dirs:
        for ext in ("*.hdmp", "*.mdmp", "*.dmp"):
            for f in glob.glob(os.path.join(wer_dir, "**", ext), recursive=True):
                try:
                    os.remove(f)
                    deleted += 1
                except Exception:
                    pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} WER memory dump(s)")
    else:
        reporter.skip(CAT, artifact, "No WER memory dump files found")


class MemoryArtifactsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["Hibernation file (hiberfil.sys)", "Page file (ClearPageFileAtShutdown)",
                         "Crash dumps / minidumps", "WER memory dumps (.hdmp/.mdmp)"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _hibernation(reporter)
        _pagefile(reporter)
        _crash_dumps(reporter)
        _wer_dumps(paths, reporter)
