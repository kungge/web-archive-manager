#!/usr/bin/env python3
"""Search the local web archive catalog."""

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="FTS5 query, e.g. Nacos or 离线安装")
    parser.add_argument("--database", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--category", help="optional primary category filter")
    args = parser.parse_args()
    connection = sqlite3.connect(str(args.database))
    sql = """
        SELECT a.title_clean, a.primary_category, a.source_domain, a.saved_at,
               a.original_path, snippet(contents_fts, 2, '[', ']', '…', 24)
        FROM contents_fts
        JOIN assets a USING(asset_id)
        WHERE contents_fts MATCH ?
    """
    params = [args.query]
    if args.category:
        sql += " AND a.primary_category = ?"
        params.append(args.category)
    sql += " ORDER BY bm25(contents_fts, 0.0, 3.0, 1.0), a.saved_at DESC LIMIT ?"
    params.append(max(1, min(args.limit, 200)))
    rows = connection.execute(sql, params).fetchall()
    for index, (title, category, domain, saved_at, path, snippet) in enumerate(rows, 1):
        print(f"{index}. {title or '(无标题)'}")
        print(f"   分类: {category}  来源: {domain or '-'}  保存: {saved_at or '-'}")
        print(f"   路径: {path}")
        if snippet:
            print(f"   摘要: {snippet.replace(chr(10), ' ')[:500]}")
    print(f"\n共返回 {len(rows)} 条结果")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
