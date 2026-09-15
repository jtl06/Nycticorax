"""Run the same offline checks locally and in GitHub Actions."""
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    commands = (
        ("ruff", "check", "src", "tests", "scripts"),
        ("mypy", "src/nycti/chat/run_state.py", "src/nycti/chat/tool_eligibility.py",
         "src/nycti/llm/provider_policy.py"),
        ("compileall", "-q", "src", "tests", "scripts"),
        ("pytest", "tests/", "-q"),
    )
    for command in commands:
        print("Checking: " + " ".join(command), flush=True)
        result = subprocess.run([sys.executable, "-m", *command], cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
