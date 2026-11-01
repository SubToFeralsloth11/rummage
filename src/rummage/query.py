"""Parse plain-English queries into filters + search text.

"that receipt from October"           -> month=10 (any year), text "receipt"
"the code where I did pathfinding"    -> kind=code, text "pathfinding"
"pdfs from last week"                 -> ext=pdf, modified in the last 7 days
"python script for scraping"          -> ext=py, text "scraping"
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTH_ABBR["sept"] = 9

STOPWORDS = {
    "a", "an", "the", "that", "this", "those", "these", "my", "mine", "me", "i", "i'm", "im", "we",
    "where", "which", "what", "when", "who", "did", "do", "does", "done", "was", "were", "is", "are",
    "be", "been", "it", "its", "of", "for", "from", "in", "on", "at", "to", "with", "about", "and",
    "or", "some", "any", "all", "find", "show", "search", "look", "get", "give", "file", "files",
    "thing", "things", "stuff", "one", "ones", "there", "had", "have", "has", "made", "make", "wrote",
    "written", "write", "saved", "save", "got", "used", "use", "using", "like", "kind", "sort", "please",
    "can", "you", "your", "had", "something", "somewhere", "around", "by", "into", "an", "our", "so",
}

# word -> (kind set, ext set, chip label)
KIND_WORDS = {
    "pdf": (set(), {"pdf"}, "PDF"), "pdfs": (set(), {"pdf"}, "PDF"),
    "image": ({"image"}, set(), "Images"), "images": ({"image"}, set(), "Images"),
    "photo": ({"image"}, set(), "Images"), "photos": ({"image"}, set(), "Images"),
    "picture": ({"image"}, set(), "Images"), "pictures": ({"image"}, set(), "Images"),
    "pic": ({"image"}, set(), "Images"), "pics": ({"image"}, set(), "Images"),
    "screenshot": ({"image"}, set(), "Screenshots"), "screenshots": ({"image"}, set(), "Screenshots"),
    "code": ({"code"}, set(), "Code"), "script": ({"code"}, set(), "Code"),
    "scripts": ({"code"}, set(), "Code"), "function": ({"code"}, set(), "Code"),
    "document": ({"doc", "pdf", "text"}, set(), "Documents"),
    "documents": ({"doc", "pdf", "text"}, set(), "Documents"),
    "doc": ({"doc", "pdf", "text"}, set(), "Documents"), "docs": ({"doc", "pdf", "text"}, set(), "Documents"),
    "note": ({"text"}, set(), "Notes"), "notes": ({"text"}, set(), "Notes"),
    "spreadsheet": (set(), {"xlsx", "csv", "ods", "numbers", "tsv"}, "Spreadsheets"),
    "spreadsheets": (set(), {"xlsx", "csv", "ods", "numbers", "tsv"}, "Spreadsheets"),
    "slides": (set(), {"pptx", "key", "odp"}, "Slides"),
    "presentation": (set(), {"pptx", "key", "odp"}, "Slides"),
    "video": ({"video"}, set(), "Videos"), "videos": ({"video"}, set(), "Videos"),
    "clip": ({"video"}, set(), "Videos"), "clips": ({"video"}, set(), "Videos"),
    "model": ({"model"}, set(), "3D models"), "models": ({"model"}, set(), "3D models"),
    "3d": ({"model"}, set(), "3D models"), "mesh": ({"model"}, set(), "3D models"),
    "blend": (set(), {"blend"}, "Blender"), "blender": (set(), {"blend"}, "Blender"),
    "blockbench": (set(), {"bbmodel"}, "Blockbench"),
    "audio": ({"audio"}, set(), "Audio"), "song": ({"audio"}, set(), "Audio"),
    "songs": ({"audio"}, set(), "Audio"), "music": ({"audio"}, set(), "Audio"),
    "sound": ({"audio"}, set(), "Audio"), "sounds": ({"audio"}, set(), "Audio"),
    "website": (set(), {"html", "htm"}, "Web pages"), "webpage": (set(), {"html", "htm"}, "Web pages"),
    "folder": ({"folder"}, set(), "Folders"), "folders": ({"folder"}, set(), "Folders"),
    "directory": ({"folder"}, set(), "Folders"), "directories": ({"folder"}, set(), "Folders"),
}
# Language words only become filters next to a "file/script/code" word: "python script".
LANG_EXTS = {
    "python": {"py", "ipynb"}, "gdscript": {"gd"}, "godot": {"gd", "gdshader"},
    "javascript": {"js", "mjs", "cjs", "jsx"}, "js": {"js", "mjs", "cjs", "jsx"},
    "typescript": {"ts", "tsx"}, "swift": {"swift"}, "html": {"html", "htm"}, "css": {"css", "scss"},
    "java": {"java"}, "c#": {"cs"}, "csharp": {"cs"}, "rust": {"rs"}, "go": {"go"}, "lua": {"lua"},
    "shell": {"sh", "zsh", "bash"}, "bash": {"sh", "bash"}, "json": {"json"}, "markdown": {"md"},
    "word": {"docx"}, "excel": {"xlsx"}, "powerpoint": {"pptx"},
}
FILE_NOUNS = {"file", "files", "script", "scripts", "code", "project", "doc", "docs", "document"}
DATE_PREPS = {"in", "from", "during", "on", "of", "since", "around", "last", "this"}


@dataclass
class Parsed:
    raw: str
    text: str = ""            # what's left for keyword + meaning search
    terms: list = field(default_factory=list)  # keyword terms
    kinds: set = field(default_factory=set)
    exts: set = field(default_factory=set)
    screenshot: bool = False
    after: float | None = None   # mtime >= after
    before: float | None = None  # mtime < before
    month: int | None = None     # month filter with no year: any year
    month_words: list = field(default_factory=list)  # also accept files whose text mentions the date
    chips: list = field(default_factory=list)

    @property
    def has_filters(self) -> bool:
        return bool(self.kinds or self.exts or self.screenshot or self.after or self.before or self.month)


def _ts(d: datetime) -> float:
    return d.timestamp()


def _month_range(year: int, month: int) -> tuple[float, float]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    return _ts(start), _ts(end)


def parse(q: str, now: datetime | None = None) -> Parsed:
    now = now or datetime.now()
    today = datetime(now.year, now.month, now.day)
    p = Parsed(raw=q)
    s = " " + q.strip().lower() + " "

    def take(pattern: str) -> re.Match | None:
        nonlocal s
        m = re.search(pattern, s)
        if m:
            s = s[: m.start()] + " " + s[m.end():]
        return m

    # Explicit extensions: "ext:py", ".gd files", "type:pdf"
    while (m := take(r"\s(?:ext|type):\.?([a-z0-9]+)\s")) or (m := take(r"\s\.([a-z0-9]{1,6})(?:\s+files?)?\s")):
        p.exts.add(m.group(1))
        p.chips.append({"type": "kind", "label": "." + m.group(1)})

    # Relative dates
    rel = [
        (r"\btoday\b", lambda: (today, None), "Today"),
        (r"\byesterday\b", lambda: (today - timedelta(days=1), today), "Yesterday"),
        (r"\b(?:this|the past|past) week\b", lambda: (today - timedelta(days=7), None), "Past week"),
        (r"\blast week\b", lambda: (today - timedelta(days=today.weekday() + 7), today - timedelta(days=today.weekday())), "Last week"),
        (r"\bthis month\b", lambda: (today.replace(day=1), None), "This month"),
        (r"\blast month\b", lambda: (
            datetime(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1), today.replace(day=1)
        ), "Last month"),
        (r"\b(?:the past|past) month\b", lambda: (today - timedelta(days=30), None), "Past month"),
        (r"\bthis year\b", lambda: (datetime(today.year, 1, 1), None), "This year"),
        (r"\blast year\b", lambda: (datetime(today.year - 1, 1, 1), datetime(today.year, 1, 1)), f"{today.year - 1}"),
        (r"\brecent(?:ly)?\b", lambda: (today - timedelta(days=14), None), "Recent"),
    ]
    for pat, fn, label in rel:
        if take(r"(?:\b(?:from|in|during|since|over)\s+)?" + pat):
            a, b = fn()
            p.after, p.before = _ts(a), (_ts(b) if b else None)
            p.chips.append({"type": "date", "label": label})
            break
    if p.after is None and (m := take(r"(?:\b(?:in|from|over)\s+)?\b(?:the\s+)?(?:last|past)\s+(\d+)\s+(day|week|month|year)s?\b")):
        n, unit = int(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        p.after = _ts(today - timedelta(days=days))
        p.chips.append({"type": "date", "label": f"Last {n} {unit}{'s' if n != 1 else ''}"})

    # Month (+ optional year): "october", "in oct 2024", "march 2025"
    if p.after is None:
        names = "|".join(sorted(list(MONTHS) + list(MONTH_ABBR), key=len, reverse=True))
        for m in re.finditer(rf"(?:\b({'|'.join(DATE_PREPS)})\s+)?\b({names})\b\.?(?:\s+(\d{{4}}))?", s):
            prep, word, year = m.group(1), m.group(2), m.group(3)
            full = word in MONTHS and word != "may"
            if not (full or prep or year):
                continue  # "may", "mar", "jan" alone are too ambiguous
            month = MONTHS.get(word) or MONTH_ABBR[word]
            s = s[: m.start()] + " " + s[m.end():]
            name = calendar.month_name[month]
            if year:
                p.after, p.before = _month_range(int(year), month)
                p.chips.append({"type": "date", "label": f"{name} {year}"})
            else:
                p.month = month
                p.chips.append({"type": "date", "label": name})
            p.month_words = [name.lower(), calendar.month_abbr[month].lower()]
            break

    # Bare year: "in 2024", "from 2023"
    if p.after is None and p.month is None and (m := take(r"\b(?:in|from|during|of)\s+((?:19|20)\d{2})\b")):
        y = int(m.group(1))
        p.after, p.before = _ts(datetime(y, 1, 1)), _ts(datetime(y + 1, 1, 1))
        p.chips.append({"type": "date", "label": str(y)})

    # Language + file noun: "python script", "gdscript files"
    for lang, exts in LANG_EXTS.items():
        if (m := take(rf"\b{re.escape(lang)}\s+({'|'.join(FILE_NOUNS)})\b")):
            p.exts |= exts
            p.chips.append({"type": "kind", "label": lang.capitalize()})

    words = re.findall(r"[\w#'+-]+", s)
    keep = []
    for w in words:
        if w in KIND_WORDS:
            kinds, exts, label = KIND_WORDS[w]
            if w.startswith("screenshot"):
                p.screenshot = True
            p.kinds |= kinds
            p.exts |= exts
            if not any(c["label"] == label for c in p.chips):
                p.chips.append({"type": "kind", "label": label})
            continue
        keep.append(w)
    # A kind filter plus extension filter would AND to nothing ("python code" -> py only).
    if p.exts and p.kinds:
        p.kinds = set()

    content = [w for w in keep if w not in STOPWORDS]
    p.terms = [w.strip("'-") for w in content if w.strip("'-")]
    p.text = " ".join(p.terms)
    return p
