#!/usr/bin/env python3
import json
import os
import tempfile
from pathlib import Path
import magic
import chardet

class FileProcessor:
    """Procesa y analiza archivos."""

    def __init__(self, max_size=10_000_000):
        self.max_size = max_size
        self.mime = magic.Magic(mime=True)
        
    def detect_mime_type(self, file_path):
        """Detecta el tipo MIME del archivo."""
        try:
            return self.mime.from_file(str(file_path))
        except Exception:
            return "application/octet-stream"
            
    def detect_encoding(self, file_path):
        """Detecta la codificación del archivo."""
        try:
            with open(file_path, "rb") as f:
                raw = f.read(4096)
                result = chardet.detect(raw)
                return result["encoding"]
        except Exception:
            return None
            
    def read_file(self, file_path, encoding=None):
        """Lee un archivo con detección de codificación."""
        path = Path(file_path)
        
        if not path.is_file():
            raise ValueError("No es un archivo")
            
        if path.stat().st_size > self.max_size:
            raise ValueError(f"Archivo demasiado grande (máx {self.max_size//1_000_000}MB)")
            
        if encoding is None:
            encoding = self.detect_encoding(path) or "utf-8"
            
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            # Reintenta con utf-8 si falla la detección
            return path.read_text(encoding="utf-8", errors="replace")
            
    def process_file(self, file_path):
        """Procesa un archivo y extrae metadata."""
        path = Path(file_path)
        
        if not path.is_file():
            return {"error": "No es un archivo"}
            
        try:
            stat = path.stat()
            
            info = {
                "path": str(path),
                "size": stat.st_size,
                "created": stat.st_ctime,
                "modified": stat.st_mtime,
                "mime_type": self.detect_mime_type(path)
            }
            
            # Para archivos de texto
            if info["mime_type"].startswith("text/"):
                info["encoding"] = self.detect_encoding(path)
                if stat.st_size <= self.max_size:
                    info["content"] = self.read_file(path)
                    info["lines"] = len(info["content"].splitlines())
                    
            return info
            
        except Exception as e:
            return {"error": str(e)}
            
    def create_temp_copy(self, file_path):
        """Crea una copia temporal del archivo."""
        path = Path(file_path)
        
        if not path.is_file():
            raise ValueError("No es un archivo")
            
        suffix = path.suffix
        
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(path.read_bytes())
            return tmp.name