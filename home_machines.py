#!/usr/bin/env python3
"""Home machine unified wake/sleep/tunnel management script.

Usage:
    python home_machines.py wake   <machine|all>
    python home_machines.py sleep  <machine|all>
    python home_machines.py status <machine|all>
    python home_machines.py tunnel <machine> -L [bind:]port:host:port
    python home_machines.py tunnel <machine> -R [bind:]port:host:port
    python home_machines.py tunnel <machine> -D [bind:]port
    python home_machines.py tunnel <machine> --preset <name>
    python home_machines.py tunnel <machine> --list
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "machines.json")


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)["machines"]


def validate_config(machines):
    """Validate config on startup. Exit with clear message if invalid."""
    for name, cfg in machines.items():
        mac = cfg.get("mac", "")
        clean = mac.replace(":", "").replace("-", "")
        if len(clean) != 12 or not all(c in "0123456789abcdefABCDEF" for c in clean):
            print(f"Error: machine '{name}' has invalid MAC address: {mac}")
            print(f"  Please edit machines.json with the real MAC address.")
            print(f"  Windows: ipconfig /all  |  Linux: ip link show")
            sys.exit(1)
        jump = cfg.get("jump_host")
        if jump and jump not in machines:
            print(f"Error: machine '{name}' references unknown jump_host '{jump}'")
            sys.exit(1)


def parse_mac(mac_str):
    """Parse MAC address string to bytes."""
    clean = mac_str.replace(":", "").replace("-", "")
    if len(clean) != 12 or not all(c in "0123456789abcdefABCDEF" for c in clean):
        raise ValueError(
            f"Invalid MAC address: '{mac_str}' — must be 12 hex characters. "
            f"Check machines.json and replace any placeholder values."
        )
    return bytes.fromhex(clean)


def build_magic_packet(mac_bytes):
    """Build WOL magic packet: 6x 0xFF + 16x MAC."""
    return b"\xff" * 6 + mac_bytes * 16


def send_wol(mac_str, broadcast="255.255.255.255"):
    """Send WOL magic packet via UDP broadcast."""
    mac_bytes = parse_mac(mac_str)
    packet = build_magic_packet(mac_bytes)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, 9))


def ping(ip):
    """Ping a host, return True if reachable."""
    param = "-c" if sys.platform != "win32" else "-n"
    try:
        result = subprocess.run(
            ["ping", param, "1", "-W", "2", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def ssh_alive(ip, user, port):
    """Check if a host is reachable via SSH (fallback when ping/ICMP is blocked)."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
             "-o", "BatchMode=yes", "-p", str(port), f"{user}@{ip}", "echo ok"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def host_alive(cfg):
    """Check if a host is reachable via ping, falling back to SSH."""
    if ping(cfg["ip"]):
        return True
    return ssh_alive(cfg["ip"], cfg["ssh_user"], cfg["ssh_port"])


def ssh_run(ip, user, port, command, jump_host_info=None):
    """Run a command on a remote machine via SSH.

    Args:
        ip: Target IP address.
        user: SSH username on target.
        port: SSH port on target.
        command: Command string to execute remotely.
        jump_host_info: Optional dict with keys ip, user, port for ProxyJump.
    """
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

    if jump_host_info:
        jump_str = f"{jump_host_info['user']}@{jump_host_info['ip']}:{jump_host_info['port']}"
        ssh_cmd += ["-J", jump_str]

    ssh_cmd += ["-p", str(port), f"{user}@{ip}", command]

    result = subprocess.run(ssh_cmd, capture_output=True, timeout=30)
    # Remote Windows (Chinese) may output GBK; decode leniently
    result.stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    result.stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    return result


def wait_for_host(ip, timeout=120, interval=5, ssh_cfg=None):
    """Wait until a host becomes reachable via ping (or SSH fallback).

    Args:
        ssh_cfg: Optional dict with ssh_user/ssh_port to try SSH when ping is blocked.
    """
    print(f"  Waiting for {ip} to come online...", end="", flush=True)
    elapsed = 0
    while elapsed < timeout:
        if ping(ip):
            print(" online!")
            return True
        if ssh_cfg and ssh_alive(ip, ssh_cfg["ssh_user"], ssh_cfg["ssh_port"]):
            print(" online (via SSH)!")
            return True
        print(".", end="", flush=True)
        time.sleep(interval)
        elapsed += interval
    print(" timeout!")
    return False


