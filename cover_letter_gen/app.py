"""FastAPI app: upload template/resume, generate letters with Claude, export PDFs."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import claude_client as cc
from . import export, storage, template

load_dotenv(storage.ROOT / ".env")
storage.ensure_dirs()

app = FastAPI(title="Cover Letter Generator")
STATIC_DIR = Path(__file__).parent / "static"

# Company research is reused across requests in this session (keyed by lowercase name).
_research_cache: dict[str, str] = {}


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    jd: str
    company: str = ""
    role: str = ""
    company_short: str = ""
    role_short: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    written: dict[str, str] = Field(default_factory=dict)
    research: str = ""
    siblings: list[str] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------- helpers

def _template_or_400() -> tuple[bytes, list[template.Slot], str]:
    data = storage.load_template()
    if data is None:
        raise HTTPException(400, "Upload a .docx template first.")
    doc = template.load(data)
    return data, template.extract_slots(doc), template.template_text(doc)


def _auto_fields(settings: storage.Settings) -> dict[str, str]:
    """Fields that come from settings rather than Claude."""
    values = {"date": date.today().strftime(settings.date_format)}
    full = f"{settings.first_name} {settings.last_name}".strip()
    if full:
        values.update({"my_name": full, "full_name": full, "first_name": settings.first_name,
                       "last_name": settings.last_name})
    values.update({template.normalize_key(k): v for k, v in settings.static_fields.items()})
    return values


def _fill_values(slots: list[template.Slot], job: Job, settings: storage.Settings) -> dict[str, str]:
    auto = _auto_fields(settings)
    values = {}
    for s in slots:
        if s.kind == "field":
            values[s.key] = job.fields.get(s.id, auto.get(s.id, ""))
        else:
            values[s.key] = job.written.get(s.id, "")
    return values


def _render_pdf(job: Job) -> bytes:
    data, slots, _ = _template_or_400()
    docx = template.fill_bytes(data, _fill_values(slots, job, storage.load_settings()))
    return export.docx_to_pdf(docx)


def _extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    if name.endswith(".docx"):
        return "\n".join(p.text for p in template.iter_paragraphs(template.load(data)))
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- setup routes

@app.get("/api/state")
def get_state():
    data = storage.load_template()
    slots = [s.to_dict() for s in template.extract_slots(template.load(data))] if data else None
    resume = storage.load_resume()
    return {
        "settings": storage.load_settings().model_dump(),
        "slots": slots,
        "auto_fields": sorted(_auto_fields(storage.load_settings())),
        "has_resume": resume is not None,
        "resume_preview": (resume or "")[:300],
    }


@app.post("/api/template")
async def upload_template(file: UploadFile):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Template must be a .docx (in Google Docs: File > Download > Microsoft Word).")
    data = await file.read()
    try:
        slots = template.extract_slots(template.load(data))
    except Exception as e:
        raise HTTPException(400, f"Couldn't read that .docx: {e}")
    storage.save_template(data)
    return {"slots": [s.to_dict() for s in slots]}


@app.post("/api/resume")
async def upload_resume(file: UploadFile):
    text = _extract_text(file.filename, await file.read()).strip()
    if not text:
        raise HTTPException(400, "Couldn't extract any text from that resume.")
    storage.save_resume(text)
    return {"has_resume": True, "resume_preview": text[:300]}


@app.put("/api/settings")
def put_settings(settings: storage.Settings):
    try:
        export.build_filename(settings, "Company", "Role")
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(400, f"Bad filename pattern: {e}. Use {{first}}, {{last}}, {{company}}, {{role}}.")
    storage.save_settings(settings)
    return settings


# ---------------------------------------------------------------- generation

class GenerateRequest(BaseModel):
    jds: list[str]
    include_resume: bool = False


def _event(**kw) -> str:
    return json.dumps(kw) + "\n"


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    _, slots, tmpl_text = _template_or_400()
    settings = storage.load_settings()
    auto = _auto_fields(settings)
    claude_fields = [s.id for s in slots if s.kind == "field" and s.id not in auto]
    written_slots = [s.to_dict() for s in slots if s.kind == "written"]
    resume = storage.load_resume() if req.include_resume else None
    jobs = [Job(jd=jd.strip()) for jd in req.jds if jd.strip()]
    if not jobs:
        raise HTTPException(400, "Paste at least one job description.")

    async def run():
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def pipeline():
            emit = queue.put_nowait
            emit(_event(type="status", message=f"Reading {len(jobs)} job description(s)…"))

            async def extract(job: Job):
                try:
                    info = await cc.extract_job(job.jd, claude_fields)
                except Exception as e:
                    job.error = cc.describe_error(e)
                    return
                job.company, job.role = info.company, info.role
                job.company_short, job.role_short = info.company_short, info.role_short
                job.fields = {f.name: f.value for f in info.fields if f.name in claude_fields}
                emit(_event(type="status", message=f"Found: {job.role} at {job.company}"))

            await asyncio.gather(*(extract(j) for j in jobs))

            by_company: dict[str, list[Job]] = {}
            for j in jobs:
                if not j.error:
                    by_company.setdefault(j.company_short.lower(), []).append(j)

            async def research(key: str, group: list[Job]):
                if key not in _research_cache:
                    emit(_event(type="status", message=f"Researching {group[0].company_short}…"))
                    try:
                        _research_cache[key] = await cc.research_company(
                            group[0].company, [j.role for j in group], group[0].jd)
                    except Exception as e:
                        for j in group:
                            j.error = f"Research failed: {cc.describe_error(e)}"
                        return
                for j in group:
                    j.research = _research_cache[key]
                    j.siblings = [o.role for o in group if o is not j]

            await asyncio.gather(*(research(k, g) for k, g in by_company.items()))

            async def write(job: Job):
                if not job.error:
                    emit(_event(type="status", message=f"Writing letter for {job.role_short}…"))
                    try:
                        job.written = await cc.write_slots(
                            template_text=tmpl_text, slots=written_slots, jd=job.jd,
                            fields={**auto, **job.fields}, research=job.research,
                            resume=resume, sibling_roles=job.siblings)
                    except Exception as e:
                        job.error = cc.describe_error(e)
                emit(_event(type="job", job=job.model_dump()))

            await asyncio.gather(*(write(j) for j in jobs))

        task = asyncio.create_task(pipeline())
        while not (task.done() and queue.empty()):
            try:
                yield await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
        if task.exception():
            yield _event(type="error", message=cc.describe_error(task.exception()))
        yield _event(type="done")

    return StreamingResponse(run(), media_type="application/x-ndjson")


class RegenerateRequest(BaseModel):
    job: Job
    slot_id: str
    hint: str | None = None
    include_resume: bool = False


@app.post("/api/regenerate-slot")
async def regenerate_slot(req: RegenerateRequest):
    _, slots, tmpl_text = _template_or_400()
    slot = next((s for s in slots if s.id == req.slot_id and s.kind == "written"), None)
    if slot is None:
        raise HTTPException(404, f"No written slot {req.slot_id} in the current template.")
    job = req.job
    try:
        result = await cc.write_slots(
            template_text=tmpl_text, slots=[slot.to_dict()], jd=job.jd,
            fields={**_auto_fields(storage.load_settings()), **job.fields}, research=job.research,
            resume=storage.load_resume() if req.include_resume else None,
            sibling_roles=job.siblings, hint=req.hint,
            previous={slot.id: job.written.get(slot.id, "")},
        )
    except Exception as e:
        raise HTTPException(502, cc.describe_error(e))
    return {"slot_id": slot.id, "text": result[slot.id]}


# ---------------------------------------------------------------- export

@app.post("/api/preview")
async def preview(job: Job):
    try:
        pdf = await asyncio.to_thread(_render_pdf, job)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return Response(pdf, media_type="application/pdf")


class ExportRequest(BaseModel):
    jobs: list[Job]


@app.post("/api/export")
async def export_letters(req: ExportRequest):
    settings = storage.load_settings()
    results, used = [], set()
    for job in req.jobs:
        name = export.build_filename(settings, job.company_short or job.company, job.role_short or job.role)
        stem, n = name[:-4], 2
        while name in used:  # two jobs with the same company/role in one batch
            name, n = f"{stem}_{n}.pdf", n + 1
        used.add(name)
        try:
            pdf = await asyncio.to_thread(_render_pdf, job)
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        (storage.OUTPUT_DIR / name).write_bytes(pdf)
        results.append({"id": job.id, "filename": name, "url": f"/api/files/{name}"})
    return {"files": results, "output_dir": str(storage.OUTPUT_DIR)}


def _output_file(name: str) -> Path:
    path = (storage.OUTPUT_DIR / name).resolve()
    if path.parent != storage.OUTPUT_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "File not found.")
    return path


@app.get("/api/files/{name}")
def download(name: str):
    return FileResponse(_output_file(name), filename=name, media_type="application/pdf")


class ZipRequest(BaseModel):
    filenames: list[str]


@app.post("/api/zip")
def download_zip(req: ZipRequest):
    data = export.zip_files([_output_file(n) for n in req.filenames])
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="cover_letters.zip"'})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main():
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    print(f"Cover Letter Generator running at http://127.0.0.1:{port}")
    uvicorn.run("cover_letter_gen.app:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
