# HomeLine Server

面向 Raspberry Pi 4 的自建家庭电话服务端，HomeLine 自有代码采用 MPL-2.0。Asterisk 独立安装和运行，通过 ARI/PJSIP 协作，不将其源代码或二进制混入本仓库。

开发分支正在逐步实现并验证，当前提交建立数据模型、安全存储、配置校验与 Alembic 迁移。尚不能按此基础提交部署完整电话服务；后续提交补充 API、PBX、推送和安装维护流程。验证记录随阶段更新，不将单元测试当作真实设备验收。

客户端对接版本固定为 hekuo5310/HomeLine 的 `ce3a3ccd9096d5d88d3ac09470560023937fffb2`（feat/flutter-voip-mvp）；该实现尚未合并到客户端 main。参见 docs/client-audit.md。
