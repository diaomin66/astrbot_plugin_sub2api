# Sub2API QQ bridge v1 二开契约

本文定义本插件需要的**新接口**，不是原版 Sub2API API 文档。
插件仓库不包含或修改后端。本文描述完整接口及目标验收要求，**不等于生产验收结论**。
2026-11-07 后端交接仅报告绑定/查询发布；签到和打劫仍关闭。
最新站内入口为 `/profile` 的「管理 QQ 绑定」弹窗；旧 `/user/qq-bindings` 兼容跳转。
公网域名路由和生产 TLS 尚不能从 IP 浏览器测试推定通过，见 `HANDOFF_ALIGNMENT.md`。
二开仍须落实站内确认授权、事务、幂等、缓存一致性与限流。

## 1. 认证与共同字段

插件请求均为 POST JSON，地址由站点 `base_url + /api/v1/qq-bot + 路由` 拼接。

```http
Authorization: Bearer <专用 bridge 服务令牌>
Content-Type: application/json
Accept: application/json
X-Bridge-Version: 1
Idempotency-Key: <64 位 SHA-256 十六进制>
```

要求：

- 令牌至少 32 位随机字符，服务端保存安全摘要、可吊销轮换，并绑定允许的 QQ AppID。
- 令牌只可调用这四个 bridge 路由，不能调用通用管理员余额接口，更不能确认绑定。
- 服务端验证 `actor.app_id` 与令牌匹配；target 必须同 AppID、同 provider、同 scope。
- 生产要求 HTTPS；配置可信内网 HTTP 时承担令牌明文传输风险。
- 后端仍须按令牌、账号、身份、群及邮箱哈希限流。日志不得保存完整 Authorization、邮箱和确认码。
- QQ 原始消息和 mentions 由机器人适配器验证；bridge 令牌是此信任边界，不能发给普通用户。
- API 返回下面的专用 envelope，不要直接返回 Sub2API 其他接口的 envelope。

共同请求示例：

```json
{
  "actor": {
    "provider": "qq_official",
    "app_id": "123456",
    "scope": "qq_group",
    "openid": "member-openid"
  },
  "context": {
    "platform_id": "qq-main",
    "group_openid": "group-openid",
    "message_id": "original-qq-message-id"
  },
  "policy": {
    "binding_ttl_seconds": 600,
    "checkin": {
      "enabled": true,
      "timezone": "Asia/Shanghai",
      "reward": {"min": "0.10", "max": "0.50"}
    },
    "robbery": {
      "enabled": true,
      "success_probability": "0.40",
      "success_amount": {"min": "0.05", "max": "0.20"},
      "failure_amount": {"min": "0.01", "max": "0.05"},
      "cooldown_seconds": 3600,
      "shield_seconds": 7200
    }
  }
}
```

`scope` 只能为 `qq_group` 或 `qq_c2c`；C2C 的 `group_openid` 为 `""`。
OpenID 与 AppID 为 1～128 字符 `[A-Za-z0-9_-]`；message_id 是非空、最多 512 字符、
不含控制字符的字符串，不能假定 QQ message_id 只有字母数字。
身份唯一键为 `(provider, app_id, scope, openid)`，不要使用可修改的昵称或邮箱作为 QQ 身份。
`platform_id` 仅表示 AstrBot 实例，不能纳入经济账号键使重建实例获得新签到资格。

## 2. 金额、配置和服务端时钟

- 所有金额使用**十进制字符串**，不接受 JSON 浮点数。契约精度为 6 位小数，
  格式为 `(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,6})?`，无符号、无指数、不得为 NaN。
- 单位与当前站点的用户可用余额一致，不直接更改 token 额度、订阅日限额或支付记录。
- 后端用 DECIMAL 或整数微单位计算。随机金额按 `10^-6` 单位均匀抽取闭区间；
  若底层账本精度更低，必须拒绝不兼容配置，不能悄悄改变奖励精度。
- 若实际余额包含超过 6 位小数，展示用 `balance` 可向下截取到 6 位，不能改写存储余额；
  参与转账的可用余额也需保守取可转出的微单位数量，避免超扣。
