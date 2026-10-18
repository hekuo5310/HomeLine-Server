# 客户端核查

2026-10-18 检查两个仓库分支、历史与许可证。服务端起始提交 50735e0，仅 README/MPL-2.0，无既有实现。客户端 main 仅初始文件，实际实现位于 feat/flutter-voip-mvp，固定提交 ce3a3ccd9096d5d88d3ac09470560023937fffb2。本项目只修改服务端。

已读取客户端 README、依赖、API、DTO、SIP 适配、CallController、WakeRecovery 及事件实现；GitHub CI run 39081458634 全部成功，包括代码生成一致性、格式、分析、Flutter 单元/Widget 测试、真实 HTTPS 证书拒绝、Android APK 构建、原生测试与产物上传。没有以此前报告代替 CI 检查；该 CI 不包含真机/PBX/FXO/实际 FCM 验收。

实际协议：HTTPS /v1，Authorization Bearer 设备作用域令牌；WSS /v1/events 仅发失效提示。SIP 为 sip_ua 1.1.0 的 WSS + WebRTC DTLS-SRTP，不是裸 5061 TLS。FCM data-only incoming_call/call_id/expires_at，有效期不超过 120 秒。SQLite 存未确认外呼幂等请求，密钥/令牌在安全存储。

须保持的 DTO：Session(access_token,expires_at,user_id,extension,device_id,account_status,device_limit)，Device(id,name,status,last_seen_at)，Call(id,sip_call_id,state,remote,updated_at,revision,answered_device_id,queue_position,waiting_since)，QueueTask(id,destination,state,revision,position,estimated_wait_seconds,failure_reason)，Snapshot(account_status,device_status,line_status,can_call,devices,calls,outbound_tasks,history)，SipCredentials(web_socket_url,uri,authorization_user,password,expires_at,ice_servers)。ice_servers 是 List<Map<String,String>>，urls 不能改为数组。

需要澄清的语义：answered_device_id 被客户端用于判定当前用户设备抢接。内部通话主叫 leg 必须返回主叫自己的设备归属，被叫 leg 返回真实胜者；将全局被叫胜者原样回给主叫，会触发客户端错误挂断。数据库将被叫胜者独立存储。尚无 INVITE 时 sip_call_id 为空字符串；一旦创建必须记录真实 PBX leg 的 SIP Call-ID，不能假造。

会话到期需要重新认证（客户端没有 refresh token）。HTTP claim 只是接听授权，实际 connected 必须来自 PBX/SIP 事件。FCM accepted 与 received/restored/registered/ringing/connected 分别记录，不能合并。

仍需真实 Firebase 项目、两台 Android、PBX 媒体及 FXO 验收环境；安装脚本的 arm64/Raspberry Pi 实测也必须与本机自动化区分。
