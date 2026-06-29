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
GMAPS_API_KEY  = os.environ["GMAPS_API_KEY"]
MY_EMAIL       = os.environ.get("MY_EMAIL", "aquilesgbi@gmail.com")
SENT_FILE      = "sent_emails.json"
MAX_PER_RUN    = 200

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Rotación diaria de ciudades — cada día busca en una ciudad diferente
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
    "Lisboa, Portugal",
    "Porto, Portugal",
]

TARGETS = [
    "agencia de marketing digital",
    "agencia inmobiliaria",
    "correduría de seguros",
    "consultoría de negocio",
    "asesoría fiscal",
    "academia de formación empresarial",
    "empresa de software B2B",
    "agencia de publicidad",
    "gestoría administrativa",
    "empresa de telecomunicaciones",
]

# Dominios genéricos que no tienen inbox real
DOMINIOS_INVALIDOS = {
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "youtube.com", "google.com", "wix.com", "wordpress.com",
    "blogspot.com", "weebly.com", "squarespace.com", "godaddy.com",
    "1and1.es", "jimdo.com",
}

_verify_cache = {}


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

def _parse_emails_from_html(html):
    """Extrae emails de HTML — prioriza mailto:, luego regex general."""
    emails = []
    # 1. mailto: links (más fiables — el propio negocio los puso)
    mailtos = re.findall(r'href=["\']mailto:([^"\'?&\s>]+)', html, re.IGNORECASE)
    for m in mailtos:
        m = m.strip().lower()
        if "@" in m and "." in m.split("@")[-1]:
            if not any(s in m for s in _EMAIL_SKIP):
                emails.append(m)
    if emails:
        return emails

    # 2. Regex sobre texto plano (fallback)
    found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html)
    for f in found:
        f = f.lower()
        if not any(s in f for s in _EMAIL_SKIP | {"example", "sentry", "schema", "pixel"}):
            emails.append(f)
    return emails


def get_real_email(website):
    """
    Visita la web del negocio (homepage + /contacto + /contact)
    y devuelve el primer email real que encuentre.
    Devuelve None si no encuentra nada.
    """
    paths = ["", "/contacto", "/contact", "/sobre-nosotros", "/about"]
    for path in paths[:3]:
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
    """
    Verifica que el email probablemente existe.
    Paso 1 — DNS: el dominio tiene registros MX (servidor de correo).
    Paso 2 — SMTP: conectar y preguntar si el buzón existe.
    Si el puerto 25 está bloqueado (común en cloud), confía en el DNS.
    """
    if email in _verify_cache:
        return _verify_cache[email]

    domain = email.split("@")[-1]

    # Paso 1 — DNS
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
        # Timeout de DNS — aceptamos el email
        _verify_cache[email] = True
        return True

    # Paso 2 — SMTP
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
            continue  # puerto 25 bloqueado — prueba siguiente MX
        except Exception:
            continue

    # SMTP inalcanzable — el dominio tiene MX, confiamos en eso
    _verify_cache[email] = True
    return True


# ══════════════════════════════════════════════════════════
# GOOGLE MAPS
# ══════════════════════════════════════════════════════════
def search_gmaps(query, ciudad):
    leads = []
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": f"{query} en {ciudad}", "key": GMAPS_API_KEY, "language": "es"}
    while True:
        r = requests.get(url, params=params, timeout=10).json()
        if r.get("status") in ("OVER_QUERY_LIMIT", "REQUEST_DENIED"):
            print(f"  [Maps] ⚠️  API error: {r.get('status')} — deteniendo búsqueda")
            break
        for p in r.get("results", []):
            place_id = p.get("place_id")
            if not place_id:
                continue
            try:
                detail = requests.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={"place_id": place_id, "fields": "name,website", "key": GMAPS_API_KEY},
                    timeout=10,
                ).json().get("result", {})
            except Exception:
                continue
            website = detail.get("website", "")
            if not website:
                continue
            domain = (
                website.replace("https://", "").replace("http://", "")
                .replace("www.", "").split("/")[0].lower()
            )
            if not dominio_valido(domain):
                continue
            leads.append({
                "nombre":  p.get("name", ""),
                "web":     website,
                "domain":  domain,
                "email":   f"info@{domain}",  # fallback — se enriquece después
            })
            time.sleep(0.2)
        next_token = r.get("next_page_token")
        if not next_token:
            break
        params = {"pagetoken": next_token, "key": GMAPS_API_KEY}
        time.sleep(2)
    return leads


