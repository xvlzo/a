from .secure_wipe import SecureWipeCleaner
from .filesystem import FilesystemCleaner
from .os_logs import OsLogsCleaner
from .shell_history import ShellHistoryCleaner
from .network_artifacts import NetworkArtifactsCleaner
from .app_artifacts import AppArtifactsCleaner
from .memory_artifacts import MemoryArtifactsCleaner

CLEANERS = {
    "secure_wipe":       SecureWipeCleaner(),
    "filesystem":        FilesystemCleaner(),
    "os_logs":           OsLogsCleaner(),
    "shell_history":     ShellHistoryCleaner(),
    "network_artifacts": NetworkArtifactsCleaner(),
    "app_artifacts":     AppArtifactsCleaner(),
    "memory_artifacts":  MemoryArtifactsCleaner(),
}
