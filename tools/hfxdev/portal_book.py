# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from typing import Any

from .model import ModelError


CHAPTER = re.compile(r"^Chapter (?P<roman>[IVXLCDM]+): (?P<title>.+)$")
SECTION = re.compile(r"^(?P<number>[1-9][0-9]*)\. (?P<title>.+)$")


@dataclass(frozen=True)
class DesignSection:
    number: int
    title: str
    markdown: str


@dataclass(frozen=True)
class DesignChapter:
    index: int
    roman: str
    title: str
    introduction: str
    sections: tuple[DesignSection, ...]

    @property
    def slug(self) -> str:
        return f"chapter-{self.index:02d}"

    @property
    def section_range(self) -> str:
        first = self.sections[0].number
        last = self.sections[-1].number
        return str(first) if first == last else f"{first}-{last}"


@dataclass(frozen=True)
class DesignBook:
    title: str
    chapters: tuple[DesignChapter, ...]

    @property
    def section_count(self) -> int:
        return sum(len(chapter.sections) for chapter in self.chapters)


def parse_design_book(source: str) -> DesignBook:
    lines = source.splitlines()
    if not lines or not lines[0].strip():
        raise ModelError("design book is empty")
    title = lines[0].strip()
    chapters: list[DesignChapter] = []
    chapter_roman: str | None = None
    chapter_title: str | None = None
    chapter_intro: list[str] = []
    sections: list[DesignSection] = []
    section_number: int | None = None
    section_title: str | None = None
    section_body: list[str] = []
    in_fence = False

    def finish_section() -> None:
        nonlocal section_number, section_title, section_body
        if section_number is None or section_title is None:
            return
        sections.append(
            DesignSection(
                number=section_number,
                title=section_title,
                markdown="\n".join(section_body).strip(),
            )
        )
        section_number = None
        section_title = None
        section_body = []

    def finish_chapter() -> None:
        nonlocal chapter_roman, chapter_title, chapter_intro, sections
        if chapter_roman is None or chapter_title is None:
            return
        finish_section()
        if not sections:
            raise ModelError(f"design book chapter {chapter_roman} has no sections")
        chapters.append(
            DesignChapter(
                index=len(chapters) + 1,
                roman=chapter_roman,
                title=chapter_title,
                introduction="\n".join(chapter_intro).strip(),
                sections=tuple(sections),
            )
        )
        chapter_roman = None
        chapter_title = None
        chapter_intro = []
        sections = []

    for raw in lines[1:]:
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        chapter_match = None if in_fence else CHAPTER.fullmatch(stripped)
        if chapter_match is not None:
            finish_chapter()
            chapter_roman = chapter_match.group("roman")
            chapter_title = chapter_match.group("title")
            continue
        section_match = None if in_fence else SECTION.fullmatch(stripped)
        if section_match is not None:
            if chapter_title is None:
                raise ModelError("design book section appears before its chapter")
            finish_section()
            section_number = int(section_match.group("number"))
            section_title = section_match.group("title")
            continue
        if section_number is None:
            chapter_intro.append(raw)
        else:
            section_body.append(raw)
    finish_chapter()
    if not chapters:
        raise ModelError("design book contains no chapters")
    numbers = [section.number for chapter in chapters for section in chapter.sections]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ModelError("design book sections are not contiguous")
    return DesignBook(title=title, chapters=tuple(chapters))


def chapter_markdown(chapter: DesignChapter) -> str:
    parts = []
    if chapter.introduction:
        parts.append(chapter.introduction)
    for section in chapter.sections:
        parts.append(
            f'<a id="section-{section.number}"></a>\n\n'
            f"## {section.number}. {section.title}\n\n{section.markdown}"
        )
    return "\n\n".join(parts).strip() + "\n"


def render_book_index(book: DesignBook, coverage: tuple[Any, ...]) -> str:
    coverage_by_section = {entry.section: entry for entry in coverage}
    chapter_rows = []
    for chapter in book.chapters:
        entries = [coverage_by_section[section.number] for section in chapter.sections]
        blocking = sum(entry.release_blocking for entry in entries)
        verified = sum(entry.status == "software-verified" for entry in entries)
        chapter_rows.append(
            '<a class="book-chapter" href="design-book/'
            f'{escape(chapter.slug)}.html">'
            f'<span class="book-number">{escape(chapter.roman)}</span>'
            '<span class="book-chapter-copy">'
            f'<strong>{escape(chapter.title)}</strong>'
            f'<small>Sections {escape(chapter.section_range)} | '
            f'{verified} software verified | {blocking} release blocking</small>'
            '</span><span class="book-arrow" aria-hidden="true">&rarr;</span></a>'
        )
    blocking_total = sum(entry.release_blocking for entry in coverage)
    verified_total = sum(entry.status == "software-verified" for entry in coverage)
    return f"""<article class="design-book-index">
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="architecture.html">Architecture</a><span>Design book</span></nav>
  <header class="page-hero page-hero--book"><p class="page-kicker">Product and engineering specification</p><h1>Design book</h1><p class="lede">Sixty-seven decisions organized as a readable twelve-chapter book, with implementation coverage kept beside the specification rather than repeated through it.</p></header>
  <section class="book-summary" aria-label="Design book summary"><div><strong>{len(book.chapters)}</strong><span>chapters</span></div><div><strong>{book.section_count}</strong><span>design sections</span></div><div><strong>{verified_total}</strong><span>software verified</span></div><div><strong>{blocking_total}</strong><span>release blocking</span></div></section>
  <div class="book-layout"><section aria-labelledby="book-contents"><div class="section-intro"><p class="page-kicker">Contents</p><h2 id="book-contents">Read by chapter</h2><p>Each chapter has its own address, outline, and previous or next navigation. Coverage status remains generated from the assurance ledger.</p></div><div class="book-chapters">{''.join(chapter_rows)}</div></section><aside class="book-note"><strong>One source</strong><p>The book is still compiled from <code>docs/architecture/design-book.md</code>. Chapter pages are generated; no second copy is maintained.</p><a href="../maintainers/coverage.html">Inspect implementation coverage</a></aside></div>
</article>"""


def chapter_search_text(chapter: DesignChapter) -> str:
    return " ".join(
        [chapter.title]
        + [f"{section.number} {section.title} {section.markdown}" for section in chapter.sections]
    ).lower()
