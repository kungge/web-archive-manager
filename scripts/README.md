# 扫描脚本

运行：

```bash
python3 scripts/build_catalog.py \
  --source '/Users/wankun/allbak/网页' \
  --output .
```

脚本只读源目录，生成以下产物：

```text
data/catalog.sqlite
reports/inventory.jsonl
reports/scan-errors.csv
reports/scan-summary.md
```

重复运行会完整重建索引，并在全部扫描结束后原子替换上一版索引。

日常新增网页后使用增量扫描，不必重建整个目录：

```bash
python3 scripts/incremental_scan.py \
  --source '/Users/wankun/allbak/网页' \
  --database data/catalog.sqlite
```

脚本会跳过未变化文件，更新内容有变化的文件，并识别新增、移动和缺失文件。缺失项只标记为 `missing`，不会删除索引记录；移动或更新也会保留原有资产 ID、收藏、已读状态和个人备注。运行摘要写入 `reports/incremental-scan-summary.json`。

生成资产清单后，构建正文与全文检索索引：

```bash
python3 scripts/build_search_index.py --database data/catalog.sqlite
```

全文索引使用 SQLite FTS5 的 trigram 分词，支持中文片段检索；默认最多保存每个网页前 200 万个可见字符。脚本忽略脚本、样式、SVG 等非正文节点，不改写网页原件。

命令行检索：

```bash
python3 scripts/search_catalog.py '离线安装'
python3 scripts/search_catalog.py 'Nacos' --category technology --limit 10
```

自动分类先执行 dry-run：

```bash
python3 scripts/classify_catalog.py
```

审阅 `reports/classification-summary.md` 后再写入本地索引：

```bash
python3 scripts/classify_catalog.py --apply
```

分类器只处理当前未分类内容，并跳过 `data/user-overrides.json` 中的人工修改。

批量确认所有自动建议：

```bash
python3 scripts/classify_catalog.py --confirm-all
```
