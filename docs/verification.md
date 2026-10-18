# 验证记录（2026-10-18）

本机：Ubuntu24.04 amd64 / Python3.12.14，独立源码编译 Asterisk22.11.0。

- Python业务/API/迁移/维护测试：最近完整运行19 passed，新增安全部署验证待最终重跑。
- Ruff、Shell语法、Python wheel构建、pip check通过。Wheel包含迁移与MPL许可证。
- 实际 Asterisk 控制测试：1 passed、1 media skipped（本机禁止PBX网卡枚举；测试客户端只指定真实loopback）。没有关闭TLS校验。
- GitHub真实PBX CI run [39108621823](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39108621823)，job117619756845：**2 passed**。官方Asterisk22.11.0从源码构建成功；真实WSS/Digest REGISTER、INVITE Call-ID映射、多设备振铃/并发claim、失败设备CANCEL、DTLS-SRTP连接及音频RTP接收、PBX重启清理均通过。
- 同次CI的Python检查发现尚未提交tools目录，已补齐工具文件，最终提交还需重跑全部job。不能将单个PBX job通过说成整个CI通过。
- arm64/Python3.13全60项锁定依赖wheel下载解析成功；这是分发支持检查，不是Pi执行验证。
- 已覆盖权限/撤销/配额、外呼幂等与并发、单线路租约/FIFO、未知拨号不重放、等待队列恢复、通道残留不释放租约、FCM失效token刷新竞争/SDK数据载荷、迁移升降级/外键/唯一性、加密备份恢复/错误密码、配置重复执行保留数据。

未实测：Pi4 arm64完整安装/重复安装、目标主机systemd启停、两台Android系统来电/锁屏/电池策略、真实Firebase项目投递、真实FXO线路及公网/VPN/TURN媒体。程序化WebRTC媒体通过不能代替Android/FXO验收。配置渲染幂等测试不能代替整机安装验收。

复验命令和真机步骤见 acceptance.md、deployment.md。所有后续结果必须关联实际运行记录。
