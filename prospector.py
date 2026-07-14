"""
LeadForge Prospector — standalone script
Busca empresas en ciudades españolas y les manda un email con el pitch de LeadForge.
Corre cada día via GitHub Actions. No tiene nada que ver con la app LeadForge.
"""

import os
import json
import time
import re
import smtplib
import socket
import threading
import concurrent.futures
import requests
import dns.resolver
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import quote_plus

BREVO_API_KEY  = os.environ["BREVO_API_KEY"]
MY_EMAIL       = os.environ.get("MY_EMAIL") or "aquilesgbi@gmail.com"
SENDER_NAME    = os.environ.get("SENDER_NAME") or "LeadForge"
# Dominio distinto al de los clientes de pago (leadforge.es) a propósito: el
# Prospector manda cold email a desconocidos (mucho más arriesgado que las
# campañas de clientes a leads cualificados) — si comparte remitente y se
# quema la reputación, arrastra también los envíos de los clientes. cobraflow.es
# ya estaba autenticado en Brevo (SPF/DKIM) de un proyecto anterior, así que no
# hizo falta tocar DNS. Ver [[strategy_growth_plan_fable]] (2026-07-08).
PROSPECTOR_SENDER_EMAIL = os.environ.get("PROSPECTOR_SENDER_EMAIL") or "aquiles@cobraflow.es"
SENT_FILE        = "sent_emails.json"
CRM_FILE         = "crm_data.json"
WARMUP_FILE      = "warmup_state.json"
SUPPRESSION_FILE = "suppression.json"

# ══════════════════════════════════════════════════════════
# RAMPA DE ENVÍO — protege la reputación del remitente del Prospector
# (aquiles@cobraflow.es, dominio separado del de los clientes de pago desde
# el 2026-07-08, ver [[strategy_growth_plan_fable]]).
#
# REINTERPRETADA 2026-07-14 (plan Fable 5 Max): esta rampa marcaba el
# objetivo de ENVÍOS TOTALES/día, pero con la secuencia de seguimiento
# (TOQUE2_DIAS/TOQUE3_DIAS más abajo) cada lead nuevo genera ~3 envíos —
# perseguir 350-500 LEADS NUEVOS/día era un objetivo físicamente imposible
# con fuentes gratuitas en España. Ahora esta tabla es el objetivo de LEADS
# NUEVOS/día (primer contacto); el volumen total de envíos = nuevos +
# seguimientos, y ese total sí puede llegar a 300-500/día real.
# Para forzar un número fijo (saltarse la rampa), define la env var
# MAX_PER_RUN_OVERRIDE (p.ej. 120) como secret en GitHub Actions.
# ══════════════════════════════════════════════════════════
RAMP_SCHEDULE = [
    (0,  50),
    (3,  80),
    (9,  100),
    (15, 120),
]

# ══════════════════════════════════════════════════════════
# SECUENCIA DE SEGUIMIENTO (plan Fable 5 Max, 2026-07-14) — hasta hoy cada
# dominio recibía un solo email en su vida. Ahora cada lead contactado
# recibe hasta 3 toques (día 0, +4, +9) salvo que responda o pida baja.
# "Responde" no se puede detectar automáticamente: el replyTo del email es
# el Gmail personal de Aquiles, el script no lo lee. Decisión explícita de
# Aquiles (2026-07-14): supresión MANUAL — en cuanto vea una respuesta o
# baja en su bandeja, añade el email a suppression.json y lo empuja al
# repo; el próximo run ya no le vuelve a escribir. Sin eso, el kill-switch
# de abajo es la única red de seguridad automática.
# ══════════════════════════════════════════════════════════
TOQUE2_DIAS = 4
TOQUE3_DIAS = 9
FOLLOWUP_DAILY_CAP = int(os.environ.get("FOLLOWUP_MAX_PER_RUN_OVERRIDE") or 130)

# ══════════════════════════════════════════════════════════
# KILL-SWITCH — a 300-500 envíos/día desde una cuenta de Brevo que también
# sostiene los transaccionales de los clientes de pago, un pico de rebotes
# duros o quejas de spam sin frenar automáticamente puede quemar reputación
# antes de que Aquiles se entere. Se consulta el mismo aggregatedReport que
# ya usa get_brevo_remaining_today(). Umbrales del plan Fable 5 Max
# (2026-07-14): ~2% rebote duro, ~0.15% quejas, medidos sobre lo entregado
# hoy. Si se dispara, el run corta el envío (no cancela lo ya mandado) y
# avisa a MY_EMAIL — no vuelve a bajar solo, hay que revisar y quitar
# KILL_SWITCH_FORCE_OFF del entorno para reanudar al día siguiente.
# ══════════════════════════════════════════════════════════
KILL_SWITCH_HARD_BOUNCE_RATE = 0.02
KILL_SWITCH_COMPLAINT_RATE   = 0.0015

# Procesar leads uno a uno (web propia + DNS/SMTP + Brevo) es lo que más
# tiempo real consume del run — con la rampa subiendo hasta 500/día, hacerlo
# en serie arriesgaba pasarse del timeout del workflow. Cada lead es un
# dominio/servidor distinto, así que se procesan en paralelo en lotes;
# LEAD_WORKERS controla cuántos a la vez, BATCH_SIZE cada cuántos se revisa
# si ya se llegó al objetivo del día (para no pasarse de largo).
LEAD_WORKERS = 8
BATCH_SIZE   = 15
SAVE_EVERY   = 10

_lock = threading.Lock()


def _load_or_init_warmup_start():
    if os.path.exists(WARMUP_FILE):
        with open(WARMUP_FILE) as f:
            data = json.load(f)
        return datetime.strptime(data["start_date"], "%Y-%m-%d").date()
    today = datetime.now().date()
    with open(WARMUP_FILE, "w") as f:
        json.dump({"start_date": today.strftime("%Y-%m-%d")}, f)
    return today


def get_max_per_run():
    override = os.environ.get("MAX_PER_RUN_OVERRIDE")
    if override:
        return int(override)
    start = _load_or_init_warmup_start()
    dias_transcurridos = (datetime.now().date() - start).days
    objetivo = RAMP_SCHEDULE[0][1]
    for dia_umbral, valor in RAMP_SCHEDULE:
        if dias_transcurridos >= dia_umbral:
            objetivo = valor
    return objetivo


