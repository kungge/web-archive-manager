#!/usr/bin/env python3
"""在临时目录中恢复并验证本地快照，不改动当前数据库。"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import ArchiveRepository  # noqa: E402


def verify_backup(backup_dir: Path, current_database: Optional[Path] = None) -> Dict[str, object]:
    backup_dir = backup_dir.resolve()
    source_database = backup_dir / "catalog.sqlite"
    if not source_database.is_file():
        raise ValueError(f"快照中缺少数据库：{source_database}")

    bundle_path = backup_dir / "overrides-bundle.json"
    legacy_bundle_path = backup_dir / "user-overrides.json"
    with tempfile.TemporaryDirectory(prefix="web-archive-restore-") as temp_dir:
        restored_database = Path(temp_dir) / "catalog.sqlite"
        shutil.copy2(source_database, restored_database)
        repository = ArchiveRepository(restored_database)

        bundle_imported = False
        if bundle_path.is_file():
            repository.import_bundle(json.loads(bundle_path.read_text(encoding="utf-8")))
            bundle_imported = True
        elif legacy_bundle_path.is_file():
            legacy = json.loads(legacy_bundle_path.read_text(encoding="utf-8"))
            if isinstance(legacy, dict) and legacy.get("format") == "web-archive-manager-overrides":
                repository.import_bundle(legacy)
            elif isinstance(legacy, dict):
                repository.import_bundle({
                    "format": "web-archive-manager-overrides",
                    "version": 1,
                    "overrides": legacy,
                })
            else:
                raise ValueError("旧版人工数据文件不是 JSON 对象")
            bundle_imported = True

        health = repository.health_report()
        result: Dict[str, object] = {
            "status": health["status"],
            "backup": str(backup_dir),
            "temporary_restore": True,
            "bundle_imported": bundle_imported,
            "health": health,
        }
        if current_database is not None:
            current = ArchiveRepository(current_database.resolve()).health_report()
            matches = health["assets"] == current["assets"] and health["fts_documents"] == current["fts_documents"]
            result["comparison"] = {
                "matches": matches,
                "current_assets": current["assets"],
                "restored_assets": health["assets"],
                "current_fts_documents": current["fts_documents"],
                "restored_fts_documents": health["fts_documents"],
            }
            if not matches:
                result["status"] = "attention"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path, help="data/backups 下的快照目录")
    parser.add_argument("--current", type=Path, help="可选：与当前数据库比较资产和全文索引数量")
    args = parser.parse_args()
    try:
        result = verify_backup(args.backup, args.current)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
