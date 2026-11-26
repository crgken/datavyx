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

1. 当前包没有声明tokenEndpointAuthMethod，因为尚未只读核验这一个Auth0应用的实际方法；该字段允许省略。正式连接前从应用Credentials核对并按实际情况补齐，不能把issuer公布的全局方法当作单应用配置。
2. 复用的应用必须对正式/mcp API具备七项User-Delegated授权。实际callback从本次Portal管理页精确复制；仅在确有新地址时追加，不覆盖已有callback，也不配置通配符。
3. offline_access已被issuer发现文档声明；API Allow Offline Access、该应用Refresh Token grant与实际刷新寿命尚未核验。包声明不等于刷新已生效，不承诺用户永不重连。
4. fixed-client开发验收、演示及最终Portal连接/扫描是不同阶段。先恢复一个使用既有客户端的开发连接，不需要新增DCR应用；每位外部用户不应再各自创建DCR连接。

## 公开资料核对

- 网站：https://www.datavyx.com/ 已读取并核对用途。首页确实标注Free，Pro/Team尚未开放；不把计划中的导出/团队管理计入本次公开能力。
- 旧版privacy.html实际GET200且内容已读取，但缺少完整MCP保存/删除/备份披露；本轮补齐静态新版及独立support.html/terms.html，实际部署前不写包URL。
- support@datavyx.com已确认同时处理支持和隐私请求，属于独立Google Workspace邮箱。当前Gmail连接为ken@datavyx.com，不能擅自代操作support邮箱；由该邮箱管理员按明确结案case执行保留流程。
- supportURL、privacyPolicyURL、termsOfServiceURL需等生产流程启用和三页公开200/正文/hash核验后写入，不把猜测URL或本地草稿标为已发布。

发布者已确认期限并授权实施：正常知识/历史与可恢复删除随账号存续保留；核验关闭后30天活动数据处理、备份30天轮转（通常核验起最长60天退出备份）；最小技术日志30天、必要支持工单结案后12日历月。实施与核验见docs/50-运行记录/Datavyx保留流程开发进度-2026-11-26.md。policy-drafts/是历史提案；当前采用的静态源在website/及官网独立Git仓库中。方案确认不等于生产启用，不提前发布保留期限承诺。

本地检查已生成datavyx-1.0.0-public-DRAFT.zip（仅3个包文件，图标789×789、457145字节、与原文件hash相同），五正三负用例尚未运行。官网及privacy.html实际GET均200且内容已读取；隐私条款的公开MCP覆盖仍不完整。完整通用mcp.schema.json当前不接受OpenAI文档规定的server.extensions字段：主manifest和去除vendor扩展后的通用投影通过，OpenAI配置单独按文档字段检查；没有把通用投影通过伪称为完整schema/Portal导入通过。最终需目标平台导入核验，当前包不是可提交成品。

## 验收和发布门槛

- 本轮只做本地包检查、源契约核对与公开URL检查，不使用真实用户数据执行写操作。
- 五正三负用例为待执行草稿；所有新包实际ChatGPT审核案例当前Not run。早期正式后端验收记录不能冒充该插件版本的实测。
- 需要专用审核账号及合成测试空间；凭据只交给Portal安全字段，审核员不能依赖发布者邮箱验证码或私有网络。
- 需要真实演示录屏及可访问URL；脚本和官网示意动画不是实际演示。先验证开发连接，录制并回看，再设置demo_recording_url。
- 政策页面、图标、录屏、审核账号、OAuth方法/回调和实测案例完善后，才形成最终ZIP。上传草稿、提交审核、批准后公开发布是不同动作；当前均未执行。
- 当前已有.gitignore官网白名单和11-13开发报告改动不是本轮产生，保留且不混入后端提交。已授权本轮保留开发、验证、Git与静态页面准备；实际是否提交/部署以进度报告为准，公开上传/审核/发布仍未执行。

参考：OpenAI Plugins Authentication、Submission文档与本机plugin-creator的公开提交规范。当前准备不保证平台审核通过。
