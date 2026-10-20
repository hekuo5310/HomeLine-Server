# Raspberry Pi 部署与维护

目标：Raspberry Pi 4，官方 Raspberry Pi OS Lite **64-bit** Bookworm (Debian12) 或 Trixie (Debian13)。2026-10-18 核查官方 OS 文档/下载页，当前主线为 Trixie。安装器检查 OS、aarch64 和 Raspberry Pi 型号，不会把本机 amd64 测试当作 Pi 验证。Python 3.11–3.14；首要部署分别为 Bookworm3.11/Trixie3.13。

## 一条命令启动安装

```bash
curl --fail --location --proto '=https' https://raw.githubusercontent.com/hekuo5310/HomeLine-Server/main/deploy/bootstrap.sh -o /tmp/homeline-bootstrap.sh && sudo bash /tmp/homeline-bootstrap.sh
```

先下载为文件再执行；下载失败不会执行残缺脚本。首次运行交互询问域名、证书链和无密码私钥文件路径。自动检测 root、OS、aarch64、Pi 型号、systemd，获取安装锁，校验证书信任链、主机名、有效期、私钥配对，然后下载官方仓库并调用已有 install.sh。源码临时目录退出时清理；Asterisk 构建失败的目录和服务配置仍保留供诊断。最终在终端询问是否立即初始化 admin / 1001，密码隐藏输入，不会生成默认密码。

无人值守方式（管理员后续通过本机 CLI 初始化）：

```bash
sudo bash /tmp/homeline-bootstrap.sh --non-interactive \
  --host phone.example --cert /etc/ssl/homeline/fullchain.pem --key /etc/ssl/homeline/key.pem \
  --gateway 192.168.1.20
```

FXO 地址可省略，后续按本文配置；`--media-address` 可指定媒体地址。默认获取运行时 main，会输出实际提交SHA；要复现审核过的版本，加 `--ref <完整40位Git提交SHA>`。入口脚本也可从对应提交的 raw URL 下载，避免 main 后续变化。安装包与 Asterisk 源码下载需要出站联网，脚本不会配置公网端口映射。

可使用已有可信证书，或使用下述 acme.sh DNS API 自动签发模式；不生成不受信任的自签证书、不关闭证书校验。私有CA需先将根证书安装到 Pi 与手机。

重复运行入口只检查已有服务，健康则成功退出，不重新安装软件、改写数据库/密钥或重设管理员。已有安装异常会返回非零并提示检查日志；部分安装用原始版本 deploy/install.sh 修复。升级使用本页先备份的 upgrade.sh 流程。入口锁只防止多个 bootstrap 同时执行，其他维护操作仍应安排在线路空闲时，避免并发运行。

自动化仅验证入口编排和 OpenSSL 证书检查；没有将隔离的 shell 测试当作 Pi 整机安装结果。

先配置静态 LAN/VPN 地址和 DNS。准备覆盖 phone.example 的 TLS fullchain/key，手机必须信任 CA。私有 CA 导入 Pi 的 /usr/local/share/ca-certificates 后运行 update-ca-certificates；手机也必须自行信任。安装脚本不会关闭 TLS 校验或悄悄安装不被手机信任的证书。

```bash
sudo deploy/install.sh --host phone.example --cert /etc/ssl/homeline/fullchain.pem --key /etc/ssl/homeline/key.pem
sudo -u homeline /opt/homeline/venv/bin/homeline init-admin --username admin --extension 1001
```

密码通过终端隐藏输入两次，不放进命令行或安装日志。安装再次运行可直接 `sudo deploy/install.sh`，保留已有数据库、密钥、配置与管理员；有配置的情况下 flags 不会覆盖旧配置。首次失败保留已完成步骤和源码构建目录以诊断，退出码非零。不要同时运行两个安装/升级命令。

独立 Asterisk22.11.0 安装到 /opt/homeline-asterisk，下载归档 SHA-256 固定并校验，源码不加入 HomeLine 仓库。构建关闭 BUILD_NATIVE，避免针对编译 CPU 优化。HomeLine Wheel 装入 /opt/homeline/venv，并自带迁移资源。专用用户 homeline 与 asterisk，共享受限组 asterisk；主密钥和 server.env 0600，生成 PJSIP 含设备密码的文件 0640。systemd 只允许必要目录写入。

| 路径 | 内容 |
|---|---|
| /etc/homeline/server.env | 环境配置，不能提交到 Git |
| /etc/homeline/master.key | 数据库凭据加密主密钥，必须随数据库备份 |
| /etc/homeline/asterisk/ | 独立 PBX 配置，tls/ 包含专用副本 |
| /etc/homeline/asterisk/generated/devices.conf | HomeLine 动态生成，勿手改 |
| /var/lib/homeline/homeline.sqlite3 | SQLite + WAL |
| /var/lib/homeline-asterisk/ | PBX 状态及 DTLS 证书 |
| /etc/nginx/sites-available/homeline | HTTPS、API 和安全 WSS 转发 |

