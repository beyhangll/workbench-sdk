"""Install the SDK bundled with an immutable Action revision in a private environment."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def install() -> int:
    if sys.version_info[:2] != (3, 12) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", os.environ.get("AQ_ACTION_REF", "")):
        print("Use Python 3.12 and pin the evaluation Action to a full reviewed commit SHA.")
        return 2
    source = Path(__file__).resolve().parents[3] / "packages" / "aq-sdk"
    environment = Path(tempfile.mkdtemp(prefix="aq-sdk-", dir=os.environ["RUNNER_TEMP"]))
    commands = ([sys.executable, "-I", "-m", "venv", str(environment)],
                [str(environment / "bin" / "python"), "-I", "-m", "pip", "--isolated", "install",
                 "--disable-pip-version-check", "--no-cache-dir", "--quiet", str(source)])
    for command in commands:
        if subprocess.run(command, capture_output=True, check=False).returncode:
            print("The bundled SDK could not be installed. No evaluation was started.")
            return 2
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
        f.write("python=" + str(environment / "bin" / "python") + "\n")
    print("Installed the SDK bundled with the pinned Action revision.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(install())
    except Exception:
        print("SDK setup failed. No evaluation was started.")
        raise SystemExit(2)
