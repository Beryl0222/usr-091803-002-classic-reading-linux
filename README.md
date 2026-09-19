# 名著多角度阅读档案

服务于中学《西游记》等名著的跨年级、跨纸书版本阅读与注释协作。系统以
**作品版本 + 章节锚点**承接个人、小组与教师观点，以**只追加事件**记录全部
发布决定，回答三个问题：这句话出自哪个文本版本？这条观点经过哪些发布决定？
过去某个时间点这个班看到的是什么？

## 它解决的问题

| 教学现场的问题 | 系统中的对应设计 |
| --- | --- |
| 共享文档固定不了引用位置（不同学生纸书不同） | 引用落在注册于**具体版本**的章节锚点上，带卷次/页码等跨版本别名 |
| 互相冲突的解释互相覆盖 | 每条观点是独立实体，各自携带证据锚点，永不就地修改 |
| 尚未公开的批注提前暴露给家长 | 发布状态机（草稿→提交→发布/退回→撤回）+ 受众范围（班级/年级/监护人） |
| 教师调整路线、撤回批注、换新版教材 | 一切修改都是追加事件；任何查询支持 `X-As-Of` 回到历史时点 |
| 班级、年龄、监护关系决定可见范围 | 成员关系 + 作品适读年龄 + 有效监护关系三者共同判定 |
| 学生离校后不能再访问，但讨论不能断链 | 身份立即停用（403），观点/评论/证据原样保留并继续显示作者署名 |
| 导入《悟空传》等改编作品 | 仅登记书目元数据与原文章节锚点的关联，不导入正文、不另立锚点 |
| 学期结束要导出本班真正读过/引用过/讨论过的材料 | 教师导出固化材料（read/cited/discussed）与每条观点的完整履历 |

## 领域模型

```
Work（原著/改编）
 └─ Edition（纸书/教材版本，可被新版 supersede）
     └─ ChapterAnchor（章节锚点：章回号、定位、起讫原文、跨版本别名）

Class ── Term ── Route（阅读路线：条目可增删/调序/停用，履历保留）
 ├─ Membership（有起止时间，支持退班/毕业）
 └─ Group（小组，成员有进出时间）

User（teacher/student/guardian）
 ├─ Guardianship（学生↔监护人，可解除）
 └─ Departure（离校：身份停用，发言保留）

Viewpoint（观点：作者 + 主锚点 + 可选小组）
 ├─ Evidence（证据：锚点 + 引文；可关联改编对照链接）
 ├─ Comment（讨论，可由作者或教师删除，删除留痕）
 └─ PublicationDecision 链：submitted / returned / published / withheld / withdrawn
```

**可见范围**：教师可见全部（含草稿，用于审校）；作者与同组成员始终可见本组
观点；已发布观点按发布时授予的受众范围可见——`class`（作者所在班）、
`grade`（同年级且达到作品适读年龄）、`guardians`（作者仍在校且监护关系
有效）。撤回发布（withhold）后学生与家长立即不可见，但教师可继续溯源。

**改编作品**：`register_adaptation` 只存书名、作者、版本注记和指向原著的
关联；`add_adaptation_link` 把改编作品中的某处（如"第三章·天宫"）挂到原著
章节锚点。观点证据引用该关联时，证据锚点必须是关联的原文锚点。

## 运行与测试

```bash
python3 service.py --check            # 基础配置检查
python3 service.py --port 8000        # 启动；GET /health 返回服务身份
python3 service.py --data events.jsonl  # 事件持久化到 JSONL，重启自动重放
npm test                              # 运行全部 29 个契约/领域/HTTP 测试
```

无外部依赖，仅需 Python 3.10+（标准库）。

## HTTP 接口

所有业务接口收发 JSON。写接口用请求头 `X-Actor: <用户id>` 标明操作人
（也可在请求体给 `actor_id`）；读接口可用 `X-As-Of: <ISO时间>` 回到历史
时点。错误码：400 校验失败、403 身份无权（如已离校）、404 不存在或对你
不可见（防止借探测发现未发布批注）、409 事件流冲突。