Nginx 到 PBX 的 /ws 上游为 https://127.0.0.1:8089，并校验系统 CA 与服务器名。ARI 仅127.0.0.1:8088，外部 /ari 被拒绝。证书续期后将 fullchain/key 更新到 /etc/homeline/asterisk/tls/server.crt 与 server.key（homeline:asterisk，0640），再依次重启 homeline-asterisk、homeline，reload nginx。变更传输配置同样需要重启 PBX，会中断当前通话。

## FXO 与 Firebase

首次安装可以显式 `--gateway 192.168.1.20` 生成固定 IP 的传统 FXO peer。网关必须在可信隔离 LAN/VPN，只有该 /32 可访问 UDP5060。传统模拟网关段可能只有 UDP/RTP，不能称其为端到端加密；手机到 PBX 始终为 WSS/DTLS-SRTP。更严格环境应使用支持 TLS/SRTP 的网关并由管理员调整 fxo.conf。网关入站路由到 homeline-fxo-in，外呼号码通过 PJSIP/number@homeline-fxo 发出。

已有安装需编辑 fxo.conf、HOMELINE_FXO_ENABLED=true、HOMELINE_INCOMING_EXTENSION=1001；endpoint 默认 homeline-fxo。确认公网付费号码/紧急号码策略、运营商费用和网关拨号规则再放开外呼账号。默认仅允许 + 和 3–20 位数字；不会向未配置的线路伪发成功。

把自己的 Firebase service account JSON 放在 /etc/homeline/firebase.json（homeline:asterisk，0600），设置 HOMELINE_FIREBASE_CREDENTIALS=/etc/homeline/firebase.json，再重启 homeline。需同一 Firebase 项目配置 Android 客户端。没有凭据时只记录 fcm_not_configured，前台 SIP 仍可通话；不具备后台来电唤醒能力。FCM 接受不代表收到/注册/振铃/接通。

ICE servers 可在 server.env 用 JSON 字符串设置 HOMELINE_ICE_SERVERS，例如 `[ {"urls":"turns:turn.example:5349","username":"temporary-user","credential":"temporary-secret"} ]`；配置文件变量整体使用单引号包裹 JSON。TURN 密钥需要受控轮换，不能把管理密钥下发客户端。默认不提供公网 TURN 服务；无 IPv4 时优先认证 VPN，使 SIP 和媒体均可路由。

## 升级、备份、恢复、卸载

```bash
# 先在 0600 文件中安全设置不少于 12 字符的备份口令，勿将口令写到命令行。
sudo deploy/backup.sh /secure/backups/home-20261018.hls /root/backup-passphrase
sudo deploy/upgrade.sh /secure/backups/pre-upgrade.hls /root/backup-passphrase
sudo deploy/restore.sh /secure/backups/home-20261018.hls /root/backup-passphrase
sudo -u homeline /opt/homeline/venv/bin/homeline recover-admin --username admin
# 管理员审核账号并重设密码、重新启用后，设备需要重新登录。
sudo deploy/uninstall.sh
```

backup 使用 SQLite backup API 得到一致快照（支持 WAL），Scrypt 派生密钥 + Fernet 认证加密。包含数据库、主密钥、server.env、PBX配置/专用TLS副本、已配置的 Firebase JSON。不含原始外部 TLS 路径、NGINX 自定义配置、系统 CA、Asterisk 二进制或 OS；这些须由系统备份恢复。当前数据库大小上限256MiB，超出会明确失败。

升级先停 HomeLine，再备份、安装精确依赖/新 Wheel、迁移，更新服务单元并检查 readiness；失败不启动不兼容版本。维护会终止当前通话，先确保线路空闲。恢复停止两个服务，取得文件锁、验证口令、SQLite完整性/外键/迁移版本及加密密钥，再写入固定文件名；保留 before-restore 时间戳副本，恢复后重新核对文件所有者并启动 PBX/API。恢复应在相同安装路径；迁移到其他路径须先审核 server.env 和 PBX 路径。不能直接把运行中 .sqlite3 文件复制当完整备份。

卸载禁用并移除两个 systemd 单元和 HomeLine Nginx 链接，保留数据库、密钥、配置、软件环境及独立 PBX 以避免误删数据。无需清理的系统包不会自动移除。需要彻底清理时先离线保存备份，再由管理员审核这些明确目录。

