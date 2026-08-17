import unittest
import tempfile
import shutil
import threading
import urllib.error
import urllib.request
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app import ArchiveRepository, AppHandler, APP_VERSION, CATEGORIES, API_VERSION, ThreadingHTTPServer, load_config
from scripts.classify_catalog import score_asset
from scripts.build_catalog import scan
from scripts.build_search_index import build
from scripts.incremental_scan import synchronize


class RepositoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = Path(__file__).resolve().parents[1] / "data" / "catalog.sqlite"
        if not cls.database.exists():
            raise unittest.SkipTest("catalog.sqlite not found")

    def test_stats_match_catalog(self):
        stats = ArchiveRepository(self.database).stats()
        self.assertEqual(stats["api_version"], API_VERSION)
        self.assertEqual(stats["total"], 657)
        self.assertEqual(stats["ignored"], 126)
        self.assertEqual(stats["indexed"], 553)
        self.assertEqual(stats["review"], 0)
        self.assertTrue(set(stats["categories"]).issubset(CATEGORIES))

    def test_chinese_full_text_search(self):
        result = ArchiveRepository(self.database).search(query="离线安装", limit=10)
        self.assertGreaterEqual(result["total"], 2)
        self.assertTrue(any("离线安装" in (item["title_clean"] or "") or "<mark>" in item["excerpt"] for item in result["items"]))

    def test_filter_category(self):
        result = ArchiveRepository(self.database).search(category="technology", limit=5)
        self.assertGreater(result["total"], 0)
        self.assertTrue(all(item["primary_category"] == "technology" for item in result["items"]))

    def test_domain_date_and_sort_filters(self):
        repository = ArchiveRepository(self.database)
        sample = next(item for item in repository.search(limit=100)["items"] if item["source_domain"] and item["saved_at"])
        saved_date = sample["saved_at"][:10]
        result = repository.search(domain=sample["source_domain"], date_from=saved_date, date_to=saved_date, sort="size_desc", limit=100)
        self.assertGreater(result["total"], 0)
        self.assertTrue(all(item["source_domain"] == sample["source_domain"] for item in result["items"]))
        self.assertTrue(all(item["saved_at"][:10] == saved_date for item in result["items"]))
        sizes = [item["size_bytes"] for item in result["items"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_common_tag_filter_is_exact(self):
        repository = ArchiveRepository(self.database)
        common_tags = repository.stats()["tags"]
        self.assertGreater(len(common_tags), 0)
        selected = common_tags[0]["tag"]
        result = repository.search(tag=selected, limit=100)
        self.assertEqual(result["total"], common_tags[0]["count"])
        self.assertTrue(all(selected in item["tags"] for item in result["items"]))

    def test_search_rejects_invalid_date_and_sort(self):
        repository = ArchiveRepository(self.database)
        with self.assertRaises(ValueError):
            repository.search(date_from="2026-99-99")
        with self.assertRaises(ValueError):
            repository.search(date_from="2026-08-16", date_to="2026-08-15")
        with self.assertRaises(ValueError):
            repository.search(sort="drop-table")
        with self.assertRaises(ValueError):
            repository.search(issue="unknown")

    def test_maintenance_counts_and_filters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            assets = repository.search(limit=4)["items"]
            with sqlite3.connect(database) as db:
                db.execute("UPDATE assets SET duplicate_group='test-group' WHERE asset_id IN (?,?)", (assets[0]["asset_id"], assets[1]["asset_id"]))
                db.execute("UPDATE assets SET parse_status='error', error_message='test parse error' WHERE asset_id=?", (assets[2]["asset_id"],))
                db.execute("UPDATE contents SET extraction_status='error', error_message='test extraction error' WHERE asset_id=?", (assets[3]["asset_id"],))
                db.execute("UPDATE assets SET file_status='missing' WHERE asset_id=?", (assets[0]["asset_id"],))
                db.commit()
            stats = repository.stats()["maintenance"]
            self.assertEqual(stats["duplicate_assets"], 1)
            self.assertEqual(stats["duplicate_groups"], 1)
            self.assertEqual(stats["parse_errors"], 1)
            self.assertEqual(stats["extraction_errors"], 1)
            self.assertEqual(stats["missing"], 1)
            self.assertEqual(repository.search(issue="duplicate")["total"], 1)
            self.assertEqual(repository.search(issue="parse_error")["total"], 1)
            self.assertEqual(repository.search(issue="extraction_error")["total"], 1)
            self.assertEqual(repository.search(issue="missing")["total"], 1)

    def test_incremental_scan_entry_uses_catalog_source_and_guards_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            expected = {"checked": 783, "unchanged": 783, "integrity_check": "ok"}
            with patch("app.synchronize", return_value=expected) as mocked_sync:
                self.assertEqual(repository.run_incremental_scan(), expected)
                mocked_sync.assert_called_once_with(repository.archive_root, database)
            repository.scan_lock.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    repository.run_incremental_scan()
            finally:
                repository.scan_lock.release()

    def test_export_import_backup_and_health_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            asset = repository.search(limit=1)["items"][0]
            repository.update_asset(asset["asset_id"], "life", ["topic:portable"], True, "read", "可移植备注")
            bundle = repository.export_bundle()
            self.assertEqual(bundle["format"], "web-archive-manager-overrides")
            self.assertIn(asset["asset_id"], bundle["overrides"])
            bundle["overrides"]["unknown-asset"] = bundle["overrides"][asset["asset_id"]]
            result = repository.import_bundle(bundle)
            self.assertGreaterEqual(result["imported"], 1)
            self.assertEqual(result["skipped"], 1)
            restored = ArchiveRepository(database).get_asset(asset["asset_id"])
            self.assertIn("topic:portable", restored["tags"])
            with self.assertRaises(ValueError):
                repository.import_bundle({"format": "other", "version": 1, "overrides": {}})
            health = repository.health_report()
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["integrity_check"], "ok")
            backup = repository.create_backup()
            backup_dir = Path(backup["path"])
            self.assertTrue((backup_dir / "catalog.sqlite").is_file())
            self.assertTrue((backup_dir / "overrides-bundle.json").is_file())

    def test_review_filter(self):
        result = ArchiveRepository(self.database).search(review=True, limit=5)
        self.assertTrue(all(item["classification_source"] == "auto-v2" for item in result["items"]))

    def test_manual_override_survives_repository_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            asset = repository.search(limit=1)["items"][0]
            repository.update_asset(asset["asset_id"], "life", ["topic:test"])
            restored = ArchiveRepository(database).get_asset(asset["asset_id"])
            self.assertEqual(restored["primary_category"], "life")
            self.assertEqual(restored["tags"], ["topic:test"])

    def test_favorite_read_and_note_persist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            asset = repository.search(limit=1)["items"][0]
            repository.update_asset(asset["asset_id"], None, None, True, "read", "稍后整理成知识笔记")
            restored = ArchiveRepository(database).get_asset(asset["asset_id"])
            self.assertEqual(restored["is_favorite"], 1)
            self.assertEqual(restored["read_status"], "read")
            self.assertEqual(restored["personal_note"], "稍后整理成知识笔记")
            favorite_results = ArchiveRepository(database).search(state_filter="favorite", limit=10)
            self.assertTrue(any(item["asset_id"] == asset["asset_id"] for item in favorite_results["items"]))

    def test_title_and_source_override_persist_and_update_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "catalog.sqlite"
            shutil.copy2(self.database, database)
            repository = ArchiveRepository(database)
            asset = repository.search(asset_type="web-html", limit=1)["items"][0]
            title = "自定义归档标题 AlphaMeta"
            source = "https://example.com/archive/item"
            saved = repository.update_asset(asset["asset_id"], None, None, title_clean=title, source_url=source)
            self.assertEqual(saved["title_clean"], title)
            restored = ArchiveRepository(database).get_asset(asset["asset_id"])
            self.assertEqual(restored["title_clean"], title)
            self.assertEqual(restored["source_url"], source)
            self.assertEqual(restored["source_domain"], "example.com")
            self.assertEqual(ArchiveRepository(database).search(query="AlphaMeta")["total"], 1)
            with self.assertRaises(ValueError):
                repository.update_asset(asset["asset_id"], None, None, title_clean="")
            with self.assertRaises(ValueError):
                repository.update_asset(asset["asset_id"], None, None, source_url="file:///tmp/item")

    def test_local_file_is_restricted_to_archive_root(self):
        repository = ArchiveRepository(self.database)
        html_asset = repository.search(asset_type="web-html", limit=1)["items"][0]
        self.assertTrue(repository.get_local_file(html_asset["asset_id"], html_only=True).is_file())
        mhtml_asset = repository.search(asset_type="web-mhtml", limit=1)["items"][0]
        with self.assertRaises(TypeError):
            repository.get_local_file(mhtml_asset["asset_id"], html_only=True)

    def test_preview_headers_and_system_open_endpoint(self):
        repository = ArchiveRepository(self.database)
        html_asset = repository.search(asset_type="web-html", limit=1)["items"][0]
        AppHandler.repository = repository
        server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/preview/{html_asset['asset_id']}") as response:
                self.assertEqual(response.status, 200)
                self.assertIn("sandbox", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                response.read(32)
            with urllib.request.urlopen(f"{base}/api/version") as response:
                version = __import__("json").loads(response.read())
                self.assertEqual(version["app_version"], APP_VERSION)
                self.assertEqual(version["api_version"], API_VERSION)
            request = urllib.request.Request(f"{base}/api/assets/{html_asset['asset_id']}/open", method="POST", data=b"")
            with patch("app.open_local_file") as mocked_open:
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                mocked_open.assert_called_once()
            reveal_request = urllib.request.Request(f"{base}/api/assets/{html_asset['asset_id']}/reveal", method="POST", data=b"")
            with patch("app.reveal_local_file") as mocked_reveal:
                with urllib.request.urlopen(reveal_request) as response:
                    self.assertEqual(response.status, 200)
                mocked_reveal.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ClassifierTest(unittest.TestCase):
    def test_title_signal_classifies_technology(self):
        category, confidence, reasons = score_asset("article-viewed/example.mhtml", "Java线程池问题排查", "网页导航和推荐内容")
        self.assertEqual(category, "technology")
        self.assertGreaterEqual(confidence, 0.67)
        self.assertIn("标题:java", reasons)

    def test_body_noise_does_not_force_category(self):
        category, _, _ = score_asset("article-viewed/example.mhtml", "如何学好英语", "历史 文化 电影 演员 新闻 教育")
        self.assertEqual(category, "uncategorized")


class ConfigurationTest(unittest.TestCase):
    def test_config_resolves_relative_paths_and_rejects_network_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"database":"db/catalog.sqlite","port":8877,"log_file":"run/app.log"}', encoding="utf-8")
            config = load_config(config_path)
            self.assertEqual(config["port"], 8877)
            self.assertEqual(config["database"], str((Path(temp_dir) / "db" / "catalog.sqlite").resolve()))
            self.assertEqual(config["log_file"], str((Path(temp_dir) / "run" / "app.log").resolve()))
            config_path.write_text('{"host":"0.0.0.0"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)
            config_path.write_text('{"unknown":true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)


class IncrementalScanTest(unittest.TestCase):
    def test_add_update_move_and_missing_preserve_identity_and_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "archive"
            output = root / "catalog"
            source.mkdir()
            original = source / "first.html"
            original.write_text("<html><title>Java 入门</title><body>第一版正文</body></html>", encoding="utf-8")
            scan(source, output)
            database = output / "data" / "catalog.sqlite"
            build(database)

            first = synchronize(source, database)
            self.assertEqual(first["unchanged"], 1)
            with sqlite3.connect(database) as db:
                asset_id = db.execute("SELECT asset_id FROM assets").fetchone()[0]
                db.execute("UPDATE assets SET is_favorite=1, read_status='read', personal_note='保留我' WHERE asset_id=?", (asset_id,))
                db.commit()

            original.write_text("<html><title>Java 进阶</title><body>第二版正文，包含线程池。</body></html>", encoding="utf-8")
            updated = synchronize(source, database)
            self.assertEqual(updated["updated"], 1)

            moved_path = source / "technology" / "renamed.html"
            moved_path.parent.mkdir()
            original.rename(moved_path)
            moved = synchronize(source, database)
            self.assertEqual(moved["moved"], 1)
            with sqlite3.connect(database) as db:
                row = db.execute("SELECT asset_id,is_favorite,read_status,personal_note,relative_path FROM assets").fetchone()
            self.assertEqual(row, (asset_id, 1, "read", "保留我", "technology/renamed.html"))

            moved_path.unlink()
            missing = synchronize(source, database)
            self.assertEqual(missing["newly_missing"], 1)
            repository = ArchiveRepository(database)
            self.assertEqual(repository.stats()["total"], 0)
            self.assertEqual(repository.stats()["missing"], 1)


if __name__ == "__main__":
    unittest.main()
