from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from orchestrator_core import (
    annotate_duplicate_usage,
    build_plan,
    capability_report,
    duplicate_identity,
    google_drive_download_url,
    migrate_collection,
    provenance_for,
    safe_download,
    source_details,
    validate_download,
)


APP = "BG3 Mod Bridge"
SUPPORTED = {".zip", ".rar", ".7z", ".pak"}
ARCHIVES = {".zip", ".rar", ".7z"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BG3ModBridge/1.0"
DEFAULT_GUIDE = "https://gall.dcinside.com/mgallery/board/view/?id=bg3&no=916407&search_head=120&page=1"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def safe_name(text: str, fallback: str = "collection") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean(text))
    return (text[:70].strip(" ._") or fallback)


def guide_metadata(page: str, url: str, full_title: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    host = parsed.netloc.lower()
    author = "작성자 미상"
    match = re.search(r'"author"\s*:\s*\{.*?"name"\s*:\s*"([^"]+)"', page, re.S)
    if not match:
        match = re.search(r'class="gall_writer[^>]*data-nick="([^"]+)"[^>]*data-loc="view"', page, re.I)
    if match:
        author = clean(match.group(1))
    if "dcinside.com" in host:
        site = "발게3 디시" if (query.get("id") or [""])[0] == "bg3" else "디시인사이드"
        content = re.sub(r"\s*-\s*발더스 게이트 3 마이너 갤러리\s*$", "", full_title).strip()
    else:
        site = host.removeprefix("www.")
        content = full_title
    return {"content": content or full_title, "site": site, "author": author}


def collection_dir_name(meta: dict) -> str:
    return safe_name(f"{meta['content']},{meta['site']}-{meta['author']}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def default_paths() -> dict:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    vortex_candidates = [
        Path(r"C:\Program Files\Vortex\Vortex.exe"),
        local / "Programs/Vortex/Vortex.exe",
        Path(r"C:\Program Files\Black Tree Gaming Ltd\Vortex\Vortex.exe"),
    ]
    vortex = next((p for p in vortex_candidates if p.exists()), vortex_candidates[0])
    downloads = home / "Downloads"
    return {
        "library": str(home / "Documents/BG3 Mod Bridge"),
        "bg3_mods": str(local / "Larian Studios/Baldur's Gate 3/Mods"),
        "vortex_exe": str(vortex),
        "vortex_staging": str(roaming / "Vortex/baldursgate3/mods"),
        "browser_downloads": str(downloads),
    }


def find_chromium() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    candidates = [
        program_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
        program_files_x86 / "BraveSoftware/Brave-Browser/Application/brave.exe",
        local / "BraveSoftware/Brave-Browser/Application/brave.exe",
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        local / "Google/Chrome/Application/chrome.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((path for path in candidates if path.exists()), None)


def open_web(url: str) -> None:
    browser = find_chromium()
    if browser:
        subprocess.Popen([str(browser), url])
    else:
        webbrowser.open(url)


class GuideParser(HTMLParser):
    """Extract title, article text and links without site-specific dependencies."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self._title_depth = 0
        self._article_depth = 0
        self._anchor = None
        self._anchor_text: list[str] = []
        self._anchor_heading = ""
        self._segments: list[str] = []
        self.links: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "title":
            self._title_depth += 1
        if tag == "div" and ({"write_div", "thum-txtin"} & set(classes)):
            self._article_depth = 1
        elif tag == "div" and self._article_depth:
            self._article_depth += 1
        if tag == "a" and self._article_depth:
            href = urllib.parse.urljoin(self.base_url, attrs.get("href", ""))
            if href.startswith(("http://", "https://")):
                self._anchor = href
                self._anchor_text = []
                self._anchor_heading = nearest_author_heading(self._segments)

    def handle_endtag(self, tag):
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag == "a" and self._anchor:
            label = clean(" ".join(self._anchor_text))
            context = clean(" ".join(self._segments[-8:]))[-500:]
            self.links.append({"url": self._anchor, "name": label, "heading": self._anchor_heading, "description": context})
            self._anchor = None
            self._anchor_text = []
            self._anchor_heading = ""
        if tag == "div" and self._article_depth:
            self._article_depth -= 1

    def handle_data(self, data):
        text = clean(data)
        if not text:
            return
        if self._title_depth:
            self.title += text
        if self._article_depth:
            if self._anchor:
                self._anchor_text.append(text)
            if text not in {"Just a moment...", "www.nexusmods.com", "www.patreon.com", "drive.google.com"}:
                self._segments.append(text)

    @property
    def article(self) -> str:
        return "\n".join(self._segments)


def item_kind(url: str) -> str:
    source_type = source_details(url)["source_type"]
    if source_type == "nexus":
        return "Nexus"
    if source_type == "patreon":
        return "Patreon"
    if source_type == "google_drive":
        return "Google Drive"
    if source_type == "dcinside":
        return "안내 글"
    host = urllib.parse.urlparse(url).netloc.lower()
    # Enhanced detection for mod-specific URLs
    if any(domain in host for domain in ["moddb.com", "modworks.com", "thunderstore.io"]):
        return "ModDB/ModWorks"
    if "github.com" in host and ("/releases/" in url or "/archive/" in url):
        return "GitHub Release"
    # Check for mod-specific file extensions
    if any(ext in url.lower() for ext in [".zip", ".rar", ".7z", ".pak", ".mod"]):
        return "Mod File Direct Link"
    # If it's a direct download page or a known mod site, classify as mod content
    mod_sites = ["moddb.com", "modworks.com", "thunderstore.io", 
                 "nexusmods.com", "patreon.com", "github.com"]
    if any(domain in host for domain in mod_sites):
        return "Mod Site Content"
    return "직접 링크"


def useful_label(label: str, url: str) -> str:
    label = clean(label)
    files = re.findall(r"[^\s<>:\"/|?*]+\.(?:zip|rar|7z|pak)\b", label, re.I)
    if files:
        return files[0]
    if label and not label.startswith(("http://", "https://")) and "Just a moment" not in label:
        return label[:120]
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).rstrip("/")
    # Enhanced detection of actual mod files from URL paths
    if any(ext in path.lower() for ext in [".zip", ".rar", ".7z", ".pak"]):
        filename = Path(path).name
        return safe_name(filename, "mod_file")
    return Path(path).name or urllib.parse.urlparse(url).netloc


def author_heading(text: str) -> str:
    text = clean(text)
    text = re.sub(r"^(?:[·•*-]|\d+[.)])\s*", "", text).strip()
    generic = {
        "선행 모드", "선행모드", "추천 모드", "추천모드", "구드 다운", "다운로드",
        "보이스 추천", "스샷", "주의사항", "필수 모드", "필수모드", "optional files", "main files",
    }
    if (not text or len(text) > 140 or text.casefold() in {value.casefold() for value in generic}
            or text.startswith(("http://", "https://")) or re.fullmatch(r"[\w.-]+\.(?:com|net|org)", text, re.I)):
        return ""
    return text


def nearest_author_heading(segments: list[str]) -> str:
    """Use the closest authored line, without reaching past a section heading."""
    for segment in reversed(segments):
        text = clean(segment)
        if (text.startswith(("http://", "https://")) or "Just a moment" in text
                or re.fullmatch(r"[\w.-]+\.(?:com|net|org)", text, re.I)):
            continue
        return author_heading(text)
    return ""


def label_score(label: str) -> int:
    label = clean(label)
    if not label or label.startswith(("http://", "https://")) or "Just a moment" in label:
        return 0
    if re.search(r"\.(?:zip|rar|7z|pak)\b", label, re.I):
        return 1000 + len(label)
    return 100 + len(label)


def parse_guide(url: str, raw: bytes, encoding: str = "utf-8") -> tuple[str, str, list[dict], dict]:
    try:
        page = raw.decode(encoding, errors="replace")
    except LookupError:
        page = raw.decode("utf-8", errors="replace")
    parser = GuideParser(url)
    parser.feed(page)
    full_title = clean(parser.title) or urllib.parse.urlparse(url).netloc
    meta = guide_metadata(page, url, full_title)
    merged: dict[str, dict] = {}
    order: list[str] = []
    for link in parser.links:
        target = link["url"].split("#", 1)[0]
        host = urllib.parse.urlparse(target).netloc.lower()
        if any(x in host for x in ("dcimg", "nstatic", "ad.dcinside")):
            continue
        # Enhanced filtering for mod-specific content detection
        if not is_mod_content_link(target):
            continue
        if target not in merged:
            order.append(target)
            merged[target] = link
        else:
            old = merged[target]
            if label_score(link["name"]) > label_score(old["name"]):
                old["name"] = link["name"]
            if len(link["description"]) > len(old["description"]):
                old["description"] = link["description"]
            # The first occurrence is the author's list entry. Later duplicates are
            # usually preview-card/image links and must not rename that entry.
            if not author_heading(old.get("heading", "")) and author_heading(link.get("heading", "")):
                old["heading"] = link.get("heading", "")
    items = []
    for target in order:
        link = merged[target]
        fallback = useful_label(link["name"], target)
        heading = author_heading(link.get("heading", ""))
        name = heading if item_kind(target) in {"Nexus", "Patreon"} and heading else fallback
        items.append({
            "id": uuid.uuid4().hex,
            "url": target,
            "name": name,
            "original_name": name,
            "custom_name": False,
            "description": link["description"],
            "kind": item_kind(target),
            "file": "",
            "group": "",
            "desired": False,
            "pending_apply": False,
            "managed_hash": "",
            "vortex_id": "",
            "source_type": source_details(target)["source_type"],
            "requirement": "unknown",
            "alternative_group": "",
            "confidence": 0.0,
            "classification_reason": "작성자의 명시적 의미 분류가 필요합니다.",
            "download_status": "planned",
            "download": {},
            "provenance": provenance_for(
                target, url, link.get("name", ""), link.get("description", ""), ""
            ),
        })
    auto_groups(items)
    return meta["content"], parser.article, items, meta


def parse_guide_page(url: str, raw: bytes, encoding: str = "utf-8"):
    """Compatibility alias for older integrations."""
    return parse_guide(url, raw, encoding)


async def nexus_download_mod_file(mod_id: str, file_id: str, download_dir: str,
                                  expected_filename: str = "", expected_size: str = "") -> dict:
    """Compatibility API that safely hands authenticated Nexus work to the user.

    Nexus downloads require an authenticated, authorized user session.  This API
    intentionally neither extracts cookies nor automates CAPTCHA/wait bypasses.
    """
    return {
        "success": False,
        "error": "Nexus 다운로드는 로그인된 브라우저에서 사용자가 직접 승인해야 합니다.",
        "file_path": None,
        "file_name": expected_filename or None,
        "file_size": 0,
        "status": "browser_required",
        "mod_id": str(mod_id),
        "file_id": str(file_id) if file_id else None,
        "expected_size": expected_size,
    }


def is_mod_file_content(content_type: str, content_disposition: str, url: str) -> bool:
    """Validate that the downloaded content is actually a mod file"""
    # Check content type first
    if content_type:
        content_type = content_type.lower()
        if any(ct in content_type for ct in ["application/zip", "application/x-rar", 
                                            "application/x-7z-compressed", "application/x-pak"]):
            return True
        # Allow common mod formats through
        if any(ct in content_type for ct in ["application/octet-stream", "binary/octet-stream"]):
            # This is a fallback check based on URL extension if necessary
            pass
    
    # Check filename from content-disposition header
    if content_disposition:
        file_match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition, re.I)
        if file_match:
            filename = file_match.group(1).strip('\'"')
            if any(ext in filename.lower() for ext in [".zip", ".rar", ".7z", ".pak", ".mod"]):
                return True
    
    # If we can't determine from headers, try checking URL extensions
    if any(ext in url.lower() for ext in [".zip", ".rar", ".7z", ".pak", ".mod"]):
        return True
        
    # Additional check for common mod site patterns that might have been missed
    # Nexus and Patreon often use specific download paths or URLs with file names
    if "nexusmods.com" in url.lower() or "patreon.com" in url.lower():
        # Check URL structure to identify real mod files vs. previews/pages
        path = urllib.parse.urlparse(url).path.lower()
        # Exclude typical preview pages or URLs that don't lead to downloads
        if any(keyword in path for keyword in ["preview", "image", "screenshot", "page", "gallery"]):
            return False
        # If file extension is present and is a mod type, it's likely a real download
        if any(ext in url.lower() for ext in [".zip", ".rar", ".7z", ".pak"]):
            return True
            
    return False


def validate_mod_download(url: str, response_headers: dict) -> bool:
    """Enhanced validation of downloaded mod content"""
    # Get content type and content disposition headers
    content_type = response_headers.get('Content-Type', '')
    content_disposition = response_headers.get('Content-Disposition', '')
    
    # If it's an HTML page (common for login pages or error pages)
    if 'text/html' in response_headers.get('Content-Type', '') and \
       not is_mod_file_content(content_type, content_disposition, url):
        return False
    
    # Validate the actual file type
    if not is_mod_file_content(content_type, content_disposition, url):
        # For URLs without explicit file extensions, do more thorough detection
        path = urllib.parse.urlparse(url).path.lower()
        if any(ext in path for ext in [".zip", ".rar", ".7z", ".pak"]):
            return True
        return False
    
    return True


def is_mod_content_link(url: str) -> bool:
    """Enhanced detection of mod-related content vs general links"""
    parsed = urllib.parse.urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path.lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if Path(path).suffix in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".pdf", ".doc", ".docx"}:
        return False
    if any(domain in host for domain in (
        "youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com", "instagram.com",
        "doubleclick.net", "googlesyndication.com", "dcimg", "nstatic",
    )):
        return False
    if source_details(url)["source_type"] in {"nexus", "patreon", "google_drive", "dcinside"}:
        return True
    return Path(path).suffix in SUPPORTED or any(
        domain in host for domain in ("moddb.com", "thunderstore.io", "github.com")
    )

    # Legacy implementation retained below for compatibility reference.
    # Always exclude image and document previews
    if any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".pdf", ".doc", ".docx"]):
        return False
    
    # Check URL path components for mod-related keywords
    path = urllib.parse.urlparse(url).path.lower()
    if any(keyword in path for keyword in ["image", "preview", "screenshot", "gallery", "thumb", "media", "page"]):
        return False
        
    # Exclude common non-mod content sites
    exclude_hosts = ["youtube.com", "youtu.be", "twitter.com", "facebook.com", "instagram.com"]
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(exclude_host in host for exclude_host in exclude_hosts):
        return False
    
    # Exclude common download managers or ad sites
    if any(domain in host for domain in ["ad.doubleclick.net", "googlesyndication.com"]):
        return False
    
    # If it's a direct mod file link, accept it
    if any(ext in url.lower() for ext in [".zip", ".rar", ".7z", ".pak", ".mod"]):
        return True
    
    # For mod sites, be more lenient but still filter by URL structure  
    mod_hosts = ["nexusmods.com", "patreon.com", "drive.google.com", 
                 "moddb.com", "modworks.com", "thunderstore.io", "dcinside.com"]
    if any(mod_host in host for mod_host in mod_hosts):
        # Additional validation: check that the URL looks like a download link
        # rather than an article page or overview
        path_parts = [part for part in path.split('/') if part]
        if len(path_parts) >= 2:
            # If it has typical download path patterns, accept it
            if any(keyword in url.lower() for keyword in ["download", "file", "mod", "archive"]):
                return True
            # Check for specific mod site structure that indicates content vs article
            if host == "nexusmods.com":
                if any(part in path_parts for part in ["files", "downloads", "download"]):
                    return True
                # If it looks like a regular mod page, be more restrictive
                if len(path_parts) >= 3 and path_parts[1] == "mods":
                    return False # This is likely an article page, not direct download
            # Patreon specific - allow only valid download paths
            elif host == "patreon.com":
                if any(part in path_parts for part in ["posts", "creators"]):
                    return False  # Likely a content view instead of direct download
                # Allow direct file downloads and /download paths
                return True
        return True
        
    # If it's not explicitly excluded and contains mod-related content, include it
    if "mod" in url.lower() or "download" in url.lower():
        # More specific check to ensure we're getting actual mod files
        if any(keyword in url.lower() for keyword in ["dl.", "file", ".zip", ".rar", "download"]):
            return True
    
    # Default to accepting the link for further processing
    return True


def auto_groups(items: list[dict]) -> None:
    candidates: dict[str, list[dict]] = {}
    for item in items:
        stem = Path(item["name"]).stem
        ext = Path(item["name"]).suffix.lower()
        if ext not in SUPPORTED:
            continue
        key = re.sub(r"\d+$", "", stem).casefold()
        if key:
            candidates.setdefault(key, []).append(item)
    for key, group_items in candidates.items():
        if len(group_items) > 1:
            group = "선택: " + re.sub(r"[_-]+", " ", key).strip()
            for item in group_items:
                item["group"] = group


def fetch_url(url: str) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    request_url = url
    if "dcinside.com" in parsed.netloc.lower():
        query = urllib.parse.parse_qs(parsed.query)
        gallery = (query.get("id") or [""])[0]
        number = (query.get("no") or [""])[0]
        mobile_match = re.fullmatch(r"/board/([^/]+)/(\d+)", parsed.path)
        if mobile_match:
            gallery, number = mobile_match.groups()
        if gallery and number:
            request_url = f"https://m.dcinside.com/board/{urllib.parse.quote(gallery)}/{number}"
    request = urllib.request.Request(request_url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        "Referer": "https://m.dcinside.com/",
    })
    with urllib.request.urlopen(request, timeout=35) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        content = response.read()
        if not content:
            raise RuntimeError("DCInside가 빈 응답을 반환했습니다. 잠시 후 다시 시도하세요.")
        return content, charset


def collect_guide_tree(root_url: str, max_depth: int = 3, max_pages: int = 40):
    """Collect a DCInside guide tree with bounded depth and cycle protection."""
    queue_items = [(root_url, 0)]
    visited: set[str] = set()
    merged: dict[str, dict] = {}
    root_result = None
    sources, errors = [], []
    while queue_items and len(visited) < max_pages:
        current, depth = queue_items.pop(0)
        identity = source_details(current)["normalized_url"]
        if identity in visited:
            continue
        visited.add(identity)
        try:
            raw, encoding = fetch_url(current)
            result = parse_guide(current, raw, encoding)
        except Exception as exc:
            errors.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if root_result is None:
            root_result = result
        title, article, items, meta = result
        sources.append({"url": current, "depth": depth, "title": title})
        for item in items:
            details = source_details(item["url"])
            key = details["source_identity"]
            if key not in merged:
                merged[key] = item
            else:
                old = merged[key]
                if len(item.get("description", "")) > len(old.get("description", "")):
                    old["description"] = item["description"]
            if details["source_type"] == "dcinside" and depth < max_depth:
                queue_items.append((item["url"], depth + 1))
    if root_result is None:
        message = errors[0]["error"] if errors else "가이드 문서를 읽지 못했습니다."
        raise RuntimeError(message)
    title, article, _items, meta = root_result
    meta["guide_sources"] = sources
    meta["collection_errors"] = errors
    return title, article, list(merged.values()), meta


def series_number(text: str) -> int | None:
    match = re.search(r"(?:NPC\s*외형\s*변경\s*모드|Npc\s*외형\s*변경\s*모드)\s*(\d{1,2})", clean(text), re.I)
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 99 else None


def merge_existing_items(old_items: list[dict], new_items: list[dict]) -> list[dict]:
    """Refresh guide metadata without losing downloaded/install state."""
    old_by_source = {source_details(item.get("url", ""))["source_identity"]: item for item in old_items}
    preserved = ("id", "file", "desired", "pending_apply", "managed_hash", "vortex_id",
                 "download", "download_status", "group", "alternative_group")
    for item in new_items:
        old = old_by_source.get(source_details(item.get("url", ""))["source_identity"])
        if not old:
            continue
        if old.get("custom_name"):
            item["name"] = old.get("name", item["name"])
            item["custom_name"] = True
        for key in preserved:
            if old.get(key) not in (None, "", False, {}):
                item[key] = old[key]
    return new_items


def collect_guide_series(root_url: str, max_pages: int = 40) -> list[dict]:
    """Turn a numbered DCInside series into one collection per guide post."""
    raw, encoding = fetch_url(root_url)
    root_title, root_article, root_items, root_meta = parse_guide(root_url, raw, encoding)
    candidates: dict[int, str] = {}
    root_number = series_number(root_title)
    if root_number:
        candidates[root_number] = root_url
    for item in root_items:
        if source_details(item["url"])["source_type"] != "dcinside":
            continue
        number = series_number(item.get("name", ""))
        if number:
            candidates.setdefault(number, item["url"])
    if not candidates:
        candidates[1] = root_url
    project_title = re.sub(r"\s*\d{1,2}(?:v\d+)?\s*(?:[.(].*)?$", "", root_title, flags=re.I).strip()
    project_title = (project_title or root_title) + " 프로젝트"
    project_id = "bg3-series:" + hashlib.sha256(
        f"{root_meta.get('author', '')}:{project_title.casefold()}".encode("utf-8")
    ).hexdigest()[:16]
    project_created = datetime.now().isoformat(timespec="seconds")
    collections = []
    for number, url in sorted(candidates.items())[:max_pages]:
        if number == root_number:
            title, article, items, meta = root_title, root_article, root_items, root_meta
        else:
            page, page_encoding = fetch_url(url)
            title, article, items, meta = parse_guide(url, page, page_encoding)
        # Series navigation and prerequisite-guide posts belong in the collection
        # sidebar, never in the download-management table.
        items = [item for item in items if source_details(item["url"])["source_type"] != "dcinside"]
        data = {
            "version": 3,
            "schema_version": 1,
            "series_number": number,
            "project_id": project_id,
            "project_title": project_title,
            "project_created": project_created,
            "game": "Baldur's Gate 3",
            "title": title,
            "content": meta["content"],
            "site": meta["site"],
            "author": meta["author"],
            "url": url,
            "created": datetime.now().isoformat(timespec="seconds"),
            "article": article,
            "items": items,
            "queue": {"jobs": []},
        }
        migrate_collection(data)
        collections.append(data)
    annotate_duplicate_usage(collections)
    return collections


def drive_download_url(url: str) -> str:
    return google_drive_download_url(url)

    # Legacy implementation retained below for compatibility reference.
    match = re.search(r"/file/d/([^/?]+)", url)
    if not match:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        file_id = (query.get("id") or [""])[0]
    else:
        file_id = match.group(1)
    if not file_id:
        return url
    return "https://drive.usercontent.google.com/download?export=download&confirm=t&id=" + urllib.parse.quote(file_id)


def content_filename(headers, fallback: str) -> str:
    value = headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", value, re.I)
    plain = re.search(r'filename="?([^";]+)', value, re.I)
    if encoded:
        return safe_name(urllib.parse.unquote(encoded.group(1)), fallback)
    if plain:
        return safe_name(plain.group(1), fallback)
    # Enhanced filename handling to detect actual mod files
    content_type = headers.get("Content-Type", "").lower()
    if "application/zip" in content_type or "application/x-zip" in content_type:
        fallback = re.sub(r'\.[^.]+$', '.zip', fallback) 
    elif "application/x-rar" in content_type or "application/rar" in content_type:
        fallback = re.sub(r'\.[^.]+$', '.rar', fallback)
    elif "application/x-7z-compressed" in content_type:
        fallback = re.sub(r'\.[^.]+$', '.7z', fallback)
    return safe_name(fallback)


def download_file(url: str, destination: Path, fallback_name: str) -> Path:
    target, _metadata = safe_download(url, destination, fallback_name)
    return target

    # Legacy implementation retained below for compatibility reference.
    if item_kind(url) == "Google Drive":
        url = drive_download_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=90) as response:
        name = content_filename(response.headers, fallback_name)
        target = destination / name
        content_type = response.headers.get_content_type()
        
        # Enhanced validation of downloaded files - prioritize actual mod files
        if content_type == "text/html" and target.suffix.lower() not in SUPPORTED:
            # Check for login or redirect pages specific to Nexus/Patreon
            try:
                html_content = response.read(2048)  # Read first 2KB for checking
                html_text = html_content.decode('utf-8', errors='ignore')
                
                # Specifically check for Nexus and Patreon error patterns
                if "nexusmods.com" in url.lower():
                    if any(keyword in html_text.lower() for keyword in ["please log in", "login required", "account verification"]):
                        raise ValueError("Nexus 로그인 또는 인증이 필요한 페이지입니다.")
                    elif "file not found" in html_text.lower() or "not found" in html_text.lower():
                        raise ValueError("Nexus 파일이 존재하지 않습니다.")
                elif "patreon.com" in url.lower():
                    if any(keyword in html_text.lower() for keyword in ["log in", "login required",
                                                                        "you must be logged in", "content is not available"]):
                        raise ValueError("Patreon 로그인 또는 인증이 필요한 페이지입니다.")
                        
                # If valid HTML but no redirect, continue with download
                # This can happen with some mod sites that use JS redirect.
            except Exception:
                pass 
            # Continue to file download if this looks like a valid HTML page  
            return target
        
        # Validate that we actually received a mod file and not an HTML page or error  
        content_disposition = response.headers.get("Content-Disposition", "")
        if content_type == "text/html" and ("filename=" not in content_disposition):
            # Check if it contains signs of login redirect or error page
            response_data = response.read(1024)
            response_text = response_data.decode('utf-8', errors='ignore')
            
            # Enhanced checks for specific mod site error patterns  
            if "nexusmods.com" in url.lower():
                if any(keyword in response_text.lower() for keyword in ["login", "redirect", "please verify your account", 
                                                                         "account verification", "please log in"]):
                    raise ValueError("Nexus 로그인 또는 인증이 필요한 페이지입니다.")
            elif "patreon.com" in url.lower():
                if any(keyword in response_text.lower() for keyword in ["log in", "login required", 
                                                                         "you must be logged in", "content is not available"]):
                    raise ValueError("Patreon 로그인 또는 인증이 필요한 페이지입니다.")
            
            # If it's HTML but doesn't look like an error, allow download
            # This can happen with some mod sites that redirect through JS.
            return target
        
        # Check if we got a valid file or HTML page and handle appropriately
        try:
            response_data = response.read(1024)
            if response_data:
                # If this is a text/html response with no actual filename specified,
                # check that it's not just redirect page or error page  
                response_text = response_data.decode('utf-8', errors='ignore')
                
                # Additional Nexus/Patreon validation
                if content_type == "text/html":
                    if any(keyword in response_text.lower() for keyword in [
                        'redirect', 'login', 'please verify', 
                        'account verification', 'access denied', 'content not available'
                    ]):
                        host = urllib.parse.urlparse(url).netloc.lower()
                        if "nexusmods.com" in host:
                            raise ValueError("Nexus 인증이 필요한 페이지입니다")
                        elif "patreon.com" in host:
                            raise ValueError("Patreon 인증이 필요한 페이지입니다") 
                        else:
                            raise ValueError("인증이 필요한 페이지입니다")
                            
            # Return back to our proper file handling logic
            response.seek(0)
            
        except Exception:
            pass  

        with target.open("wb") as f:
            shutil.copyfileobj(response, f)
            
    # Additional validation for mod file types    
    if target.exists():
        if target.suffix.lower() in SUPPORTED:
            # Verify it's a valid archive type where possible
            try:
                if target.suffix.lower() == ".zip":
                    with zipfile.ZipFile(target) as zip_file:
                        if not zip_file.namelist(): 
                            raise ValueError("ZIP 파일이 비어 있습니다.")
                        # Check for mod-specific content in the zip file (e.g., .pak files)
                        pak_files = [name for name in zip_file.namelist() if name.lower().endswith('.pak')]
                        if not pak_files and len(zip_file.namelist()) > 10:
                            # If it's a large zip but no .pak files, it might be invalid
                            pass
                elif target.suffix.lower() == ".rar":
                    # For .rar files, we can't easily validate without additional libraries
                    pass
                elif target.suffix.lower() == ".7z":
                    # For .7z files, we can't easily validate without additional libraries  
                    pass
            except Exception as e:
                # If validation fails, remove the file and raise an error
                target.unlink()
                if "empty" in str(e).lower():
                    raise ValueError("파일이 유효하지 않습니다: 파일이 비어 있습니다")
                else:
                    raise ValueError(f"파일이 유효하지 않습니다: {str(e)}")
    
    return target


def download_with_retries(url: str, destination: Path, fallback_name: str, attempts: int = 3) -> Path:
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            return download_file(url, destination, fallback_name)
        except Exception as exc:
            # Enhanced error logging with more specific messages
            error_msg = f"{attempt}차: {exc}"
            errors.append(error_msg)
            
            # Check if this is a known authentication issue that can't be auto-resolved
            if any(keyword in str(exc).lower() for keyword in ["login", "auth", "verify", "account", "please log in"]):
                # For authentication issues, don't retry as it won't succeed
                raise RuntimeError("인증이 필요한 페이지입니다. 수동 로그인 후 다시 시도해주세요.\n" + "\n".join(errors))
            elif attempt < attempts:
                time.sleep(attempt * 2)  # Exponential backoff
    
    raise RuntimeError("3회 다운로드 실패\n" + "\n".join(errors))


def locate_browser_download(reported: str, download_folder: str, queued: float = 0,
                            claimed: set[str] | None = None, hint: str = "") -> Path | None:
    claimed = claimed or set()
    candidates = []
    if reported:
        candidates.append(Path(reported))
    folder = Path(download_folder)
    if reported:
        candidates.append(folder / Path(reported).name)
    if folder.exists():
        candidates.extend(folder.glob("*"))
    valid = []
    for path in candidates:
        try:
            resolved = str(path.resolve())
            if (resolved not in claimed and path.is_file() and path.suffix.lower() in SUPPORTED
                    and path.stat().st_mtime >= queued - 5):
                valid.append(path)
        except OSError:
            pass
    if reported:
        exact = [path for path in valid if path.name.casefold() == Path(reported).name.casefold()]
        if exact:
            return max(exact, key=lambda path: path.stat().st_mtime)
    words = [word for word in re.findall(r"[a-z0-9가-힣]{3,}", hint.casefold())]
    return max(valid, key=lambda path: (
        sum(word in path.name.casefold() for word in words), path.stat().st_mtime
    )) if valid else None


def archive_paks(path: Path) -> list[str]:
    if path.suffix.lower() != ".zip":
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            return sorted({Path(n).name for n in archive.namelist() if n.lower().endswith(".pak")})
    except (OSError, zipfile.BadZipFile):
        return []


def norm_stem(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", Path(value).stem.casefold())


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings: dict, on_save):
        super().__init__(parent)
        self.title("경로 설정")
        self.resizable(True, False)
        self.transient(parent)
        self.grab_set()
        self.entries = {}
        labels = [
            ("library", "컬렉션 보관 폴더"),
            ("bg3_mods", "BG3 Mods 폴더"),
            ("vortex_exe", "Vortex.exe"),
            ("vortex_staging", "Vortex staging 폴더"),
            ("browser_downloads", "브라우저 다운로드 폴더"),
        ]
        for row, (key, label) in enumerate(labels):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=7)
            var = tk.StringVar(value=settings.get(key, ""))
            self.entries[key] = var
            ttk.Entry(self, textvariable=var, width=72).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Button(self, text="찾기", command=lambda k=key: self.pick(k)).grid(row=row, column=2, padx=10)
        ttk.Label(self, text="경로 변경 후 목록을 새로 읽습니다.", foreground="#666").grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=8)
        ttk.Button(self, text="저장", command=lambda: self.save(on_save)).grid(row=5, column=2, padx=10, pady=8)
        self.columnconfigure(1, weight=1)

    def pick(self, key):
        if key == "vortex_exe":
            value = filedialog.askopenfilename(parent=self, filetypes=[("Vortex", "Vortex.exe"), ("실행 파일", "*.exe")])
        else:
            value = filedialog.askdirectory(parent=self)
        if value:
            self.entries[key].set(value)

    def save(self, callback):
        callback({key: var.get().strip() for key, var in self.entries.items()})
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("1280x760")
        self.minsize(980, 620)
        self.option_add("*Font", "{맑은 고딕} 10")
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "BG3ModBridge"
        self.config_path = local / "config.json"
        self.settings = default_paths()
        if self.config_path.exists():
            try:
                self.settings.update(json.loads(self.config_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        self.collection_paths: list[Path] = []
        self.collection: dict | None = None
        self.collection_path: Path | None = None
        self.busy = False
        self.sort_column = ""
        self.sort_reverse = False
        self.collection_sort_mode = "번호"
        self.collection_sort_reverse = False
        self.shared_sort_mode = "이름"
        self.shared_sort_reverse = False
        self.project_sort_mode = "추가 날짜시간"
        self.project_sort_reverse = False
        self.active_project_id = ""
        self.download_watch: dict | None = None
        self.make_ui()
        self.load_collections()
        self.after(2500, self.periodic_refresh)

    def make_ui(self):
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=27)
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="모드 적용법 링크").pack(side="left")
        self.url_var = tk.StringVar(value=DEFAULT_GUIDE)
        ttk.Entry(top, textvariable=self.url_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="수집", command=self.collect).pack(side="left", padx=3)
        ttk.Button(top, text="설정", command=lambda: SettingsDialog(self, self.settings, self.save_settings)).pack(side="left", padx=3)

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(paned, width=250)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=4)

        left_tabs = ttk.Notebook(left)
        left_tabs.pack(fill="both", expand=True)
        project_tab = ttk.Frame(left_tabs, padding=6)
        toc_tab = ttk.Frame(left_tabs, padding=6)
        left_tabs.add(project_tab, text="컬렉션 프로젝트")
        left_tabs.add(toc_tab, text="링크별 컬렉션 목차")
        self.project_tree = ttk.Treeview(
            project_tab, columns=("game", "created", "collections", "mods", "shared"),
            show="tree headings", selectmode="browse", height=12,
        )
        self.project_tree.heading("#0", text="프로젝트")
        for key, label, width in (
            ("game", "게임", 120), ("created", "추가 날짜시간", 140),
            ("collections", "목차", 55), ("mods", "총 모드", 65), ("shared", "공유모드", 70),
        ):
            self.project_tree.heading(key, text=label)
            self.project_tree.column(key, width=width, minwidth=45)
        self.project_tree.column("#0", width=190, minwidth=120)
        self.project_tree.pack(fill="both", expand=True)
        self.project_tree.bind("<<TreeviewSelect>>", self.select_project)
        project_controls = ttk.Frame(project_tab)
        project_controls.pack(fill="x", pady=(6, 0))
        self.project_sort_var = tk.StringVar(value=self.project_sort_mode)
        project_sort = ttk.Combobox(
            project_controls, textvariable=self.project_sort_var, state="readonly", width=11,
            values=("추가 날짜시간", "이름", "게임", "목차 수", "총 모드 수", "공유모드 수"),
        )
        project_sort.pack(side="left", padx=(0, 3))
        project_sort.bind("<<ComboboxSelected>>", self.change_project_sort)
        self.project_direction_button = ttk.Button(
            project_controls, text="오름차순 ↑", command=self.toggle_project_sort_direction
        )
        self.project_direction_button.pack(side="left")
        project_actions = ttk.Frame(project_tab)
        project_actions.pack(fill="x", pady=(4, 0))
        ttk.Button(project_actions, text="프로젝트 폴더 열기", command=self.open_project_folder).pack(
            side="left", fill="x", expand=True, padx=(0, 2)
        )
        ttk.Button(project_actions, text="프로젝트 삭제", command=self.delete_project).pack(
            side="left", fill="x", expand=True, padx=(2, 0)
        )

        collection_sort = ttk.Frame(toc_tab)
        collection_sort.pack(fill="x", pady=(0, 5))
        ttk.Label(collection_sort, text="정렬").pack(side="left")
        self.collection_sort_var = tk.StringVar(value=self.collection_sort_mode)
        sort_combo = ttk.Combobox(
            collection_sort, textvariable=self.collection_sort_var, state="readonly", width=9,
            values=("번호", "이름", "모드 수", "중복 수"),
        )
        sort_combo.pack(side="left", padx=(5, 3))
        sort_combo.bind("<<ComboboxSelected>>", self.change_collection_sort)
        self.collection_direction_button = ttk.Button(
            collection_sort, text="오름차순 ↑", command=self.toggle_collection_sort_direction
        )
        self.collection_direction_button.pack(side="left", fill="x", expand=True)
        ttk.Label(toc_tab, text="Shift/Ctrl로 여러 컬렉션 선택", foreground="#666").pack(anchor="w", pady=(0, 4))
        self.collections = tk.Listbox(toc_tab, exportselection=False, selectmode=tk.EXTENDED)
        self.collections.pack(fill="both", expand=True)
        self.collections.bind("<<ListboxSelect>>", self.select_collection)
        lf = ttk.Frame(toc_tab)
        lf.pack(fill="x", pady=6)
        ttk.Button(lf, text="폴더 열기", command=self.open_collection_folder).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(lf, text="선택 일괄삭제", command=self.delete_collection).pack(side="left", expand=True, fill="x", padx=(2, 0))

        right_tabs = ttk.Notebook(right)
        right_tabs.pack(fill="both", expand=True)
        shared_tab = ttk.Frame(right_tabs, padding=6)
        situation_tab = ttk.Frame(right_tabs, padding=6)
        right_tabs.add(shared_tab, text="공유모드")
        right_tabs.add(situation_tab, text="컬렉션 모드상황")
        self.right_tabs = right_tabs
        self.situation_tab = situation_tab
        shared_tools = ttk.Frame(shared_tab)
        shared_tools.pack(fill="x", pady=(0, 6))
        ttk.Label(shared_tools, text="검색").pack(side="left")
        self.shared_search_var = tk.StringVar()
        ttk.Entry(shared_tools, textvariable=self.shared_search_var, width=28).pack(side="left", padx=(5, 12))
        ttk.Label(shared_tools, text="정렬").pack(side="left")
        self.shared_sort_var = tk.StringVar(value=self.shared_sort_mode)
        shared_sort = ttk.Combobox(
            shared_tools, textvariable=self.shared_sort_var, state="readonly", width=10,
            values=("이름", "출처", "중복 수", "연결 파일", "다운로드 상태", "컬렉션"),
        )
        shared_sort.pack(side="left", padx=(5, 3))
        shared_sort.bind("<<ComboboxSelected>>", self.change_shared_sort)
        self.shared_direction_button = ttk.Button(
            shared_tools, text="오름차순 ↑", command=self.toggle_shared_sort_direction
        )
        self.shared_direction_button.pack(side="left")
        self.shared_search_var.trace_add("write", lambda *_: self.refresh_shared_tree(getattr(self, "project_records", {})))
        self.shared_tree = ttk.Treeview(
            shared_tab, columns=("name", "kind", "collections", "copies", "files", "status", "group"),
            show="headings", selectmode="extended",
        )
        shared_headers = {
            "name": "공유 모드/링크", "kind": "출처", "collections": "사용 컬렉션",
            "copies": "중복 수", "files": "연결 파일", "status": "다운로드 상태", "group": "선택 그룹",
        }
        shared_widths = {"name": 250, "kind": 80, "collections": 150, "copies": 65, "files": 75, "status": 150, "group": 90}
        for key in self.shared_tree["columns"]:
            self.shared_tree.heading(key, text=shared_headers[key])
            self.shared_tree.column(key, width=shared_widths[key], minwidth=55)
        self.shared_tree.pack(fill="both", expand=True)
        self.shared_tree.bind("<Double-1>", lambda _event: self.show_shared_in_collection())
        shared_actions = ttk.Frame(shared_tab)
        shared_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(shared_actions, text="공유모드 다운로드", command=self.download_shared_selected).pack(side="left", padx=(0, 5))
        ttk.Button(shared_actions, text="공유모드 링크 열기", command=self.open_shared_links).pack(side="left", padx=(0, 5))
        ttk.Button(shared_actions, text="컬렉션에서 보기", command=self.show_shared_in_collection).pack(side="left", padx=(0, 5))
        ttk.Button(shared_actions, text="연결 상태 새로고침", command=lambda: self.load_collections(select=self.collection_path)).pack(side="left")
        ttk.Label(
            shared_tab, text="같은 프로젝트의 여러 목차에서 사용되는 항목입니다. 선택 그룹은 '공유모드'로 자동 관리됩니다.",
            foreground="#666",
        ).pack(anchor="w", pady=(6, 0))

        columns = ("check", "name", "kind", "duplicates", "file", "group", "status")
        filters = ttk.Frame(situation_tab, padding=(0, 0, 0, 6))
        filters.pack(fill="x")
        ttk.Label(filters, text="컬렉션 모드상황", font=("맑은 고딕", 11, "bold")).pack(side="left", padx=(0, 10))
        ttk.Separator(filters, orient="vertical").pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(filters, text="검색").pack(side="left")
        self.filter_text = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.filter_text, width=28)
        search.pack(side="left", padx=(5, 12))
        ttk.Label(filters, text="출처").pack(side="left")
        self.filter_kind = tk.StringVar(value="전체")
        ttk.Combobox(filters, textvariable=self.filter_kind, state="readonly", width=12,
                     values=("전체", "Nexus", "Patreon", "Google Drive", "안내 글", "직접 링크")).pack(side="left", padx=(5, 12))
        ttk.Label(filters, text="적용").pack(side="left")
        self.filter_active = tk.StringVar(value="전체")
        ttk.Combobox(filters, textvariable=self.filter_active, state="readonly", width=9,
                     values=("전체", "적용", "미적용")).pack(side="left", padx=(5, 12))
        ttk.Label(filters, text="다운로드").pack(side="left")
        self.filter_file = tk.StringVar(value="전체")
        ttk.Combobox(filters, textvariable=self.filter_file, state="readonly", width=9,
                     values=("전체", "있음", "없음")).pack(side="left", padx=5)
        self.filter_text.trace_add("write", lambda *_: self.refresh_tree())
        for variable in (self.filter_kind, self.filter_active, self.filter_file):
            variable.trace_add("write", lambda *_: self.refresh_tree())

        self.tree = ttk.Treeview(situation_tab, columns=columns, show="headings", selectmode="extended")
        self.tree_headers = {"check": "적용", "name": "모드/링크", "kind": "출처", "duplicates": "중복 컬렉션", "file": "다운로드", "group": "선택 그룹", "status": "상태"}
        widths = {"check": 55, "name": 220, "kind": 85, "duplicates": 110, "file": 155, "group": 130, "status": 120}
        for col in columns:
            self.tree.heading(col, text=self.tree_headers[col], command=lambda column=col: self.sort_tree(column))
            self.tree.column(col, width=widths[col], minwidth=45, anchor="center" if col in {"check", "kind"} else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<Button-3>", self.show_item_context_menu)
        self.tree.bind("<<TreeviewSelect>>", self.show_details)

        buttons = ttk.Frame(situation_tab, padding=(0, 7))
        buttons.pack(fill="x")
        for text, command in [
            ("☑ 적용/해제", self.toggle_selected),
            ("다운로드 대기", self.download_selected),
            ("공개 링크 전체 받기", self.download_all),
            ("수동 파일 연결", self.import_files),
            ("선택그룹 지정", self.set_group),
            ("링크 열기", self.open_links),
            ("Vortex 열기", self.open_vortex),
            ("Vortex 연동 설치", self.install_vortex_bridge),
            ("↻ 동기화", self.refresh_tree),
        ]:
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="다운로드 계획", command=self.export_plan).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="환경 진단", command=self.run_doctor).pack(side="left", padx=(0, 5))

        self.details = tk.Text(situation_tab, height=7, wrap="word", state="disabled", background="#f5f5f5")
        self.details.tag_configure("hyperlink", foreground="#0066cc", underline=True)
        self.details.pack(fill="x")
        self.status_var = tk.StringVar(value="준비")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5).pack(fill="x", side="bottom")

    @property
    def library(self) -> Path:
        return Path(self.settings["library"])

    def save_settings(self, values: dict):
        self.settings.update(values)
        atomic_json(self.config_path, self.settings)
        self.load_collections()
        self.status("설정을 저장했습니다.")

    def status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def run_worker(self, label, work, done, failed=None):
        if self.busy:
            return
        self.busy = True
        self.status(label)

        def runner():
            try:
                result = work()
                self.after(0, lambda: finish(result, None))
            except Exception as exc:
                self.after(0, lambda: finish(None, exc))

        def finish(result, error):
            self.busy = False
            if error:
                if failed:
                    failed(error)
                self.status("실패: " + str(error))
                messagebox.showerror(APP, str(error), parent=self)
            else:
                done(result)

        threading.Thread(target=runner, daemon=True).start()

    def watch_selected_download(self, item: dict, apply=False, open_page=True):
        if not self.collection_path:
            return
        self.download_watch = {"item_id": item["id"], "started": time.time(), "apply": apply}
        if apply:
            item["pending_apply"] = True
            self.save_collection()
            self.refresh_tree()
        self.status(f"'{item['name']}' 다운로드 대기 중 · Brave에서 파일을 한 번 내려받으세요.")
        if open_page:
            open_web(item["url"])

    def check_download_watch(self):
        watch = self.download_watch
        if not watch or not self.collection_path:
            return
        item = self.item_by_id(watch["item_id"])
        if not item:
            self.download_watch = None
            return
        source = locate_browser_download(
            "", self.settings.get("browser_downloads", ""), watch["started"],
            hint=f"{item['name']} {item.get('description', '')}",
        )
        if not source:
            return
        label = item["name"]
        destination = self.collection_path / "downloads"
        destination.mkdir(exist_ok=True)
        target = destination / source.name
        if target.exists() and sha256(target) != sha256(source):
            target = destination / f"{source.stem} ({int(time.time())}){source.suffix}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        item["file"] = str(target.relative_to(self.collection_path))
        item["pending_apply"] = False
        auto_groups(self.collection["items"])
        apply = watch["apply"]
        self.download_watch = None
        self.save_collection()
        self.refresh_tree()
        self.status(f"{target.name}을(를) '{label}'에 연결했습니다.")
        if apply:
            self.toggle_item(item)

    def launch_download_browser(self, start_url: str):
        now = time.time()
        if now - self.browser_launching_at < 5:
            return True
        browser = find_chromium()
        extension = Path(__file__).resolve().parent / "chrome-extension"
        if not browser or not extension.exists():
            messagebox.showerror(APP, "Chrome/Brave/Edge 또는 chrome-extension 폴더를 찾지 못했습니다.", parent=self)
            return False
        if not self.browser_server:
            messagebox.showerror(APP, f"자동 다운로드 통신 포트({BROWSER_PORT})를 열 수 없습니다. 프로그램을 한 번만 실행했는지 확인하세요.", parent=self)
            return False
        if browser.name.casefold() in {"brave.exe", "chrome.exe"}:
            label = "Brave" if browser.name.casefold() == "brave.exe" else "Chrome"
            if process_running(browser.name):
                if self.browser_version == BROWSER_EXTENSION_VERSION and (
                    now - self.browser_seen_at < 45 or self.active_browser_task is not None
                ):
                    self.browser_launching_at = now
                    subprocess.Popen([str(browser), "about:blank"])
                    return True
                if not messagebox.askyesno(
                    APP,
                    f"자동 다운로드 연결을 위해 {label}를 지금 재시작할까요?\n"
                    "열려 있던 탭은 브라우저의 세션 복구 기능으로 유지됩니다.",
                    parent=self,
                ):
                    return False
                subprocess.run(
                    ["taskkill", "/F", "/IM", browser.name], capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                for _ in range(30):
                    if not process_running(browser.name):
                        break
                    time.sleep(0.1)
                if process_running(browser.name):
                    messagebox.showerror(APP, f"{label}를 종료하지 못했습니다. 직접 종료한 뒤 다시 시도하세요.", parent=self)
                    return False
            vendor = "BraveSoftware/Brave-Browser" if label == "Brave" else "Google/Chrome"
            user_data = Path(os.environ.get("LOCALAPPDATA", "")) / vendor / "User Data"
            profile = browser_profile_name(user_data)
            command = [
                str(browser), f"--user-data-dir={user_data}", f"--profile-directory={profile}",
                f"--load-extension={extension}", "--no-first-run", "--new-window", "about:blank",
            ]
        else:
            user_data = self.config_path.parent / "download-browser-profile"
            user_data.mkdir(parents=True, exist_ok=True)
            command = [
                str(browser), f"--user-data-dir={user_data}", f"--load-extension={extension}",
                "--no-first-run", "--new-window", "about:blank",
            ]
        self.browser_connected = False
        self.browser_seen_at = 0.0
        self.browser_version = ""
        self.browser_launching_at = now
        subprocess.Popen(command)
        return True

    def queue_browser_downloads(self, items: list[dict], apply=False):
        # Legacy callers now use the same user-click download watch; never automate site clicks.
        if items:
            self.watch_selected_download(items[0], apply=apply)
        return
        if not items or not self.collection_path:
            return
        batch_id = uuid.uuid4().hex
        self.download_batches[batch_id] = {
            "remaining": len(items), "complete": 0, "failures": [], "claimed": set(),
            "queued_at": time.time(), "extension_seen": False,
            "item_ids": {item["id"] for item in items},
        }
        for item in items:
            name = item.get("name", "")
            description = item.get("description", "")
            self.browser_tasks.put({
                "id": uuid.uuid4().hex,
                "batch_id": batch_id,
                "item_id": item["id"],
                "url": item["url"],
                "kind": item["kind"],
                "name": name,
                "hint": clean(f"{name} {description}")[:1400],
                "collection": str(self.collection_path),
                "queued": time.time(),
                "apply": apply,
            })
        if not self.launch_download_browser(items[0]["url"]):
            kept = []
            while True:
                try:
                    task = self.browser_tasks.get_nowait()
                    if task["batch_id"] != batch_id:
                        kept.append(task)
                except queue.Empty:
                    break
            for task in kept:
                self.browser_tasks.put(task)
            del self.download_batches[batch_id]
            if apply:
                for item in items:
                    item["pending_apply"] = False
                self.save_collection()
                self.refresh_tree()
            return
        self.status(f"Nexus/Patreon {len(items)}개 자동 다운로드 대기 중… (각 3회 재시도)")
        self.after(15000, lambda bid=batch_id: self.check_browser_connection(bid))

    def note_browser(self, version=""):
        if version:
            self.browser_version = version
        self.browser_connected = version == BROWSER_EXTENSION_VERSION
        self.browser_seen_at = time.time()

    def claim_browser_task(self, version=""):
        self.note_browser(version)
        if version != BROWSER_EXTENSION_VERSION:
            return {"upgrade": True, "required_version": BROWSER_EXTENSION_VERSION}
        with self.browser_lock:
            if self.active_browser_task is None:
                try:
                    self.active_browser_task = self.browser_tasks.get_nowait()
                except queue.Empty:
                    return {}
            task = self.active_browser_task
            batch = self.download_batches.get(task["batch_id"])
            if batch:
                batch["extension_seen"] = True
            return {key: task[key] for key in ("id", "url", "kind", "name", "hint")}

    def check_browser_connection(self, batch_id: str, connected_check=0):
        batch = self.download_batches.get(batch_id)
        if not batch or batch.get("extension_seen"):
            return
        if self.browser_version == BROWSER_EXTENSION_VERSION and (self.active_browser_task or connected_check == 0):
            self.after(25000, lambda bid=batch_id: self.check_browser_connection(bid, connected_check + 1))
            return
        kept = []
        while True:
            try:
                task = self.browser_tasks.get_nowait()
                if task["batch_id"] != batch_id:
                    kept.append(task)
            except queue.Empty:
                break
        for task in kept:
            self.browser_tasks.put(task)
        if self.active_browser_task and self.active_browser_task.get("batch_id") == batch_id:
            return
        collection_path = self.collection_path
        if collection_path and self.collection:
            for item in self.collection["items"]:
                if item["id"] in batch["item_ids"]:
                    item["pending_apply"] = False
            self.save_collection()
            self.refresh_tree()
        self.download_batches.pop(batch_id, None)
        self.status("Brave 자동 다운로드 확장 연결 실패")
        messagebox.showwarning(
            APP,
            "Brave 자동 다운로드 확장이 연결되지 않았습니다.\n"
            "프로그램을 종료한 뒤 새 'BG3 Mod Bridge 실행.cmd'로 다시 실행해 주세요.\n"
            "다시 실행하면 Brave가 확장과 함께 열리고 다운로드가 자동으로 이어집니다.",
            parent=self,
        )

    def finish_browser_task(self, payload: dict):
        with self.browser_lock:
            task = self.active_browser_task
            if not task or payload.get("id") != task["id"]:
                return
            self.active_browser_task = None
        self.after(0, lambda: self.handle_browser_result(task, payload))

    def cancel_pending_apply(self, item_id: str):
        with self.browser_lock:
            if self.active_browser_task and self.active_browser_task["item_id"] == item_id:
                self.active_browser_task["apply"] = False
            kept = []
            while True:
                try:
                    task = self.browser_tasks.get_nowait()
                    if task["item_id"] == item_id:
                        task["apply"] = False
                    kept.append(task)
                except queue.Empty:
                    break
            for task in kept:
                self.browser_tasks.put(task)

    def handle_browser_result(self, task: dict, payload: dict):
        batch = self.download_batches.get(task["batch_id"])
        if not batch:
            return
        error = payload.get("error", "다운로드 실패")
        reported = payload.get("path", "")
        source = locate_browser_download(
            reported, self.settings.get("browser_downloads", ""),
            task.get("queued", 0), batch["claimed"],
        ) if reported else None
        collection_path = Path(task["collection"])
        if source:
            try:
                destination = collection_path / "downloads"
                destination.mkdir(exist_ok=True)
                target = destination / source.name
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                data_path = collection_path / "collection.json"
                data = json.loads(data_path.read_text(encoding="utf-8"))
                item = next(x for x in data["items"] if x["id"] == task["item_id"])
                item["file"] = str(target.relative_to(collection_path))
                item["pending_apply"] = False
                auto_groups(data["items"])
                atomic_json(data_path, data)
                batch["claimed"].add(str(source.resolve()))
                batch["complete"] += 1
            except Exception as exc:
                error = str(exc)
                batch["failures"].append(f"{task['url']}\n{error}")
        else:
            batch["failures"].append(f"{task['url']}\n{error}")
            if task.get("apply"):
                try:
                    data_path = collection_path / "collection.json"
                    data = json.loads(data_path.read_text(encoding="utf-8"))
                    next(x for x in data["items"] if x["id"] == task["item_id"])["pending_apply"] = False
                    atomic_json(data_path, data)
                except (OSError, json.JSONDecodeError, StopIteration):
                    pass
        batch["remaining"] -= 1
        if self.collection_path and self.collection_path.resolve() == collection_path.resolve():
            current = self.item_by_id(task["item_id"])
            if current:
                current["pending_apply"] = False
                if source:
                    current["file"] = str((collection_path / "downloads" / source.name).relative_to(collection_path))
                    auto_groups(self.collection["items"])
                    if task.get("apply"):
                        self.toggle_item(current)
                self.save_collection()
                self.refresh_tree()
        if batch["remaining"] == 0:
            failures = batch["failures"]
            self.status(f"보호 사이트 다운로드 {batch['complete']}개 완료, {len(failures)}개 실패")
            if failures:
                messagebox.showwarning(APP, "3회 재시도 후에도 받지 못한 항목입니다.\n전용 브라우저에서 Nexus 로그인 또는 Patreon 무료 가입 상태를 확인하세요.\n\n" + "\n\n".join(failures[:8]), parent=self)
            else:
                messagebox.showinfo(APP, f"Nexus/Patreon {batch['complete']}개 다운로드를 완료했습니다.", parent=self)
            del self.download_batches[task["batch_id"]]

    def collect(self):
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning(APP, "http 또는 https 링크를 입력하세요.", parent=self)
            return

        def work():
            return collect_guide_series(url)

        def done(series):
            existing = {}
            for path in self.library.rglob("collection.json"):
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    existing[source_details(saved.get("url", ""))["source_identity"]] = (path.parent, saved)
                except (OSError, json.JSONDecodeError):
                    continue
            selected_folder = None
            total_items = 0
            project_folder = self.library / safe_name(series[0].get("project_title", "컬렉션 프로젝트"), "collection-project")
            project_folder.mkdir(parents=True, exist_ok=True)
            for data in series:
                identity = source_details(data["url"])["source_identity"]
                previous = existing.get(identity)
                if previous:
                    folder, old = previous
                    data["items"] = merge_existing_items(old.get("items", []), data["items"])
                    data["created"] = old.get("created", data["created"])
                else:
                    number = int(data.get("series_number", 0))
                    folder = project_folder / f"{number:02d} - {safe_name(data['title'], f'collection-{number}') }"
                    folder.mkdir(parents=True, exist_ok=True)
                (folder / "downloads").mkdir(exist_ok=True)
                atomic_json(folder / "collection.json", data)
                (folder / "설치안내.txt").write_text(
                    f"{data['title']}\n{data['url']}\n\n{data['article']}", encoding="utf-8"
                )
                total_items += len(data["items"])
                if data.get("series_number") == series_number(series[-1].get("title", "")):
                    selected_folder = folder
            atomic_json(project_folder / "project.json", self.project_summary(series, project_folder))
            self.status(f"{len(series)}개 링크별 컬렉션과 {total_items}개 모드 항목을 갱신했습니다.")
            self.load_collections(select=selected_folder)

        self.run_worker("1~30 링크별 컬렉션을 수집하는 중…", work, done)

    def change_collection_sort(self, _event=None):
        self.collection_sort_mode = self.collection_sort_var.get()
        self.load_collections(select=self.collection_path)

    def toggle_collection_sort_direction(self):
        self.collection_sort_reverse = not self.collection_sort_reverse
        self.collection_direction_button.configure(
            text="내림차순 ↓" if self.collection_sort_reverse else "오름차순 ↑"
        )
        self.load_collections(select=self.collection_path)

    def project_summary(self, collections: list[dict], folder: Path | None = None) -> dict:
        first = collections[0] if collections else {}
        identities = {
            duplicate_identity(item.get("url", ""))
            for data in collections for item in data.get("items", [])
        }
        shared = {
            duplicate_identity(item.get("url", ""))
            for data in collections for item in data.get("items", []) if item.get("duplicate_count", 0) > 1
        }
        return {
            "project_id": first.get("project_id", ""),
            "project_title": first.get("project_title", "컬렉션 프로젝트"),
            "game": first.get("game", "Baldur's Gate 3"),
            "created": first.get("project_created") or first.get("created", ""),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "folder": str(folder.resolve()) if folder else "",
            "collection_count": len(collections),
            "total_mod_rows": sum(len(data.get("items", [])) for data in collections),
            "unique_mod_count": len(identities),
            "shared_mod_count": len(shared),
        }

    def synchronize_shared_files(self, project_records: list[tuple[Path, dict]]) -> None:
        """Link one verified shared archive to every matching collection entry."""
        groups: dict[str, list[tuple[Path, dict, dict]]] = {}
        for collection_path, data in project_records:
            for item in data.get("items", []):
                if item.get("duplicate_count", 0) > 1:
                    groups.setdefault(duplicate_identity(item.get("url", "")), []).append(
                        (collection_path, data, item)
                    )
        if not project_records:
            return
        project_folder = project_records[0][0].parent
        shared_downloads = project_folder / "_공유모드" / "downloads"
        for identity, entries in groups.items():
            candidates: dict[str, Path] = {}
            for collection_path, _data, item in entries:
                if not item.get("file"):
                    continue
                candidate = (collection_path / item["file"]).resolve()
                try:
                    if candidate.is_file() and candidate.suffix.lower() in SUPPORTED:
                        candidates.setdefault(sha256(candidate), candidate)
                except OSError:
                    continue
            if not candidates:
                for _collection_path, _data, item in entries:
                    item["shared_status"] = "미다운로드"
                    item["shared_file"] = ""
                continue
            if len(candidates) > 1:
                for _collection_path, _data, item in entries:
                    item["shared_status"] = f"파일 충돌 {len(candidates)}개"
                    item["shared_file"] = ""
                continue
            file_hash, source = next(iter(candidates.items()))
            shared_downloads.mkdir(parents=True, exist_ok=True)
            target = shared_downloads / source.name
            if target.exists() and sha256(target) != file_hash:
                target = shared_downloads / f"{source.stem}-{file_hash[:8]}{source.suffix}"
            if source != target.resolve() and not target.exists():
                shutil.copy2(source, target)
            for collection_path, _data, item in entries:
                item["file"] = os.path.relpath(target, collection_path)
                item["shared_file"] = str(target)
                item["shared_status"] = f"연결됨 · {len(entries)}개 컬렉션"
                item["download_status"] = "verified"
    def organize_project_folders(self, records: list[tuple[Path, dict | None]]) -> list[tuple[Path, dict | None]]:
        """Move legacy collection folders under their physical project folder."""
        library = self.library.resolve()
        project_folders: dict[str, Path] = {}
        used_names: dict[str, str] = {}
        for _path, data in records:
            if data is None:
                continue
            project_id = data["project_id"]
            if project_id in project_folders:
                continue
            base = safe_name(data.get("project_title", "컬렉션 프로젝트"), "collection-project")
            name = base
            if name.casefold() in used_names and used_names[name.casefold()] != project_id:
                name = f"{base} - {hashlib.sha256(project_id.encode()).hexdigest()[:6]}"
            used_names[name.casefold()] = project_id
            project_folders[project_id] = library / name
        moved: list[tuple[Path, dict | None]] = []
        previous_project_folders: set[Path] = set()
        for path, data in records:
            if data is None:
                moved.append((path, data))
                continue
            project_folder = project_folders[data["project_id"]]
            project_folder.mkdir(parents=True, exist_ok=True)
            source = path.resolve()
            if (source.parent / "project.json").exists():
                previous_project_folders.add(source.parent)
            if source.parent == project_folder.resolve():
                target = source
            else:
                target = project_folder / source.name
                counter = 2
                while target.exists():
                    try:
                        existing = json.loads((target / "collection.json").read_text(encoding="utf-8"))
                        if source_details(existing.get("url", ""))["source_identity"] == source_details(data.get("url", ""))["source_identity"]:
                            break
                    except (OSError, json.JSONDecodeError):
                        pass
                    target = project_folder / f"{source.name} ({counter})"
                    counter += 1
                if not target.exists():
                    source.rename(target)
            moved.append((target, data))
        for old_folder in previous_project_folders:
            if old_folder in project_folders.values() or not old_folder.exists():
                continue
            marker = old_folder / "project.json"
            if marker.exists() and not list(old_folder.glob("*/collection.json")):
                marker.unlink()
                if not any(old_folder.iterdir()):
                    old_folder.rmdir()
        grouped: dict[str, list[dict]] = {}
        for _path, data in moved:
            if data is not None:
                grouped.setdefault(data["project_id"], []).append(data)
        for project_id, collections in grouped.items():
            atomic_json(project_folders[project_id] / "project.json", self.project_summary(collections, project_folders[project_id]))
        return moved

    def load_collections(self, select: Path | None = None):
        self.library.mkdir(parents=True, exist_ok=True)
        records = []
        for data_path in self.library.rglob("collection.json"):
            path = data_path.parent
            try:
                records.append((path, json.loads((path / "collection.json").read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                records.append((path, None))
        projects: dict[str, list[tuple[Path, dict]]] = {}
        for path, data in records:
            if data is None:
                continue
            migrate_collection(data)
            projects.setdefault(data["project_id"], []).append((path, data))
        records = self.organize_project_folders(records)
        projects = {}
        for path, data in records:
            if data is not None:
                projects.setdefault(data["project_id"], []).append((path, data))
        for project_records in projects.values():
            annotate_duplicate_usage([data for _path, data in project_records])
            self.synchronize_shared_files(project_records)
            project_folder = project_records[0][0].parent
            atomic_json(
                project_folder / "project.json",
                self.project_summary([data for _path, data in project_records], project_folder),
            )
        self.project_records = projects
        for path, data in records:
            if data is not None:
                atomic_json(path / "collection.json", data)
        if self.active_project_id not in projects:
            self.active_project_id = next(iter(projects), "")
        self.refresh_project_tree(projects)
        self.refresh_shared_tree(projects)

        if self.active_project_id in projects:
            records = list(projects[self.active_project_id])
        else:
            records = []

        def sort_key(record):
            path, data = record
            if data is None:
                corrupt_values = {
                    "번호": (1, 999999, path.name.casefold()),
                    "이름": (1, path.name.casefold()),
                    "모드 수": (1, -1, path.name.casefold()),
                    "중복 수": (1, -1, path.name.casefold()),
                }
                return corrupt_values.get(self.collection_sort_mode, corrupt_values["번호"])
            number = data.get("series_number")
            title = str(data.get("title", path.name)).casefold()
            mod_count = len(data.get("items", []))
            duplicate_count = sum(1 for item in data.get("items", []) if item.get("duplicate_count", 0) > 1)
            values = {
                "번호": (0, int(number)) if number is not None else (1, 999999),
                "이름": (0, title),
                "모드 수": (0, mod_count, title),
                "중복 수": (0, duplicate_count, title),
            }
            return values.get(self.collection_sort_mode, values["번호"])

        records.sort(key=sort_key, reverse=self.collection_sort_reverse)
        self.collection_paths = [path for path, _data in records]
        self.collections.delete(0, "end")
        selected = None
        for idx, (path, data) in enumerate(records):
            if data is not None:
                prefix = f"{int(data['series_number']):02d}. " if data.get("series_number") else ""
                mod_count = len(data.get("items", []))
                duplicate_count = sum(1 for item in data.get("items", []) if item.get("duplicate_count", 0) > 1)
                self.collections.insert(
                    "end", f"{prefix}{data.get('title', path.name)}  [모드 {mod_count} · 중복 {duplicate_count}]"
                )
            else:
                self.collections.insert("end", path.name + " (손상됨)")
            if select and path.resolve() == select.resolve():
                selected = idx
        if self.collection_paths:
            idx = selected if selected is not None else 0
            self.collections.selection_set(idx)
            self.collections.activate(idx)
            self.collections.see(idx)
            self.collections.event_generate("<<ListboxSelect>>")
        else:
            self.collection = None
            self.collection_path = None
            self.tree.delete(*self.tree.get_children())

    def refresh_project_tree(self, projects: dict[str, list[tuple[Path, dict]]]):
        self.project_tree.delete(*self.project_tree.get_children())
        project_rows = []
        self.project_folders = {}
        for project_id, records in projects.items():
            first = records[0][1]
            total_mods = sum(len(data.get("items", [])) for _path, data in records)
            identities = {
                duplicate_identity(item.get("url", ""))
                for _path, data in records for item in data.get("items", [])
            }
            shared_identities = {
                duplicate_identity(item.get("url", ""))
                for _path, data in records for item in data.get("items", [])
                if item.get("duplicate_count", 0) > 1
            }
            created = str(first.get("project_created") or first.get("created", "")).replace("T", " ")[:19]
            project_folder = records[0][0].parent
            self.project_folders[project_id] = project_folder
            project_rows.append((project_id, records, first, total_mods, len(shared_identities), len(identities), created))

        def project_key(row):
            _project_id, records, first, total_mods, shared_count, _unique_count, created = row
            keys = {
                "추가 날짜시간": (created,),
                "이름": (str(first.get("project_title", "")).casefold(),),
                "게임": (str(first.get("game", "")).casefold(), str(first.get("project_title", "")).casefold()),
                "목차 수": (len(records), str(first.get("project_title", "")).casefold()),
                "총 모드 수": (total_mods, str(first.get("project_title", "")).casefold()),
                "공유모드 수": (shared_count, str(first.get("project_title", "")).casefold()),
            }
            return keys.get(self.project_sort_mode, keys["추가 날짜시간"])

        project_rows.sort(key=project_key, reverse=self.project_sort_reverse)
        for project_id, records, first, total_mods, shared_count, unique_count, created in project_rows:
            self.project_tree.insert(
                "", "end", iid=project_id, text=first.get("project_title", "컬렉션 프로젝트"),
                values=(first.get("game", "Baldur's Gate 3"), created, len(records), total_mods,
                        f"{shared_count} / 고유 {unique_count}"),
            )
        if self.active_project_id in projects:
            self.project_tree.selection_set(self.active_project_id)
            self.project_tree.focus(self.active_project_id)

    def change_project_sort(self, _event=None):
        self.project_sort_mode = self.project_sort_var.get()
        self.refresh_project_tree(getattr(self, "project_records", {}))

    def toggle_project_sort_direction(self):
        self.project_sort_reverse = not self.project_sort_reverse
        self.project_direction_button.configure(
            text="내림차순 ↓" if self.project_sort_reverse else "오름차순 ↑"
        )
        self.refresh_project_tree(getattr(self, "project_records", {}))

    def select_project(self, _event=None):
        selected = self.project_tree.selection()
        if not selected or selected[0] == self.active_project_id:
            return
        self.active_project_id = selected[0]
        self.collection = None
        self.collection_path = None
        self.load_collections()

    def open_project_folder(self):
        selected = self.project_tree.selection()
        project_id = selected[0] if selected else self.active_project_id
        folder = getattr(self, "project_folders", {}).get(project_id)
        if folder and folder.exists():
            os.startfile(folder)

    def delete_project(self):
        selected = self.project_tree.selection()
        project_id = selected[0] if selected else self.active_project_id
        records = getattr(self, "project_records", {}).get(project_id, [])
        if not records:
            return
        first = records[0][1]
        folder = records[0][0].parent.resolve()
        library = self.library.resolve()
        if folder.parent != library or not (folder / "project.json").exists():
            messagebox.showerror(APP, "안전하게 확인할 수 없는 프로젝트 폴더라 삭제를 중단했습니다.", parent=self)
            return
        title = first.get("project_title", folder.name)
        total_mods = sum(len(data.get("items", [])) for _path, data in records)
        if not messagebox.askyesno(
            APP,
            f"프로젝트 '{title}'을(를) 삭제할까요?\n\n링크별 컬렉션 {len(records)}개 · 모드 {total_mods}개\n"
            "프로젝트 폴더와 보관 다운로드가 휴지통 없이 삭제됩니다.\n적용 중인 BG3/Vortex 파일은 삭제하지 않습니다.",
            parent=self,
        ):
            return
        shutil.rmtree(folder)
        self.active_project_id = ""
        self.collection = None
        self.collection_path = None
        self.load_collections()
        self.status(f"컬렉션 프로젝트 '{title}'을(를) 삭제했습니다.")

    def change_shared_sort(self, _event=None):
        self.shared_sort_mode = self.shared_sort_var.get()
        self.refresh_shared_tree(getattr(self, "project_records", {}))

    def toggle_shared_sort_direction(self):
        self.shared_sort_reverse = not self.shared_sort_reverse
        self.shared_direction_button.configure(
            text="내림차순 ↓" if self.shared_sort_reverse else "오름차순 ↑"
        )
        self.refresh_shared_tree(getattr(self, "project_records", {}))

    def refresh_shared_tree(self, projects: dict[str, list[tuple[Path, dict]]]):
        self.shared_tree.delete(*self.shared_tree.get_children())
        self.shared_entry_map = {}
        current_project = self.collection.get("project_id") if self.collection else ""
        records = projects.get(current_project) or next(iter(projects.values()), [])
        aggregated = {}
        for path, data in records:
            for item in data.get("items", []):
                if item.get("duplicate_count", 0) < 2:
                    continue
                identity = duplicate_identity(item.get("url", ""))
                entry = aggregated.setdefault(identity, {
                    "name": item.get("name", ""), "kind": item.get("kind", ""),
                    "collections": set(), "files": 0, "entries": [], "statuses": set(),
                })
                entry["collections"].add(str(data.get("series_number") or data.get("title", path.name)))
                entry["files"] += bool(item.get("file"))
                entry["entries"].append((path, data, item))
                entry["statuses"].add(item.get("shared_status", "미다운로드"))
        query = self.shared_search_var.get().strip().casefold() if hasattr(self, "shared_search_var") else ""
        rows = []
        for identity, entry in aggregated.items():
            labels = sorted(entry["collections"], key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
            entry["label_text"] = ", ".join(labels)
            entry["status_text"] = "파일 충돌" if any("충돌" in value for value in entry["statuses"]) else (
                f"연결됨 {entry['files']}/{len(entry['entries'])}" if entry["files"] else "미다운로드"
            )
            searchable = f"{entry['name']} {entry['kind']} {entry['label_text']} {entry['status_text']} 공유모드".casefold()
            if not query or query in searchable:
                rows.append((identity, entry))

        def shared_key(row):
            entry = row[1]
            keys = {
                "이름": (entry["name"].casefold(),),
                "출처": (entry["kind"].casefold(), entry["name"].casefold()),
                "중복 수": (len(entry["collections"]), entry["name"].casefold()),
                "연결 파일": (entry["files"], entry["name"].casefold()),
                "다운로드 상태": (entry["status_text"], entry["name"].casefold()),
                "컬렉션": (entry["label_text"], entry["name"].casefold()),
            }
            return keys.get(self.shared_sort_mode, keys["이름"])

        rows.sort(key=shared_key, reverse=self.shared_sort_reverse)
        for index, (identity, entry) in enumerate(rows):
            iid = f"shared-{index}"
            self.shared_entry_map[iid] = entry["entries"]
            status = entry["status_text"]
            self.shared_tree.insert(
                "", "end", iid=iid,
                values=(entry["name"], entry["kind"], entry["label_text"], len(entry["collections"]), entry["files"], status, "공유모드"),
            )

    def selected_shared_entries(self) -> list[list[tuple[Path, dict, dict]]]:
        return [self.shared_entry_map[iid] for iid in self.shared_tree.selection() if iid in self.shared_entry_map]

    def open_shared_links(self):
        urls = []
        for entries in self.selected_shared_entries():
            for _path, _data, item in entries:
                if item.get("url") and item["url"] not in urls:
                    urls.append(item["url"])
                    break
        for url in urls:
            open_web(url)
        if urls:
            self.status(f"공유모드 링크 {len(urls)}개를 열었습니다.")

    def download_shared_selected(self):
        selected = self.selected_shared_entries()
        if not selected:
            messagebox.showinfo(APP, "다운로드할 공유모드를 먼저 선택하세요.", parent=self)
            return
        if len(selected) > 1:
            messagebox.showinfo(APP, "사이트 로그인과 파일 선택 확인을 위해 공유모드를 한 번에 하나씩 다운로드하세요.", parent=self)
            return
        entries = selected[0]
        preferred = next((entry for entry in entries if entry[0] == self.collection_path), entries[0])
        collection_path, data, item = preferred
        self.active_project_id = data["project_id"]
        self.load_collections(select=collection_path)
        current = self.item_by_id(item["id"])
        if not current:
            return
        self.right_tabs.select(self.situation_tab)
        self.tree.selection_set(current["id"])
        self.tree.see(current["id"])
        if self.item_file(current):
            messagebox.showinfo(APP, f"이미 공유 파일이 연결되어 있습니다.\n{self.item_file(current)}", parent=self)
            return
        self.download_selected(item=current)

    def show_shared_in_collection(self):
        selected = self.selected_shared_entries()
        if not selected:
            return
        entries = selected[0]
        preferred = next((entry for entry in entries if entry[0] == self.collection_path), entries[0])
        collection_path, data, item = preferred
        self.active_project_id = data["project_id"]
        self.load_collections(select=collection_path)
        current = self.item_by_id(item["id"])
        if current:
            self.right_tabs.select(self.situation_tab)
            self.tree.selection_set(current["id"])
            self.tree.focus(current["id"])
            self.tree.see(current["id"])
            self.show_details()

    def select_collection(self, _event=None):
        selected = self.collections.curselection()
        if not selected:
            return
        active = self.collections.index(tk.ACTIVE)
        index = active if active in selected else selected[-1]
        self.collection_path = self.collection_paths[index]
        try:
            self.collection = json.loads((self.collection_path / "collection.json").read_text(encoding="utf-8"))
            if migrate_collection(self.collection):
                atomic_json(self.collection_path / "collection.json", self.collection)
            if not self.collection.get("series_number") and all(self.collection.get(key) for key in ("content", "site", "author")):
                wanted = self.collection_path.parent / collection_dir_name(self.collection)
                if self.collection_path.name != wanted.name and not wanted.exists():
                    self.collection_path.rename(wanted)
                    self.collection_path = wanted
                    self.collection_paths[index] = wanted
            self.url_var.set(self.collection.get("url", ""))
            self.refresh_shared_tree(getattr(self, "project_records", {}))
            self.refresh_tree()
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP, f"컬렉션을 읽을 수 없습니다.\n{exc}", parent=self)

    def save_collection(self):
        if self.collection and self.collection_path:
            migrate_collection(self.collection)
            atomic_json(self.collection_path / "collection.json", self.collection)
            project_id = self.collection.get("project_id", "")
            project_folder = self.collection_path.parent
            records = []
            for data_path in project_folder.glob("*/collection.json"):
                try:
                    path = data_path.parent
                    data = self.collection if path.resolve() == self.collection_path.resolve() else json.loads(
                        data_path.read_text(encoding="utf-8")
                    )
                    migrate_collection(data)
                    if data.get("project_id") == project_id:
                        records.append((path, data))
                except (OSError, json.JSONDecodeError):
                    continue
            if records:
                annotate_duplicate_usage([data for _path, data in records])
                self.synchronize_shared_files(records)
                for path, data in records:
                    atomic_json(path / "collection.json", data)
                    if path.resolve() == self.collection_path.resolve():
                        self.collection = data
                self.project_records[project_id] = records
                atomic_json(project_folder / "project.json", self.project_summary([data for _path, data in records], project_folder))
                self.refresh_shared_tree(self.project_records)

    def export_plan(self):
        if not self.collection or not self.collection_path:
            messagebox.showinfo(APP, "먼저 컬렉션을 선택하세요.", parent=self)
            return
        plan = build_plan(self.collection, self.collection_path)
        output = self.collection_path / "download-plan.json"
        atomic_json(output, plan)
        self.save_collection()
        self.refresh_tree()
        counts = {}
        for job in plan["jobs"]:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        summary = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        messagebox.showinfo(APP, f"검토용 다운로드 계획을 저장했습니다.\n{output}\n\n{summary}", parent=self)

    def run_doctor(self):
        output = self.config_path.parent / "capabilities.json"
        atomic_json(output, capability_report())
        messagebox.showinfo(APP, f"환경 진단 결과를 저장했습니다.\n{output}", parent=self)

    def item_file(self, item: dict) -> Path | None:
        if not item.get("file") or not self.collection_path:
            return None
        path = self.collection_path / item["file"]
        return path if path.exists() else None

    def item_by_id(self, item_id: str) -> dict | None:
        if not self.collection:
            return None
        return next((x for x in self.collection["items"] if x["id"] == item_id), None)

    def selected_items(self) -> list[dict]:
        return [item for iid in self.tree.selection() if (item := self.item_by_id(iid))]

    def state_for(self, item: dict) -> tuple[bool, str]:
        file = self.item_file(item)
        if not file:
            return False, "링크만 있음"
        ext = file.suffix.lower()
        mods = Path(self.settings["bg3_mods"])
        if ext == ".pak":
            target = mods / file.name
            if target.exists():
                try:
                    same = sha256(target) == sha256(file)
                except OSError:
                    same = False
                return True, "적용됨" if same else "동명 파일 다름"
            return False, "다운로드됨"
        staging = Path(self.settings["vortex_staging"])
        key = norm_stem(file.name)
        try:
            installed = [p for p in staging.iterdir() if p.is_dir() and (key in norm_stem(p.name) or norm_stem(p.name) in key)] if staging.exists() else []
        except OSError:
            installed = []
        if installed:
            item["vortex_id"] = installed[0].name
        paks = archive_paks(file)
        if paks and any((mods / name).exists() for name in paks):
            return True, "Vortex 적용됨"
        if installed:
            return False, "Vortex 설치됨/해제"
        return False, "다운로드됨"

    def refresh_tree(self):
        if not self.collection:
            return
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        changed = False
        rows = []
        query = self.filter_text.get().strip().casefold()
        kind_filter = self.filter_kind.get()
        active_filter = self.filter_active.get()
        file_filter = self.filter_file.get()
        for item in self.collection["items"]:
            active, state = self.state_for(item)
            if item.get("desired") != active and state in {"적용됨", "Vortex 적용됨", "Vortex 설치됨/해제", "다운로드됨"}:
                item["desired"] = active
                changed = True
            file = self.item_file(item)
            searchable = f"{item['name']} {item['url']} {item.get('description', '')}".casefold()
            if query and query not in searchable:
                continue
            if kind_filter != "전체" and item["kind"] != kind_filter:
                continue
            if active_filter == "적용" and not active or active_filter == "미적용" and active:
                continue
            if file_filter == "있음" and not file or file_filter == "없음" and file:
                continue
            pending = item.get("pending_apply", False)
            if pending:
                state = "다운로드 후 적용 예정"
            duplicate_text = item.get("duplicate_display") or "—"
            values = ("☑" if active or pending else "☐", item["name"], item["kind"], duplicate_text, file.name if file else "—", item.get("group") or "—", state)
            rows.append((item, active, values))
        if self.sort_column:
            index = ("check", "name", "kind", "duplicates", "file", "group", "status").index(self.sort_column)
            rows.sort(key=lambda row: (row[1] if self.sort_column == "check" else str(row[2][index]).casefold()), reverse=self.sort_reverse)
        for item, _active, values in rows:
            self.tree.insert("", "end", iid=item["id"], values=values)
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        if changed:
            self.save_collection()
        self.status(f"{len(rows)}/{len(self.collection['items'])}개 표시 · Vortex/BG3 상태 동기화")

    def sort_tree(self, column: str):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        for name, label in self.tree_headers.items():
            arrow = " ▼" if name == self.sort_column and self.sort_reverse else " ▲" if name == self.sort_column else ""
            self.tree.heading(name, text=label + arrow, command=lambda selected=name: self.sort_tree(selected))
        self.refresh_tree()

    def periodic_refresh(self):
        self.check_download_watch()
        if self.collection and not self.busy:
            self.refresh_tree()
        self.after(1500, self.periodic_refresh)

    def double_click(self, event):
        if self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                self.tree.selection_set(row)
                self.toggle_selected()

    def show_item_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self.tree.focus(row)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="모드/링크 이름 변경", command=self.rename_selected_item)
        menu.add_command(label="최초 추출 이름으로 되돌리기", command=self.reset_selected_item_name)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def rename_selected_item(self):
        items = self.selected_items()
        if not items:
            return
        item = items[0]
        value = simpledialog.askstring(
            "모드/링크 이름 변경", "새 표시 이름:", initialvalue=item.get("name", ""), parent=self
        )
        if value is None:
            return
        value = clean(value)[:160]
        if not value:
            messagebox.showwarning(APP, "이름은 비워둘 수 없습니다.", parent=self)
            return
        identity = duplicate_identity(item.get("url", ""))
        if item.get("duplicate_count", 0) > 1:
            for path, data in self.project_records.get(self.collection.get("project_id", ""), []):
                for shared_item in data.get("items", []):
                    if duplicate_identity(shared_item.get("url", "")) == identity:
                        shared_item["name"] = value
                        shared_item["custom_name"] = True
                atomic_json(path / "collection.json", data)
        item["name"] = value
        item["custom_name"] = True
        self.save_collection()
        selected_path = self.collection_path
        self.load_collections(select=selected_path)
        self.status(f"표시 이름을 '{value}'(으)로 변경했습니다.")

    def reset_selected_item_name(self):
        items = self.selected_items()
        if not items:
            return
        item = items[0]
        original = clean(item.get("original_name", ""))
        if not original:
            messagebox.showwarning(APP, "저장된 최초 추출 이름이 없습니다.", parent=self)
            return
        identity = duplicate_identity(item.get("url", ""))
        if item.get("duplicate_count", 0) > 1:
            for path, data in self.project_records.get(self.collection.get("project_id", ""), []):
                for shared_item in data.get("items", []):
                    if duplicate_identity(shared_item.get("url", "")) == identity:
                        shared_item["name"] = clean(shared_item.get("original_name", "")) or shared_item.get("name", "")
                        shared_item["custom_name"] = False
                atomic_json(path / "collection.json", data)
        item["name"] = original
        item["custom_name"] = False
        self.save_collection()
        selected_path = self.collection_path
        self.load_collections(select=selected_path)
        self.status(f"최초 추출 이름 '{original}'(으)로 되돌렸습니다.")

    def toggle_selected(self):
        items = self.selected_items()
        if not items:
            return
        for item in items:
            self.toggle_item(item)
        self.save_collection()
        self.refresh_tree()

    def toggle_item(self, item: dict):
        file = self.item_file(item)
        if not file:
            if item.get("pending_apply"):
                item["pending_apply"] = False
                self.cancel_pending_apply(item["id"])
                self.status(item["name"] + " 자동 적용 취소")
                return
            if item["kind"] == "안내 글":
                open_web(item["url"])
                return
            item["pending_apply"] = True
            self.save_collection()
            self.refresh_tree()
            if item["kind"] in {"Nexus", "Patreon"}:
                self.watch_selected_download(item, apply=True)
            else:
                self.download_selected(apply=True, item=item)
            return
        active, state = self.state_for(item)
        if not active and item.get("group") and item.get("group") != "공유모드":
            for sibling in self.collection["items"]:
                if sibling is not item and sibling.get("group") == item["group"]:
                    sibling_active, _ = self.state_for(sibling)
                    if sibling_active:
                        sibling_file = self.item_file(sibling)
                        if sibling_file and sibling_file.suffix.lower() in ARCHIVES:
                            self.open_vortex()
                            messagebox.showinfo(APP, f"'{sibling['name']}'이 Vortex에서 적용 중입니다.\n먼저 Vortex에서 Disable하세요. 해제가 감지된 뒤 새 항목을 적용할 수 있습니다.", parent=self)
                            return
                        self.disable_item(sibling, quiet=True)
        if active:
            self.disable_item(item)
        elif file.suffix.lower() == ".pak":
            target_dir = Path(self.settings["bg3_mods"])
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / file.name
            if target.exists() and sha256(target) != sha256(file):
                messagebox.showerror(APP, f"동명 파일이 이미 있으며 내용이 다릅니다. 덮어쓰지 않았습니다.\n{target}", parent=self)
                return
            shutil.copy2(file, target)
            item["managed_hash"] = sha256(file)
            item["desired"] = True
            self.status(file.name + " 적용 완료")
        else:
            if item.get("vortex_id") and self.vortex_bridge_installed():
                self.send_vortex_command(item["vortex_id"], True)
                item["desired"] = True
                return
            vortex = Path(self.settings["vortex_exe"])
            if not vortex.exists():
                messagebox.showerror(APP, "설정에서 Vortex.exe 경로를 지정하세요.", parent=self)
                return
            subprocess.Popen([str(vortex), "--install-archive", str(file)], cwd=str(vortex.parent))
            item["desired"] = True
            self.status("Vortex에 설치 요청을 보냈습니다. 설치 화면을 완료하세요.")

    def disable_item(self, item: dict, quiet=False):
        file = self.item_file(item)
        if not file:
            return
        if file.suffix.lower() == ".pak":
            target = Path(self.settings["bg3_mods"]) / file.name
            if target.exists():
                expected = item.get("managed_hash") or sha256(file)
                if sha256(target) != expected:
                    if not quiet:
                        messagebox.showerror(APP, "적용된 파일 내용이 외부에서 바뀌어 안전을 위해 삭제하지 않았습니다.", parent=self)
                    return
                if not item.get("managed_hash") and not messagebox.askyesno(APP, "이 파일은 프로그램 밖에서 적용된 것으로 보입니다.\nBG3 Mods 폴더에서 제거할까요?", parent=self):
                    return
                target.unlink()
            item["desired"] = False
            return
        item["desired"] = False
        if item.get("vortex_id") and self.vortex_bridge_installed():
            self.send_vortex_command(item["vortex_id"], False)
        elif not quiet:
            self.open_vortex()
            messagebox.showinfo(APP, "압축 모드는 Vortex가 소유합니다.\n'Vortex 연동 설치'를 한 번 실행하면 이 체크박스에서 직접 해제할 수 있습니다. 지금은 Vortex에서 Disable하세요.", parent=self)

    def download_selected(self, apply=False, item=None):
        if item is None:
            items = self.selected_items()
            if not items:
                return
            item = items[0]
        if item["kind"] in {"Nexus", "Patreon"}:
            self.watch_selected_download(item, apply=apply)
            return
        if item["kind"] == "안내 글":
            open_web(item["url"])
            return
        fallback = item["name"]
        if Path(fallback).suffix.lower() not in SUPPORTED:
            fallback += ".zip"
        destination = self.collection_path / "downloads"

        def work():
            return download_with_retries(item["url"], destination, fallback)

        def done(path: Path):
            if path.suffix.lower() not in SUPPORTED:
                path.unlink(missing_ok=True)
                item["pending_apply"] = False
                open_web(item["url"])
                messagebox.showinfo(APP, "직접 다운로드 파일을 찾지 못해 웹페이지를 열었습니다. 내려받은 뒤 '파일 가져오기'를 누르세요.", parent=self)
                return
            item["file"] = str(path.relative_to(self.collection_path))
            item["download"] = validate_download(path)
            item["download_status"] = "verified"
            item["pending_apply"] = False
            if apply:
                self.toggle_item(item)
            self.save_collection()
            self.status(path.name + " 다운로드 완료")
            self.refresh_tree()

        def failed(_error):
            item["pending_apply"] = False
            self.save_collection()
            self.refresh_tree()

        self.run_worker("다운로드 중…", work, done, failed)

    def download_all(self):
        if not self.collection:
            return
        public = [x for x in self.collection["items"] if not self.item_file(x) and x["kind"] in {"Google Drive", "직접 링크"}]
        if not public:
            messagebox.showinfo(APP, "새로 받을 링크가 없습니다.", parent=self)
            return
        destination = self.collection_path / "downloads"

        def work():
            complete, errors = [], []
            for item in public:
                fallback = item["name"]
                if Path(fallback).suffix.lower() not in SUPPORTED:
                    fallback += ".zip"
                try:
                    path = download_with_retries(item["url"], destination, fallback)
                    if path.suffix.lower() not in SUPPORTED:
                        path.unlink(missing_ok=True)
                        raise ValueError("지원 모드 파일이 아님")
                    complete.append((item["id"], path))
                except Exception as exc:
                    errors.append((item["url"], str(exc)))
            return complete, errors

        def done(result):
            complete, errors = result
            for item_id, path in complete:
                item = self.item_by_id(item_id)
                if item:
                    item["file"] = str(path.relative_to(self.collection_path))
            auto_groups(self.collection["items"])
            self.save_collection()
            self.refresh_tree()
            message = f"공개 링크 다운로드 {len(complete)}개 완료"
            if errors:
                message += f", 실패 {len(errors)}개"
            self.status(message)

        self.run_worker("공개 파일을 순서대로 다운로드하는 중…", work, done)

    def import_files(self):
        items = self.selected_items()
        if not items:
            messagebox.showinfo(APP, "파일을 연결할 링크 행을 먼저 선택하세요.", parent=self)
            return
        destination = self.collection_path / "downloads"
        destination.mkdir(exist_ok=True)
        files = filedialog.askopenfilenames(
            parent=self, initialdir=str(destination),
            filetypes=[("모드 파일", "*.zip *.rar *.7z *.pak"), ("모든 파일", "*.*")],
        )
        if not files:
            return
        for index, source_name in enumerate(files):
            source = Path(source_name)
            if source.suffix.lower() not in SUPPORTED:
                continue
            target = destination / source.name
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            item = items[min(index, len(items) - 1)]
            item["file"] = str(target.relative_to(self.collection_path))
        auto_groups(self.collection["items"])
        self.save_collection()
        self.refresh_tree()
        self.status(f"{len(files)}개 파일을 컬렉션에 가져왔습니다.")

    def set_group(self):
        items = self.selected_items()
        if not items:
            return
        current = items[0].get("group", "")
        group = simpledialog.askstring("선택 그룹", "같은 그룹에서는 동시에 하나만 적용됩니다.\n그룹 이름(비우면 해제):", initialvalue=current, parent=self)
        if group is None:
            return
        for item in items:
            item["group"] = clean(group)
        self.save_collection()
        self.refresh_tree()

    def open_links(self):
        items = self.selected_items()
        if len(items) == 1 and not self.item_file(items[0]) and items[0]["kind"] in {"Nexus", "Patreon"}:
            self.watch_selected_download(items[0])
            return
        for item in items:
            open_web(item["url"])

    def open_vortex(self):
        vortex = Path(self.settings["vortex_exe"])
        if vortex.exists():
            subprocess.Popen([str(vortex), "--game", "baldursgate3"], cwd=str(vortex.parent))
        else:
            messagebox.showerror(APP, "설정에서 Vortex.exe 경로를 지정하세요.", parent=self)

    def vortex_bridge_dir(self) -> Path:
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return roaming / "Vortex/plugins/bg3-mod-bridge"

    def vortex_bridge_installed(self) -> bool:
        return (self.vortex_bridge_dir() / "index.js").exists()

    def install_vortex_bridge(self):
        source = Path(__file__).resolve().parent / "vortex-extension"
        if not source.exists() and all((Path(__file__).resolve().parent / name).exists() for name in ("index.js", "info.json")):
            source = Path(__file__).resolve().parent
        if not source.exists():
            messagebox.showerror(APP, "배포 파일에 vortex-extension 폴더가 없습니다.", parent=self)
            return
        target = self.vortex_bridge_dir()
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "index.js", target / "index.js")
        shutil.copy2(source / "info.json", target / "info.json")
        messagebox.showinfo(APP, f"Vortex 연동을 설치했습니다.\n\n{target}\n\nVortex를 완전히 종료하고 다시 실행하면 체크박스 직접 해제가 활성화됩니다.", parent=self)
        self.open_vortex()

    def send_vortex_command(self, mod_id: str, enabled: bool):
        command = self.config_path.parent / "vortex-command.json"
        atomic_json(command, {"id": uuid.uuid4().hex, "gameId": "baldursgate3", "modId": mod_id, "enabled": enabled})
        self.open_vortex()
        self.status(f"Vortex에 {mod_id} {'Enable' if enabled else 'Disable'} 요청을 보냈습니다.")

    def open_collection_folder(self):
        selected = self.collections.curselection()
        if selected:
            active = self.collections.index(tk.ACTIVE)
            index = active if active in selected else selected[-1]
            os.startfile(self.collection_paths[index])
        elif self.collection_path:
            os.startfile(self.collection_path)

    def delete_collection(self):
        selected = self.collections.curselection()
        if not selected:
            return
        targets = [self.collection_paths[index].resolve() for index in selected]
        library = self.library.resolve()
        safe_targets = [
            target for target in targets
            if target.parent.parent == library and (target.parent / "project.json").exists()
            and (target / "collection.json").exists()
        ]
        if len(safe_targets) != len(targets):
            messagebox.showerror(APP, "안전하게 확인할 수 없는 컬렉션이 포함되어 삭제를 중단했습니다.", parent=self)
            return
        preview = "\n".join(f"• {target.name}" for target in safe_targets[:8])
        if len(safe_targets) > 8:
            preview += f"\n• 외 {len(safe_targets) - 8}개"
        message = (
            f"선택한 컬렉션 {len(safe_targets)}개와 보관 다운로드를 휴지통 없이 일괄삭제할까요?\n"
            "적용 중인 BG3/Vortex 파일은 삭제하지 않습니다.\n\n" + preview
        )
        if messagebox.askyesno(APP, message, parent=self):
            for target in safe_targets:
                shutil.rmtree(target)
            self.collection = None
            self.collection_path = None
            self.load_collections()
            self.status(f"컬렉션 {len(safe_targets)}개를 일괄삭제했습니다.")

    def show_details(self, _event=None):
        items = self.selected_items()
        text = ""
        if items:
            item = items[0]
            duplicate = item.get("duplicate_display", "")
            duplicate_line = f"\n중복 사용 컬렉션: {duplicate}" if duplicate else ""
            text = f"{item['name']}\n{item['url']}{duplicate_line}\n\n{item.get('description', '')}"
        elif self.collection:
            text = self.collection.get("article", "")[:3000]
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        position = 0
        for number, match in enumerate(re.finditer(r"https?://[^\s<>\"']+", text)):
            self.details.insert("end", text[position:match.start()])
            url = match.group(0).rstrip(".,;)]")
            suffix = match.group(0)[len(url):]
            tag = f"link_{number}"
            self.details.insert("end", url, ("hyperlink", tag))
            self.details.tag_bind(tag, "<Button-1>", lambda _event, target=url: open_web(target))
            self.details.tag_bind(tag, "<Enter>", lambda _event: self.details.configure(cursor="hand2"))
            self.details.tag_bind(tag, "<Leave>", lambda _event: self.details.configure(cursor="xterm"))
            self.details.insert("end", suffix)
            position = match.end()
        self.details.insert("end", text[position:])
        self.details.configure(state="disabled")


def self_test() -> None:
    sample = b'''<html><title>Guide</title><script type="application/ld+json">{"author":{"name":"dragon"}}</script><div class="write_div"><p>choose one</p>
    <a href="https://drive.google.com/file/d/a">AstarionF.zip</a>
    <a href="https://drive.google.com/file/d/b"><strong>AstarionF2.zip</strong></a></div></html>'''
    title, article, items, meta = parse_guide("https://example.test/guide", sample)
    assert title == "Guide"
    assert meta["author"] == "dragon"
    assert "choose one" in article
    assert len(items) == 2 and items[0]["group"] == items[1]["group"]
    assert drive_download_url("https://drive.google.com/file/d/abc/view").endswith("id=abc")

    authored = b'''<html><title>Labels</title><div class="write_div">
    <p>\xec\x84\xa0\xed\x96\x89 \xeb\xaa\xa8\xeb\x93\x9c</p>
    <p>1. hijimare hairs</p><a href="https://www.nexusmods.com/baldursgate3/mods/13881">https://www.nexusmods.com/baldursgate3/mods/13881</a>
    <a href="https://www.nexusmods.com/baldursgate3/mods/13881"><strong>Just a moment...</strong></a>
    <p>2. Myky's Hairstyles</p><a href="https://www.nexusmods.com/baldursgate3/mods/16198">https://www.nexusmods.com/baldursgate3/mods/16198</a>
    <p>3. OnHee's Head Presets</p><a href="https://www.nexusmods.com/baldursgate3/mods/8495">OnHee's Head Presets at Baldur's Gate 3 Nexus - Mods and community</a>
    <p>4. Moonlight Horn Collection</p><a href="https://www.patreon.com/posts/moonlight-horn-138448628">Moonlight Horn Collection | Patreon</a>
    <p>Unrelated later preview</p><a href="https://www.nexusmods.com/baldursgate3/mods/13881"></a>
    </div></html>'''
    _title, _article, authored_items, _meta = parse_guide("https://example.test/labels", authored)
    assert [item["name"] for item in authored_items] == [
        "hijimare hairs", "Myky's Hairstyles", "OnHee's Head Presets", "Moonlight Horn Collection"
    ]
    with tempfile.TemporaryDirectory() as folder:
        downloaded = Path(folder) / "Moonlight Horn.zip"
        downloaded.write_bytes(b"test")
        assert locate_browser_download("", folder, time.time() - 1) == downloaded
        assert locate_browser_download("", folder, time.time() - 1, {str(downloaded.resolve())}) is None
        target = Path(folder) / "AstarionF.zip"
        target.write_bytes(b"test")
        time.sleep(0.01)
        unrelated = Path(folder) / "Other Newer.pak"
        unrelated.write_bytes(b"test")
        assert locate_browser_download("", folder, time.time() - 1, hint="AstarionF hair") == target
    print("self-test: OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        try:
            App().mainloop()
        except Exception:
            error = traceback.format_exc()
            local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "BG3ModBridge"
            local.mkdir(parents=True, exist_ok=True)
            (local / "error.log").write_text(error, encoding="utf-8")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, error, "BG3 Mod Bridge 실행 오류", 0x10)
            finally:
                raise
