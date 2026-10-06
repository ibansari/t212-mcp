"""Install/remove a macOS launchd job that runs the look-through refresh daily."""

import plistlib
import shutil
import subprocess
from pathlib import Path

from ..config import PROJECT_ROOT, Settings

LABEL = "com.t212mcp.refresh"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(settings: Settings, hour: int = 7, minute: int = 30) -> str:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found on PATH")
    log = settings.data_dir / "refresh.log"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [uv, "--directory", str(PROJECT_ROOT), "run", "t212-mcp", "refresh-holdings"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}", str(PLIST)], capture_output=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(PLIST)], check=True)
    return f"Installed {LABEL}: daily at {hour:02d}:{minute:02d}, logging to {log}"


def uninstall() -> str:
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    return f"Removed {LABEL}"


def _uid() -> int:
    import os

    return os.getuid()
