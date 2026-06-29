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

# Etiqueta legible para cada target — se usa en el email personalizado
TARGET_LABEL = {
    "agencia de marketing digital":   "agencias de marketing digital",
    "agencia inmobiliaria":            "agencias inmobiliarias",
    "correduría de seguros":           "corredurías de seguros",
    "consultoría de negocio":          "consultoras de negocio",
    "asesoría fiscal":                 "asesorías fiscales",
    "academia de formación empresarial": "academias de formación",
    "empresa de software B2B":         "empresas de software B2B",
    "agencia de publicidad":           "agencias de publicidad",
    "gestoría administrativa":         "gestorías administrativas",
    "empresa de telecomunicaciones":   "empresas de telecomunicaciones",
}

# 4 asuntos rotativos — rotan por día para A/B natural y mejor deliverability
SUBJECTS = [
    "Clientes nuevos en {ciudad} — sin publicidad",
    "{nombre}, hay contactos en {ciudad} esperándote",
    "Cómo conseguir clientes en {ciudad} sin llamadas en frío",
    "20 empresas en {ciudad} que podrían contratarte",
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
                "target":  query,
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
def build_email(nombre_empresa, ciudad, sector_label):
    ciudad_corta = ciudad.split(",")[0]  # "Madrid, España" → "Madrid"
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
        "sender":      {"name": "Aquiles — LeadForge", "email": "hola@leadforge.es"},
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

        sector_label = TARGET_LABEL.get(lead.get("target", ""), "empresas")
        ok = send_email(lead["email"], nombre, ciudad, sector_label, dia)
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
