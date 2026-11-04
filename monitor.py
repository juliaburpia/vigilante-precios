#!/usr/bin/env python3
"""
Vigilante de errores de precio.
Recorre tiendas online, guarda el historial de precios y avisa por Telegram
cuando un producto aparece muchísimo más barato de lo normal.
"""
import html
import json
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

CARPETA = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(CARPETA, "config.json"), encoding="utf-8"))
ARCHIVO_PRECIOS = os.path.join(CARPETA, "precios.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
REPORTE = os.environ.get("REPORTE", "0") == "1"

INICIO = time.time()
ESTADOS = {}  # última respuesta de cada tienda, para el reporte
DEBUG = {}
TIEMPO_MAXIMO = CONFIG.get("tiempo_maximo_segundos", 420)

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "es-UY,es;q=0.9",
})


def queda_tiempo():
    return time.time() - INICIO < TIEMPO_MAXIMO


def pedir(url):
    try:
        r = S.get(url, timeout=20)
        if r.ok:
            return r
        ESTADOS[urlparse(url).netloc] = f"error {r.status_code}"
    except requests.RequestException as e:
        ESTADOS[urlparse(url).netloc] = type(e).__name__
    return None


def json_de(r):
    try:
        return r.json()
    except ValueError:
        return None


# ---------------------------------------------------------------- lectores --
# Cada lector devuelve una lista de dicts: id, nombre, precio, moneda, url

def leer_shopify(t):
    base, out = t["url"], []
    for pag in range(1, t.get("max_paginas", 40) + 1):
        if not queda_tiempo():
            break
        r = pedir(f"{base}/products.json?limit=250&page={pag}")
        datos = json_de(r) if r else None
        prods = (datos or {}).get("products") if isinstance(datos, dict) else None
        if not prods:
            break
        for p in prods:
            for v in p.get("variants", [])[:1]:
                if v.get("available") is False:
                    continue
                try:
                    precio = float(v["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                out.append(dict(id=f"s{v.get('id')}", nombre=p.get("title", ""),
                                precio=precio, moneda=None,
                                url=f"{base}/products/{p.get('handle')}"))
    return out


