#!/usr/bin/env python3
"""Extract local archive text and build an SQLite FTS5 search index."""

import argparse
import codecs
import email
import html
import re
import sqlite3
import sys
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


TEXT_LIMIT = 2_000_000
MHTML_SIZE_LIMIT = 128 * 1024 * 1024


class VisibleTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "svg", "canvas", "noscript", "template"}
    BREAK_TAGS = {"p", "div", "article", "section", "main", "header", "footer", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, limit: int = TEXT_LIMIT) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.length = 0
        self.skip_depth = 0
        self.parts: List[str] = []

    @property
    def full(self) -> bool:
        return self.length >= self.limit

    def add(self, value: str) -> None:
        if self.full:
            return
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return
        value = value[: self.limit - self.length]
        self.parts.append(value)
        self.length += len(value)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif tag in self.BREAK_TAGS and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BREAK_TAGS and not self.skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.add(data)

    def text(self) -> str:
        value = " ".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\s*\n\s*", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return html.unescape(value).strip()[: self.limit]


def detect_encoding(prefix: bytes) -> str:
    match = re.search(br"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)", prefix[:4096], re.I)
    if match:
        candidate = match.group(1).decode("ascii", "ignore")
        try:
            codecs.lookup(candidate)
            return candidate
        except LookupError:
            pass
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8"


def extract_html_file(path: Path) -> Tuple[str, bool]:
    parser = VisibleTextParser()
    with path.open("rb") as stream:
        prefix = stream.read(8192)
        encoding = detect_encoding(prefix)
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        parser.feed(decoder.decode(prefix))
        while not parser.full:
            block = stream.read(1024 * 1024)
            if not block:
                break
            parser.feed(decoder.decode(block))
        if not parser.full:
            parser.feed(decoder.decode(b"", final=True))
    return parser.text(), parser.full


def html_parts(message: email.message.Message) -> Iterable[email.message.Message]:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/html":
                yield part
    elif message.get_content_type() == "text/html":
        yield message


def extract_mhtml_file(path: Path) -> Tuple[str, bool]:
    if path.stat().st_size > MHTML_SIZE_LIMIT:
        raise ValueError(f"mhtml exceeds {MHTML_SIZE_LIMIT} byte extraction limit")
    with path.open("rb") as stream:
        message = email.message_from_binary_file(stream, policy=policy.default)
    parser = VisibleTextParser()
    found = False
    for part in html_parts(message):
        found = True
        payload = part.get_payload(decode=True) or b""
        encoding = part.get_content_charset() or detect_encoding(payload[:8192])
        parser.feed(payload.decode(encoding, "replace"))
        if parser.full:
            break
    if not found:
        raise ValueError("no text/html MIME part found")
    return parser.text(), parser.full


def prepare_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        DROP TABLE IF EXISTS contents;
        DROP TABLE IF EXISTS contents_fts;
        CREATE TABLE contents (
            asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
            body_text TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            extraction_status TEXT NOT NULL,
            error_message TEXT
        );
        CREATE VIRTUAL TABLE contents_fts USING fts5(
            asset_id UNINDEXED,
            title,
            body,
            tokenize='trigram'
        );
    """)


def build(db_path: Path) -> dict:
    connection = sqlite3.connect(str(db_path))
    prepare_schema(connection)
    rows = connection.execute("""
        SELECT asset_id, original_path, extension, asset_type, COALESCE(title_clean, '')
        FROM assets
        WHERE asset_type IN ('web-html','web-mhtml','video-page','search-page','ai-chat')
        ORDER BY relative_path
    """).fetchall()
    success = errors = truncated_count = indexed_chars = 0
    for index, (asset_id, original_path, extension, asset_type, title) in enumerate(rows, 1):
        path = Path(original_path)
        try:
            if extension in {"mht", "mhtml"}:
                body, truncated = extract_mhtml_file(path)
            else:
                body, truncated = extract_html_file(path)
            connection.execute("INSERT INTO contents VALUES (?,?,?,?,?,NULL)", (asset_id, body, len(body), int(truncated), "success"))
            connection.execute("INSERT INTO contents_fts(asset_id,title,body) VALUES (?,?,?)", (asset_id, title, body))
            success += 1
            truncated_count += int(truncated)
            indexed_chars += len(body)
        except Exception as exc:
            connection.execute("INSERT INTO contents VALUES (?,?,?,?,?,?)", (asset_id, "", 0, 0, "error", f"{type(exc).__name__}: {exc}"))
            errors += 1
        if index % 25 == 0:
            connection.commit()
            print(f"extracted {index}/{len(rows)}", file=sys.stderr, flush=True)
    connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('search_index_version','2-trigram')")
    connection.commit()
    connection.execute("INSERT INTO contents_fts(contents_fts) VALUES('optimize')")
    connection.commit()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    return {
        "candidates": len(rows), "success": success, "errors": errors,
        "truncated": truncated_count, "indexed_characters": indexed_chars,
        "integrity_check": integrity,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.database.is_file():
        print(f"database not found: {args.database}", file=sys.stderr)
        return 2
    result = build(args.database)
    print(result)
    return 0 if result["errors"] == 0 and result["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