## 诊断

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
sudo journalctl -u homeline -u homeline-asterisk --since '10 minutes ago'
sudo -u asterisk /opt/homeline-asterisk/sbin/asterisk -C /etc/homeline/asterisk/asterisk.conf -rx 'pjsip show endpoints'
sudo -u asterisk /opt/homeline-asterisk/sbin/asterisk -C /etc/homeline/asterisk/asterisk.conf -rx 'core show channels'
sudo nginx -t
```

安装/升级/恢复还执行 `homeline doctor`，检查数据库完整性、必需ARI模块和外部WSS证书/握手（包括Nginx上游）。它不验证实际媒体和FCM投递。可手动运行 `sudo -u homeline /opt/homeline/venv/bin/homeline doctor`。

live 只表示进程活着；ready503 时不能认为电话可用。检查ARI凭据、loopback监听、required modules、文件权限；会话正常但无SIP时检查证书/主机名、凭据有效期、WebSocket升级和PJSIP contacts。振铃能接但无声音检查双向UDP、ICE、VPN路由、TURN、NAT媒体地址；勿关闭证书校验。生产默认不记录完整请求/推送/SIP调试，不要将包含凭据的 pjsip logger 输出公开。

## 备份与恢复故障保护

维护脚本使用当前检出版本的维护代码，因此从旧版 CLI 升级时也能执行新的预检。upgrade.sh / restore.sh 在停止服务前分别执行 `homeline backup ... --check` / `homeline restore ... --check`：检查路径、口令、主密钥，以及恢复归档的格式/完整性；预检不写入目标文件。错误口令或备份目标已存在时，不会先中断通话。预检通过不能消除随后磁盘/网络故障，实际维护仍应在线路空闲时执行。

备份先完整写入并同步临时文件，再以禁止覆盖的方式发布归档。数据库主密钥不匹配现存 SIP/推送密文时拒绝备份。只备份受支持的 PBX `.conf` 与专用 TLS 文件，忽略 `before-restore-*` 保留副本；连续备份/恢复不会因保留副本混入归档而失败。

恢复遇到普通写入错误会尝试回滚数据库、密钥、配置与 WAL/SHM 文件的整组变更。若恢复进程被强制终止或断电，会保留数据库目录下 `restore-in-progress.json`；API 和 doctor 拒绝启动，防止使用混合的新旧文件。不要直接删除该记录后启动。

异常中断时先停止两个服务，离线查看记录中的 `snapshots` 映射：每个原目标对应一个同目录的 `before-restore-*` 原始副本，null 表示该文件此前不存在。由管理员核对路径后恢复每个副本，清理本次新建且此前不存在的文件，恢复正确所有者/权限，并核查 SQLite 完整性及主密钥匹配；完成后将状态记录移到离线故障档案，再重新执行恢复命令。回滚本身因磁盘故障失败时同样保留记录与副本，需要先处理存储故障。普通 I/O 回滚和模拟进程中断已做自动化测试；真实掉电不作已实测声明。

恢复属于回到历史时间点，无法从旧备份知道哪些等待任务后来已拨出、哪些会话后来已撤销。因此恢复成功后所有账号先暂停，所有设备/会话撤销，FCM token 清除，生成的 SIP endpoint 文件清空；未完成呼叫/外呼任务标记为 failed/backup_restored，旧待发送指令终止。不会自动重拨备份中的等待任务。完成的历史记录保留。

通过本机受保护的 OS 账号运行 `homeline recover-admin --username <已有管理员名>`，隐藏输入新的管理员密码两次，只启用指定管理员。随后管理员审核其他账号、重置密码后再启用；其他管理员也不会自动恢复有效。设备需重新登录、重新注册 SIP/FCM。恢复时服务健康检查200只表示软件链路就绪，不表示旧会话或队列已恢复执行。使用这些流程前应安装本轮版本的API与CLI，避免只复制维护脚本却继续运行旧服务端。


## 第三方 FRP：HTTPS 加 TURN，无需转发 RTP 端口段

普通用户不需要把10000–10100作为公网FRP端口段开放。这只是Asterisk本地分配媒体socket的范围。第三方FRP只承载 HTTPS（API、事件WS、SIP/WSS），语音使用单独的、经过认证的公网 TURN 服务中继。Pi 和手机主动向 TURN 建立连接，通常不需要任何 FRP UDP 映射。

`deploy/frpc.example.toml` 提供 HTTPS 类型示例，按运营商/第三方FRP服务填写控制地址、鉴权及获配域名；HTTPS入口需可访问，默认客户端凭据使用公网443。保持透传到Pi的443，不使用https2http插件；证书安装在Pi上，host填对应公网域名。

TURN 必须是实际公网可达的中继，可自建在公网服务器或选用经过验证的TURN供应商。不能把一个普通FRP UDP端口填写成TURN地址，也不能靠缩小到单个RTP端口来支持任意多设备通话。TURN服务方需要负责中继端口和容量，终端用户只配置TURN连接参数；本项目不提供内置公共TURN账号，也不声称任意免费FRP服务可独立完成媒体中继。

复制 `deploy/turn.example.json` 到 `/root/homeline-turn.json`，填写自己的服务参数，设为0600。`server`/`username`/`password`用于PBX；`ice_servers`专门下发给已认证客户端，不应放TURN管理密钥或REST共享签名密钥。建议PBX和客户端用不同、限额且可撤销的TURN账号；当前配置为固定凭据，不提供每用户动态REST签发。根配置文件不可提交Git，已渲染的rtp.conf及server.env也含敏感信息。

固定版本Asterisk22.11.0的源码 `res/res_rtp_asterisk.c` 在配置turnaddr后经TCP创建TURN allocation；必须选择支持普通TURN/TCP、并提供IPv4地址或A记录的服务，`server`格式为 `host:port`，不接受turns URL。手机URL可按服务能力使用 `turn:...?transport=tcp` 或 `turns:...:5349?transport=tcp`。仅支持TLS入口且不提供兼容TCP入口的供应商不能直接作为此版本PBX的TURN上游。媒体仍使用DTLS-SRTP加密；PBX的TURN/TCP控制连接本身不是TLS。

新装添加 `--turn-config /root/homeline-turn.json`，自动同时生成PBX的TURN配置和API下发的客户端ICE配置。无需 `--media-address`；保留直连ICE候选便于LAN使用，外网由ICE协商选取可达中继。已有安装的配置不会被安装器重写，需在线路空闲时审核修改rtp.conf与HOMELINE_ICE_SERVERS并重启。公网/不同运营商/第三方FRP仍需实测；不要仅因页面可登录或SIP已注册就宣称音频正常。

## 通用 acme.sh 自动签发与续期

不需要向维护者提供域名或Token。可复制 `deploy/setup.example.sh` 为同目录 `setup.local.sh`，自行填写域名、邮箱、DNS插件名、DNS凭据JSON路径、TURN JSON路径和可选FXO地址，再执行：

```bash
sudo bash deploy/setup.local.sh
```

也可向下载的一键入口直接传参：

```bash
sudo bash /tmp/homeline-bootstrap.sh --host phone.example.com \
  --acme-email you@example.com --acme-dns dns_cf --dns-env /root/homeline-dns.json \
  --turn-config /root/homeline-turn.json