def resolve_targets(machines, target):
    """Resolve 'all' or a specific machine name to a list of (name, config) pairs."""
    if target == "all":
        return list(machines.items())
    if target not in machines:
        print(f"Error: unknown machine '{target}'")
        print(f"Available: {', '.join(machines.keys())}")
        sys.exit(1)
    return [(target, machines[target])]


def get_jump_host_info(machines, jump_host_name):
    """Get connection info for a jump host."""
    jh = machines[jump_host_name]
    return {"ip": jh["ip"], "user": jh["ssh_user"], "port": jh["ssh_port"]}


def ping_via_jump(machines, jump_host_name, target_ip):
    """Ping target_ip from the jump host via SSH. Returns True if reachable."""
    jh = machines[jump_host_name]
    if not host_alive(jh):
        return False
    # Windows: ping -n 1 -w 2000; Linux: ping -c 1 -W 2
    if jh.get("os") == "windows":
        cmd = f"ping -n 1 -w 2000 {target_ip}"
    else:
        cmd = f"ping -c 1 -W 2 {target_ip}"
    try:
        result = ssh_run(jh["ip"], jh["ssh_user"], jh["ssh_port"], cmd)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def check_online(machines, cfg):
    """Check if machine is online. Try direct ping first, then via jump host.

    Returns (status, detail) where status is one of:
        "online"       — machine responded to ping
        "jump_offline" — jump host unreachable, cannot determine target status
        "offline"      — machine did not respond
    """
    if host_alive(cfg):
        return "online", "direct"
    jump_host_name = cfg.get("jump_host")
    if jump_host_name:
        jh = machines[jump_host_name]
        if not host_alive(jh):
            return "jump_offline", jump_host_name
        if ping_via_jump(machines, jump_host_name, cfg["ip"]):
            return "online", f"via {jump_host_name}"
        return "offline", f"via {jump_host_name}"
    return "offline", ""


# ── Commands ──────────────────────────────────────────────────────────


def cmd_status(machines, target):
    targets = resolve_targets(machines, target)
    for name, cfg in targets:
        state, detail = check_online(machines, cfg)
        if state == "online":
            status = f"ONLINE ({detail})" if detail != "direct" else "ONLINE"
        elif state == "jump_offline":
            status = f"UNKNOWN (jump host {detail} offline)"
        else:
            status = "OFFLINE"
        print(f"  {name:20s} {cfg['ip']:15s} {status}")


