# 全文索引构建报告

> 构建日期：2026-08-15

## 结果

| 指标 | 结果 |
| --- | ---: |
| 候选网页资产 | 553 |
| 正文抽取成功 | 553 |
| 正文抽取失败 | 0 |
| 达到单页截断上限 | 0 |
| 索引可见字符 | 4,612,979 |
| SQLite 完整性检查 | `ok` |
| 分词方式 | FTS5 trigram |

索引对象包括 HTML、MHTML/MHT、视频详情页、搜索结果页和 AI 对话页。Markdown、PDF、Office、XMind、图片和压缩包暂时只登记基础元数据，未进入正文索引。

## 使用方式

```bash
cd /Users/wankun/note/cnb/MyNote/NoteHub/70-projects/my-tools/web-archive-manager
python3 scripts/search_catalog.py '离线安装'
```

可以通过 `--category technology` 限制主分类，通过 `--limit 10` 控制数量。

## 当前限制

- 正文抽取为通用规则，部分站点会混入导航、相关推荐等页面噪声。
- 现阶段分类主要依据路径和标题，425 项仍为 `uncategorized`。
- 搜索结果页、AI 对话页和视频页已经区分资产类型，但尚未实现站点专用字段。
- 当前是命令行检索，还没有图形化管理界面。

## 抽样验证

- 搜索 `离线安装`：返回 4 条结果，可命中 HTML 与 MHTML 正文。
- 搜索 `银行项目`：返回 4 条结果，可展示本地路径和命中摘要。
- 抽样发现“Java 银行项目面试题”先命中 Java 规则，被建议为 `technology`。分类规则需在下一阶段增加用途关键词优先级，暂不能用于自动搬迁。
