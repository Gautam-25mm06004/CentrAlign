"""What the user's request permits: which cases are in scope, and whether changes are allowed at all.

The request is the user's instruction, so its limits are enforced by code rather than left to the model. The
supported request grammar is deliberately small and documented:

  scope   - all open cases (no case named)
          - named cases only ("resolve T-301", "only inv_1042")
          - all open cases except named ones ("all tickets except T-301", "excluding T-301 and T-302")
  mode    - write (default)
          - read-only: the console checkbox / --read-only, or clear information-only wording
            ("don't change anything", "without making edits", "just prepare recommendations", "dry run")

Anything outside this grammar is never silently widened into permission to write:
  - a named case that is not open is an unresolved scope issue (it never widens the scope);
  - a case both requested and excluded is ambiguous;
  - a partial restriction ("do not change customer contact details") or an amount/date filter
    ("refunds over 5,000", "orders before Monday") cannot be enforced by this prototype, so the run asks a
    human before any write: proceed with the listed cases, recommendations only, or stop.
"""
from __future__ import annotations

import re
from pathlib import Path

# Information-only intent for the whole request.
_INFO_ONLY = re.compile(
    r"\b(read[- ]?only|dry[- ]?run|no changes|report only|for review only|recommendations? only"
    r"|without (?:making |any |doing )*(?:any )?(?:changes|edits|modifications|updates|writes)"
    r"|without (?:changing|modifying|editing|updating|touching|saving) (?:anything|any(?:thing)? (?:records?|data))"
    r"|(?:do not|don['’]?t|never) (?:change|modify|update|edit|touch|alter) (?:anything|any(?:thing)? (?:records?|data))"
    r"|(?:do not|don['’]?t|never) (?:make|do|save) (?:any )?(?:changes|edits|modifications|updates|writes)"
    r"|(?:just|only) (?:tell|list|report|show|check|review|summari[sz]e|inspect|look|prepare|recommend|suggest|draft)"
    r"|(?:prepare|give|write|draft) (?:me )?(?:a |the |your )?recommendations?)\b", re.I)

# A restriction on part of the work ("do not issue refunds", "don't change customer contact details").
_PARTIAL_RESTRICTION = re.compile(
    r"\b(?:do not|don['’]?t|never|without|avoid|no need to)\s+(?:\w+\s+){0,1}?"
    r"(?:issue|change|modify|update|edit|touch|alter|post|save|submit|enrol|enroll|refund|replace|reply|send|"
    r"message|email|delete|remove|reject|approve|waitlist|contact)\w*\b(?!\s+(?:anything|any changes))", re.I)

# Amount and date filters the prototype cannot enforce.
_FILTER = re.compile(
    r"\b(?:above|over|below|under|more than|less than|greater than|at least|at most|up to)\s+(?:₹|rs\.?|inr)?\s?\d"
    r"|\b(?:before|after|since|until|older than|newer than)\s+(?:\d|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|yesterday|today|last|this|next|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)

_EXCLUDE = re.compile(r"\b(?:except(?: for)?|excluding|exclude|other than|apart from|but not|leaving out|"
                      r"skip(?:ping)?|not including|besides)\b", re.I)


def request_mode(task: str) -> tuple[str, str]:
    """('read_only' | 'write' | 'unclear', reason). 'unclear' means: ask a human before any write."""
    task = task or ""
    if _INFO_ONLY.search(task):
        return "read_only", "the request asks for information only"
    partial = _PARTIAL_RESTRICTION.search(task)
    if partial:
        return "unclear", (f"the request restricts part of the work (\"{_clause(task, partial)}\"), which this "
                           f"prototype cannot enforce field by field")
    flt = _FILTER.search(task)
    if flt:
        return "unclear", f"the request filters cases by amount or date (\"{_clause(task, flt)}\"), which is not supported"
    return "write", ""


def _clause(task: str, m: re.Match) -> str:
    """The words from the match to the end of its clause, for a readable explanation."""
    end = re.search(r"[.;,\n]", task[m.start():])
    return task[m.start():m.start() + end.start() if end else len(task)].strip()


def looks_read_only(task: str) -> bool:
    return request_mode(task)[0] == "read_only"


def _stem(c: str) -> str:
    return Path(c).stem if "." in c else c


def _id_patterns(candidates: list[str]) -> list[re.Pattern]:
    """Shapes of this company's case ids, learned from the open cases (T-301 -> T-\\d+, inv_1042.pdf -> inv_\\d+)."""
    shapes = set()
    for c in candidates:
        shapes.add(re.sub(r"\\\\d", r"\\d", re.sub(r"\d+", r"\\d+", re.escape(_stem(c)))))
    return [re.compile(rf"(?<![\w-])({sh})(?:\.\w+)?(?![\w-])", re.I) for sh in shapes]


def _ids_in(text: str, candidates: list[str]) -> list[str]:
    """Case ids mentioned in text, in order (open or not), as they map to candidates when open."""
    found = []
    for p in _id_patterns(candidates):
        for m in p.finditer(text):
            found.append((m.start(), m.group(1)))
    out = []
    for _, raw in sorted(found):
        match = next((c for c in candidates if _stem(c).lower() == raw.lower()), raw)
        if match not in out:
            out.append(match)
    return out


def _split_exclusion(task: str) -> tuple[str, str]:
    """(included part, excluded part). The excluded part runs from the exclusion word to the end of its clause."""
    m = _EXCLUDE.search(task)
    if not m:
        return task, ""
    end = re.search(r"[.;\n]|\b(?:and then|then|but)\b", task[m.end():])
    stop = m.end() + end.start() if end else len(task)
    return task[:m.start()] + " " + task[stop:], task[m.start():stop]


def interpret(task: str, candidates: list[str]) -> dict:
    """Scope of a request: {'cases', 'note', 'unresolved' (named but not open), 'issues' (need a human before writes)}."""
    task = task or ""
    incl_text, excl_text = _split_exclusion(task)
    included, excluded = _ids_in(incl_text, candidates), _ids_in(excl_text, candidates)
    open_set = set(candidates)
    unknown_incl = [c for c in included if c not in open_set]
    unknown_excl = [c for c in excluded if c not in open_set]
    included = [c for c in included if c in open_set]
    excluded = [c for c in excluded if c in open_set]
    issues, notes = [], []
    both = sorted(set(included) & set(excluded))
    if both:
        issues.append(f"{', '.join(both)} is both requested and excluded")
    if included or unknown_incl:
        cases = [c for c in included if c not in excluded]
        notes.append(f"request names {len(included) + len(unknown_incl)} case(s): scope limited to "
                     f"{', '.join(cases) or 'none'}")
    else:
        cases = [c for c in candidates if c not in excluded]
        notes.append(f"all {len(candidates)} open case(s)" + (f" except {', '.join(excluded)}" if excluded else "")
                     + f": {len(cases)} in scope")
    if unknown_incl:
        notes.append(f"not open or not found: {', '.join(unknown_incl)}")
    if unknown_excl:
        notes.append(f"excluded but not open: {', '.join(unknown_excl)}")
    return {"cases": cases, "note": "; ".join(notes), "unresolved": unknown_incl, "issues": issues}


def narrow_scope(task: str, candidates: list[str]) -> tuple[list[str], str]:
    """Compatibility wrapper: (scope, explanation)."""
    s = interpret(task, candidates)
    return s["cases"], s["note"]
