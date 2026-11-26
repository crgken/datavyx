# Datavyx 公开插件准备记录

日期：2026-11-26。状态：本地草稿准备中，未上传、未提交审核、未公开发布；不是可直接提交的完整包。

## 已确认的发布决定

- 用户授权面向外部用户和个人账号准备统一公开插件、预配置OAuth客户端。
- 发布者名称按用户确认填写Datavyx, LLC；Developer Portal实际已验证身份尚未读取，提交前必须选择并核对该身份，不以包字段代替身份验证。
- 首发覆盖所有平台允许地区；publication.countries=[]明确不加额外地区限制，不代表承诺覆盖平台禁止地区。
- 当前没有付款、订阅购买或结账流程，commerce=false。未来可能在官网提供购买，但不属于本次能力；上线付费前另行更新政策、申报及审核材料。
- 正式服务沿用https://kb.datavyx.com/mcp，复用Auth0的AtlasAI ChatGPT Candidate（公开Client ID 7CoGooGTzCivdedhDG2P2sjhGbcQJZo1），不新增Auth0应用、不删除已有客户端。已授权补充后端账户关闭与保留管理，不改变11工具/七权限，不重建前端。
- 注册方式provided，不走DCR。最终用户安装同一公开插件并使用自己的账号登录授权，按身份和空间权限隔离。Secret只由发布者填入Portal受保护配置，不进Git、包、录屏或聊天。

## 草稿文件与范围

- datavyx/plugin.json：英文名称/介绍/三条示例、五正三负审核用例、发行说明、地区和当前无付款申报。
- datavyx/mcp.json：正式Streamable HTTP端点、provided客户端、七权限及公开OIDC声明。
- datavyx/assets/datavyx-logo.png：复用既有Datavyx图标，不重新设计或编辑图像。
- review-materials/：独立于上传包的审核执行记录、政策缺口和真实演示指导。
- 不加入技能、MCP Apps或新UI：现有11工具和服务端说明已覆盖已确认功能。
- 不把根目录.mcp.json视为公共插件配置，不复制本地凭据、账号绑定、.app.json或其他项目。

## 配置需要实测的边界

1. 发布者2026-11-26提供应用Credentials截图，Client Authentication Methods明确选中Client Secret (Post)；当前包已写client.tokenEndpointAuthMethod=client_secret_post。截图Secret保持遮挡，未读取、复制或写入包。应用实际方法与provider全局方法是不同证据，本次以该应用截图核验。
2. 复用的应用必须对正式/mcp API具备七项User-Delegated授权。实际callback从本次Portal管理页精确复制；仅在确有新地址时追加，不覆盖已有callback，也不配置通配符。
3. 发布者2026-11-26只读核验正式/mcp API：Maximum Access Token Lifetime=86400秒（最大24小时），Allow Offline Access=关闭；目前未具备已验证的refresh-token自动续期配置。不能把issuer全局声明offline_access当作这一个API已允许刷新。应用Refresh Token grant、rotation、闲置/最长寿命及实际客户端行为仍需核验；本轮没有修改这些认证设置，不承诺用户永不重连。
4. fixed-client开发验收、演示及最终Portal连接/扫描是不同阶段。先恢复一个使用既有客户端的开发连接，不需要新增DCR应用；每位外部用户不应再各自创建DCR连接。

## 公开资料核对

- 网站：https://www.datavyx.com/ 已读取并核对用途。首页确实标注Free，Pro/Team尚未开放；不把计划中的导出/团队管理计入本次公开能力。
- 新版privacy.html及独立support.html/terms.html已随官网main=a37ce6ab12379685b2388597f611385c8ea4227c公开；首页和三页全部HTTPS200、正文与该Git提交字节一致，sitemap同样核对通过。对应后端保留任务已先行启用，再发布政策。
- support@datavyx.com已确认同时处理支持和隐私请求，属于独立Google Workspace邮箱。当前Gmail连接为ken@datavyx.com，不能擅自代操作support邮箱；由该邮箱管理员按明确结案case执行保留流程。
- supportURL=https://www.datavyx.com/support.html、privacyPolicyURL=https://www.datavyx.com/privacy.html、termsOfServiceURL=https://www.datavyx.com/terms.html已在上述核验后写入plugin.json。

发布者已确认期限并授权实施：正常知识/历史与可恢复删除随账号存续保留；核验关闭后30天活动数据处理、备份30天轮转（通常核验起最长60天退出备份）；最小技术日志30天、必要支持工单结案后12日历月。正式/候选现运行3c37571，首轮每日任务、三个profile真实恢复、到期备份/日志检查通过；196项回归通过，没有关闭真实账号。实施与核验见docs/50-运行记录/Datavyx保留流程开发进度-2026-11-26.md。policy-drafts/是历史提案，现行静态源已按官网Git流程公开。

本地检查已重新生成datavyx-1.0.0-public-DRAFT.zip，SHA256=c481d997ae2c354a142181009dba7f62347f88a0815e044132a06db83245628a（仅3个包文件，图标789×789、457145字节、与原文件hash相同），四个公开页面校验通过；包无Secret/令牌/账号绑定。五正三负用例尚未运行。完整通用mcp.schema.json当前不接受OpenAI文档规定的server.extensions字段：主manifest和去除vendor扩展后的通用投影通过，OpenAI配置单独按文档字段检查；没有把通用投影通过伪称为完整schema/Portal导入通过。最终需目标平台导入核验，当前包不是可提交成品。

## 验收和发布门槛

- 本轮只做本地包检查、源契约核对与公开URL检查，不使用真实用户数据执行写操作。
- 五正三负用例为待执行草稿；所有新包实际ChatGPT审核案例当前Not run。早期正式后端验收记录不能冒充该插件版本的实测。
- 需要专用审核账号及合成测试空间；凭据只交给Portal安全字段，审核员不能依赖发布者邮箱验证码或私有网络。
- 需要真实演示录屏及可访问URL；脚本和官网示意动画不是实际演示。先验证开发连接，录制并回看，再设置demo_recording_url。
- 政策页面、图标、录屏、审核账号、OAuth方法/回调和实测案例完善后，才形成最终ZIP。上传草稿、提交审核、批准后公开发布是不同动作；当前均未执行。
- 当前已有.gitignore官网白名单和11-13开发报告改动不是本轮产生，保留且不混入后端提交。已授权本轮保留开发、验证、Git与静态页面准备；实际是否提交/部署以进度报告为准，公开上传/审核/发布仍未执行。

参考：OpenAI Plugins Authentication、Submission文档与本机plugin-creator的公开提交规范。当前准备不保证平台审核通过。
