#!/usr/bin/env python3
"""Local web UI for browsing and classifying archived webpages."""

import argparse
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from scripts.incremental_scan import synchronize


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "web"
DEFAULT_DB = PROJECT_ROOT / "data" / "catalog.sqlite"
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
APP_VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
CATEGORIES = ["technology", "ai", "career-work", "finance-business", "life", "society-culture", "productivity-tools", "uncategorized"]
API_VERSION = 10
DATA_BUNDLE_VERSION = 1
SORT_OPTIONS = {
    "saved_desc": "a.saved_at DESC, a.asset_id",
    "saved_asc": "a.saved_at ASC, a.asset_id",
    "modified_desc": "a.modified_at DESC, a.asset_id",
    "size_desc": "a.size_bytes DESC, a.asset_id",
}


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_query(value: str) -> str:
    return '"' + value.replace('"', '""').strip() + '"'


def load_config(path: Path) -> Dict[str, object]:
    defaults: Dict[str, object] = {
        "database": str(DEFAULT_DB), "host": "127.0.0.1", "port": 8765,
        "log_file": str(PROJECT_ROOT / "logs" / "app.log"),
    }
    if not path.is_file():
        return defaults
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是有效的 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象")
    unknown = set(value) - set(defaults)
    if unknown:
        raise ValueError(f"配置文件包含未知字段：{', '.join(sorted(unknown))}")
    config = {**defaults, **value}
    base = path.resolve().parent
    for key in ("database", "log_file"):
        candidate = Path(str(config[key])).expanduser()
        config[key] = str(candidate if candidate.is_absolute() else (base / candidate).resolve())
    if config["host"] not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("监听地址必须是本机回环地址")
    if not isinstance(config["port"], int) or not 1 <= config["port"] <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    return config


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("web_archive_manager")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating = RotatingFileHandler(str(log_file), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    rotating.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(rotating)
    return logger


class ArchiveRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.override_path = database.parent / "user-overrides.json"
        self.override_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.archive_root = self.read_archive_root()
        self.ensure_classification_columns()
        self.apply_overrides()

    def read_archive_root(self) -> Path:
        with connect(self.database) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='source_path'").fetchone()
        if not row:
            raise ValueError("索引元数据中缺少 source_path")
        return Path(row[0]).resolve()

    def ensure_classification_columns(self) -> None:
        with connect(self.database) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
            additions = {
                "classification_source": "TEXT NOT NULL DEFAULT 'legacy'",
                "classification_confidence": "REAL",
                "classification_reason": "TEXT",
                "is_favorite": "INTEGER NOT NULL DEFAULT 0",
                "read_status": "TEXT NOT NULL DEFAULT 'unread'",
                "personal_note": "TEXT NOT NULL DEFAULT ''",
                "file_status": "TEXT NOT NULL DEFAULT 'active'",
                "file_mtime_ns": "INTEGER",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
            db.commit()

    def read_overrides(self) -> Dict[str, object]:
        if not self.override_path.is_file():
            return {}
        try:
            value = json.loads(self.override_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def apply_overrides(self) -> None:
        overrides = self.read_overrides()
        if not overrides:
            return
        with connect(self.database) as db:
            for asset_id, value in overrides.items():
                category = value.get("primary_category")
                tags = value.get("tags", [])
                favorite = int(bool(value.get("is_favorite", False)))
                read_status = value.get("read_status", "unread")
                note = value.get("personal_note", "")
                title = value.get("title_clean")
                source_url = value.get("source_url")
                source_domain = urlparse(source_url).hostname if source_url else None
                has_title = "title_clean" in value
                has_source = "source_url" in value
                if category in CATEGORIES and read_status in {"read", "unread"}:
                    db.execute("""
                        UPDATE assets SET primary_category=?, tags_json=?, classification_source='manual',
                        classification_confidence=1.0, classification_reason='用户手工确认',
                        is_favorite=?, read_status=?, personal_note=?,
                        title_clean=CASE WHEN ? THEN ? ELSE title_clean END,
                        source_url=CASE WHEN ? THEN ? ELSE source_url END,
                        source_domain=CASE WHEN ? THEN ? ELSE source_domain END WHERE asset_id=?
                    """, (category, json.dumps(tags, ensure_ascii=False), favorite, read_status, note, has_title, title, has_source, source_url, has_source, source_domain, asset_id))
                    if has_title and title:
                        db.execute("UPDATE contents_fts SET title=? WHERE asset_id=?", (title, asset_id))
            db.commit()

    def write_override(self, asset_id: str, value: Dict[str, object]) -> None:
        with self.override_lock:
            overrides = self.read_overrides()
            overrides[asset_id] = value
            temp = self.override_path.with_suffix(".tmp")
            temp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.override_path)

    def export_bundle(self) -> Dict[str, object]:
        return {
            "format": "web-archive-manager-overrides",
            "version": DATA_BUNDLE_VERSION,
            "exported_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
            "overrides": self.read_overrides(),
        }

    def import_bundle(self, bundle: object) -> Dict[str, int]:
        if not isinstance(bundle, dict) or bundle.get("format") != "web-archive-manager-overrides":
            raise ValueError("不支持的人工数据包格式")
        if bundle.get("version") != DATA_BUNDLE_VERSION or not isinstance(bundle.get("overrides"), dict):
            raise ValueError("不支持的人工数据包版本")
        with connect(self.database) as db:
            known_ids = {row[0] for row in db.execute("SELECT asset_id FROM assets")}
        validated: Dict[str, object] = {}
        skipped = 0
        for asset_id, value in bundle["overrides"].items():
            if asset_id not in known_ids or not isinstance(value, dict):
                skipped += 1
                continue
            category = value.get("primary_category")
            tags = value.get("tags", [])
            read_status = value.get("read_status", "unread")
            title = value.get("title_clean")
            source_url = value.get("source_url")
            personal_note = value.get("personal_note", "")
            if category not in CATEGORIES or read_status not in {"read", "unread"}:
                skipped += 1
                continue
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                skipped += 1
                continue
            if not isinstance(personal_note, str):
                skipped += 1
                continue
            if title is not None and (not isinstance(title, str) or not title.strip() or len(title.strip()) > 1000):
                skipped += 1
                continue
            if source_url is not None and (not isinstance(source_url, str) or urlparse(source_url).scheme not in {"http", "https"}):
                skipped += 1
                continue
            validated[asset_id] = {
                "primary_category": category,
                "tags": sorted({tag.strip() for tag in tags if tag.strip()}),
                "is_favorite": bool(value.get("is_favorite", False)),
                "read_status": read_status,
                "personal_note": personal_note,
                **({"title_clean": title.strip()} if title is not None else {}),
                **({"source_url": source_url} if "source_url" in value else {}),
            }
        with self.override_lock:
            merged = self.read_overrides()
            merged.update(validated)
            temp = self.override_path.with_suffix(".tmp")
            temp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.override_path)
        self.apply_overrides()
        return {"imported": len(validated), "skipped": skipped, "total": len(merged)}

    def create_backup(self) -> Dict[str, object]:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup_dir = self.database.parent / "backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        database_backup = backup_dir / "catalog.sqlite"
        with connect(self.database) as source, sqlite3.connect(str(database_backup)) as target:
            source.backup(target)
        bundle_path = backup_dir / "overrides-bundle.json"
        bundle_path.write_text(json.dumps(self.export_bundle(), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"backup_id": stamp, "path": str(backup_dir), "database_bytes": database_backup.stat().st_size, "overrides": len(self.read_overrides())}

    def health_report(self) -> Dict[str, object]:
        with connect(self.database) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = len(db.execute("PRAGMA foreign_key_check").fetchall())
            assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            indexed = db.execute("SELECT COUNT(*) FROM contents_fts").fetchone()[0]
            orphan_contents = db.execute("SELECT COUNT(*) FROM contents c LEFT JOIN assets a USING(asset_id) WHERE a.asset_id IS NULL").fetchone()[0]
        stats = self.stats()
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
            "status": "healthy" if integrity == "ok" and foreign_key_errors == 0 and orphan_contents == 0 else "attention",
            "integrity_check": integrity, "foreign_key_errors": foreign_key_errors,
            "assets": assets, "fts_documents": indexed, "orphan_contents": orphan_contents,
            "maintenance": stats["maintenance"], "overrides": len(self.read_overrides()),
        }

    def stats(self) -> Dict[str, object]:
        with connect(self.database) as db:
            active = "file_status='active'"
            total = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active}").fetchone()[0]
            ignored = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name = '.DS_Store' AND {active}").fetchone()[0]
            missing = db.execute("SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND file_status='missing'").fetchone()[0]
            indexed = db.execute("SELECT COUNT(*) FROM contents c JOIN assets a USING(asset_id) WHERE c.extraction_status='success' AND a.file_status='active'").fetchone()[0]
            categories = {row[0]: row[1] for row in db.execute(f"SELECT primary_category, COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} GROUP BY primary_category")}
            types = {row[0]: row[1] for row in db.execute(f"SELECT asset_type, COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} GROUP BY asset_type")}
            review = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} AND classification_source='auto-v2'").fetchone()[0]
            favorites = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} AND is_favorite=1").fetchone()[0]
            read = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} AND read_status='read'").fetchone()[0]
            domains = [{"domain": row[0], "count": row[1]} for row in db.execute(f"""
                SELECT source_domain, COUNT(*) FROM assets
                WHERE file_name != '.DS_Store' AND {active} AND source_domain IS NOT NULL AND source_domain != ''
                GROUP BY source_domain ORDER BY COUNT(*) DESC, source_domain LIMIT 30
            """)]
            duplicate_assets = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} AND duplicate_group IS NOT NULL").fetchone()[0]
            duplicate_groups = db.execute(f"SELECT COUNT(DISTINCT duplicate_group) FROM assets WHERE file_name != '.DS_Store' AND {active} AND duplicate_group IS NOT NULL").fetchone()[0]
            parse_errors = db.execute(f"SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND {active} AND parse_status='error'").fetchone()[0]
            extraction_errors = db.execute(f"SELECT COUNT(*) FROM contents c JOIN assets a USING(asset_id) WHERE a.file_name != '.DS_Store' AND {active} AND c.extraction_status='error'").fetchone()[0]
            tags = [{"tag": row[0], "count": row[1]} for row in db.execute(f"""
                SELECT tag.value, COUNT(*) FROM assets a, json_each(a.tags_json) tag
                WHERE a.file_name != '.DS_Store' AND {active} AND tag.value NOT LIKE 'format:%'
                GROUP BY tag.value ORDER BY COUNT(*) DESC, tag.value LIMIT 20
            """)]
        maintenance = {"duplicate_assets": duplicate_assets, "duplicate_groups": duplicate_groups, "parse_errors": parse_errors, "extraction_errors": extraction_errors, "missing": missing}
        return {"api_version": API_VERSION, "app_version": APP_VERSION, "total": total, "ignored": ignored, "missing": missing, "indexed": indexed, "review": review, "favorites": favorites, "read": read, "categories": categories, "types": types, "domains": domains, "tags": tags, "maintenance": maintenance}

    def search(self, query: str = "", category: str = "", asset_type: str = "", state_filter: str = "", review: bool = False, page: int = 1, limit: int = 30, domain: str = "", date_from: str = "", date_to: str = "", sort: str = "saved_desc", issue: str = "", tag: str = "") -> Dict[str, object]:
        page, limit = max(page, 1), max(1, min(limit, 100))
        if sort not in SORT_OPTIONS and sort != "relevance":
            raise ValueError("未知的排序方式")
        if issue not in {"", "duplicate", "parse_error", "extraction_error", "missing"}:
            raise ValueError("未知的维护筛选条件")
        for value in (date_from, date_to):
            if value:
                try:
                    __import__("datetime").date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
        if date_from and date_to and date_from > date_to:
            raise ValueError("开始日期不能晚于结束日期")
        params: List[object] = []
        filters = ["a.file_name != '.DS_Store'", "a.file_status = 'missing'" if issue == "missing" else "a.file_status = 'active'"]
        if query.strip():
            source = "contents_fts JOIN assets a USING(asset_id)"
            filters.append("contents_fts MATCH ?")
            params.append(normalize_query(query))
            rank = "bm25(contents_fts, 0.0, 4.0, 1.0)"
            snippet = "snippet(contents_fts, 2, '<mark>', '</mark>', '…', 30)"
        else:
            source = "assets a LEFT JOIN contents c USING(asset_id)"
            rank = "0"
            snippet = "substr(COALESCE(c.body_text,''),1,260)"
        if category:
            filters.append("a.primary_category = ?")
            params.append(category)
        if asset_type:
            filters.append("a.asset_type = ?")
            params.append(asset_type)
        if domain:
            filters.append("a.source_domain = ?")
            params.append(domain)
        if tag:
            filters.append("EXISTS (SELECT 1 FROM json_each(a.tags_json) selected_tag WHERE selected_tag.value = ?)")
            params.append(tag)
        if date_from:
            filters.append("substr(a.saved_at,1,10) >= ?")
            params.append(date_from)
        if date_to:
            filters.append("substr(a.saved_at,1,10) <= ?")
            params.append(date_to)
        if review:
            filters.append("a.classification_source = 'auto-v2'")
        if issue == "duplicate":
            filters.append("a.duplicate_group IS NOT NULL")
        elif issue == "parse_error":
            filters.append("a.parse_status = 'error'")
        elif issue == "extraction_error":
            filters.append("EXISTS (SELECT 1 FROM contents issue_content WHERE issue_content.asset_id=a.asset_id AND issue_content.extraction_status='error')")
        if state_filter == "favorite":
            filters.append("a.is_favorite = 1")
        elif state_filter in {"read", "unread"}:
            filters.append("a.read_status = ?")
            params.append(state_filter)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        order_by = rank if query.strip() and sort == "relevance" else SORT_OPTIONS.get(sort, SORT_OPTIONS["saved_desc"])
        with connect(self.database) as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
            rows = db.execute(f"""
                SELECT a.asset_id, a.title_clean, a.primary_category, a.asset_type,
                       a.source_domain, a.source_url, a.saved_at, a.original_path,
                       a.size_bytes, a.tags_json, a.classification_source,
                       a.classification_confidence, a.classification_reason,
                       a.is_favorite, a.read_status, a.personal_note,
                       a.file_status, a.duplicate_group, a.parse_status,
                       a.error_message, (SELECT extraction_status FROM contents issue_status WHERE issue_status.asset_id=a.asset_id) AS extraction_status,
                       (SELECT error_message FROM contents issue_status WHERE issue_status.asset_id=a.asset_id) AS extraction_error,
                       {snippet} AS excerpt, {rank} AS rank
                FROM {source}{where}
                ORDER BY {order_by} LIMIT ? OFFSET ?
            """, params + [limit, (page - 1) * limit]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            item.pop("rank", None)
            items.append(item)
        return {"items": items, "total": total, "page": page, "limit": limit, "pages": max(1, (total + limit - 1) // limit)}

    def run_incremental_scan(self) -> Dict[str, object]:
        if not self.scan_lock.acquire(blocking=False):
            raise RuntimeError("增量同步正在运行，请稍后再试")
        try:
            result = synchronize(self.archive_root, self.database)
            self.apply_overrides()
            return result
        finally:
            self.scan_lock.release()

    def update_asset(self, asset_id: str, category: Optional[str], tags: Optional[List[str]], is_favorite: Optional[bool] = None, read_status: Optional[str] = None, personal_note: Optional[str] = None, title_clean: Optional[str] = None, source_url: Optional[str] = None) -> Dict[str, object]:
        with connect(self.database) as db:
            current = db.execute("SELECT primary_category,tags_json,is_favorite,read_status,personal_note,title_clean,source_url FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            if not current:
                raise KeyError(asset_id)
            category = category or current[0]
            if category not in CATEGORIES:
                raise ValueError("未知的主分类")
            normalized_tags = sorted({tag.strip() for tag in (tags if tags is not None else json.loads(current[1])) if tag.strip()})
            favorite_value = int(bool(is_favorite)) if is_favorite is not None else current[2]
            read_value = read_status if read_status is not None else current[3]
            note_value = personal_note if personal_note is not None else current[4]
            title_value = (title_clean if title_clean is not None else current[5] or "").strip()
            source_value = (source_url if source_url is not None else current[6] or "").strip() or None
            if read_value not in {"read", "unread"}:
                raise ValueError("未知的阅读状态")
            if not title_value or len(title_value) > 1000:
                raise ValueError("标题长度必须为 1 到 1000 个字符")
            if source_value and urlparse(source_value).scheme not in {"http", "https"}:
                raise ValueError("原始网址必须使用 http 或 https")
            source_domain = urlparse(source_value).hostname if source_value else None
            db.execute("""
                UPDATE assets SET primary_category=?, tags_json=?, classification_source='manual',
                classification_confidence=1.0, classification_reason='用户手工确认',
                is_favorite=?, read_status=?, personal_note=?, title_clean=?, source_url=?, source_domain=? WHERE asset_id=?
            """, (category, json.dumps(normalized_tags, ensure_ascii=False), favorite_value, read_value, note_value, title_value, source_value, source_domain, asset_id))
            db.execute("UPDATE contents_fts SET title=? WHERE asset_id=?", (title_value, asset_id))
            db.commit()
        override = {"primary_category": category, "tags": normalized_tags, "is_favorite": bool(favorite_value), "read_status": read_value, "personal_note": note_value, "title_clean": title_value, "source_url": source_value}
        self.write_override(asset_id, override)
        return {"asset_id": asset_id, **override}

    def get_asset(self, asset_id: str) -> Dict[str, object]:
        with connect(self.database) as db:
            row = db.execute("""
                SELECT a.*, COALESCE(c.body_text,'') AS body_text,
                       c.extraction_status, c.error_message AS extraction_error
                FROM assets a LEFT JOIN contents c USING(asset_id) WHERE a.asset_id=?
            """, (asset_id,)).fetchone()
        if not row:
            raise KeyError(asset_id)
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
        return result

    def get_local_file(self, asset_id: str, html_only: bool = False) -> Path:
        with connect(self.database) as db:
            row = db.execute("SELECT original_path, extension FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if not row:
            raise KeyError(asset_id)
        path = Path(row[0]).resolve()
        if self.archive_root != path and self.archive_root not in path.parents:
            raise PermissionError("档案路径不在归档根目录内")
        if not path.is_file():
            raise FileNotFoundError(path)
        if html_only and row[1].lower() not in {"html", "htm"}:
            raise TypeError("仅 HTML 文件支持安全预览")
        return path


def open_local_file(path: Path) -> None:
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_local_file(path: Path) -> None:
    subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AppHandler(BaseHTTPRequestHandler):
    repository: ArchiveRepository
    logger = logging.getLogger("web_archive_manager")

    def log_message(self, format: str, *args: object) -> None:
        self.logger.info("%s - %s", self.address_string(), format % args)

    def json_response(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, status: int, message: str) -> None:
        self.json_response({"error": message}, status)

    def download_json(self, value: object, filename: str) -> None:
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/stats":
            return self.json_response(self.repository.stats())
        if parsed.path == "/api/version":
            return self.json_response({"app_version": APP_VERSION, "api_version": API_VERSION, "sqlite_version": sqlite3.sqlite_version})
        if parsed.path == "/api/data/export":
            return self.download_json(self.repository.export_bundle(), "web-archive-overrides.json")
        if parsed.path == "/api/health":
            return self.json_response(self.repository.health_report())
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            try:
                return self.json_response(self.repository.search(
                    query=params.get("q", [""])[0], category=params.get("category", [""])[0],
                    asset_type=params.get("type", [""])[0], review=params.get("review", ["0"])[0] == "1",
                    state_filter=params.get("state", [""])[0],
                    domain=params.get("domain", [""])[0], date_from=params.get("from", [""])[0],
                    date_to=params.get("to", [""])[0], sort=params.get("sort", ["saved_desc"])[0],
                    issue=params.get("issue", [""])[0],
                    tag=params.get("tag", [""])[0],
                    page=int(params.get("page", ["1"])[0]),
                    limit=int(params.get("limit", ["30"])[0])))
            except (ValueError, sqlite3.Error) as exc:
                return self.error_response(400, str(exc))
        if parsed.path.startswith("/api/assets/"):
            try:
                return self.json_response(self.repository.get_asset(parsed.path.rsplit("/", 1)[-1]))
            except KeyError:
                return self.error_response(404, "未找到该档案")
        if parsed.path.startswith("/preview/"):
            return self.serve_archive_preview(parsed.path.rsplit("/", 1)[-1])
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/maintenance/scan":
            try:
                return self.json_response(self.repository.run_incremental_scan())
            except RuntimeError as exc:
                return self.error_response(409, str(exc))
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.error_response(500, str(exc))
        if parsed.path == "/api/data/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 10 * 1024 * 1024:
                    raise ValueError("导入文件大小必须在 1 字节到 10 MiB 之间")
                bundle = json.loads(self.rfile.read(length))
                return self.json_response(self.repository.import_bundle(bundle))
            except (ValueError, json.JSONDecodeError) as exc:
                return self.error_response(400, str(exc))
        if parsed.path == "/api/data/backup":
            try:
                return self.json_response(self.repository.create_backup())
            except (OSError, sqlite3.Error) as exc:
                return self.error_response(500, str(exc))
        if parsed.path.startswith("/api/assets/") and parsed.path.endswith("/reveal"):
            asset_id = parsed.path.split("/")[-2]
            try:
                path = self.repository.get_local_file(asset_id)
                reveal_local_file(path)
                return self.json_response({"revealed": True, "asset_id": asset_id})
            except KeyError:
                return self.error_response(404, "未找到该档案")
            except FileNotFoundError:
                return self.error_response(410, "本地原文件已不存在")
            except PermissionError as exc:
                return self.error_response(403, str(exc))
        if not parsed.path.startswith("/api/assets/") or not parsed.path.endswith("/open"):
            return self.error_response(404, "未找到请求的资源")
        asset_id = parsed.path.split("/")[-2]
        try:
            path = self.repository.get_local_file(asset_id)
            open_local_file(path)
            self.json_response({"opened": True, "asset_id": asset_id})
        except KeyError:
            self.error_response(404, "未找到该档案")
        except FileNotFoundError:
            self.error_response(410, "本地原文件已不存在")
        except PermissionError as exc:
            self.error_response(403, str(exc))
        except OSError as exc:
            self.error_response(500, f"打开本地文件失败：{exc}")

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/assets/"):
            return self.error_response(404, "未找到请求的资源")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = self.repository.update_asset(
                parsed.path.rsplit("/", 1)[-1], payload.get("primary_category"), payload.get("tags"),
                payload.get("is_favorite"), payload.get("read_status"), payload.get("personal_note"),
                payload.get("title_clean"), payload.get("source_url"),
            )
            self.json_response(result)
        except KeyError:
            self.error_response(404, "未找到该档案")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_response(400, str(exc))

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
            return self.error_response(404, "未找到请求的资源")
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_archive_preview(self, asset_id: str) -> None:
        try:
            path = self.repository.get_local_file(asset_id, html_only=True)
        except KeyError:
            return self.error_response(404, "未找到该档案")
        except FileNotFoundError:
            return self.error_response(410, "本地原文件已不存在")
        except PermissionError as exc:
            return self.error_response(403, str(exc))
        except TypeError as exc:
            return self.error_response(415, str(exc))
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline' data:; font-src data:; media-src data: blob:")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--check", action="store_true", help="run startup health check and exit")
    args = parser.parse_args()
    try:
        config = load_config(args.config.resolve())
    except ValueError as exc:
        raise SystemExit(f"配置错误：{exc}")
    database = (args.database or Path(str(config["database"]))).resolve()
    host = args.host or str(config["host"])
    port = args.port if args.port is not None else int(config["port"])
    log_file = (args.log_file or Path(str(config["log_file"]))).resolve()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("监听地址必须是本机回环地址")
    if not 1 <= port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间")
    if not database.is_file():
        raise SystemExit(f"未找到索引数据库：{database}")
    logger = setup_logging(log_file)
    AppHandler.logger = logger
    AppHandler.repository = ArchiveRepository(database)
    health = AppHandler.repository.health_report()
    logger.info("startup health check: status=%s integrity=%s assets=%s fts=%s", health["status"], health["integrity_check"], health["assets"], health["fts_documents"])
    if health["status"] != "healthy":
        logger.error("startup health check needs attention: %s", json.dumps(health, ensure_ascii=False))
        if not args.check:
            raise SystemExit("启动健康检查未通过；请运行 python3 app.py --check 查看详情")
    if args.check:
        print(json.dumps({"app_version": APP_VERSION, **health}, ensure_ascii=False, indent=2))
        return 0 if health["status"] == "healthy" else 1
    server = ThreadingHTTPServer((host, port), AppHandler)
    logger.info("Web Archive Manager %s: http://%s:%s", APP_VERSION, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("Web Archive Manager stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
