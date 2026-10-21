# 验证记录（2026-10-18）

本机：Ubuntu24.04 amd64 / Python3.12.14，独立源码编译 Asterisk22.11.0。

- Python业务/API/迁移/维护测试：首轮完整运行26 passed，另2项真实PBX默认跳过。
- Ruff、Shell语法、Python wheel构建、pip check通过。Wheel包含迁移与MPL许可证；已在独立venv中安装Wheel，从仓库以外目录执行打包迁移两次成功。
- 实际 Asterisk 控制测试：1 passed、1 media skipped（本机禁止PBX网卡枚举；测试客户端只指定真实loopback）。没有关闭TLS校验。
- GitHub真实PBX CI run [39108621823](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39108621823)，job117619756845：**2 passed**。官方Asterisk22.11.0从源码构建成功；真实WSS/Digest REGISTER、INVITE Call-ID映射、多设备振铃/并发claim、失败设备CANCEL、DTLS-SRTP连接及音频RTP接收、PBX重启清理均通过。
- 后续完整CI run [39109053042](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39109053042) 全部成功，包含Python3.11/3.13、静态检查、API/业务测试、Shell/Wheel与真实PBX媒体。最终源码修订还会运行独立CI，请核对PR中对应提交状态。
- arm64/Python3.13全60项锁定依赖wheel下载解析成功；这是分发支持检查，不是Pi执行验证。
- 已覆盖权限/撤销/配额、外呼幂等与并发、单线路租约/FIFO、未知拨号不重放、等待队列恢复、通道残留不释放租约、FCM失效token刷新竞争/SDK数据载荷、迁移升降级/外键/唯一性、加密备份恢复/错误密码、配置重复执行保留数据。

未实测：Pi4 arm64完整安装/重复安装、目标主机systemd启停、两台Android系统来电/锁屏/电池策略、真实Firebase项目投递、真实FXO线路及公网/VPN/TURN媒体。程序化WebRTC媒体通过不能代替Android/FXO验收。配置渲染幂等测试不能代替整机安装验收。

复验命令和真机步骤见 acceptance.md、deployment.md。所有后续结果必须关联实际运行记录。

## 本轮维护与反向代理验证

2026-10-18 续做：本机业务/API/维护测试新增至 **35 passed、3 native skipped**。新增覆盖连续备份恢复、带点号的自定义PBX配置、半成品备份不发布、整组文件回滚、进程中断标记、API拒绝带标记启动、备份密钥不匹配、备份/恢复CLI预检不改写目标。

[CI run39131376981](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39131376981) 已通过两种Python检查与 **3项真实PBX测试**：新增proxy-media使用部署模板生成的真实Nginx配置，经HTTPS执行API、事件WebSocket、TLS上游SIP/WSS与SRTP音频，并检查ARI不可透出和上游证书名错误返回502。该次CI业务测试对应新增预检前的源码；后续预检修订由PR最新提交的CI单独验证。这里没有把历史CI结果当作最新源码结果。

物理Pi、Android、真实FXO/Firebase及真实断电仍未实测；Linux代理/媒体测试与模拟I/O故障不能替代这些项目。

恢复权限隔离回归已覆盖：旧会话/设备失效、所有账号暂停、待执行外呼终止、生成的SIP凭据清空；本地重设管理员密码后只恢复指定管理员，不自动启用普通用户。客户端待提交外呼按 user_id:device_id 绑定，新设备登录不会自动重放旧设备意图（只读核查客户端代码）。

另已验证配置与管理员输入的Pydantic错误文字隐藏原始输入，防止CLI错误输出回显密码或ARI凭据；OpenAPI生成一致性保持不变。


## ACME 与 TURN 续做（2026-10-20）

- 业务/API/维护/部署配置测试本机67 passed、4 native skipped；Python3.11/3.13 CI均通过。
- 源码56c251f586907cf3e5678521997995ca06cc651b的 [CI run39423955701](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39423955701) 通过4项真实Asterisk22.11.0用例。新增coturn经TCP allocation、Nginx HTTPS/WSS、双方SDP仅保留relay候选，DTLS-SRTP音频接收通过；测试输出media verified=True、nginx proxy=True、relay-only=True。
- 实际从固定上游提交807da6498377ee5e0cf43a78091f46f12dc59a89安装acme.sh到临时目录，验证v3.1.6和dns_cf插件存在，随后清理。没有发起真实CA签发请求。
- 四个systemd续期/启用单元在隔离根目录、提供占位执行文件后使用systemd-analyze verify通过；这仅为静态单元验证，不是Pi运行测试。
- 隔离测试覆盖JSON密钥权限/保留环境变量拒绝、Token不进命令行、PBX/客户端TURN配置生成、忙线延迟证书启用、启用失败保留pending及服务恢复。发行命令采用故障驱动器，不将其记为真实DNS签发。

后续提交为证书就绪探针增加明确连接/总超时，并补充验收步骤；以PR对应提交CI状态为准。用户实际DNS申请/续期、Pi systemd生命周期、第三方FRP公网、Android与FXO仍未实测。
