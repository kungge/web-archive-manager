import unittest
import tempfile
import shutil
from pathlib import Path

from app import ArchiveRepository, CATEGORIES, API_VERSION
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
