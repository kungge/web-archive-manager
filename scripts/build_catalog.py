#!/usr/bin/env python3
"""Build a read-only catalog for a local web archive.

The source tree is only opened for reading. All generated artifacts are written
under --output. The script has no third-party dependencies.
"""

import argparse
import datetime as dt
import email
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


SCHEMA_VERSION = 1
READ_CHUNK = 1024 * 1024
HTML_METADATA_LIMIT = 8 * 1024 * 1024
TITLE_LIMIT = 1000


class HeadMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: List[str] = []
        self.canonical_url: Optional[str] = None
        self.og_url: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "link" and values.get("rel", "").lower() == "canonical":
            self.canonical_url = values.get("href") or self.canonical_url
        elif tag.lower() == "meta" and values.get("property", "").lower() == "og:url":
            self.og_url = values.get("content") or self.og_url

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and sum(map(len, self.title_parts)) < TITLE_LIMIT:
            self.title_parts.append(data)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(READ_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_prefix(path: Path, limit: int = HTML_METADATA_LIMIT) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def decode_html(data: bytes) -> Tuple[str, str]:
    prefix = data[:4096]
    match = re.search(br"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)", prefix, re.I)
    candidates = []
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030", "utf-16"])
    for encoding in candidates:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", "replace"), "utf-8-replace"


def clean_title(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = html.unescape(value)
    value = re.sub(r"^\([^)]+(?:私信|消息|通知)[^)]*\)\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:TITLE_LIMIT] or None


def parse_html_metadata(path: Path) -> Dict[str, Optional[str]]:
    text, encoding = decode_html(read_prefix(path))
    parser = HeadMetadataParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    title_raw = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()[:TITLE_LIMIT] or None
    source_url = parser.canonical_url or parser.og_url
    return {
        "title_raw": title_raw,
        "title_clean": clean_title(title_raw),
        "source_url": source_url,
        "encoding": encoding,
    }


def parse_mhtml_metadata(path: Path) -> Dict[str, Optional[str]]:
    # Header parsing is intentionally bounded; full MIME/body extraction belongs
    # to phase 2 and must have per-file memory/time limits.
    raw = read_prefix(path, 2 * 1024 * 1024)
    message = email.message_from_bytes(raw, policy=policy.default)
    subject = message.get("Subject")
    location = message.get("Content-Location") or message.get("Snapshot-Content-Location")
    return {
        "title_raw": str(subject)[:TITLE_LIMIT] if subject else None,
        "title_clean": clean_title(str(subject)) if subject else None,
        "source_url": str(location) if location else None,
        "encoding": message.get_content_charset(),
    }


def actual_mime(path: Path, extension: str) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    try:
        prefix = read_prefix(path, 512).lstrip()
    except OSError:
        return guessed or "application/octet-stream"
    lower = prefix.lower()
    if lower.startswith((b"<!doctype html", b"<html")) or b"<html" in lower[:300]:
        return "text/html"
    if lower.startswith(b"from:") and b"mime-version:" in lower:
        return "multipart/related"
    signatures = [
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF8", "image/gif"),
        (b"Rar!", "application/vnd.rar"),
    ]
    for signature, mime in signatures:
        if prefix.startswith(signature):
            return mime
    if extension in {"mht", "mhtml"}:
        return "multipart/related"
    return guessed or "application/octet-stream"


def infer_saved_at(file_name: str, stat: os.stat_result) -> Tuple[str, str]:
    patterns = [
        r"[_ (](20\d{2})[_-](\d{1,2})[_-](\d{1,2})[_ ](\d{1,2})[：:](\d{1,2})[：:](\d{1,2})",
        r"[_ (](20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})",
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, file_name))
        if matches:
            try:
                parts = [int(value) for value in matches[-1].groups()]
                value = dt.datetime(*parts, tzinfo=dt.timezone(dt.timedelta(hours=8)))
                return value.isoformat(), "file_name"
            except ValueError:
                pass
    value = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).astimezone()
    return value.isoformat(timespec="seconds"), "file_mtime"


def classify_asset(relative_path: str, extension: str, mime: str, title: Optional[str]) -> Tuple[str, str, List[str]]:
    combined = f"{relative_path} {title or ''}".lower()
    if extension in {"html", "htm"}:
        asset_type = "web-html"
    elif extension in {"mht", "mhtml"}:
        asset_type = "web-mhtml"
    elif extension in {"md", "txt", "log"}:
        asset_type = "note"
    else:
        asset_type = "attachment"

    tags = [f"format:{asset_type}"]
    source_rules = {
        "zhihu": ["知乎", "zhihu"], "bilibili": ["哔哩哔哩", "bilibili"],
        "csdn": ["csdn"], "juejin": ["掘金", "juejin"],
        "toutiao": ["今日头条", "toutiao"], "baidu": ["百度搜索", "baidu"],
    }
    for source, needles in source_rules.items():
        if any(needle.lower() in combined for needle in needles):
            tags.append(f"source:{source}")
            break
    if any(term in combined for term in ["哔哩哔哩", "bilibili", "/video/"]):
        asset_type = "video-page"
        tags[0] = "format:video-page"
    elif any(term in combined for term in ["百度搜索", "google 搜索", "搜索结果"]):
        asset_type = "search-page"
        tags[0] = "format:search-page"
    elif "ai-chat" in combined or "deepseek" in combined:
        asset_type = "ai-chat"
        tags[0] = "format:ai-chat"

    category_rules = [
        ("technology", ["java", "spring", "mysql", "redis", "linux", "macos", "git", "nacos", "gateway", "架构", "数据库", "devops", "/tech/", "/it/"]),
        ("ai", ["人工智能", "大模型", "llm", "agent", "ollama", "openai", "chatgpt", "cursor", "workbuddy", "/ai/"]),
        ("career-work", ["面试", "招聘", "职场", "工作", "银行项目", "/job/"]),
        ("finance-business", ["股票", "投资", "财经", "信用卡", "商业", "赚钱", "finance"]),
        ("life", ["健康", "家庭", "房产", "租房", "宠物", "生活", "/life/"]),
        ("society-culture", ["社会", "历史", "人文", "影视", "热点事件"]),
        ("productivity-tools", ["效率工具", "软件推荐", "浏览器", "截图", "rpa", "/tool/"]),
    ]
    primary_category = "uncategorized"
    for category, needles in category_rules:
        if any(needle in combined for needle in needles):
            primary_category = category
            break
    return asset_type, primary_category, tags


def discover_files(source: Path, output: Path) -> Iterable[Path]:
    output_resolved = output.resolve()
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs.sort()
        files.sort()
        dirs[:] = [d for d in dirs if (root_path / d).resolve() != output_resolved]
        for name in files:
            yield root_path / name


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY,
            original_path TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT,
            title_raw TEXT,
            title_clean TEXT,
            source_url TEXT,
            source_domain TEXT,
            saved_at TEXT,
            saved_at_source TEXT,
            modified_at TEXT,
            encoding TEXT,
            asset_type TEXT NOT NULL,
            primary_category TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            parse_status TEXT NOT NULL,
            error_message TEXT,
            duplicate_group TEXT,
            file_status TEXT NOT NULL DEFAULT 'active',
            file_mtime_ns INTEGER
        );
        CREATE INDEX idx_assets_sha256 ON assets(sha256);
        CREATE INDEX idx_assets_category ON assets(primary_category);
        CREATE INDEX idx_assets_type ON assets(asset_type);
        CREATE INDEX idx_assets_domain ON assets(source_domain);
    """)


def scan(source: Path, output: Path) -> Dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    reports = output / "reports"
    data_dir = output / "data"
    reports.mkdir(exist_ok=True)
    data_dir.mkdir(exist_ok=True)

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    db_tmp = data_dir / f"catalog.{run_id}.tmp.sqlite"
    json_tmp = reports / f"inventory.{run_id}.tmp.jsonl"
    errors_tmp = reports / f"scan-errors.{run_id}.tmp.csv"
    connection = sqlite3.connect(str(db_tmp))
    create_schema(connection)
    started_at = now_iso()
    counts = Counter()
    bytes_total = 0
    hash_paths: Dict[str, List[str]] = defaultdict(list)

    with json_tmp.open("w", encoding="utf-8") as json_out, errors_tmp.open("w", encoding="utf-8") as error_out:
        error_out.write("relative_path,error\n")
        for index, path in enumerate(discover_files(source, output), 1):
            relative = path.relative_to(source).as_posix()
            record: Dict[str, object] = {
                "original_path": str(path), "relative_path": relative,
                "file_name": path.name, "extension": path.suffix.lower().lstrip("."),
                "parse_status": "success", "error_message": None,
            }
            try:
                stat = path.stat()
                extension = str(record["extension"])
                mime = actual_mime(path, extension)
                digest = sha256_file(path)
                saved_at, saved_source = infer_saved_at(path.name, stat)
                modified_at = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
                metadata: Dict[str, Optional[str]] = {"title_raw": None, "title_clean": None, "source_url": None, "encoding": None}
                if extension in {"html", "htm"} or mime == "text/html":
                    metadata = parse_html_metadata(path)
                elif extension in {"mht", "mhtml"} or mime == "multipart/related":
                    metadata = parse_mhtml_metadata(path)
                title_fallback = path.stem[:TITLE_LIMIT]
                title_clean = metadata.get("title_clean") or clean_title(title_fallback)
                asset_type, category, tags = classify_asset(relative, extension, mime, title_clean)
                source_url = metadata.get("source_url")
                domain = urlparse(source_url).hostname if source_url else None
                stable_id = hashlib.sha256(f"{digest}\0{relative}".encode("utf-8")).hexdigest()[:16]
                record.update({
                    "asset_id": stable_id, "mime_type": mime, "size_bytes": stat.st_size,
                    "sha256": digest, "title_raw": metadata.get("title_raw"), "title_clean": title_clean,
                    "source_url": source_url, "source_domain": domain, "saved_at": saved_at,
                    "saved_at_source": saved_source, "modified_at": modified_at,
                    "encoding": metadata.get("encoding"), "asset_type": asset_type,
                    "primary_category": category, "tags": tags, "duplicate_group": None,
                })
                counts[f"extension:{extension or '[no_ext]'}"] += 1
                counts[f"type:{asset_type}"] += 1
                counts[f"category:{category}"] += 1
                counts["files"] += 1
                bytes_total += stat.st_size
                hash_paths[digest].append(relative)
            except Exception as exc:
                counts["errors"] += 1
                record.update({
                    "asset_id": hashlib.sha256(relative.encode()).hexdigest()[:16],
                    "mime_type": "application/octet-stream", "size_bytes": 0, "sha256": None,
                    "title_raw": None, "title_clean": clean_title(path.stem), "source_url": None,
                    "source_domain": None, "saved_at": None, "saved_at_source": None,
                    "modified_at": None, "encoding": None, "asset_type": "attachment",
                    "primary_category": "uncategorized", "tags": [], "parse_status": "error",
                    "error_message": f"{type(exc).__name__}: {exc}", "duplicate_group": None,
                })
                safe_error = str(record["error_message"]).replace('"', "'")
                error_out.write(f'"{relative.replace(chr(34), chr(39))}","{safe_error}"\n')

            json_out.write(json.dumps(record, ensure_ascii=False) + "\n")
            connection.execute("""
                INSERT INTO assets (
                    asset_id, original_path, relative_path, file_name, extension, mime_type,
                    size_bytes, sha256, title_raw, title_clean, source_url, source_domain,
                    saved_at, saved_at_source, modified_at, encoding, asset_type,
                    primary_category, tags_json, parse_status, error_message, duplicate_group,
                    file_status, file_mtime_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record["asset_id"], record["original_path"], record["relative_path"], record["file_name"],
                record["extension"], record["mime_type"], record["size_bytes"], record.get("sha256"),
                record.get("title_raw"), record.get("title_clean"), record.get("source_url"),
                record.get("source_domain"), record.get("saved_at"), record.get("saved_at_source"),
                record.get("modified_at"), record.get("encoding"), record["asset_type"],
                record["primary_category"], json.dumps(record["tags"], ensure_ascii=False),
                record["parse_status"], record.get("error_message"), record.get("duplicate_group"),
                "active", stat.st_mtime_ns if record["parse_status"] == "success" else None,
            ))
            if index % 50 == 0:
                connection.commit()
                print(f"scanned {index} files", file=sys.stderr, flush=True)

    duplicate_groups = 0
    duplicate_files = 0
    for digest, paths in hash_paths.items():
        if len(paths) > 1:
            duplicate_groups += 1
            duplicate_files += len(paths)
            connection.execute("UPDATE assets SET duplicate_group=? WHERE sha256=?", (digest[:16], digest))
    completed_at = now_iso()
    metadata_rows = {
        "schema_version": str(SCHEMA_VERSION), "source_path": str(source), "started_at": started_at,
        "completed_at": completed_at, "file_count": str(counts["files"]), "total_bytes": str(bytes_total),
    }
    connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", metadata_rows.items())
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    db_final = data_dir / "catalog.sqlite"
    json_final = reports / "inventory.jsonl"
    errors_final = reports / "scan-errors.csv"
    os.replace(db_tmp, db_final)
    os.replace(json_tmp, json_final)
    os.replace(errors_tmp, errors_final)

    summary = {
        "started_at": started_at, "completed_at": completed_at, "source": str(source),
        "files": counts["files"], "bytes": bytes_total, "errors": counts["errors"],
        "duplicate_groups": duplicate_groups, "duplicate_files": duplicate_files,
        "extensions": dict(sorted((k[10:], v) for k, v in counts.items() if k.startswith("extension:"))),
        "asset_types": dict(sorted((k[5:], v) for k, v in counts.items() if k.startswith("type:"))),
        "categories": dict(sorted((k[9:], v) for k, v in counts.items() if k.startswith("category:"))),
    }
    write_report(reports / "scan-summary.md", summary)
    return summary


