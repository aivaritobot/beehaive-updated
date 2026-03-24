#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright

class BrowserTool:
    """Herramienta para automatización de navegador con Playwright."""
    
    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        
    async def start(self, headless=True):
        """Inicia el navegador."""
        if self._browser:
            return
            
        playwright = await async_playwright().start()
        self._browser = await playwright.chromium.launch(headless=headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        
    async def close(self):
        """Cierra el navegador."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None 
            self._page = None
            
    async def goto(self, url, wait_until="load", timeout=30000):
        """Navega a una URL."""
        if not self._page:
            await self.start()
        await self._page.goto(url, wait_until=wait_until, timeout=timeout)
        
    async def click(self, selector, timeout=None):
        """Click en un elemento."""
        await self._page.click(selector, timeout=timeout)
        
    async def type(self, selector, text, delay=None):
        """Escribe texto en un elemento."""
        await self._page.type(selector, text, delay=delay)
        
    async def wait_for_selector(self, selector, timeout=None):
        """Espera a que aparezca un elemento."""
        await self._page.wait_for_selector(selector, timeout=timeout)
        
    async def wait_for_load_state(self, state="load", timeout=None):
        """Espera a que la página alcance cierto estado."""
        await self._page.wait_for_load_state(state, timeout=timeout)
        
    async def screenshot(self, path=None, **kwargs):
        """Toma una captura de pantalla."""
        return await self._page.screenshot(path=path, **kwargs)
        
    async def get_text(self, selector):
        """Obtiene el texto de un elemento."""
        el = await self._page.query_selector(selector)
        if not el:
            return None
        return await el.text_content()
        
    async def get_attribute(self, selector, name):
        """Obtiene un atributo de un elemento."""
        el = await self._page.query_selector(selector)
        if not el:
            return None
        return await el.get_attribute(name)
        
    async def evaluate(self, expression):
        """Evalúa código JavaScript."""
        return await self._page.evaluate(expression)
        
    async def fill_form(self, form_data):
        """Rellena un formulario con los datos dados."""
        for selector, value in form_data.items():
            await self._page.fill(selector, str(value))
            
    async def execute_actions(self, actions):
        """Ejecuta una lista de acciones."""
        results = []
        
        for action in actions:
            action_type = action.get("type")
            
            if action_type == "goto":
                await self.goto(action["url"])
                results.append({"goto": action["url"]})
                
            elif action_type == "click":
                await self.click(action["selector"])
                results.append({"click": action["selector"]})
                
            elif action_type == "type":
                await self.type(action["selector"], action["text"])
                results.append({"type": f"{action['selector']}={action['text']}"})
                
            elif action_type == "wait":
                await asyncio.sleep(action.get("ms", 1000) / 1000)
                results.append({"wait": f"{action.get('ms', 1000)}ms"})
                
            elif action_type == "extract":
                text = await self.get_text(action["selector"])
                results.append({"extract": {
                    "selector": action["selector"],
                    "text": text
                }})
                
        return {
            "ok": True,
            "actions_executed": len(results),
            "results": results,
            "url": self._page.url,
            "title": await self._page.title()
        }