#!/usr/bin/env python3
"""Apply explainable category suggestions to uncategorized catalog assets."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


CATEGORY_RULES = {
    "technology": ["java", "spring", "mysql", "redis", "linux", "docker", "kafka", "nacos", "git", "github", "gitee", "程序员", "编程", "代码", "数据库", "架构", "算法", "线程", "接口", "服务器", "云盘", "c#", "python", "前端", "后端", "开发"],
    "ai": ["人工智能", "大模型", "deepseek", "chatgpt", "openai", "ollama", "agent", "workbuddy", "cursor", "模型训练", "深度学习", "机器学习", "ai工具", "生成式ai"],
    "career-work": ["面试", "招聘", "求职", "简历", "职场", "职业", "工作经验", "晋升", "领导力", "毕业", "考研", "公务员", "银行项目"],
    "finance-business": ["股票", "基金", "上证", "沪深", "中证", "投资", "理财", "财经", "金融", "税收", "个税", "信用卡", "保险", "贷款", "商业", "创业", "赚钱"],
    "life": ["健康", "健身", "跑步", "减肥", "腹肌", "前列腺", "心率", "养生", "美白", "食谱", "汽车", "买车", "驾驶", "装修", "房子", "租房", "居住证", "社保", "吉他", "英语", "托福", "购物", "家庭", "宠物", "上海", "南昌"],
    "society-culture": ["历史", "社会", "文化", "电影", "影视", "演员", "小品", "方言", "科学家", "图灵", "霍金", "冷知识", "段子", "搞笑", "新闻", "事故", "法律", "派出所", "教育", "大学排名"],
    "productivity-tools": ["软件", "工具", "浏览器", "插件", "idm", "xmind", "pdf", "截图", "录屏", "office", "vscode", "idea", "postman", "效率", "下载器", "电子书", "kindle", "ipad"],
}

PATH_HINTS = {
    "technology": ["/tech/", "/it/", "it-improve/"],
    "ai": ["/ai/", "ai-chat/ai相关/"],
    "career-work": ["/job/", "工作职场/"],
    "finance-business": ["/finance/", "financedata/"],
    "life": ["/life/", "lifenotedata/", "/health/", "/shanghai/"],
    "society-culture": ["历史人文/", "社会热点/", "热点事件/", "影视剧/"],
    "productivity-tools": ["/tool/", "开发工具/", "桌面工具/"],
}


def ensure_columns(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
    additions = {
        "classification_source": "TEXT NOT NULL DEFAULT 'legacy'",
        "classification_confidence": "REAL",
        "classification_reason": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
    db.execute("UPDATE assets SET classification_source='unclassified' WHERE primary_category='uncategorized' AND classification_source='legacy'")
    db.commit()


def score_asset(relative_path: str, title: str, body: str) -> Tuple[str, float, List[str]]:
    path_text = f"/{relative_path.lower()}"
    title_text = (title or "").lower()
    body_text = (body or "")[:30000].lower()
    scores: Dict[str, float] = Counter()
    reasons: Dict[str, List[str]] = {category: [] for category in CATEGORY_RULES}
    for category, hints in PATH_HINTS.items():
        for hint in hints:
            if hint.lower() in path_text:
                scores[category] += 5
                reasons[category].append(f"路径:{hint}")
    for category, keywords in CATEGORY_RULES.items():
        body_hits = []
        for keyword in keywords:
            needle = keyword.lower()
            if needle in title_text:
                scores[category] += 3
                reasons[category].append(f"标题:{keyword}")
            elif needle in body_text:
                body_hits.append(keyword)
        # Archived pages contain navigation and recommendations. Body matches
        # are supporting evidence only and cannot accumulate without bound.
        scores[category] += min(2, len(body_hits))
        reasons[category].extend(f"正文:{keyword}" for keyword in body_hits[:2])
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return "uncategorized", 0.0, []
    best_category, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    lead = best_score - second_score
    confidence = min(0.98, 0.42 + best_score * 0.055 + lead * 0.035)
    # Require either a strong path/title signal, or several independent signals.
    if best_score < 4 or lead < 2 or confidence < 0.67:
        return "uncategorized", round(confidence, 2), reasons[best_category][:5]
    return best_category, round(confidence, 2), reasons[best_category][:5]


def classify(database: Path, apply: bool) -> Dict[str, object]:
    override_path = database.parent / "user-overrides.json"
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.is_file() else {}
    db = sqlite3.connect(str(database))
    ensure_columns(db)
    rows = db.execute("""
        SELECT a.asset_id, a.relative_path, COALESCE(a.title_clean,''), COALESCE(c.body_text,'')
        FROM assets a LEFT JOIN contents c USING(asset_id)
        WHERE a.file_name != '.DS_Store' AND a.primary_category='uncategorized'
        ORDER BY a.relative_path
    """).fetchall()
    suggestions = []
    counts = Counter()
    for asset_id, path, title, body in rows:
        if asset_id in overrides:
            counts["manual_skipped"] += 1
            continue
        category, confidence, reasons = score_asset(path, title, body)
        counts[category] += 1
        if category != "uncategorized":
            suggestions.append({"asset_id": asset_id, "path": path, "title": title, "category": category, "confidence": confidence, "reasons": reasons})
            if apply:
                db.execute("""
                    UPDATE assets SET primary_category=?, classification_source='auto-v2',
                    classification_confidence=?, classification_reason=? WHERE asset_id=?
                """, (category, confidence, "；".join(reasons), asset_id))
        elif apply:
            db.execute("UPDATE assets SET classification_source='unclassified', classification_confidence=?, classification_reason=? WHERE asset_id=?", (confidence, "；".join(reasons), asset_id))
    if apply:
        db.commit()
    db.close()
    return {"mode": "apply" if apply else "dry-run", "candidates": len(rows), "suggested": len(suggestions), "remaining": counts["uncategorized"], "manual_skipped": counts["manual_skipped"], "categories": dict(counts), "suggestions": suggestions}


def confirm_all(database: Path) -> int:
    db = sqlite3.connect(str(database))
    ensure_columns(db)
    cursor = db.execute("""
        UPDATE assets SET classification_source='confirmed-auto',
        classification_reason=COALESCE(classification_reason,'') || '；用户批量确认'
        WHERE classification_source='auto-v2'
    """)
    db.commit()
    count = cursor.rowcount
    db.close()
    return count


def write_report(path: Path, result: Dict[str, object]) -> None:
    lines = [
        "# 自动分类报告", "", f"> 模式：`{result['mode']}`", "", "## 汇总", "",
        "| 指标 | 数量 |", "| --- | ---: |", f"| 待分析 | {result['candidates']} |",
        f"| 产生建议 | {result['suggested']} |", f"| 仍未分类 | {result['remaining']} |",
        f"| 跳过人工覆盖 | {result['manual_skipped']} |", "", "## 分类分布", "",
        "| 分类 | 数量 |", "| --- | ---: |",
    ]
    lines.extend(f"| `{category}` | {count} |" for category, count in sorted(result["categories"].items(), key=lambda item: -item[1]))
    lines.extend(["", "## 建议样本", "", "| 建议分类 | 置信度 | 标题 | 依据 |", "| --- | ---: | --- | --- |"])
    for item in result["suggestions"][:80]:
        title = item["title"].replace("|", "｜")[:80]
        reason = "；".join(item["reasons"]).replace("|", "｜")
        lines.append(f"| `{item['category']}` | {item['confidence']:.2f} | {title} | {reason} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument("--apply", action="store_true", help="write suggestions to the local catalog")
    parser.add_argument("--confirm-all", action="store_true", help="confirm every pending auto-v2 suggestion")
    parser.add_argument("--report", type=Path, default=Path("reports/classification-summary.md"))
    args = parser.parse_args()
    if args.confirm_all:
        count = confirm_all(args.database)
        print(json.dumps({"confirmed": count}, ensure_ascii=False, indent=2))
        return 0
    result = classify(args.database, args.apply)
    write_report(args.report, result)
    printable = {key: value for key, value in result.items() if key != "suggestions"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
