#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

def run_command(cmd, cwd=None, timeout=60, env=None):
    """Ejecuta un comando shell con timeout."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr
        }
    except subprocess.TimeoutExpired as e:
        return {
            "error": f"Timeout después de {timeout}s",
            "stdout": e.stdout.decode() if e.stdout else "",
            "stderr": e.stderr.decode() if e.stderr else ""
        }
    except Exception as e:
        return {"error": str(e)}