"""Run every test module, each in its own process.

    py tests/run_all.py

Separate processes so one module's global changes can't leak into the next.
Exits non-zero if any module failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    modules = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    failed = []
    for module in modules:
        done = subprocess.run([sys.executable, "-m", f"tests.{module}"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        last = (done.stdout.strip().splitlines() or ["(no output)"])[-1]
        if done.returncode != 0:
            failed.append(module)
            print(f"FAIL  {module}")
            print("\n".join("      " + line for line in
                            (done.stdout + done.stderr).strip().splitlines()[-15:]))
        else:
            print(f"ok    {last}")
    print(f"\n{len(modules) - len(failed)} of {len(modules)} modules passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
