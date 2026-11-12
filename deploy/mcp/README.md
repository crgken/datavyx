# MCP 候选与正式发布操作边界

M00–M09扩展：当前实现有12工具，新scope为update/delete/restore/history/purge/bulk；实际候选状态看扩展进度。普通业务直接执行无审批；开发与正式切换授权边界保留。MCP_RECORDING_ENABLED屏障覆盖所有新写工具。

永久清除清单必须持久保存在数据库卷/不可变发布目录之外：候选`/var/lib/atlasai-mcp/candidate-purge-ledger`，正式`/var/lib/atlasai-mcp/purge-ledger`。服务账号ubuntu、目录0700/文件0600，systemd只开放对应ReadWritePaths；MCP_PURGE_LEDGER_DIR必须显式匹配。升级/回滚不得覆盖或清空该目录。首次升级旧候选安装器前先创建目录并更新受保护配置；后续新安装器会准备目录。

MCP_PURGE_LEDGER_SECRET为独立持久签名密钥（至少32字符，repr/日志不显示），应与受保护MCP配置一起备份到数据库卷之外；不能跟随MCP_CURSOR_SECRET轮换。已存在清单后不得直接替换此密钥，须在受控维护中处理旧签名/幂等指纹兼容再轮换。开发者可自动生成，不要求用户提供模型key。

`python -m scripts.mcp_erasure_recovery --dataset EXACT --action audit|reconcile|apply`为管理恢复入口，不是MCP工具。reconcile核对DB提交标记、至少120秒间隔及无活动事务后才能将未知意图判为aborted；apply拒绝prepared并重放committed清单。正式reconcile/apply要求授权commit与ATLASAI_RELEASE_AUTHORIZED_COMMIT一致、可信安装清单、正式库17693、关闭新增和停旧API。DB备份必须同时记录外部清单边界；恢复后先处理清单再开放任何知识读取。删除/清除后不能切回不理解生命周期的v1候选或旧卷来覆盖数据。

这是新的 MCP 专用发布入口。旧 deploy-lightsail.ps1 / atlasai-apply-release.sh 继续保留历史用途，本次不调用；不构建 Flutter，不重新处理网页资产，不安装旧 requirements 或启动旧 API。

## 候选锁定

完成规定测试 → 提交 → 推送 codex/mcp-neo4j → 核对本地 HEAD 与 origin 分支 → 创建一次候选包：

```powershell
python deploy/mcp/release_artifact.py build --root . --remote-ref origin/codex/mcp-neo4j --output knowledge-management-demo/.mcp-artifacts/release-COMMIT.tar
```

输出 commit、整个包 SHA-256 和文件数。包内 MCP-MANIFEST.json 逐项固定路径、大小和 SHA-256。只收录 MCP 后端/测试/脚本/独立锁与本目录；不收录旧 demo、Flutter、数据库、真实配置或密钥。工作区必须干净且已经推送，不得从未提交内容打包。

上传同一个包至 /home/ubuntu/atlasai-deploy/mcp/。服务器先用可信校验器 verify；解包拒绝路径穿越、链接、重复文件、额外文件、成员篡改、错误 commit、非空目标。完整包 hash 必须来自本地记录，不能仅相信包里自行声明的 hash。

## 隔离安装和认证

1. 先从已有一致性备份恢复到单独 Neo4j 测试容器，仅绑定 127.0.0.1:17694。不复制运行中的卷，不停生产 17693/17695。记录备份时点/版本及差异，时效不足明确报告。
2. 新 Auth0 API Identifier 为 https://kb.datavyx.com/mcp-candidate，RS256，scope 为 knowledge:read / record / update / delete / restore / history / purge / bulk（每项均有knowledge:前缀）。独立 ChatGPT OAuth 客户端采用授权码 + S256 PKCE；回调从 ChatGPT 管理页面精确复制。Client ID 可作为公开配置，client secret 仅存身份提供方和 ChatGPT 受保护配置，不放服务端包。删除、恢复和清除工具的OAuth声明包含条件批量scope，避免客户端只按静态声明补授权时漏掉bulk；实际能否批量仍由token与当前空间角色、范围共同判定，普通读取不要求写权限。
3. 服务器 /etc/atlasai/mcp-candidate.env 使用例子，root:root/0600，MCP_MODE=test、数据库 17694、服务 8098、resource/audience 相同，公开客户端 allowlist 明确。run_env.py 不执行 shell、不继承模型变量、不打印配置内容；拒绝候选误指生产。
4. 执行 `sudo bash install-candidate.sh ARCHIVE SHA256 COMMIT`，仅创建不可变 candidate 目录和独立 venv，显式迁移测试库，安装候选 systemd 服务并检查新 ready。不会安装网关、停旧服务或执行生产迁移；失败只恢复旧候选指针。
5. nginx-candidate.conf 只增加候选及其发现文档两个 location，在现有 HTTPS server 内审阅加入、nginx -t 后 reload。保留旧网页/API；匿名知识调用必须 401，元数据可公开。
6. 本机健康/401 不代替真实 Auth0/ChatGPT。真实客户端需完成上下文、候选、直接保存、查询、来源及同键回执恢复，并单独记录。

