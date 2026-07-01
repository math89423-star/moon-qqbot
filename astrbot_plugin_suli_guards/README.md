# 粟藜 · 注入拦截与滥用检测

纯库插件。LLM 调用前的安全防护层。

## 模块

- **InjectionGuard** — 正则 + 规则引擎，检测 prompt 注入 / jailbreak / 越狱
- **AbuseGuard** — 滥用行为检测，频率限制，拒绝服务防护
- **BotDetector** — 可疑 bot 识别，行为建模，社交信号分析
- **PeerIsolation** — 双 bot 环境消息隔离，防止交叉污染
- **DualBot** — 双 bot 协调刹车机制（回合计数 / token 抢占）

## 安全等级

- weight ≥ 9：D4 硬线，即时拦截，管理员不豁免
- weight < 9：累积评分，过阈值后仲裁

