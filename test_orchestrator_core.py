import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestrator_core import (
    annotate_duplicate_usage,
    DownloadValidationError,
    build_plan,
    migrate_collection,
    source_details,
    validate_download,
)


class OrchestratorCoreTests(unittest.TestCase):
    def test_nexus_file_id_is_never_guessed(self):
        page = source_details("https://www.nexusmods.com/baldursgate3/mods/13881")
        explicit = source_details("https://www.nexusmods.com/baldursgate3/mods/13881?file_id=98765")
        self.assertEqual(page["nexus"]["mod_id"], "13881")
        self.assertIsNone(page["nexus"]["file_id"])
        self.assertEqual(explicit["nexus"]["file_id"], "98765")

    def test_missing_nexus_file_id_needs_review(self):
        data = {"url": "https://example.test", "items": [{"id": "a", "name": "A", "url":
            "https://www.nexusmods.com/baldursgate3/mods/13881", "file": ""}]}
        plan = build_plan(data)
        self.assertEqual(plan["jobs"][0]["status"], "needs_review")

    def test_migration_preserves_existing_fields(self):
        data = {"url": "https://guide.test", "items": [{"id": "a", "name": "A", "url":
            "https://example.test/a.zip", "custom": 42}]}
        self.assertTrue(migrate_collection(data))
        self.assertEqual(data["items"][0]["custom"], 42)
        self.assertEqual(data["items"][0]["provenance"]["origin_guide_url"], "https://guide.test/")

    def test_valid_zip_and_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "mod.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("Mods/example.pak", b"pak")
            result = validate_download(archive)
            self.assertEqual(result["entries"], 1)
            self.assertEqual(len(result["sha256"]), 64)

    def test_zip_slip_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "evil.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("../../evil.exe", b"no")
            with self.assertRaises(DownloadValidationError):
                validate_download(archive)

    def test_html_disguised_as_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "login.zip"
            archive.write_text("<!doctype html><html>login</html>", encoding="utf-8")
            with self.assertRaises(DownloadValidationError):
                validate_download(archive)

    def test_duplicate_usage_lists_collection_numbers(self):
        collections = [
            {"series_number": 1, "items": [{"url": "https://www.nexusmods.com/baldursgate3/mods/2900"}]},
            {"series_number": 2, "items": [{"url": "https://www.nexusmods.com/baldursgate3/mods/2900?file_id=42"}]},
            {"series_number": 3, "items": [{"url": "https://example.test/unique.zip"}]},
        ]
        annotate_duplicate_usage(collections)
        self.assertEqual(collections[0]["items"][0]["duplicate_display"], "1, 2")
        self.assertEqual(collections[1]["items"][0]["duplicate_display"], "1, 2")
        self.assertEqual(collections[0]["items"][0]["group"], "공유모드")
        self.assertEqual(collections[1]["items"][0]["alternative_group"], "공유모드")
        self.assertEqual(collections[2]["items"][0]["duplicate_display"], "")

    def test_collection_migration_adds_project_status(self):
        data = {"series_number": 1, "title": "NPC 1", "site": "발게3 디시", "author": "Dragon", "items": []}
        migrate_collection(data)
        self.assertEqual(data["game"], "Baldur's Gate 3")
        self.assertTrue(data["project_id"])
        self.assertTrue(data["project_created"])

    def test_project_id_is_case_insensitive(self):
        upper = {"project_id": "Legacy:BG3:Dragon", "title": "A", "items": []}
        lower = {"project_id": "legacy:bg3:dragon", "title": "B", "items": []}
        migrate_collection(upper)
        migrate_collection(lower)
        self.assertEqual(upper["project_id"], lower["project_id"])

    def test_migration_preserves_display_name_separately_from_file(self):
        data = {"url": "https://guide.test", "items": [{
            "name": "Myky's Hairstyles", "url": "https://example.test/mod.zip",
            "file": "downloads/completely-different-file.zip",
        }]}
        migrate_collection(data)
        item = data["items"][0]
        self.assertEqual(item["name"], "Myky's Hairstyles")
        self.assertEqual(item["original_name"], "Myky's Hairstyles")
        self.assertEqual(item["file"], "downloads/completely-different-file.zip")

    def test_dcinside_identity_ignores_list_navigation_query(self):
        desktop = source_details("https://gall.dcinside.com/mgallery/board/view/?id=bg3&no=926806&page=1")
        mobile = source_details("https://m.dcinside.com/board/bg3/926806")
        self.assertEqual(desktop["source_identity"], mobile["source_identity"])


if __name__ == "__main__":
    unittest.main()
