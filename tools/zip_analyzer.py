#!/usr/bin/env python3
import io
import os
import zipfile
from pathlib import Path

class ZipAnalyzer:
    """Analiza y manipula archivos ZIP."""

    def __init__(self, file_path=None):
        self.file_path = Path(file_path) if file_path else None
        self._zip = None

    def __enter__(self):
        if self.file_path:
            self._zip = zipfile.ZipFile(self.file_path)
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._zip:
            self._zip.close()
            self._zip = None

    def analyze_bytes(self, data):
        """Analiza contenido ZIP desde bytes."""
        self._zip = zipfile.ZipFile(io.BytesIO(data))
        return self._analyze()

    def analyze_file(self, file_path):
        """Analiza un archivo ZIP."""
        self.file_path = Path(file_path)
        with zipfile.ZipFile(self.file_path) as z:
            return self._analyze()

    def _analyze(self):
        """Analiza la estructura del ZIP actual."""
        if not self._zip:
            raise ValueError("No hay ZIP abierto")

        info = {
            "files": [],
            "dirs": set(),
            "total_size": 0,
            "compressed_size": 0
        }

        for item in self._zip.filelist:
            # Ignora __MACOSX
            if "__MACOSX" in item.filename:
                continue

            # Es directorio
            if item.filename.endswith("/"):
                info["dirs"].add(item.filename)
                continue

            # Analiza archivo
            file_info = {
                "name": item.filename,
                "size": item.file_size,
                "compressed_size": item.compress_size,
                "date": f"{item.date_time[0]}-{item.date_time[1]:02d}-{item.date_time[2]:02d}",
                "is_encrypted": item.flag_bits & 0x1
            }

            info["files"].append(file_info)
            info["total_size"] += item.file_size
            info["compressed_size"] += item.compress_size

            # Agrega directorio padre
            parent = str(Path(item.filename).parent)
            if parent and parent != ".":
                info["dirs"].add(parent + "/")

        info["dirs"] = sorted(info["dirs"])
        info["compression_ratio"] = round(
            (info["total_size"] - info["compressed_size"]) / info["total_size"] * 100, 1
        ) if info["total_size"] else 0

        return info

    def extract_file(self, filename, output_dir=None):
        """Extrae un archivo específico."""
        if not self._zip:
            raise ValueError("No hay ZIP abierto")

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            self._zip.extract(filename, output_dir)
            return str(Path(output_dir) / filename)
        else:
            return self._zip.read(filename)

    def extract_all(self, output_dir):
        """Extrae todo el contenido."""
        if not self._zip:
            raise ValueError("No hay ZIP abierto")

        os.makedirs(output_dir, exist_ok=True)
        for item in self._zip.filelist:
            if "__MACOSX" in item.filename:
                continue
            self._zip.extract(item, output_dir)
        return str(Path(output_dir))