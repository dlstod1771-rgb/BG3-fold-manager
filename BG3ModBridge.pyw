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
        if tag == "div" and "write_div" in classes:
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
    host = urllib.parse.urlparse(url).netloc.lower()
    if "nexusmods.com" in host:
        return "Nexus"
    if "patreon.com" in host:
        return "Patreon"
    if "drive.google.com" in host or "drive.usercontent.google.com" in host:
        return "Google Drive"
    if "dcinside.com" in host:
        return "안내 글"
    return "직접 링크"


def useful_label(label: str, url: str) -> str:
    label = clean(label)
    files = re.findall(r"[^\s<>:\"/|?*]+\.(?:zip|rar|7z|pak)\b", label, re.I)
    if files:
        return files[0]
    if label and not label.startswith(("http://", "https://")) and "Just a moment" not in label:
        return label[:120]
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).rstrip("/")
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
        name = heading if item_kind(target) in {"Nexus", "Patreon", "안내 글"} and heading else fallback
        items.append({
            "id": uuid.uuid4().hex,
            "url": target,
            "name": name,
            "description": link["description"],
            "kind": item_kind(target),
            "file": "",
            "group": "",
            "desired": False,
            "pending_apply": False,
            "managed_hash": "",
            "vortex_id": "",
        })
    auto_groups(items)
    return meta["content"], parser.article, items, meta


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
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"})
    with urllib.request.urlopen(request, timeout=35) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(), charset


def drive_download_url(url: str) -> str:
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
    return safe_name(fallback)


def download_file(url: str, destination: Path, fallback_name: str) -> Path:
    if item_kind(url) == "Google Drive":
        url = drive_download_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=90) as response:
        name = content_filename(response.headers, fallback_name)
        target = destination / name
        content_type = response.headers.get_content_type()
        if content_type == "text/html" and target.suffix.lower() not in SUPPORTED:
            raise ValueError("로그인 또는 다운로드 확인이 필요한 페이지입니다.")
        with target.open("wb") as f:
            shutil.copyfileobj(response, f)
    return target