def leer_woocommerce(t):
    base, out = t["url"], []
    for pag in range(1, t.get("max_paginas", 40) + 1):
        if not queda_tiempo():
            break
        r = pedir(f"{base}/wp-json/wc/store/v1/products?per_page=100&page={pag}")
        prods = json_de(r) if r else None
        if not isinstance(prods, list) or not prods:
            break
        for p in prods:
            pz = p.get("prices") or {}
            try:
                precio = int(pz["price"]) / 10 ** int(pz.get("currency_minor_unit", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if p.get("is_in_stock") is False:
                continue
            out.append(dict(id=f"w{p.get('id')}", nombre=html.unescape(p.get("name", "")),
                            precio=precio, moneda=pz.get("currency_code"),
                            url=p.get("permalink")))
    return out


def leer_vtex(t):
    """Si la tienda tiene 'busquedas' (ej. supermercados), solo revisa esos productos."""
    base, out, vistos = t.get("api", t["url"]), [], set()
    busquedas = t.get("busquedas", [])
    if isinstance(busquedas, str):  # nombre de una lista definida en config.json
        busquedas = CONFIG.get("listas_de_busqueda", {}).get(busquedas, [])
    consultas = ([f"&ft={requests.utils.quote(b)}" for b in busquedas]
                 + [f"&fq=C:/{c}/" for c in t.get("categorias", [])]) or [""]
    if t.get("sc"):  # canal de venta (TaTa vende online por el canal 4)
        consultas = [c + f"&sc={t['sc']}" for c in consultas]
    for q in consultas:
        out += leer_vtex_consulta(t, base, q, vistos)
    return out


def leer_vtex_consulta(t, base, q, vistos):
    out = []
    for i in range(t.get("max_paginas", 6 if q else 40)):
        if not queda_tiempo():
            break
        desde = i * 50
        if desde > 2500:  # límite de la API de VTEX
            break
        r = pedir(f"{base}/api/catalog_system/pub/products/search"
                  f"?_from={desde}&_to={desde + 49}&O=OrderByPriceASC{q}")
        prods = json_de(r) if r else None
        if not isinstance(prods, list) or not prods:
            break
        for p in prods:
            if p.get("productId") in vistos:
                continue
            vistos.add(p.get("productId"))
            try:
                oferta = p["items"][0]["sellers"][0]["commertialOffer"]
            except (KeyError, IndexError, TypeError):
                continue
            if not oferta.get("AvailableQuantity"):
                continue
            out.append(dict(id=f"v{p.get('productId')}", nombre=p.get("productName", ""),
                            precio=float(oferta.get("Price") or 0), moneda=None,
                            url=(f"{t['url']}/{p.get('linkText')}-{p['items'][0].get('itemId')}/p"
                                 if t.get("api") else p.get("link"))))
    return out


INICIO_FENICIO = re.compile(r'\{"sku":\{"fen"')
BLOQUE_JSONLD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def productos_fenicio(texto):
    """Tiendas hechas con Fenicio (ZonaTecno y muchas uruguayas) traen un JSON por producto."""
    t, dec, out = html.unescape(texto), json.JSONDecoder(), []
    for m in INICIO_FENICIO.finditer(t):
        try:
            obj, _ = dec.raw_decode(t, m.start())
        except ValueError:
            continue
        var = obj.get("variante") or {}
        if obj.get("precioMonto") is None:  # tieneStock no es confiable en los listados
            continue
        out.append(dict(
            id="f" + str(var.get("codigoCompleto") or (obj.get("sku") or {}).get("com")),
            nombre=obj.get("nombreCompleto") or obj.get("nombre", ""),
            precio=float(obj["precioMonto"]),
            moneda=(obj.get("moneda") or {}).get("cod"),
            url=var.get("url")))
    return out


def productos_jsonld(texto, url_pagina):
    out = []
    for bloque in BLOQUE_JSONLD.findall(texto):
        try:
            datos = json.loads(bloque.strip())
        except ValueError:
            continue
        pila = [datos]
        while pila:
            d = pila.pop()
            if isinstance(d, list):
                pila.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            tipo = str(d.get("@type", ""))
            if "@graph" in d:
                pila.append(d["@graph"])
            if "ItemList" in tipo:
                for e in d.get("itemListElement", []):
                    if isinstance(e, dict):
                        pila.append(e.get("item", e))
            if "Product" not in tipo:
                continue
            of = d.get("offers")
            if isinstance(of, list):
                of = of[0] if of else None
            if not isinstance(of, dict):
                continue
            if "OutOfStock" in str(of.get("availability", "")):
                continue
            try:
                precio = float(str(of.get("price") or of.get("lowPrice")).replace(",", "."))
            except ValueError:
                continue
            url = d.get("url") or of.get("url") or url_pagina
            out.append(dict(id="j" + str(d.get("sku") or url), nombre=d.get("name", ""),
                            precio=precio, moneda=of.get("priceCurrency"),
                            url=urljoin(url_pagina, url)))
    return out


def a_numero(txt):
    """'1.145' -> 1145 ; '1.234,50' -> 1234.5 ; '548' -> 548"""
    m = re.search(r"\d[\d.,]*", txt or "")
    if not m:
        return None
    n = m.group(0).rstrip(".,")
    if "," in n:
        n = n.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", n):
        n = n.replace(".", "")
    try:
        return float(n)
    except ValueError:
        return None


def productos_nopcommerce(texto, base):
    """Tiendas hechas con nopCommerce (Banifox): bloques 'product-item' con título y 'actual-price'."""
    out = []
    for blk in texto.split('class="product-item')[1:]:
        pid = re.search(r'data-productid="(\d+)"', blk)
        tit = re.search(r'class="product-title"><a href="([^"]+)">(.*?)</a>', blk, re.S)
        pre = re.search(r'class="price actual-price">(.*?)</span>', blk, re.S)
        if not (pid and tit and pre):
            continue
        precio = a_numero(pre.group(1))
        if not precio:
            continue
        out.append(dict(id="n" + pid.group(1), nombre=html.unescape(tit.group(2)).strip(),
                        precio=precio, moneda="USD" if re.search(r"U\$S|USD|US\$", pre.group(1)) else "UYU",
                        url=urljoin(base + "/", tit.group(1))))
    return out


def leer_html(t):
    """Recorre las páginas de listado indicadas, sumando ?pag=2, 3... hasta que no aparezcan productos nuevos."""
    base, vistos, out = t["url"], set(), []
    for ruta in t.get("paginas", ["/catalogo", "/ofertas", "/"]):
        inicio = urljoin(base + "/", ruta.lstrip("/"))
        param = t.get("param_pagina", "pag")
        for pag in range(1, t.get("max_paginas", 40) + 1):
            if not queda_tiempo():
                return out
            sep = "&" if "?" in inicio else "?"
            url = inicio if pag == 1 else f"{inicio}{sep}{param}={pag}"
            r = pedir(url)
            if not r:
                break
            prods = productos_fenicio(r.text) or productos_nopcommerce(r.text, base) or productos_jsonld(r.text, url)
            if not prods:
                i = r.text.find("precioMonto")
                DEBUG[urlparse(url).netloc] = r.text[max(0, i - 1500):i + 500] if i >= 0 else r.text[:3000]
                titulo = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
                ESTADOS[urlparse(url).netloc] = (f"página sin productos ({len(r.text)} bytes, status {r.status_code}, "
                    f"title: {titulo.group(1).strip()[:60] if titulo else '-'}, precioMonto: {r.text.count('precioMonto')}, "
                    f"sku: {r.text.count('sku')}, ld: {r.text.count('ld+json')}, url final: {r.url[:80]})")
            nuevos = [p for p in prods if p["id"] not in vistos]
            if not nuevos:
                break
            for p in nuevos:
                vistos.add(p["id"])
                out.append(p)
            time.sleep(0.3)
    return out


def leer_loi(t):
    """Loi usa una API propia que pide un permiso temporal (token) que entrega gratis."""
    api, out, vistos = "https://api.loi.com.uy", [], set()
    r = S.post(f"{api}/request-token", timeout=20)
    tok = r.json()["data"]["token"]
    cab = {"Authorization": f"Bearer {tok}", "Origin": "https://loi.com.uy", "Referer": "https://loi.com.uy/"}
    for cat in t.get("categorias", []):
        for pag in range(1, t.get("max_paginas", 20) + 1):
            if not queda_tiempo():
                return out
            r = S.get(f"{api}/category/{cat}/products", headers=cab, timeout=20,
                      params={"perPage": 100, "page": pag, "with_information": 1, "variantMode": "principal"})
            if not r.ok:
                ESTADOS["loi.com.uy"] = f"error {r.status_code}"
                break
            d = r.json().get("data") or {}
            for p in d.get("products") or []:
                if p.get("producto_no_disponible") or not p.get("precio_numerico") or p.get("id") in vistos:
                    continue
                vistos.add(p.get("id"))
                # Loi corta el título ("Smartwatch HONOR Watch") y pone el modelo al inicio del subtítulo ("X5i con Pantalla...")
                modelo = re.split(r"(?:^|\s+)con\s+|,|\s+-\s+|\s+\+\s+", (p.get("subtitulo") or "").strip(), maxsplit=1)[0]
                nombre = p.get("titulo", "") + (f" {modelo}" if modelo and len(modelo) <= 25 else "")
                fab = p.get("fabricante")
                marca = fab.get("nombre") if isinstance(fab, dict) else fab
                out.append(dict(id=f"l{p.get('id')}", nombre=nombre, marca=marca,
                                precio=float(p["precio_numerico"]),
                                moneda="USD" if "U" in str(p.get("moneda", "")).upper() else "UYU",
                                url=f"https://loi.com.uy/{p.get('urlseo') or p.get('slug')}"))
            if pag >= (d.get("pagination") or {}).get("lastPage", 1):
                break
    return out


def leer_carlos_gutierrez(t):
    """Carlos Gutiérrez: API propia. Recorre todas las subfamilias del menú y toma el precio contado."""
    api, out, vistos = "https://api.carlosgutierrez.com.uy/CGSAWebApiV2/api", [], set()
    r = pedir(f"{api}/home/menus")
    menus = json_de(r) if r else None
    subs = [s.get("SFAID") for f in (menus or []) for s in (f.get("SubFamiliaArticulos") or [])]
    for sid in subs:
        if not queda_tiempo():
            break
        r = pedir(f"{api}/articulos/Agrupados/?idSubFamilia={sid}&idGrupo=0")
        for a in (json_de(r) if r else None) or []:
            if a.get("ArtId") in vistos or not a.get("HabilitadoVenta", True) or not a.get("Stock"):
                continue
            precios = [p.get("PViPrecio") for p in a.get("PrecioVigente") or [] if p.get("PViPrecio")]
            if not precios:
                continue
            vistos.add(a.get("ArtId"))
            out.append(dict(id=f"cg{a['ArtId']}", nombre=(a.get("ArtNombre") or "").strip(),
                            marca=((a.get("Marca") or {}).get("MarNombre") or "").strip(),
                            precio=float(min(precios)), moneda="UYU",
                            url=f"https://www.carlosgutierrez.com.uy/products/{a['ArtId']}"))
    return out


def slug(texto):
    import unicodedata
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def leer_sodimac(t):
    """Sodimac trae los productos dentro de la página (__NEXT_DATA__). Recorre las categorías del inicio."""
    base, out, vistos = t["url"], [], set()
    cats = t.get("categorias")
    if not cats:
        inicio = pedir(f"{base}/sodimac-uy/")
        cats = sorted(set(re.findall(r"/sodimac-uy/category/cat\d+/[a-z0-9\-]+/", inicio.text))) if inicio else []
    for cat in cats:
        for pag in range(1, t.get("max_paginas", 4) + 1):
            if not queda_tiempo():
                return out
            r = pedir(f"{base}{cat}?currentpage={pag}")
            if not r:
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                break
            sd = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}).get("searchProps", {}).get("searchData") or {}
            for p in sd.get("results") or []:
                precios = [x for x in p.get("prices") or [] if x.get("priceWithoutFormatting")]
                if not precios or p.get("productId") in vistos:
                    continue
                vistos.add(p.get("productId"))
                mejor = min(precios, key=lambda x: x["priceWithoutFormatting"])
                out.append(dict(id=f"so{p.get('productId')}", nombre=p.get("displayName", ""),
                                precio=float(mejor["priceWithoutFormatting"]),
                                moneda="USD" if str(mejor.get("symbol", "")).upper().startswith("U") else "UYU",
                                url=f"{base}/sodimac-uy/product/{p.get('productId')}/{slug(p.get('displayName', 'x'))}/{p.get('skuId') or p.get('productId')}/"))
            pg = sd.get("pagination") or {}
            if pag * (pg.get("perPage") or 40) >= (pg.get("count") or 0):
                break
            time.sleep(0.3)
    return out


