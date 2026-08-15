import unittest
import tempfile
import shutil
from pathlib import Path

from app import ArchiveRepository, CATEGORIES


class RepositoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = Path(__file__).resolve().parents[1] / "data" / "catalog.sqlite"
        if not cls.database.exists():
            raise unittest.SkipTest("catalog.sqlite not found")

    def test_stats_match_catalog(self):
        stats = ArchiveRepository(self.database).stats()
        self.assertEqual(stats["total"], 657)
        self.assertEqual(stats["ignored"], 126)
        self.assertEqual(stats["indexed"], 553)
        self.assertTrue(set(stats["categories"]).issubset(CATEGORIES))

    def test_chinese_full_text_search(self):
        result = ArchiveRepository(self.database).search(query="离线安装", limit=10)
        self.assertGreaterEqual(result["total"], 2)
        self.assertTrue(any("离线安装" in (item["title_clean"] or "") or "<mark>" in item["excerpt"] for item in result["items"]))

    def test_filter_category(self):
        result = ArchiveRepository(self.database).search(category="technology", limit=5)
        self.assertGreater(result["total"], 0)
        self.assertTrue(all(item["primary_category"] == "technology" for item in result["items"]))

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


if __name__ == "__main__":
    unittest.main()
