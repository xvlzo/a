import os
import glob
import codecs
import sqlite3
import subprocess
import platform
import time
from .base import BaseCleaner, StatusReporter

CAT = "shell_history"


def _run(cmd: list, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _lnk_files(paths: list, reporter: StatusReporter) -> None:
    artifact = "LNK files (Recent)"
    reporter.running(CAT, artifact)
    recent_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Recent")
    if not os.path.isdir(recent_dir):
        reporter.skip(CAT, artifact, "Recent directory not found")
        return
    targets_utf16 = [p.encode("utf-16-le") for p in paths]
    basenames_utf16 = [os.path.basename(p).encode("utf-16-le") for p in paths]
    deleted = 0
    for lnk in glob.glob(os.path.join(recent_dir, "*.lnk")):
        try:
            with open(lnk, "rb") as f:
                data = f.read()
            if any(t in data for t in targets_utf16) or any(b in data for b in basenames_utf16):
                os.remove(lnk)
                deleted += 1
        except Exception:
            pass
    reporter.ok(CAT, artifact, f"Removed {deleted} LNK file(s)")


def _jump_lists(paths: list, reporter: StatusReporter) -> None:
    artifact = "Jump Lists"
    reporter.running(CAT, artifact)
    auto_dest = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Recent\AutomaticDestinations")
    cust_dest = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Recent\CustomDestinations")
    targets_utf16 = [p.encode("utf-16-le") for p in paths]
    basenames_utf16 = [os.path.basename(p).encode("utf-16-le") for p in paths]
    deleted = 0
    for pattern_dir, ext in [(auto_dest, "*.automaticDestinations-ms"), (cust_dest, "*.customDestinations-ms")]:
        if not os.path.isdir(pattern_dir):
            continue
        for jl in glob.glob(os.path.join(pattern_dir, ext)):
            try:
                with open(jl, "rb") as f:
                    data = f.read()
                if any(t in data for t in targets_utf16) or any(b in data for b in basenames_utf16):
                    os.remove(jl)
                    deleted += 1
            except Exception:
                pass
    reporter.ok(CAT, artifact, f"Removed {deleted} Jump List file(s)")


def _userassist(paths: list, reporter: StatusReporter) -> None:
    artifact = "UserAssist registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        ua_base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
        deleted = 0
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ua_base) as ua:
            guids = []
            i = 0
            while True:
                try:
                    guids.append(winreg.EnumKey(ua, i))
                    i += 1
                except OSError:
                    break
        for guid in guids:
            count_path = f"{ua_base}\\{guid}\\Count"
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, count_path, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                        decoded = codecs.decode(name, "rot_13").lower()
                        if any(b in decoded for b in basenames_lower) or any(p in decoded for p in path_set_lower):
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
        reporter.ok(CAT, artifact, f"Deleted {deleted} value(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _recent_docs(paths: list, reporter: StatusReporter) -> None:
    artifact = "RecentDocs registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        exts = {os.path.splitext(p)[1].lower() for p in paths}
        base_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs"
        deleted = 0

        def _scan_key(hkey, subpath):
            nonlocal deleted
            try:
                key = winreg.OpenKey(hkey, subpath, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        if isinstance(data, bytes):
                            dl = data.lower()
                            if any(b.encode("utf-16-le") in dl or b.encode() in dl for b in basenames_lower):
                                to_del.append(name)
                        i += 1
                    except OSError:
                        break
                for name in to_del:
                    winreg.DeleteValue(key, name)
                    deleted += 1
                winreg.CloseKey(key)
            except Exception:
                pass

        _scan_key(winreg.HKEY_CURRENT_USER, base_key)
        for ext in exts:
            if ext:
                _scan_key(winreg.HKEY_CURRENT_USER, base_key + "\\" + ext)
        reporter.ok(CAT, artifact, f"Deleted {deleted} value(s)")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _registry_mru(paths: list, reporter: StatusReporter) -> None:
    """MuiCache, TypedPaths, WordWheelQuery, RunMRU, OpenSavePidlMRU, LastVisitedPidlMRU."""
    try:
        import winreg
    except ImportError:
        for name in ["MuiCache", "TypedPaths", "WordWheelQuery", "RunMRU", "OpenSavePidlMRU", "LastVisitedPidlMRU"]:
            reporter.skip(CAT, name, "winreg not available (non-Windows)")
        return

    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}

    def _delete_matching(hive, key_path, artifact_name, match_fn):
        reporter.running(CAT, artifact_name)
        try:
            key = winreg.OpenKey(hive, key_path, access=winreg.KEY_ALL_ACCESS)
            to_del = []
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                    val = (str(data) + name).lower()
                    if match_fn(val):
                        to_del.append(name)
                    i += 1
                except OSError:
                    break
            for name in to_del:
                winreg.DeleteValue(key, name)
            winreg.CloseKey(key)
            reporter.ok(CAT, artifact_name, f"Deleted {len(to_del)} value(s)")
        except FileNotFoundError:
            reporter.skip(CAT, artifact_name, "Key not found")
        except Exception as e:
            reporter.warn(CAT, artifact_name, str(e))

    def _matches(val):
        return any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower)

    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache",
        "MuiCache",
        _matches,
    )
    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\TypedPaths",
        "TypedPaths",
        _matches,
    )
    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\WordWheelQuery",
        "WordWheelQuery",
        _matches,
    )
    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU",
        "RunMRU",
        _matches,
    )

    # OpenSavePidlMRU — binary pidl data
    reporter.running(CAT, "OpenSavePidlMRU")
    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\OpenSavePidlMRU",
        "OpenSavePidlMRU",
        lambda val: any(b in val for b in basenames_lower),
    )

    # LastVisitedPidlMRU
    _delete_matching(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\LastVisitedPidlMRU",
        "LastVisitedPidlMRU",
        _matches,
    )