def leer_odoo(t):
    """Tiendas hechas con Odoo (Tienda Montevideo Portal): /shop/page/N, 20 productos por página.
    Pasada la última página, Odoo repite la última: se corta cuando no aparecen productos nuevos."""
    base, out, vistos = t["url"], [], set()
    lote, pag = t.get("paginas_a_la_vez", 6), 1
    while pag <= t.get("max_paginas", 600) and queda_tiempo():
        urls = [f"{base}/shop" + (f"/page/{n}" if n > 1 else "") for n in range(pag, pag + lote)]
        with ThreadPoolExecutor(max_workers=lote) as ex:
            respuestas = list(ex.map(pedir, urls))
        nuevos = 0
        for r in respuestas:
            if not r:
                continue
            for blk in r.text.split('class="oe_product_cart"')[1:]:
                url = re.search(r'itemprop="name" href="([^"?]+)', blk)
                nom = re.search(r'itemprop="name"[^>]*>([^<]+)</a>', blk)
                pre = re.search(r'itemprop="price"[^>]*>([\d.]+)<', blk)
                mon = re.search(r'itemprop="priceCurrency"[^>]*>([A-Z]+)<', blk)
                if not (url and nom and pre):
                    continue
                pid = url.group(1).rsplit("-", 1)[-1]
                if pid in vistos:
                    continue
                vistos.add(pid)
                nuevos += 1
                out.append(dict(id=f"o{pid}", nombre=html.unescape(nom.group(1)).strip(),
                                precio=float(pre.group(1)), moneda=mon.group(1) if mon else None,
                                url=urljoin(base + "/", url.group(1))))
        if not nuevos:
            break
        pag += lote
    return out


