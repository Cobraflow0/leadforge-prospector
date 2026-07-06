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
import requests
import dns.resolver
from datetime import datetime

BREVO_API_KEY  = os.environ["BREVO_API_KEY"]
MY_EMAIL       = os.environ.get("MY_EMAIL") or "aquilesgbi@gmail.com"
SENDER_NAME    = os.environ.get("SENDER_NAME") or "Aquiles — LeadForge"
SENT_FILE      = "sent_emails.json"
CRM_FILE       = "crm_data.json"
WARMUP_FILE    = "warmup_state.json"

# ══════════════════════════════════════════════════════════
# RAMPA DE ENVÍO — protege la reputación del dominio hola@leadforge.es
# (compartido con los emails transaccionales de los clientes de pago).
# Sube el volumen diario poco a poco en vez de saltar directo al objetivo.
# Para forzar un número fijo (saltarse la rampa), define la env var
# MAX_PER_RUN_OVERRIDE (p.ej. 500) como secret en GitHub Actions.
# ══════════════════════════════════════════════════════════
RAMP_SCHEDULE = [
    (0,  50),
    (3,  100),
    (6,  150),
    (9,  250),
    (12, 350),
    (15, 500),
]


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

TARGETS = [
    "empresa de reformas",
    "fontanería",
    "electricista",
    "cerrajería",
    "pintor profesional",
    "despacho de abogados",
    "agencia inmobiliaria",
    "clínica dental",
    "clínica de fisioterapia",
    "taller mecánico",
]

