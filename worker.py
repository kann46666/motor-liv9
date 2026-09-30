# worker.py – Script para los Motores (Trabajadores) en Render
import os
import re
import json
import concurrent.futures
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
import gradio as gr
from fastapi import FastAPI, Request
import uvicorn

# ============================ Config ============================
PATRON_IMG = r"https://(?:ss|sp)\d+\.liverpool\.com\.mx/(?:xl|i)/[\w\d\-\_]+\.jpg"
BASE_IMG_FALLBACK = "https://sp540.liverpool.com.mx/i/"
BASE_PDP = "https://www.liverpool.com.mx/tienda/pdp"
BASE_HOME = "https://www.liverpool.com.mx"
DEFAULT_TIMEOUT = 5

def slug_a_nombre(slug: str) -> str:
    if not slug: return ""
    s = unquote(slug).split("?")[0].split("#")[0]
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip().title()

def normalize_sku(raw) -> str:
    s = str(raw).strip()
    if s.endswith(".0"): s = s[:-2]
    return re.sub(r"\D+", "", s)

# ============================ Cliente Scraping ============================
@dataclass(frozen=True)
class ClientConfig:
    timeout: int = DEFAULT_TIMEOUT
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0"

class LiverpoolWorkerClient:
    def __init__(self, cfg: ClientConfig | None = None):
        self.cfg = cfg or ClientConfig()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg.user_agent,
            "Accept-Language": "es-MX,es;q=0.9",
            "Referer": BASE_HOME,
        })
        retries = Retry(total=1, backoff_factor=0.2, status_forcelist=(429, 500, 502, 503))
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self._img_cache = {}

    def _get_html(self, url: str) -> str:
        try:
            r = self.session.get(url, timeout=self.cfg.timeout)
            if r.status_code == 200 and r.text: return r.text
        except: pass
        return ""

    def _check_single_url(self, url: str) -> bool:
        try:
            rg = self.session.get(url, timeout=3, stream=True)
            return rg.status_code == 200
        except: return False

    def _check_image_validity(self, url: str) -> bool:
        if not url: return False
        if url in self._img_cache: return self._img_cache[url]
        v = self._check_single_url(url)
        self._img_cache[url] = v
        return v

    @lru_cache(maxsize=2048)
    def buscar_slug_en_liverpool(self, sku: str) -> str:
        html = self._get_html(f"{BASE_HOME}/tienda?s={sku}")
        if not html or 'null-search-landing' in html: return ""
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if "/pdp/" in a["href"]:
                m = re.search(r"/pdp/([^/]+)/?", a["href"])
                if m: return m.group(1).strip()
        return ""

    @lru_cache(maxsize=2048)
    def extraer_imagenes_de_html(self, html: str, sku: str = "") -> list[str]:
        if not html: return [""]*6
        soup = BeautifulSoup(html, "html.parser")
        main_img = ""
        tag_main = soup.find("img", {"data-testid": re.compile(r"gallery.*main.*image", re.I)})
        if tag_main:
            main_img = tag_main.get("src") or tag_main.get("data-src", "")
        if not main_img and sku:
            for u in re.findall(PATRON_IMG, html, flags=re.IGNORECASE):
                if sku in u: main_img = u; break
        
        cands = [main_img] if main_img else []
        while len(cands) < 6 and main_img:
            cands.append(main_img)
        return (cands + [""]*6)[:6]

    @lru_cache(maxsize=2048)
    def extraer_datos_pdp(self, pdp_url: str):
        html = self._get_html(pdp_url)
        if not html: return 0.0, 0.0, "", "", "", "Disponible"
        soup = BeautifulSoup(html, "html.parser")
        
        nombre_real = ""
        h1 = soup.find("h1")
        if h1: nombre_real = h1.get_text(strip=True)

        estado = "Preventa" if soup.find(attrs={"data-testid": "flag-presale"}) else "Disponible"

        def _limp(t):
            return float(re.sub(r'[^\d.]', '', t.get_text(strip=True))) if t else 0.0

        p_act = _limp(soup.find(attrs={"data-testid": "discounted"}))
        p_orig = _limp(soup.find(attrs={"data-testid": "original"}))
        
        marca = ""
        b_tag = soup.find("a", class_=lambda c: c and "ml-product-info-brand-link" in c)
        if b_tag: marca = b_tag.get_text(strip=True)
            
        categoria = ""
        nav = soup.find("nav", attrs={"data-testid": lambda x: x and str(x).endswith("-breadcrumb")})
        if nav:
            links = nav.find_all("a", href=re.compile(r"/tienda/.*?/cat"))
            if links: categoria = links[0].get_text(strip=True)

        return p_act, p_orig, marca, categoria, nombre_real, estado

    def resolver_producto(self, sku: str):
        clean_sku = normalize_sku(sku)
        if not clean_sku: return [""]*6, "", "", "invalid-sku", 0.0, 0.0, "", "", "Disponible"

        slug = self.buscar_slug_en_liverpool(clean_sku)
        pdp_url = f"{BASE_PDP}/{slug}/{clean_sku}" if slug else f"{BASE_PDP}/default/{clean_sku}"
        
        html = self._get_html(pdp_url)
        imgs = self.extraer_imagenes_de_html(html, clean_sku)
        
        is_offline = not imgs[0] and not html
        if is_offline:
            return [""]*6, "", "", "offline / no encontrado", 0.0, 0.0, "", "", "Disponible"

        p_act, p_orig, marca, cat, name, estado = self.extraer_datos_pdp(pdp_url)
        return imgs, pdp_url, name or slug_a_nombre(slug), "slug+sku", p_act, p_orig, marca, cat, estado

client = LiverpoolWorkerClient()

# ====================== Servidor FastAPI / Motor ======================
app = FastAPI()

@app.post("/procesar_lote")
async def procesar_lote(request: Request):
    data = await request.json()
    skus_lote = data.get("skus", [])
    
    valid_records = []
    offline_dict = {}

    def procesar_individual(item):
        grupo, sku = item
        imgs, purl, pname, strat, p_actual, p_original, marca, categoria, estado = client.resolver_producto(sku)
        
        if strat == "offline / no encontrado" or not imgs[0]:
            return {"type": "offline", "grupo": grupo, "sku": sku}
        else:
            pct_desc = int(round((1 - p_actual / p_original) * 100)) if p_original > p_actual > 0 else 0
            return {
                "type": "valid",
                "record": {
                    "Grupo_Pegado": grupo, "Producto": sku, "Categoria": categoria, "Marca": marca, "Estado": estado,
                    "Precio_Actual": p_actual, "Precio_Original": p_original, "Descuento_Porcentaje": f"{pct_desc}%" if pct_desc > 0 else "0%",
                    "Image_1": imgs[0], "Image_2": imgs[1], "Image_3": imgs[2], "Image_4": imgs[3], "Image_5": imgs[4], "Image_6": imgs[5],
                    "producto_url": purl, "Producto_Nombre": pname, "Estrategia": strat,
                }
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(procesar_individual, item) for item in skus_lote]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res["type"] == "valid":
                valid_records.append(res["record"])
            else:
                g, s = res["grupo"], res["sku"]
                if g not in offline_dict: offline_dict[g] = []
                offline_dict[g].append(s)

    return {"valid_records": valid_records, "offline": offline_dict}

demo = gr.Interface(fn=lambda: "Motor Worker Activo y Escuchando", inputs=[], outputs="text")
demo.app = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)