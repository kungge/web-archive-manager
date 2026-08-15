#!/usr/bin/env python3
"""Incrementally synchronize the catalog with the read-only archive tree."""

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

try:
    from .build_catalog import (
        TITLE_LIMIT, actual_mime, classify_asset, clean_title, discover_files,
        infer_saved_at, now_iso, parse_html_metadata, parse_mhtml_metadata, sha256_file,
    )
    from .build_search_index import extract_html_file, extract_mhtml_file
    from .classify_catalog import ensure_columns as ensure_classification_columns, score_asset
except ImportError:  # Direct execution: python3 scripts/incremental_scan.py
    from build_catalog import (
        TITLE_LIMIT, actual_mime, classify_asset, clean_title, discover_files,
        infer_saved_at, now_iso, parse_html_metadata, parse_mhtml_metadata, sha256_file,
    )
    from build_search_index import extract_html_file, extract_mhtml_file
    from classify_catalog import ensure_columns as ensure_classification_columns, score_asset


def ensure_schema(db: sqlite3.Connection) -> None:
    ensure_classification_columns(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
    additions = {
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


def extract_content(path: Path, extension: str) -> tuple[str, int, str, Optional[str]]:
    try:
        if extension in {"mht", "mhtml"}:
            body, truncated = extract_mhtml_file(path)
        elif extension in {"html", "htm"}:
            body, truncated = extract_html_file(path)
        else:
            return "", 0, "not_applicable", None
        return body, int(truncated), "success", None
    except Exception as exc:
        return "", 0, "error", f"{type(exc).__name__}: {exc}"


def inspect_file(path: Path, source: Path, digest: Optional[str] = None) -> Dict[str, object]:
    relative = path.relative_to(source).as_posix()
    stat = path.stat()
    extension = path.suffix.lower().lstrip(".")
    mime = actual_mime(path, extension)
    digest = digest or sha256_file(path)
    metadata: Dict[str, Optional[str]] = {"title_raw": None, "title_clean": None, "source_url": None, "encoding": None}
    if extension in {"html", "htm"} or mime == "text/html":
        metadata = parse_html_metadata(path)
    elif extension in {"mht", "mhtml"} or mime == "multipart/related":
        metadata = parse_mhtml_metadata(path)
    title = metadata.get("title_clean") or clean_title(path.stem[:TITLE_LIMIT])
    asset_type, _, tags = classify_asset(relative, extension, mime, title)
    saved_at, saved_source = infer_saved_at(path.name, stat)
    source_url = metadata.get("source_url")
    body, truncated, extraction_status, extraction_error = extract_content(path, extension)
    category, confidence, reasons = score_asset(relative, title or "", body)
    return {
        "original_path": str(path), "relative_path": relative, "file_name": path.name,
        "extension": extension, "mime_type": mime, "size_bytes": stat.st_size,
        "sha256": digest, "title_raw": metadata.get("title_raw"), "title_clean": title,
        "source_url": source_url, "source_domain": urlparse(source_url).hostname if source_url else None,
        "saved_at": saved_at, "saved_at_source": saved_source,
        "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "encoding": metadata.get("encoding"), "asset_type": asset_type,
        "primary_category": category, "tags_json": json.dumps(tags, ensure_ascii=False),
        "parse_status": "success", "error_message": None, "duplicate_group": None,
        "file_status": "active", "file_mtime_ns": stat.st_mtime_ns,
        "classification_source": "auto-v2" if category != "uncategorized" else "unclassified",
        "classification_confidence": confidence, "classification_reason": "；".join(reasons),
        "body_text": body, "truncated": truncated, "extraction_status": extraction_status,
        "extraction_error": extraction_error,
    }


ASSET_FIELDS = [
    "original_path", "relative_path", "file_name", "extension", "mime_type", "size_bytes",
    "sha256", "title_raw", "title_clean", "source_url", "source_domain", "saved_at",
    "saved_at_source", "modified_at", "encoding", "asset_type", "primary_category",
    "tags_json", "parse_status", "error_message", "duplicate_group", "file_status",
    "file_mtime_ns", "classification_source", "classification_confidence", "classification_reason",
]


def write_asset(db: sqlite3.Connection, asset_id: str, record: Dict[str, object], insert: bool) -> None:
    if insert:
        fields = ["asset_id", *ASSET_FIELDS]
        db.execute(
            f"INSERT INTO assets ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            [asset_id, *(record[field] for field in ASSET_FIELDS)],
        )
    else:
        assignments = ",".join(f"{field}=?" for field in ASSET_FIELDS)
        db.execute(f"UPDATE assets SET {assignments} WHERE asset_id=?", [*(record[field] for field in ASSET_FIELDS), asset_id])
    db.execute("DELETE FROM contents_fts WHERE asset_id=?", (asset_id,))
    db.execute("DELETE FROM contents WHERE asset_id=?", (asset_id,))
    if record["extraction_status"] != "not_applicable":
        db.execute(
            "INSERT INTO contents(asset_id,body_text,text_length,truncated,extraction_status,error_message) VALUES(?,?,?,?,?,?)",
            (asset_id, record["body_text"], len(record["body_text"]), record["truncated"], record["extraction_status"], record["extraction_error"]),
        )
        if record["extraction_status"] == "success":
            db.execute("INSERT INTO contents_fts(asset_id,title,body) VALUES(?,?,?)", (asset_id, record["title_clean"] or "", record["body_text"]))


def synchronize(source: Path, database: Path) -> Dict[str, object]:
    db = sqlite3.connect(str(database))
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    stored_source = db.execute("SELECT value FROM metadata WHERE key='source_path'").fetchone()
    if stored_source and not os.path.samefile(stored_source[0], source):
        db.close()
        raise ValueError(f"source does not match catalog metadata: {stored_source[0]}")
    existing = {row["relative_path"]: row for row in db.execute("SELECT * FROM assets")}
    current_paths = {path.relative_to(source).as_posix(): path for path in discover_files(source, database.parent.parent)}
    absent = {relative: row for relative, row in existing.items() if relative not in current_paths}
    absent_by_hash: Dict[str, list] = {}
    for row in absent.values():
        if row["sha256"]:
            absent_by_hash.setdefault(row["sha256"], []).append(row)
    counts = Counter()
    seen_ids = set()
    for index, (relative, path) in enumerate(sorted(current_paths.items()), 1):
        stat = path.stat()
        old = existing.get(relative)
        if old and old["size_bytes"] == stat.st_size and (
            old["file_mtime_ns"] == stat.st_mtime_ns or
            (old["file_mtime_ns"] is None and old["modified_at"] == dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds"))
        ):
            db.execute("UPDATE assets SET file_status='active', file_mtime_ns=? WHERE asset_id=?", (stat.st_mtime_ns, old["asset_id"]))
            seen_ids.add(old["asset_id"])
            counts["unchanged"] += 1
            continue
        digest = sha256_file(path)
        moved = None
        if old is None:
            candidates = absent_by_hash.get(digest, [])
            if len(candidates) == 1 and candidates[0]["asset_id"] not in seen_ids:
                moved = candidates[0]
        record = inspect_file(path, source, digest)
        if moved:
            asset_id = moved["asset_id"]
            # Moving a file must not overwrite the user's category or tags.
            for field in ("primary_category", "tags_json", "classification_source", "classification_confidence", "classification_reason"):
                record[field] = moved[field]
            write_asset(db, asset_id, record, False)
            counts["moved"] += 1
        elif old:
            asset_id = old["asset_id"]
            # Preserve explicit user choices while refreshing derived metadata.
            if old["classification_source"] in {"manual", "confirmed-auto"}:
                for field in ("primary_category", "tags_json", "classification_source", "classification_confidence", "classification_reason"):
                    record[field] = old[field]
            write_asset(db, asset_id, record, False)
            counts["updated"] += 1
        else:
            asset_id = hashlib.sha256(f"{digest}\0{relative}".encode("utf-8")).hexdigest()[:16]
            write_asset(db, asset_id, record, True)
            counts["added"] += 1
        seen_ids.add(asset_id)
        if index % 25 == 0:
            db.commit()
            print(f"checked {index}/{len(current_paths)}", file=sys.stderr, flush=True)
    if seen_ids:
        missing_cursor = db.execute(
            "UPDATE assets SET file_status='missing' WHERE asset_id NOT IN ({}) AND file_status!='missing'".format(
                ",".join("?" for _ in seen_ids)
            ), tuple(seen_ids)
        )
    else:
        missing_cursor = db.execute("UPDATE assets SET file_status='missing' WHERE file_status!='missing'")
    counts["newly_missing"] = missing_cursor.rowcount
    counts["missing_total"] = db.execute("SELECT COUNT(*) FROM assets WHERE file_status='missing'").fetchone()[0]
    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('incremental_scan_at',?)", (now_iso(),))
    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('file_count',?)", (str(len(current_paths)),))
    db.commit()
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    return {"source": str(source), "database": str(database), "checked": len(current_paths), **counts, "integrity_check": integrity}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument("--report", type=Path, default=Path("reports/incremental-scan-summary.json"))
    args = parser.parse_args()
    source, database = args.source.resolve(), args.database.resolve()
    if not source.is_dir() or not database.is_file():
        print("source directory or database does not exist", file=sys.stderr)
        return 2
    try:
        result = synchronize(source, database)
    except Exception as exc:
        print(f"incremental scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
