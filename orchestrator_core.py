"""Safety-first orchestration primitives for BG3 Mod Bridge.

This module is intentionally UI independent.  It owns deterministic URL parsing,
collection migrations, planning, capability discovery and downloaded-file
validation.  Browser login/CAPTCHA handling is deliberately outside its scope.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
SUPPORTED_EXTENSIONS = {".zip", ".rar", ".7z", ".pak"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
HTML_PREFIXES = (b"<!doctype html", b"<html", b"<?xml")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BG3ModBridge/2.0"


class DownloadValidationError(ValueError):
    """The server response is not a safe, usable mod download."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url(url: str) -> str:
    """Normalize only syntax; never invent or remove download identifiers."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return url.strip()
    host = (parsed.hostname or "").lower()
    if parsed.port and parsed.port not in {80, 443}:
        host += f":{parsed.port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, path, query, ""))


def source_details(url: str) -> dict[str, Any]:
    normalized = normalize_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    query = urllib.parse.parse_qs(parsed.query)
    result: dict[str, Any] = {
        "source_type": "direct",
        "normalized_url": normalized,
        "source_identity": normalized,
    }
    if host.endswith("nexusmods.com"):
        match = re.fullmatch(r"/([^/]+)/mods/(\d+)/?", parsed.path)
        game_domain = match.group(1) if match else None
        mod_id = match.group(2) if match else None
        # A file id is trustworthy only when the source URL explicitly supplies it.
        raw_file_id = (query.get("file_id") or query.get("fileId") or [None])[0]
        file_id = raw_file_id if raw_file_id and str(raw_file_id).isdigit() else None
        result.update({
            "source_type": "nexus",
            "nexus": {"game_domain": game_domain, "mod_id": mod_id, "file_id": file_id},
            "source_identity": f"nexus:{game_domain}:{mod_id}:{file_id or 'page'}",
        })
    elif host.endswith("patreon.com"):
        result.update({"source_type": "patreon", "source_identity": f"patreon:{parsed.path.rstrip('/')}"})
    elif host in {"drive.google.com", "drive.usercontent.google.com"}:
        match = re.search(r"/file/d/([^/?]+)", parsed.path)
        file_id = match.group(1) if match else (query.get("id") or [None])[0]
        result.update({
            "source_type": "google_drive",
            "google_drive": {"file_id": file_id},
            "source_identity": f"google_drive:{file_id}" if file_id else normalized,
        })
    elif host.endswith("dcinside.com"):
        gallery = (query.get("id") or [None])[0]
        number = (query.get("no") or [None])[0]
        mobile = re.fullmatch(r"/board/([^/]+)/(\d+)", parsed.path)
        if mobile:
            gallery, number = mobile.groups()
        identity = f"dcinside:{gallery}:{number}" if gallery and number else normalized
        result.update({"source_type": "dcinside", "source_identity": identity})
    return result


def duplicate_identity(url: str) -> str:
    """Identity used to find the same mod across different guide collections."""
    details = source_details(url)
    if details["source_type"] == "nexus":
        nexus = details.get("nexus") or {}
        if nexus.get("game_domain") and nexus.get("mod_id"):
            return f"nexus:{nexus['game_domain']}:{nexus['mod_id']}"
    return details["source_identity"]


def annotate_duplicate_usage(collections: list[dict[str, Any]]) -> dict[str, list[str]]:
    usage: dict[str, set[str]] = {}
    for collection in collections:
        label = str(collection.get("series_number") or collection.get("title") or "?")
        for item in collection.get("items", []):
            if source_details(item.get("url", ""))["source_type"] == "dcinside":
                continue
            usage.setdefault(duplicate_identity(item.get("url", "")), set()).add(label)
    result = {key: sorted(values, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
              for key, values in usage.items() if len(values) > 1}
    for collection in collections:
        for item in collection.get("items", []):
            labels = result.get(duplicate_identity(item.get("url", "")), [])
            item["duplicate_collections"] = labels
            item["duplicate_count"] = len(labels)
            item["duplicate_display"] = ", ".join(labels) if labels else ""
            if labels:
                item["group"] = "공유모드"
                item["alternative_group"] = "공유모드"
            elif item.get("group") == "공유모드":
                item["group"] = ""
                item["alternative_group"] = ""
    return result


def provenance_for(url: str, origin_guide_url: str = "", anchor_text: str = "",
                   context_before: str = "", context_after: str = "") -> dict[str, Any]:
    details = source_details(url)
    return {
        "source_url": url,
        "normalized_url": details["normalized_url"],
        "source_identity": details["source_identity"],
        "origin_guide_url": normalize_url(origin_guide_url) if origin_guide_url else "",
        "anchor_text": anchor_text,
        "context_before": context_before,
        "context_after": context_after,
        "captured_at": utc_now(),
    }


def migrate_item(item: dict[str, Any], origin_guide_url: str = "") -> bool:
    changed = False
    url = item.get("url", "")
    details = source_details(url)
    original_name = item.get("name", "")
    if item.get("file") and Path(str(item["file"])).name == original_name:
        anchor = str((item.get("provenance") or {}).get("anchor_text", "")).strip()
        if anchor and not anchor.startswith(("http://", "https://")) and "Just a moment" not in anchor:
            original_name = anchor[:160]
            item["name"] = original_name
            changed = True
    defaults: dict[str, Any] = {
        "original_name": original_name,
        "custom_name": False,
        "source_type": details["source_type"],
        "requirement": "unknown",
        "alternative_group": item.get("group", ""),
        "confidence": 0.0,
        "classification_reason": "작성자의 명시적 의미 분류가 필요합니다.",
        "download_status": "verified" if item.get("file") else "planned",
        "download": {},
        "provenance": provenance_for(
            url, origin_guide_url, item.get("name", ""), item.get("description", ""), ""
        ),
    }
    if details.get("nexus") is not None:
        defaults["nexus"] = details["nexus"]
    if details.get("google_drive") is not None:
        defaults["google_drive"] = details["google_drive"]
    for key, value in defaults.items():
        if key not in item:
            item[key] = value
            changed = True
    return changed


def migrate_collection(data: dict[str, Any]) -> bool:
    changed = False
    if data.get("schema_version") != SCHEMA_VERSION:
        data["schema_version"] = SCHEMA_VERSION
        changed = True
    if data.get("version", 0) < 3:
        data["version"] = 3
        changed = True
    project_defaults = {
        "game": "Baldur's Gate 3",
        "project_id": f"legacy:{data.get('site', 'local')}:{data.get('author', 'unknown')}" if data.get("series_number") else f"collection:{data.get('url', data.get('title', 'local'))}",
        "project_title": "NPC 외형 변경 모드 1~30" if data.get("series_number") else data.get("title", "개별 컬렉션"),
        "project_created": data.get("created", utc_now()),
    }
    for key, value in project_defaults.items():
        if key not in data:
            data[key] = value
            changed = True
    canonical_project_id = str(data.get("project_id", "")).casefold()
    if data.get("project_id") != canonical_project_id:
        data["project_id"] = canonical_project_id
        changed = True
    origin = data.get("url", "")
    for item in data.setdefault("items", []):
        changed = migrate_item(item, origin) or changed
    if "queue" not in data:
        data["queue"] = {"updated_at": utc_now(), "jobs": []}
        changed = True
    return changed


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith(("/", "\\")) or "\x00" in normalized:
        return False
    pure = PurePosixPath(normalized)
    if any(part == ".." for part in pure.parts):
        return False
    return not bool(re.match(r"^[A-Za-z]:", normalized))


def validate_download(path: Path, content_type: str = "") -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise DownloadValidationError("다운로드 파일이 없거나 비어 있습니다.")
    with path.open("rb") as stream:
        prefix = stream.read(512).lstrip().lower()
    if "html" in content_type.lower() or prefix.startswith(HTML_PREFIXES) or b"<html" in prefix:
        raise DownloadValidationError("서버가 모드 파일 대신 HTML 로그인/오류 페이지를 반환했습니다.")
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise DownloadValidationError(f"지원하지 않는 파일 형식입니다: {extension or '(확장자 없음)'}")
    entries = 0
    if extension == ".zip":
        if not zipfile.is_zipfile(path):
            raise DownloadValidationError("확장자는 ZIP이지만 유효한 ZIP 보관 파일이 아닙니다.")
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if not infos:
                    raise DownloadValidationError("ZIP 보관 파일이 비어 있습니다.")
                for info in infos:
                    if not _safe_member(info.filename):
                        raise DownloadValidationError(f"ZIP 경로 이탈 항목을 차단했습니다: {info.filename}")
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise DownloadValidationError(f"ZIP 심볼릭 링크를 차단했습니다: {info.filename}")
                corrupt = archive.testzip()
                if corrupt:
                    raise DownloadValidationError(f"ZIP CRC 검사 실패: {corrupt}")
                entries = len(infos)
        except zipfile.BadZipFile as exc:
            raise DownloadValidationError("손상된 ZIP 보관 파일입니다.") from exc
    elif extension == ".rar" and not prefix.startswith(b"rar!\x1a\x07"):
        raise DownloadValidationError("확장자는 RAR이지만 RAR 서명이 없습니다.")
    elif extension == ".7z" and not prefix.startswith(b"7z\xbc\xaf\x27\x1c"):
        raise DownloadValidationError("확장자는 7z이지만 7z 서명이 없습니다.")
    return {
        "path": str(path.resolve()),
        "file_name": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "content_type": content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "archive_format": extension.lstrip("."),
        "entries": entries,
        "verified_at": utc_now(),
    }


def google_drive_download_url(url: str) -> str:
    details = source_details(url)
    file_id = (details.get("google_drive") or {}).get("file_id")
    if not file_id:
        return url
    return "https://drive.usercontent.google.com/download?export=download&confirm=t&id=" + urllib.parse.quote(file_id)


def _response_filename(headers: Any, fallback: str) -> str:
    value = headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", value, re.I)
    plain = re.search(r'filename="?([^";]+)', value, re.I)
    name = urllib.parse.unquote(encoded.group(1)) if encoded else plain.group(1) if plain else fallback
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download.bin"


def safe_download(url: str, destination: Path, fallback_name: str, timeout: int = 90) -> tuple[Path, dict[str, Any]]:
    details = source_details(url)
    if details["source_type"] in {"nexus", "patreon", "dcinside"}:
        raise PermissionError("이 주소는 파일 직링크가 아닙니다. 브라우저에서 로그인 후 직접 다운로드하세요.")
    request_url = google_drive_download_url(url) if details["source_type"] == "google_drive" else url
    destination.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(request_url, headers={"User-Agent": USER_AGENT})
    part: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            name = _response_filename(response.headers, fallback_name)
            target = destination / name
            if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise DownloadValidationError("응답 파일명에 지원되는 모드 확장자가 없습니다.")
            part = destination / f".{name}.{uuid.uuid4().hex}.part"
            expected = response.headers.get("Content-Length")
            written = 0
            with part.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    written += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if expected and expected.isdigit() and written != int(expected):
                raise DownloadValidationError(f"다운로드 크기 불일치: 예상 {expected}, 실제 {written}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            # Validation uses the final extension while the temporary file remains atomic.
            validation_view = part.with_name(part.name + target.suffix)
            part.replace(validation_view)
            part = validation_view
            metadata = validate_download(part, content_type)
            if target.exists():
                if sha256_file(target) == metadata["sha256"]:
                    part.unlink()
                    metadata["path"] = str(target.resolve())
                    return target, metadata
                target = destination / f"{target.stem} ({int(datetime.now().timestamp())}){target.suffix}"
            part.replace(target)
            metadata["path"] = str(target.resolve())
            metadata["file_name"] = target.name
            return target, metadata
    finally:
        if part and part.exists():
            part.unlink(missing_ok=True)


def build_plan(collection: dict[str, Any], collection_path: Path | None = None) -> dict[str, Any]:
    migrate_collection(collection)
    jobs = []
    for item in collection.get("items", []):
        details = source_details(item.get("url", ""))
        status = "planned"
        adapter = "direct_http"
        reason = "공개 파일 링크는 실행 승인 후 다운로드할 수 있습니다."
        if item.get("file"):
            file_path = (collection_path / item["file"]) if collection_path else Path(item["file"])
            if file_path.exists():
                try:
                    item["download"] = validate_download(file_path)
                    status, reason = "verified", "로컬 파일 검증 완료"
                except DownloadValidationError as exc:
                    status, reason = "needs_review", str(exc)
        elif details["source_type"] == "nexus":
            adapter = "nexus_cli_or_browser"
            if not (details.get("nexus") or {}).get("file_id"):
                status, reason = "needs_review", "원문 URL에 Nexus file_id가 없어 추측하지 않습니다."
            else:
                status, reason = "browser_required", "Nexus 로그인/권한을 사용자 브라우저에서 확인해야 합니다."
        elif details["source_type"] == "patreon":
            adapter, status = "browser", "browser_required"
            reason = "Patreon 접근 권한 및 로그인은 사용자가 직접 확인해야 합니다."
        elif details["source_type"] == "dcinside":
            adapter, status = "guide_parser", "needs_review"
            reason = "내부 가이드 링크는 수집 대상으로 재귀 분석해야 합니다."
        elif details["source_type"] == "google_drive":
            adapter = "gdown_or_http"
            if not (details.get("google_drive") or {}).get("file_id"):
                status, reason = "needs_review", "Google Drive 파일 ID를 URL에서 확인할 수 없습니다."
        job = {
            "job_id": item.get("id") or uuid.uuid4().hex,
            "item_name": item.get("name", ""),
            "source_identity": details["source_identity"],
            "source_type": details["source_type"],
            "adapter": adapter,
            "status": status,
            "reason": reason,
        }
        jobs.append(job)
        item["download_status"] = status
    collection["queue"] = {"updated_at": utc_now(), "jobs": jobs}
    return {"schema_version": SCHEMA_VERSION, "dry_run": True, "created_at": utc_now(), "jobs": jobs}


def capability_report() -> dict[str, Any]:
    commands = {"python": ["python", "--version"], "git": ["git", "--version"], "7z": ["7z", "--help"],
                "gdown": ["gdown", "--version"], "nexus-cli": ["nexus-cli", "--version"]}
    tools: dict[str, Any] = {}
    for name, command in commands.items():
        executable = shutil.which(command[0])
        info: dict[str, Any] = {"available": bool(executable), "path": executable or "", "version": ""}
        if executable:
            try:
                completed = subprocess.run([executable, *command[1:]], capture_output=True, text=True,
                                           timeout=5, shell=False)
                info["version"] = (completed.stdout or completed.stderr).strip().splitlines()[0][:300]
            except (OSError, subprocess.SubprocessError) as exc:
                info["error"] = type(exc).__name__
        tools[name] = info
    if os.name == "nt":
        handler = ""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"nxm\shell\open\command") as key:
                handler = str(winreg.QueryValueEx(key, None)[0])
        except (ImportError, OSError):
            pass
        tools["nxm_handler"] = {
            "available": bool(handler),
            "command": handler,
            "manager": "Kortex" if "kortex" in handler.casefold() else "Vortex" if "vortex" in handler.casefold() else "other" if handler else "",
        }
    return {"checked_at": utc_now(), "platform": os.name, "tools": tools}


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