- 验证上下限、概率 `0～1`、启用标志、时区和时长；签到/成功金额下限必须大于零，
  失败下限允许零；冷却/护盾范围为 `0～2592000` 秒，绑定有效期 `60～3600` 秒。
- 同一服务端经济域固定签到时区。改变时区应通过迁移/管理员受控变更，不能由一条请求
  任意切换时区获取两次签到。插件配置与服务端时区不匹配时返回 `POLICY_REJECTED`。
- 插件政策是可信服务客户端提交的意图，不是绕过额度控制的授权。服务端设置单笔上限、
  签到总预算、账号每日转入上限、接口频率、允许时区及启用开关。超限拒绝，不静默放宽。
- 两个 AstrBot 配置不要同时为同一账号经济域提交互相矛盾的政策；服务端统一管控。
- 所有日期、有效期、护盾和冷却以**数据库/服务端时间**为准，不能使用客户端提交日期。

## 3. 幂等与交易边界（上线前必须实现）

插件按如下 JSON 的 UTF-8 编码计算 SHA-256：

```python
identity = [1, action, actor, context.get("group_openid", ""), context["message_id"]]
canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
key = hashlib.sha256(canonical.encode()).hexdigest()
```

`action` 是 `bind`、`account`、`checkin`、`robbery`。政策和令牌不纳入键，因此消息重投、
改配置和令牌轮换不会把同一消息变成另一笔交易。原消息、actor 和目标必须来源于官方事件。

后端要求：

1. `/account` 为只读实时查询，不复用缓存幂等响应，以便显示最新状态。
2. 其他接口按 `(app_id, operation, idempotency_key)` 保存操作记录，并对业务消息身份
   `(app_id, operation, scope, openid, group_openid, message_id)` 再加唯一约束。
3. 对规范化业务参数保存摘要，包含 actor、context、email/target、policy；同键不同参数
   返回 `IDEMPOTENCY_CONFLICT`，不能在重投时按新政策重算随机结果。
4. 并发同键必须等待第一次事务提交，然后返回原结果；不得让两个请求各自抽奖或扣款。
5. **幂等结果、签到记录/冷却、余额变更、账本流水在同一数据库事务提交。**
   响应丢失时重发应读到原结果；不能先写余额、再单独写幂等缓存。
6. 随机结果在持锁且校验通过后只产生一次并持久化；重复请求不再次采样。
7. 金额操作去重记录与账本长期保留；如清理完整响应，至少保留去重墓碑，
   旧 QQ 消息不能因为 Redis TTL 过期或进程重启而再次执行。
8. 为避免延迟重投中的旧绑定申请重新生效，绑定申请同样持久化去重；重投不延长 TTL。
9. 事务失败必须整体回滚；不要仅依赖进程内锁、Python 字典、单机内存或短期 Redis 锁。
10. 与 Sub2API 的实际扣费/充值路径共享原子更新或行锁，并正确失效/更新余额缓存。
    网关缓存若未同步，账本正确也可能造成超额消费或签到后仍被拒绝。

插件最多对网络错误及 502/503/504 以相同字节和 key 重试一次，不重试业务错误。
账户查询实时；重放已提交的变更响应是历史快照，最新余额和冷却请再次查询。

## 4. 发起绑定

`POST /api/v1/qq-bot/bindings/request`

共同请求基础上加：

```json
{"email": "user@example.com"}
```

流程：

1. 按 Sub2API 自身的邮箱规范定位账号；插件仅检查常见 ASCII 邮箱格式，
   保留 local-part 大小写、规范化域名，不把邮箱命中当作账号所有权证明。
2. 已绑定身份拒绝覆盖并返回 `ALREADY_BOUND`；只能在已登录用户的站内设置中解绑/换绑。
3. 创建 pending 申请，关联请求的 QQ 身份、目标账号、群上下文、发起时间、到期时间。
4. 生成不可预测的 request_id 和至少 10 个随机 Base32 字符的确认码。
   插件为兼容性接受 6～32 位 `[A-Z0-9-]`；服务端上线策略应使用更强随机长度。
   保存码摘要，确认接口限制尝试次数。确认码不是登录 token。
