#!/usr/bin/env python3
import io
import fitz  # PyMuPDF
from pathlib import Path

class PDFReader:
    """Lee y extrae contenido de archivos PDF."""

    def __init__(self):
        self.current_doc = None

    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Cierra el documento actual."""
        if self.current_doc:
            self.current_doc.close()
            self.current_doc = None

    def load_file(self, file_path):
        """Carga un archivo PDF."""
        self.close()
        self.current_doc = fitz.open(file_path)
        return {
            "pages": len(self.current_doc),
            "metadata": self.current_doc.metadata
        }

    def load_bytes(self, data):
        """Carga PDF desde bytes."""
        self.close()
        stream = io.BytesIO(data)
        self.current_doc = fitz.open(stream=stream)
        return {
            "pages": len(self.current_doc),
            "metadata": self.current_doc.metadata
        }

    def extract_text(self, start_page=0, end_page=None):
        """Extrae texto del PDF."""
        if not self.current_doc:
            raise ValueError("No hay documento abierto")

        if end_page is None:
            end_page = len(self.current_doc)

        text = []
        for page_num in range(start_page, end_page):
            page = self.current_doc[page_num]
            text.append(page.get_text())

        return "\n\n".join(text)

    def extract_images(self, output_dir, start_page=0, end_page=None):
        """Extrae imágenes del PDF."""
        if not self.current_doc:
            raise ValueError("No hay documento abierto")

        if end_page is None:
            end_page = len(self.current_doc)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        extracted = []
        for page_num in range(start_page, end_page):
            page = self.current_doc[page_num]
            
            image_list = page.get_images()
            for img_idx, img in enumerate(image_list):
                xref = img[0]
                
                try:
                    base_image = self.current_doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    
                    ext = base_image["ext"]
                    image_name = f"page_{page_num + 1}_img_{img_idx + 1}.{ext}"
                    image_path = output_dir / image_name
                    
                    image_path.write_bytes(image_bytes)
                    extracted.append(str(image_path))
                except Exception as e:
                    print(f"Error extracting image {img_idx} from page {page_num}: {e}")

        return extracted

    def get_table_of_contents(self):
        """Obtiene la tabla de contenidos."""
        if not self.current_doc:
            raise ValueError("No hay documento abierto")
            
        toc = self.current_doc.get_toc()
        return [{
            "level": t[0],
            "title": t[1],
            "page": t[2]
        } for t in toc]

    def get_page_count(self):
        """Retorna el número de páginas."""
        if not self.current_doc:
            raise ValueError("No hay documento abierto")
        return len(self.current_doc)