LECTORES = {"shopify": leer_shopify, "odoo": leer_odoo, "woocommerce": leer_woocommerce,
            "vtex": leer_vtex, "html": leer_html, "loi": leer_loi, "sodimac": leer_sodimac,
            "carlos_gutierrez": leer_carlos_gutierrez}


def detectar_tipo(t):
    """Prueba qué tipo de tienda es, del más fácil de leer al más difícil."""
    base = t["url"]
    r = pedir(f"{base}/products.json?limit=1")
    if r and isinstance(json_de(r), dict) and "products" in json_de(r):
        return "shopify"
    r = pedir(f"{base}/wp-json/wc/store/v1/products?per_page=1")
    if r and isinstance(json_de(r), list):
        return "woocommerce"
    r = pedir(f"{base}/api/catalog_system/pub/products/search?_from=0&_to=0")
    if r and isinstance(json_de(r), list) and json_de(r):
        return "vtex"
    return "html"


# ------------------------------------------------------------- detección --

EN_DOLARES = {"USD", "US$", "U$S", "U$D", "DOLARES", "DÓLARES"}


def a_pesos(precio, moneda, t):
    mon = (moneda or t.get("moneda") or "UYU").upper().strip()
    return precio * CONFIG["dolar_en_pesos"] if mon in EN_DOLARES else precio


def mostrar_precio(precio, moneda, t):
    mon = (moneda or t.get("moneda") or "UYU").upper().strip()
    num = f"{precio:,.0f}".replace(",", ".")
    return f"USD {num}" if mon in EN_DOLARES else f"$ {num}"


