"""Building a Scriptor bundle for a book, and judging whether it may replace the PDF.

A bundle carries citation addresses the PDF path cannot give, but a bad run
costs more than it brings: text that silently went missing, footnotes that were
dropped, page labels that belong to another page. Four conditions decide, all
measured on the run itself (Naht §5, user's release of 2026-09-10):

1. **coverage** -- the text of the PDF's pages is in the master (>= 98 % of
   pages). A page is judged by all of its eight-word windows, not by two
   probes: a probe straddling a footnote boundary fails although the text is
   there, which made the two-probe measure call registers and tables of
   contents lost.
2. **notes** -- the master defines at least as many footnotes as Scriptor's own
   audit counted with certainty. A gap is a silent loss of apparatus.
3. **attested** -- the bundle only replaces the PDF if it brings citation
   addresses at all (>= 20 % of pages attested).
4. **inherited** -- the share of text standing under another page's number
   stays below 5 %. Good volumes measure 0 to 1.8 %, damage cases 16 % and up
   (51 volumes, September 2026). Scriptor computes it; a bundle whose sidecar
   does not carry the field is not admitted, because nothing else can tell.

The word balance that §2 of the briefing proposed is deliberately not among
them: Scriptor drops running heads and feet on purpose, so every volume loses
4-5 % of its words, the flawless ones included.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_COVERAGE = 0.98
MIN_ATTESTED = 0.20
MAX_INHERITED = 0.05

# A page with less text than this is a plate, a divider or blank -- it carries
# no window worth checking.
_MIN_PAGE_WORDS = 40
_WINDOW_WORDS = 8
# Below this a normalised window is too short to be a distinctive fingerprint.
_MIN_WINDOW_CHARS = 25

_NON_WORD = re.compile(r"[^0-9a-zà-öø-ÿ]+", re.IGNORECASE)
_DEFINITION = re.compile(r"^\[\^[^\]]+\]:", re.MULTILINE)
_AUDIT_CERTAIN = re.compile(r"(\d+) certain footnotes")
# Spec markup that stands for nothing on the page: page markers, their anchors,
# region marks. They normalise to digits in the middle of a sentence, so a
# window straddling one would never be found although its words are all there.
_SPEC_MARKUP = re.compile(r"\[p\.[^\]]*\]|\{#p-[^}]*\}|\[region:[^\]]*\]")
# A footnote anchor and its definition head stand for a number that IS on the
# page -- the superscript. Reduced to that number they match the PDF's text
# layer, where the superscript sits against the word ("fois.1 Autre exemple").
_FOOTNOTE_MARK = re.compile(r"\[\^([^\]]+)\]:?")


def _normalise(text: str) -> str:
    """Letters and digits only, lowercased.

    Hyphenation, line breaks, spacing and punctuation differ between a PDF's
    text layer and the reflowed master by design; none of them may cause a
    miss.
    """
    return _NON_WORD.sub("", text.lower())


def _haystack(master_text: str) -> str:
    """The master as the page's own text: markup removed, footnote marks kept
    as the numbers they replaced."""
    from scriptor.reflow.regions import strip_metadata_block

    text = strip_metadata_block(master_text)
    text = _SPEC_MARKUP.sub(" ", text)
    text = _FOOTNOTE_MARK.sub(r"", text)
    return _normalise(text)


def _windows(words: list[str]) -> list[str]:
    """Every eight-word window of a page, back to back."""
    out = [
        _normalise(" ".join(words[i:i + _WINDOW_WORDS]))
        for i in range(0, max(len(words) - _WINDOW_WORDS, 0), _WINDOW_WORDS)
    ]
    return [w for w in out if len(w) >= _MIN_WINDOW_CHARS]


def text_coverage(pdf_path: Path, master_text: str) -> tuple[float, list[int]]:
    """Share of the PDF's pages whose text is in the master, and the lost ones.

    The page is the unit, the windows are how a page is judged: it counts as
    present when at least half of its eight-word windows are in the master
    (the criterion of ``baltrusch_windows.py``). Counting windows instead of
    pages would measure something else -- every page loses the window that
    holds its running head and the one that holds its foot, both of which
    Scriptor removes on purpose, and a volume in perfect shape then reads
    92 to 96 %.
    """
    import pymupdf

    haystack = _haystack(master_text)
    checked = present = 0
    lost: list[int] = []
    with pymupdf.open(pdf_path) as doc:
        for number, page in enumerate(doc, 1):
            words = page.get_text().split()
            if len(words) < _MIN_PAGE_WORDS:
                continue
            windows = _windows(words)
            if not windows:
                continue
            checked += 1
            if sum(w in haystack for w in windows) >= len(windows) / 2:
                present += 1
            else:
                lost.append(number)
    if not checked:
        return 0.0, lost
    return present / checked, lost


def audit_certain_notes(audit_text: str) -> int | None:
    """The footnotes Scriptor's audit counted with certainty, or None."""
    match = _AUDIT_CERTAIN.search(audit_text)
    return int(match.group(1)) if match else None


