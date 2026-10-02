"""Filenames, docx -> PDF conversion via LibreOffice, and zip bundling."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path

from .storage import DATA_DIR, Settings

_soffice_lock = threading.Lock()  # concurrent soffice instances on one profile fail


def safe_part(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    return text.strip("_")


def build_filename(settings: Settings, company: str, role: str, ext: str = "pdf") -> str:
    parts = {
        "first": safe_part(settings.first_name),
        "last": safe_part(settings.last_name),
        "company": safe_part(company),
        "role": safe_part(role),
    }
    name = settings.filename_pattern.format(**parts)
    name = re.sub(r"_+", "_", name).strip("_") or "CoverLetter"
    return f"{name}.{ext}"


def soffice_binary() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("LibreOffice not found; install it to export PDFs (e.g. `sudo apt install libreoffice-writer`).")


def docx_to_pdf(docx_bytes: bytes) -> bytes:
    profile = (DATA_DIR / "lo_profile").resolve()
    with tempfile.TemporaryDirectory() as tmp, _soffice_lock:
        src = Path(tmp) / "letter.docx"
        src.write_bytes(docx_bytes)
        result = subprocess.run(
            [
                soffice_binary(), f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src),
            ],
            capture_output=True, text=True, timeout=120,
        )
        out = Path(tmp) / "letter.pdf"
        if not out.exists():
            raise RuntimeError(f"PDF conversion failed: {result.stderr or result.stdout}")
        return out.read_bytes()


def zip_files(paths: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, arcname=p.name)
    return buf.getvalue()
