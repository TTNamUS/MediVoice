"""Markdown → Chunk splitter with section breadcrumbs.

Splits clinic KB docs into chunks sized ≤ 500 tokens by heading boundaries,
then by paragraph if a section is too long. Prepends a breadcrumb so
the embedding captures full context:
    "Cancellation Policy > 24-hour rule: ..."
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

_MAX_TOKENS = 500
# Rough token estimate: 1 token ≈ 4 chars (conservative for English prose)
_CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    id: str
    doc_id: str
    title: str
    category: str
    tags: list[str]
    text: str  # breadcrumb + content
    tokens: int
    source_path: str


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split YAML front-matter from body. Returns (meta, body)."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.index("---", 3)
    meta = yaml.safe_load(raw[3:end])
    body = raw[end + 3 :].strip()
    return meta or {}, body


def _split_by_headings(body: str) -> list[tuple[str, str]]:
    """Return [(heading, content)] pairs. Top-level # heading is the doc title."""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in body.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    return [(h, c) for h, c in sections if c.strip()]


def _split_by_paragraphs(text: str, max_tokens: int) -> list[str]:
    """Further split a long section by blank-line paragraphs."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        pt = _estimate_tokens(para)
        if current_tokens + pt > max_tokens and current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = []
            current_tokens = 0
        current_parts.append(para)
        current_tokens += pt

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks or [text]


def chunk_document(path: Path) -> list[Chunk]:
    """Chunk a single KB markdown file into Chunk objects."""
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)

    doc_id = meta.get("id", path.stem)
    title = meta.get("title", path.stem)
    category = meta.get("category", "general")
    tags = meta.get("tags", [])

    sections = _split_by_headings(body)
    if not sections:
        sections = [("", body)]

    chunks: list[Chunk] = []
    for idx, (heading, content) in enumerate(sections):
        # Build breadcrumb: "Cancellation Policy > 24-hour rule"
        if heading and heading.lower() != title.lower():
            breadcrumb = f"{title} > {heading}"
        else:
            breadcrumb = title

        sub_chunks = _split_by_paragraphs(content, _MAX_TOKENS - _estimate_tokens(breadcrumb) - 4)

        for sub_idx, sub_text in enumerate(sub_chunks):
            full_text = f"{breadcrumb}: {sub_text}"
            # Deterministic ID: sha256-derived hex
            chunk_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{idx}:{sub_idx}").hex[:16]

            chunks.append(
                Chunk(
                    id=chunk_id,
                    doc_id=doc_id,
                    title=title,
                    category=category,
                    tags=tags,
                    text=full_text,
                    tokens=_estimate_tokens(full_text),
                    source_path=str(path),
                )
            )

    return chunks


def chunk_directory(kb_dir: Path) -> list[Chunk]:
    """Chunk all .md files in a directory tree."""
    all_chunks: list[Chunk] = []
    for md_file in sorted(kb_dir.rglob("*.md")):
        all_chunks.extend(chunk_document(md_file))
    return all_chunks
