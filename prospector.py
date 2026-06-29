"""
LeadForge Prospector — standalone script
Busca empresas en Madrid que puedan comprar LeadForge y les manda un email.
Corre cada semana via GitHub Actions. No tiene nada que ver con la app LeadForge.
"""

import os
import json
import time
import requests

BREVO_API_KEY  = os.environ["BREVO_API_KEY"]
GMAPS_API_KEY  = os.environ["GMAPS_API_KEY"]
MY_EMAIL       = os.environ.get("MY_EMAIL", "aquilesgbi@gmail.com")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL", "aquilesgbi@gmail.com")
SENT_FILE      = "sent_emails.json"
MAX_PER_RUN    = 200
CIUDAD         = "Madrid, España"

# Sectores con alta probabilidad de querer LeadForge
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


def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE) as f:
            return set(json.load(f))
    return set()


def save_sent(sent):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent), f)


def search_gmaps(query, ciudad):
    leads = []
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": f"{query} en {ciudad}", "key": GMAPS_API_KEY, "language": "es"}
    while True:
        r = requests.get(url, params=params, timeout=10).json()
        for p in r.get("results", []):
            place_id = p.get("place_id")
            if not place_id:
                continue
            detail = requests.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": place_id, "fields": "name,formatted_phone_number,website,rating,user_ratings_total", "key": GMAPS_API_KEY},
                timeout=10,
            ).json().get("result", {})
            website = detail.get("website", "")
            if not website:
                continue
            # Extraer email del dominio (simplificado)
            domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
            email = f"info@{domain}"
            leads.append({
                "nombre":  p.get("name", ""),
                "email":   email,
                "web":     website,
                "rating":  detail.get("rating", 0),
            })
            time.sleep(0.2)
        next_token = r.get("next_page_token")
        if not next_token:
            break
        params = {"pagetoken": next_token, "key": GMAPS_API_KEY}
        time.sleep(2)
    return leads


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
        <a href="https://cobraflow0.github.io/leadforge-app/index.html"
           style="display:inline-block;padding:13px 28px;color:#fff;text-decoration:none;font-weight:700;font-size:15px;">
          Ver LeadForge gratis →
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


def send_email(to_email, nombre_empresa):
    payload = {
        "sender":      {"name": "Aquiles — LeadForge", "email": SENDER_EMAIL},
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


def main():
    sent = load_sent()
    print(f"[prospector] {len(sent)} emails ya enviados anteriormente")

    all_leads = []
    for target in TARGETS:
        print(f"[prospector] Buscando: {target}")
        leads = search_gmaps(target, CIUDAD)
        all_leads.extend(leads)
        time.sleep(1)

    # Dedup por email
    seen = set()
    unique_leads = []
    for l in all_leads:
        if l["email"] not in seen:
            seen.add(l["email"])
            unique_leads.append(l)

    # Filtrar ya enviados
    nuevos = [l for l in unique_leads if l["email"] not in sent]
    print(f"[prospector] {len(nuevos)} leads nuevos encontrados")

    enviados = 0
    for lead in nuevos[:MAX_PER_RUN]:
        ok = send_email(lead["email"], lead["nombre"])
        if ok:
            sent.add(lead["email"])
            enviados += 1
            print(f"  ✅ {lead['email']} ({lead['nombre']})")
        else:
            print(f"  ❌ {lead['email']} — error")
        time.sleep(0.5)

    save_sent(sent)
    print(f"[prospector] Fin — {enviados} emails enviados hoy")

    # Resumen a ti mismo
    resumen = {
        "sender": {"name": "LeadForge Prospector", "email": SENDER_EMAIL},
        "to":     [{"email": MY_EMAIL}],
        "subject": f"[Prospector] {enviados} emails enviados hoy",
        "htmlContent": f"<p>Hoy el prospector envió <b>{enviados} emails</b> a potenciales clientes de LeadForge en Madrid.</p><p>Total acumulado contactados: {len(sent)}</p>",
    }
    requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=resumen, timeout=10,
    )


if __name__ == "__main__":
    main()