def definition_count(master_text: str) -> int:
    """``[^N]:`` definitions in the master."""
    return len(_DEFINITION.findall(master_text))


@dataclass
class BundleCheck:
    """What the four conditions measured, and whether the bundle is admitted."""

    coverage: float | None = None
    lost_pages: list[int] = field(default_factory=list)
    definitions: int | None = None
    certain_notes: int | None = None
    attested: float | None = None
    inherited: float | None = None
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def admitted(self) -> bool:
        return not self.reasons and self.error is None

    def as_dict(self) -> dict:
        return {
            "coverage": None if self.coverage is None else round(self.coverage, 4),
            "lost_pages": self.lost_pages[:20],
            "definitions": self.definitions,
            "certain_notes": self.certain_notes,
            "attested": self.attested,
            "inherited": self.inherited,
            "admitted": self.admitted,
            "reasons": self.reasons,
            "error": self.error,
        }


def check_bundle(master: Path, pdf_path: Path) -> BundleCheck:
    """Measure the four conditions on a finished Scriptor run.

    Reads the master and its sidecars; opens the PDF once. Never raises: a
    condition that cannot be measured is a reason to refuse, not a crash in
    the middle of a batch.
    """
    check = BundleCheck()
    try:
        master_text = master.read_text(encoding="utf-8")
    except OSError as exc:
        check.error = f"master unreadable: {exc}"
        return check

    # 1 -- text coverage
    try:
        check.coverage, check.lost_pages = text_coverage(pdf_path, master_text)
    except Exception as exc:
        check.error = f"coverage not measurable: {type(exc).__name__}: {exc}"
        return check
    if check.coverage < MIN_COVERAGE:
        check.reasons.append(
            f"text coverage {check.coverage:.1%} < {MIN_COVERAGE:.0%}"
            + (f" ({len(check.lost_pages)} pages lost)" if check.lost_pages else "")
        )

    # 2 -- notes against the audit
    check.definitions = definition_count(master_text)
    audit = master.with_name(master.name + ".audit.txt")
    if audit.exists():
        check.certain_notes = audit_certain_notes(audit.read_text(encoding="utf-8"))
    if check.certain_notes is not None and check.definitions < check.certain_notes:
        check.reasons.append(
            f"{check.definitions} definitions < {check.certain_notes} certain footnotes"
        )

    # 3 and 4 -- what the pagination sidecar witnessed
    sidecar = master.with_name(master.name + ".pagination.json")
    profile: dict = {}
    if sidecar.exists():
        try:
            profile = json.loads(sidecar.read_text(encoding="utf-8")).get("profile", {})
        except (OSError, json.JSONDecodeError) as exc:
            check.reasons.append(f"pagination sidecar unreadable: {exc}")
    else:
        check.reasons.append("no pagination sidecar")

    check.attested = profile.get("attested")
    check.inherited = profile.get("inherited")

    if check.attested is None:
        if sidecar.exists():
            check.reasons.append("sidecar names no attested share")
    elif check.attested < MIN_ATTESTED:
        check.reasons.append(
            f"only {check.attested:.0%} of pages attested < {MIN_ATTESTED:.0%}"
        )

    # A missing field counts as unknown, and unknown is not admitted: nothing
    # but the producer can count it (Scriptor a77f765).
    if check.inherited is None:
        if sidecar.exists():
            check.reasons.append("sidecar names no inherited share (Scriptor too old)")
    elif check.inherited >= MAX_INHERITED:
        check.reasons.append(
            f"{check.inherited:.0%} of the text cites as another page >= {MAX_INHERITED:.0%}"
        )

    return check


def open_decisions(master: Path) -> int:
    """Lines in the decisions sidecar -- the handwork the volume still invites."""
    path = master.with_name(master.name + ".decisions.txt")
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
