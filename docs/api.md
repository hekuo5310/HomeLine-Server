# 客户端 API 契约

基于实际客户端 commit ce3a3ccd9096d5d88d3ac09470560023937fffb2，不修改客户端。机器可读 schema 为 openapi.json，可运行 `python tools/export_openapi.py` 再生；运行服务提供 /docs 和 /openapi.json。只有 POST /v1/sessions 免认证；其余 /v1 需要 Authorization: Bearer 设备会话令牌。TLS 由 Nginx 提供，管理 API 不允许直接在公网裸露。

| 方法与路径 | 请求 | 响应 |
|---|---|---|
| POST /v1/sessions | username,password,device_name,platform | 201 Session |
| GET /v1/session | — | Session，同一已认证令牌回显，不是新令牌 |
| DELETE /v1/session | — | {}，撤销当前设备及会话 |
| GET /v1/snapshot | — | Snapshot |
| GET /v1/devices | — | Device[]（附加管理便利接口） |
| PATCH /v1/devices/{id} | name | Device |
| DELETE /v1/devices/{id} | Idempotency-Key | {} |
| PUT /v1/devices/current/push-token | provider:fcm,token:string/null | {} |
| POST /v1/devices/current/sip-credentials | — | SipCredentials |
| GET /v1/calls/{id} | — | 当前设备 leg 的 Call |
| POST /v1/calls/resolve | sip_call_id | Call，必须已属于当前设备 |
| POST /v1/calls/{id}/claim | Idempotency-Key | Call，claimed 不代表接通 |
| POST /v1/calls/{id}/reject | Idempotency-Key | Call |
| POST /v1/calls/{id}/hangup | Idempotency-Key | Call |
| POST /v1/calls/{id}/device-events | stage，Idempotency-Key | {} |
| GET /v1/inbound-queue | — | 当前设备外部来电 Call[] |
| POST /v1/outbound-tasks | destination，Idempotency-Key | 201 Task |
| GET /v1/outbound-tasks | — | 当前用户 Task[] |
| POST /v1/outbound-tasks/{id}/cancel | Idempotency-Key | {}，仅 waiting 可取消 |
| GET /v1/me | — | id,username,extension,account_status,device_limit,role |
| GET/POST /v1/admin/users | 创建见 OpenAPI | 用户列表 / {id} |
| PATCH /v1/admin/users/{id} | status/device_limit/password | {}；不能禁用最后管理员 |
| GET/DELETE /v1/admin/devices[/{id}] | — | 脱敏设备列表 / {} |
| PATCH /v1/admin/outbound-tasks/{id} | priority [-100,100] | {}，仅 waiting |
| GET /v1/admin/audit | — | 最近 200 条脱敏审计 |
| GET /health/live、/health/ready | — | alive；PBX 未准备好时 ready=503 |

DTO 精确字段见客户端审计及 OpenAPI。时间使用 UTC RFC3339；nullable 必须保留。呼叫无 INVITE 时 sip_call_id=""，不能猜测 ID。设备列表与管理员账号/设备列表完整返回，不以显示截断形成隐含设备配额；登录配额只由管理员设置。通话/历史/任务的 snapshot 返回最近 200 项，旧记录保留于数据库/备份；现有客户端未实现历史分页。history 是当前设备参加过的终态通话。

呼叫状态 waiting/ringing/claimed/connected/ended/cancelled/failed/timeout；终态不逆转。Task 状态 waiting/ringing/dialing/connected/completed/failed/cancelled。line_status 为 offline/unknown/idle/busy。SIP credentials 返回 WSS、sip:d{device_id}@domain、设备密码、期限和 ice_servers。urls 必须是字符串，符合 Flutter 的 List<Map<String,String>>。默认凭据授权期 600 秒，PJSIP 注册最长 60 秒；客户端定期重新获取。过期设备不能继续发新呼叫。

幂等键为 1–200 字符且不能含控制字符。同设备、同操作和同键保存精确请求摘要及结果；重试相同请求返回原结果，换请求体返回 409 idempotency_conflict。请求在事务内提交，不存在先拨号再记幂等结果的窗口。撤销本人设备后旧令牌再重试会返回 401，不能绕过撤销。

WebSocket /v1/events 使用相同 Bearer 请求头，服务端发送 {"type":"snapshot_changed"}，含义是重新抓取 snapshot。不能在 URL 放令牌；不接受客户端业务命令。失效会话以 4401 关闭。

FCM 为 Android high priority data-only：type=incoming_call、call_id、expires_at，TTL≤120 秒且受呼叫 deadline 限制。不含 SIP 密码、会话令牌或电话号码。receipt stage 支持 received/restored/registered/ringing/connected/failed/media_lost，不能据收据伪造 PBX 状态。

错误格式 {code,message}，输入错误仅返回字段路径，不回显敏感输入。常见状态：401 authentication_required/session_expired/invalid_credentials；403 access_revoked/account_disabled/admin_required/not_call_participant；404 device_not_found/call_not_found/task_not_found；409 answered_elsewhere/sip_not_ringing/call_expired/device_limit_reached/idempotency_conflict/task_already_running/constraint_conflict；400 idempotency_key_required；413 request_too_large；422 invalid_input；429 rate_limited；503 pbx_unavailable/sip_provision_failed/fxo_not_configured。请求体上限 16 KiB。HTTP 总速率 1000/min、登录 20/5min、WS 30/min，按实际连接 IP 计；当前 Nginx 下共享回环地址，适合家庭规模，公网接入需额外认证代理与边界限速。
