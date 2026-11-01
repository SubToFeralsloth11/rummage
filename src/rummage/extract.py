"""Turn a file into (kind, text). Kind drives icons and filters."""

from __future__ import annotations

import html
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config

TEXT_EXTS = {"txt", "md", "markdown", "rst", "org", "log", "csv", "tsv", "tex", "rtf", "srt", "vtt"}
CODE_EXTS = {
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "gd", "gdshader", "swift", "c", "h", "cpp", "hpp",
    "cc", "cs", "java", "kt", "kts", "go", "rs", "rb", "php", "lua", "sh", "zsh", "bash", "fish", "sql",
    "html", "htm", "css", "scss", "sass", "less", "json", "yaml", "yml", "toml", "ini", "cfg", "xml",
    "vue", "svelte", "dart", "r", "m", "mm", "pl", "scala", "zig", "glsl", "hlsl", "shader", "bat",
    "ps1", "mcfunction", "mcmeta", "properties", "gradle", "cmake", "ipynb",
}
CODE_NAMES = {"makefile", "dockerfile", "gemfile", "rakefile", "procfile", "justfile"}
DOC_EXTS = {"docx", "pptx", "xlsx", "odt", "odp", "ods"}
PDF_EXTS = {"pdf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "heic", "heif", "webp", "tiff", "tif", "gif", "bmp"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "mkv", "avi", "webm"}
AUDIO_EXTS = {"mp3", "m4a", "wav", "flac", "aac", "ogg", "opus"}
MODEL_EXTS = {"glb", "gltf", "obj", "fbx", "stl", "ply", "dae", "blend", "bbmodel", "usdz"}
# Indexed by name only: no text to pull out, but worth finding.
NAME_ONLY_EXTS = {
    "zip", "rar", "7z", "dmg", "pkg", "psd", "kra", "aseprite", "key", "pages", "numbers", "epub",
    "apk", "jar", "exe", "iso", "mcpack", "mcworld", "svg",
}


@dataclass
class Extracted:
    kind: str
    text: str
    ocr: bool = False


def kind_for(path: Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    if ext in TEXT_EXTS:
        return "text"
    if ext in CODE_EXTS or path.name.lower() in CODE_NAMES:
        return "code"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOC_EXTS:
        return "doc"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in MODEL_EXTS:
        return "model"
    if ext in NAME_ONLY_EXTS:
        return "media"
    return None


def in_asset_dir(path: Path) -> bool:
    return any(part.lower() in config.ASSET_DIRS for part in path.parts[:-1])


def _read_text(path: Path, size: int) -> str:
    if size > config.MAX_TEXT_BYTES:
        return ""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:8192]:
        return ""  # binary despite the extension
    return raw.decode("utf-8", errors="replace")


_RTF_GROUP = re.compile(r"\{\\\*[^{}]*\}")
_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?|\\'[0-9a-f]{2}|[{}]")


def _rtf_to_text(s: str) -> str:
    s = _RTF_GROUP.sub("", s)
    s = s.replace("\\par", "\n").replace("\\line", "\n")
    return _RTF_CTRL.sub("", s)


_TAG = re.compile(r"<[^>]+>")


def _xml_text(xml: str, para_tags: tuple[str, ...]) -> str:
    for t in para_tags:
        xml = xml.replace(t, "\n")
    return html.unescape(_TAG.sub("", xml))


def _office_text(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    parts: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if ext == "docx":
            for n in names:
                if n.startswith("word/") and n.endswith(".xml") and (
                    "document" in n or "header" in n or "footer" in n or "footnotes" in n
                ):
                    parts.append(_xml_text(z.read(n).decode("utf-8", "replace"), ("</w:p>", "<w:tab/>", "<w:br/>")))
        elif ext == "pptx":
            slides = sorted(
                (n for n in names if re.match(r"ppt/(slides|notesSlides)/\w+\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
            )
            for n in slides:
                parts.append(_xml_text(z.read(n).decode("utf-8", "replace"), ("</a:p>",)))
        elif ext == "xlsx":
            for n in names:
                if n == "xl/sharedStrings.xml" or n == "xl/workbook.xml":
                    xml = z.read(n).decode("utf-8", "replace")
                    if n == "xl/workbook.xml":
                        parts.append(" ".join(re.findall(r'<sheet [^>]*name="([^"]+)"', xml)))
                    else:
                        parts.append(_xml_text(xml, ("</si>",)))
        elif ext in ("odt", "odp", "ods") and "content.xml" in names:
            parts.append(_xml_text(z.read("content.xml").decode("utf-8", "replace"), ("</text:p>", "</text:h>")))
    return "\n".join(parts)


def _pdf_text(path: Path, size: int, allow_ocr: bool) -> tuple[str, bool]:
    if size > config.MAX_PDF_BYTES:
        return "", False
    from pypdf import PdfReader

    text = ""
    pages = 1
    try:
        reader = PdfReader(str(path))
        pages = max(len(reader.pages), 1)
        out = []
        for page in reader.pages[:200]:
            out.append(page.extract_text() or "")
        text = "\n\n".join(out)
    except Exception:
        text = ""
    # Little or no text layer: probably a scan. OCR the first pages.
    if allow_ocr and len(text.strip()) < 40 * min(pages, 10):
        from .ocr import ocr_pdf

        ocr_text = ocr_pdf(str(path))
        if len(ocr_text.strip()) > len(text.strip()):
            return ocr_text, True
    return text, False


def extract(path: Path, size: int, allow_ocr: bool) -> Extracted | None:
    kind = kind_for(path)
    if kind is None:
        return None
    if kind == "text":
        text = _read_text(path, size)
        if path.suffix.lower() == ".rtf":
            text = _rtf_to_text(text)
        return Extracted(kind, text)
    if kind == "code":
        text = _read_text(path, size)
        if path.suffix.lower() == ".ipynb":
            text = "\n".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text))
        return Extracted(kind, text)
    if kind == "doc":
        return Extracted(kind, _office_text(path))
    if kind == "pdf":
        text, used = _pdf_text(path, size, allow_ocr)
        return Extracted(kind, text, used)
    if kind == "image":
        if (
            allow_ocr
            and config.MIN_OCR_IMAGE_BYTES <= size <= config.MAX_IMAGE_BYTES
            and not in_asset_dir(path)
        ):
            from .ocr import ocr_image

            return Extracted(kind, ocr_image(str(path)), True)
        return Extracted(kind, "")
    if kind == "model" and path.suffix.lower() in (".bbmodel", ".gltf") and size <= config.MAX_TEXT_BYTES * 10:
        return Extracted(kind, _model_names(path))
    return Extracted(kind, "")


def _model_names(path: Path) -> str:
    """Part, bone, texture and animation names from JSON model formats (Blockbench, glTF)."""
    from .lzutf8 import read_bbmodel_text

    data = json.loads(read_bbmodel_text(path.read_text(encoding="utf-8", errors="replace")))
    names: list[str] = []
    for key in ("elements", "groups", "textures", "animations", "nodes", "meshes", "materials", "images"):
        for item in data.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])

    def groups(items):
        for it in items or []:
            if isinstance(it, dict):
                if isinstance(it.get("name"), str):
                    names.append(it["name"])
                groups(it.get("children"))

    groups(data.get("outliner"))
    return "\n".join(dict.fromkeys(names))


def folder_listing(path: Path, limit: int = 300) -> str:
    """What's inside a folder (subfolders first), so it can be found by its contents."""
    try:
        entries = [e for e in os.scandir(path) if not e.name.startswith(".")]
    except OSError:
        return ""
    dirs = sorted(e.name + "/" for e in entries if e.is_dir(follow_symlinks=False))
    files = sorted(e.name for e in entries if not e.is_dir(follow_symlinks=False))
    names = (dirs + files)[:limit]
    more = len(dirs) + len(files) - len(names)
    return "\n".join(names) + (f"\n… and {more} more" if more > 0 else "")


def chunk_text(text: str) -> list[str]:
    """Split on paragraph/line boundaries into ~CHUNK_CHARS windows with overlap."""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        return []
    size, overlap = config.CHUNK_CHARS, config.CHUNK_OVERLAP
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n and len(chunks) < config.MAX_CHUNKS_PER_FILE:
        end = min(start + size, n)
        if end < n:
            # Prefer to break at a paragraph, then a line, then a sentence/space.
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > size // 2:
                    end = start + cut + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]
