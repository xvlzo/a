import os
import subprocess
import platform
from .base import BaseCleaner, StatusReporter, Status

CAT = "secure_wipe"


def _strip_ads(path: str, reporter: StatusReporter) -> None:
    """Remove all alternate data streams from a file before wiping."""
    artifact = f"Strip ADS: {os.path.basename(path)}"
    reporter.running(CAT, artifact)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'Get-Item -LiteralPath "{path}" -Stream * | '
             f'Where-Object {{$_.Stream -ne ":$DATA"}} | '
             f'ForEach-Object {{Remove-Item -LiteralPath "{path}" -Stream $_.Stream -Force}}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            reporter.ok(CAT, artifact)
        else:
            reporter.warn(CAT, artifact, result.stderr.strip() or "ADS removal may have partially failed")
    except Exception as e:
        reporter.warn(CAT, artifact, str(e))


def _wipe_file(path: str, reporter: StatusReporter) -> None:
    artifact = f"3-pass wipe: {os.path.basename(path)}"
    reporter.running(CAT, artifact)
    try:
        if not os.path.exists(path):
            reporter.skip(CAT, artifact, "File not found")
            return

        # Clear read-only attribute
        try:
            attrs = os.stat(path).st_mode
            os.chmod(path, attrs | 0o200)
        except Exception:
            pass

        size = os.path.getsize(path)
        if size == 0:
            os.remove(path)
            reporter.ok(CAT, artifact, "Empty file deleted")
            return

        chunk = 65536
        patterns = [b'\x00', None, b'\x00']  # None = random
        with open(path, "r+b") as f:
            for pattern in patterns:
                f.seek(0)
                written = 0
                while written < size:
                    n = min(chunk, size - written)
                    data = os.urandom(n) if pattern is None else pattern * n
                    f.write(data)
                    written += n
                f.flush()
                os.fsync(f.fileno())

        os.remove(path)
        reporter.ok(CAT, artifact)
    except PermissionError as e:
        reporter.error(CAT, artifact, f"Permission denied: {e}")
    except Exception as e:
        reporter.error(CAT, artifact, str(e))


class SecureWipeCleaner(BaseCleaner):
    CATEGORY = CAT

    def run(self, paths: list, reporter: StatusReporter) -> None:
        is_windows = platform.system() == "Windows"
        for path in paths:
            if is_windows:
                _strip_ads(path, reporter)
            _wipe_file(path, reporter)
