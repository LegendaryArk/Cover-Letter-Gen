"""Claude calls: extract job fields, research the company, write the tailored slots."""

from __future__ import annotations

from functools import lru_cache

import anthropic
from pydantic import BaseModel

MODEL = "claude-sonnet-5-5"
# Server-side fallback: if a safety classifier declines, the API re-runs the
# request on Anthropic's recommended fallback model instead of failing.
FALLBACK_KW = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


class ClaudeError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic()


def _check(response) -> None:
    if response.stop_reason == "refusal":
        raise ClaudeError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise ClaudeError("Claude's response was cut off (max_tokens).")


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


# ---------------------------------------------------------------- extraction

class FieldValue(BaseModel):
    name: str
    value: str


class JobInfo(BaseModel):
    company: str
    role: str
    company_short: str
    role_short: str
    fields: list[FieldValue]


EXTRACT_SYSTEM = """You extract facts from job postings to fill a cover letter template.
Return the company name, the exact role title, and short filename-friendly forms of each
(company_short: the common short name, e.g. "Stripe" not "Stripe, Inc."; role_short: the title
without requisition numbers or locations, e.g. "Software Engineer Intern").
For each requested template field, give the value exactly as it should appear in the letter.
If the posting doesn't state a value, give a sensible neutral default (e.g. hiring_manager ->
"Hiring Manager", team -> the role's department if implied) and never invent specific facts."""


async def extract_job(jd: str, field_names: list[str]) -> JobInfo:
    fields = ", ".join(field_names) if field_names else "(none)"
    response = await client().beta.messages.parse(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        output_format=JobInfo,
        system=EXTRACT_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"<job_description>\n{jd}\n</job_description>\n\nTemplate fields to fill: {fields}",
        }],
        **FALLBACK_KW,
    )
    _check(response)
    return response.parsed_output


# ---------------------------------------------------------------- research

RESEARCH_SYSTEM = """You research companies so a job applicant can write a few specific, genuine
sentences in a cover letter. Search the web, then write a compact brief (under 250 words) of
concrete, verifiable points: what the company builds and for whom, mission/values in their own
words, notable recent launches or news (with approximate dates), and anything about the teams
or roles listed. Prefer specifics over generic praise. Note uncertainty rather than guessing."""


async def research_company(company: str, roles: list[str], jd_excerpt: str) -> str:
    messages = [{
        "role": "user",
        "content": (
            f"Company: {company}\nRoles being applied to: {', '.join(roles)}\n\n"
            f"<job_description_excerpt>\n{jd_excerpt[:4000]}\n</job_description_excerpt>"
        ),
    }]
    for _ in range(5):  # resume on pause_turn (long server-side search turns)
        async with client().beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=RESEARCH_SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}],
            messages=messages,
            **FALLBACK_KW,
        ) as stream:
            response = await stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    _check(response)
    return _text(response)


# ---------------------------------------------------------------- writing

class WrittenSlot(BaseModel):
    id: str
    text: str


class WrittenSlots(BaseModel):
    slots: list[WrittenSlot]


WRITE_SYSTEM = """You fill in the personalized parts of a job applicant's cover letter template.
The template is the applicant's own writing; markers like [WRITE w1: guidance] are the only
places you write. For each requested slot:
- Follow the slot's guidance exactly. If it offers choices (e.g. "pick one: A / B / C"), choose
  the best fit for this company and role, and write about that choice.
- Be brief: one or two sentences unless the guidance says otherwise. No filler, no clichés,
  no exclamation marks.
- Write in the applicant's voice, matching the surrounding template's tone and tense, so the
  text reads seamlessly in place (consider the words immediately before and after the marker,
  including capitalization and ending punctuation).
- Ground claims in the company research and job description. Never invent facts about the
  company or the applicant.
- Don't restate the applicant's resume; a cover letter should add motivation and fit, not
  repeat bullet points.
Return the text for each requested slot id, with no surrounding quotes or markers."""


async def write_slots(
    *,
    template_text: str,
    slots: list[dict],
    jd: str,
    fields: dict[str, str],
    research: str,
    resume: str | None,
    sibling_roles: list[str],
    hint: str | None = None,
    previous: dict[str, str] | None = None,
) -> dict[str, str]:
    if not slots:
        return {}
    parts = [f"<template>\n{template_text}\n</template>"]
    parts.append("<filled_fields>\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n</filled_fields>")
    parts.append(f"<company_research>\n{research or '(none)'}\n</company_research>")
    parts.append(f"<job_description>\n{jd}\n</job_description>")
    if resume:
        parts.append(
            "<applicant_resume>\n" + resume + "\n</applicant_resume>\n"
            "Use the resume only to understand the applicant's background and pick relevant angles; don't repeat it."
        )
    if sibling_roles:
        parts.append(
            "The applicant is also applying to these other roles at the same company: "
            + ", ".join(sibling_roles)
            + ". Tailor the text to what is specific about THIS role so the letters differ meaningfully."
        )
    if previous:
        parts.append(
            "Previous attempt (write something different):\n"
            + "\n".join(f"{k}: {v}" for k, v in previous.items())
        )
    if hint:
        parts.append(f"Applicant's note for this rewrite: {hint}")
    ids = ", ".join(s["id"] for s in slots)
    parts.append(f"Write these slots: {ids}")

    response = await client().beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        output_format=WrittenSlots,
        system=WRITE_SYSTEM,
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
        **FALLBACK_KW,
    )
    _check(response)
    wanted = {s["id"] for s in slots}
    result = {s.id: s.text.strip() for s in response.parsed_output.slots if s.id in wanted}
    missing = wanted - result.keys()
    if missing:
        raise ClaudeError(f"Claude didn't return text for: {', '.join(sorted(missing))}")
    return result


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ClaudeError):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY in .env."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API; try again shortly."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API (network error)."
    if isinstance(exc, anthropic.AnthropicError) and "api_key" in str(exc).lower():
        return "Anthropic API key missing. Set ANTHROPIC_API_KEY in .env."
    return f"{type(exc).__name__}: {exc}"
