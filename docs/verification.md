# 验证记录（2026-10-18）

本机：Ubuntu24.04 amd64 / Python3.12.14，独立源码编译 Asterisk22.11.0。

- Python业务/API/迁移/维护测试：最近完整运行26 passed，另2项真实PBX默认跳过。
- Ruff、Shell语法、Python wheel构建、pip check通过。Wheel包含迁移与MPL许可证；已在独立venv中安装Wheel，从仓库以外目录执行打包迁移两次成功。
- 实际 Asterisk 控制测试：1 passed、1 media skipped（本机禁止PBX网卡枚举；测试客户端只指定真实loopback）。没有关闭TLS校验。
- GitHub真实PBX CI run [39108621823](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39108621823)，job117619756845：**2 passed**。官方Asterisk22.11.0从源码构建成功；真实WSS/Digest REGISTER、INVITE Call-ID映射、多设备振铃/并发claim、失败设备CANCEL、DTLS-SRTP连接及音频RTP接收、PBX重启清理均通过。
- 后续完整CI run [39109053042](https://github.com/hekuo5310/HomeLine-Server/actions/runs/39109053042) 全部成功，包含Python3.11/3.13、静态检查、API/业务测试、Shell/Wheel与真实PBX媒体。最终源码修订还会运行独立CI，请核对PR中对应提交状态。
- arm64/Python3.13全60项锁定依赖wheel下载解析成功；这是分发支持检查，不是Pi执行验证。
- 已覆盖权限/撤销/配额、外呼幂等与并发、单线路租约/FIFO、未知拨号不重放、等待队列恢复、通道残留不释放租约、FCM失效token刷新竞争/SDK数据载荷、迁移升降级/外键/唯一性、加密备份恢复/错误密码、配置重复执行保留数据。

未实测：Pi4 arm64完整安装/重复安装、目标主机systemd启停、两台Android系统来电/锁屏/电池策略、真实Firebase项目投递、真实FXO线路及公网/VPN/TURN媒体。程序化WebRTC媒体通过不能代替Android/FXO验收。配置渲染幂等测试不能代替整机安装验收。

复验命令和真机步骤见 acceptance.md、deployment.md。所有后续结果必须关联实际运行记录。