再次安装相同 commit 会先检查已装清单，复用相同源码和已完成的依赖环境；失败的依赖下载可用同一包继续。发现源码漂移直接拒绝。restore-candidate.sh 仅切回已经验证的旧候选并复核健康，不改生产或数据库。依赖、运行代码或必要模板改变才生成新的 Git 候选。

M07后新包必须data_contract=2并包含生命周期及清除模块；旧七工具包不再是可恢复目标，安装健康失败也不自动退回旧代代码。后续进度/报告提交若仅改变文档，不重新打包：使用`release_artifact.py compare --root . --ref origin/codex/mcp-neo4j --archive ARCHIVE --sha256 SHA --commit CANDIDATE`核对运行文件集合与全部hash，正式合并后再对origin/main执行相同比较。发布授权和脚本绑定已验证候选运行commit，同时记录main文档commit映射；若任何运行文件变化必须重新验证和锁定候选。

## 历史兼容与恢复

在已加载显式 MCP 配置的隔离副本内：

```bash
python -m scripts.mcp_legacy_compatibility --dataset EXACT_DATASET
python -m scripts.mcp_legacy_compatibility --dataset EXACT_DATASET --apply --expected-fingerprint AUDIT_HASH
```

audit 仅输出计数、状态、兼容阻塞和指纹，无原文、token 或用户名称。apply 在数据锁中核对最新指纹，仅补齐可确定的 number_scaled；不改变 ID、事实内容、原文、成员、旧归档/撤回/提案状态。数值不一致、缺来源、孤立关联、未分类旧待确认记录必须先调查，不能自动激活或重抽消息。

逻辑副本 export_snapshot 要求调用方先冻结**副本**写入；restore_test_snapshot 只能写本机 17694 的空目标数据集，拒绝覆盖和跨空间 ID。它用于隔离演练，不替代正式库的离线卷备份。正式恢复必须使用已有经演练的备份方式。

若既有备份过旧，`export_current_copy.py` 可用旧写入链的 KDData 节点锁，在最多 10 秒且最终始终 rollback 的事务中导出当前规范图谱；同时锁定空间/成员并检查新增绑定。它不调用旧 Store/Agent、不停服务、不提交线上属性变化。并发写入只在短导出期间等待，结果落在受保护目录；此逻辑验证副本仍不能代替正式切换时的最终离线备份。执行前必须先在隔离库验证源属性完全保持及并发写入等待边界，记录实际锁定时长。

## 正式发布（尚未授权执行）

U15/U16 全部通过后，先提交具体发布报告。用户正式发布后再合并/推送 main 并核对候选运行清单一致；不用旧发布脚本。需要执行的固定顺序：

1. 核验 main、包 hash、配置、备份和实测恢复记录；先保护旧入口在途请求，建立旧写入屏障并停相关 worker。
2. 按已核验方式作最终一致性备份；仅备份方法需要时才在明确窗口短暂停库。保留旧代码、配置、网关和 volume，不删容器或数据。
3. 把已经演练的候选准备为 /opt/atlasai-mcp/current，使用 atlasai-mcp.service（8099）、独立 /etc/atlasai/mcp.env，resource/audience 为正式 /mcp。首次默认 MCP_RECORDING_ENABLED=0，MCP_RECORDING_ACTORS 只允许限定验证账号。
4. 对正式规范库运行已演练的显式 schema/兼容迁移；验证来源/ID/状态/统计差异后，限制开放完成最小闭环。
5. 使用 nginx-production.conf 的切换逻辑**替换重叠旧 location**，不能直接叠加。停止旧自然语言/语音/修改/确认 API 及 Graphiti worker；逐项验证旧接口不可用。静态迁移页通过网关提供，不构建前端。既有 Flutter service worker 缓存需要独立核对，后端 410 始终阻止旧写入。
6. 检查通过后才开启 MCP_RECORDING_ENABLED=1，重启 MCP 服务，复核真实客户端及日志。测试/候选数据库不作为正式业务库。

开放新写入前失败：保持屏障，按备份恢复旧代码/配置/必要数据，网关与旧服务恢复属于明确回滚，不是新 MCP 的模型降级。开放新写入后失败：先保护新记录和回执，优先前向修复/兼容代码回退；禁止用旧卷覆盖新记录。旧语义卷永久清理另行授权。

以上生产步骤需在 U15 的实际服务/备份盘点基础上形成精确发布清单；本目录模板不会自动授权生产切换。

`switch-production.sh` 为 U17 授权后的分阶段操作入口，必须显式给出相同 commit 的授权标记和已存在的最终备份目录。prepare 要求旧请求已排空、旧 API 停止、正式新增关闭；activate 只安装人工审阅并校验 hash 的完整 nginx 文件；open 在开启新增前落地不可回滚标记；rollback 只允许在该标记不存在时恢复旧路由/API，且不会还原数据库。该脚本从不操作 Docker 容器/卷或删除数据。

本次服务器实际盘点只有旧 atlasai-knowledge-api.service 和 atlasai-knowledge-db.service；旧 Graphiti 在 API 进程内运行，停 API 会停止这一运行入口。DB 服务继续保留，不能把它当 worker 停掉。U18 还须复核是否出现新的独立 worker/手动进程，不能凭旧盘点跳过检查。
