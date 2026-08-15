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