# ══════════════════════════════════════════════════════════
# EMAIL HTML
# ══════════════════════════════════════════════════════════
def build_email(nombre_empresa):
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:40px 20px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08);">
  <tr><td style="background:linear-gradient(135deg,#0066FF,#00C8FF);padding:28px 40px;">
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:800;">⚡ LeadForge</h1>
    <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">Generación automática de leads para empresas españolas</p>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="margin:0 0 16px;font-size:16px;color:#1a1a2e;">Hola, equipo de {nombre_empresa}</p>
    <p style="margin:0 0 16px;font-size:15px;color:#4a5568;line-height:1.7;">
      Te escribo porque creo que <strong>LeadForge</strong> puede ser útil para tu empresa.
    </p>
    <p style="margin:0 0 16px;font-size:15px;color:#4a5568;line-height:1.7;">
      LeadForge busca automáticamente contactos de negocios en Google Maps, directorios y otras fuentes,
      y lanza campañas de email personalizadas en minutos.
    </p>
    <ul style="margin:0 0 20px;padding-left:20px;color:#4a5568;font-size:15px;line-height:2;">
      <li>Hasta <strong>1.000 leads</strong> por búsqueda con email y teléfono</li>
      <li>Excel profesional con scoring de calidad automático</li>
      <li>Campañas de cold email con follow-up automático</li>
      <li>45+ ciudades españolas · 15 sectores B2B</li>
    </ul>
    <p style="margin:0 0 24px;font-size:15px;color:#4a5568;line-height:1.7;">
      Nuestro primer cliente consiguió <strong>3 presupuestos en su primer día</strong> de uso.
    </p>
    <table cellpadding="0" cellspacing="0" style="margin:0 auto 24px;">
      <tr><td style="background:linear-gradient(135deg,#0066FF,#00C8FF);border-radius:8px;">
        <a href="https://cobraflow0.github.io/leadforge-app/app.html?demo=true"
           style="display:inline-block;padding:13px 28px;color:#fff;text-decoration:none;font-weight:700;font-size:15px;">
          Prueba LeadForge gratis — busca tus leads →
        </a>
      </td></tr>
    </table>
    <p style="margin:0;font-size:13px;color:#7a8ba0;text-align:center;">
      Planes desde 19€/mes · Sin permanencia · Cancela cuando quieras<br>
      ¿No es para ti? Responde a este email y no volvemos a escribir.
    </p>
  </td></tr>
  <tr><td style="background:#f8fafc;padding:14px 40px;border-top:1px solid #e2e8f0;text-align:center;">
    <p style="margin:0;font-size:11px;color:#a0aec0;">LeadForge · leadforge.es · hola@leadforge.es</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# ══════════════════════════════════════════════════════════
# ENVÍO
# ══════════════════════════════════════════════════════════
def send_email(to_email, nombre_empresa):
    payload = {
        "sender":      {"name": "Aquiles — LeadForge", "email": "hola@leadforge.es"},
        "replyTo":     {"email": MY_EMAIL},
        "to":          [{"email": to_email}],
        "subject":     f"¿LeadForge puede ayudar a {nombre_empresa}?",
        "htmlContent": build_email(nombre_empresa),
        "tags":        ["prospector"],
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    return r.status_code in (200, 201)


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main():
    dia = datetime.now().timetuple().tm_yday
    ciudad = CIUDADES[dia % len(CIUDADES)]
    print(f"[prospector] Ciudad de hoy: {ciudad}")

    sent = load_sent()
    print(f"[prospector] {len(sent)} emails ya enviados anteriormente")

    # 1. Recoger candidatos de Google Maps
    all_leads = []
    for target in TARGETS:
        print(f"[prospector] Buscando: {target} en {ciudad}")
        leads = search_gmaps(target, ciudad)
        all_leads.extend(leads)
        time.sleep(1)

    # Dedup por dominio (no por email adivinado, porque el email real puede variar)
    seen_domains = set()
    unique_leads = []
    for l in all_leads:
        if l["domain"] not in seen_domains:
            seen_domains.add(l["domain"])
            unique_leads.append(l)

    # Filtrar ya enviados (por dominio — evita reenviar aunque cambie el alias)
    nuevos = [
        l for l in unique_leads
        if l["domain"] not in {e.split("@")[-1] for e in sent}
    ]
    print(f"[prospector] {len(nuevos)} candidatos nuevos")

    # 2. Enriquecer + verificar + enviar
    enviados       = 0
    rechazados_dns = 0
    emails_reales  = 0

    for lead in nuevos:
        if enviados >= MAX_PER_RUN:
            break

        nombre  = lead["nombre"]
        website = lead["web"]

        # Intentar obtener email real de la web
        real = get_real_email(website)
        if real:
            lead["email"] = real
            emails_reales += 1
            print(f"  📧 Email real: {real} ({nombre})")
        else:
            print(f"  ↩  Usando info@: {lead['email']} ({nombre})")

        # Verificar que el email existe (DNS + SMTP si disponible)
        if not verify_email(lead["email"]):
            rechazados_dns += 1
            print(f"  ⛔ {lead['email']} — no existe, descartado")
            continue

        # Evitar reenvío si el email real ya estaba en sent
        if lead["email"] in sent:
            continue

        ok = send_email(lead["email"], nombre)
        if ok:
            sent.add(lead["email"])
            enviados += 1
            print(f"  ✅ {lead['email']} ({nombre})")
        else:
            print(f"  ❌ {lead['email']} — error Brevo")
        time.sleep(0.5)

    save_sent(sent)
    print(f"\n[prospector] Fin — {enviados} enviados | {emails_reales} emails reales | {rechazados_dns} descartados por DNS")

    # Resumen por email
    resumen_html = f"""
    <p>Hoy el prospector buscó en <b>{ciudad}</b>.</p>
    <ul>
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
            "sender":      {"name": "LeadForge Prospector", "email": "hola@leadforge.es"},
            "replyTo":     {"email": MY_EMAIL},
            "to":          [{"email": MY_EMAIL}],
            "subject":     f"[Prospector] {enviados} emails enviados — {ciudad}",
            "htmlContent": resumen_html,
        },
        timeout=10,
    )


if __name__ == "__main__":
    main()