def minimo_por_palabra(nombre):
    """Si el nombre es de un producto 'caro' (notebook, heladera...), devuelve su precio mínimo lógico."""
    n = f" {nombre.lower()} "
    if any(f" {x}" in n for x in CONFIG["palabras_excluidas"]):
        return None, None
    for palabra, minimo in CONFIG["minimos_por_palabra"].items():
        i = n.find(f" {palabra}")
        if i >= 0:
            # "Espada para motosierra", "Inflador para compresor": es un accesorio, no la máquina
            if " para " in n[:i + 1]:
                return None, None
            # El tipo de producto va al principio del nombre ("Consola Sony PS5", "Taladro Bosch").
            # Si aparece más atrás ("Gamepad Inalámbrico Bt Sony Ps5") es un accesorio de ese producto.
            if len(n[:i + 1].split()) >= 3:
                continue
            return palabra, minimo
    return None, None


def esc(s):
    return html.escape(str(s or ""), quote=False)


PALABRAS_VACIAS = {"de", "con", "para", "y", "el", "la", "los", "las", "en", "x", "-", "+", "t"}


CAPACIDAD = re.compile(r"\d+(gb|tb|mb)")


def familia(nombre, marca=None):
    """Agrupa solo productos del mismo tipo, marca y modelo.
    'Smartwatch HONOR Watch X5i' y 'Smartwatch HONOR Watch 6' quedan en grupos distintos,
    pero 'Pendrive SanDisk Fortnite 64GB' y '... 128GB' quedan juntos (solo cambia la capacidad)."""
    palabras = [w for w in re.findall(r"[a-záéíóúñü0-9]+", nombre.lower()) if w not in PALABRAS_VACIAS]
    texto = [w for w in palabras if not re.search(r"\d", w)][:3]
    modelo = sorted({w for w in palabras if re.search(r"\d", w) and not CAPACIDAD.fullmatch(w)})
    if len(texto) < 2:
        return None
    marca = (marca or "").lower().strip()
    clave = ([marca] if marca and marca not in texto else []) + texto + modelo
    return " ".join(clave)


def precios_por_familia(t, prods):
    grupos = {}
    for p in prods:
        f = familia(p.get("nombre", ""), p.get("marca"))
        if f and p.get("precio"):
            grupos.setdefault(f, []).append((p["id"], a_pesos(p["precio"], p.get("moneda"), t), p.get("nombre", "")))
    return grupos


def le_falta_un_digito(normal, precio):
    """263 -> 26, 63 o 23: el precio nuevo es el normal con un número de menos."""
    a, b = str(round(normal)), str(round(precio))
    return len(b) == len(a) - 1 and any(a[:i] + a[i + 1:] == b for i in range(len(a)))


def tipo_de_error(normales, pesos, precio, en_usd, exacto=True):
    """Errores típicos de carga. Devuelve (tipo, precio normal en pesos) o (None, None).
    - "moneda": lo cargaron en pesos en vez de dólares (queda ~40 veces más barato).
    - "digito": le falta un número (USD 263 -> USD 26, 63 o 23)."""
    d = CONFIG["dolar_en_pesos"]
    for normal in sorted(set(normales), reverse=True):
        if pesos <= 0 or normal < CONFIG["referencia_minima_pesos"]:
            continue
        r = normal / pesos
        if d * 0.8 <= r <= d * 1.25:
            return "moneda", normal
        # comparar en la misma moneda en que lo muestra la tienda
        if exacto and r >= 2.5 and le_falta_un_digito(normal / d if en_usd else normal, precio):
            return "digito", normal
        if 9.3 <= r <= 11:
            return "digito", normal
    return None, None


def texto_error(tipo, normal, en_usd):
    d = CONFIG["dolar_en_pesos"]
    normal_txt = (f"USD {normal / d:,.0f}" if en_usd else f"$ {normal:,.0f}").replace(",", ".")
    if tipo == "moneda" and en_usd:
        return (f"💱 POSIBLE ERROR DE MONEDA: normalmente sale {normal_txt} "
                f"y ahora figura unas {d} veces más barato.")
    if tipo == "moneda":
        usd = f"USD {normal / d:,.0f}".replace(",", ".")
        return (f"💱 POSIBLE ERROR DE MONEDA: normalmente sale {normal_txt} (≈ {usd}). "
                f"Parece cargado en pesos en vez de dólares.")
    return f"🔢 POSIBLE NÚMERO MAL CARGADO (le falta un dígito): normalmente sale {normal_txt}."