5. 同 QQ 身份的新申请可在限流后使先前 pending 申请失效；不能覆盖 confirmed 绑定。
6. 不论邮箱是否存在，对外给出相同形状的 pending 回复，随机伪码/相同有效期，
   不泄露账号是否注册。不存在邮箱不能创建可确认的账号关联。
   `/account` 对尚未确认的申请也不能泄露邮箱是否存在，须保留无权限的 pending 状态或
   使用等价响应策略，不能通过查询状态绕过防枚举。
7. 未确认身份对签到、额度读取和打劫没有任何权限。

HTTP 200/202：

```json
{
  "ok": true,
  "data": {
    "state": "pending",
    "verification_code": "A7KD-2QPR-9W",
    "expires_in_seconds": 600
  }
}
```

### 站内确认 UI 与接口（你需要二开）

在 Sub2API `/profile` 个人设置的「管理 QQ 绑定」弹窗中，列出**当前登录用户自己**的 pending 申请。
2026-11-07 交接实现中，普通管理按钮不跳页，旧 `/user/qq-bindings` 链接转到
`/profile#qq-bindings` 并自动打开弹窗。插件不需要调用用户端路由或读取用户会话。
展示 AppID/机器人名称、QQ OpenID 的安全摘要、群上下文摘要、申请时间、剩余时间、
风险提示、确认/拒绝按钮；不要把机器人传入的昵称当作实名身份证明。

可采用以下新增用户端路由（插件不调用它们）：

```text
GET    /api/v1/user/qq-bindings/requests
POST   /api/v1/user/qq-bindings/requests/:request_id/confirm
POST   /api/v1/user/qq-bindings/requests/:request_id/reject
GET    /api/v1/user/qq-bindings
DELETE /api/v1/user/qq-bindings/:binding_id
```

confirm 请求：`{"verification_code": "A7KD-2QPR-9W"}`。
页面**不要显示或预填确认码**；由用户把自己发起绑定后机器人给出的码填入。
提示「只确认本人刚发起的申请；确认后这个 QQ 身份可操作你的额度，并参与群内打劫」。

确认事务必须：

- 验证 Sub2API 用户登录态/必要的二次认证及 CSRF/Origin 防护；拒绝机器人令牌确认。
- 按主键锁定申请，验证 `request.user_id == session.user_id`、状态 pending、未过期、
  尝试次数以及确认码摘要，不能相信请求体提供的 user_id/email。
- 唯一约束防止同 QQ 身份同时绑定两个账号；确认与解绑/换绑及经济操作采用一致的锁顺序。
- 为每个 `(user_id, provider, app_id, scope)` 最多保留一个有效身份，减少共享账号扩大打劫面。
  一个用户可以分别绑定群聊和私聊身份；这两者必须各自经过确认。
- 绑定和申请状态变更一起提交。过期申请不能确认，重复确认不授予新的关联。
- 解绑撤销关联和 pending，清理相关缓存，但不能清除用户原有签到、护盾或冷却记录。
- 站内拒绝不会建立绑定。全部记录可审计；不要因拒绝某申请泄露其他用户的身份。

## 5. 查询账号

`POST /api/v1/qq-bot/account`，请求为共同字段，按已确认绑定查找账号。
不允许客户端给任意 user_id 或邮箱读取余额。

未绑定：

```json
{"ok": true, "data": {"state": "unbound"}}
```

待确认：

```json
{"ok": true, "data": {"state": "pending", "expires_in_seconds": 180}}
```

已绑定：

```json
{
  "ok": true,
  "data": {
    "state": "bound",
    "balance": "12.500000",
    "checked_in_today": true,
    "shield_remaining_seconds": 2400,
    "robbery_cooldown_remaining_seconds": 900
  }
}
```

不返回完整邮箱、密码、用户 API Key 或管理员备注。
remaining 使用 `max(0, ceil(expires_at - server_now))`，必须为整数。

