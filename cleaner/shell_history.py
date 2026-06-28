import os
import glob
import codecs
import sqlite3
import subprocess
import platform
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
    # Thumbnail DBs are monolithic blobs — surgical removal without killing Explorer
    # is not possible. Skipping to avoid system disruption on a live desktop.
    reporter.skip(CAT, artifact, "Skipped: safe surgical removal requires Explorer restart; run manually if needed")


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


def _bits(paths: list, reporter: StatusReporter) -> None:
    artifact = "BITS job history"
    reporter.running(CAT, artifact)
    basenames_lower = {os.path.basename(p).lower() for p in paths}
    path_set_lower = {p.lower() for p in paths}
    # Enumerate BITS jobs via PowerShell and cancel only those referencing target paths
    ps_cmd = (
        "Get-BitsTransfer -AllUsers 2>$null | "
        "ForEach-Object { $j=$_; $j.FileList | ForEach-Object { "
        "  [PSCustomObject]@{Id=$j.JobId;Remote=$_.RemoteName;Local=$_.LocalName} "
        "}} | ConvertTo-Json -Depth 2"
    )
    rc, out, _ = _run(["powershell", "-NoProfile", "-Command", ps_cmd], timeout=20)
    cancelled = 0
    if rc == 0 and out.strip():
        import json as _json
        try:
            jobs = _json.loads(out)
            if isinstance(jobs, dict):
                jobs = [jobs]
            seen_ids = set()
            for entry in jobs:
                local = str(entry.get("Local", "")).lower()
                remote = str(entry.get("Remote", "")).lower()
                combined = local + remote
                if any(b in combined for b in basenames_lower) or any(p in combined for p in path_set_lower):
                    job_id = str(entry.get("Id", ""))
                    if job_id and job_id not in seen_ids:
                        seen_ids.add(job_id)
                        _run(["bitsadmin", "/cancel", job_id])
                        cancelled += 1
        except Exception:
            pass
    if cancelled:
        reporter.ok(CAT, artifact, f"Cancelled {cancelled} BITS job(s) referencing target path(s)")
    else:
        reporter.skip(CAT, artifact, "No BITS jobs referencing target path(s) found")


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