TARGET_LABEL = {
    "empresa de reformas":       "empresas de reformas",
    "fontanería":                "fontaneros",
    "electricista":              "electricistas",
    "cerrajería":                "cerrajeros",
    "pintor profesional":        "pintores",
    "despacho de abogados":      "despachos de abogados",
    "agencia inmobiliaria":      "agencias inmobiliarias",
    "clínica dental":            "clínicas dentales",
    "clínica de fisioterapia":   "clínicas de fisioterapia",
    "taller mecánico":           "talleres mecánicos",
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
    "despacho de abogados":    [("office", "lawyer")],
    "agencia inmobiliaria":    [("office", "estate_agent")],
    "clínica dental":          [("amenity", "dentist")],
    "clínica de fisioterapia": [("healthcare", "physiotherapist")],
    "taller mecánico":         [("shop", "car_repair")],
}

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
    misma consulta dos veces por ciudad al servidor público."""
    key = (area_id, tags)
    if key in _osm_elements_cache:
        return _osm_elements_cache[key]

    time.sleep(1.5)  # margen entre llamadas al servidor público de Overpass — evita 429/respuesta vacía

    filtros = "".join(f'node["{k}"="{v}"](area.a);way["{k}"="{v}"](area.a);' for k, v in tags)
    query = f'[out:json][timeout:50];area({area_id})->.a;({filtros});out center tags 200;'

    try:
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query}, headers=OSM_HEADERS, timeout=55,
        )
        if r.status_code != 200:
            print(f"  [OSM] Error HTTP {r.status_code} tags={tags!r} area={area_id}")
            _osm_elements_cache[key] = []
            return []
        elements = r.json().get("elements", [])
    except Exception as e:
        print(f"  [OSM] Error: {e} tags={tags!r} area={area_id}")
        _osm_elements_cache[key] = []
        return []

    _osm_elements_cache[key] = elements
    return elements


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
def build_email(nombre_empresa, ciudad, sector_label):
    ciudad_corta = ciudad.split(",")[0]
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
      La mayoría de empresas en <strong>{ciudad_corta}</strong> pierden clientes potenciales
      cada semana porque no tienen tiempo de buscarlos uno a uno.
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      LeadForge los encuentra automáticamente — nombre, email, teléfono y web
      de cada empresa que podría contratarte — y los tiene listos en 30 segundos.
    </p>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      Si quieres verlo funcionar, aquí tienes una prueba gratuita con 20 leads reales de {ciudad_corta}:
      <a href="https://cobraflow0.github.io/leadforge-app/app.html?demo=true" style="color:#0066FF;font-weight:600;">prueba LeadForge gratis</a>
    </p>

    <!-- CTA -->
    <table cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
      <tr><td style="background:linear-gradient(135deg,#0066FF,#0052cc);border-radius:8px;box-shadow:0 4px 16px rgba(0,102,255,0.3);">
        <a href="https://cobraflow0.github.io/leadforge-app/app.html?demo=true"
           style="display:inline-block;padding:15px 36px;color:#fff;text-decoration:none;font-weight:700;font-size:15px;letter-spacing:0.2px;">
          Ver mis leads gratis →
        </a>
      </td></tr>
    </table>

    <p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.8;">
      Un cliente consiguió 3 presupuestos nuevos el primer día de uso, sin llamadas en frío ni publicidad.
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
        "sender":      {"name": SENDER_NAME, "email": "hola@leadforge.es"},
        "replyTo":     {"email": MY_EMAIL},
        "to":          [{"email": to_email}],
        "subject":     subject,
        "htmlContent": build_email(nombre_empresa, ciudad, sector_label),
        "tags":        ["prospector"],
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 201):
        message_id = r.json().get("messageId", "")
        return True, message_id
    print(f"    [Brevo error] status={r.status_code} body={r.text[:200]}")
    return False, None


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main():
    dia = datetime.now().timetuple().tm_yday
    ciudades_orden = get_ciudades_orden(dia)

    sent    = load_sent()
    crm     = load_crm()
    crm_ids = {e["email"] for e in crm}
    print(f"[prospector] {len(sent)} emails ya enviados anteriormente")

    max_per_run = get_max_per_run()
    print(f"[prospector] Objetivo de hoy: {max_per_run} emails (rampa/override)")

    sent_domains = {e.split("@")[-1] for e in sent}

    # Recorre ciudades una a una (Madrid primero, luego el resto rotado) y
    # manda sobre la marcha — para en cuanto llega al objetivo del día, pero
    # si una ciudad no da suficientes candidatos sigue probando con la
    # siguiente en vez de rendirse, hasta agotar las 20 si hace falta.
    SAVE_EVERY      = 10
    enviados        = 0
    rechazados_dns  = 0
    emails_reales   = 0
    seen_domains    = set()
    ciudades_usadas = []

    try:
        for ciudad in ciudades_orden:
            if enviados >= max_per_run:
                break
            ciudades_usadas.append(ciudad)

            leads_ciudad = []
            for target in TARGETS:
                print(f"[prospector] Buscando: {target} en {ciudad}")
                leads_ciudad.extend(search_osm(target, ciudad))
                time.sleep(1)

            for lead in leads_ciudad:
                if enviados >= max_per_run:
                    break

                domain = lead["domain"]
                if domain in seen_domains or domain in sent_domains:
                    continue
                seen_domains.add(domain)

                nombre  = lead["nombre"]
                website = lead["web"]

                real = get_real_email(website) if website else None
                if real:
                    lead["email"] = real
                    emails_reales += 1
                    print(f"  📧 Email real: {real} ({nombre})")
                elif website:
                    print(f"  ↩  Usando info@: {lead['email']} ({nombre})")
                else:
                    print(f"  📇 Email directo de OSM (sin web propia): {lead['email']} ({nombre})")

                if not verify_email(lead["email"]):
                    rechazados_dns += 1
                    print(f"  ⛔ {lead['email']} — no existe, descartado")
                    continue

                if lead["email"] in sent:
                    continue

                sector_label = TARGET_LABEL.get(lead.get("target", ""), "empresas")
                ok, message_id = send_email(lead["email"], nombre, ciudad, sector_label, dia)
                if ok:
                    sent.add(lead["email"])
                    sent_domains.add(domain)
                    enviados += 1
                    print(f"  ✅ {lead['email']} ({nombre})")
                    if enviados % SAVE_EVERY == 0:
                        save_sent(sent)
                        save_crm(crm)
                    if lead["email"] not in crm_ids:
                        crm.append({
                            "email":     lead["email"],
                            "nombre":    nombre,
                            "web":       website,
                            "ciudad":    ciudad.split(",")[0],
                            "sector":    sector_label,
                            "fecha":     datetime.now().strftime("%Y-%m-%d"),
                            "messageId": message_id or "",
                            "status":    "sent",
                        })
                        crm_ids.add(lead["email"])
                else:
                    print(f"  ❌ {lead['email']} — error Brevo")
                time.sleep(0.5)
    finally:
        save_sent(sent)
        save_crm(crm)

    print(f"\n[prospector] Fin — {enviados} enviados | {emails_reales} emails reales | {rechazados_dns} descartados por DNS | ciudades usadas: {len(ciudades_usadas)}/{len(ciudades_orden)}")

    ciudad_str = ", ".join(c.split(",")[0] for c in ciudades_usadas)
    objetivo_cumplido = enviados >= max_per_run
    aviso_corto = "" if objetivo_cumplido else "<p style='color:#b45309'>⚠️ No se llegó al objetivo — se agotaron las 20 ciudades sin encontrar más candidatos válidos.</p>"
    resumen_html = f"""
    <p>Hoy el prospector buscó en <b>{ciudad_str}</b> ({len(ciudades_usadas)} de {len(ciudades_orden)} ciudades).</p>
    {aviso_corto}
    <ul>
      <li>🎯 Objetivo del día (rampa/override): <b>{max_per_run}</b></li>
      <li>✅ Enviados: <b>{enviados}</b></li>
      <li>📧 Emails reales encontrados en web: <b>{emails_reales}</b></li>
      <li>⛔ Descartados por DNS/SMTP: <b>{rechazados_dns}</b></li>
      <li>📬 Total acumulado contactados: <b>{len(sent)}</b></li>
    </ul>
    """
    requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender":      {"name": SENDER_NAME, "email": "hola@leadforge.es"},
            "replyTo":     {"email": MY_EMAIL},
            "to":          [{"email": MY_EMAIL}],
            "subject":     f"[Prospector] {enviados} emails enviados — {ciudad_str}",
            "htmlContent": resumen_html,
        },
        timeout=10,
    )


if __name__ == "__main__":
    main()
