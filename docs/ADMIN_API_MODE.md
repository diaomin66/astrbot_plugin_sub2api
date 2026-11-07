# 管理员 API 模式（v0.2.1）

插件只持有 bridge 令牌，管理员 Key 只存 bridge 服务端，调用 Sub2API 管理员余额 add/subtract 接口，不直接修改用户余额表。

## 指令和权限

- /签到：已绑定站内账号每日一次，使用后台配置的随机奖励范围。
- /打劫 @群友：双方绑定不同账号，成功转入、失败赔偿，保留概率、冷却和护盾。
- /充值 @已绑定群友 10.50：给 QQ 群身份绑定的账号充值。
- /充值 user@example.com 10.50：按精确站内邮箱充值，邮箱目标无需绑定 QQ。

充值同时要求 AstrBot event.is_admin() 和 bridge 服务端白名单。群主或群管理员不自动取得权限。
服务端精确匹配 provider/app_id/scope/openid/platform_id；不信任请求中自报的管理员标记。
本实例由 root 定时任务每 10 秒读取 AstrBot 管理员配置，生成仅含身份的私有白名单；每次充值重新读取，快照 30 秒过期。配置不可读、无效、过期时拒绝充值，不回退到旧管理员。新增或移除机器人管理员后无需重启 bridge，正常约 10 秒同步；同时须确保 AstrBot 自身已加载管理员设置。
recharge.enabled 默认 false。金额必须大于零且最多六位小数，不接受负数、科学计数、多目标或邮箱/@混用。

## 接口

POST /api/v1/qq-bot/recharge 沿用 Bearer bridge 令牌、X-Bridge-Version: 1、消息幂等键和 actor/context/policy。
额外字段 amount 为十进制字符串，recipient 为仅含 email 的对象，或者仅含 identity 的对象（值为 QQ 官方群身份）。
成功返回 state=credited、amount、balance。三种待处理状态 processing/review_required/refunded 带 operation_id；插件不会把它们显示为充值或打劫成功。

## 账务模式与边界

这不是跨账户原子转账。服务端在发送请求前持久化随机结果、金额、两条记账任务和账号占用，再先扣后加。
每一笔使用固定 Idempotency-Key 和唯一 notes。写请求超时/进程崩溃后只按唯一 notes、金额和已使用状态查余额流水；不根据余额快照猜测、不自动重复提交。
找不到完整证据时保持 review_required 和账号占用，管理员须核对实际流水。扣款明确成功而入账在认证/路由阶段明确拒绝时才尝试退款；入账不明绝不盲退。
每 20 秒自动恢复，deploy/admin_reconcile.py 可列出或触发一次核对。绝不自动按 TTL 清除未决交易。

管理员 API 的正常消费缓存/计费行为保持不变。本模式不是网关资金预留：消费请求仍可能并发结算或产生真实欠费，不能宣称全部消费入口已被锁定。图片冻结余额拒绝打劫，数据库条件扣款仍由管理员接口负责。

签到、打劫入账、退款不应生成邀请返佣。每次准备和新加款前核对 affiliate_admin_recharge_enabled=false，否则拒绝新入账；不会擅自关闭全站返佣设置。管理员切换该全站设置前应先关闭 bridge 并清理未决交易，跨服务检查不是配置原子锁。

## 部署

后端入口 bridge.admin_store:create_admin_app；BRIDGE_ADMIN_CONFIG 指向 root/service 私有 JSON，包含 base_url、admin_key、enabled、admins。
base_url 必须使用通过校验的 HTTPS，不跟随重定向。admins 是上述完整身份对象的列表。可选 admins_file 指向定时生成的短效权限快照；配置后忽略静态 admins，不给 bridge 读取整个 AstrBot 配置的权限。同步程序、systemd timer 和快照权限须一起部署；仅更新插件不会自动安装后端同步。
先执行兼容 schema 和 admin_api.sql 迁移，赋予专用运行角色 bridge 元数据表权限，不能授予 public.users 余额写权限或密码字段读取权限。
保留之前独立的网关 fence 方案，本模式不把其验收代号伪装成完成。真实 QQ 验收由账号本人操作，不代替绑定确认。

## 安全与运维

管理员 Key 不进入插件配置、仓库、普通日志或 QQ 回复。已发送但不明的记账可能一直保持待核对：这是防重复资金操作的保守选择，不承诺任何异常都自动恢复。
新充值消息视为新操作；未决时不要重新发起。确认成功后再次发命令会再次充值。

## 本次部署状态

2026-11-07：现有实例使用 admin_api_saga 后端；插件 v0.2.1 修复 HTTP 403/404 业务错误的识别。充值管理员白名单与 AstrBot 配置自动同步，三个开关保持启用。公网只做不存在账号的无资金变动验证；不声称真实 QQ 充值/打劫已经实测到账。详细回执在后端 HANDOFF-ADMIN-API-20261107.md。

## 充值报错排查

- ADMIN_REQUIRED：机器人管理员身份或服务端白名单未通过；不会执行加款，不应要求更换有效令牌。
- TARGET_NOT_FOUND：未找到精确站内邮箱；不是 bridge 路由未安装。
- APP_NOT_ALLOWED：令牌未授权当前 AppID；检查机器人应用和服务端令牌范围。
- UNAUTHORIZED：真正的令牌认证失败，或未返回可识别业务码的 401/403。
- processing/review_required：结果未确认，先对账，不要换消息重复充值。
