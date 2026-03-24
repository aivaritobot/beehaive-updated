#!/usr/bin/env python3
import json
import os
import requests
from pathlib import Path

class ImageGenerator:
    """Generador de imágenes usando APIs de modelos de difusión."""
    
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        
    def generate(self, prompt, num_images=1, size="1024x1024", model="dall-e-3"):
        """Genera imágenes con DALL-E."""
        if not self.api_key:
            raise ValueError("Se requiere API key de OpenAI")
            
        url = "https://api.openai.com/v1/images/generations"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        data = {
            "prompt": prompt,
            "n": num_images,
            "size": size,
            "model": model
        }
        
        r = requests.post(url, headers=headers, json=data)
        r.raise_for_status()
        
        return [img["url"] for img in r.json()["data"]]

    def save_images(self, urls, output_dir):
        """Descarga y guarda las imágenes generadas."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        saved = []
        for i, url in enumerate(urls):
            r = requests.get(url)
            r.raise_for_status()
            
            ext = "png" if "png" in r.headers.get("content-type","") else "jpg"
            path = output_dir / f"generated_{i}.{ext}"
            
            path.write_bytes(r.content)
            saved.append(str(path))
            
        return saved