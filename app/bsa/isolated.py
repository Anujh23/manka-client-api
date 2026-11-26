"""Run analyze_file() in its own process with a hard time limit.

Online audit 2026-11-23: a hostile 123 KB PDF kept MuPDF busy for minutes inside one call; a stuck thread can't be
stopped, a process can. The web app calls analyze_isolated(); tests, scripts and gates keep calling analyze_file().

    analyze_isolated(path, password=None, applicant_name=None, original_filename=None, timeout=None) -> report
    raises PasswordRequired / UnsupportedFile / AAStatement like the engine, AnalysisTimeout after the limit,
    EngineCrash for anything else (the child's exception name + message).

The child is `python -m app.bsa.isolated`, fed a JSON job on stdin (the password never appears on a command
line) and answering one JSON line on stdout. Env: BSA_ANALYZE_TIMEOUT seconds (default 120);
BSA_ANALYZE_ISOLATE=0 runs in-process with no limit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_MARK = "@@BSA-RESULT@@"


class AnalysisTimeout(Exception):
    pass


class EngineCrash(Exception):
    pass


def analyze_isolated(path, password=None, applicant_name=None, original_filename=None, timeout=None) -> dict:
    from . import AAStatement, PasswordRequired, UnsupportedFile, analyze_file
    if os.environ.get("BSA_ANALYZE_ISOLATE", "1") == "0":
        return analyze_file(path, password=password, applicant_name=applicant_name,
                            original_filename=original_filename)
    limit = float(timeout or os.environ.get("BSA_ANALYZE_TIMEOUT") or 120)
    pkg_root = str(Path(__file__).resolve().parents[2])          # the folder holding app/
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join([pkg_root] + [p for p in [os.environ.get("PYTHONPATH")] if p]))
    job = json.dumps({"path": str(Path(path).resolve()), "password": password, "applicant_name": applicant_name,
                      "original_filename": original_filename})
    try:
        p = subprocess.run([sys.executable, "-m", "app.bsa.isolated"], input=job.encode("utf-8"),
                           capture_output=True, timeout=limit, env=env, cwd=pkg_root)
    except subprocess.TimeoutExpired as e:   # subprocess.run kills the child before raising
        raise AnalysisTimeout(f"Analysis did not finish in {int(limit)} s - the PDF may be damaged or built to "
                              "stall readers. Ask for a fresh download from the bank.") from e
    out = p.stdout.decode("utf-8", "replace")
    line = next((ln[len(_MARK):] for ln in reversed(out.splitlines()) if ln.startswith(_MARK)), None)
    if line is None:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise EngineCrash(f"engine process exited with code {p.returncode}: {tail[0][:200]}")
    res = json.loads(line)
    if "report" in res:
        return res["report"]
    name, msg = res.get("error", "Exception"), res.get("message", "")
    if name == "AAStatement":
        raise AAStatement(msg)
    if name == "PasswordRequired":
        raise PasswordRequired(msg)
    if name == "UnsupportedFile":
        raise UnsupportedFile(msg)
    raise EngineCrash(f"{name}: {msg}")


def _worker() -> None:
    real_out = sys.stdout
    sys.stdout = sys.stderr                   # anything the engine prints must not corrupt the answer line
    job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        from app.bsa import analyze_file
        rep = analyze_file(job["path"], password=job.get("password"), applicant_name=job.get("applicant_name"),
                           original_filename=job.get("original_filename"))
        res = {"report": rep}
    except Exception as e:  # noqa: BLE001 - shipped to the parent as name + message
        res = {"error": type(e).__name__, "message": str(e)}
    real_out.write(_MARK + json.dumps(res, default=str) + "\n")
    real_out.flush()


if __name__ == "__main__":
    _worker()