def revisar_tienda(t, prods, hist, ahora):
    alertas, caidas = [], 0
    grupos = precios_por_familia(t, prods)
    for p in prods:
        if not p.get("precio") or p["precio"] <= 0 or not p.get("url"):
            continue
        clave = f"{t['nombre']}|{p['id']}"
        pesos = a_pesos(p["precio"], p.get("moneda"), t)
        h = hist.get(clave)
        motivo = None
        error_carga = None  # "moneda" o "digito": se avisa enseguida y primero
        en_usd = (p.get("moneda") or t.get("moneda") or "UYU").upper().strip() in EN_DOLARES

        if h:
            referencia = statistics.median(h["p"])
            caida = 1 - pesos / referencia if referencia else 0
            if referencia >= CONFIG["referencia_minima_pesos"] and caida >= CONFIG["caida_minima"]:
                if not h.get("a"):
                    caidas += 1
                    h["a"] = True
                antes = f"$ {referencia:,.0f}".replace(",", ".")
                motivo = f"Antes: {antes} (−{caida:.0%})"
            elif caida < CONFIG["caida_minima"] / 2:
                h["a"] = False  # volvió a la normalidad: puede volver a avisar
            # Errores de carga: se comparan contra los precios que tuvo antes (aunque la caída sea menor al 80%)
            error_carga, normal_err = tipo_de_error(h["p"], pesos, p["precio"], en_usd)
            if error_carga:
                motivo = texto_error(error_carga, normal_err, en_usd)
            if abs(pesos - h["p"][-1]) > 0.5:
                h["p"] = (h["p"] + [round(pesos, 2)])[-8:]
        else:
            h = hist[clave] = {"p": [round(pesos, 2)], "a": False}

        palabra, minimo = minimo_por_palabra(p["nombre"])
        if minimo and pesos < minimo * CONFIG.get("factor_minimos", 1):
            motivo = motivo or f"Muy barato para un/a {palabra} (menos de $ {minimo:,}".replace(",", ".") + ")"

        # Comparación con productos parecidos de la misma tienda (sirve aunque sea la primera vez que lo ve)
        otros = [(pz, n) for i, pz, n in grupos.get(familia(p["nombre"], p.get("marca")) or "", []) if i != p["id"]]
        if len(otros) >= CONFIG.get("parecidos_minimos", 1):
            normal = statistics.median(pz for pz, _ in otros)
            if normal >= CONFIG.get("valor_minimo_parecidos", 0) and pesos < normal * CONFIG.get("fraccion_vs_parecidos", 0.25):
                normal_txt = f"$ {normal:,.0f}".replace(",", ".")
                ejemplo = min(otros, key=lambda x: abs(x[0] - normal))[1]
                motivo = motivo or (f"El mismo modelo/marca en la tienda cuesta unos {normal_txt} "
                                    f"(ej: {ejemplo[:70]})")
                if not error_carga:
                    error_carga, _ = tipo_de_error([normal], pesos, p["precio"], en_usd, exacto=False)
                    if error_carga:
                        motivo = texto_error(error_carga, normal, en_usd) + f" (ej: {ejemplo[:70]})"

        h["v"] = ahora
        for viejo in ("k", "f"):
            h.pop(viejo, None)

        # Confirmar: el precio bajo tiene que aparecer en dos revisiones seguidas
        # (si la tienda muestra un precio raro una vez y después vuelve al normal, no avisa).
        if motivo and not error_carga:  # los errores de moneda/dígito pueden durar poco: van directo
            pend = h.get("pend")
            if not pend or abs(pesos - pend) > pend * 0.05:
                h["pend"] = round(pesos, 2)
                motivo = None
        elif not motivo:
            h.pop("pend", None)

        # No repetir: un producto ya avisado no se vuelve a avisar en 7 días,
        # aunque el precio suba y baje, salvo que baje todavía más (5% o más).
        if h.get("av"):
            h.setdefault("avd", ahora)  # avisos viejos (antes de este cambio): cuentan desde hoy
        if h.get("av") and h["avd"] < (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"):
            h.pop("av", None)
            h.pop("avd", None)
        if motivo and h.get("av") and pesos >= h["av"] * 0.95:
            motivo = None
        if motivo:
            h["av"] = round(pesos, 2)
            h["avd"] = ahora
            alertas.append((categoria(p["nombre"], t),
                f"{'💱' if error_carga else '🚨'} <b>{esc(t['nombre'])}</b>\n{esc(p['nombre'])}\n"
                f"Precio: <b>{mostrar_precio(p['precio'], p.get('moneda'), t)}</b>\n"
                f"{esc(motivo)}\n{esc(p['url'])}"))
    return alertas, caidas


def categoria(nombre, t):
    """hogar, ferreteria o tecnologia: primero por palabras del nombre, si no por el rubro de la tienda."""
    n = f" {nombre.lower()} "
    for cat in ("tecnologia", "ferreteria", "hogar"):
        if any(f" {w}" in n for w in CONFIG.get("palabras_por_categoria", {}).get(cat, [])):
            return cat
    return t.get("rubro", "hogar")


# ------------------------------------------------- comparar entre tiendas --

MEDIDA = re.compile(r"\d+(gb|tb|mb|w|mm|cm|mts|m|l|lt|lts|kg|gr|g|v|hz|mah|btu|cc|ml|pulgadas|hp|rpm|bar|psi)")


def codigos_modelo(nombre):
    """Códigos de modelo del nombre: 'MMB6382M', 'TAT11601', 'MDRS619FGR28'.
    Tienen letras y al menos 2 números; no cuentan medidas como '1200W' o '128GB'."""
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9\-/\.]{3,}[a-z0-9]", nombre.lower()):
        w = re.sub(r"[\-/\.]", "", w)
        if (len(w) >= 5 and re.search(r"[a-z]", w) and len(re.findall(r"\d", w)) >= 2
                and not MEDIDA.fullmatch(w)):
            out.add(w)
    return out