def markdown_table(values: Dict[str, int]) -> str:
    lines = ["| 项目 | 数量 |", "| --- | ---: |"]
    lines.extend(f"| `{key}` | {value} |" for key, value in sorted(values.items(), key=lambda item: (-item[1], item[0])))
    return "\n".join(lines)


def write_report(path: Path, summary: Dict[str, object]) -> None:
    size_gib = int(summary["bytes"]) / 1024 / 1024 / 1024
    body = f"""# 资产清单扫描报告

> 本报告由 `scripts/build_catalog.py` 生成。扫描过程只读源目录。

## 扫描信息

| 指标 | 结果 |
| --- | --- |
| 源目录 | `{summary['source']}` |
| 开始时间 | `{summary['started_at']}` |
| 完成时间 | `{summary['completed_at']}` |
| 成功登记文件 | {summary['files']} |
| 总字节数 | {summary['bytes']}（约 {size_gib:.2f} GiB） |
| 扫描错误 | {summary['errors']} |
| 完全重复组 | {summary['duplicate_groups']} |
| 重复组内文件 | {summary['duplicate_files']} |

## 扩展名分布

{markdown_table(summary['extensions'])}

## 资产类型分布

{markdown_table(summary['asset_types'])}

## 初步主分类分布

> 当前分类仅为基于路径和标题的可解释规则结果，属于待审阅建议，不会触发文件移动。

{markdown_table(summary['categories'])}

## 产物

- `data/catalog.sqlite`：结构化资产目录。
- `reports/inventory.jsonl`：逐文件审计清单。
- `reports/scan-errors.csv`：失败项。
- `reports/scan-summary.md`：本报告。
"""
    temp = path.with_suffix(".tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="archive root (read only)")
    parser.add_argument("--output", type=Path, required=True, help="catalog output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        print(f"source is not a directory: {source}", file=sys.stderr)
        return 2
    if source == output or source in output.parents:
        print("output must not be inside the source archive", file=sys.stderr)
        return 2
    summary = scan(source, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