def _ps_history(paths: list, reporter: StatusReporter) -> None:
    artifact = "PowerShell history"
    reporter.running(CAT, artifact)
    ps_hist = os.path.expandvars(
        r"%APPDATA%\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    )
    if not os.path.exists(ps_hist):
        reporter.skip(CAT, artifact, "No PS history file found")
        return
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}
    try:
        with open(ps_hist, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        before = len(lines)
        lines = [l for l in lines if not (
            any(b in l.lower() for b in basenames_lower) or
            any(p in l.lower() for p in path_set_lower)
        )]
        with open(ps_hist, "w", encoding="utf-8") as f:
            f.writelines(lines)
        reporter.ok(CAT, artifact, f"Removed {before - len(lines)} line(s)")
    except Exception as e:
        reporter.error(CAT, artifact, str(e))


def _thumbcache(reporter: StatusReporter) -> None:
    artifact = "Thumbnail cache"
    reporter.running(CAT, artifact)
    thumb_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer")
    if not os.path.isdir(thumb_dir):
        reporter.skip(CAT, artifact, "Explorer cache dir not found")
        return
    _run(["taskkill", "/F", "/IM", "explorer.exe"])
    time.sleep(1)
    deleted = 0
    errors = []
    for db in glob.glob(os.path.join(thumb_dir, "thumbcache_*.db")):
        try:
            os.remove(db)
            deleted += 1
        except Exception as e:
            errors.append(str(e))
    subprocess.Popen(["explorer.exe"])
    if errors:
        reporter.warn(CAT, artifact, f"Deleted {deleted}, failed: {'; '.join(errors)}")
    else:
        reporter.ok(CAT, artifact, f"Deleted {deleted} cache file(s); Explorer restarted")


def _search_index(reporter: StatusReporter) -> None:
    artifact = "Windows Search index"
    reporter.running(CAT, artifact)
    _run(["sc", "stop", "WSearch"])
    time.sleep(2)
    index_dir = r"C:\ProgramData\Microsoft\Search\Data\Applications\Windows"
    deleted = 0
    for f in glob.glob(os.path.join(index_dir, "Windows.edb")):
        try:
            os.remove(f)
            deleted += 1
        except Exception:
            pass
    _run(["sc", "start", "WSearch"])
    if deleted:
        reporter.ok(CAT, artifact, "Search index deleted; will be rebuilt by WSearch")
    else:
        reporter.skip(CAT, artifact, "Index file not found or already cleared")


def _recycle_bin(paths: list, reporter: StatusReporter) -> None:
    artifact = "Recycle Bin ($I/$R)"
    reporter.running(CAT, artifact)
    targets_utf16 = [p.encode("utf-16-le") for p in paths]
    basenames_utf16 = [os.path.basename(p).encode("utf-16-le") for p in paths]
    deleted = 0
    drives = {os.path.splitdrive(p)[0].upper() + "\\" for p in paths if os.path.splitdrive(p)[0]}
    if not drives:
        drives = {"C:\\"}
    for drive in drives:
        rb = os.path.join(drive, "$Recycle.Bin")
        if not os.path.isdir(rb):
            continue
        for root, dirs, files in os.walk(rb):
            for fname in files:
                if not fname.upper().startswith("$I"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                    if any(t in data for t in targets_utf16) or any(b in data for b in basenames_utf16):
                        r_name = "$R" + fname[2:]
                        r_path = os.path.join(root, r_name)
                        os.remove(fpath)
                        if os.path.exists(r_path):
                            os.remove(r_path)
                        deleted += 1
                except Exception:
                    pass
    reporter.ok(CAT, artifact, f"Removed {deleted} Recycle Bin entry(s)")


def _notification_db(paths: list, reporter: StatusReporter) -> None:
    artifact = "Notification database"
    reporter.running(CAT, artifact)
    notif_db = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")
    if not os.path.exists(notif_db):
        reporter.skip(CAT, artifact, "Notification DB not found")
        return
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}
    deleted = 0
    try:
        conn = sqlite3.connect(notif_db)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in c.fetchall()]
        for table in tables:
            try:
                c.execute(f"PRAGMA table_info({table})")
                cols = [row[1] for row in c.fetchall()]
                for col in cols:
                    for b in basenames_lower:
                        try:
                            c.execute(f"DELETE FROM {table} WHERE lower({col}) LIKE ?", (f"%{b}%",))
                            deleted += c.rowcount
                        except Exception:
                            pass
            except Exception:
                pass
        conn.commit()
        conn.close()
        reporter.ok(CAT, artifact, f"Deleted {deleted} row(s)")
    except Exception as e:
        reporter.warn(CAT, artifact, f"DB may be locked: {e}")


