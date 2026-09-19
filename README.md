# 名著多角度阅读档案

服务于中学名著（如《西游记》）整本书阅读与注释协作。三个年级共用一套档案，
学生纸书版本不同、会对照《悟空传》等改编作品讨论，系统以**作品版本与章节锚点**
承接个人、小组和教师观点，并完整记录发布决定。

## 领域模型如何回应教学场景

| 教学中的问题 | 系统的做法 |
| --- | --- |
| 共享文档固定不了引用位置 | 引用固定为「文本版本 + 章节路径 + 字符偏移 + 原文片段」的锚点（`Anchor`），人文社纸书与教材节选各自定位，互不串用 |
| 互相冲突的解释互相覆盖 | 个人 / 小组 / 教师观点各自成条，携带自己的证据锚点，在同一章节下并存 |
| 未公开批注被家长提前看到 | 观点有草稿 / 已发布 / 已撤回状态；草稿仅作者与本班教师可见，发布是一条显式决定 |
| 教师调路线、撤回批注、换新版教材 | 所有改变只追加为事件（append-only 事件日志），可用 `snapshot_at(序号)` 复原任一时间点班级所见；换教材用「接替观点」在新版本上重新落锚，旧观点保留关联 |
| 班级、年龄、监护关系决定可见范围 | `can_view` 综合班级成员身份、`min_age` 年龄门禁、监护关系判定；家长只能看到被监护学生所在班级、且面向全班/家长的内容 |
| 学生离校 | 凭证立即失效（不能再访问、不能再发言），身份与已发布讨论保留，引用不断链，出处仍署名 |
| 导入《悟空传》等改编作品 | 只登记与原著的关联（adaptation）和必要元数据，正文不入库，也不能在其上落锚 |
| 学期结束导出 | `export_class_term` 只导出本班**读过**（路线编排）、**引用过**（证据锚点）、**讨论过**（观点）的材料；撤回内容不导出正文但保留决定链 |
| 追溯任一观点 | `provenance` 给出作者、依据的文本版本、证据、完整发布决定链与编辑史 |

## 模块

- `reading_archive.py`：领域层（仅标准库）。事件日志 `EventStore`（可选 JSONL 持久化）
  与事件溯源的 `ReadingArchive`，含作品 / 版本 / 章节、人员 / 班级 / 监护关系、
  小组、阅读路线、观点与发布决定、可见性、时间点复原、出处与学期导出。
- `service.py`：HTTP 运行入口，提供稳定的 `/health`。
- `service_contract.py`：服务契约测试。
- `test_archive.py`：领域测试（17 个场景）。

## 运行

```bash
python3 service.py --check          # 基础配置检查
python3 service.py --port 8000      # 启动后访问 /health
npm test                            # 运行全部测试（契约 + 领域）
```

## 典型流程

```python
from reading_archive import EventStore, ReadingArchive

archive = ReadingArchive(EventStore("events.jsonl"))
archive.register_person("t-wang", "t-wang", "王老师", "teacher")   # 首位教师引导
archive.register_work("t-wang", "w-xyj", "西游记", author="吴承恩")
archive.register_version("t-wang", "v-jc2024", "w-xyj", "语文教材2024版节选")
archive.register_chapter("t-wang", "ch-27", "v-jc2024", "第027回", "尸魔三戏唐三藏")
archive.register_class("t-wang", "c-7y1", "七年级一班")
archive.register_person("t-wang", "s-ba", "巴同学", "student", birth_year=2013)
archive.add_class_member("t-wang", "c-7y1", "s-ba")

archive.create_viewpoint(
    "s-ba", "vp-1", "class",
    {"version_id": "v-jc2024", "chapter_path": "第027回", "start": 100, "end": 160,
     "quote": "那怪物……"},
    "唐僧人妖不辨，逐走悟空是糊涂。", class_id="c-7y1", publish=True)

withdraw_seq = archive.withdraw_viewpoint("t-wang", "vp-1")["seq"]  # 撤回：立即不可见
archive.can_view("vp-1", "s-ba")                  # -> (False, "withdrawn")
archive.snapshot_at(withdraw_seq - 1).can_view("vp-1", "s-ba")      # 撤回前可复原 -> True
archive.provenance("vp-1")                        # 谁、依据哪个版本、哪些发布决定
archive.export_class_term("t-wang", "c-7y1")      # 学期导出：读过 / 引用过 / 讨论过
```
