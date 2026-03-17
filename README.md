# HomeMachines

A single-script tool to **wake**, **sleep**, **check status**, and **SSH tunnel** into your home machines — across subnets, through jump hosts.

## Features

- **Wake-on-LAN** — Wake machines directly or through a jump host
- **Sleep/Hibernate** — Suspend Linux (`systemctl suspend`) or hibernate Windows (`shutdown /h`) remotely
- **Status Check** — Ping + SSH fallback for ICMP-blocked hosts; distinguishes "offline" from "jump host unreachable"
- **SSH Tunnel** — Local/remote port forwarding and SOCKS5 proxy with preset support
- **Jump Host Aware** — Automatically wakes jump host first; sleeps dependents before jump host
- **Startup Validation** — Catches invalid MAC addresses before they cause cryptic errors

## Requirements

- Python 3.7+
- SSH client
- `sshpass` — only required for machines without SSH key trust (password auth)
  - macOS: `brew install hudochenkov/sshpass/sshpass`
  - Linux: `sudo apt install sshpass` / `sudo yum install sshpass`
- No third-party Python packages needed

## 前置依赖安装

### macOS

```bash
# 1. 安装 Homebrew（已安装可跳过）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 安装 sshpass（仅当机器使用密码认证时需要）
brew install hudochenkov/sshpass/sshpass

# 3. 验证
sshpass -V
ssh -V
python3 --version
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y openssh-client sshpass python3
```

### CentOS / RHEL / Rocky Linux

```bash
sudo yum install -y openssh-clients sshpass python3
# 或使用 dnf：
sudo dnf install -y openssh-clients sshpass python3
```

> 安装完所有依赖后，执行 `python3 home_machines.py status all` 验证配置是否正确。

## Quick Start

### 1. Clone

```bash
git clone https://github.com/rateyu/homemachines.git
cd homemachines
```

### 2. Create config

Copy the example and fill in your real machine info:

```bash
cp machines.example.json machines.json
```

Edit `machines.json`:

```json
{
  "machines": {
    "my-pc": {
      "mac": "AA:BB:CC:DD:EE:FF",
      "ip": "192.168.0.100",
      "os": "windows",
      "ssh_user": "your_user",
      "ssh_port": 22,
      "broadcast": "192.168.0.255"
    },
    "my-linux": {
      "mac": "11:22:33:44:55:66",
      "ip": "192.168.1.100",
      "os": "linux",
      "ssh_user": "your_user",
      "ssh_port": 22,
      "broadcast": "192.168.1.255",
      "jump_host": "my-pc",
      "tunnels": {
        "jupyter": "-L 8888:127.0.0.1:8888",
        "web": "-L 8080:127.0.0.1:80",
        "proxy": "-D 1080"
      }
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `mac` | Yes | MAC address for WOL (format: `AA:BB:CC:DD:EE:FF`) |
| `ip` | Yes | IP address of the machine |
| `os` | Yes | `linux` or `windows` |
| `ssh_user` | Yes | SSH username |
| `ssh_port` | Yes | SSH port (usually `22`) |
| `broadcast` | Yes | Broadcast address for WOL |
| `jump_host` | No | Name of another machine to use as SSH jump host |
| `ssh_password` | No | SSH password (if no key trust); requires `sshpass` installed |
| `tunnels` | No | Predefined SSH tunnel presets |

### 3. Use

```bash
python home_machines.py status all       # Check all machines
python home_machines.py wake my-linux    # Wake a machine
python home_machines.py sleep my-linux   # Suspend a machine
python home_machines.py help             # Show full help
```

## Commands

### status

```bash
python home_machines.py status all
python home_machines.py status my-linux
```

Output examples:
```
  my-pc       192.168.0.100   ONLINE
  my-linux    192.168.1.100   ONLINE (via my-pc)
  my-linux    192.168.1.100   UNKNOWN (jump host my-pc offline)
  my-linux    192.168.1.100   OFFLINE
```

### wake

```bash
python home_machines.py wake all         # Wake all machines
python home_machines.py wake my-linux    # Wake a specific machine
```

If the target requires a jump host, the script will automatically wake the jump host first and wait for it to come online.

### sleep

```bash
python home_machines.py sleep all        # Sleep all (correct order)
python home_machines.py sleep my-linux   # Sleep a specific machine
```

When sleeping all machines, dependents are automatically suspended before their jump hosts.

### tunnel

```bash
# Local forwarding (access remote service locally)
python home_machines.py tunnel my-linux -L 8888:127.0.0.1:8888

# Remote forwarding (expose local service to remote)
python home_machines.py tunnel my-linux -R 9090:localhost:3000

# SOCKS5 proxy
python home_machines.py tunnel my-linux -D 1080

# Use a predefined preset
python home_machines.py tunnel my-linux --preset jupyter

# Multiple presets at once
python home_machines.py tunnel my-linux --preset jupyter,web

# List available presets
python home_machines.py tunnel my-linux --list
```

Press `Ctrl+C` to close the tunnel.

## Tips

### 配置 Shell 别名，快捷使用

每次都要进入项目目录再输入 `python home_machines.py ...` 非常繁琐。配置 alias 后，在任意目录都可以用简短命令操作。

在 `~/.zshrc`（macOS/Linux zsh）或 `~/.bashrc`（Linux bash）末尾添加：

```bash
# 家庭机器管理 - 快捷命令
alias hm='python3 /path/to/home_machines.py'

# SSH 快捷连接（按需添加，替换为你的实际 IP 和用户名）
alias ssh-pc='ssh your_user@192.168.0.100'
alias ssh-linux='ssh your_user@192.168.1.100'
```

保存后执行 `source ~/.zshrc` 使其生效。之后即可在任意目录使用：

```bash
hm status all           # 查看所有机器状态
hm wake my-linux        # 唤醒指定机器
hm sleep all            # 休眠所有机器（自动处理顺序）
hm tunnel my-linux --preset jupyter   # 建立隧道

ssh-pc                  # 快捷 SSH 登录
ssh-linux               # 快捷 SSH 登录
```

### Linux 休眠免密码配置

远程执行 `sudo systemctl suspend` 时，如果提示需要密码，需要在目标 Linux 机器上配置免密 sudo：

```bash
# 登录到目标 Linux 机器后执行（将 your_user 替换为你的用户名）：
echo 'your_user ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/suspend
sudo chmod 440 /etc/sudoers.d/suspend
```

配置后即可通过脚本自动休眠，无需手动输入密码。

### Windows 唤醒（WOL）不生效

如果 Windows 机器发送 WOL 后没有唤醒，请逐项检查：

1. **BIOS/UEFI** — 进入 BIOS，确认 `Wake on LAN` / `Wake on PCI-E` 已开启
2. **网卡高级属性** — 设备管理器 → 网卡 → 高级 → 启用 "Wake on Magic Packet"
3. **电源管理** — 设备管理器 → 网卡 → 电源管理 → 取消勾选 "允许计算机关闭此设备以节约电源"
4. **确认 MAC 地址** — 在 Windows 上运行 `ipconfig /all`，核对配置文件中的 MAC 是否一致

## License

MIT