def _bits(reporter: StatusReporter) -> None:
    artifact = "BITS job history"
    reporter.running(CAT, artifact)
    rc, _, err = _run(["bitsadmin", "/reset", "/allusers"])
    if rc == 0:
        reporter.ok(CAT, artifact)
    else:
        _run(["bitsadmin", "/reset"])
        reporter.ok(CAT, artifact, "Reset current user BITS jobs")


def _activity_history(reporter: StatusReporter) -> None:
    artifact = "Windows Timeline / Activity History"
    reporter.running(CAT, artifact)
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\ConnectedDevicesPlatform\*\ActivitiesCache.db")
    deleted = 0
    for db in glob.glob(pattern):
        try:
            os.remove(db)
            deleted += 1
        except Exception:
            pass
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\System",
            access=winreg.KEY_ALL_ACCESS,
        )
        winreg.SetValueEx(key, "EnableActivityFeed", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
    except Exception:
        pass
    if deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} ActivitiesCache.db file(s); feed disabled")
    else:
        reporter.skip(CAT, artifact, "No ActivitiesCache.db found; feed disabled via policy")


class ShellHistoryCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["LNK files (Recent)", "Jump Lists", "UserAssist registry", "RecentDocs registry",
                         "MuiCache", "TypedPaths", "WordWheelQuery", "RunMRU", "OpenSavePidlMRU",
                         "LastVisitedPidlMRU", "PowerShell history", "Thumbnail cache",
                         "Windows Search index", "Recycle Bin ($I/$R)", "Notification database",
                         "BITS job history", "Windows Timeline / Activity History"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _lnk_files(paths, reporter)
        _jump_lists(paths, reporter)
        _userassist(paths, reporter)
        _recent_docs(paths, reporter)
        _registry_mru(paths, reporter)
        _ps_history(paths, reporter)
        _thumbcache(reporter)
        _search_index(reporter)
        _recycle_bin(paths, reporter)
        _notification_db(paths, reporter)
        _bits(reporter)
        _activity_history(reporter)