## 6. 每日签到

`POST /api/v1/qq-bot/checkin`，请求为共同字段。

同一事务中：

1. 校验令牌、策略、绑定已确认、账号启用及服务端签到预算。
2. 根据后端统一时区和当前时间计算业务日期。
3. 按 Sub2API `user_id + activity_date` 唯一，**不是按群、OpenID、AppID 或插件实例去重**。
4. 已签到返回 `already_checked_in`，不发钱；并发不同 QQ 消息也只能发放一次。
5. 用安全随机源在奖励范围生成整数微单位金额，原子增加可用余额，
   写入签到记录、奖励流水和幂等结果。预算检查也需原子执行。
6. 使用 Sub2API 余额服务/事务原语更新缓存一致性，不直接绕开现有账务流程。

成功：

```json
{"ok": true, "data": {"state": "credited", "reward": "0.237541", "balance": "12.737541"}}
```

当天重复：

```json
{"ok": true, "data": {"state": "already_checked_in", "balance": "12.737541"}}
```

## 7. 打劫

`POST /api/v1/qq-bot/robbery`。共同请求增加目标身份：

```json
{
  "target": {
    "provider": "qq_official",
    "app_id": "123456",
    "scope": "qq_group",
    "openid": "target-member-openid"
  }
}
```

服务端只接受双方均 `qq_group` 且有非空群上下文；目标来自机器人验证过的真实 @。
不从 QQ 号猜 OpenID，不按昵称找人。

事务规则（账号为全站 Sub2API user_id，不按群分开）：

1. 锁定并校验双方已确认绑定，防止解绑/换绑检查与扣款之间出现 TOCTOU。
2. 拒绝同身份或同 Sub2API 账号；校验双方账号启用、策略允许。
3. 将两个账号按 user_id 升序锁定，防止 A 抢 B 与 B 抢 A 的死锁。
   护盾/冷却行也按一致顺序锁定，所有余额写路径共用相同锁约定。
4. 检查发起者 `robbery_available_at <= now`、目标 `shield_until <= now`。
5. 检查目标可转账余额大于零、发起者余额不低于失败赔偿下限。
   失败赔偿下限为零允许零余额参与，这是明确的可配置玩法，不是无意漏检。
6. 在全部校验通过后，安全随机一次决定成功/失败：概率为 0 恒失败、1 恒成功。
7. 成功：从目标转给发起者 `min(random(success_range), target_available)`。
   失败：从发起者转给目标 `min(random(failure_range), actor_available)`。
   无论哪种结果，双方总余额在本事务内守恒，余额不得为负；可能因付款方余额不足
   低于配置下限，这是明确的余额封顶规则。不要扣走已被其他交易预留的额度。
8. 发起者冷却设为 `now + cooldown_seconds`；目标护盾设为 `now + shield_seconds`。
   成败均生效；校验被拒绝（未绑定、目标有护盾、余额不足等）不扣款、不触发新冷却。
9. 清除发起者自身护盾，避免带盾无限攻击；仅在有效打劫事务中清除。
10. 在同事务中写双方余额、成对账本流水、冷却/护盾、打劫结果及幂等记录。
    即使失败赔偿为零，也写操作结果和冷却/护盾，确保不能重新抽奖。
11. 更新/失效 Sub2API 消费路径的余额缓存，再提供一致读。

成功：

```json
{
  "ok": true,
  "data": {
    "outcome": "success",
    "amount": "0.120000",
    "balance": "12.857541",
    "robbery_cooldown_remaining_seconds": 3600,
    "target_shield_remaining_seconds": 7200
  }
}
```

失败仍然是业务成功 HTTP 200：同样字段，`outcome="failure"`，amount 为实际赔偿额。
目标受保护或发起者冷却应返回业务错误而不是伪造 `failure`，因为它们没有产生有效打劫。

## 8. 错误格式

```json
{
  "ok": false,
  "error": {
    "code": "TARGET_SHIELDED",
    "retry_after_seconds": 420
  }
}
```

