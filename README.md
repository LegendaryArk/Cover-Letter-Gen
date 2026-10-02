# Cover Letter Generator

Fill your own cover letter template from one or more job descriptions using Claude, then export
submission-ready PDFs named like `Noah_Sun_CoverLetter_Stripe_Software_Engineer_Intern.pdf`.

- **Simple fields** (company, role, location, hiring manager…) are extracted from the job description.
- **Written slots** get one or two sentences from Claude, following guidance you put in the template,
  grounded in a quick web search on the company.
- Several roles at the same company share one research pass, and each letter is tailored to its role.
- You review and edit everything (or ask for a rewrite with a note) before exporting.

## Setup

Requirements: Python 3.10+, LibreOffice (for PDF export), an Anthropic API key.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env          # then paste your ANTHROPIC_API_KEY into .env
.venv/bin/python -m cover_letter_gen.app
```

Open http://127.0.0.1:8765. Set `PORT=...` to use a different port.

## Writing the template

Write your letter in Google Docs as usual, with placeholders where things change:

| Placeholder | Filled with |
|---|---|
| `{{company}}`, `{{role}}`, `{{location}}`, `{{hiring_manager}}`, any `{{name}}` | Extracted from the job description |
| `{{date}}` | Today's date |
| `{{my_name}}`, `{{first_name}}`, `{{last_name}}` | Your name from the Setup panel |
| Anything in "Fixed fields" (e.g. `{{my_email}}`) | The value you entered |
| `{{? guidance}}` | 1–2 sentences written by Claude following your guidance |

Examples of written slots:

```
{{? One sentence on what draws me to their product. Pick one: mission / developer tools / recent launch}}
{{? Briefly connect my interest in distributed systems to this team's work}}
```

Placeholders take on the formatting of their first character, so style them the way you want the text
to look. Then **File → Download → Microsoft Word (.docx)** and upload that file in the app. The setup
panel lists every placeholder it found, so you can check they were all recognized.

## Using it

1. **Setup:** upload the template, optionally a resume (PDF/.docx/text), and enter your name.
2. **Jobs:** paste one job description per box. "Include resume as context" is off by default:
   the letter shouldn't repeat your resume, but turning it on can help Claude pick relevant angles.
3. **Review:** edit any field or sentence, click **Rewrite** (with an optional note) to redo one slot,
   then **Preview PDF**.
4. **Export:** PDFs are saved to `output/` and can be downloaded one at a time or as a zip.

Your template, resume, and settings are stored locally in `data/` (gitignored).

## How it uses Claude

All calls use `claude-sonnet-5-5` with adaptive thinking, and server-side refusal fallback is turned on.

1. Per job: extract the company, role, and field values (structured output, low effort).
2. Per company: research with the web search tool to produce a short brief.
3. Per job: write the guided slots (structured output, high effort).

## Tests

```bash
.venv/bin/python -m pytest
```
