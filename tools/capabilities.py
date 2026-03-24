#!/usr/bin/env python3
import json
import os
import subprocess
from pathlib import Path

class Capabilities:
    """Gestiona las capacidades y permisos del builder."""
    
    def __init__(self, config_path=None):
        self.config_path = Path(config_path) if config_path else Path("builder_config.json")
        self.config = self._load_config()
        
    def _load_config(self):
        """Carga la configuración del builder."""
        if not self.config_path.exists():
            return {}
        try:
            return json.loads(self.config_path.read_text())
        except Exception:
            return {}
            
    def _save_config(self):
        """Guarda la configuración actual."""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def get_allowed_paths(self):
        """Retorna las rutas permitidas para acceso a archivos."""
        paths = self.config.get("local_roots", [])
        if not paths:
            # Por defecto solo el home del usuario
            try:
                paths = [str(Path.home())]
            except Exception:
                paths = []
        return paths

    def is_path_allowed(self, path):
        """Verifica si una ruta está permitida."""
        try:
            target = Path(path).resolve()
            for root in self.get_allowed_paths():
                try:
                    target.relative_to(Path(root).resolve())
                    return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def can_execute_command(self, cmd):
        """Verifica si un comando está permitido."""
        # Lista blanca de comandos permitidos
        ALLOWED = {
            "git",
            "python",
            "python3", 
            "pip",
            "npm",
            "node",
            "pytest",
            "ls",
            "cat"
        }
        try:
            prog = cmd.split()[0]
            return prog in ALLOWED
        except Exception:
            return False

    def run_command(self, cmd, cwd=None, timeout=60):
        """Ejecuta un comando si está permitido."""
        if not self.can_execute_command(cmd):
            raise ValueError(f"Comando no permitido: {cmd}")
            
        if cwd and not self.is_path_allowed(cwd):
            raise ValueError(f"Ruta no permitida: {cwd}")

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
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

    def read_file(self, path, max_size=1_000_000):
        """Lee un archivo si está permitido."""
        if not self.is_path_allowed(path):
            raise ValueError(f"Ruta no permitida: {path}")
            
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"No es un archivo: {path}")
            
        if path.stat().st_size > max_size:
            raise ValueError(f"Archivo demasiado grande (máx {max_size//1000}KB)")
            
        return path.read_text()

    def write_file(self, path, content):
        """Escribe un archivo si está permitido."""
        if not self.is_path_allowed(path):
            raise ValueError(f"Ruta no permitida: {path}")
            
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def list_directory(self, path, pattern="*"):
        """Lista archivos en un directorio si está permitido."""
        if not self.is_path_allowed(path):
            raise ValueError(f"Ruta no permitida: {path}")
            
        path = Path(path)
        if not path.is_dir():
            raise ValueError(f"No es un directorio: {path}")
            
        return [str(p.relative_to(path)) for p in path.glob(pattern)]

    def get_api_key(self, service):
        """Obtiene una clave de API del config o variables de entorno."""
        env_key = f"{service.upper()}_API_KEY"
        return os.getenv(env_key) or self.config.get(f"{service}_api_key")