# ══════════════════════════════════════════════════════════
# CUPO COMPARTIDO DE BREVO — hola@leadforge.es es la misma cuenta que usan
# los clientes de pago de la app leadforge-api (mismo límite de 300/día en
# el plan free). Sin esto, la rampa podía intentar sus 150-500/día sin saber
# que los clientes ya se habían gastado el cupo esa mañana, y encontrarse
# con "insufficient credits" a las pocas decenas de envíos.
# ══════════════════════════════════════════════════════════
BREVO_DAILY_CAP     = 300
BREVO_SAFETY_MARGIN = 10


def _get_brevo_report_today():
    """Un único fetch del aggregatedReport de hoy, reusado por el cupo
    restante y por el kill-switch — evita pedirlo dos veces por run."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.brevo.com/v3/smtp/statistics/aggregatedReport",
            headers={"api-key": BREVO_API_KEY},
            params={"startDate": today, "endDate": today},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_brevo_remaining_today(report=None):
    """Cupo que queda hoy en la cuenta de Brevo compartida con leadforge-api,
    con colchón de seguridad. Si falla la consulta, no limita más allá del
    colchón — el límite real de Brevo actuará de todos modos si hace falta."""
    report = report if report is not None else _get_brevo_report_today()
    if report is not None:
        used = report.get("requests", 0)
        return max(0, BREVO_DAILY_CAP - BREVO_SAFETY_MARGIN - used)
    return BREVO_DAILY_CAP - BREVO_SAFETY_MARGIN


def kill_switch_triggered(report):
    """(disparado, motivo) según los umbrales del plan Fable 5 Max. Sobre
    muy poco volumen entregado (inicio del día) los ratios son ruido, así
    que exige un mínimo de entregas antes de poder disparar."""
    if not report:
        return False, ""
    delivered = report.get("delivered", 0)
    if delivered < 20:
        return False, ""
    hard_bounces = report.get("hardBounces", 0)
    complaints   = report.get("spamReports", report.get("complaints", 0))
    bounce_rate    = hard_bounces / delivered
    complaint_rate = complaints / delivered
    if bounce_rate > KILL_SWITCH_HARD_BOUNCE_RATE:
        return True, f"rebote duro {bounce_rate:.1%} > {KILL_SWITCH_HARD_BOUNCE_RATE:.1%} ({hard_bounces}/{delivered})"
    if complaint_rate > KILL_SWITCH_COMPLAINT_RATE:
        return True, f"quejas de spam {complaint_rate:.2%} > {KILL_SWITCH_COMPLAINT_RATE:.2%} ({complaints}/{delivered})"
    return False, ""


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CIUDADES = [
    "Madrid, España",
    "Barcelona, España",
    "Valencia, España",
    "Sevilla, España",
    "Bilbao, España",
    "Málaga, España",
    "Zaragoza, España",
    "Murcia, España",
    "Palma de Mallorca, España",
    "Alicante, España",
    "Granada, España",
    "Córdoba, España",
    "Valladolid, España",
    "A Coruña, España",
    "San Sebastián, España",
    "Santander, España",
    "Salamanca, España",
    "Toledo, España",
    "Burgos, España",
    "Vigo, España",
]

# Solo oficios de construcción/reformas (2026-07-08): el único caso de éxito
# real (Neopanels, paneles de revestimiento) es de este vertical — mandar el
# mismo pitch a un dentista o un abogado diluye la prueba social. Enfocar el
# marketing en un solo nicho mientras hay 1 solo cliente de referencia
# (recomendación de Fable 5, ver [[strategy_growth_plan_fable]]).
TARGETS = [
    "empresa de reformas",
    "fontanería",
    "electricista",
    "cerrajería",
    "pintor profesional",
    "carpintería",
    "cristalería",
    "climatización",
]

TARGET_LABEL = {
    "empresa de reformas":       "empresas de reformas",
    "fontanería":                "fontaneros",
    "electricista":              "electricistas",
    "cerrajería":                "cerrajeros",
    "pintor profesional":        "pintores",
    "carpintería":               "carpinteros",
    "cristalería":               "cristaleros",
    "climatización":             "empresas de climatización",
}

# ══════════════════════════════════════════════════════════
# OPENSTREETMAP (Overpass API) — reemplaza a Google Maps
# ══════════════════════════════════════════════════════════
# Dato abierto, sin coste ni cuota de pago — mismo enfoque ya validado en
# leadforge-api tras el incidente de facturación de Google Maps Platform
# (2026-07-02). Todos los TARGETS de abajo son negocios locales con
# necesidad real y constante de captar clientes nuevos (nada de agencias
# de marketing/publicidad — esas son competencia directa de LeadForge,
# no clientes) y con tag OSM físico bien poblado (craft/office/amenity),
# a diferencia de los "servicios de oficina abstractos" (consultoría,
# telecomunicaciones, formación genérica) que antes se saltaban por falta
# de equivalente OSM y no aportaban volumen.
OSM_HEADERS = {"User-Agent": "LeadForgeProspector/1.0 (contacto: hola@leadforge.es)"}

OSM_TAGS_MAP = {
    "empresa de reformas":     [("craft", "builder")],
    "fontanería":              [("craft", "plumber")],
    "electricista":            [("craft", "electrician")],
    "cerrajería":              [("shop", "locksmith"), ("craft", "locksmith")],
    "pintor profesional":      [("craft", "painter")],
    # Añadidos 2026-07-13: densidad real en OSM comprobada vía Overpass antes
    # de añadirlos (Madrid/Barcelona) — carpenter y hvac dan un volumen
    # comparable a builder/plumber; glaziery algo menos pero mejor que painter
    # (que ya sabíamos que apenas rendía). Se descartan roofer/tiler/plasterer
    # por tener una densidad casi nula (0-1 por ciudad), igual de mal que painter.
    "carpintería":             [("craft", "carpenter")],
    "cristalería":             [("craft", "glaziery")],
    "climatización":           [("craft", "hvac")],
}


# ══════════════════════════════════════════════════════════
# EUROPAGES — fuente nacional gratis (sin ScraperAPI), complementa OSM
# ══════════════════════════════════════════════════════════
# Añadido 2026-07-13, misma fuente gratuita que ya usa leadforge_scraper.py
# (leadforge-api) para clientes de pago. Descartadas del resto de fuentes
# gratis de ese repo: Habitissimo (nunca da email ni web propia — solo perfil
# de directorio, sin ficha que visitar para sacar contacto real, confirmado
# revisando su código) y Sortlist (agencias de marketing, sector irrelevante
# y competencia directa de LeadForge). LinkedIn/Instagram por Google dork
# también descartadas: scrapean resultados de Google directamente, muy frágil
# (Google bloquea/CAPTCHA esto fácilmente) para un cron diario.
#
# A diferencia de OSM, Europages NO es una fuente por ciudad — es un
# directorio paneuropeo con búsqueda nacional, así que se consulta una sola
# vez por oficio (no 20 veces, una por ciudad). Cada ficha de empresa trae
# un bloque JSON-LD (schema.org Organization) con teléfono/email/web reales.
#
# Probado en vivo antes de portarlo (2026-07-13): ?country=ES en la URL de
# búsqueda NO filtra de verdad (mismo bug ya conocido en leadforge-api,
# confirmado otra vez: búsquedas devolvieron negocios franceses/británicos
# mezclados). La página de resultados trae, en un bloque JSON-LD ItemList
# aparte, el país real de cada empresa — se usa para descartar las que no
# son de España antes de gastar una petición en visitar su ficha.
EUROPAGES_TERMS_MAP = {
    "empresa de reformas": ["empresa reformas", "reformas integrales"],
    "fontanería":          ["fontanero", "empresa fontanería"],
    "electricista":        ["electricista", "empresa electricidad"],
    "cerrajería":          ["cerrajero", "empresa cerrajería"],
    "pintor profesional":  ["pintor", "empresa pintura"],
    "climatización":       ["instalador aire acondicionado", "empresa climatización"],
    "carpintería":         ["carpintero", "empresa carpintería"],
    "cristalería":         ["cristalero", "empresa cristalería"],
}

EUROPAGES_PAGINAS_POR_TERMINO = 2  # conservador — es gratis pero cada página tarda, y esto corre una sola vez por oficio, no por ciudad


def _europages_get(url, timeout=15):
    try:
        r = requests.get(url, headers=OSM_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        return r
    except Exception:
        return None


def _europages_country_map(html):
    """El país real de cada empresa de la página de resultados, sacado del
    bloque JSON-LD ItemList — el ?country= de la URL no filtra de verdad."""
    mapa = {}
    for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict) or item.get("@type") != "ItemList":
                continue
            for el in item.get("itemListElement", []) or []:
                org = el.get("item", {}) if isinstance(el, dict) else {}
                url = org.get("url", "")
                direccion = org.get("address") or {}
                pais_real = direccion.get("addressCountry", "")
                ciudad_real = direccion.get("addressLocality", "")
                if url and pais_real:
                    mapa[url] = (pais_real, ciudad_real)
    return mapa


def _europages_contacto_desde_ficha(fuente_url):
    """La ficha de cada empresa trae un bloque JSON-LD (schema.org
    Organization) con teléfono, email y web real — no hace falta adivinar
    selectores."""
    r = _europages_get(fuente_url, timeout=12)
    if not r:
        return {}
    contacto = {}
    for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict) or item.get("@type") != "Organization":
                continue
            for cp in item.get("contactPoint", []) or []:
                if not contacto.get("email") and cp.get("email"):
                    contacto["email"] = cp["email"]
            web = item.get("sameAs", "")
            if isinstance(web, list):
                web = web[0] if web else ""
            if web:
                contacto["web"] = web
    return contacto


def buscar_europages(target):
    """Busca candidatos de un oficio en toda España de una sola vez (no por
    ciudad). Devuelve leads ya en el mismo formato que search_osm()."""
    terminos = EUROPAGES_TERMS_MAP.get(target)
    if not terminos:
        return []

    candidatos = []  # (nombre, fuente_url, ciudad_real)
    vistos_url = set()
    for term in terminos:
        for page in range(1, EUROPAGES_PAGINAS_POR_TERMINO + 1):
            url = f"https://www.europages.es/empresas/pg-{page}/{quote_plus(term)}.html?country=ES"
            r = _europages_get(url)
            if not r:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select('[data-test="company"]')
            if not cards:
                break

            cmap = _europages_country_map(r.text)

            for card in cards:
                link_el = card.select_one('[data-test="company-name"]')
                if not link_el:
                    continue
                nombre_el = card.select_one('[data-test="company-name"] h2') or card.select_one("h2, h3")
                nombre = clean(nombre_el.get_text()) if nombre_el else ""
                if not nombre:
                    continue
                href = link_el.get("href", "")
                fuente_url = f"https://www.europages.es{href}" if href.startswith("/") else href
                if not fuente_url or fuente_url in vistos_url:
                    continue

                pais_real, ciudad_real = cmap.get(fuente_url, ("", ""))
                if pais_real != "ES":
                    continue  # descarta negocios de fuera de España (el ?country= no filtra de verdad)

                vistos_url.add(fuente_url)
                candidatos.append((nombre, fuente_url, ciudad_real or "España"))

            time.sleep(1)

    if not candidatos:
        return []

    # Segunda pasada en paralelo: visitar cada ficha para sacar email/web real.
    leads = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_europages_contacto_desde_ficha, fu): (nombre, fu, ciudad_real)
            for nombre, fu, ciudad_real in candidatos
        }
        for fut in concurrent.futures.as_completed(futures):
            nombre, fuente_url, ciudad_real = futures[fut]
            try:
                contacto = fut.result()
            except Exception:
                contacto = {}

            website = contacto.get("web", "")
            email_directo = _clean_email((contacto.get("email") or "").lower())
            if not website and not email_directo:
                continue

            if website:
                domain = (
                    website.replace("https://", "").replace("http://", "")
                    .replace("www.", "").split("/")[0].lower()
                )
                if not dominio_valido(domain):
                    continue
                email = email_directo or f"info@{domain}"
            else:
                email_domain = email_directo.split("@")[-1]
                if not dominio_valido(email_domain):
                    continue
                domain = email_directo if email_domain in PROVEEDORES_EMAIL_GENERICOS else email_domain
                website = ""
                email = email_directo

            leads.append({
                "nombre": nombre,
                "web":    website,
                "domain": domain,
                "email":  email,
                "target": target,
                "ciudad": ciudad_real,
            })

    if leads:
        print(f"  [Europages] {target}: {len(leads)} candidatos con contacto real")
    return leads


def clean(text):
    if not text:
        return ""
    return " ".join(str(text).strip().split())


_osm_area_cache = {}


def _osm_area_id(ciudad):
    """Geocodifica la ciudad vía Nominatim para sacar el area id real de
    Overpass — buscar solo por nombre en Overpass es ambiguo. Cachea por
    ciudad para no regeocodificar en cada categoría del mismo run."""
    if ciudad in _osm_area_cache:
        return _osm_area_cache[ciudad]

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": ciudad, "format": "json", "addressdetails": 1, "limit": 5},
            headers=OSM_HEADERS, timeout=15,
        )
        candidatos = r.json()
    except Exception as e:
        print(f"  [OSM] Error geocodificando {ciudad}: {e}")
        _osm_area_cache[ciudad] = None
        return None

    tipos_buenos = {"city", "town", "village", "municipality"}
    elegido = next(
        (c for c in candidatos if c.get("osm_type") == "relation" and c.get("addresstype") in tipos_buenos),
        None,
    )
    if not elegido:
        elegido = next((c for c in candidatos if c.get("osm_type") == "relation"), None)

    if not elegido:
        print(f"  [OSM] No se encontró área OSM para {ciudad}")
        _osm_area_cache[ciudad] = None
        return None

    area_id = 3600000000 + int(elegido["osm_id"])
    _osm_area_cache[ciudad] = area_id
    time.sleep(1)  # cortesía Nominatim: máx. 1 request/segundo
    return area_id


_osm_elements_cache = {}


def _osm_elements(area_id, tags):
    """Cachea los elementos crudos de Overpass por (área, tags) — varios
    TARGETS distintos (p.ej. 'agencia de marketing digital' y 'agencia de
    publicidad') comparten el mismo tag OSM, y sin este cache se lanzaba la
    misma consulta dos veces por ciudad al servidor público.

    Reintenta una vez en 429/504 — confirmado en vivo 2026-07-13: al pasar
    de 5 a 8 oficios (100→160 consultas/run) el servidor público de Overpass
    empezó a rechazar/tumbar casi la mitad de las consultas (48%: 32 HTTP 429
    + 44 HTTP 504 en un solo run). Antes se rendía a la primera, perdiendo
    candidatos reales que solo hacía falta volver a pedir un momento después."""
    key = (area_id, tags)
    if key in _osm_elements_cache:
        return _osm_elements_cache[key]

    filtros = "".join(f'node["{k}"="{v}"](area.a);way["{k}"="{v}"](area.a);' for k, v in tags)
    query = f'[out:json][timeout:50];area({area_id})->.a;({filtros});out center tags 200;'

    for intento in range(2):
        time.sleep(1.5)  # margen entre llamadas al servidor público de Overpass
        try:
            r = requests.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query}, headers=OSM_HEADERS, timeout=55,
            )
        except Exception as e:
            if intento == 0:
                continue
            print(f"  [OSM] Error: {e} tags={tags!r} area={area_id}")
            _osm_elements_cache[key] = []
            return []

        if r.status_code == 200:
            elements = r.json().get("elements", [])
            _osm_elements_cache[key] = elements
            return elements

        if r.status_code in (429, 504) and intento == 0:
            try:
                espera = min(int(r.headers.get("Retry-After", 8)), 15)
            except ValueError:
                espera = 8
            print(f"  [OSM] HTTP {r.status_code} — reintentando en {espera}s")
            time.sleep(espera)
            continue

        print(f"  [OSM] Error HTTP {r.status_code} tags={tags!r} area={area_id}")
        _osm_elements_cache[key] = []
        return []

    _osm_elements_cache[key] = []
    return []


def search_osm(target, ciudad):
    tags = OSM_TAGS_MAP.get(target)
    if not tags:
        print(f"  [OSM] Saltando '{target}' — sin categoría OSM equivalente")
        return []

    area_id = _osm_area_id(ciudad)
    if not area_id:
        return []

    leads = []
    vistos = set()
    for el in _osm_elements(area_id, tuple(tags)):
        t = el.get("tags", {}) or {}
        nombre = t.get("name", "").strip()
        if not nombre or nombre in vistos:
            continue
        vistos.add(nombre)

        website   = t.get("website") or t.get("contact:website") or ""
        email_tag = _clean_email((t.get("email") or t.get("contact:email") or "").lower())

        # Antes se exigía "website" sí o sí, y se perdían negocios que en OSM
        # solo tienen tag de email (frecuente en oficios locales: fontaneros,
        # electricistas...). Ahora basta con tener uno de los dos.
        if not website and not email_tag:
            continue

        if website:
            domain = (
                website.replace("https://", "").replace("http://", "")
                .replace("www.", "").split("/")[0].lower()
            )
            if not dominio_valido(domain):
                continue
            email = email_tag or f"info@{domain}"
            clave_dedup = domain
        else:
            # Solo hay tag de email, sin web propia — no hay dominio de empresa
            # que visitar/enriquecer. Si es un proveedor de correo genérico
            # (gmail, hotmail...) el "dominio" no identifica a la empresa, así
            # que se deduplica por el email completo en vez de por dominio.
            email_domain = email_tag.split("@")[-1]
            if not dominio_valido(email_domain):
                continue
            clave_dedup = email_tag if email_domain in PROVEEDORES_EMAIL_GENERICOS else email_domain
            website = ""
            email = email_tag

        leads.append({
            "nombre":  nombre,
            "web":     website,
            "domain":  clave_dedup,
            "email":   email,
            "target":  target,
            "ciudad":  ciudad,
        })

    return leads

SUBJECTS = [
    "Clientes nuevos en {ciudad} — sin publicidad",
    "{nombre}, hay contactos en {ciudad} esperándote",
    "Cómo conseguir clientes en {ciudad} sin llamadas en frío",
    "20 empresas en {ciudad} que podrían contratarte",
]

DOMINIOS_INVALIDOS = {
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "youtube.com", "google.com", "wix.com", "wordpress.com",
    "blogspot.com", "weebly.com", "squarespace.com", "godaddy.com",
    "1and1.es", "jimdo.com",
}

# Proveedores de correo genéricos — cuando un negocio solo tiene tag de email
# en OSM (sin web propia) y usa uno de estos, el dominio no identifica a la
# empresa (lo comparten miles de negocios distintos), así que no sirve como
# clave de deduplicación — se deduplica por el email completo en su lugar.
PROVEEDORES_EMAIL_GENERICOS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.es",
    "live.com", "icloud.com", "hotmail.es", "aol.com",
}

_verify_cache = {}


# ══════════════════════════════════════════════════════════
# ORDEN DE CIUDADES — Madrid siempre primero, el resto rotando
# ══════════════════════════════════════════════════════════
# Antes se miraban solo 4 ciudades/día y, si no daban suficientes
# candidatos, el run se quedaba corto del objetivo aunque quedaran
# ciudades sin probar. Ahora se recorren las 20 en orden (rotado cada
# día para repartir cuál va primero) y se para en cuanto se alcanza
# max_per_run — solo se gastan de más las llamadas a Overpass/Nominatim
# de las ciudades que hagan falta ese día, nunca las 20 si no hace falta.
def get_ciudades_orden(dia):
    resto = [c for c in CIUDADES if c != "Madrid, España"]
    orden_resto = [resto[(dia + i) % len(resto)] for i in range(len(resto))]
    return ["Madrid, España"] + orden_resto


# ══════════════════════════════════════════════════════════
# PERSISTENCIA
# ══════════════════════════════════════════════════════════
def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE) as f:
            return set(json.load(f))
    return set()


def save_sent(sent):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent), f)


def load_crm():
    if os.path.exists(CRM_FILE):
        with open(CRM_FILE) as f:
            return json.load(f)
    return []


def save_crm(data):
    with open(CRM_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_suppression():
    """Lista de emails que nunca deben recibir otro toque — respondieron,
    pidieron baja, o rebotaron. Mantenimiento MANUAL (ver nota en
    TOQUE2_DIAS más arriba): Aquiles la edita a mano cuando ve algo en su
    bandeja y la empuja al repo antes del próximo run."""
    if os.path.exists(SUPPRESSION_FILE):
        with open(SUPPRESSION_FILE) as f:
            return set(json.load(f))
    return set()


# ══════════════════════════════════════════════════════════
# VALIDACIÓN DE DOMINIO
# ══════════════════════════════════════════════════════════
def dominio_valido(domain):
    if not domain or len(domain) < 4 or "." not in domain:
        return False
    if domain in DOMINIOS_INVALIDOS:
        return False
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", domain):
        return False
    if domain.split(".")[0].isdigit():
        return False
    return True


# ══════════════════════════════════════════════════════════
# EXTRAER EMAIL REAL DE LA WEB
# ══════════════════════════════════════════════════════════
_EMAIL_SKIP = {"noreply", "no-reply", "donotreply", "webmaster", "bounce", "mailer"}
_EMAIL_RE   = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')


def _clean_email(raw):
    """Quita BOM/espacios invisibles y exige un formato de email válido —
    Brevo rechaza (400) direcciones con caracteres colados del HTML, p.ej.
    'info@empresa.com\\ufeff', y antes se perdía ese envío sin aviso."""
    e = raw.strip().strip("﻿​‌‍")
    return e if _EMAIL_RE.match(e) else None


def _parse_emails_from_html(html):
    emails = []
    mailtos = re.findall(r'href=["\']mailto:([^"\'?&\s>]+)', html, re.IGNORECASE)
    for m in mailtos:
        m = _clean_email(m.lower())
        if m and not any(s in m for s in _EMAIL_SKIP):
            emails.append(m)
    if emails:
        return emails
    found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html)
    for f in found:
        f = _clean_email(f.lower())
        if f and not any(s in f for s in _EMAIL_SKIP | {"example", "sentry", "schema", "pixel"}):
            emails.append(f)
    return emails


def get_real_email(website):
    paths = ["", "/contacto", "/contact", "/sobre-nosotros", "/about"]
    for path in paths:
        url = website.rstrip("/") + path
        try:
            r = requests.get(url, timeout=5, headers=HEADERS, allow_redirects=True)
            if r.status_code != 200:
                continue
            emails = _parse_emails_from_html(r.text)
            if emails:
                return emails[0]
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════════
# VERIFICACIÓN DNS + SMTP
# ══════════════════════════════════════════════════════════
def verify_email(email):
    if email in _verify_cache:
        return _verify_cache[email]

    domain = email.split("@")[-1]

    try:
        mx_records = dns.resolver.resolve(domain, "MX", lifetime=3)
        mx_hosts = sorted([(r.preference, str(r.exchange).rstrip(".")) for r in mx_records])
    except dns.resolver.NXDOMAIN:
        _verify_cache[email] = False
        return False
    except dns.resolver.NoAnswer:
        _verify_cache[email] = False
        return False
    except Exception:
        _verify_cache[email] = True
        return True

    for _, mx_host in mx_hosts[:2]:
        try:
            with smtplib.SMTP(timeout=5) as smtp:
                smtp.connect(mx_host, 25)
                smtp.ehlo("leadforge.es")
                smtp.mail("")
                code, _ = smtp.rcpt(email)
                smtp.quit()
                result = code == 250 or (250 <= code < 500)
                _verify_cache[email] = result
                return result
        except (socket.timeout, socket.error, smtplib.SMTPConnectError):
            continue
        except Exception:
            continue

    _verify_cache[email] = True
    return True


# ══════════════════════════════════════════════════════════
# EMAIL HTML
# ══════════════════════════════════════════════════════════
def build_email(nombre_empresa, ciudad, sector_label, lead_email=""):
    ciudad_corta = ciudad.split(",")[0]
    # Tracking de conversión — sin esto no había forma de saber si el
    # prospector convierte en algo real (clics, registros), solo si Brevo
    # decía que se abrió el email.
    demo_url = (
        "https://cobraflow0.github.io/leadforge-app/app.html"
        f"?demo=true&utm_source=prospector&lead={quote_plus(lead_email)}"
    )
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:40px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08);">

  <!-- CABECERA -->
  <tr><td style="background:linear-gradient(135deg,#0D1420,#1a2540);padding:24px 40px;">
    <h1 style="margin:0;color:#fff;font-size:19px;font-weight:700;letter-spacing:-0.3px;">⚡ LeadForge</h1>
    <p style="margin:4px 0 0;color:rgba(255,255,255,0.5);font-size:12px;">Generación automática de leads B2B</p>
  </td></tr>

  <!-- CUERPO -->
  <tr><td style="padding:36px 40px 28px;">

    <p style="margin:0 0 22px;font-size:15px;color:#111827;line-height:1.7;">
      Hola,
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      La mayoría de empresas de reformas y construcción en <strong>{ciudad_corta}</strong>
      dependen del boca a boca o de plataformas que se quedan con parte del margen —
      y pierden clientes potenciales cada semana porque no tienen tiempo de buscarlos uno a uno.
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      LeadForge los encuentra automáticamente — nombre, email, teléfono y web
      de cada empresa que podría contratarte — y los tiene listos en 30 segundos.
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      Si quieres verlo funcionar, aquí tienes una prueba gratuita con 20 leads reales de {ciudad_corta}:
      <a href="{demo_url}" style="color:#0066FF;font-weight:600;">prueba LeadForge gratis</a>
    </p>

    <!-- CTA -->
    <table cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
      <tr><td style="background:linear-gradient(135deg,#0066FF,#0052cc);border-radius:8px;box-shadow:0 4px 16px rgba(0,102,255,0.3);">
        <a href="{demo_url}"
           style="display:inline-block;padding:15px 36px;color:#fff;text-decoration:none;font-weight:700;font-size:15px;letter-spacing:0.2px;">
          Ver mis leads gratis →
        </a>
      </td></tr>
    </table>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      A una empresa de reformas (paneles de revestimiento) le conseguimos 1.309 leads cualificados
      de su sector y 3 clientes nuevos el primer día de campaña, sin llamadas en frío ni publicidad.
    </p>

    <p style="margin:0 0 6px;font-size:14px;color:#6b7280;line-height:1.7;">
      Planes desde <strong>19€/mes</strong>. Sin permanencia.
    </p>

    <p style="margin:24px 0 0;font-size:14px;color:#374151;line-height:1.8;">
      Un saludo,<br>
      <strong>Aquiles</strong><br>
      <span style="color:#9ca3af;font-size:13px;">Fundador · LeadForge · hola@leadforge.es</span>
    </p>

  </td></tr>

  <!-- PIE -->
  <tr><td style="background:#f9fafb;padding:14px 40px;border-top:1px solid #e5e7eb;text-align:center;">
    <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
      ¿No es para ti? Responde a este email y no volvemos a escribirte.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


# ══════════════════════════════════════════════════════════
# ENVÍO
# ══════════════════════════════════════════════════════════
def send_email(to_email, nombre_empresa, ciudad, sector_label, dia):
    nombre_corto = nombre_empresa.split()[0] if nombre_empresa else "equipo"
    ciudad_corta = ciudad.split(",")[0]
    subject_tpl  = SUBJECTS[dia % len(SUBJECTS)]
    subject = subject_tpl.format(
        nombre=nombre_corto,
        sector=sector_label,
        ciudad=ciudad_corta,
    )
    payload = {
        "sender":      {"name": SENDER_NAME, "email": PROSPECTOR_SENDER_EMAIL},
        "replyTo":     {"email": MY_EMAIL},
        "to":          [{"email": to_email}],
        "subject":     subject,
        "htmlContent": build_email(nombre_empresa, ciudad, sector_label, to_email),
        "tags":        ["prospector", "toque1"],
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 201):
        message_id = r.json().get("messageId", "")
        return True, message_id, subject
    print(f"    [Brevo error] status={r.status_code} body={r.text[:200]}")
    return False, None, subject


# ══════════════════════════════════════════════════════════
# SEGUIMIENTOS (toque 2 / toque 3) — plan Fable 5 Max 2026-07-14. Copy
# corto, en el mismo asunto con "Re:" para simular hilo, ofreciendo
# directamente la muestra piloto (25-50 leads de su zona en 48h) en vez de
# repetir el pitch largo del primer email — es lo que de verdad acorta el
# ciclo de respuesta según el propio plan.
# ══════════════════════════════════════════════════════════
FOLLOWUP_COPY = {
    2: {
        "parrafo": (
            "Te escribí hace unos días — no sé si te llegó o simplemente no ha sido "
            "el momento. Te dejo la oferta en dos líneas: te preparo gratis una muestra "
            "de 25-50 leads reales de tu zona en 48h, sin compromiso. Si te interesa, "
            "responde a este email y te la mando yo mismo."
        ),
        "cta": "Quiero mi muestra gratis →",
    },
    3: {
        "parrafo": (
            "Último aviso por mi parte — no quiero insistir más de la cuenta. Si en algún "
            "momento te hace falta captar clientes nuevos sin depender solo del boca a boca, "
            "aquí me tienes. Si no es para ti, no hace falta que respondas, no te vuelvo a escribir."
        ),
        "cta": "Ver cómo funciona →",
    },
}


def build_followup_email(nombre_empresa, ciudad, numero_toque, lead_email=""):
    ciudad_corta = ciudad.split(",")[0]
    demo_url = (
        "https://cobraflow0.github.io/leadforge-app/app.html"
        f"?demo=true&utm_source=prospector_followup{numero_toque}&lead={quote_plus(lead_email)}"
    )
    copy = FOLLOWUP_COPY[numero_toque]
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:40px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08);">

  <tr><td style="padding:32px 40px 24px;">

    <p style="margin:0 0 18px;font-size:15px;color:#111827;line-height:1.7;">
      Hola,
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      {copy['parrafo']}
    </p>

    <table cellpadding="0" cellspacing="0" style="margin:0 0 24px;">
      <tr><td style="background:linear-gradient(135deg,#0066FF,#0052cc);border-radius:8px;">
        <a href="{demo_url}"
           style="display:inline-block;padding:13px 28px;color:#fff;text-decoration:none;font-weight:700;font-size:14px;">
          {copy['cta']}
        </a>
      </td></tr>
    </table>

    <p style="margin:20px 0 0;font-size:14px;color:#374151;line-height:1.8;">
      Un saludo,<br>
      <strong>Aquiles</strong><br>
      <span style="color:#9ca3af;font-size:13px;">Fundador · LeadForge</span>
    </p>

  </td></tr>

  <tr><td style="background:#f9fafb;padding:14px 40px;border-top:1px solid #e5e7eb;text-align:center;">
    <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
      ¿No es para ti? Responde a este email y no volvemos a escribirte.
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def send_followup_email(to_email, nombre_empresa, ciudad, numero_toque, asunto_original):
    subject = "Re: " + asunto_original
    payload = {
        "sender":      {"name": SENDER_NAME, "email": PROSPECTOR_SENDER_EMAIL},
        "replyTo":     {"email": MY_EMAIL},
        "to":          [{"email": to_email}],
        "subject":     subject,
        "htmlContent": build_followup_email(nombre_empresa, ciudad, numero_toque, to_email),
        "tags":        ["prospector", f"toque{numero_toque}"],
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 201):
        return True, r.json().get("messageId", "")
    print(f"    [Brevo error toque{numero_toque}] status={r.status_code} body={r.text[:200]}")
    return False, None


# ══════════════════════════════════════════════════════════
# PROCESAR UN LEAD (email real + verificación + envío)
# ══════════════════════════════════════════════════════════
# Se llama en paralelo desde un ThreadPoolExecutor — cada lead visita su
# propia web y su propio servidor de correo, así que no compiten entre sí.
# Lo único compartido es el estado (sent/crm/contadores), protegido por _lock.
def procesar_lead(lead, dia, sent, sent_domains, crm, crm_ids, suppression, estado):
    nombre  = lead["nombre"]
    website = lead["web"]
    domain  = lead["domain"]
    # Cada lead trae su propia ciudad (search_osm y buscar_europages ya la
    # incluyen) — antes se pasaba una "ciudad" aparte igual para todo el lote,
    # que casualmente coincidía siempre con la de OSM, pero con Europages
    # (fuente nacional, no por ciudad) cada lead tiene una ciudad distinta.
    ciudad = lead["ciudad"]

    real  = get_real_email(website) if website else None
    email = real or lead["email"]
    if real:
        print(f"  📧 Email real: {email} ({nombre})")
    elif website:
        print(f"  ↩  Usando info@: {email} ({nombre})")
    else:
        print(f"  📇 Email directo de OSM (sin web propia): {email} ({nombre})")

    if email in suppression:
        return

    if not verify_email(email):
        with _lock:
            estado["rechazados_dns"] += 1
        print(f"  ⛔ {email} — no existe, descartado")
        return

    with _lock:
        if email in sent or estado["nuevos"] >= estado["nuevos_max"]:
            return

    sector_label = TARGET_LABEL.get(lead.get("target", ""), "empresas")
    ok, message_id, asunto = send_email(email, nombre, ciudad, sector_label, dia)
    if not ok:
        print(f"  ❌ {email} — error Brevo")
        return

    with _lock:
        sent.add(email)
        sent_domains.add(domain)
        estado["nuevos"] += 1
        estado["enviados"] += 1
        if real:
            estado["emails_reales"] += 1
        if email not in crm_ids:
            crm.append({
                "email":     email,
                "nombre":    nombre,
                "web":       website,
                "ciudad":    ciudad.split(",")[0],
                "sector":    sector_label,
                "fecha":     datetime.now().strftime("%Y-%m-%d"),
                "asunto":    asunto,
                "messageId": message_id or "",
                "status":    "sent",
            })
            crm_ids.add(email)
        if estado["enviados"] % SAVE_EVERY == 0:
            save_sent(sent)
            save_crm(crm)
    print(f"  ✅ {email} ({nombre})")


# ══════════════════════════════════════════════════════════
# PROCESAR SEGUIMIENTOS (toque 2 / toque 3)
# ══════════════════════════════════════════════════════════
# No re-verifica el email (ya pasó verify_email() en el toque 1 hace días —
# decisión de Fable 5 Max: riesgo de rebote marginal, no vale la pena
# repetir la sonda SMTP para esto). Prioriza los contactos más antiguos
# primero, así el backfill de ~600 se reparte solo entre varios runs según
# el tope diario (FOLLOWUP_DAILY_CAP), sin necesidad de un puntero aparte.
#
# FILTRO POR SECTOR (añadido 2026-07-14 al probar esto en seco contra el
# crm_data.json real): crm_data.json arrastra ~380 contactos de ANTES del
# pivote a construcción (07-06/07-08) — abogados, clínicas dentales,
# fisioterapia, inmobiliarias, seguros, talleres, asesorías, incluso
# agencias de marketing digital (competencia directa, ya descartada
# explícitamente). Sin este filtro el backfill les habría mandado un
# seguimiento hablando de "empresas de reformas", contradiciendo la
# decisión de un solo vertical y arriesgando quejas de gente que no
# entiende por qué le llega esto. Solo se reactivan los sectores de
# TARGET_LABEL (los 8 oficios de construcción actuales).
SECTORES_ACTIVOS = set(TARGET_LABEL.values())


def procesar_seguimientos(crm, suppression, estado, tope):
    if tope <= 0:
        return
    hoy = datetime.now().date()

    elegibles = []
    for entry in crm:
        email = entry.get("email")
        if not email or email in suppression:
            continue
        if entry.get("sector") not in SECTORES_ACTIVOS:
            continue
        try:
            fecha_toque1 = datetime.strptime(entry["fecha"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        dias = (hoy - fecha_toque1).days

        if not entry.get("toque2_fecha") and dias >= TOQUE2_DIAS:
            elegibles.append((fecha_toque1, entry, 2))
        elif entry.get("toque2_fecha") and not entry.get("toque3_fecha") and dias >= TOQUE3_DIAS:
            elegibles.append((fecha_toque1, entry, 3))

    elegibles.sort(key=lambda x: x[0])  # más antiguos primero

    for _, entry, numero_toque in elegibles[:tope]:
        if estado["seguimientos"] >= tope:
            break
        email  = entry["email"]
        asunto = entry.get("asunto") or f"Clientes nuevos en {entry.get('ciudad', 'tu ciudad')} — sin publicidad"
        ok, message_id = send_followup_email(email, entry.get("nombre", ""), entry.get("ciudad", ""), numero_toque, asunto)
        if not ok:
            print(f"  ❌ [toque{numero_toque}] {email} — error Brevo")
            continue
        entry[f"toque{numero_toque}_fecha"] = hoy.strftime("%Y-%m-%d")
        estado["seguimientos"] += 1
        estado["enviados"] += 1
        print(f"  ✅ [toque{numero_toque}] {email}")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main():
    dia = datetime.now().timetuple().tm_yday
    ciudades_orden = get_ciudades_orden(dia)

    sent        = load_sent()
    crm         = load_crm()
    crm_ids     = {e["email"] for e in crm}
    suppression = load_suppression()
    print(f"[prospector] {len(sent)} emails ya enviados anteriormente | {len(suppression)} en supresión")

    reporte = _get_brevo_report_today()
    disparado, motivo_kill = kill_switch_triggered(reporte)
    if disparado:
        print(f"[prospector] 🚨 KILL-SWITCH activado antes de enviar nada: {motivo_kill}")
        requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender":      {"name": SENDER_NAME, "email": "hola@leadforge.es"},
                "replyTo":     {"email": MY_EMAIL},
                "to":          [{"email": MY_EMAIL}],
                "subject":     "🚨 [Prospector] Kill-switch activado — 0 envíos hoy",
                "htmlContent": f"<p>El prospector no ha mandado nada hoy porque ya se había disparado el kill-switch: <b>{motivo_kill}</b>.</p><p>Revisa rebotes/quejas en Brevo antes del próximo run.</p>",
            },
            timeout=10,
        )
        return

    nuevos_ramp     = get_max_per_run()
    brevo_remaining = get_brevo_remaining_today(reporte)
    print(f"[prospector] Cupo restante hoy en Brevo: {brevo_remaining} | objetivo de leads nuevos (rampa/override): {nuevos_ramp} | tope de seguimientos: {FOLLOWUP_DAILY_CAP}")

    # Los seguimientos van primero — son la palanca de mayor impacto/menor
    # esfuerzo del plan (2026-07-14): reactivar ~650 contactos ya
    # verificados pesa más que perseguir leads nuevos en un pool agotado.
    seguimientos_tope = min(FOLLOWUP_DAILY_CAP, brevo_remaining)
    nuevos_max         = 0  # se calcula después de saber cuánto gastaron los seguimientos

    estado = {
        "enviados": 0, "rechazados_dns": 0, "emails_reales": 0,
        "nuevos": 0, "nuevos_max": 0, "seguimientos": 0,
        "europages_candidatos": 0,
    }

    procesar_seguimientos(crm, suppression, estado, seguimientos_tope)
    save_sent(sent)
    save_crm(crm)
    print(f"[prospector] Seguimientos enviados: {estado['seguimientos']}/{seguimientos_tope}")

    # Re-chequeo del kill-switch tras los seguimientos, antes de gastar
    # tiempo cosechando leads nuevos — Brevo tarda en reportar rebotes, así
    # que esto es una red de seguridad adicional, no la única.
    if estado["seguimientos"] >= 20:
        reporte2 = _get_brevo_report_today()
        disparado2, motivo2 = kill_switch_triggered(reporte2)
        if disparado2:
            print(f"[prospector] 🚨 KILL-SWITCH activado tras seguimientos: {motivo2} — no se cosechan leads nuevos hoy")
            nuevos_max = 0
        else:
            nuevos_max = max(0, min(nuevos_ramp, brevo_remaining - estado["seguimientos"]))
    else:
        nuevos_max = max(0, min(nuevos_ramp, brevo_remaining - estado["seguimientos"]))

    estado["nuevos_max"] = nuevos_max
    print(f"[prospector] Objetivo de leads nuevos hoy: {nuevos_max}")

    sent_domains = {e.split("@")[-1] for e in sent}

    # Recorre ciudades una a una (Madrid primero, luego el resto rotado) y
    # manda sobre la marcha — para en cuanto llega al objetivo del día, pero
    # si una ciudad no da suficientes candidatos sigue probando con la
    # siguiente en vez de rendirse, hasta agotar las 20 si hace falta.
    seen_domains    = set()
    ciudades_usadas = []

    try:
        if nuevos_max > 0:
            with concurrent.futures.ThreadPoolExecutor(max_workers=LEAD_WORKERS) as executor:

                def procesar_lote(leads_lote):
                    # Dedup por dominio en serie antes de paralelizar — así dos
                    # leads del mismo dominio nunca se procesan a la vez.
                    pendientes = []
                    for lead in leads_lote:
                        domain = lead["domain"]
                        if domain in seen_domains or domain in sent_domains:
                            continue
                        seen_domains.add(domain)
                        pendientes.append(lead)

                    for i in range(0, len(pendientes), BATCH_SIZE):
                        if estado["nuevos"] >= nuevos_max:
                            break
                        lote = pendientes[i:i + BATCH_SIZE]
                        futures = [
                            executor.submit(procesar_lead, lead, dia, sent, sent_domains, crm, crm_ids, suppression, estado)
                            for lead in lote
                        ]
                        concurrent.futures.wait(futures)

                # Europages es una fuente nacional (no por ciudad, a diferencia
                # de OSM) — se consulta una sola vez por oficio, antes del bucle
                # de ciudades, no 20 veces.
                if estado["nuevos"] < nuevos_max:
                    leads_nacionales = []
                    for target in TARGETS:
                        print(f"[prospector] Buscando en Europages: {target} (nacional)")
                        leads_nacionales.extend(buscar_europages(target))
                    estado["europages_candidatos"] = len(leads_nacionales)
                    procesar_lote(leads_nacionales)

                for ciudad in ciudades_orden:
                    if estado["nuevos"] >= nuevos_max:
                        break
                    ciudades_usadas.append(ciudad)

                    leads_ciudad = []
                    for target in TARGETS:
                        print(f"[prospector] Buscando: {target} en {ciudad}")
                        leads_ciudad.extend(search_osm(target, ciudad))
                        time.sleep(1)

                    procesar_lote(leads_ciudad)
    finally:
        save_sent(sent)
        save_crm(crm)

    enviados        = estado["enviados"]
    rechazados_dns  = estado["rechazados_dns"]
    emails_reales   = estado["emails_reales"]
    europages_cand  = estado["europages_candidatos"]
    nuevos_enviados = estado["nuevos"]
    seguimientos_enviados = estado["seguimientos"]

    print(f"\n[prospector] Fin — {enviados} enviados ({nuevos_enviados} nuevos + {seguimientos_enviados} seguimientos) | {emails_reales} emails reales | {rechazados_dns} descartados por DNS | ciudades usadas: {len(ciudades_usadas)}/{len(ciudades_orden)} | Europages: {europages_cand} candidatos")

    ciudad_str = ", ".join(c.split(",")[0] for c in ciudades_usadas) or "—"
    objetivo_cumplido = nuevos_enviados >= nuevos_max
    aviso_corto = "" if objetivo_cumplido or nuevos_max == 0 else "<p style='color:#b45309'>⚠️ No se llegó al objetivo de nuevos — se agotaron las ciudades/fuentes sin encontrar más candidatos válidos.</p>"
    resumen_html = f"""
    <p>Hoy el prospector mandó <b>{seguimientos_enviados} seguimientos</b> (toque 2/3) y buscó leads nuevos en <b>{ciudad_str}</b> ({len(ciudades_usadas)} de {len(ciudades_orden)} ciudades), más Europages a nivel nacional.</p>
    {aviso_corto}
    <ul>
      <li>✅ Enviados totales: <b>{enviados}</b></li>
      <li>🔁 Seguimientos (toque 2/3): <b>{seguimientos_enviados}</b> / tope {seguimientos_tope}</li>
      <li>🆕 Leads nuevos: <b>{nuevos_enviados}</b> / objetivo {nuevos_max}</li>
      <li>📧 Emails reales encontrados en web: <b>{emails_reales}</b></li>
      <li>⛔ Descartados por DNS/SMTP: <b>{rechazados_dns}</b></li>
      <li>📇 Candidatos de Europages (con contacto real): <b>{europages_cand}</b></li>
      <li>📬 Total acumulado contactados: <b>{len(sent)}</b></li>
      <li>🚫 En lista de supresión: <b>{len(suppression)}</b></li>
    </ul>
    """
    requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender":      {"name": SENDER_NAME, "email": "hola@leadforge.es"},
            "replyTo":     {"email": MY_EMAIL},
            "to":          [{"email": MY_EMAIL}],
            "subject":     f"[Prospector] {enviados} emails ({nuevos_enviados} nuevos + {seguimientos_enviados} seguimientos)",
            "htmlContent": resumen_html,
        },
        timeout=10,
    )


if __name__ == "__main__":
    main()
