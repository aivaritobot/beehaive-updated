#!/usr/bin/env python3
import subprocess
import sys
import tempfile
from pathlib import Path

class CodeSandbox:
    """Ejecuta código Python en un entorno aislado."""

    def __init__(self, workspace_dir=None):
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def run_code(self, code, timeout=30):
        """Ejecuta código Python en un archivo temporal."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', dir=self.workspace_dir, delete=False) as f:
            f.write(code)
            temp_path = f.name

        try:
            # Ejecuta con subprocess para aislar
            proc = subprocess.run(
                [sys.executable, temp_path],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return {
                'exit_code': proc.returncode,
                'stdout': proc.stdout,
                'stderr': proc.stderr
            }
        except subprocess.TimeoutExpired as e:
            return {
                'error': f'Timeout después de {timeout}s',
                'stdout': e.stdout.decode() if e.stdout else '',
                'stderr': e.stderr.decode() if e.stderr else ''
            }
        except Exception as e:
            return {'error': str(e)}
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def run_tests(self, test_code):
        """Ejecuta tests unitarios."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='_test.py', dir=self.workspace_dir, delete=False) as f:
            f.write(test_code) 
            test_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", test_path, "-v"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True
            )
            return {
                'exit_code': proc.returncode,
                'stdout': proc.stdout,
                'stderr': proc.stderr
            }
        except Exception as e:
            return {'error': str(e)}
        finally:
            Path(test_path).unlink(missing_ok=True)