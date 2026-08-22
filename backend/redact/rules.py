"""Deterministic redaction detectors. The safety net under the model.

Every span this module returns has source="rule" and confidence=1.0. If Ollama is
slow, wrong, or dead, this layer is what still puts black bars on the screen, so it
must stay fast (<5ms per segment) and it must not guess. Regexes are precompiled at
import time.

Two design notes worth knowing before you change anything here:

  * Whisper writes spoken numbers as words, so "NHS number four zero zero, one two
    three, four five six four" never matches a digit regex. The spoken-digit scanner
    below converts word runs to digits, applies the same Modulus 11 check, and maps
    the result back to the original character offsets.
  * The NHS Modulus 11 check is the point of this layer. A ten digit number that
    fails the check is not an NHS number and we deliberately do not redact it.
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from shared.contracts import Exemption, RedactionSpan

MONEY_THRESHOLD_GBP = 10_000.0

# An NHS number is personal data first and health data second. The FOI officer's
# default is s.40(2); flip this constant to Exemption.S38 if your redaction policy
# treats the number itself as health information.
NHS_EXEMPTION = Exemption.S40_2


def _span(start: int, end: int, exemption: Exemption, text: str) -> RedactionSpan:
    return RedactionSpan(
        start=start,
        end=end,
        exemption=exemption,
        surface=text[start:end],
        source="rule",
        confidence=1.0,
    )


# --------------------------------------------------------------------------
# NHS number
# --------------------------------------------------------------------------

_NHS_DIGITS_RE = re.compile(r"(?<!\d)(\d{3})[ \-]?(\d{3})[ \-]?(\d{4})(?!\d)")

_DIGIT_WORDS = {
    "zero": 0, "oh": 0, "o": 0, "nought": 0, "naught": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_DIGIT_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_DIGIT_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_SPOKEN_GAP_RE = re.compile(r"^[\s,.\-–—]*$")


def nhs_check_digit_ok(digits: str) -> bool:
    """Modulus 11 on a 10 digit NHS number.

    Weights 10..2 run over the first nine digits. check = 11 - (sum mod 11).
    A result of 11 becomes 0. A result of 10 means the number is invalid.
    """
    if len(digits) != 10 or not (digits.isascii() and digits.isdigit()):
        return False
    total = sum(int(d) * w for d, w in zip(digits[:9], range(10, 1, -1)))
    check = 11 - (total % 11)
    if check == 11:
        check = 0
    if check == 10:
        return False
    return check == int(digits[9])


def _spoken_digit_tokens(text: str) -> list[tuple[int, int, int]]:
    """(start, end, value) for every digit word in text."""
    return [
        (m.start(), m.end(), _DIGIT_WORDS[m.group(1).lower()])
        for m in _DIGIT_WORD_RE.finditer(text)
    ]


def _spoken_runs(text: str) -> Iterator[list[tuple[int, int, int]]]:
    """Group digit words into runs separated by nothing but whitespace/punctuation."""
    run: list[tuple[int, int, int]] = []
    prev_end = -1
    for tok in _spoken_digit_tokens(text):
        if run and not _SPOKEN_GAP_RE.match(text[prev_end:tok[0]]):
            if len(run) >= 10:
                yield run
            run = []
        run.append(tok)
        prev_end = tok[1]
    if len(run) >= 10:
        yield run


def find_nhs_numbers(text: str) -> list[RedactionSpan]:
    spans: list[RedactionSpan] = []
    for m in _NHS_DIGITS_RE.finditer(text):
        if nhs_check_digit_ok("".join(m.groups())):
            spans.append(_span(m.start(), m.end(), NHS_EXEMPTION, text))

    for run in _spoken_runs(text):
        i = 0
        while i + 10 <= len(run):
            window = run[i:i + 10]
            digits = "".join(str(v) for _, _, v in window)
            if nhs_check_digit_ok(digits):
                spans.append(_span(window[0][0], window[-1][1], NHS_EXEMPTION, text))
                i += 10
            else:
                i += 1
    return spans


# --------------------------------------------------------------------------
# National Insurance number
# --------------------------------------------------------------------------

_NINO_RE = re.compile(
    r"\b(?![DFIQUV])[A-CEGHJ-PR-TW-Z]"      # first letter
    r"(?![DFIOQUV])[A-CEGHJ-NPR-TW-Z]"      # second letter
    r"\s?\d{2}\s?\d{2}\s?\d{2}\s?"
    r"[A-D]\b"
)
_NINO_FORBIDDEN_PREFIXES = {"BG", "GB", "NK", "KN", "TN", "NT", "ZZ"}


def find_nino(text: str) -> list[RedactionSpan]:
    spans = []
    for m in _NINO_RE.finditer(text.upper()):
        if m.group(0)[:2] in _NINO_FORBIDDEN_PREFIXES:
            continue
        end = m.end()
        while end > m.start() and text[end - 1].isspace():
            end -= 1
        spans.append(_span(m.start(), end, Exemption.S40_2, text))
    return spans


# --------------------------------------------------------------------------
# Postcode, email, phone
# --------------------------------------------------------------------------

_POSTCODE_RE = re.compile(
    r"\b(?:GIR\s?0AA|"
    r"(?:[A-PR-UWYZ][0-9]{1,2}|"
    r"[A-PR-UWYZ][A-HK-Y][0-9]{1,2}|"
    r"[A-PR-UWYZ][0-9][A-HJKPSTUW]|"
    r"[A-PR-UWYZ][A-HK-Y][0-9][ABEHMNPRVWXY])"
    r"\s?[0-9][ABD-HJLNP-UW-Z]{2})\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_PHONE_RE = re.compile(
    r"(?<![\d\w])(?:"
    r"(?:\+44\s?|0)7\d{3}\s?\d{3}\s?\d{3}"                 # mobile
    r"|(?:\+44\s?\(0\)\s?|\+44\s?|0)[123]\d{0,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"  # landline
    r")(?![\d])"
)


def find_postcodes(text: str) -> list[RedactionSpan]:
    return [_span(m.start(), m.end(), Exemption.S40_2, text)
            for m in _POSTCODE_RE.finditer(text)]


def find_emails(text: str) -> list[RedactionSpan]:
    return [_span(m.start(), m.end(), Exemption.S40_2, text)
            for m in _EMAIL_RE.finditer(text)]


def find_phones(text: str) -> list[RedactionSpan]:
    spans = []
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 10:
            continue
        spans.append(_span(m.start(), m.end(), Exemption.S40_2, text))
    return spans


# --------------------------------------------------------------------------
# Bank details
# --------------------------------------------------------------------------

_SORT_CODE_RE = re.compile(r"(?<!\d)\d{2}[- ]\d{2}[- ]\d{2}(?!\d)")
_ACCOUNT_CUE_RE = re.compile(
    r"\b(?:account(?:\s+number)?|acc(?:t)?\.?\s*(?:no\.?|number)?)\b[^\d\n]{0,20}(\d{8})(?!\d)",
    re.IGNORECASE,
)


def find_bank_details(text: str) -> list[RedactionSpan]:
    spans = [_span(m.start(), m.end(), Exemption.S40_2, text)
             for m in _SORT_CODE_RE.finditer(text)]
    for m in _ACCOUNT_CUE_RE.finditer(text):
        spans.append(_span(m.start(1), m.end(1), Exemption.S40_2, text))
    return spans


# --------------------------------------------------------------------------
# Dates of birth
# --------------------------------------------------------------------------

_MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
           r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE_NUMERIC = r"\b(?:0?[1-9]|[12]\d|3[01])[/.\-](?:0?[1-9]|1[0-2])[/.\-](?:19|20)?\d{2}\b"
_DATE_WORDY = (r"\b(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTHS +
               r"\.?\s+(?:19|20)\d{2}\b")
_DOB_CUE = r"(?:born(?:\s+on)?|date\s+of\s+birth|d\.?o\.?b\.?|DOB|birthday)"

_DOB_RE = re.compile(
    _DOB_CUE + r"[^\n]{0,15}?(" + _DATE_NUMERIC + r"|" + _DATE_WORDY + r")",
    re.IGNORECASE,
)
_BARE_DATE_RE = re.compile(_DATE_NUMERIC + r"|" + _DATE_WORDY, re.IGNORECASE)


def find_dates_of_birth(text: str) -> list[RedactionSpan]:
    """Cued dates are always a DOB. Bare dates count only when a cue word is nearby."""
    spans: list[RedactionSpan] = []
    cued: set[tuple[int, int]] = set()
    for m in _DOB_RE.finditer(text):
        spans.append(_span(m.start(), m.end(), Exemption.S40_2, text))
        cued.add((m.start(1), m.end(1)))

    lowered = text.lower()
    for m in _BARE_DATE_RE.finditer(text):
        if (m.start(), m.end()) in cued:
            continue
        if any((m.start(), m.end()) >= (s.start, s.start) and m.end() <= s.end for s in spans):
            continue
        window = lowered[max(0, m.start() - 40):m.start()]
        if re.search(_DOB_CUE, window, re.IGNORECASE):
            spans.append(_span(m.start(), m.end(), Exemption.S40_2, text))
    return spans


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "millions": 1_000_000,
    "bn": 1_000_000_000, "b": 1_000_000_000, "billion": 1_000_000_000,
}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT = r"(?:k|m|mn|bn|b|thousand|million|millions|billion)"

_MONEY_SYMBOL_RE = re.compile(
    r"£\s?(" + _NUM + r")(?:\s*(" + _MULT + r"))?\b",
    re.IGNORECASE,
)
_MONEY_WORD_RE = re.compile(
    r"\b(" + _NUM + r")\s*(" + _MULT + r")?\s*(?:pounds|GBP|sterling)\b",
    re.IGNORECASE,
)


def _money_value(number: str, mult: str | None) -> float:
    value = float(number.replace(",", ""))
    if mult:
        value *= _MULTIPLIERS[mult.lower()]
    return value


def find_money(text: str, threshold: float | None = None) -> list[RedactionSpan]:
    limit = MONEY_THRESHOLD_GBP if threshold is None else threshold
    spans = []
    for regex in (_MONEY_SYMBOL_RE, _MONEY_WORD_RE):
        for m in regex.finditer(text):
            if _money_value(m.group(1), m.group(2)) >= limit:
                spans.append(_span(m.start(), m.end(), Exemption.S43_2, text))
    return spans


# --------------------------------------------------------------------------
# Titled personal names and named suppliers
# --------------------------------------------------------------------------

_TITLE_NAME_RE = re.compile(
    r"\b(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Prof|Professor|Cllr|Councillor|Sir|Dame|Lord|Lady|"
    r"Rev|Reverend|Sgt|Insp|Chief Inspector)\.?\s+"
    r"(?:[A-Z][a-z’'\-]+)(?:\s+[A-Z][a-z’'\-]+){0,3}"
)
_POSSESSIVE_RE = re.compile(r"[’']s$")

_ORG_SUFFIX = (r"(?:Systems|Solutions|Ltd|Limited|LLP|PLC|Plc|Group|Holdings|Consulting|"
               r"Consultancy|Partners|Partnership|Services|Technologies|Technology|"
               r"Associates|Industries|Enterprises|Corporation|Contractors|Logistics)")
_ORG_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&’'\-]+\s+){1,3}" + _ORG_SUFFIX + r"\b"
)
_ORG_STOPWORDS = {"The", "This", "That", "Our", "Their", "These", "Those", "A", "An"}


def find_titled_names(text: str) -> list[RedactionSpan]:
    spans = []
    for m in _TITLE_NAME_RE.finditer(text):
        end = m.end()
        trimmed = _POSSESSIVE_RE.search(m.group(0))
        if trimmed:
            end = m.start() + trimmed.start()
        spans.append(_span(m.start(), end, Exemption.S40_2, text))
    return spans


def find_organisations(text: str) -> list[RedactionSpan]:
    spans = []
    for m in _ORG_RE.finditer(text):
        start = m.start()
        words = m.group(0).split()
        while words and words[0] in _ORG_STOPWORDS:
            start += len(words[0]) + 1
            words = words[1:]
        if len(words) < 2:
            continue
        spans.append(_span(start, m.end(), Exemption.S43_2, text))
    return spans


# --------------------------------------------------------------------------
# High precision cue phrases
# --------------------------------------------------------------------------
# These are narrow on purpose. They exist so the demo still redacts s.35 and s.42
# content when the model is unavailable. Widening them costs false positives.

_LEGAL_ADVISER = (r"(?:solicitors?|counsel|barristers?|lawyers?|legal team|legal department|"
                  r"legal services|monitoring officer|QC|KC)")

_CUE_PATTERNS: tuple[tuple[re.Pattern[str], Exemption], ...] = (
    (re.compile(
        r"\b(?:legal advice(?:\s+(?:from|by)\s+(?:the\s+|our\s+)?"
        r"(?:[A-Za-z’'\-]+\s+){0,3}" + _LEGAL_ADVISER + r")?"
        r"|advice\s+from\s+(?:the\s+|our\s+)?(?:[A-Za-z’'\-]+\s+){0,3}" + _LEGAL_ADVISER +
        r"|counsel['’]?s? (?:opinion|advice)"
        r"|legally privileged"
        r"|legal professional privilege)\b",
        re.IGNORECASE), Exemption.S42),
    (re.compile(
        r"\b(?:(?:is|are|was|were|remains?)\s+)?(?:still\s+)?in\s+formulation\b"
        r"|\bpolicy\s+(?:is\s+)?(?:still\s+)?(?:being\s+(?:developed|formulated)"
        r"|under\s+(?:development|formulation))\b",
        re.IGNORECASE), Exemption.S35),
    (re.compile(
        r"\b(?:(?:a|the)\s+)?(?:child|children|young person|case)\s+"
        r"(?:open\s+)?on\s+the\s+at[-\s]risk\s+register\b"
        r"|\bat[-\s]risk\s+register\b"
        r"|\bchild protection plan\b",
        re.IGNORECASE), Exemption.S40_2),
)


def find_cue_phrases(text: str) -> list[RedactionSpan]:
    return [_span(m.start(), m.end(), exemption, text)
            for regex, exemption in _CUE_PATTERNS
            for m in regex.finditer(text)]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

# Identifiers are matched, not judged: an NHS number either passes Modulus 11 or it
# does not. These must never depend on a model being up, so they always run.
_IDENTIFIER_DETECTORS = (
    find_nhs_numbers,
    find_nino,
    find_postcodes,
    find_emails,
    find_phones,
    find_bank_details,
    find_dates_of_birth,
)

# These approximate judgement — is this phrase a person, a supplier, unformed policy?
# That is Gemma's job. They run only when the model could not answer, so that the
# demo degrades to regex instead of dying, without pre-empting the model.
_JUDGEMENT_DETECTORS = (
    find_titled_names,
    find_organisations,
    find_cue_phrases,
)

_DETECTORS = _IDENTIFIER_DETECTORS + _JUDGEMENT_DETECTORS


def _run(detectors, text: str) -> list[RedactionSpan]:
    spans: list[RedactionSpan] = []
    for detector in detectors:
        spans.extend(detector(text))
    return spans


def find_identifier_spans(text: str, money_threshold: float | None = None) -> list[RedactionSpan]:
    """Identifiers and money. Always safe to trust, never needs the model."""
    if not text:
        return []
    spans = _run(_IDENTIFIER_DETECTORS, text)
    spans.extend(find_money(text, money_threshold))
    spans.sort(key=lambda s: (s.start, -s.end))
    return spans


def find_judgement_spans(text: str) -> list[RedactionSpan]:
    """The regex fallback for what Gemma normally decides."""
    if not text:
        return []
    spans = _run(_JUDGEMENT_DETECTORS, text)
    spans.sort(key=lambda s: (s.start, -s.end))
    return spans


def find_rule_spans(text: str, money_threshold: float | None = None) -> list[RedactionSpan]:
    """Every deterministic span in text, sorted by start offset. May overlap."""
    if not text:
        return []
    spans: list[RedactionSpan] = []
    for detector in _DETECTORS:
        spans.extend(detector(text))
    spans.extend(find_money(text, money_threshold))
    spans.sort(key=lambda s: (s.start, -s.end))
    return spans
