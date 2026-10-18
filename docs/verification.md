# 验证记录（2026-10-18）

当前本机：Ubuntu 24.04 amd64 / Python 3.12.14，独立源码构建 Asterisk 22.11.0。

- Python：18 passed；默认 2 项真实 PBX 测试因未指定独立安装前缀而跳过。
- Ruff 检查/格式、Shell 语法检查通过。
- 实际 Asterisk 控制测试通过：WSS TLS（信任测试 CA，未关闭校验）、Digest REGISTER、真实 INVITE Call-ID、两个设备同时振铃与并发 claim、失败设备收到 CANCEL、PBX 重启后清理。
- 当前 runner 禁止 getifaddrs/网卡枚举。SIP 测试仅将客户端候选地址限定到真实 loopback；PBX 的 ICE 地址枚举仍被系统拒绝，因此媒体测试没有通过。未伪造 DTLS/SRTP 音频通过记录。CI 包含独立媒体测试。
- 已测试迁移重复运行/升降级、SQLite 外键与唯一性、权限/撤销、外呼幂等、单线路租约、未知拨号不重放、等待队列恢复、FCM 失效 token 更新竞争、加密备份恢复/错误密码、配置重复执行保留数据库与密钥。

未实测：Raspberry Pi arm64 完整安装/重复安装、systemd 主机级启停、两台 Android 系统来电/锁屏、电池策略、真实 Firebase、真实 FXO 模拟线路以及公网/VPN/TURN 媒体。配置渲染的幂等测试不能代替整机安装验收。

复验命令与完整真机步骤见 deployment.md 与 acceptance.md。CI 结果须以 GitHub 运行记录为准，不能把已编写工作流视作通过。
