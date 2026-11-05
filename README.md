# 鲜牛 Linux 经典模式客户端（实验版）

已实现一种实际观察并验证的鲜牛 PC 经典协议：`0x69`，IPv4，单线路。
目标是 Warframe 国际服；本次配置保留你在官方客户端选择的线路和区服。

客户端用 Python 3.11+ 标准库实现，不需要 Wine、Windows 虚拟机或鲜牛 Windows 引擎。
游戏流量接管使用独立的开源 `hev-socks5-tunnel`。

## 已验证范围

- 官方鲜牛完全退出后，自行进行手机号密码登录。
- 查询账号状态、恢复计时、启动线路、更新线路凭据。
- 通过本地 SOCKS5 接口访问 Warframe API，TLS 证书验证正常。
- 通过同一接口完成 UDP DNS 查询，收到有效响应。
- 停止加速，并由服务端确认账号计时已暂停。
- 协议、账号生命周期、跨进程互斥和启动器异常清理均有自动测试；最新结果见 `VALIDATION.md`。

以上真实节点验证运行在当前 Windows 主机上的同一份跨平台 Python 代码。
Linux 网络命名空间、TUN 和游戏专用启动入口已在 NixOS 服务器上用模拟节点验证，
没有在服务器上登录真实账号。Warframe 在桌面 Linux 中的完整登录、匹配和长时间游戏尚未验证。

## 在 Arch 使用

解压源码包，在解压后的 `xianniu-linux` 目录打开终端。

```bash
sudo pacman -S --needed python iproute2 util-linux sudo ca-certificates polkit
```

Debian 12 可使用 `sudo apt install python3 iproute2 util-linux sudo ca-certificates pkexec`。
使用 Steam 图形界面启动时，桌面需要正在运行的 polkit 认证代理。

### 1. 准备账号配置

这次研究生成的个人配置位于 Windows 工程目录的 `private/account.json`。
将该文件复制到 Linux 的 `~/.config/xianniu-linux/account.json`。
它已经包含本次选定的游戏、区服、线路和本机授权；源码压缩包不包含该个人文件。

```bash
mkdir -p ~/.config/xianniu-linux
chmod 700 ~/.config/xianniu-linux
# 将个人 account.json 复制到上述目录后：
chmod 600 ~/.config/xianniu-linux/account.json
```

不复制现有授权时，可以从示例开始：

```bash
mkdir -p ~/.config/xianniu-linux
cp examples/account.example.json ~/.config/xianniu-linux/account.json
python3 ./xianniu login --account ~/.config/xianniu-linux/account.json
```

`login` 在终端询问手机号和密码，密码输入不回显，也不写入配置或日志。
保存的账号令牌可用于后续启动；失效时再执行 `login`。服务端若要求额外验证，
客户端会返回错误，不尝试绕过验证。

### 2. 先检查节点连通性

确保官方鲜牛已经退出，避免两个客户端操作同一账号状态。

```bash
python3 ./xianniu check --account ~/.config/xianniu-linux/account.json
```

此命令会按鲜牛正常规则恢复计时、启动同线路、更新凭据，检查 TCP TLS 和 UDP，
最后停止加速并暂停计时。成功时应显示 `tcp_tls: true`、`udp_dns: true`，
以及 `Server confirmed test session stopped and billing paused.`。

访问 API 根路径得到 `HTTP 404` 仍可证明 TLS 与 HTTP 已穿过节点；
它不代表已登录 Warframe 游戏账号。

### 3. 安装游戏流量接管组件