```

`--cert/--key` 与ACME模式互斥。`deploy/dns-credentials.example.json`以Cloudflare的CF_Token/CF_Zone_ID为例；其他DNS商按acme.sh官方dnsapi文档填写其变量名并选择相应dns_PLUGIN。文件必须root拥有、0600，内容是JSON数据，不会当shell脚本执行；Token仅传给acme.sh进程环境，不进入命令行。限制Token到需要的DNS区域和DNS编辑权限。

签发使用Let's Encrypt DNS-01，不依赖FRP开放80；DNS服务需要提供兼容API。acme.sh 3.1.6从固定上游提交独立安装到 `/opt/homeline-acme`，不混入MPL源码。账户与续期配置在 `/etc/homeline-acme`，托管证书在 `/etc/homeline-certs`。密钥/账户目录0700、证书私钥0600；Nginx root主进程读取源文件，Asterisk使用受限组副本。

systemd的homeline-acme.timer每天运行续期检查；成功安装证书后留下pending标记。homeline-cert-activate.timer每5分钟重试：校验证书、检测PBX零活动通道，暂停API后再次确认空闲，更新PBX副本、reload Nginx、重启PBX/API并检查ready；忙线或状态未知时延迟，不主动挂断正在进行的通话。维护切换窗口内的新呼叫可能失败，应监控证书到期时间及pending积压，不承诺完全无感更新。失败保留pending供重试。

已有安装若使用其他证书路径，独立ACME设置会拒绝直接改动；先审核把Nginx证书路径迁移到托管路径。bootstrap对已安装系统依然只做诊断，不暗中切换证书/网络策略。升级及新功能配置需按维护流程操作。

```bash
systemctl list-timers 'homeline-*'
journalctl -u homeline-acme -u homeline-cert-activate
```

加密业务备份不包含 `/etc/homeline-acme`、DNS API原始JSON及TURN原始JSON。PBX渲染配置和专用证书副本在备份内；完整灾难恢复需另外安全备份ACME账户/续期配置和外部证书路径。卸载会停止并移除上述timer/service，保留账户、证书及密钥文件，不让续期任务重新拉起已卸载服务。

真实DNS申请/续期和Pi系统定时任务仍需使用用户自己的域名及环境验收。隔离编排测试不冒充真实CA签发；本地coturn媒体测试也不冒充已验证任意第三方FRP网络。