def mismo_producto(a, b):
    """Para no mezclar el aparato con un accesorio que tiene el mismo código ("Vaso para licuadora X")."""
    na, nb = f" {a.lower()} ", f" {b.lower()} "
    accesorio = lambda n: " para " in n or any(f" {x}" in n for x in CONFIG["palabras_excluidas"])
    return accesorio(na) == accesorio(nb)


def pesos_txt(x):
    return f"$ {x:,.0f}".replace(",", ".")


def comparar_entre_tiendas(resultados, hist, ahora):
    """Busca el mismo modelo (mismo código) en otras tiendas y avisa si en una está muchísimo más barato."""
    indice = {}
    for t, _, prods, _ in resultados:
        for p in prods:
            if not p.get("precio") or p["precio"] <= 0 or not p.get("url"):
                continue
            pesos = a_pesos(p["precio"], p.get("moneda"), t)
            for c in codigos_modelo(p.get("nombre", "")):
                indice.setdefault(c, []).append((t, p, pesos))

    alertas, revisados = [], set()
    hace_7_dias = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    for items in indice.values():
        if len({t["nombre"] for t, _, _ in items}) < 2:
            continue
        for t, p, pesos in items:
            clave = f"{t['nombre']}|{p['id']}"
            h = hist.get(clave)
            if clave in revisados or not h:
                continue
            # el precio más bajo del mismo producto en cada una de las otras tiendas
            por_tienda = {}
            for t2, p2, pz in items:
                if t2["nombre"] != t["nombre"] and mismo_producto(p["nombre"], p2["nombre"]):
                    if pz < por_tienda.get(t2["nombre"], (float("inf"),))[0]:
                        por_tienda[t2["nombre"]] = (pz, p2["nombre"])
            if not por_tienda:
                continue
            normal = statistics.median(pz for pz, _ in por_tienda.values())
            fraccion = CONFIG.get("fraccion_otras_tiendas", 0.4) if len(por_tienda) >= 2 else 0.3
            if normal < CONFIG.get("valor_minimo_otras_tiendas", 2000) or pesos >= normal * fraccion:
                h.pop("xpend", None)
                continue
            revisados.add(clave)
            en_usd = (p.get("moneda") or t.get("moneda") or "UYU").upper().strip() in EN_DOLARES
            error_carga, _ = tipo_de_error([normal], pesos, p["precio"], en_usd, exacto=False)
            if not error_carga:  # confirmar en dos revisiones seguidas
                pend = h.get("xpend")
                if not pend or abs(pesos - pend) > pend * 0.05:
                    h["xpend"] = round(pesos, 2)
                    continue
            # no repetir (comparte el registro con los otros avisos)
            if h.get("av") and h.get("avd", ahora) >= hace_7_dias and pesos >= h["av"] * 0.95:
                continue
            h["av"], h["avd"] = round(pesos, 2), ahora
            tienda_ej, (precio_ej, nombre_ej) = min(por_tienda.items(), key=lambda x: abs(x[1][0] - normal))
            otras = f"En {len(por_tienda)} otra(s) tienda(s) el mismo modelo sale unos {pesos_txt(normal)}"
            motivo = (texto_error(error_carga, normal, en_usd) + " " if error_carga else "") + otras + \
                f" (ej: {tienda_ej} a {pesos_txt(precio_ej)}: {nombre_ej[:60]})"
            alertas.append((categoria(p["nombre"], t),
                f"{'💱' if error_carga else '🔎'} <b>{esc(t['nombre'])}</b>\n{esc(p['nombre'])}\n"
                f"Precio: <b>{mostrar_precio(p['precio'], p.get('moneda'), t)}</b>\n"
                f"{esc(motivo)}\n{esc(p['url'])}"))
    return alertas