- 作品：`POST /works`、`GET /works`、`POST /adaptations`、
  `POST /adaptation-links`
- 版本与锚点：`POST /editions`、`POST /editions/<id>/supersede`、
  `POST /anchors`、`GET /anchors/<id>`、`POST /anchors/<id>/aliases`
- 人员：`POST /users`、`POST /users/<id>/depart`、
  `POST /guardianships/link`、`/guardianships/unlink`
- 班级学期：`POST /classes`、`POST /classes/<id>/enrollments`、
  `POST /classes/<id>/terms`、`.../terms/<tid>/close`、
  `GET /classes/<id>/terms/<tid>/export`
- 小组：`POST /groups`、`POST /groups/<id>/members`、`.../remove`
- 路线：`POST /routes`、`GET /routes/<id>`、`POST /routes/<id>/items`、
  `.../items/<iid>/remove|reorder`、`.../retire`
- 观点：`POST /viewpoints`、`GET /viewpoints?class_id=&anchor_id=`、
  `GET /viewpoints/<id>`、`.../evidence`、`.../comments`、
  `POST /viewpoints/<id>/decisions`（`submit|return|publish|withhold|withdraw`）、
  `GET /viewpoints/<id>/provenance`（教师完整溯源）

### 典型流程

```bash
# 1. 版本与锚点（教师）
curl -X POST localhost:8000/anchors -H 'X-Actor: t_wang' -H 'Content-Type: application/json' \
  -d '{"anchor_id":"a7","edition_id":"ed2018","chapter_no":7,
       "chapter_title":"八卦炉中逃大圣","locator":"上册第82页"}'

# 2. 学生提出观点并提交
curl -X POST localhost:8000/viewpoints -H 'Content-Type: application/json' \
  -d '{"viewpoint_id":"vp1","author_id":"s1","primary_anchor_id":"a7",
       "summary":"孙悟空反抗等级秩序"}'
curl -X POST localhost:8000/viewpoints/vp1/decisions -H 'X-Actor: s1' \
  -H 'Content-Type: application/json' -d '{"decision":"submit"}'

# 3. 教师发布给班级和监护人；如发现不妥可撤回
curl -X POST localhost:8000/viewpoints/vp1/decisions -H 'X-Actor: t_wang' \
  -H 'Content-Type: application/json' \
  -d '{"decision":"publish","scopes":["class","guardians"]}'
curl -X POST localhost:8000/viewpoints/vp1/decisions -H 'X-Actor: t_wang' \
  -H 'Content-Type: application/json' \
  -d '{"decision":"withhold","reason":"批注尚未准备好对家长公开"}'

# 4. 期末导出（含读过/引用过/讨论过的材料与每条观点履历）
curl -H 'X-Actor: t_wang' \
  localhost:8000/classes/c1/terms/term2026autumn/export
```

## 溯源与可复原

- `GET /viewpoints/<id>/provenance`（教师）返回作者、每条证据所依据的版本
  锚点、完整发布决定链（决定人、时间、理由、发布范围）；被删除的证据/评论
  正文仅在溯源中保留。
- 任何读接口加 `X-As-Of` 都以该时刻的状态重放：撤回前家长能看到什么、
  调路线前班级读到哪几回、学生离校前能否访问，都可精确复原。
- 导出本身也作为 `TermExported` 事件留痕（谁在何时导出了哪班哪学期）。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `eventlog.py` | 只追加事件日志：全局有序 seq、流内时间戳守卫、as_of 重放、JSONL 持久化 |
| `archive.py` | 领域服务：命令（产生事件）、查询（重放投影）、可见范围判定、学期导出 |
| `service.py` | HTTP 入口：路由、`X-Actor`/`X-As-Of`、统一 JSON 错误、`/health` 契约 |
| `service_contract.py` / `test_archive.py` / `test_web_api.py` | 契约、领域、端到端测试 |