按 [HevSocks5Tunnel 上游说明](https://github.com/heiher/hev-socks5-tunnel) 安装或构建。
例如从上游源码构建：

```bash
sudo pacman -S --needed base-devel git
git clone --recursive https://github.com/heiher/hev-socks5-tunnel
make -C hev-socks5-tunnel -j"$(nproc)"
sudo install -m755 hev-socks5-tunnel/bin/hev-socks5-tunnel /usr/local/bin/hev-socks5-tunnel
```

### 4. 只接管 Warframe

正常启动原生 Steam，不需要退出 Steam。暂不支持 Flatpak Steam。
在 **Warframe → 属性 → 通用 → 启动选项** 中填写以下一行，将源码路径替换为实际的绝对路径：

```text
python3 "/完整路径/xianniu-linux/xianniu" warframe -- %command%
```

默认读取 `~/.config/xianniu-linux/account.json`。自定义配置或 Hev 路径时：

```text
python3 "/完整路径/xianniu-linux/xianniu" warframe --account "/完整路径/account.json" --hev "/完整路径/hev-socks5-tunnel" -- %command%
```

点击 Steam 中的“开始游戏”，桌面会请求创建临时网络所需的管理员授权。
授权后，Warframe 启动命令及其 Proton／游戏启动器子进程进入独立网络命名空间：

```text
Steam 商店／社区／Steam 下载／其他游戏 → 原有网络
Warframe 启动器／Proton／游戏进程      → TUN → 本机 SOCKS5 → 鲜牛节点
```

只修改临时命名空间中的默认路由和 DNS；不修改主机默认路由、转发设置和防火墙。
游戏仍以原桌面用户身份运行，保留 Steam 的 Proton 环境、兼容前缀、显示环境和启动工作目录。
Warframe 自身启动器产生的更新流量也会经过加速；Steam 客户端执行的下载不会。

此入口检查 Steam 应用 ID `230410`，误用到其他游戏时会拒绝启动。
旧的 `run -- steam` 方式已禁用，避免把整个 Steam 放进加速网络。
`run -- <命令>` 仅保留用于独立应用和网络诊断，不用于启动 Steam 客户端。

游戏退出后清理临时网络并请求停止加速、暂停计时；Steam 结束该启动包装进程时也会触发清理。
取消管理员授权或启动失败时不会回退到无代理启动游戏。

退出时分别报告本地网络清理结果和服务端停止／暂停计时结果。服务端未确认、代理异常退出
或本地清理失败时，命令返回非零退出码；即使游戏正常退出也不会隐藏这些错误。

若没有认证窗口，请检查 `pkexec` 和桌面 polkit 认证代理。不要添加针对任意 Python 或
`ip netns exec` 的免密 sudo 规则。`--auth sudo` 只供有终端和 sudo 权限的诊断启动使用。
本项目的进程接管方式不代表官方鲜牛的游戏分流规则。

如果不能确认服务端已暂停，使用下面的恢复命令重试；不能仅凭本地进程退出认定计时已停：

```bash
python3 ./xianniu stop --account ~/.config/xianniu-linux/account.json
```

诊断文件保留在终端显示的 `/tmp/xianniu-.../` 目录。

## 其他用法

只运行本地 SOCKS5 服务（默认 `127.0.0.1:1080`）：

```bash
python3 ./xianniu serve --account ~/.config/xianniu-linux/account.json
```

服务启动、续期和退出由客户端管理。HTTP 代理环境变量不能替代游戏的 TCP/UDP 接管。

续期 API 连续失败两次，或出现凭据校验、文件写入等异常时，代理会退出并尝试停止加速、
暂停计时。一次暂时的 API 错误仍允许下一次续期重试。

同一系统用户只允许一个受管账号操作或加速会话：`serve --account`、`check --account`、
`warframe`／`run --account` 的代理、`login` 和 `stop` 共用操作系统进程锁。因为现有配置没有稳定的账号 ID，
暂时对该用户的所有账号串行处理，复制配置或更换令牌不会绕过锁。先正常退出已有会话，再重试。
进程退出后锁自动释放；不要删除锁文件。此锁不协调其他电脑、其他系统用户或官方客户端。

查询线路：

```bash
python3 ./xianniu lines --account ~/.config/xianniu-linux/account.json --area-id 1726
```

配置中的 `line_id` 和 `area_id` 决定线路与区服。示例数值来自本次账号界面的实际选择，
线路列表可能随服务端变化。

仅使用已有线路会话、不管理账号计时：

```bash
python3 ./xianniu serve --session /路径/session.json
```

这种模式不负责恢复或暂停账号计时；日常使用优先选择 `--account`。

## 测试与边界

```bash
python3 -m unittest discover -s tests -v
```

默认测试会模拟 Linux 命令与进程，覆盖网络创建、权限切换参数、正常退出、SIGTERM、
代理／隧道崩溃、启动超时和失败清理。真实 Linux 内核与 Hev 的集成测试默认跳过。
在已安装上述依赖的 Linux 上，从普通用户可读取的源码目录运行：

```bash
sudo -E env XIANNIU_LINUX_INTEGRATION=1 python3 -m unittest discover -s tests -p test_linux_integration.py -v
```

此集成测试使用本机模拟节点和虚拟凭据，验证真实 TUN 的 TCP/UDP 往返、非 root 应用身份、
命名空间 DNS、IPv6 禁用、Steam 父进程网络不变、Proton 环境传递、父进程退出后的清理，
不登录真实账号，也不会启动鲜牛计时。
它不能替代真实节点与 Warframe 登录、组队及长时间运行验证。

当前只支持观察到的经典单线路 IPv4 变体；不支持双专线、IPv6、其他协议模式和网页验证码流程。
没有实现官方的游戏规则库、专用 CDN 调度、掉线自动切线和下载优化。
NixOS 内核层接管已用模拟节点验证；实际桌面授权窗口、Steam／Proton 的完整游戏流程、
Warframe 联机效果和延迟改善仍需在游戏电脑上测试。

个人授权与原始研究资料保存在本机 `private/` 中，未放入源码包。
不要将个人 `account.json` 当作公开配置分享。
