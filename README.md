# HomeLine Server

自建家庭电话控制服务：Python/FastAPI、SQLite/SQLAlchemy/Alembic、独立 Asterisk PJSIP/ARI、Firebase Admin。HomeLine 自有代码使用 MPL-2.0；不包含 Asterisk 源码或二进制。

服务端在 `feat/server-core` 分支开发。客户端只读核查固定于 `HomeLine/feat/flutter-voip-mvp` 的 `ce3a3ccd9096d5d88d3ac09470560023937fffb2`，并核对其实际成功 CI。详见 [客户端审计](docs/client-audit.md)。

已实现设备独立 SIP 身份、设备作用域会话与撤销、账号/配额管理、版本化 API、多设备振铃与事务抢接、单 FXO 线路的 FIFO/管理员优先级外呼队列、幂等请求、持久化指令、FCM token 管理与推送、PBX 中断后保守恢复、迁移、加密备份及维护脚本。HTTP claim 与 FCM accepted 均不会生成 connected 状态；真实 PBX 事件才确认接通。

这是可运行的首个服务端版本，尚未完成 Raspberry Pi、Android、FXO 和实时 Firebase 的整机验收。本机已经运行真实 Asterisk/WSS/SIP 控制测试；媒体验收的独立测试与环境限制见 [测试记录](docs/verification.md)。

## 安装（目标设备）

目标为 Raspberry Pi 4 + Raspberry Pi OS Lite 64-bit Bookworm/Trixie。安装会独立编译固定版本 Asterisk 22.11.0，不改写其他 Asterisk 实例。

无需先克隆仓库，可下载一键入口到本地后运行：

```bash
curl --fail --location --proto '=https' https://raw.githubusercontent.com/hekuo5310/HomeLine-Server/main/deploy/bootstrap.sh -o /tmp/homeline-bootstrap.sh && sudo bash /tmp/homeline-bootstrap.sh
```

已有证书模式由向导询问域名、TLS fullchain 和私钥路径；ACME模式传入DNS插件与凭据JSON，自动签发。随后安装 Python、独立 Asterisk、Nginx 与 systemd 服务，执行迁移和健康检查；最后可在终端创建管理员并隐藏输入密码。支持已有可信证书，或配置 acme.sh DNS API 自动申请与续期。已有安装只执行诊断，不绕过备份直接升级。可加 `--non-interactive --host ... --cert ... --key ...`，或用 `--ref <40位提交SHA>` 固定源码版本。完整说明见 [部署文档](docs/deployment.md)。

以下为已克隆仓库时的安装方式：

先准备与 DNS 名称匹配、手机和服务器均信任的 TLS 证书；局域网私有 CA 必须先安装信任链。普通 HTTP 或关闭证书校验不受支持。

```bash
git clone --branch feat/server-core https://github.com/hekuo5310/HomeLine-Server.git
cd HomeLine-Server
sudo deploy/install.sh --host phone.example --cert /etc/ssl/homeline/fullchain.pem --key /etc/ssl/homeline/key.pem
sudo -u homeline /opt/homeline/venv/bin/homeline init-admin --extension 1001
```

不指定 FXO 时可以进行内部分机通话；外线状态为 offline，外呼请求明确返回 fxo_not_configured。FXO 开通、Firebase、路由与端口设置见 [部署文档](docs/deployment.md)。只有一条模拟外线时，队列不会增加外线并发容量。

## 开发与验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests deploy tools
.venv/bin/pytest -q
.venv/bin/pip wheel --no-deps . -w dist
```

默认单元/API 测试不依赖 PBX；真实 PBX 用例默认跳过，必须按 [验收步骤](docs/acceptance.md) 单独运行。普通运行必须配置真实 ARI 与 WSS 参数、创建 0600 主密钥并执行 `homeline migrate`，没有“假接通”运行模式。

- [架构与恢复策略](docs/architecture.md)
- [API 与错误码](docs/api.md)、[OpenAPI](docs/openapi.json)
- [部署、升级与备份](docs/deployment.md)
- [网络与安全](docs/security.md)
- [测试与真机验收](docs/acceptance.md)
- [第三方依赖与许可证](THIRD_PARTY.md)、[精确版本与元数据](licenses/python-dependencies.json)


第三方FRP用户可使用 **HTTPS隧道 + 经认证的公网TURN**，无需开放10000–10100的FRP UDP端口段。复制 `deploy/setup.example.sh` 为 `setup.local.sh` 自行填写DNS/ACME/TURN参数；详见 [部署文档](docs/deployment.md)。TURN需自建或由供应商提供，本项目不附带公共中继账号。
