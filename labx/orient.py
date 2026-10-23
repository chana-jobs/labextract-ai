"""Upright the page before anything else (several forms are scanned sideways or upside down)."""
import os
import re
import subprocess
import tempfile

from . import render

TESSERACT = os.environ.get("LABX_TESSERACT", r"C:\Program Files\Tesseract-OCR\tesseract.exe")


# Downscaling for OSD was measured: at 1400 px it missed 2 of 6 sideways pages (p11, p14), so full size is the default.
OSD_MAX_SIDE = int(os.environ.get("LABX_OSD_MAX_SIDE", "2400"))


def _small(jpeg):
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    if max(img.size) <= OSD_MAX_SIDE:
        return jpeg
    img.thumbnail((OSD_MAX_SIDE, OSD_MAX_SIDE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def detect_rotation(jpeg):
    """Degrees to rotate the image clockwise to make it upright (0/90/180/270), per Tesseract OSD; 0 if unsure."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(_small(jpeg))
        path = f.name
    try:
        out = subprocess.run([TESSERACT, path, "-", "--psm", "0"], capture_output=True, text=True, timeout=60)
        txt = out.stdout + out.stderr
        rot = re.search(r"Rotate:\s*(\d+)", txt)
        conf = re.search(r"Orientation confidence:\s*([\d.]+)", txt)
        if rot and conf and float(conf.group(1)) >= 1.5:
            return int(rot.group(1))
        return 0
    except Exception:
        return 0
    finally:
        os.unlink(path)


def upright(jpeg):
    deg = detect_rotation(jpeg)
    # PIL rotates counter-clockwise for positive angles; Tesseract's "Rotate" is clockwise.
    return (render.rotate(jpeg, -deg), deg) if deg else (jpeg, 0)
