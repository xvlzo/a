import os
import glob
import sqlite3
import subprocess
import platform
import shutil
import time
from .base import BaseCleaner, StatusReporter

CAT = "browser_history"


def _kill_process(name: str) -> bool:
    try:
        r = subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _is_running(name: str) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                           capture_output=True, text=True, timeout=10)
        return name.lower() in r.stdout.lower()
    except Exception:
        return False


def _edit_sqlite(db_path: str, deletions: list, reporter: StatusReporter, artifact: str) -> None:
    """deletions: list of (table, where_col, value)"""
    if not os.path.exists(db_path):
        reporter.skip(CAT, artifact, "Database not found")
        return
    # Work on a copy to avoid lock issues, then swap back
    tmp = db_path + ".artclean_tmp"
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()
        deleted_total = 0
        for table, col, val in deletions:
            try:
                c.execute(f"DELETE FROM {table} WHERE lower({col}) LIKE ?", (f"%{val.lower()}%",))
                deleted_total += c.rowcount
            except Exception:
                pass
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
        shutil.move(tmp, db_path)
        reporter.ok(CAT, artifact, f"Deleted {deleted_total} row(s)")
    except Exception as e:
        reporter.warn(CAT, artifact, f"DB edit failed: {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _chrome_edge(paths: list, reporter: StatusReporter) -> None:
    browsers = [
        ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data", "chrome.exe"),
        ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data", "msedge.exe"),
        ("Chrome (Beta)", r"%LOCALAPPDATA%\Google\Chrome Beta\User Data", "chrome.exe"),
        ("Brave", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data", "brave.exe"),
    ]
    basenames_lower = [os.path.basename(p).lower() for p in paths]

    for name, profile_base, proc_name in browsers:
        artifact = f"{name} download history"
        reporter.running(CAT, artifact)
        base = os.path.expandvars(profile_base)
        if not os.path.isdir(base):
            reporter.skip(CAT, artifact, "Not installed")
            continue

        if _is_running(proc_name):
            reporter.warn(CAT, artifact, f"{proc_name} is running; close it for reliable cleaning. Attempting anyway.")

        # Find all profile History files
        history_files = glob.glob(os.path.join(base, "**", "History"), recursive=True)
        history_files += glob.glob(os.path.join(base, "Default", "History"))

        total_deleted = 0
        for hist in set(history_files):
            if not os.path.exists(hist):
                continue
            tmp = hist + ".artclean_tmp"
            try:
                shutil.copy2(hist, tmp)
                conn = sqlite3.connect(tmp)
                c = conn.cursor()
                for basename in basenames_lower:
                    c.execute("DELETE FROM downloads WHERE lower(target_path) LIKE ?", (f"%{basename}%",))
                    total_deleted += c.rowcount
                    # Clean chain table
                    c.execute("""DELETE FROM downloads_url_chains WHERE id NOT IN
                                 (SELECT id FROM downloads)""")
                conn.commit()
                conn.execute("VACUUM")
                conn.close()
                shutil.move(tmp, hist)
            except Exception as e:
                reporter.warn(CAT, artifact, f"Could not edit {os.path.basename(hist)}: {e}")
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

        if total_deleted > 0:
            reporter.ok(CAT, artifact, f"Deleted {total_deleted} download record(s)")
        else:
            reporter.skip(CAT, artifact, "No matching download records found")


def _firefox(paths: list, reporter: StatusReporter) -> None:
    artifact = "Firefox download history"
    reporter.running(CAT, artifact)
    profiles_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(profiles_dir):
        reporter.skip(CAT, artifact, "Firefox not installed")
        return

    if _is_running("firefox.exe"):
        reporter.warn(CAT, artifact, "firefox.exe is running; close it for reliable cleaning. Attempting anyway.")

    basenames_lower = [os.path.basename(p).lower() for p in paths]
    total_deleted = 0

    for profile in os.listdir(profiles_dir):
        places_db = os.path.join(profiles_dir, profile, "places.sqlite")
        if not os.path.exists(places_db):
            continue
        tmp = places_db + ".artclean_tmp"
        try:
            shutil.copy2(places_db, tmp)
            conn = sqlite3.connect(tmp)
            c = conn.cursor()
            for basename in basenames_lower:
                # moz_annos stores download metadata
                c.execute("""DELETE FROM moz_annos WHERE place_id IN (
                               SELECT id FROM moz_places WHERE lower(url) LIKE ?
                             )""", (f"%{basename}%",))
                total_deleted += c.rowcount
                c.execute("DELETE FROM moz_places WHERE lower(url) LIKE ?", (f"%{basename}%",))
                total_deleted += c.rowcount
                # Also check title column
                c.execute("DELETE FROM moz_places WHERE lower(title) LIKE ?", (f"%{basename}%",))
                total_deleted += c.rowcount
            conn.commit()
            conn.execute("VACUUM")
            conn.close()
            shutil.move(tmp, places_db)
        except Exception as e:
            reporter.warn(CAT, artifact, f"Could not edit {profile}: {e}")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    if total_deleted > 0:
        reporter.ok(CAT, artifact, f"Deleted {total_deleted} record(s) across Firefox profiles")
    else:
        reporter.skip(CAT, artifact, "No matching Firefox download records found")


class BrowserHistoryCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        if platform.system() != "Windows":
            for name in ["Chrome download history", "Edge download history", "Firefox download history"]:
                reporter.skip(CAT, name, "Windows only")
            return
        _chrome_edge(paths, reporter)
        _firefox(paths, reporter)
