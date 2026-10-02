"""Local persistence: template, resume, and settings live in ./data."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
TEMPLATE_PATH = DATA_DIR / "template.docx"
RESUME_PATH = DATA_DIR / "resume.txt"
RESUME_PDF_PATH = DATA_DIR / "resume.pdf"  # original resume, for attaching to exports
SETTINGS_PATH = DATA_DIR / "settings.json"


class Settings(BaseModel):
    first_name: str = ""
    last_name: str = ""
    filename_pattern: str = "{first}_{last}_CoverLetter_{company}_{role}"
    # Used when the resume is attached after the cover letter in one PDF.
    combined_filename_pattern: str = "{first}_{last}_CoverLetter_Resume_{company}_{role}"
    date_format: str = "%B %-d, %Y"
    include_resume_default: bool = False
    attach_resume_default: bool = False
    # Fields filled from here instead of by Claude, e.g. {"my_email": "me@x.com"}.
    static_fields: dict[str, str] = Field(default_factory=dict)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def load_settings() -> Settings:
    if SETTINGS_PATH.exists():
        return Settings.model_validate_json(SETTINGS_PATH.read_text())
    return Settings()


def save_settings(settings: Settings) -> None:
    ensure_dirs()
    SETTINGS_PATH.write_text(settings.model_dump_json(indent=2))


def load_template() -> bytes | None:
    return TEMPLATE_PATH.read_bytes() if TEMPLATE_PATH.exists() else None


def save_template(data: bytes) -> None:
    ensure_dirs()
    TEMPLATE_PATH.write_bytes(data)


def load_resume() -> str | None:
    return RESUME_PATH.read_text() if RESUME_PATH.exists() else None


def save_resume(text: str) -> None:
    ensure_dirs()
    RESUME_PATH.write_text(text)


def load_resume_pdf() -> bytes | None:
    return RESUME_PDF_PATH.read_bytes() if RESUME_PDF_PATH.exists() else None


def save_resume_pdf(data: bytes | None) -> None:
    """Store the resume PDF, or remove a stale one when the new resume has no PDF form."""
    ensure_dirs()
    if data is None:
        RESUME_PDF_PATH.unlink(missing_ok=True)
    else:
        RESUME_PDF_PATH.write_bytes(data)
