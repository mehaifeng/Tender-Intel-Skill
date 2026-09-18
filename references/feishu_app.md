# 飞书应用权限与授权实测

本项目所有飞书读写都走同一个自建应用。**开通权限（scope）和资源授权（协作者）是两件独立的事，缺任何一件调用都失败**，而且两者的报错码完全不同——先照着第 4 节的错误码对照表定位是哪一道坎，再去对应章节修。

实测日期 2026-09-17，租户 `cp-pharm`。

## 1. 应用身份与凭据

| 项 | 值 |
|---|---|
| app_id | `cli_aade3b19793c9bef` |
| 开发者后台 | https://open.feishu.cn/app/cli_aade3b19793c9bef |
| 凭据来源 | 环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID` 优先，其次 `config/feishu_app.json` |
| 加载入口 | `scripts/feishu_client.py` 的 `load_credentials()`，缺任何一项直接抛错，不猜 |

app_secret 只存在于本地配置或环境变量，不写进任何文档和提交。

## 2. 需要开通的能力与权限

### 2.1 应用能力

| 能力 | 状态 | 为什么需要 |
|---|---|---|
| 机器人 | 必须开启 | **不开机器人，应用在云文档协作者搜索框里根本搜不到**。云文档的协作者认「用户 / 群组 / 部门 / 机器人」四类身份，应用靠机器人身份才被检索到。未开启时 `/bot/v3/info` 返回 `11205 app do not have bot` |

### 2.2 权限（scope）

| scope | 状态 | 支撑的操作 |
|---|---|---|
| `bitable:app` | 已开通 | 多维表格台账读写（本项目主链路） |
| `drive:drive` | 已开通 | 云空间读、列目录、新建文件夹、上传、移动、复制、删除、下载 |
| `docx:document` `docx:document:create` | **未开通** | 新建/编辑飞书文档（docx）。不做文档就不需要 |
| `drive:export:readonly` `docs:document:export` | **未开通** | 把云文档导出成 xlsx/pdf/docx 再下载。**上传上去的原始文件下载不需要这个** |

改完 scope 必须**发布新版本并由管理员审批**，才对 tenant_access_token 生效。申请链接形如：
`https://open.feishu.cn/app/cli_aade3b19793c9bef/auth?q=drive:drive&op_from=openapi&token_type=tenant`

## 3. 资源授权（协作者）

开通 scope 只是拿到「这类接口的调用资格」，具体某个文件夹/文档能不能动，取决于应用是不是该节点的协作者。两者分别失败时的表现：

- 只缺 scope：`99991672`，报文直接列出缺哪个权限
- scope 齐、缺节点权限：`91204 forbidden` / `1061004 forbidden` / `1254701 DriveNodePermNotAllow`

### 目标资源

| 资源 | token | 应用权限 |
|---|---|---|
| 文件夹 `Bidding` | `NGxkfS2cOlhBDSdKUJuc2x6Vnnc` | `full_access`（可管理） |
| 标讯台账多维表格 | `Fy94b1zv8a6HefsftLGcxBPlnYd` | 可读写 |
| 标讯台账数据表 | `tbl8tFKaOFHW7Cau` | 可读写 |

协作者列表实际返回：

```
member_type=appid   member_id=cli_aade3b19793c9bef   perm=full_access   perm_type=container
```

### 加协作者的操作路径

1. 开发者后台 → 添加应用能力 → **机器人** → 发布版本 → 管理员审批
2. 打开目标文件夹 → 共享 → 添加协作者 → 搜应用名 → 给**可编辑**或**可管理**
3. 若该租户的文件夹共享面板只认群组：把机器人拉进一个群，再把文件夹共享给该群，权限一样继承

文件夹权限对子节点是继承的（`perm_type=container`），所以授权一次到文件夹即可，不必逐个文件加。

## 4. 错误码对照表

| code | 含义 | 该修哪里 |
|---|---|---|
| `99991672` | 缺 scope，报文里列出具体权限名 | 开发者后台申请 + 发版 + 管理员审批 |
| `91204` / `1061004` | forbidden：scope 有了，节点没权限 | 把应用加成该节点协作者 |
| `1063004` | User has no share permission | 同上，且应用至少要「可管理」才能读协作者列表 |
| `1254701` | DriveNodePermNotAllow | 同上：往别人的文件夹里建东西被节点权限挡住 |
| `11205` | app do not have bot | 应用没开机器人能力 |
| `99992402` | field validation failed | 参数或接口本身不对，与权限无关（见 5.2） |

## 5. 实测结果

在 `Bidding` 下新建 `_claude_probe` 子文件夹跑完全部用例，结束后原样删除，原有内容未触碰。

### 5.1 通过

| 类别 | 操作 | 接口 |
|---|---|---|
| 阅读 | 文件夹元信息 | `GET /drive/explorer/v2/folder/:token/meta` |
| 阅读 | 列出文件夹内容 | `GET /drive/v1/files?folder_token=` |
| 阅读 | 协作者列表 | `GET /drive/v1/permissions/:token/members?type=folder` |
| 新增 | 新建子文件夹 | `POST /drive/v1/files/create_folder` |
| 新增 | 新建多维表格 | `POST /bitable/v1/apps` |
| 新增 | 新建电子表格 | `POST /sheets/v3/spreadsheets` |
| 新增 | 上传文件 | `POST /drive/v1/files/upload_all` |
| 修改 | 多维表格增 / 改 / 删记录 | `POST|PUT|DELETE /bitable/v1/apps/:app/tables/:tbl/records` |
| 修改 | 电子表格写单元格并回读校验一致 | `PUT /sheets/v2/spreadsheets/:token/values` |
| 修改 | 云文档改名 | `PUT /bitable/v1/apps/:app_token` |
| 修改 | 文件移动 / 复制 | `POST /drive/v1/files/:token/move` `/copy` |
| 下载 | 下载上传的文件，66 字节逐字节一致 | `GET /drive/v1/files/:token/download` |
| 删除 | 删文件 / 多维表格 / 电子表格 | `DELETE /drive/v1/files/:token?type=` |
| 删除 | 删文件夹（异步任务 `status=success`） | `DELETE /drive/v1/files/:token?type=folder` + `GET /drive/v1/files/task_check` |

删除进回收站，30 天内可还原，不是物理抹除。

### 5.2 不通过

| 操作 | 原因 | 处理 |
|---|---|---|
| 新建 docx 文档 | 缺 `docx:document` `docx:document:create` | 需要文档能力时再申请 |
| 云文档导出 xlsx | 缺 `drive:export:readonly` `docs:document:export` | 需要导出时再申请 |
| 给上传的文件改名 | 开放平台没有这个接口，`PATCH /drive/v1/files/:token` 返回 `99992402` | 用 `/copy` 带新名复制，或上传前定好文件名 |

### 5.3 结论

当前权限对本项目用法——多维表格台账读写、附件上传下载、云空间文件归档整理——已经够用，没有待补项。

## 6. 复测

换应用、改权限或迁移文件夹后，按以下顺序验证，每一步的失败码直接对应第 4 节：

1. `POST /auth/v3/tenant_access_token/internal` 取 token —— 验凭据
2. `GET /bot/v3/info` —— 验机器人能力
3. `GET /drive/explorer/v2/folder/:token/meta` —— 验 scope + 节点权限
4. `GET /drive/v1/permissions/:token/members?type=folder` —— 确认 appid 在列且 perm 足够
5. `POST /drive/v1/files/create_folder` 建临时文件夹，再 `DELETE ...?type=folder` 删掉 —— 验写权限，且不留痕
