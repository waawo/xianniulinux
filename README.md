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
- 33 项自动测试通过。

以上真实节点验证运行在当前 Windows 主机上的同一份跨平台 Python 代码。
Linux 网络命名空间启动器已实现，但尚未在 Arch 实机运行；还不能据此声称
Warframe 在 Linux 中的完整登录、匹配和长时间游戏已验证。

## 在 Arch 使用

解压源码包，在解压后的 `xianniu-linux` 目录打开终端。

```bash
sudo pacman -S --needed python iproute2 util-linux sudo ca-certificates
```

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

### 4. 启动 Steam 和 Warframe

先完全退出已经运行的 Steam。首次建议使用 Arch 原生 Steam，暂不使用 Flatpak 版本。

```bash
sudo -E python3 ./xianniu run \
  --account "$HOME/.config/xianniu-linux/account.json" \
  -- steam
```

随后在新打开的 Steam 中运行 Warframe。需要更换 Hev 可执行文件位置时，
在 `run` 后添加 `--hev /完整路径/hev-socks5-tunnel`。

启动器会为这次 Steam 创建独立网络命名空间和临时虚拟网卡：

```text
Steam / Warframe → TUN → 本机 SOCKS5 → 鲜牛经典协议 → 鲜牛节点
```

只修改临时命名空间中的默认路由和 DNS；不修改主机默认路由、转发设置和防火墙。
游戏以原桌面用户身份运行。关闭这次 Steam 或按 Ctrl+C 后，启动器清理临时网络，
代理请求停止加速并暂停计时。

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

当前只支持观察到的经典单线路 IPv4 变体；不支持双专线、IPv6、其他协议模式和网页验证码流程。
没有实现官方的游戏规则库、专用 CDN 调度、掉线自动切线和下载优化。
Linux 实机接管、Warframe 联机效果和延迟改善仍需实际测试。

个人授权与原始研究资料保存在本机 `private/` 中，未放入源码包。
不要将个人 `account.json` 当作公开配置分享。
