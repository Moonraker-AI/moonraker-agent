"""
apply_neo_overlay.py

Composites a NEO image with branded overlay:
  - Logo (top-left, frosted glass background)
  - QR code (top-right, frosted glass background, from GBP share link)
  - White footer bar (practice name + plus code)

Uses PIL for compositing, qrcode for QR generation.
Downloads logo from Google Drive via service account.
"""

import base64
import io
import json
import logging
import os
import time

import httpx
import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger("moonraker.neo_overlay")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "images"
TARGET_WIDTH = 1200
TARGET_HEIGHT = 900


async def run_apply_neo_overlay(task_id, params, status_callback, env):
    """
    params:
        base_image_url: str
        logo_drive_file_id: str (optional)
        logo_url: str (optional, direct URL)
        practice_name: str
        plus_code: str
        gbp_share_link: str
        client_slug: str
        output_name: str
        neo_image_id: str (optional)
        callback_url: str
    """
    await status_callback(task_id, "running", "Starting NEO overlay...")

    try:
        base_url = params.get("base_image_url", "")
        client_slug = params.get("client_slug", "")
        output_name = params.get("output_name", f"neo-{int(time.time())}")
        callback_url = params.get("callback_url", "")
        agent_api_key = env.get("AGENT_API_KEY", "")

        if not base_url:
            await status_callback(task_id, "failed", "base_image_url required")
            return

        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Download base image
            await status_callback(task_id, "running", "Downloading base image...")
            resp = await client.get(base_url)
            if resp.status_code != 200:
                await status_callback(task_id, "failed", f"Base image download failed: {resp.status_code}")
                return
            base_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")

            # 2. Download logo
            logo_img = None
            logo_url = params.get("logo_url", "")
            logo_fid = params.get("logo_drive_file_id", "")
            if logo_url:
                await status_callback(task_id, "running", "Downloading logo...")
                r = await client.get(logo_url)
                if r.status_code == 200:
                    logo_img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            elif logo_fid:
                await status_callback(task_id, "running", "Downloading logo from Drive...")
                logo_bytes = await download_from_drive(client, logo_fid, env)
                if logo_bytes:
                    logo_img = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

            # 3. Generate QR code
            qr_img = None
            gbp_link = params.get("gbp_share_link", "")
            if gbp_link:
                await status_callback(task_id, "running", "Generating QR code...")
                qr_img = generate_qr(gbp_link)

            # 4. Composite
            await status_callback(task_id, "running", "Compositing overlay...")
            practice_name = params.get("practice_name", "")
            plus_code = params.get("plus_code", "")
            final = composite_neo(base_img, logo_img, qr_img, practice_name, plus_code)

            # 5. Save as JPEG and upload
            await status_callback(task_id, "running", "Uploading final image...")
            buf = io.BytesIO()
            final.convert("RGB").save(buf, format="JPEG", quality=92)
            out_bytes = buf.getvalue()

            path = f"{client_slug}/neo/{output_name}.jpg"
            ok = await upload_to_storage(client, path, out_bytes, "image/jpeg")
            if not ok:
                await status_callback(task_id, "failed", "Storage upload failed")
                return

            hosted_url = f"https://clients.moonraker.ai/{client_slug}/img/neo/{output_name}.jpg"

            # 6. Callback
            if callback_url and agent_api_key:
                try:
                    await client.post(callback_url, json={
                        "neo_image_id": params.get("neo_image_id"),
                        "hosted_url": hosted_url,
                        "output_name": output_name,
                        "image_size": len(out_bytes),
                    }, headers={"Authorization": f"Bearer {agent_api_key}", "Content-Type": "application/json"})
                except Exception as e:
                    logger.error(f"Callback failed: {e}")

            await status_callback(task_id, "completed", f"NEO overlay complete: {hosted_url} ({len(out_bytes)} bytes)")

    except Exception as e:
        logger.error(f"NEO overlay error: {e}", exc_info=True)
        await status_callback(task_id, "failed", f"Error: {str(e)[:200]}")


