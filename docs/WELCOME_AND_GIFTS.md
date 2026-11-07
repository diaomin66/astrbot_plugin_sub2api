# v0.4.0 入群欢迎与首绑新人礼

## 自定义欢迎消息

AstrBot → 插件 → Sub2API 账号与互动 → 配置 → `welcome`：

- `enabled`：开启真实入群事件欢迎。新安装默认关闭，已有部署可按需开启。
- `template`：欢迎语，最多 1600 字符；`{at}` 为平台认证事件中的新成员 @，`{site_url}` 为已配置站点。
- 不支持 Python 表达式、属性访问或格式化运算；字面大括号使用 `{{` 和 `}}`。
- 仅监听 QQ 官方 `GROUP_MEMBER_ADD`，订阅 `GROUP_MEMBER_EVENT (1 << 24)`。
  不把机器人加群、频道成员加入或首次发言伪装为新人进群。
- 当前欢迎实现针对 `qq_official` WebSocket + AstrBot 4.29.1 已安装的 ManagedBotWebSocket；
  `qq_official_webhook` 保留全部原有命令，但此欢迎监听尚不支持 Webhook。
- QQ 开放平台必须允许群成员事件和群消息发送，相关权限/频控由 QQ 决定。
  新增 Intent 后现有 QQ 连接需重新鉴权一次，插件只更新匹配 AppID/平台 ID 的连接，不重启 AstrBot 容器。
- 旧 SDK 不包含成员事件解析器时，仅在目标连接上补接解析器，卸载时还原，其他机器人不受影响。
- `data/plugin_data/astrbot_plugin_sub2api/welcomes.sqlite3` 持久化事件去重；忽略过期/异常事件。
  发送前先预留去重键，超时结果不确定时不自动重发，避免重载刷屏。真实退群后再次入群会产生新事件。
- `{at}` 渲染为 QQ 官方 `<qqbot-at-user id="..." />`，不猜测真实 QQ 号或昵称。

官方事件定义（2026-11-07 核对）：
https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html

示例模板：

```text
🎉 欢迎 {at} 加入小金库！
🌐 站点：{site_url}
🔗 /绑定 你的站内邮箱 → 到站内亲自确认
🎁 首次确认绑定的站内账号获 $2 新人礼，每个账号仅一次，历史绑定不补发。
📅 /签到 领惊喜，📊 /战绩 看战果，/额度帮助 查看玩法！
```

## 首次绑定自动送 $2

**必须同步升级配套 bridge 后端。插件自己不能发钱，修改欢迎文案也不会开启赠送。**

- 后端 `binding_gift_enabled=true` 时，在站内本人验证确认绑定的同一数据库事务中创建赠送资格。
- 固定增加 Sub2API USD 余额 `2.000000`；不是人民币换算，也不是 token 数。
- 按站内账号去重，群聊/私聊共用资格；历史绑定（含已解绑）不补发，解绑、重绑、重启不重复领。
- 只提交 `/绑定`、错误确认码、拒绝绑定、过期申请均不会发放。
- 成功确认后的后台任务通常在约 20 秒内处理；遇到账号未决交易/风控需等待。
- 管理员 API 加款，令牌只在后端；不直接 UPDATE 用户余额，也不绕过账号状态和返佣安全检查。
- 采用已有持久化 credit 流程，外部 API 超时只核对准确流水，不重新加款。
- 专用 `qq_bridge.binding_gifts` 表记录资格与 operation；operations.operation 和 ledger.kind
  分别为 `binding_gift`、`admin_binding_gift`。该固定赠送独立于每日签到随机额度预算。
- 多个不同站内账号属于不同资格；没有声称可以跨账号识别现实中的同一自然人。
- `GET` 绑定确认页面不会触发赠送，插件 `/我的账号` 或 `/绑定状态` 展示 `processing`、`credited` 或需核对状态。
- 确认绑定本身和礼金入账是两个阶段；未确认的资金绝不显示为已到账。
- 新版 account 可选字段：`binding_gift: {"state":"processing|credited|review_required|failed","amount":"2.000000"}`。
  不存在资格时省略；旧版后端缺失该字段仍兼容。

## 验收与回滚

自动化使用模拟 QQ API 和独立临时 PostgreSQL，不给生产账号试充值。
上线后由本人邀请新人真实进群，并由该新人自行确认首次绑定完成端到端验收。
禁用欢迎只需关闭 `welcome.enabled` 并重载；关闭后台礼金开关会暂停排队，已经发送的未决流水仍应对账。
**不要删除赠送资格、流水、去重表来重试**，这会破坏防重复保障。
