# 本次验证的协议

这份说明记录实际观察与成功互通的部分，不代表鲜牛全部模式。

## API

本次官方客户端连接 `https://api1.xianniu.com`。
桌面 POST 请求和对应响应的封装为：

```text
JSON UTF-8 → zlib → RC4（客户端内的固定兼容密钥）→ Base64 → HTTPS
```

登录额外使用 `/open/tools/datetime` 的服务端时间，并在 User-Agent 的
`RequestTime/` 字段和 JSON `datetime` 中携带同一时间。缺少该字段的请求实际被拒绝。
密码字段为 UTF-8 密码的 MD5 小写十六进制值；传输仍使用正常验证证书的 HTTPS。

观察和实现的操作：

| 路径 | 作用 |
| --- | --- |
| `/client/auth/login` | 登录并取得访问令牌 |
| `/client/member/details` | 读取账号与计时状态 |
| `/client/member/recovery` | 恢复账号计时 |
| `/client/member/suspend` | 暂停账号计时 |
| `/client/accelerate/linelist` | 查询游戏线路 |
| `/client/accelerate/start` | 开始加速、取得节点及线路凭据 |
| `/client/accelerate/token` | 续期线路凭据并上报实际流量 |
| `/client/accelerate/stop` | 停止当前游戏加速 |

计时状态的实际约定是 `pause_state = 0` 暂停、`1` 正在计时。
停止游戏加速与暂停账号计时是独立操作。官方退出还会注销访问令牌；
本客户端保存本机授权，授权过期时支持重新登录。

## 节点握手

固定头 8 字节：版本 `0x69`、命令、网络序目标端口、4 字节目标标记。
后面依次是四个以 uint32 小端长度开头的字节字段。

- 命令 `1`：TCP CONNECT。目标标记 `00 00 49 00`，第 3 个字段是用当前线路 token
  进行 RC4 处理的目标域名。前两个字段均为服务端下发的线路 user，第 4 个字段为空。
- 命令 `4`：UDP 分配。固定头 `69 04 00 01 00 00 00 01`。
  前两个字段均为下发的线路 user，其余两个字段为空。

这里只实现该实际观察到的变体，不将其当作标准 SOCKS4/5 或推断出其他认证模式。

成功响应 8 字节，状态为 `0x5a`。TCP 响应版本 `4`，UDP 响应版本 `2`。
UDP 响应的分配端口为小端序；连接到请求节点的同一 IPv4 地址。

## 数据

- TCP：每块明文流数据前添加 uint32 小端长度。观察到的最大帧限制为 204800 字节。
  游戏自己的 TLS 保持在其上层，测试中完成了真实证书校验。
- UDP：`fe` + 目标 IPv4（4 字节）+ 网络序端口（2 字节）+ UDP 负载。
  返回使用同样布局，标记原始来源。
- UDP 分配后的 TCP 建链连接可以由节点关闭，UDP 仍然有效。
  不能把此 EOF 误当成本地 SOCKS5 UDP 控制连接关闭。

线路 user/token 由加速 API 下发和轮换。源码、测试向量与发布包不含真实用户凭据。