def composite_neo(base, logo, qr, practice_name, plus_code):
    """Build the final composite image."""
    base = cover_crop(base, TARGET_WIDTH, TARGET_HEIGHT)
    canvas = base.copy()
    margin = 24
    overlay_sz = int(TARGET_WIDTH * 0.15)

    # Logo (top-left)
    if logo:
        logo_h = overlay_sz
        ratio = logo.width / logo.height
        logo_w = int(logo_h * ratio)
        if logo_w > overlay_sz * 1.8:
            logo_w = int(overlay_sz * 1.8)
            logo_h = int(logo_w / ratio)
        lr = logo.resize((logo_w, logo_h), Image.LANCZOS)
        pad = 16
        gw, gh = logo_w + pad * 2, logo_h + pad * 2
        glass = frosted_glass(canvas, margin, margin, gw, gh)
        canvas.paste(glass, (margin, margin), glass)
        canvas.paste(lr, (margin + pad, margin + pad), lr)

    # QR (top-right)
    if qr:
        qr_sz = overlay_sz
        qr_r = qr.resize((qr_sz, qr_sz), Image.LANCZOS)
        pad = 12
        gw, gh = qr_sz + pad * 2, qr_sz + pad * 2
        qx = TARGET_WIDTH - margin - gw
        glass = frosted_glass(canvas, qx, margin, gw, gh)
        canvas.paste(glass, (qx, margin), glass)
        canvas.paste(qr_r, (qx + pad, margin + pad))

    # Footer bar
    if practice_name or plus_code:
        bar_h = 70
        new = Image.new("RGBA", (TARGET_WIDTH, TARGET_HEIGHT + bar_h), (255, 255, 255, 255))
        new.paste(canvas, (0, 0))
        draw = ImageDraw.Draw(new)
        try:
            nf = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            cf = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            nf = ImageFont.load_default()
            cf = nf
        if practice_name:
            bb = draw.textbbox((0, 0), practice_name, font=nf)
            draw.text(((TARGET_WIDTH - (bb[2] - bb[0])) // 2, TARGET_HEIGHT + 12), practice_name, fill=(51, 51, 51), font=nf)
        if plus_code:
            bb = draw.textbbox((0, 0), plus_code, font=cf)
            draw.text(((TARGET_WIDTH - (bb[2] - bb[0])) // 2, TARGET_HEIGHT + 40), plus_code, fill=(119, 119, 119), font=cf)
        canvas = new

    return canvas


def cover_crop(img, tw, th):
    sr = img.width / img.height
    tr = tw / th
    if sr > tr:
        nh = th; nw = int(th * sr)
    else:
        nw = tw; nh = int(tw / sr)
    img = img.resize((nw, nh), Image.LANCZOS)
    l = (nw - tw) // 2; t = (nh - th) // 2
    return img.crop((l, t, l + tw, t + th))


def frosted_glass(canvas, x, y, w, h, blur=15, opacity=180):
    region = canvas.crop((max(0, x), max(0, y), min(canvas.width, x + w), min(canvas.height, y + h))).convert("RGBA")
    blurred = region.filter(ImageFilter.GaussianBlur(blur))
    white = Image.new("RGBA", blurred.size, (255, 255, 255, opacity))
    glass = Image.alpha_composite(blurred, white)
    mask = Image.new("L", glass.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), glass.size], radius=12, fill=255)
    glass.putalpha(mask)
    return glass


def generate_qr(url, size=400):
    q = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    q.add_data(url)
    q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").convert("RGBA").resize((size, size), Image.LANCZOS)


def base64url_encode(data):
    if isinstance(data, str): data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def download_from_drive(client, file_id, env):
    try:
        sa_json = env.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not sa_json: return None
        sa = json.loads(sa_json) if isinstance(sa_json, str) else sa_json

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        now = int(time.time())
        header = base64url_encode(json.dumps({"alg": "RS256", "typ": "JWT"}))
        payload = base64url_encode(json.dumps({
            "iss": sa["client_email"], "sub": "support@moonraker.ai",
            "scope": "https://www.googleapis.com/auth/drive",
            "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600
        }))
        signing_input = f"{header}.{payload}"
        pk = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
        sig = pk.sign(signing_input.encode(), asym_padding.PKCS1v15(), hashes.SHA256())
        jwt_token = f"{signing_input}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"

        tr = await client.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token
        })
        if tr.status_code != 200: return None
        token = tr.json().get("access_token")

        dr = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                              headers={"Authorization": f"Bearer {token}"})
        return dr.content if dr.status_code == 200 else None
    except Exception as e:
        logger.error(f"Drive download error: {e}", exc_info=True)
        return None


async def upload_to_storage(client, path, data, content_type):
    if not SUPABASE_URL or not SUPABASE_KEY: return False
    r = await client.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}", content=data, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type, "x-upsert": "true"
    })
    return r.status_code in (200, 201)