建议使用 HTTP 409 表示业务冲突、429 表示限流、400/422 表示参数/策略不合法。
`retry_after_seconds` 可省略，默认 0；非负整数，最多 31536000。
错误 code 为不超过 64 字符的 `[A-Z_]+`。插件不显示服务端 message 字段。

| code | 含义 |
| --- | --- |
| `NOT_BOUND` / `BINDING_PENDING` | 发起者未绑定/未确认 |
| `ALREADY_BOUND` | 不允许邮箱请求覆盖现有绑定 |
| `TARGET_NOT_BOUND` | 目标未完成绑定 |
| `SAME_ACCOUNT` | 同一账号，包含不同 QQ 身份 |
| `TARGET_SHIELDED` | 目标有护盾，返回剩余秒数 |
| `ROBBERY_COOLDOWN` | 发起者在冷却，返回剩余秒数 |
| `INSUFFICIENT_BALANCE` | 发起者不能覆盖最低失败赔偿 |
| `TARGET_EMPTY` | 目标无可转出额度 |
| `ACCOUNT_DISABLED` | 账号不可用 |
| `FEATURE_DISABLED` | 服务端功能关闭 |
| `RATE_LIMITED` | 超过请求/预算限制 |
| `POLICY_REJECTED` | 超限、不兼容或不合法政策 |
| `IDEMPOTENCY_CONFLICT` | 同 key 但请求参数不同 |

未知错误只显示通用安全提示。不要用 HTTP 404 表示“邮箱不存在”；
插件把 404 解释为未安装 bridge。401/403 保留给服务令牌或 AppID 授权失败。

## 9. 持久化模型建议（需适配你的实际 ORM/数据库）

这不是可直接执行的数据库迁移；先检查你二开的 Sub2API 表结构和余额服务。

| 逻辑表 | 最少字段/约束 |
| --- | --- |
| binding_requests | request_id、actor 复合身份、目标 user_id（可空的防枚举占位）、code_hash、状态、expires_at、尝试次数、上下文 |
| qq_bindings | 复合身份唯一、已确认 user_id、confirmed_at；账号/AppID/scope 有效关联唯一 |
| qq_activity_state | user_id 唯一、robbery_available_at、shield_until |
| qq_checkins | `(user_id, activity_date)` 唯一、reward、operation_id |
| qq_operations | operation_id、幂等键唯一、原始消息复合键唯一、request_hash、结果 JSON、完成时间 |
| balance_ledger | operation_id、user_id、signed_delta、kind；转账两条流水的和为零 |

转账账本与用户余额应使用你已有账务设施，避免建立与实际可用余额脱节的第二套余额。

## 10. 后端上线验收清单

- [ ] A 请求绑定 B 邮箱，A 无法确认 B 的申请；跨账号 request_id 返回拒绝。
- [ ] 请求回复和 `/account` 不可枚举邮箱；过期、重复、错误确认码正确处理且限流。
- [ ] 未确认账号不可签到/打劫/查询余额；解绑立刻撤销权限。
- [ ] 两个并发确认不能把一个 QQ 绑定到两个账号；换绑与经济请求没有 TOCTOU。
- [ ] 跨群/私聊/机器人、多条消息并发签到，同账号同日只发放一次。
- [ ] 双向并发打劫不死锁，不出现负余额、丢失更新或余额凭空增加。
- [ ] 并发充值/消费/打劫保持账本及余额缓存一致。
- [ ] 概率 0 和 1、上下限相等、零赔偿、余额不足封顶、护盾/冷却边界符合规定。
- [ ] 重启、跨实例、断线和超时重试不重复转账；同 key 参数改变直接拒绝。
- [ ] 被护盾拒绝不触发发起者冷却；有效失败会触发冷却并给目标护盾。
- [ ] 站内换绑不会清空签到/冷却；所有状态按账号而非 QQ/群保存。
- [ ] QQ 真机群消息含第三方 @ 身份；缺失身份时插件安全拒绝。
- [ ] 令牌可吊销轮换，不出现在日志、前端响应或群消息。
