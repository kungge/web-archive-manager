# 资产清单扫描报告

> 本报告由 `scripts/build_catalog.py` 生成。扫描过程只读源目录。

## 扫描信息

| 指标 | 结果 |
| --- | --- |
| 源目录 | `/Users/wankun/allbak/网页` |
| 开始时间 | `2026-08-15T14:58:34+08:00` |
| 完成时间 | `2026-08-15T14:59:25+08:00` |
| 成功登记文件 | 783 |
| 总字节数 | 3630871311（约 3.38 GiB） |
| 扫描错误 | 0 |
| 完全重复组 | 0 |
| 重复组内文件 | 0 |

## 扩展名分布

| 项目 | 数量 |
| --- | ---: |
| `html` | 300 |
| `mhtml` | 225 |
| `[no_ext]` | 127 |
| `md` | 66 |
| `jpg` | 19 |
| `png` | 16 |
| `xmind` | 11 |
| `mht` | 8 |
| `gif` | 2 |
| `pdf` | 2 |
| `xlsx` | 2 |
| `doc` | 1 |
| `log` | 1 |
| `pptx` | 1 |
| `rar` | 1 |
| `txt` | 1 |

## 资产类型分布

| 项目 | 数量 |
| --- | ---: |
| `web-mhtml` | 208 |
| `web-html` | 178 |
| `attachment` | 162 |
| `video-page` | 77 |
| `ai-chat` | 70 |
| `note` | 68 |
| `search-page` | 20 |

## 初步主分类分布

> 当前分类仅为基于路径和标题的可解释规则结果，属于待审阅建议，不会触发文件移动。

| 项目 | 数量 |
| --- | ---: |
| `uncategorized` | 425 |
| `technology` | 200 |
| `finance-business` | 39 |
| `ai` | 30 |
| `career-work` | 27 |
| `productivity-tools` | 26 |
| `life` | 19 |
| `society-culture` | 17 |

## 产物

- `data/catalog.sqlite`：结构化资产目录。
- `reports/inventory.jsonl`：逐文件审计清单。
- `reports/scan-errors.csv`：失败项。
- `reports/scan-summary.md`：本报告。
