import os
import glob
import subprocess
import platform
from .base import BaseCleaner, StatusReporter

CAT = "network_artifacts"


def _run(cmd: list, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _dns_cache(reporter: StatusReporter) -> None:
    artifact = "DNS cache"
    reporter.running(CAT, artifact)
    rc, _, err = _run(["ipconfig", "/flushdns"])
    if rc == 0:
        reporter.ok(CAT, artifact)
    else:
        reporter.warn(CAT, artifact, err or "flush failed")


def _firewall_logs(reporter: StatusReporter) -> None:
    artifact = "Windows Firewall logs"
    reporter.running(CAT, artifact)
    log_path = r"C:\Windows\System32\LogFiles\Firewall\pfirewall.log"
    deleted = 0
    if os.path.exists(log_path):
        try:
            open(log_path, "w").close()
            deleted = 1
        except Exception:
            pass
    # Disable future firewall logging
    _run(["netsh", "advfirewall", "set", "allprofiles", "logging", "filename", "none"])
    if deleted:
        reporter.ok(CAT, artifact, "Log cleared and future logging disabled")
    else:
        reporter.ok(CAT, artifact, "No existing log; future logging disabled")


def _network_registry(reporter: StatusReporter) -> None:
    artifact = "Network connection registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        deleted = 0
        keys_to_clear = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Nla\Cache"),
        ]
        for hive, path in keys_to_clear:
            try:
                key = winreg.OpenKey(hive, path, access=winreg.KEY_ALL_ACCESS)
                # Delete all subkeys recursively
                subkeys = []
                i = 0
                while True:
                    try:
                        subkeys.append(winreg.EnumKey(key, i))
                        i += 1
                    except OSError:
                        break
                for sk in subkeys:
                    try:
                        winreg.DeleteKey(key, sk)
                        deleted += 1
                    except Exception:
                        pass
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass
        reporter.ok(CAT, artifact, f"Cleared {deleted} cache entry(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _smb_audit(reporter: StatusReporter) -> None:
    artifact = "SMB/File share audit policy"
    reporter.running(CAT, artifact)
    cmds = [
        ["auditpol", "/set", "/subcategory:Detailed File Share", "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:File System", "/success:disable", "/failure:disable"],
        ["auditpol", "/set", "/subcategory:Handle Manipulation", "/success:disable", "/failure:disable"],
    ]
    failed = []
    for cmd in cmds:
        rc, _, err = _run(cmd)
        if rc != 0:
            failed.append(err or cmd[3])
    if failed:
        reporter.warn(CAT, artifact, f"Some audit policies failed (requires admin): {'; '.join(failed)}")
    else:
        reporter.ok(CAT, artifact, "Detailed file share and file system auditing disabled")


class NetworkArtifactsCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["DNS cache", "Windows Firewall logs", "Network connection registry",
                         "SMB/File share audit policy"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _dns_cache(reporter)
        _firewall_logs(reporter)
        _network_registry(reporter)
        _smb_audit(reporter)