def cmd_wake(machines, target):
    targets = resolve_targets(machines, target)
    for name, cfg in targets:
        jump_host_name = cfg.get("jump_host")

        if jump_host_name:
            # Need to wake via jump host
            jh_cfg = machines[jump_host_name]
            print(f"  {name}: requires jump host '{jump_host_name}'")

            # Ensure jump host is online
            if not host_alive(jh_cfg):
                print(f"  {name}: jump host offline, waking it first...")
                send_wol(jh_cfg["mac"], jh_cfg.get("broadcast", "255.255.255.255"))
                if not wait_for_host(jh_cfg["ip"], ssh_cfg=jh_cfg):
                    print(f"  {name}: FAILED — jump host did not come online")
                    continue
                # Give SSH service a moment to start
                time.sleep(5)

            # Send WOL from jump host
            # Build a python one-liner to send magic packet from the jump host
            mac_hex = cfg["mac"].replace(":", "").replace("-", "")
            broadcast = cfg.get("broadcast", "255.255.255.255")
            wol_script = (
                f"python3 -c \""
                f"import socket;"
                f"p=b'\\xff'*6+bytes.fromhex('{mac_hex}')*16;"
                f"s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                f"s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);"
                f"s.sendto(p,('{broadcast}',9));"
                f"s.close();"
                f"print('WOL sent')\""
            )
            jh_info = get_jump_host_info(machines, jump_host_name)
            try:
                result = ssh_run(
                    jh_cfg["ip"], jh_cfg["ssh_user"], jh_cfg["ssh_port"], wol_script
                )
                if result.returncode == 0:
                    print(f"  {name}: WOL sent via {jump_host_name}")
                else:
                    # Fallback: try powershell on Windows jump host
                    if jh_cfg.get("os") == "windows":
                        ps_cmd = _build_windows_wol_command(mac_hex, broadcast)
                        result = ssh_run(
                            jh_cfg["ip"], jh_cfg["ssh_user"], jh_cfg["ssh_port"], ps_cmd
                        )
                        if result.returncode == 0:
                            print(f"  {name}: WOL sent via {jump_host_name} (powershell)")
                        else:
                            print(f"  {name}: FAILED to send WOL via jump host")
                            print(f"    stderr: {result.stderr.strip()}")
                    else:
                        print(f"  {name}: FAILED to send WOL via jump host")
                        print(f"    stderr: {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"  {name}: FAILED — SSH to jump host timed out")
        else:
            # Direct WOL from this machine
            broadcast = cfg.get("broadcast", "255.255.255.255")
            send_wol(cfg["mac"], broadcast)
            print(f"  {name}: WOL packet sent")


def _build_windows_wol_command(mac_hex, broadcast):
    """Build a PowerShell command to send WOL packet."""
    return (
        f'powershell -Command "'
        f"$mac = '{mac_hex}';"
        f"$bytes = [byte[]](,0xFF * 6);"
        f"for ($i=0; $i -lt 16; $i++) {{"
        f"  for ($j=0; $j -lt 6; $j++) {{"
        f"    $bytes += [byte](\\\"0x$($mac.Substring($j*2,2))\\\")"
        f"  }}"
        f"}};"
        f"$udp = New-Object System.Net.Sockets.UdpClient;"
        f"$udp.Connect('{broadcast}', 9);"
        f"$udp.Send($bytes, $bytes.Length) | Out-Null;"
        f"$udp.Close();"
        f"Write-Output 'WOL sent'"
        f'"'
    )


def _jump_depth(machines, cfg, _seen=None):
    """Return how many jump hops this machine requires (0 = direct)."""
    if _seen is None:
        _seen = set()
    jump = cfg.get("jump_host")
    if not jump or jump in _seen:
        return 0
    _seen.add(jump)
    return 1 + _jump_depth(machines, machines[jump], _seen)


def cmd_sleep(machines, target):
    targets = resolve_targets(machines, target)
    # Sleep dependents first, jump hosts last
    targets.sort(key=lambda t: -_jump_depth(machines, t[1]))
    for name, cfg in targets:
        state, detail = check_online(machines, cfg)
        if state == "jump_offline":
            print(f"  {name}: cannot reach — jump host '{detail}' is offline, try waking it first")
            continue
        if state != "online":
            print(f"  {name}: already offline, skipping")
            continue
        if detail != "direct":
            print(f"  {name}: reachable {detail}")

        jump_host_name = cfg.get("jump_host")
        jh_info = None
        if jump_host_name:
            jh_info = get_jump_host_info(machines, jump_host_name)

        if cfg["os"] == "linux":
            command = "sudo systemctl suspend"
        elif cfg["os"] == "windows":
            command = "shutdown /h"
        else:
            print(f"  {name}: unsupported OS '{cfg['os']}'")
            continue

        try:
            result = ssh_run(
                cfg["ip"], cfg["ssh_user"], cfg["ssh_port"], command,
                jump_host_info=jh_info,
            )
            # suspend/hibernate may kill the SSH session, so returncode != 0 is expected
            if result.returncode == 0 or "closed by remote host" in result.stderr:
                print(f"  {name}: sleep command sent")
            elif "password is required" in result.stderr or "terminal is required" in result.stderr:
                print(f"  {name}: FAILED — sudo requires NOPASSWD for non-interactive use")
                print(f"    Fix: on {name}, run: sudo visudo -f /etc/sudoers.d/suspend")
                print(f"    Add: {cfg['ssh_user']} ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend")
            elif "Connection" in result.stderr or result.returncode == 255:
                print(f"  {name}: sleep command sent (connection dropped, expected)")
            else:
                print(f"  {name}: sleep command returned code {result.returncode}")
                if result.stderr.strip():
                    print(f"    stderr: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"  {name}: SSH timed out (machine may have suspended)")


def cmd_tunnel(machines, args):
    """Establish SSH tunnel to a machine."""
    target = args.target
    if target not in machines:
        print(f"Error: unknown machine '{target}'")
        print(f"Available: {', '.join(machines.keys())}")
        sys.exit(1)

    cfg = machines[target]
    presets = cfg.get("tunnels", {})

    # ── list presets ──
    if args.list:
        if not presets:
            print(f"  {target}: no predefined tunnels")
        else:
            print(f"  {target}: predefined tunnels:")
            for name, spec in presets.items():
                desc = _describe_tunnel_spec(spec)
                print(f"    {name:12s} {spec:36s}  ({desc})")
        return

    # ── resolve tunnel spec ──
    tunnel_flags = []

    if args.preset:
        # Can specify multiple presets: --preset jupyter,db
        for preset_name in args.preset.split(","):
            preset_name = preset_name.strip()
            if preset_name not in presets:
                print(f"Error: unknown preset '{preset_name}' for {target}")
                print(f"Available: {', '.join(presets.keys())}")
                sys.exit(1)
            tunnel_flags.extend(presets[preset_name].split())
    else:
        # Ad-hoc tunnel flags from -L / -R / -D
        if args.local_forward:
            for spec in args.local_forward:
                tunnel_flags += ["-L", spec]
        if args.remote_forward:
            for spec in args.remote_forward:
                tunnel_flags += ["-R", spec]
        if args.dynamic_forward:
            for spec in args.dynamic_forward:
                tunnel_flags += ["-D", spec]

    if not tunnel_flags:
        print("Error: specify -L, -R, -D, or --preset")
        print(f"  Run: python {sys.argv[0]} tunnel {target} --list")
        sys.exit(1)

    # ── build SSH command ──
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-N"]

    jump_host_name = cfg.get("jump_host")
    if jump_host_name:
        jh = machines[jump_host_name]
        jump_str = f"{jh['ssh_user']}@{jh['ip']}:{jh['ssh_port']}"
        ssh_cmd += ["-J", jump_str]

    ssh_cmd += tunnel_flags
    ssh_cmd += ["-p", str(cfg["ssh_port"]), f"{cfg['ssh_user']}@{cfg['ip']}"]

    # ── print summary ──
    print(f"  target: {target} ({cfg['ip']})")
    if jump_host_name:
        print(f"  jump:   {jump_host_name} ({machines[jump_host_name]['ip']})")
    for i in range(0, len(tunnel_flags), 2):
        flag, spec = tunnel_flags[i], tunnel_flags[i + 1] if i + 1 < len(tunnel_flags) else ""
        desc = _describe_tunnel_spec(f"{flag} {spec}")
        print(f"  tunnel: {flag} {spec}  ({desc})")
    print()
    print(f"  command: {' '.join(ssh_cmd)}")
    print()
    print("  Tunnel active. Press Ctrl+C to stop.")
    print()

    # ── run (blocking, Ctrl+C to stop) ──
    try:
        proc = subprocess.Popen(ssh_cmd)
        proc.wait()
    except KeyboardInterrupt:
        print("\n  Tunnel closed.")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _describe_tunnel_spec(spec):
    """Return a human-readable description of a tunnel spec string."""
    parts = spec.strip().split()
    if len(parts) < 2:
        return spec

    flag, value = parts[0], parts[1]

    if flag == "-L":
        # -L [bind:]port:host:port
        segments = value.split(":")
        if len(segments) == 3:
            local_port, rhost, rport = segments
            return f"local :{local_port} -> {rhost}:{rport}"
        elif len(segments) == 4:
            bind, local_port, rhost, rport = segments
            return f"local {bind}:{local_port} -> {rhost}:{rport}"
    elif flag == "-R":
        segments = value.split(":")
        if len(segments) == 3:
            rport, lhost, lport = segments
            return f"remote :{rport} -> {lhost}:{lport}"
        elif len(segments) == 4:
            bind, rport, lhost, lport = segments
            return f"remote {bind}:{rport} -> {lhost}:{lport}"
    elif flag == "-D":
        return f"SOCKS5 proxy :{value}"

    return spec


# ── Main ──────────────────────────────────────────────────────────────


def print_help(machines=None):
    """Print friendly help with machine names from config."""
    prog = os.path.basename(sys.argv[0])
    names = ""
    if machines:
        names = ", ".join(machines.keys())

    print(f"""\
Home Machine Manager - 家庭机器统一管理

Commands:
  status <machine|all>    Check if machines are online
  wake   <machine|all>    Wake machines via WOL (Wake-on-LAN)
  sleep  <machine|all>    Suspend/hibernate machines
  tunnel <machine> [opts] Establish SSH tunnel
  help                    Show this help

Machines ({len(machines) if machines else 0}):
  {names or '(config not loaded)'}

Quick start:
  {prog} status all                        Check all machines
  {prog} wake all                          Wake all machines
  {prog} sleep <name>                      Suspend a machine

Tunnel:
  {prog} tunnel <name> -L port:host:port   Local forward  (pull remote to local)
  {prog} tunnel <name> -R port:host:port   Remote forward (push local to remote)
  {prog} tunnel <name> -D port             SOCKS5 proxy
  {prog} tunnel <name> --preset <preset>   Use predefined tunnel
  {prog} tunnel <name> --preset a,b        Multiple presets
  {prog} tunnel <name> --list              List predefined tunnels

Tips:
  - sleep order: sleep machines behind jump host FIRST, then jump host
  - wake order: script auto-wakes jump host if needed, order doesn't matter
  - tunnel: Ctrl+C to close
""")


def main():
    # Handle: no args, "help", "-h", "--help" — all show friendly help
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "-h", "--help"):
        try:
            machines = load_config()
        except Exception:
            machines = None
        print_help(machines)
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="Home machine wake/sleep/tunnel management",
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="action")

    # ── wake / sleep / status ──
    wake_p = subparsers.add_parser("wake", help="Wake machines via WOL")
    wake_p.add_argument("target", help="Machine name or 'all'")

    sleep_p = subparsers.add_parser("sleep", help="Suspend/hibernate machines")
    sleep_p.add_argument("target", help="Machine name or 'all'")

    status_p = subparsers.add_parser("status", help="Ping check machines")
    status_p.add_argument("target", help="Machine name or 'all'")

    # ── tunnel ──
    tunnel_p = subparsers.add_parser(
        "tunnel",
        help="Establish SSH tunnel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
tunnel types:
  -L [bind:]local_port:remote_host:remote_port   Local forwarding (pull remote to local)
  -R [bind:]remote_port:local_host:local_port     Remote forwarding (push local to remote)
  -D [bind:]port                                  Dynamic SOCKS5 proxy

examples:
  tunnel linux-1153 -L 8888:127.0.0.1:8888       Jupyter on remote -> localhost:8888
  tunnel linux-1153 -R 9090:localhost:3000        Push local :3000 -> remote :9090
  tunnel linux-1153 -D 1080                       SOCKS5 proxy on localhost:1080
  tunnel linux-1153 --preset jupyter              Use predefined tunnel
  tunnel linux-1153 --preset jupyter,db           Multiple presets at once
""",
    )
    tunnel_p.add_argument("target", help="Machine name")
    tunnel_p.add_argument(
        "-L", dest="local_forward", action="append", metavar="[bind:]port:host:port",
        help="Local port forwarding (repeatable)",
    )
    tunnel_p.add_argument(
        "-R", dest="remote_forward", action="append", metavar="[bind:]port:host:port",
        help="Remote port forwarding (repeatable)",
    )
    tunnel_p.add_argument(
        "-D", dest="dynamic_forward", action="append", metavar="[bind:]port",
        help="Dynamic SOCKS5 proxy (repeatable)",
    )
    tunnel_p.add_argument(
        "--preset", metavar="name",
        help="Use predefined tunnel(s) from config, comma-separated",
    )
    tunnel_p.add_argument(
        "--list", action="store_true",
        help="List predefined tunnels for the machine",
    )

    args = parser.parse_args()

    if not args.action:
        try:
            machines = load_config()
        except Exception:
            machines = None
        print_help(machines)
        sys.exit(0)

    machines = load_config()
    validate_config(machines)
    print(f"[{args.action}]")

    if args.action == "status":
        cmd_status(machines, args.target)
    elif args.action == "wake":
        cmd_wake(machines, args.target)
    elif args.action == "sleep":
        cmd_sleep(machines, args.target)
    elif args.action == "tunnel":
        cmd_tunnel(machines, args)


if __name__ == "__main__":
    main()
