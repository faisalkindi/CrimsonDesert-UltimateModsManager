"""PyInstaller build script for Crimson Desert Ultimate Mods Manager."""
import subprocess
import sys
from pathlib import Path


def build() -> None:
    project_root = Path(__file__).resolve().parent.parent
    spec_file = project_root / "cdumm.spec"

    if not spec_file.exists():
        print(f"Spec file not found: {spec_file}")
        sys.exit(1)

    cmd = [sys.executable, "-m", "PyInstaller", str(spec_file), "--clean"]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(project_root))
    sys.exit(result.returncode)


if __name__ == "__main__":
    build()
