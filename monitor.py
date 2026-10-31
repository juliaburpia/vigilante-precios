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
            prods = productos_fenicio(r.text) or productos_jsonld(r.text, url)
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
                out.append(dict(id=f"l{p.get('id')}", nombre=p.get("titulo", ""),
                                precio=float(p["precio_numerico"]),
                                moneda="USD" if "U" in str(p.get("moneda", "")).upper() else "UYU",
                                url=f"https://loi.com.uy/{p.get('urlseo') or p.get('slug')}"))
            if pag >= (d.get("pagination") or {}).get("lastPage", 1):
                break
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


LECTORES = {"shopify": leer_shopify, "woocommerce": leer_woocommerce,
            "vtex": leer_vtex, "html": leer_html, "loi": leer_loi, "sodimac": leer_sodimac}


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
        if f" {palabra}" in n:
            return palabra, minimo
    return None, None


def esc(s):
    return html.escape(str(s or ""), quote=False)


PALABRAS_VACIAS = {"de", "con", "para", "y", "el", "la", "los", "las", "en", "x", "-", "+", "t"}


def familia(nombre):
    """Primeras 3 palabras útiles del nombre: 'Pendrive SanDisk Fortnite edición...' -> 'pendrive sandisk fortnite'."""
    palabras = [w for w in re.findall(r"[a-záéíóúñü0-9]+", nombre.lower())
                if w not in PALABRAS_VACIAS and not re.fullmatch(r"\d+(gb|tb|mb)?", w)]
    return " ".join(palabras[:3]) if len(palabras) >= 3 else None


def precios_por_familia(t, prods):
    grupos = {}
    for p in prods:
        f = familia(p.get("nombre", ""))
        if f and p.get("precio"):
            grupos.setdefault(f, []).append((p["id"], a_pesos(p["precio"], p.get("moneda"), t)))
    return grupos


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

        if h:
            referencia = statistics.median(h["p"])
            caida = 1 - pesos / referencia if referencia else 0
            if referencia >= CONFIG["referencia_minima_pesos"] and caida >= CONFIG["caida_minima"]:
                if not h.get("a"):
                    caidas += 1
                    antes = f"$ {referencia:,.0f}".replace(",", ".")
                    motivo = f"Antes: {antes} (−{caida:.0%})"
                    h["a"] = True
            elif caida < CONFIG["caida_minima"] / 2:
                h["a"] = False  # volvió a la normalidad: puede volver a avisar
            if abs(pesos - h["p"][-1]) > 0.5:
                h["p"] = (h["p"] + [round(pesos, 2)])[-8:]
        else:
            h = hist[clave] = {"p": [round(pesos, 2)], "a": False}

        palabra, minimo = minimo_por_palabra(p["nombre"])
        if minimo and pesos < minimo:
            if not h.get("k"):
                motivo = motivo or f"Muy barato para un/a {palabra} (menos de $ {minimo:,}".replace(",", ".") + ")"
                h["k"] = True
        else:
            h["k"] = False

        # Comparación con productos parecidos de la misma tienda (sirve aunque sea la primera vez que lo ve)
        otros = [pz for i, pz in grupos.get(familia(p["nombre"]) or "", []) if i != p["id"]]
        if otros:
            normal = statistics.median(otros)
            if normal >= CONFIG["referencia_minima_pesos"] and pesos < normal * CONFIG.get("fraccion_vs_parecidos", 0.25):
                if not h.get("f"):
                    normal_txt = f"$ {normal:,.0f}".replace(",", ".")
                    motivo = motivo or f"Productos parecidos en la tienda cuestan unos {normal_txt}"
                    h["f"] = True
            else:
                h["f"] = False

        h["v"] = ahora
        if motivo:
            alertas.append(
                f"🚨 <b>{esc(t['nombre'])}</b>\n{esc(p['nombre'])}\n"
                f"Precio: <b>{mostrar_precio(p['precio'], p.get('moneda'), t)}</b>\n"
                f"{esc(motivo)}\n{esc(p['url'])}")
    return alertas, caidas


# -------------------------------------------------------------- telegram --

def telegram(texto):
    if not TOKEN or not CHAT_ID:
        print("[sin Telegram configurado]\n" + texto + "\n")
        return
    for i in range(0, len(texto), 3900):
        try:
            S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                   data={"chat_id": CHAT_ID, "text": texto[i:i + 3900],
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
                todas.append(f"⚠️ <b>{esc(t['nombre'])}</b>: {caidas} productos bajaron de golpe a la vez. "
                             f"Puede ser un cambio general de la página (o una liquidación). Revisalo: {esc(t['url'])}")
            elif not primera_vez or CONFIG.get("avisar_primera_vez_por_minimos", True):
                todas.extend(alertas)
            resumen.append(f"{'✅' if prods else '❌'} {t['nombre']} ({tipo}): {len(prods)} productos"
                           + ("" if prods else f" [{ESTADOS.get(urlparse(t['url']).netloc, 'sin datos')}]"))
        except Exception as e:  # una tienda que falla no frena a las demás
            resumen.append(f"❌ {t['nombre']}: error {type(e).__name__}")
        print(resumen[-1])

    # limpiar productos que no se ven hace más de 30 días
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for k in [k for k, v in hist.items() if v.get("v", ahora) < limite]:
        del hist[k]

    tope = CONFIG.get("maximo_avisos_por_vuelta", 15)
    for a in todas[:tope]:
        telegram(a)
    if len(todas) > tope:
        telegram(f"…y {len(todas) - tope} avisos más en esta vuelta.")
    if REPORTE:
        telegram("📋 <b>Reporte del vigilante</b>\n" + "\n".join(esc(x) for x in resumen))

    datos.pop("debug", None)
    with open(ARCHIVO_PRECIOS, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    main()
