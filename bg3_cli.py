"""Command-line workflow for inspecting and running BG3 Mod Bridge jobs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from BG3ModBridge import collect_guide_tree
from orchestrator_core import (
    atomic_write_json,
    build_plan,
    capability_report,
    migrate_collection,
    safe_download,
    validate_download,
)


def load_collection(path: Path) -> tuple[Path, dict]:
    data_path = path / "collection.json" if path.is_dir() else path
    data = json.loads(data_path.read_text(encoding="utf-8"))
    migrate_collection(data)
    return data_path, data


def command_doctor(args) -> int:
    report = capability_report()
    if args.output:
        atomic_write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_parse(args) -> int:
    title, article, items, meta = collect_guide_tree(args.url, args.max_depth, args.max_pages)
    collection = {
        "version": 3,
        "schema_version": 1,
        "title": title,
        "content": meta["content"],
        "site": meta["site"],
        "author": meta["author"],
        "url": args.url,
        "article": article,
        "items": items,
        "guide_sources": meta.get("guide_sources", []),
        "collection_errors": meta.get("collection_errors", []),
        "queue": {"jobs": []},
    }
    migrate_collection(collection)
    output = Path(args.output)
    atomic_write_json(output, collection)
    print(f"수집 완료: {len(items)}개 항목, {len(collection['guide_sources'])}개 가이드 → {output}")
    return 0


def command_plan(args) -> int:
    data_path, collection = load_collection(Path(args.collection))
    plan = build_plan(collection, data_path.parent)
    output = Path(args.output) if args.output else data_path.parent / "download-plan.json"
    atomic_write_json(data_path, collection)
    atomic_write_json(output, plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"계획 저장: {output}")
    return 0


def command_validate(args) -> int:
    print(json.dumps(validate_download(Path(args.file)), ensure_ascii=False, indent=2))
    return 0


def command_download(args) -> int:
    data_path, collection = load_collection(Path(args.collection))
    plan = build_plan(collection, data_path.parent)
    runnable = {job["job_id"] for job in plan["jobs"] if job["status"] == "planned"}
    if not args.execute:
        print(f"DRY RUN: 공개 파일 {len(runnable)}개를 다운로드할 수 있습니다. 파일은 변경하지 않았습니다.")
        print("실행하려면 --execute --approve-downloads를 함께 지정하세요.")
        return 0
    if not args.approve_downloads:
        print("실행 승인이 없습니다: --approve-downloads가 필요합니다.", file=sys.stderr)
        return 2
    destination = data_path.parent / "downloads"
    failures = []
    for item in collection["items"]:
        if item.get("id") not in runnable:
            continue
        fallback = item.get("name", "mod")
        if Path(fallback).suffix.lower() not in {".zip", ".rar", ".7z", ".pak"}:
            fallback += ".zip"
        item["download_status"] = "running"
        atomic_write_json(data_path, collection)
        try:
            target, metadata = safe_download(item["url"], destination, fallback)
            item["file"] = str(target.relative_to(data_path.parent))
            item["name"] = target.name
            item["download"] = metadata
            item["download_status"] = "verified"
        except Exception as exc:
            item["download_status"] = "failed"
            item["download_error"] = f"{type(exc).__name__}: {exc}"
            failures.append(item["name"])
        atomic_write_json(data_path, collection)
    print(f"완료: 성공 {len(runnable) - len(failures)}, 실패 {len(failures)}")
    return 1 if failures else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="BG3 Mod Bridge 안전 수집·다운로드 도구")
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="사용 가능한 도구를 진단합니다.")
    doctor.add_argument("--output")
    doctor.set_defaults(func=command_doctor)
    parse = sub.add_parser("parse-guide", help="DCInside 가이드 트리를 수집합니다.")
    parse.add_argument("url")
    parse.add_argument("--output", required=True)
    parse.add_argument("--max-depth", type=int, default=3)
    parse.add_argument("--max-pages", type=int, default=40)
    parse.set_defaults(func=command_parse)
    plan = sub.add_parser("plan", help="실행하지 않고 다운로드 계획을 만듭니다.")
    plan.add_argument("collection")
    plan.add_argument("--output")
    plan.set_defaults(func=command_plan)
    validate = sub.add_parser("validate", help="보관 파일을 안전 검사합니다.")
    validate.add_argument("file")
    validate.set_defaults(func=command_validate)
    download = sub.add_parser("download", help="계획된 공개 파일을 다운로드합니다.")
    download.add_argument("collection")
    download.add_argument("--execute", action="store_true")
    download.add_argument("--approve-downloads", action="store_true")
    download.set_defaults(func=command_download)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
