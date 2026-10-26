# Atlas Knowledge Demo

独立的文字自然语言知识管理实验：录入 → 自动保存待确认 → 普通查询与星图 → 人工确认 → 精准统计。当前说明见 [自动待确认实现](docs/auto-pending-implementation.md)，下文较早的手工预览描述保留为演进记录。

本项目用于验证 Pydantic AI + Graphiti + Neo4j 的开放业务对象能力，独立于 Frappe 业务入口。没有引入新的 AtlasAI 主后端；本地 HTTP 服务由 Python 标准库提供。

手机端使用 Auth0 认证。每个已验证账号首次登录时会自动创建独立个人空间；用户、成员角色和访问审计保存在 Neo4j，知识数据按空间的 `dataset` 范围隔离。密码和 Access Token 不写入业务数据库。团队空间的创建与邀请暂不在本期开放。

从旧版单空间部署升级时，先备份 Neo4j，再将旧数据绑定给确认的 Auth0 账号一次：

```bash
cd /opt/atlasai-knowledge/knowledge-management-demo
.venv/bin/python -m demo.workspace_migrate --subject 'auth0|USER_ID' --acknowledge-data-binding
```

该迁移只创建用户、个人空间、所有者关系和审计记录，沿用旧数据集，不复制知识节点。后续新用户无需运行此命令，首次登录会获得自己的空个人空间。团队成员的本地管理入口见 `python -m demo.mobile_members --help`。

## 本地启动

需要 Python 3.10+、Docker Compose。Pydantic AI slim 固定为 **2.54.0**、Graphiti 为 **0.30.1**、OpenAI SDK 为 **3.35.1**。使用独立虚拟环境，避免 Windows 全局旧版 Graphiti 或 NumPy/Pandas 依赖冲突。

Linux / WSL：

```bash
cd knowledge-management-demo
cp .env.example .env
# 在 .env 填写模型与嵌入服务配置
bash start.sh
```

