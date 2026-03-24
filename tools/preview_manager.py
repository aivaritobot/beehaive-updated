#!/usr/bin/env python3
import mimetypes
import os
from pathlib import Path
import magic

class PreviewManager:
    """Gestiona la vista previa de archivos."""

    def __init__(self, max_preview_size=500_000):
        self.max_preview_size = max_preview_size
        self.mime = magic.Magic(mime=True)

    def get_mime_type(self, file_path):
        """Detecta el tipo MIME del archivo."""
        try:
            return self.mime.from_file(str(file_path))
        except Exception:
            # Fallback a mimetypes si falla magic
            mime_type, _ = mimetypes.guess_type(file_path)
            return mime_type or "application/octet-stream"

    def is_text_file(self, file_path):
        """Determina si es un archivo de texto."""
        mime_type = self.get_mime_type(file_path)
        return (
            mime_type.startswith("text/") or
            mime_type in [
                "application/json",
                "application/javascript",
                "application/xml"
            ]
        )

    def is_image_file(self, file_path):
        """Determina si es una imagen."""
        mime_type = self.get_mime_type(file_path)
        return mime_type.startswith("image/")

    def is_pdf_file(self, file_path):
        """Determina si es un PDF."""
        mime_type = self.get_mime_type(file_path)
        return mime_type == "application/pdf"

    def get_preview(self, file_path):
        """Obtiene una vista previa del archivo."""
        path = Path(file_path)
        
        if not path.exists():
            return {"error": "Archivo no encontrado"}
            
        size = path.stat().st_size
        if size > self.max_preview_size:
            return {"error": f"Archivo demasiado grande (máx {self.max_preview_size//1000}KB)"}
            
        mime_type = self.get_mime_type(path)
        
        preview = {
            "path": str(path),
            "size": size,
            "mime_type": mime_type
        }
        
        try:
            if self.is_text_file(path):
                preview["type"] = "text"
                preview["content"] = path.read_text(errors="replace")
                
            elif self.is_image_file(path):
                preview["type"] = "image"
                preview["content"] = str(path)
                
            elif self.is_pdf_file(path):
                preview["type"] = "pdf" 
                preview["content"] = str(path)
                
            else:
                preview["type"] = "binary"
                preview["content"] = None
                
        except Exception as e:
            preview["error"] = str(e)
            
        return preview

    def get_directory_preview(self, dir_path):
        """Vista previa de un directorio."""
        path = Path(dir_path)
        
        if not path.is_dir():
            return {"error": "No es un directorio"}
            
        entries = []
        try:
            for entry in sorted(path.iterdir()):
                entry_info = {
                    "name": entry.name,
                    "path": str(entry),
                    "type": "directory" if entry.is_dir() else "file"
                }
                
                if entry.is_file():
                    entry_info["size"] = entry.stat().st_size
                    entry_info["mime_type"] = self.get_mime_type(entry)
                    
                entries.append(entry_info)
                
        except Exception as e:
            return {"error": str(e)}
            
        return {
            "path": str(path),
            "entries": entries
        }