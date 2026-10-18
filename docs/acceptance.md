# 自动化与部署验收

每项实测结果与环境见 verification.md。下列步骤不是已通过的真机记录。

## 本地自动化

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests deploy tools
.venv/bin/ruff format --check src tests deploy tools
.venv/bin/pytest -q
bash -n deploy/*.sh
.venv/bin/pip wheel --no-deps . -w dist
.venv/bin/pip check
.venv/bin/python tools/export_openapi.py
.venv/bin/python tools/dependency_report.py
```

API 测试注入明确标记的 TestRuntime，业务测试注入 PBX/Push 故障驱动器，检验事务与恢复策略；它们不声称验证 SIP 或 FCM 网络。包括并发 HTTP claim/外呼、越权、配额、撤销、加密、设备token刷新竞争、单线 FIFO、PBX未知拨号不重放、通道残留不释放租约、迁移升降级和加密恢复。

## 独立真实 Asterisk

不要指向正在使用的家庭 PBX。编译并安装独立 Asterisk22.11.0 到临时 prefix（流程见 CI）；测试另建临时配置和随机凭据、可信临时 CA，绑定 loopback18088/18089 与临时 RTP21000–21100。安装测试额外依赖：

```bash
.venv/bin/pip install '.[native]'
HOMELINE_TEST_ASTERISK_PREFIX=/tmp/hls-asterisk .venv/bin/pytest tests/native -q -s
```

两个用例分别验证真实控制与媒体：三条 WSS 连接、Digest注册、发起真实SIP呼叫、两设备INVITE、Call-ID与API匹配、并发claim仅一成功、失败设备CANCEL、桥接及PBX重启恢复；媒体用例还要求两端DTLS连接并接收超过10个真实解密音频RTP包。测试用程序化WebRTC端点，不等于Android/Telecom验收。

当前受限 runner 禁止系统网卡枚举，只能运行：

```bash
HOMELINE_TEST_ASTERISK_PREFIX=/tmp/hls-asterisk-prefix HOMELINE_TEST_LOOPBACK=1 HOMELINE_TEST_CONTROL_ONLY=1 .venv/bin/pytest tests/native -q -s
```

这里 LOOPBACK 仅将测试客户端 ICE 地址枚举设为127.0.0.1，SIP/ARI/TLS全部是真实网络。媒体用例明确跳过，不以它替代媒体通过。正常主机和CI应不设置这两个限制变量。

## Raspberry Pi 与电话设备（尚需实测）

1. 在干净 Pi4 arm64 Raspberry Pi OS 64-bit 安装，记录 OS/内核/Python/Asterisk版本。运行 install.sh，ready200，TLS/WSS握手成功；初始化管理员并通过受保护API创建第二用户。再次运行安装，核对用户/设备/DB/密钥没有变化。断网/错误证书/磁盘只读时应非零失败且不重建数据库。
2. 两台Android使用同一账户登录，确认逻辑分机相同、device_id/SIP密码不同。检查API快照、PBX contacts和客户端实际注册。用另一个账户内拨分机，两机同时响；同时接听仅一成功，另一机立即停止并显示其他设备接听。主叫继续通话，双向音频、静音/蓝牙/扬声器正常。
3. 配置真实Firebase项目与service account；锁屏/休眠到不保持SIP连接。FXO或内部分机来电触发推送，分别检查fcm_accepted、device_received/restored/registered/ringing、PBXconnected。重复/延迟同call_id不会产生第二个电话；过期/其他设备已接听时停止。失效token清除，刷新竞态不误删新token。强制停止/无Google服务/离线允许超时，不得报已接通。
4. 撤销其中设备并禁用账户，旧Bearer、旧SIP凭据、旧设备接听/新INVITE全部被拒绝。PBX失联期间权限仍被API拒绝，恢复时旧通道清理。配额由管理员设置，0禁止新设备、null不限产品级设备数。
5. 连接真实FXO网关并确认一条模拟外线。两用户提交多个任务，重复同Idempotency-Key不会二次拨号，默认FIFO。等待可取消，运行中不可取消队列任务；呼叫应通过hangup结束。外线实际忙时只等待不抢线，租约必须在实际通道消失后释放。核对运营商/网关日志的真实拨号次数。
6. 在外呼create/dial、ringing、connected各阶段断网、杀API/PBX、重启设备。已执行任务失败/终止但不重拨，waiting任务在通道确认空闲后恢复。无回答在ring_seconds超时，不进入无文档的转移策略。重复/乱序PBX事件不能复活终态。
7. 用错误CA/主机名证书连接HTTPS/WSS，必须拒绝且不回落明文。测试VPN、移动网络切换与无公网IPv4，核对ICE候选、双向SRTP；ARI/AMI不可从LAN/公网连接。
8. 加密备份、升级迁移、恢复，核对用户、设备、队列、密钥、SQLite完整性与新旧凭据行为。错误口令不得改写现有数据。恢复后旧令牌/旧SIP凭据必须失效，历史waiting任务不能自动再拨；本地recover-admin重设密码后审核启用用户，设备重新登录。卸载两次均可执行，数据库与配置保留。还原网络/电源故障后的诊断日志，确认没有密码/令牌泄露。

只有完成这些操作并保留真实日志/版本/结果，才能声称完成 Pi、Android、FXO、后台推送的最终验收。

## 实际反向代理链路

CI 额外安装独立 Nginx 测试进程，使用 `deploy/configure.py` 实际生成的代理配置，仅改为隔离测试端口和临时可信 CA：

```bash
HOMELINE_TEST_ASTERISK_PREFIX=/tmp/hls-asterisk HOMELINE_TEST_NGINX=/usr/sbin/nginx .venv/bin/pytest tests/native -q -s
```

第三个 proxy-media 用例通过真实 Nginx→Uvicorn 执行 HTTPS API，通过 Nginx→Asterisk TLS 执行 SIP/WSS，并验证事件 WebSocket、ARI 对外拒绝、错误上游证书名返回 502、并发抢接、设备撤销、SRTP 音频及 PBX 重启清理。前两个直连用例仍独立保留；未提供真实 Nginx 路径时第三个用例明确跳过。此 Linux 软件栈测试不能代替 Pi4/systemd/Android/FXO 的实机验收。
