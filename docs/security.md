# 网络与安全

默认拓扑为手机（LAN 或经过认证的 VPN）→ Nginx HTTPS/WSS → 本机 API/独立 Asterisk。FCM 是 Pi 主动向 Google 发出的 HTTPS；媒体走手机与 PBX 的 ICE/DTLS-SRTP，不能由普通 HTTP 隧道自动承载。无公网 IPv4 时不要求开放管理接口，优先 VPN；公网中继/TURN 必须独立认证、配额、限速、监测和密钥轮换，当前不自动部署中继。

| 端口 | 范围与用途 |
|---|---|
| TCP443 | 允许家庭 LAN/VPN；HTTPS API、事件与 SIP WSS |
| TCP8000 | 127.0.0.1；Uvicorn，禁止公网访问 |
| TCP8088 | 127.0.0.1；ARI/基础 HTTP，管理用户名密码仅服务器持有 |
| TCP8089 | 127.0.0.1；Asterisk HTTPS/WSS，供 Nginx 校验 TLS 后转发 |
| UDP10000–10100 | 只对需要媒体的 LAN/VPN/受控中继开放；DTLS-SRTP，包含 RTCP/ICE |
| UDP5060 | 仅显式配置 FXO 时绑定；防火墙只允许网关固定 IP |
| TCP5038 | AMI 禁用，不开放 |
| 出站 TCP443 | Firebase API/OAuth、受信任依赖安装/更新；非入站推送端口 |

Nginx 同时终止外部 TLS 并验证到 Asterisk 的 TLS。客户端必须校验证书。PBX 服务端证书有独立受限副本；ARI URL 限定 loopback，禁止 URL 凭据，HTTP Basic 只放 Header。HomeLine 不在客户端保存 ARI/AMI 或 Firebase 管理凭据。

账号密码 Argon2id，随机会话令牌只保存哈希；独立设备 SIP secret 和 FCM token 在 SQLite 中加密，主密钥 0600 独立存放。运行时 PJSIP 配置必须有明文 SIP 密码，因此配置目录/文件严格限权；这不是“数据库加密即可覆盖所有敏感文件”。备份必须同时保护主密钥和数据库。

认证令牌不放查询参数，不记录 access log；错误和输入验证不回显密码、token 或推送内容。审计只保存参与者 ID、动作、资源 ID 和时间。生产日志不写完整 ARI 异常 URL 或服务账户内容。SIP Digest 为互操作可能使用 MD5，但密码随机且传输 WSS；账号密码哈希不是 MD5。

撤销和禁用先持久化，再同步移除可注册 endpoint，挂断受影响 leg/通话。故障期间 API 仍拒绝旧会话；PBX 恢复后保守清理自有通道。已建立 SIP 认证对象在 PBX 内有生命周期，不能把“配置 reload 返回成功”当作所有旧网络包都消失；必须按验收步骤实际复验旧凭据注册与外呼被拒绝。正常凭据授权过期不主动中断既有媒体，但不再允许建立新呼叫。

本实现是单机家庭规模控制服务。管理角色与普通用户严格校验，禁止越权设备/通话/队列；幂等并不授予权限。管理员更改账号状态/密码会撤销其全部设备。暴露到公网前须增加身份保护代理、防火墙与独立边界限速；不支持直接暴露 ARI/AMI。通用 HTTPS tunnel 不能替代 SIP 媒体网络设计。
