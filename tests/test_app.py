import unittest
import tempfile
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app import ArchiveRepository, AppHandler, CATEGORIES, API_VERSION, ThreadingHTTPServer
from scripts.classify_catalog import score_asset


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
            request = urllib.request.Request(f"{base}/api/assets/{html_asset['asset_id']}/open", method="POST", data=b"")
            with patch("app.open_local_file") as mocked_open:
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                mocked_open.assert_called_once()
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


if __name__ == "__main__":
    unittest.main()
