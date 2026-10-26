# 知识助手 Flutter Web + PWA

独立网页子项目，页面和业务操作以 `../knowledge-mobile` 为参考。复用现有 8096 知识后端、Auth0 租户与用户、工作空间和 Azure Speech，不依赖 Frappe。

## 本地启动

1. 复制 `web-config.example.json` 为 `web-config.json`，填入公开的 Auth0 SPA Client ID。不要填写 Client Secret、Azure Key 或用户令牌。
2. 安装依赖并构建：

```powershell
flutter pub get
npm ci
flutter build web --release --no-web-resources-cdn --dart-define-from-file=web-config.json
npm run pwa
python tools/serve.py
```

打开 **http://localhost:8097**。现有后端需运行在 `127.0.0.1:8096`。本地代理只开放 `/api/mobile/session`、`/api/mobile/execute`、`/api/mobile/transcribe`、`/api/mobile/logout`，不代理旧网页管理接口。服务地址不由用户选择。

## Auth0 配置

- 应用类型：Single Page Application，和安卓 Native 应用分开注册。
- Callback URL、Logout URL、Allowed Web Origins：均为 `http://localhost:8097`。
- 使用现有 API Audience：`https://atlasai-knowledge-api`，授予 User-Delegated Access。
- 启用原有账号数据库连接、Refresh Token Rotation；API 开启 Allow Offline Access。
- 后端 `KD_AUTH0_CLIENT_IDS` 同时保留安卓和网页 Client ID；`KD_WEB_ALLOWED_ORIGINS` 包含本地网页来源。
- Auth0 SDK 在内存中管理凭据。重新加载后的登录恢复仍受 Auth0 会话及浏览器隐私策略影响，不保证离线登录。
- 身份、工作空间成员关系和操作权限均由现有后端验证；前端登录成功不直接授予业务权限。

## 录音与 PWA

- 录音在浏览器内采集为 WAV，显示实时振幅及 60 秒倒计时；识别语言跟随简中/英文设置。
- 录音通过受保护的后端接口送入 Azure SDK，密钥不进入网页。识别文字可编辑后发送。
- Service Worker 只预缓存静态资源，不缓存 API、录音或令牌；有新版本时由用户选择更新，录音和业务请求期间不刷新页面。
- 支持安装提示及手动“添加到主屏幕”说明；正式部署需 HTTPS、同源 API 反向代理和对应的 Auth0 HTTPS 地址配置。
- 本地 localhost 的麦克风权限不能代替真机 HTTPS 验证；iOS Safari 安装和录音仍需分别验收。

## 验证记录与边界

- Flutter 静态分析、Web release 构建和 Workbox 静态缓存构建通过。
- 本地页面返回 200；未登录访问会话接口返回 401；旧管理接口不通过网页代理开放。
- 独立 Edge 浏览器使用受控合成音频作为麦克风输入，生产录音控制器的中文、英文采集均通过：16kHz、单声道、16-bit WAV，波形随输入变化。
- 采集后的两份 WAV 通过既有后端 Azure Speech SDK 成功识别。这是实际云服务验证，但不等于真实用户登录后的完整浏览器请求链路已通过。
- 真实 Auth0 浏览器登录及登录后的业务/语音请求，需在控制台配置完成后由用户登录验证；未在此处记录为通过。
- `test_support/audio_probe.dart`、`tools/run-media-probe.cjs`、`tools/audio_collector.py` 仅用于独立验证页，不进入正式入口。验证输出位于忽略提交的 `artifacts/`。
- Windows 下 Flutter 浏览器测试运行器存在本地 CanvasKit 路径加载问题，因此录音验证采用独立 release 验证页；不将挂起的测试报告为通过。
