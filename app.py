#!/usr/bin/env python3
"""Local web UI for browsing and classifying archived webpages."""

import argparse
import json
import mimetypes
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "web"
DEFAULT_DB = PROJECT_ROOT / "data" / "catalog.sqlite"
CATEGORIES = ["technology", "ai", "career-work", "finance-business", "life", "society-culture", "productivity-tools", "uncategorized"]
API_VERSION = 3


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_query(value: str) -> str:
    return '"' + value.replace('"', '""').strip() + '"'


class ArchiveRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.override_path = database.parent / "user-overrides.json"
        self.override_lock = threading.Lock()
        self.archive_root = self.read_archive_root()
        self.ensure_classification_columns()
        self.apply_overrides()

    def read_archive_root(self) -> Path:
        with connect(self.database) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='source_path'").fetchone()
        if not row:
            raise ValueError("catalog metadata does not contain source_path")
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
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
            db.commit()

    def read_overrides(self) -> Dict[str, object]:
        if not self.override_path.is_file():
            return {}
        try:
            return json.loads(self.override_path.read_text(encoding="utf-8"))
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
                if category in CATEGORIES and read_status in {"read", "unread"}:
                    db.execute("""
                        UPDATE assets SET primary_category=?, tags_json=?, classification_source='manual',
                        classification_confidence=1.0, classification_reason='用户手工确认',
                        is_favorite=?, read_status=?, personal_note=? WHERE asset_id=?
                    """, (category, json.dumps(tags, ensure_ascii=False), favorite, read_status, note, asset_id))
            db.commit()

    def write_override(self, asset_id: str, value: Dict[str, object]) -> None:
        with self.override_lock:
            overrides = self.read_overrides()
            overrides[asset_id] = value
            temp = self.override_path.with_suffix(".tmp")
            temp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.override_path)

    def stats(self) -> Dict[str, object]:
        with connect(self.database) as db:
            total = db.execute("SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store'").fetchone()[0]
            ignored = db.execute("SELECT COUNT(*) FROM assets WHERE file_name = '.DS_Store'").fetchone()[0]
            indexed = db.execute("SELECT COUNT(*) FROM contents WHERE extraction_status='success'").fetchone()[0]
            categories = {row[0]: row[1] for row in db.execute("SELECT primary_category, COUNT(*) FROM assets WHERE file_name != '.DS_Store' GROUP BY primary_category")}
            types = {row[0]: row[1] for row in db.execute("SELECT asset_type, COUNT(*) FROM assets WHERE file_name != '.DS_Store' GROUP BY asset_type")}
            review = db.execute("SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND classification_source='auto-v2'").fetchone()[0]
            favorites = db.execute("SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND is_favorite=1").fetchone()[0]
            read = db.execute("SELECT COUNT(*) FROM assets WHERE file_name != '.DS_Store' AND read_status='read'").fetchone()[0]
        return {"api_version": API_VERSION, "total": total, "ignored": ignored, "indexed": indexed, "review": review, "favorites": favorites, "read": read, "categories": categories, "types": types}

    def search(self, query: str = "", category: str = "", asset_type: str = "", state_filter: str = "", review: bool = False, page: int = 1, limit: int = 30) -> Dict[str, object]:
        page, limit = max(page, 1), max(1, min(limit, 100))
        params: List[object] = []
        filters = ["a.file_name != '.DS_Store'"]
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
        if review:
            filters.append("a.classification_source = 'auto-v2'")
        if state_filter == "favorite":
            filters.append("a.is_favorite = 1")
        elif state_filter in {"read", "unread"}:
            filters.append("a.read_status = ?")
            params.append(state_filter)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with connect(self.database) as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source}{where}", params).fetchone()[0]
            rows = db.execute(f"""
                SELECT a.asset_id, a.title_clean, a.primary_category, a.asset_type,
                       a.source_domain, a.source_url, a.saved_at, a.original_path,
                       a.size_bytes, a.tags_json, a.classification_source,
                       a.classification_confidence, a.classification_reason,
                       a.is_favorite, a.read_status, a.personal_note,
                       {snippet} AS excerpt, {rank} AS rank
                FROM {source}{where}
                ORDER BY rank, a.saved_at DESC LIMIT ? OFFSET ?
            """, params + [limit, (page - 1) * limit]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            item.pop("rank", None)
            items.append(item)
        return {"items": items, "total": total, "page": page, "limit": limit, "pages": max(1, (total + limit - 1) // limit)}

    def update_asset(self, asset_id: str, category: Optional[str], tags: Optional[List[str]], is_favorite: Optional[bool] = None, read_status: Optional[str] = None, personal_note: Optional[str] = None) -> Dict[str, object]:
        with connect(self.database) as db:
            current = db.execute("SELECT primary_category,tags_json,is_favorite,read_status,personal_note FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            if not current:
                raise KeyError(asset_id)
            category = category or current[0]
            if category not in CATEGORIES:
                raise ValueError("unknown category")
            normalized_tags = sorted({tag.strip() for tag in (tags if tags is not None else json.loads(current[1])) if tag.strip()})
            favorite_value = int(bool(is_favorite)) if is_favorite is not None else current[2]
            read_value = read_status if read_status is not None else current[3]
            note_value = personal_note if personal_note is not None else current[4]
            if read_value not in {"read", "unread"}:
                raise ValueError("unknown read status")
            db.execute("""
                UPDATE assets SET primary_category=?, tags_json=?, classification_source='manual',
                classification_confidence=1.0, classification_reason='用户手工确认',
                is_favorite=?, read_status=?, personal_note=? WHERE asset_id=?
            """, (category, json.dumps(normalized_tags, ensure_ascii=False), favorite_value, read_value, note_value, asset_id))
            db.commit()
        override = {"primary_category": category, "tags": normalized_tags, "is_favorite": bool(favorite_value), "read_status": read_value, "personal_note": note_value}
        self.write_override(asset_id, override)
        return {"asset_id": asset_id, **override}

    def get_asset(self, asset_id: str) -> Dict[str, object]:
        with connect(self.database) as db:
            row = db.execute("SELECT a.*, COALESCE(c.body_text,'') AS body_text FROM assets a LEFT JOIN contents c USING(asset_id) WHERE a.asset_id=?", (asset_id,)).fetchone()
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
            raise PermissionError("asset path is outside archive root")
        if not path.is_file():
            raise FileNotFoundError(path)
        if html_only and row[1].lower() not in {"html", "htm"}:
            raise TypeError("only HTML files support safe preview")
        return path


def open_local_file(path: Path) -> None:
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AppHandler(BaseHTTPRequestHandler):
    repository: ArchiveRepository

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/stats":
            return self.json_response(self.repository.stats())
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            try:
                return self.json_response(self.repository.search(
                    query=params.get("q", [""])[0], category=params.get("category", [""])[0],
                    asset_type=params.get("type", [""])[0], review=params.get("review", ["0"])[0] == "1",
                    state_filter=params.get("state", [""])[0],
                    page=int(params.get("page", ["1"])[0]),
                    limit=int(params.get("limit", ["30"])[0])))
            except (ValueError, sqlite3.Error) as exc:
                return self.error_response(400, str(exc))
        if parsed.path.startswith("/api/assets/"):
            try:
                return self.json_response(self.repository.get_asset(parsed.path.rsplit("/", 1)[-1]))
            except KeyError:
                return self.error_response(404, "asset not found")
        if parsed.path.startswith("/preview/"):
            return self.serve_archive_preview(parsed.path.rsplit("/", 1)[-1])
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/assets/") or not parsed.path.endswith("/open"):
            return self.error_response(404, "not found")
        asset_id = parsed.path.split("/")[-2]
        try:
            path = self.repository.get_local_file(asset_id)
            open_local_file(path)
            self.json_response({"opened": True, "asset_id": asset_id})
        except KeyError:
            self.error_response(404, "asset not found")
        except FileNotFoundError:
            self.error_response(410, "local file no longer exists")
        except PermissionError as exc:
            self.error_response(403, str(exc))
        except OSError as exc:
            self.error_response(500, f"failed to open local file: {exc}")

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/assets/"):
            return self.error_response(404, "not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = self.repository.update_asset(
                parsed.path.rsplit("/", 1)[-1], payload.get("primary_category"), payload.get("tags"),
                payload.get("is_favorite"), payload.get("read_status"), payload.get("personal_note"),
            )
            self.json_response(result)
        except KeyError:
            self.error_response(404, "asset not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_response(400, str(exc))

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
            return self.error_response(404, "not found")
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
            return self.error_response(404, "asset not found")
        except FileNotFoundError:
            return self.error_response(410, "local file no longer exists")
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
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit(f"database not found: {args.database}")
    AppHandler.repository = ArchiveRepository(args.database.resolve())
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Web Archive Manager: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