首次启动创建 `.venv`，按 `requirements.lock.txt` 安装本次验收的完整依赖版本，启动专用 Neo4j 后打开服务。访问 [http://127.0.0.1:8096](http://127.0.0.1:8096)。服务只监听本机，没有多人鉴权，不用于公网部署。

Windows（当前 PowerShell 中需能调用 Docker Compose）：

```powershell
cd E:\porject\AtlasAI\knowledge-management-demo
Copy-Item .env.example .env
# 填写 .env 中的模型配置
.\start.ps1
```

Windows 脚本使用 `.venv-win`，WSL 使用 `.venv`，避免两个系统相互覆盖虚拟环境。

本项目自己的 `.venv` 已完成独立安装。已有旧虚拟环境升级时先执行 `.venv/bin/python -m pip install -r requirements.lock.txt`（Windows 使用 `.venv-win\\Scripts\\python`）。复用现有模型配置的启动方式：

```bash
cd /mnt/e/porject/AtlasAI/knowledge-management-demo
bash start.sh --model-env /mnt/e/porject/WSL/graphiti-experiment/.env
```

若要指定其他已安装依赖的隔离 Python 环境，可用 `KD_PYTHON`：

```bash
cd /mnt/e/porject/AtlasAI/knowledge-management-demo
KD_PYTHON=/mnt/e/porject/WSL/graphiti-experiment/.venv/bin/python \
  bash start.sh --model-env /mnt/e/porject/WSL/graphiti-experiment/.env
```

`--model-env` 只读取模型与嵌入配置，兼容 `LLM_*`、`OPENAI_*`、`DEEPSEEK_*`、`GRAPHITI_EMBEDDING_*` 命名。数据库只读取本项目的 `KD_*` 配置；不会从旧环境继承业务 Neo4j 地址。密钥不会传给浏览器或写进示例配置。

专用数据库端口：Bolt `17693`，Browser `17493`；服务 `8096`。Compose 数据保存在 `demo-neo4j` 卷。停止应用用 Ctrl+C；停止数据库用 `docker compose stop`，数据仍保留。

## 使用

1. 在左侧描述具体业务信息，如“甲公司有合同 A，金额 20 万元人民币，审核中。”
2. 核对对象类型、编号、适用范围、属性、币种和来源。有同名/别名候选时，先选择对象身份并更新预览，再确认保存。
3. 解析不准确时点击“修改描述”，在左侧调整描述后重新发送。更正已保存信息需明确说明原对象及更正意图。
4. 保存后，右侧从动态类型目录浏览对象、分页属性、历史和双向关联。可以输入对象 ID 直接打开；每次沿关系展开一层，默认最大深度 4。
5. 输入“目前记录了多少份合同？”“审核中的合同有多少份？”“按所属公司统计合同数量。”“合同金额总计多少人民币？”执行确定性查询。
6. 数据库完成查询后，LLM 根据问题和结构化查询结果生成自然语言，直接在左侧回答数量、金额或分组等结论；同一回答保存在聊天历史中。右侧统计结果附范围、条件、参与/排除数量、排除原因和可分页的复核对象。对象详情可进一步打开来源原文。

### 整表录入

粘贴 Markdown 表格后，模型先解释表头，生成实体字段、跨行身份依据、属性与关系映射，并复核是否遗漏明确关系或添加无依据关系。程序不按“合同”“客户”等列名编写分支；依据模型映射逐字读取全部单元格，每批最多 10 行，持久化进度和已完成批次。表头单位和数据行分别保留原文摘录。只提供名称不补编号，只提供编号不补名称；短数字保持原值。

界面显示整表进度；全部非空列及全部行覆盖通过后，才生成一个待确认预览，显示分类数量、新建/复用数量、关系/属性数量及逐行覆盖清单。失败进入全局错误区，可从检查点重试或取消；应用重启后可恢复任务。处理期间没有业务对象或事实写入；整表通过既有版本、来源、权限和人工确认后，在一次事务中保存。相同已确认编号复用已有对象；名称候选有歧义时须在预览选择。重复导入同一已提交表格还会使用原导入绑定的对象 ID，避免按姓名重新创建。

普通对话仍限制 40 个对象、100 条事实；服务端汇总表格最多 1000 个对象、5000 条事实。当前表格上限为 200 行、24 列，仍受单条消息 16000 字限制。仅支持一张语义一致的 Markdown 表；不规则列数、超限、无法按统一映射理解或不完整数值会显式停止，不截断、不以半份数据提交。需要补充说明时可取消后修改原表重新提交。若模型判断用户只是在用表格提问，后续执行只授予读取权限，不生成写入预览。

`GET /api/import?id=...` 返回当前范围导入进度；`POST /api/import` 仅接受任务 ID 与 `retry/cancel`。`KDImport` 保存来源、映射、检查点与已确认对象引用；不替代 `KDDraft/KDApproval` 的确认、幂等和事务边界。

对话执行由 Pydantic AI 主 Agent 统一完成，当前配置仍接 DeepSeek。模型调用受控查询工具后，根据实际结果生成自然语言；数据库负责精确计算。分页、排除项和未确定分组需按结果说明。完整的原生模型/工具消息由服务端持久化，页面仅展示对话文字；系统错误进入全局错误区域。

多轮录入由模型结合完整历史、当前草稿、对象引用和检索结果判断承接、补充、更正或新话题，通过通用结构表达。服务端检查任务来源与版本；中途查询保留待确认草稿，保存后保留对象引用以便继续补关系。必要歧义才追问，澄清输出经模型必要性复核后展示，已明确的信息不重复询问。名称与编号独立保存；仅有编号时名称留空，界面用编号显示。角色增加不自动触发实体类型更正。

多轮预览显示相关用户原文，每条事实分别保留支持它的消息与摘录，例如金额来自第一轮、币种来自后续回复。预览仍需人工确认；修改、取消或替换任务后，旧预览会失效。任务来源最多 24 条用户消息、合计 48000 字；提案最多 40 个对象、100 条事实，超限明确报错。新任务只以本轮原文提出新变更；继续草稿才可沿用当前任务来源。升级保留聊天和规范记录，旧版预览需重新生成。

顶部与“对象浏览”“统计与复核”同级的 **“星状图”** 标签使用 G6 全图力导向布局。打开即显示当前范围全部未归档对象及当前关系，包括孤立对象，无需选择中心。节点按类型着色，待确认对象有虚线和文字标记；有效关系用实线，待确认或受依赖阻塞的关系用虚线，箭头表示方向。支持拖动画布/节点、缩放、适配全图和重新布局。点击节点查看属性，点击关系查看状态与来源，再按需打开对象浏览。下方提供完整对象/关系清单，支持键盘操作。归档对象及已撤回/被更正的关系不进入当前图。

`GET /api/graph` 在一次数据库事务中读取同一版本的完整规范图，返回 `nodes / edges / node_count / edge_count / revision / complete`，没有相关性采样或原有分页截断。图使用固定 G6 5.1.1 本地资源 `web/vendor/g6-5.1.1.min.js`，许可证在同目录；运行时不请求外部 CDN。

模糊金额、推测存在、身份未确认的对象会保留待确认状态。点击保存确认的是“记录这句不确定表述”，不会把不确定内容自动变成精确事实。

可选加载三份已确认的结构化示例（明确绕过自然语言提取，仅用于界面演示）：

```bash
.venv/bin/python scripts/seed_demo.py
```

示例预期：合同 3 份，审核中 2 份，甲公司 2 / 乙公司 1，人民币合计 600000 元。再明确更正 A 为 250000 元并确认后，合计 650000 元，数量仍为 3。

当前详细方案见 [Pydantic AI＋Graphiti 实施说明](docs/pydantic-ai-graphiti-implementation.md)，包含通用接口、澄清规则、三项修订和验收场景。

## 数据与 Graphiti 边界

同一 Demo 专用 Neo4j 中，规范记录使用独立 `KD*` 标签。对象身份、事实状态、来源、冻结预览、更正链、投影任务和查询审计均持久化；不是进程内临时数据。

- **对话与提案**：Pydantic AI 主 Agent 理解上下文、调用规范对象和记忆工具，生成带来源的 ChangeProposal。权限、数据范围、schema、来源、版本和确认由服务端校验，不编写特定话术分支。
- **确认记录**：应用将已审阅草稿转为原生延迟工具调用；只有框架确认和服务端授权都通过，才能在事务内写 `KDObject` / `KDFact`。对象编号按“类型 + 适用范围 + 编号”确定身份；名称和语义相似性只是候选，不自动证明相同。
- **统计与浏览**：受控的参数化 Cypher 只读规范标签，COUNT/LIST/GROUP/SUM 不读取 Graphiti 搜索命中。关系支持单值归属和多值关联；分组支持入向与出向关系。多归属分组单列未确定，避免重复分配。
- **记忆检索**：GraphitiAdapter 先按已确认 episode 限制可检索关系 UUID，返回后再次校验来源，映射到规范对象当前快照；没有索引与检索故障显式区分。
- **语义投影**：确认后持久化待投影任务，后台调用 Graphiti `add_episode`，复用其去重、时序事实与语义索引。失败显示“已保存，索引待更新”，后台重试；不撤销规范事实。
- **完整性**：分页按稳定 ID/明确排序执行，返回总量、已返回数量和是否有后续页。后续页必须复用同一数据版本；期间发生写入则拒绝继续拼接，要求重启遍历。

同库标签不是数据库权限隔离。本 Demo 不提供 Graphiti 清库/重建操作；不能对这个混合数据库调用 Graphiti 的全库删除工具。上游搜索仍是相关性检索，不承诺完整；完整性承诺只适用于规范记录的确定性枚举与查询。

## 验收

数据库验收使用单独的一次性容器 `atlas-knowledge-demo-test-neo4j`，仅绑定 `127.0.0.1:17694`，与 Demo 和现有业务数据库隔离。脚本核对已有测试容器的镜像和端口后才复用，不清空其他数据库。

```bash
# 生成/查看独立 CSV 真值表：105 个对象，含同名不同编号
.venv/bin/python scripts/prepare_data.py

# 真实 Neo4j：确认、更正、幂等、同名、别名、精确统计与分页完整性
bash scripts/verify.sh

# 加入真实主 Agent、DeepSeek 和 Graphiti 投影（会产生 API 调用）
bash scripts/verify.sh --natural --projection --model-env /path/to/model.env

# 对照本次审阅的实际 Graphiti 源码
.venv/bin/python scripts/source_audit.py

# 真实模型对查询结果的语言表达（固定结果夹具，不新增业务对象）
.venv/bin/python scripts/verify_query_answers.py --model-env /path/to/model.env

# 先运行 verify.sh 启动专用测试库，再验收主 Agent、硬约束和记忆检索
.venv/bin/python scripts/verify_agent.py --repeat 5 --model-env /path/to/model.env
.venv/bin/python scripts/verify_guards.py
.venv/bin/python scripts/verify_memory.py --model-env /path/to/model.env
```

已有环境同样可通过 `KD_PYTHON=/path/to/python` 指定。Windows 无 WSL/Docker 时不能用假数据库代替通过验收。

运行结果写入 `artifacts/acceptance.json`、`acceptance-natural.json`、`natural-trace.json`。固定交付记录见 [验收记录](docs/validation.md)。CSV 真值表见 [fixtures/truth.csv](fixtures/truth.csv)。

可选浏览器验收需要 Node.js、Playwright 与本机 Edge，先加载三条示例并启动服务，然后执行 `node scripts/verify_ui.cjs`。它检查真实页面的对象浏览、来源、自然语言统计、复核、移动端布局并生成截图，未拦截或模拟 HTTP 请求。

旧的 `scripts/verify_star_ui.cjs` 和 [星状图检查报告](docs/star-ui-report.json) 记录原中心对象/分页模式，属于历史验收。当前 G6 全图的核对范围和结果见 [验证记录](docs/validation.md)。

## 结构与实现说明

| 路径 | 职责 |
| --- | --- |
| `demo/model.py` / `contracts.py` | 规范 schema、十进制、语义提案与执行协议 |
| `demo/agent_runtime.py` | Pydantic AI 主 Agent、工具、历史和延迟确认 |
| `demo/change_service.py` / `execution_store.py` | 提案校验、会话、授权和变更审计 |
| `demo/store.py` | Neo4j 事务、身份候选、确认、更正、完整查询与审计 |
| `demo/graphiti_extension.py` | Graphiti 客户端和确认后投影（检索见 graphiti_adapter.py） |
| `demo/service.py` | 类型化 Agent 结果、澄清、预览、查询和确认协调 |
| `demo/__main__.py` | 本地 HTTP API、静态页面、后台任务与显式错误 |
| `web/` | 对话、预览、浏览、统计与来源复核单页 |
| `scripts/` / `fixtures/` | 启动、示例、独立真值、真实数据库/模型/浏览器验收 |
| `docs/` | 源码核查、设计决策、验收结果与限制 |

确认事务、冻结版本、来源、幂等及定点数值方案参考并改编自既有 `atlasai_frappe_app/atlasai/knowledge/model.py`、`store.py`。没有复制 Frappe 身份、权限或业务入口；旧项目保持原样。新增完整类型发现、双向邻居分页、原文来源分页、审计成员清单、关系方向与多值基数。

Graphiti 上游文件未直接修改。本次增加的是仓库维护的 Graphiti 扩展模块，使用固定版本的原生投影/检索接口；源码位置、版本、哈希及原因见 [源码核查与决策](docs/architecture.md)。

## 已知边界

- 自然语言提取、类型/谓词命名、对象消歧仍可能出错，必须核对预览；并不保证任意开放业务 100% 正确。
- 每个数值/文本属性只允许一个已确认当前值；多值关联支持新增。已有多个目标时，复杂关系替换仍需进一步明确旧关系，不能静默替换全部。
- 标识按明确命名空间比较；对象名称、别名、近义谓词不能自动证明同一性。可提出名称/编号/范围更正和事实撤回，经预览确认；暂不提供对象合并或物理删除。
- 初始业务目录最多 100 个对象、类型、谓词和关系签名，标记截断；Agent 可再调用候选查找和对象详情。完整统计和浏览不受此上下文上限影响。
- 每页最多 100 条；深度最多 8，逐层枚举直接关系。频繁写入会使旧分页失效，第一版不保留跨版本快照。
- 当前查询为记录审计会保存参与对象 ID 和排除原因，大图时审计/聚合内存与 offset 分页仍需优化；验证范围为 105 个合同及关联对象。
- 数值最多六位小数，单值绝对值不超过十亿；合计超安全整数范围明确拒绝。不同币种只按指定币种分别汇总，无自动汇率换算。
- 不含文件全文解析、语音、原生 App、多人权限、Frappe 集成、审批、流程执行或工作流引擎。
# 当前工作流（2026-10-20）

输入信息在解析和校验通过后自动保存为待确认，可普通查询、浏览并显示于星状图；确认前不参与精准统计。右侧“待确认记录”支持多个持久化批次，按选中范围确认、修改或撤回。待确认更正不会提前覆盖原已确认值。

Graphiti 已确认与待确认记忆使用两个独立 Neo4j 实例及卷。`docker compose up -d --wait` 启动两者；待确认 Bolt 端口为 17695，配置项为 `KD_PENDING_NEO4J_URI`。语义投影异步执行，失败会显示状态和重试入口。

详细状态、接口、迁移和验收说明见 [自动待确认实现说明](docs/auto-pending-implementation.md)。本文较早的手工预览流程保留为演进记录，以本节及新说明为当前实现。