def download_with_retries(url: str, destination: Path, fallback_name: str, attempts: int = 3) -> Path:
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            return download_file(url, destination, fallback_name)
        except Exception as exc:
            errors.append(f"{attempt}차: {exc}")
            if attempt < attempts:
                time.sleep(attempt)
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

        ttk.Label(left, text="링크별 컬렉션", font=("맑은 고딕", 11, "bold")).pack(anchor="w", pady=(0, 5))
        self.collections = tk.Listbox(left, exportselection=False)
        self.collections.pack(fill="both", expand=True)
        self.collections.bind("<<ListboxSelect>>", self.select_collection)
        lf = ttk.Frame(left)
        lf.pack(fill="x", pady=6)
        ttk.Button(lf, text="폴더 열기", command=self.open_collection_folder).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(lf, text="삭제", command=self.delete_collection).pack(side="left", expand=True, fill="x", padx=(2, 0))

        columns = ("check", "name", "kind", "file", "group", "status")
        filters = ttk.Frame(right, padding=(0, 0, 0, 6))
        filters.pack(fill="x")
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

        self.tree = ttk.Treeview(right, columns=columns, show="headings", selectmode="extended")
        self.tree_headers = {"check": "적용", "name": "모드/링크", "kind": "출처", "file": "다운로드", "group": "선택 그룹", "status": "상태"}
        widths = {"check": 55, "name": 235, "kind": 90, "file": 175, "group": 145, "status": 125}
        for col in columns:
            self.tree.heading(col, text=self.tree_headers[col], command=lambda column=col: self.sort_tree(column))
            self.tree.column(col, width=widths[col], minwidth=45, anchor="center" if col in {"check", "kind"} else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<<TreeviewSelect>>", self.show_details)

        buttons = ttk.Frame(right, padding=(0, 7))
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

        self.details = tk.Text(right, height=7, wrap="word", state="disabled", background="#f5f5f5")
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
        item["name"] = target.name
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
                item["name"] = target.name
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
                    current["name"] = source.name
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
            raw, encoding = fetch_url(url)
            return parse_guide(url, raw, encoding)

        def done(result):
            title, article, items, meta = result
            folder = self.library / collection_dir_name(meta)
            if folder.exists():
                number = 2
                while (candidate := self.library / f"{collection_dir_name(meta)} ({number})").exists():
                    number += 1
                folder = candidate
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "downloads").mkdir()
            data = {"version": 2, "title": title, "content": meta["content"], "site": meta["site"], "author": meta["author"], "url": url, "created": datetime.now().isoformat(timespec="seconds"), "article": article, "items": items}
            atomic_json(folder / "collection.json", data)
            (folder / "설치안내.txt").write_text(f"{title}\n{url}\n\n{article}", encoding="utf-8")
            self.status(f"{len(items)}개 링크를 수집했습니다.")
            self.load_collections(select=folder)

        self.run_worker("페이지 설명과 링크를 수집하는 중…", work, done)

    def load_collections(self, select: Path | None = None):
        self.library.mkdir(parents=True, exist_ok=True)
        self.collection_paths = sorted((p.parent for p in self.library.glob("*/collection.json")), reverse=True)
        self.collections.delete(0, "end")
        selected = None
        for idx, path in enumerate(self.collection_paths):
            try:
                data = json.loads((path / "collection.json").read_text(encoding="utf-8"))
                self.collections.insert("end", data.get("title", path.name))
            except (OSError, json.JSONDecodeError):
                self.collections.insert("end", path.name + " (손상됨)")
            if select and path.resolve() == select.resolve():
                selected = idx
        if self.collection_paths:
            idx = selected if selected is not None else 0
            self.collections.selection_set(idx)
            self.collections.event_generate("<<ListboxSelect>>")
        else:
            self.collection = None
            self.collection_path = None
            self.tree.delete(*self.tree.get_children())

    def select_collection(self, _event=None):
        selected = self.collections.curselection()
        if not selected:
            return
        self.collection_path = self.collection_paths[selected[0]]
        try:
            self.collection = json.loads((self.collection_path / "collection.json").read_text(encoding="utf-8"))
            if all(self.collection.get(key) for key in ("content", "site", "author")):
                wanted = self.collection_path.parent / collection_dir_name(self.collection)
                if self.collection_path.name != wanted.name and not wanted.exists():
                    self.collection_path.rename(wanted)
                    self.collection_path = wanted
                    self.collection_paths[selected[0]] = wanted
            self.url_var.set(self.collection.get("url", ""))
            self.refresh_tree()
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP, f"컬렉션을 읽을 수 없습니다.\n{exc}", parent=self)

    def save_collection(self):
        if self.collection and self.collection_path:
            atomic_json(self.collection_path / "collection.json", self.collection)

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
            values = ("☑" if active or pending else "☐", item["name"], item["kind"], file.name if file else "—", item.get("group") or "—", state)
            rows.append((item, active, values))
        if self.sort_column:
            index = ("check", "name", "kind", "file", "group", "status").index(self.sort_column)
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
        if not active and item.get("group"):
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
            item["name"] = path.name
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
                    item["name"] = path.name
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
        initial = self.settings.get("browser_downloads", "")
        files = filedialog.askopenfilenames(parent=self, initialdir=initial, filetypes=[("모드 파일", "*.zip *.rar *.7z *.pak"), ("모든 파일", "*.*")])
        if not files:
            return
        destination = self.collection_path / "downloads"
        destination.mkdir(exist_ok=True)
        for index, source_name in enumerate(files):
            source = Path(source_name)
            if source.suffix.lower() not in SUPPORTED:
                continue
            target = destination / source.name
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            item = items[min(index, len(items) - 1)]
            item["file"] = str(target.relative_to(self.collection_path))
            item["name"] = target.name
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
        if self.collection_path:
            os.startfile(self.collection_path)

    def delete_collection(self):
        if not self.collection_path:
            return
        if messagebox.askyesno(APP, "이 컬렉션 폴더와 보관한 다운로드를 휴지통 없이 삭제할까요?\n적용 중인 BG3/Vortex 파일은 삭제하지 않습니다.", parent=self):
            target = self.collection_path.resolve()
            if target.parent == self.library.resolve() and (target / "collection.json").exists():
                shutil.rmtree(target)
                self.load_collections()

    def show_details(self, _event=None):
        items = self.selected_items()
        text = ""
        if items:
            item = items[0]
            text = f"{item['name']}\n{item['url']}\n\n{item.get('description', '')}"
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
