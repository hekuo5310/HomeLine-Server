# 第三方软件与许可证

HomeLine 自有源代码许可证为 MPL-2.0，完整文本见 LICENSE。每个自有代码文件标注 SPDX-License-Identifier。此清单记录上游声明，不代表未核实的法律兼容性结论。

2026-10-18 已核查精确版本、上游元数据/源码和实际安装。完整60项锁定Python发行版的许可证声明、Requires-Python、源项目URL及安装包许可证文件SHA-256见 licenses/python-dependencies.json，可运行 tools/dependency_report.py 重建。运行依赖和测试工具均列出，requirements.lock保留精确传递版本。

| 直接组件 | 固定版本 | 上游许可证声明 | 使用方式 |
|---|---|---|---|
| FastAPI | 0.143.3 | MIT | API |
| SQLAlchemy | 2.0.55 | MIT | ORM，选取维护中的2.0系列 |
| Alembic | 1.20.0 | MIT | 迁移 |
| Pydantic / settings | 2.14.1 / 2.16.0 | MIT | DTO/配置 |
| Uvicorn | 0.54.0 | BSD-3-Clause | ASGI |
| HTTPX | 0.28.1 | BSD-3-Clause | loopback ARI HTTP |
| websockets | 16.1.1 | BSD-3-Clause | ARI事件，使用验证过的16系列 |
| Firebase Admin Python | 7.7.0 | Apache-2.0 | 官方FCM服务端SDK |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Fernet/Scrypt |
| argon2-cffi | 25.1.0 | MIT | 账号密码 |
| pytest / Ruff | 9.1.1 / 0.17.1 | MIT | 测试/静态检查 |
| aiortc（可选测试） | 1.14.0 | BSD-3-Clause | 程序化WebRTC测试端点 |

Python服务保持纯Python业务层，含本机扩展的上游依赖有manylinux/arm64发布支持；当前锁文件在本机Python3.12实际解析安装，CI验证3.11/3.13。不能据有arm64 wheel推断已经在Pi验证。依赖没有因为“最新”而盲目跨主版本；源码中实际API已检查，升级须重新运行集成测试。

Asterisk22.11.0独立源码从官方downloads.asterisk.org下载，SHA-256：3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94。其源码LICENSE说明GPL-2.0与可协商的其他授权，并附COPYING及例外说明。安装器完整保留上游源码归档/构建目录；不把Asterisk源码或二进制纳入HomeLine MPL文件，也不链接自有Python代码为Asterisk模块。通过ARI/PJSIP网络协议集成。再分发PBX二进制、修改模块、打包镜像时须单独审核其GPL及源代码提供义务，不能用本项目许可证覆盖。

PJSIP由Asterisk自身bundled pjproject构建路径管理（当前22.11.0使用pjproject2.17），保留其GPL/商业授权和Asterisk构建选择，不能把它归为MIT。OpenSSL、libsrtp、libedit、libxml2、SQLite、systemd、Nginx等由目标OS包管理器单独安装，许可证及版权以该发行版/usr/share/doc/<package>/copyright和Asterisk源码LICENSE/第三方说明为准。

可选媒体测试aiortc会引入PyAV/FFmpeg、pylibsrtp及其二进制/系统依赖。aiortc为BSD不意味着FFmpeg构建所有组件都为BSD；具体wheel的LGPL/GPL配置和再分发义务需按来源审查。它们不属于生产服务器强制依赖，也不在仓库分发二进制。Firebase服务另受Google服务条款约束。

官方来源：

- https://github.com/asterisk/asterisk/blob/22.11.0/LICENSE
- https://docs.asterisk.org/Asterisk_22_Documentation/
- https://firebase.google.com/docs/cloud-messaging/send/admin-sdk
- https://github.com/firebase/firebase-admin-python
- https://pypi.org/ （精确发行版元数据与仓库许可证链接见JSON清单）


可选证书工具acme.sh 3.1.6：官方仓库 https://github.com/acmesh-official/acme.sh ，固定提交807da6498377ee5e0cf43a78091f46f12dc59a89，LICENSE.md声明GPL-3.0。安装时独立下载、执行和保存上游许可证，不复制进HomeLine MPL源码或Python包；单独再分发时应满足其许可证。DNS API和CA另受供应商条款约束。

可选测试服务coturn由CI发行版包管理器安装，官方项目 https://github.com/coturn/coturn ，上游声明BSD-3-Clause，具体发行版包版权以/usr/share/doc/coturn/copyright为准。仅运行独立测试进程，不将代码或二进制放入MPL仓库。生产TURN供应商、自建TURN及frp需要各自配置、容量和许可证审查。