def _shell_bags(paths: list, reporter: StatusReporter) -> None:
    """
    Shell Bags record every folder ever opened in Windows Explorer, including the
    parent directory of our target file. Autopsy, FTK, and AXIOM all specifically
    check ShellBags — one of the top forensic artifacts for proving folder access.
    """
    artifact = "Shell Bags (BagMRU/Bags)"
    reporter.running(CAT, artifact)
    try:
        import winreg
        parent_dirs_lower = {os.path.dirname(p).lower() for p in paths}
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        bag_roots = [
            (winreg.HKEY_CURRENT_USER,
             r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\Shell\BagMRU"),
        ]
        deleted = 0

        def _scan(hive, path):
            nonlocal deleted
            try:
                key = winreg.OpenKey(hive, path, access=winreg.KEY_ALL_ACCESS)
                to_del_vals = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        if isinstance(data, bytes):
                            try:
                                decoded = data.decode("utf-16-le", errors="ignore").lower()
                            except Exception:
                                decoded = ""
                            raw = data.decode("latin-1", errors="ignore").lower()
                            combined = decoded + raw
                            if (any(p in combined for p in parent_dirs_lower) or
                                    any(b in combined for b in basenames_lower)):
                                to_del_vals.append(name)
                        i += 1
                    except OSError:
                        break
                subkeys = []
                j = 0
                while True:
                    try:
                        subkeys.append(winreg.EnumKey(key, j))
                        j += 1
                    except OSError:
                        break
                for name in to_del_vals:
                    try:
                        winreg.DeleteValue(key, name)
                        deleted += 1
                    except Exception:
                        pass
                winreg.CloseKey(key)
                for sk in subkeys:
                    _scan(hive, path + "\\" + sk)
            except (FileNotFoundError, Exception):
                pass

        for hive, path in bag_roots:
            _scan(hive, path)
        if deleted:
            reporter.ok(CAT, artifact, f"Deleted {deleted} ShellBag entry(s)")
        else:
            reporter.skip(CAT, artifact, "No matching ShellBag entries found")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _icon_cache(reporter: StatusReporter) -> None:
    """
    Icon cache databases store file preview images. iconcache_*.db files can
    often be removed without killing Explorer (rebuilt on next access).
    """
    artifact = "Icon cache (iconcache_*.db)"
    reporter.running(CAT, artifact)
    cache_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer")
    deleted = 0
    errors = []
    for pattern in ["iconcache_*.db", "thumbcache_sr.db"]:
        for db in glob.glob(os.path.join(cache_dir, pattern)):
            try:
                os.remove(db)
                deleted += 1
            except Exception:
                errors.append(os.path.basename(db))
    legacy = os.path.expandvars(r"%LOCALAPPDATA%\IconCache.db")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
            deleted += 1
        except Exception:
            errors.append("IconCache.db")
    if errors:
        reporter.warn(CAT, artifact,
                      f"Deleted {deleted}; locked (restart Explorer to clear): {', '.join(errors[:3])}")
    elif deleted:
        reporter.ok(CAT, artifact, f"Deleted {deleted} icon cache file(s)")
    else:
        reporter.skip(CAT, artifact, "No icon cache files found")


def _capability_access(paths: list, reporter: StatusReporter) -> None:
    """
    CapabilityAccessManager records which apps accessed resources via file pickers.
    If the target file was opened through a dialog, this records the accessing app.
    """
    artifact = "CapabilityAccessManager registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        cam_base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
        deleted = 0

        def _scan(hive, path):
            nonlocal deleted
            try:
                key = winreg.OpenKey(hive, path, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        val = (str(data) + name).lower()
                        if any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower):
                            to_del.append(name)
                        i += 1
                    except OSError:
                        break
                subkeys = []
                j = 0
                while True:
                    try:
                        subkeys.append(winreg.EnumKey(key, j))
                        j += 1
                    except OSError:
                        break
                for name in to_del:
                    try:
                        winreg.DeleteValue(key, name)
                        deleted += 1
                    except Exception:
                        pass
                winreg.CloseKey(key)
                for sk in subkeys:
                    _scan(hive, path + "\\" + sk)
            except (FileNotFoundError, Exception):
                pass

        _scan(winreg.HKEY_CURRENT_USER, cam_base)
        if deleted:
            reporter.ok(CAT, artifact, f"Deleted {deleted} entry(s)")
        else:
            reporter.skip(CAT, artifact, "No CapabilityAccessManager references found")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


def _feature_usage(paths: list, reporter: StatusReporter) -> None:
    """
    FeatureUsage and AppHost keys track app launches and file access patterns.
    Checked by forensic tools and some PC checker suites for execution evidence.
    """
    artifact = "FeatureUsage / AppHost registry"
    reporter.running(CAT, artifact)
    try:
        import winreg
        basenames_lower = {os.path.basename(p).lower() for p in paths}
        path_set_lower = {p.lower() for p in paths}
        keys_to_scan = [
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Search\FeatureUsage"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\AppHost\Launched"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\AppHost\AppPath"),
        ]
        deleted = 0
        for hive, path in keys_to_scan:
            try:
                key = winreg.OpenKey(hive, path, access=winreg.KEY_ALL_ACCESS)
                to_del = []
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(key, i)
                        val = (str(data) + name).lower()
                        if any(b in val for b in basenames_lower) or any(p in val for p in path_set_lower):
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
        if deleted:
            reporter.ok(CAT, artifact, f"Deleted {deleted} entry(s)")
        else:
            reporter.skip(CAT, artifact, "No FeatureUsage/AppHost references found")
    except ImportError:
        reporter.skip(CAT, artifact, "winreg not available (non-Windows)")


class ShellHistoryCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["LNK files (Recent)", "Jump Lists", "UserAssist registry", "RecentDocs registry",
                         "MuiCache", "TypedPaths", "WordWheelQuery", "RunMRU", "OpenSavePidlMRU",
                         "LastVisitedPidlMRU", "PowerShell history", "Thumbnail cache",
                         "Windows Search index", "Recycle Bin ($I/$R)", "Notification database",
                         "BITS job history", "Windows Timeline / Activity History",
                         "Shell Bags (BagMRU/Bags)", "Icon cache (iconcache_*.db)",
                         "CapabilityAccessManager registry", "FeatureUsage / AppHost registry"]:
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
        _bits(paths, reporter)
        _activity_history(reporter)
        _shell_bags(paths, reporter)
        _icon_cache(reporter)
        _capability_access(paths, reporter)
        _feature_usage(paths, reporter)