# -------------------------------------------------------------- telegram --

def telegram(texto, cat=None):
    """Manda al grupo de la categoría (config.json -> grupos_telegram); si no hay grupo, al chat del bot."""
    destino = str((CONFIG.get("grupos_telegram") or {}).get(cat) or CHAT_ID)
    if not TOKEN or not destino:
        print(f"[sin Telegram configurado] ({cat})\n" + texto + "\n")
        return
    for i in range(0, len(texto), 3900):
        try:
            S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                   data={"chat_id": destino, "text": texto[i:i + 3900],
                         "parse_mode": "HTML", "disable_web_page_preview": "false"},
                   timeout=20)
        except requests.RequestException as e:
            print("Error enviando a Telegram:", e)


# ------------------------------------------------------------------ main --

def main():
    try:
        datos = json.load(open(ARCHIVO_PRECIOS, encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    tipos = datos.setdefault("tipos", {})
    hist = datos.setdefault("productos", {})
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def leer_tienda(t):
        t["url"] = t["url"].rstrip("/")
        try:
            tipo = t.get("tipo") or tipos.get(t["nombre"]) or detectar_tipo(t)
            prods = LECTORES[tipo](t)
            if not prods and tipo != "html" and not t.get("tipo"):
                tipo, prods = "html", leer_html(t)
            return t, tipo, prods, None
        except Exception as e:  # una tienda que falla no frena a las demás
            return t, None, [], e

    activas = [t for t in CONFIG["tiendas"] if t.get("activa", True)]
    with ThreadPoolExecutor(max_workers=CONFIG.get("tiendas_a_la_vez", 6)) as ex:
        resultados = list(ex.map(leer_tienda, activas))

    resumen, todas = [], []
    for t, tipo, prods, error in resultados:
        if error:
            resumen.append(f"❌ {t['nombre']}: error {type(error).__name__}")
            print(resumen[-1])
            continue
        try:
            if prods:
                tipos[t["nombre"]] = tipo
            else:
                tipos.pop(t["nombre"], None)
            primera_vez = not any(k.startswith(t["nombre"] + "|") for k in hist)
            alertas, caidas = revisar_tienda(t, prods, hist, ahora)
            if prods and caidas > max(20, len(prods) * 0.3):
                # Demasiadas caídas juntas: casi seguro cambió de moneda o la página, no son errores reales
                todas.append((t.get("rubro", "hogar"),
                              f"⚠️ <b>{esc(t['nombre'])}</b>: {caidas} productos bajaron de golpe a la vez. "
                              f"Puede ser un cambio general de la página (o una liquidación). Revisalo: {esc(t['url'])}"))
            elif not primera_vez or CONFIG.get("avisar_primera_vez_por_minimos", True):
                todas.extend(alertas)
            resumen.append(f"{'✅' if prods else '❌'} {t['nombre']} ({tipo}): {len(prods)} productos"
                           + ("" if prods else f" [{ESTADOS.get(urlparse(t['url']).netloc, 'sin datos')}]"))
        except Exception as e:  # una tienda que falla no frena a las demás
            resumen.append(f"❌ {t['nombre']}: error {type(e).__name__}")
        print(resumen[-1])

    # el mismo modelo en otras tiendas
    try:
        todas.extend(comparar_entre_tiendas([r for r in resultados if not r[3]], hist, ahora))
    except Exception as e:
        print("Error comparando entre tiendas:", e)

    # limpiar productos que no se ven hace más de 30 días
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for k in [k for k, v in hist.items() if v.get("v", ahora) < limite]:
        del hist[k]

    # Primero los posibles errores de moneda o de dígito
    todas.sort(key=lambda a: not a[1].startswith("💱"))
    tope = CONFIG.get("maximo_avisos_por_vuelta", 15)
    for cat in ("hogar", "ferreteria", "tecnologia"):
        de_cat = [texto for c, texto in todas if c == cat]
        for texto in de_cat[:tope]:
            telegram(texto, cat)
        if len(de_cat) > tope:
            telegram(f"…y {len(de_cat) - tope} avisos más en esta vuelta.", cat)
    if REPORTE:
        telegram("📋 <b>Reporte del vigilante</b>\n" + "\n".join(esc(x) for x in resumen))
        for cat, nombre in (("hogar", "Hogar"), ("ferreteria", "Ferretería"), ("tecnologia", "Tecnología")):
            if (CONFIG.get("grupos_telegram") or {}).get(cat):
                telegram(f"✅ Grupo {nombre} conectado al vigilante. Acá te van a llegar los avisos de {nombre.lower()}.", cat)

    datos.pop("debug", None)
    with open(ARCHIVO_PRECIOS, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    main()
