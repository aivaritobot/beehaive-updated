#!/usr/bin/env python3
import json
import shutil
from pathlib import Path

class FileHandler:
    """Maneja operaciones con archivos."""

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def read_file(self, rel_path, encoding="utf-8"):
        """Lee un archivo."""
        path = (self.base_dir / rel_path).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("Path traversal no permitido")
        return path.read_text(encoding=encoding)

    def write_file(self, rel_path, content, encoding="utf-8"):
        """Escribe un archivo."""
        path = (self.base_dir / rel_path).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("Path traversal no permitido")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)
        
    def delete_file(self, rel_path):
        """Elimina un archivo."""
        path = (self.base_dir / rel_path).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("Path traversal no permitido")
        path.unlink()

    def copy_file(self, src_path, dst_path):
        """Copia un archivo."""
        src = (self.base_dir / src_path).resolve()
        dst = (self.base_dir / dst_path).resolve()
        
        if not (self.base_dir.resolve() in src.parents and
                self.base_dir.resolve() in dst.parents):
            raise ValueError("Path traversal no permitido")
            
        shutil.copy2(src, dst)

    def move_file(self, src_path, dst_path): 
        """Mueve un archivo."""
        src = (self.base_dir / src_path).resolve()
        dst = (self.base_dir / dst_path).resolve()
        
        if not (self.base_dir.resolve() in src.parents and
                self.base_dir.resolve() in dst.parents):
            raise ValueError("Path traversal no permitido")
            
        shutil.move(src, dst)

    def list_files(self, rel_path=".", pattern="*"):
        """Lista archivos en un directorio."""
        path = (self.base_dir / rel_path).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("Path traversal no permitido")
            
        return [str(p.relative_to(path)) for p in path.glob(pattern)]