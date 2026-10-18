# 架构与一致性

`api.py` 负责真实客户端契约、身份验证和输入/响应。`service.py` 负责会话、设备、幂等、账号权限、抢接与序列化。`runtime.py` 是单个 PBX 控制器；`asterisk.py`、`push.py` 分别封装真实 ARI 和 Firebase SDK。`models.py/db.py` 管理持久化，`migrations/` 是可升降级的冻结迁移。`maintenance.py/cli.py` 与 `deploy/` 提供安装维护。

SQLite 开启外键、WAL、FULL 同步、15 秒 busy timeout。所有业务写入用 BEGIN IMMEDIATE；抢接、外呼幂等结果、线路租约和指令写在相同事务内。所有外部网络请求位于数据库事务外。业务状态有 revision，设备投递收据有唯一约束。线路主键 fxo 只对应一条模拟线路，call_id 和 channel_id 唯一。

一个用户的唯一 extension 表示个人分机。每次登录生成服务器设备 ID、随机设备 SIP 密码及随机设备会话令牌。数据库只保存会话令牌 SHA-256，密码使用 Argon2id；SIP 密码和推送 token 用主密钥 Fernet 加密。配额 nullable 表示管理员未限制。管理员禁用、密码重置、设备撤销以及会话过期会撤销设备能力，并持久化通话清理指令。

## 呼入与抢接

内部分机的实际 SIP INVITE 进入受限 dialplan，Stasis 参数使用 endpoint 配置的 HOMELINE_DEVICE_ID；服务端再核对实际 CHANNEL(endpoint)、安全传输、账号/设备/凭据有效期。不会相信 SIP From 声明的用户身份。FXO 来电必须来自配置的 endpoint 与固定可信 IP。

服务端分配 call_id，给每台有效设备建 leg。FCM 只提示唤醒；等待设备更新凭据并注册。ARI `/channels/create` 先创建未拨号 channel，持久化真实 protocol_id（SIP Call-ID）与 leg，再执行 `/dial`。客户端按自己的 SIP Call-ID resolve，不允许通过任意 wire ID 认领其他设备通话。

多个设备先通过 HTTP claim 在数据库事务中争夺唯一 winner。胜者得到 claimed；其他设备立即获得 snapshot 变化提示并收到 PBX CANCEL。只有胜者的 PBX Up 事件允许桥接/应答原始通道；未经授权的 Up 会清理。客户端 connected 收据只记遥测。每个参与设备看到自己的 SIP leg；内部主叫的 answered_device_id 表示主叫设备归属，避免误判为其他被叫设备接听。

无人接听策略为 ring_seconds 超时、标记 timeout/no_answer、挂断相关通道、确认 PBX 通道消失后释放线路。当前没有语音信箱或转移到公网号码的隐式策略。外部排队位置只报告服务器当前独占 FXO 呼叫的 1；不伪造网关/运营商不可观测的排队位置。

## 外呼与恢复

提交外呼只创建 OutboundTask；幂等键和请求摘要避免重试生成重复任务。排队按 priority 降序、created_at/id 升序，默认 priority=0 即 FIFO。调度必须同时检查持久线路租约与实际 FXO channels。先回呼用户设备；胜者实际接听后才拨外线。等待时间没有可靠估计，返回 null。

外线 channel ID 在实际 create/dial 前持久化。外部拨号结果不明时标记 external_dial_uncertain，不重放 originate/dial。挂断和清理可以重试，释放线路前必须实际查询 channels 并确认已消失。外部未知 FXO channel 会阻止调度。

事件 WebSocket 断线立即暂停控制。重连时读取真实 channels，保守终止已知或 hl- 前缀的自有通道，失败已执行任务，保留等待任务，确认通道已清理后重建线路状态。不会把 DB 里的 connected 恢复成实际上不存在的通话。代价是控制面重启会终止进行中的通话，避免重复占线与重复外呼。

PBX 指令和推送分别运行，Firebase 超时不会阻塞 CANCEL。FCM accepted、device received/restored/registered/ringing/connected、PBX connected 分别审计。无 Firebase 配置明确记录 fcm_not_configured；不会使用隐式云凭据。重复提示以同一个 call_id 发出，最终权威状态仍需客户端查询。

只有一个 Uvicorn worker，fcntl 文件锁阻止第二控制器。当前针对单机家庭部署，不提供多节点 HA。事件以每秒权限重查与 snapshot_changed 失效提示实现；断线后客户端重新抓 snapshot，不把事件流当完整呼叫信令。
