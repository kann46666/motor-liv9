# worker.py – Motor / Trabajador en API Pura (FastAPI) sin Gradio
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
    base_pdp: str = BASE_PDP
    base_img_fallback: str = BASE_IMG_FALLBACK
    patron_img: str = PATRON_IMG
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

class LiverpoolWorkerClient:
    def __init__(self, cfg: ClientConfig | None = None):
        self.cfg = cfg or ClientConfig()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg.user_agent,
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "close",
            "Referer": BASE_HOME,
        })
        retries = Retry(total=1, backoff_factor=0.3, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(["GET", "HEAD"]), raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=15)
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
            r = self.session.head(url, timeout=3, allow_redirects=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", "").lower(): return True
            rg = self.session.get(url, timeout=3, stream=True)
            chunk = next(rg.iter_content(chunk_size=64), b"")
            return rg.status_code == 200 and bool(chunk)
        except: return False

    def _check_image_validity(self, url: str) -> bool:
        if not url: return False
        if url in self._img_cache: return self._img_cache[url]
        is_valid = self._check_single_url(url)
        self._img_cache[url] = is_valid
        return is_valid

    def _check_validity_bulk(self, urls: list[str]) -> dict[str, bool]:
        results = {}
        to_check = []
        for u in urls:
            if u in self._img_cache: results[u] = self._img_cache[u]
            else: to_check.append(u)
        if to_check:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                future_to_url = {executor.submit(self._check_single_url, u): u for u in to_check}
                for future in concurrent.futures.as_completed(future_to_url):
                    u = future_to_url[future]
                    try: is_valid = future.result()
                    except: is_valid = False
                    results[u] = is_valid
                    self._img_cache[u] = is_valid
        return results

    @lru_cache(maxsize=2048)
    def buscar_slug_en_liverpool(self, sku: str) -> str:
        if not sku: return ""
        html = self._get_html(f"{BASE_HOME}/tienda?s={sku}")
        if not html: return ""
        if 'data-testid="null-search-landing"' in html or 'search-not-found-content' in html: return ""
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if "/pdp/" in a["href"]:
                m = re.search(r"/pdp/([^/]+)/?", a["href"])
                if m: return m.group(1).strip()
        m = re.search(r"/pdp/([^/]+)/", html)
        return m.group(1).strip() if m else ""

    @lru_cache(maxsize=2048)
    def candidatos_pdp_desde_busqueda(self, sku: str) -> list[str]:
        out = []
        html = self._get_html(f"{BASE_HOME}/tienda?s={sku}")
        if not html or 'data-testid="null-search-landing"' in html or 'search-not-found-content' in html: return out
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        for a in soup.find_all("a", href=True):
            if "/pdp/" in a["href"]:
                abs_url = urljoin(BASE_HOME, a["href"])
                if abs_url not in seen:
                    seen.add(abs_url)
                    out.append(abs_url)
        return out

    def _get_prefix(self, url: str) -> str:
        m = re.match(r"^(\d+)", url.split('/')[-1])
        if m: return f"{url.rsplit('/', 1)[0]}/{m.group(1)}"
        return re.sub(r"(_|-)[0-9]+[a-zA-Z]?$", "", url.replace(".jpg.jpg", ".jpg").replace(".jpg", ""))

    def _deducir_base_y_variantes(self, main_img: str, thumb_imgs: list[str], html: str) -> list[str]:
        candidatos = []
        if main_img: candidatos.append(main_img)
        candidatos.extend(thumb_imgs)
        cands_uniq = []
        for c in candidatos:
            if c and c not in cands_uniq: cands_uniq.append(c)
        if main_img and len(cands_uniq) < 6:
            main_prefix = self._get_prefix(main_img)
            extra_cands = set(u for u in re.findall(self.cfg.patron_img, html, flags=re.IGNORECASE) if u.startswith(main_prefix) and u.endswith(".jpg"))
            extra_cands.add(f"{main_prefix}.jpg")
            for i in range(1, 7):
                extra_cands.update([f"{main_prefix}_{i}p.jpg", f"{main_prefix}-{i}p.jpg", f"{main_prefix}_{i}.jpg", f"{main_prefix}-{i}.jpg"])
            for url in sorted(list(extra_cands)):
                if url not in cands_uniq: cands_uniq.append(url)
        validities = self._check_validity_bulk(cands_uniq)
        finales = [u for u in cands_uniq if validities.get(u, False)]
        return (finales + [""]*6)[:6]

    @lru_cache(maxsize=2048)
    def extraer_imagenes_de_html(self, html: str, sku: str = "") -> list[str]:
        if not html: return [""]*6
        soup = BeautifulSoup(html, "html.parser")
        main_img = ""
        thumb_imgs = []
        tag_main = soup.find("img", {"data-testid": re.compile(r"gallery.*main.*image", re.I)})
        if tag_main:
            src = tag_main.get("src") or tag_main.get("data-src", "")
            if re.search(self.cfg.patron_img, src, flags=re.IGNORECASE): main_img = src
        for tag in soup.find_all("img", {"data-testid": re.compile(r"gallery.*thumbnail.*image", re.I)}):
            src = tag.get("src") or tag.get("data-src", "")
            if src and re.search(self.cfg.patron_img, src, flags=re.IGNORECASE): thumb_imgs.append(src)
        if not main_img:
            meta_og = soup.find("meta", property="og:image")
            if meta_og and meta_og.get("content") and re.search(self.cfg.patron_img, meta_og.get("content").strip(), flags=re.IGNORECASE):
                main_img = meta_og.get("content").strip()
        if not main_img and sku:
            for u in re.findall(self.cfg.patron_img, html, flags=re.IGNORECASE):
                if sku in u: main_img = u; break
        if not main_img:
            m = re.search(self.cfg.patron_img, html, flags=re.IGNORECASE)
            if m: main_img = m.group(0)
        return self._deducir_base_y_variantes(main_img, thumb_imgs, html)

    @lru_cache(maxsize=2048)
    def extraer_datos_pdp(self, pdp_url: str):
        html = self._get_html(pdp_url)
        if not html: return 0.0, 0.0, "", "", "", "Disponible"
        soup = BeautifulSoup(html, "html.parser")
        
        nombre_real = ""
        h1_tag = soup.find("h1")
        if h1_tag: nombre_real = h1_tag.get_text(strip=True)

        estado = "Disponible"
        presale_flag = soup.find(attrs={"data-testid": "flag-presale"})
        if presale_flag and "preventa" in presale_flag.get_text(strip=True).lower(): estado = "Preventa"
        else:
            for sp in soup.find_all("span"):
                if sp.get_text(strip=True).lower() == "preventa":
                    estado = "Preventa"; break

        def _limpiar_precio(tag):
            if not tag: return 0.0
            try: return float(re.sub(r'[^\d.]', '', tag.get_text(separator="", strip=True)))
            except: return 0.0
        
        p_act = _limpiar_precio(soup.find(attrs={"data-testid": "discounted"}))
        p_orig = _limpiar_precio(soup.find(attrs={"data-testid": "original"}))
        
        marca = ""
        brand_tag = soup.find("a", class_=lambda c: c and "ml-product-info-brand-link" in c)
        if brand_tag: marca = brand_tag.get_text(strip=True)
            
        categoria = ""
        breadcrumb_nav = soup.find("nav", attrs={"data-testid": lambda x: x and str(x).endswith("-breadcrumb")})
        if breadcrumb_nav:
            cat_links = breadcrumb_nav.find_all("a", href=re.compile(r"/tienda/.*?/cat"))
            if cat_links: categoria = cat_links[0].get_text(strip=True) or cat_links[0].get("aria-label", "")

        return p_act, p_orig, marca, categoria, nombre_real, estado

    @staticmethod
    def _variants_fixup(url: str) -> list[str]:
        cands = []
        if url.endswith(".jpg") and not url.endswith(".jpg.jpg"): cands.append(url + ".jpg")
        if url.endswith(".jpg.jpg"): cands.append(url[:-4])
        return cands

    def resolver_producto(self, sku: str):
        clean_sku = normalize_sku(sku)
        if not clean_sku: return [""]*6, "", "", "invalid-sku", 0.0, 0.0, "", "", "Disponible"

        urls_a_probar = [f"{BASE_PDP}/default/{clean_sku}"]
        slug = self.buscar_slug_en_liverpool(clean_sku)
        if slug: urls_a_probar.extend([f"{BASE_PDP}/{slug}/{clean_sku}", f"{BASE_PDP}/{slug}/"])
        urls_a_probar.extend(self.candidatos_pdp_desde_busqueda(clean_sku))

        producto_url, imagenes_url, estrategia = "", [""]*6, "fallback"

        for url in urls_a_probar:
            cands = self.extraer_imagenes_de_html(self._get_html(url), clean_sku)
            if cands[0]:
                producto_url, imagenes_url, estrategia = url, cands, ("slug+sku" if url.endswith(f"/{clean_sku}") else "slug")
                break

        if not imagenes_url[0]:
            html_busq = self._get_html(f"{BASE_HOME}/tienda?s={clean_sku}")
            if html_busq:
                if 'data-testid="null-search-landing"' in html_busq or 'search-not-force-content' in html_busq:
                    estrategia = "offline / no encontrado"
                else:
                    cands = self.extraer_imagenes_de_html(html_busq, clean_sku)
                    if cands[0]:
                        imagenes_url, producto_url, estrategia = cands, f"{BASE_HOME}/tienda?s={clean_sku}", "busqueda"

        if estrategia == "offline / no encontrado":
            return [""]*6, "", "", estrategia, 0.0, 0.0, "", "", "Disponible"

        def _valid_or_fix(u: str) -> tuple[str, str]:
            if not u: return "", ""
            if self._check_image_validity(u): return u, "valid"
            for v in self._variants_fixup(u):
                if self._check_image_validity(v): return v, "fixup"
            return "", ""

        if imagenes_url[0]:
            fixed, tag = _valid_or_fix(imagenes_url[0])
            if fixed: imagenes_url[0], estrategia = fixed, tag if tag != "valid" else estrategia
            elif len(imagenes_url) > 1 and imagenes_url[1]:
                fixed_var, _ = _valid_or_fix(imagenes_url[1])
                if fixed_var: imagenes_url[0], estrategia = fixed_var, "promoted_variant"
        
        if not imagenes_url[0]:
            fallback = f"{BASE_IMG_FALLBACK}{clean_sku}.jpg"
            if self._check_image_validity(fallback): imagenes_url[0], estrategia = fallback, "fallback"
            else: estrategia = "no-image"

        producto_nombre = slug_a_nombre(slug)
        if not producto_url and slug: producto_url = f"{BASE_PDP}/{slug}/"

        p_actual, p_original, marca, categoria, nombre_real, estado = 0.0, 0.0, "", "", "", "Disponible"
        if producto_url:
            p_actual, p_original, marca, categoria, nombre_real, estado = self.extraer_datos_pdp(producto_url)

        if nombre_real: producto_nombre = nombre_real

        return imagenes_url, producto_url, producto_nombre, estrategia, p_actual, p_original, marca, categoria, estado

client = LiverpoolWorkerClient()

app = FastAPI()

@app.get("/")
@app.get("/health")
async def root_health():
    return {"status": "ok", "service": "worker"}

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
