"""ptrm_inv — what ptrm knows about a host beyond its services, and what it does when one fails.

- inventory: hardware (machine, cpu, memory, GPUs with their VRAM, also the ones that were attached once and are gone),
  OS and its last boot, network (interfaces, medium: Wi-Fi/Ethernet, addresses, DHCP, routing, DNS, network name and
  category), firewall (and whether it lets ssh in on the current network), disks (space, health), power (AC/battery,
  sleep after idle, lid), sleep/wake/power-loss events, sshd (starts by itself? password/forwarding), the account
  (admin, password expiry) and the keys (host keys, the keys authorised there). Collected in the discovery's ssh call
  (ptrm_core.discover), for Windows (MSYS sshd: one PowerShell call), macOS and Linux.
- access: what this machine sees of the host — the ssh client's config for it, the name resolved, the known host key,
  our keys, and the network this machine was on.
- profiles: each distinct configuration a host was seen in (hardware · network · OS): a host that moves, joins another
  network or gets new hardware gets a new profile; going back to a known one is recognised.
- diff: what changed between two inventories ("rebooted at …", "GPU RTX 5060 Ti removed", "network RS285 → Office").
- risks: what can take the services of a host that is not a 24/7 server away (it sleeps after 2 h idle, ssh allowed on
  Private networks only, sshd does not start by itself, disk nearly full, on battery …).
- diagnose: when a service behind a port fails, the layer that broke (this machine's network, name resolution, host
  down or asleep, ssh blocked, sshd down, key refused, host key changed, service down, tunnel) with the evidence, what
  ptrm can fix itself, and the checklist for the human who has physical access (remote hands).
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import ptrm_core as core
from ptrm_core import clip, run, utc_now, when

# ---------------------------------------------------------------- the inventory script
# Windows (MSYS bash under Win32-OpenSSH): one PowerShell call, JSON out. Times in UTC.
WIN_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'; $ProgressPreference = 'SilentlyContinue'
$U = { param($t) if ($t) { $t.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } }
$o = [ordered]@{}
$os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem
$o.os = [ordered]@{ name = "$($os.Caption)".Trim(); version = "$($os.Version)"; build = "$($os.BuildNumber)"; boot = (& $U $os.LastBootUpTime) }
$o.machine = [ordered]@{ vendor = "$($cs.Manufacturer)"; model = "$($cs.Model)"; laptop = ($cs.PCSystemType -eq 2) }
$cpu = @(Get-CimInstance Win32_Processor)
$o.cpu = [ordered]@{ model = "$($cpu[0].Name)".Trim(); cores = ($cpu | Measure-Object NumberOfCores -Sum).Sum; threads = ($cpu | Measure-Object NumberOfLogicalProcessors -Sum).Sum }
$o.mem = [ordered]@{ total_mb = [math]::Round($os.TotalVisibleMemorySize / 1024); free_mb = [math]::Round($os.FreePhysicalMemory / 1024) }
$o.gpu_present = @(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name)" })
$o.gpu_dx = @(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\DirectX' | ForEach-Object { $p = Get-ItemProperty $_.PSPath
  if ($p.Description) { [ordered]@{ name = "$($p.Description)"; vram_mb = [math]::Round([double]$p.DedicatedVideoMemory / 1MB); seen = $(if ($p.LastSeen) { [DateTime]::FromFileTimeUtc($p.LastSeen).ToString('yyyy-MM-ddTHH:mm:ssZ') }) } } })
$o.disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { [ordered]@{ mount = $_.DeviceID; fs = "$($_.FileSystem)"; size_gb = [math]::Round($_.Size / 1GB, 1); free_gb = [math]::Round($_.FreeSpace / 1GB, 1) } })
$o.pdisks = @(Get-PhysicalDisk | ForEach-Object { [ordered]@{ name = "$($_.FriendlyName)"; media = "$($_.MediaType)"; bus = "$($_.BusType)"; health = "$($_.HealthStatus)"; size_gb = [math]::Round($_.Size / 1GB) } })
$ip = @(Get-NetIPAddress -AddressFamily IPv4)
$o.net = @(Get-NetAdapter | ForEach-Object { $a = $_; $mine = @($ip | Where-Object InterfaceIndex -eq $a.ifIndex)
  [ordered]@{ name = $a.Name; desc = "$($a.InterfaceDescription)"; status = "$($a.Status)"; mac = "$($a.MacAddress)"; speed = "$($a.LinkSpeed)"; medium = "$($a.PhysicalMediaType)"; virtual = [bool]$a.Virtual; ips = @($mine | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" }); origin = "$(($mine | Select-Object -First 1).PrefixOrigin)" } })
$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
$o.route = [ordered]@{ gateway = "$($r.NextHop)"; iface = "$($r.InterfaceAlias)" }
$o.dns = @(Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | ForEach-Object { $_.ServerAddresses } | Select-Object -Unique)
$o.netprofile = @(Get-NetConnectionProfile | ForEach-Object { [ordered]@{ name = "$($_.Name)"; iface = "$($_.InterfaceAlias)"; category = "$($_.NetworkCategory)" } })
$o.wifi = "$(netsh wlan show interfaces | Out-String)"
$o.fw = @(Get-NetFirewallProfile | ForEach-Object { [ordered]@{ profile = "$($_.Name)"; enabled = ("$($_.Enabled)" -eq 'True') } })
$o.fw_ssh = @(Get-NetFirewallRule -DisplayName '*ssh*' | Where-Object { "$($_.Direction)" -eq 'Inbound' } | ForEach-Object { [ordered]@{ name = "$($_.DisplayName)"; enabled = ("$($_.Enabled)" -eq 'True'); action = "$($_.Action)"; profile = "$($_.Profile)" } })
$b = Get-CimInstance Win32_Battery
$pq = { param($s, $k) $t = powercfg /q SCHEME_CURRENT $s $k | Out-String; $ac = $null; $dc = $null
  if ($t -match 'Current AC Power Setting Index:\s+0x([0-9a-fA-F]+)') { $ac = [convert]::ToInt32($matches[1], 16) }
  if ($t -match 'Current DC Power Setting Index:\s+0x([0-9a-fA-F]+)') { $dc = [convert]::ToInt32($matches[1], 16) }
  [ordered]@{ ac = $ac; dc = $dc } }
$o.power = [ordered]@{ battery = $(if ($b) { $b.EstimatedChargeRemaining } else { $null }); on_ac = $(if ($b) { $b.BatteryStatus -ne 1 } else { $true }); sleep_s = (& $pq SUB_SLEEP STANDBYIDLE); hibernate_s = (& $pq SUB_SLEEP HIBERNATEIDLE); lid = (& $pq SUB_BUTTONS LIDACTION) }
$s = Get-Service sshd
$o.sshd = [ordered]@{ status = "$($s.Status)"; start = "$($s.StartType)"; config = @(Get-Content "$env:ProgramData\ssh\sshd_config" | Where-Object { $_ -match '^\s*(Port|ListenAddress|PasswordAuthentication|PubkeyAuthentication|AllowTcpForwarding|AllowUsers|AllowGroups|DenyUsers|Match|AuthorizedKeysFile)\s' } | ForEach-Object { $_.Trim() }) }
$nu = net user $env:USERNAME 2>$null
$adm = $null; try { $adm = [bool](Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.Name -like "*\$env:USERNAME" }) } catch {}
$o.account = [ordered]@{ user = "$env:USERNAME"; admin = $adm; info = @($nu | Where-Object { $_ -match '^(Account active|Account expires|Password expires)' } | ForEach-Object { ($_ -replace '\s{2,}', ': ').Trim() }) }
$o.events = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power', 'Microsoft-Windows-Power-Troubleshooter', 'EventLog', 'Microsoft-Windows-Kernel-General'; Id = 1, 12, 13, 41, 42, 107, 506, 507, 6008; StartTime = (Get-Date).AddDays(-14) } -MaxEvents 40 | ForEach-Object { [ordered]@{ at = (& $U $_.TimeCreated); id = $_.Id; src = ($_.ProviderName -replace 'Microsoft-Windows-', '') } })
'INVJSON:' + ($o | ConvertTo-Json -Compress -Depth 5)
"""


def _ps_encoded(script: str) -> str:
    lines = [l.strip() for l in script.strip().splitlines() if l.strip() and not l.strip().startswith("#")]
    return base64.b64encode("\n".join(lines).encode("utf-16-le")).decode("ascii")


SSHD_KEYS = r"Port|ListenAddress|PasswordAuthentication|PubkeyAuthentication|AllowTcpForwarding|AllowUsers|AllowGroups|DenyUsers|Match|AuthorizedKeysFile"

# Runs inside DISCOVER_SCRIPT (W=1 on Windows/MSYS, $OS = uname -s), before its @@end.
INV_SCRIPT = r"""
echo "@@inv"
if [ $W = 1 ]; then powershell.exe -NoProfile -NonInteractive -EncodedCommand __PS__ < /dev/null 2>/dev/null
elif [ "$OS" = Darwin ]; then
echo "@@inv.os"; sw_vers -productName; sw_vers -productVersion; sysctl -n hw.model; sysctl -n kern.boottime
echo "@@inv.cpu"; sysctl -n machdep.cpu.brand_string; sysctl -n hw.physicalcpu; sysctl -n hw.logicalcpu
echo "@@inv.mem"; sysctl -n hw.memsize
echo "@@inv.gpu"; system_profiler SPDisplaysDataType 2>/dev/null | grep -E 'Chipset Model|VRAM'
echo "@@inv.df"; df -kP 2>/dev/null | awk 'NR>1 && $1 ~ /^\/dev\//'
echo "@@inv.smart"; diskutil info / 2>/dev/null | grep -E 'SMART Status|Media Name|Solid State|Disk Size'
echo "@@inv.ports"; networksetup -listallhardwareports 2>/dev/null
echo "@@inv.ifconfig"; ifconfig 2>/dev/null | grep -E '^[a-z]|^[[:space:]]+(inet |ether |status|media)'
echo "@@inv.route"; route -n get default 2>/dev/null | grep -E 'gateway|interface'
echo "@@inv.dns"; scutil --dns 2>/dev/null | awk '/nameserver\[/{print $3}' | sort -u | head -6
echo "@@inv.wifi"; for d in $(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi|AirPort/{getline; print $2}'); do networksetup -getairportnetwork $d 2>/dev/null; done
echo "@@inv.fw"; /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null; /usr/libexec/ApplicationFirewall/socketfilterfw --getblockall 2>/dev/null
echo "@@inv.power"; pmset -g batt 2>/dev/null | head -3; pmset -g custom 2>/dev/null | grep -E 'Power:| sleep |autorestart|womp'
echo "@@inv.events"; pmset -g log 2>/dev/null | tail -n 6000 | grep -E '^[0-9-]+ [0-9:]+ [+-][0-9]{4} (Sleep|Wake|DarkWake|Shutdown|Start)[[:space:]][[:space:]]' | tail -12 | cut -c1-140
echo "@@inv.sshd"; grep -hE '^[[:space:]]*(__SSHD__)[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null
echo "@@inv.account"; id -un; id -Gn
else
echo "@@inv.os"; ( . /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"; echo "$ID $VERSION_ID" ); echo "vendor $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)"; echo "model $(cat /sys/class/dmi/id/product_name 2>/dev/null)"; echo "chassis $(cat /sys/class/dmi/id/chassis_type 2>/dev/null)"; echo "btime $(awk '/^btime/{print $2}' /proc/stat 2>/dev/null)"
echo "@@inv.cpu"; lscpu 2>/dev/null | grep -E '^(Model name|Socket\(s\)|Core\(s\) per socket|CPU\(s\)):'
echo "@@inv.mem"; awk '/MemTotal/{print $2}' /proc/meminfo
echo "@@inv.gpu"; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | sed 's/^/nvidia,/'; for f in /sys/class/drm/card*/device/mem_info_vram_total; do [ -r "$f" ] && echo "amd,$(cat $f)"; done; lspci 2>/dev/null | grep -Ei 'vga|3d controller|display' | cut -d: -f3- | sed 's/^ */pci,/'
echo "@@inv.df"; df -PkT -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | tail -n +2
echo "@@inv.blk"; lsblk -dno NAME,ROTA,SIZE,TRAN,MODEL 2>/dev/null | grep -v '^loop'
echo "@@inv.links"; for i in /sys/class/net/*; do n=${i##*/}; [ "$n" = lo ] && continue; t=ethernet; [ -d $i/wireless ] && t=wifi; [ -e $i/device ] || t=virtual; echo "$n $t $(cat $i/operstate 2>/dev/null) $(cat $i/address 2>/dev/null) $(cat $i/speed 2>/dev/null)"; done
echo "@@inv.addr"; ip -o -4 addr show 2>/dev/null | awk '{print $2, $4, ($0 ~ /dynamic/ ? "dhcp" : "static")}'
echo "@@inv.route"; ip route show default 2>/dev/null | head -2
echo "@@inv.dns"; grep '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}'
echo "@@inv.wifi"; iwgetid -r 2>/dev/null || nmcli -t -f active,ssid,signal dev wifi 2>/dev/null | grep '^yes'
echo "@@inv.fw"; ufw status 2>&1 | head -2; firewall-cmd --state 2>&1 | head -1
echo "@@inv.power"; for p in /sys/class/power_supply/*; do [ -e "$p" ] && echo "${p##*/} $(cat $p/type 2>/dev/null) online=$(cat $p/online 2>/dev/null) cap=$(cat $p/capacity 2>/dev/null) $(cat $p/status 2>/dev/null)"; done; echo "sleep $(systemctl is-enabled sleep.target 2>/dev/null) $(systemctl is-enabled suspend.target 2>/dev/null)"; grep -hE '^[[:space:]]*HandleLidSwitch=' /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf 2>/dev/null | tail -1
echo "@@inv.events"; last -x -F reboot shutdown 2>/dev/null | head -6
echo "@@inv.sshd"; echo "unit $(systemctl is-enabled ssh 2>/dev/null || systemctl is-enabled sshd 2>/dev/null)"; grep -hE '^[[:space:]]*(__SSHD__)[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null
echo "@@inv.account"; id -un; id -Gn; chage -l "$(id -un)" 2>/dev/null | grep -E 'Password expires|Account expires'
fi
echo "@@inv.hostkeys"; for f in /etc/ssh/ssh_host_*_key.pub /c/ProgramData/ssh/ssh_host_*_key.pub; do [ -r "$f" ] && ssh-keygen -lf "$f" 2>/dev/null; done
echo "@@inv.authkeys"; [ -r ~/.ssh/authorized_keys ] && ssh-keygen -lf ~/.ssh/authorized_keys 2>/dev/null | awk '{print "user", $2}'; f=/c/ProgramData/ssh/administrators_authorized_keys; [ -r $f ] && ssh-keygen -lf $f 2>/dev/null | awk '{print "admin", $2}'
""".replace("__PS__", _ps_encoded(WIN_PS)).replace("__SSHD__", SSHD_KEYS)


# ---------------------------------------------------------------- parsing into one shape for every OS
def _int(s, default=None):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _num(s) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or ""))
    return float(m.group(0)) if m else None


def mac_norm(m: str) -> str:
    parts = re.split(r"[:-]", str(m or "").strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        return ""
    return ":".join(p.zfill(2) for p in parts)


def os_short(system: str, name: str = "", version: str = "", build: str = "") -> str:
    """win11 · win10 · ws2022 · mac13 · ubu24 · deb12 · fed40 · arch … (DASH's OS chip)."""
    n = (name or "").lower()
    if system == "Windows" or "windows" in n:
        if "server" in n:
            m = re.search(r"20\d\d", n)
            return "ws" + (m.group(0) if m else "")
        b = _int(build, 0) or _int((version or "").split(".")[-1], 0)
        return "win11" if b >= 22000 or "windows 11" in n else "win10" if b or "windows 10" in n else "win"
    if system == "Darwin":
        return "mac" + (version.split(".")[0] if version else "")
    ident, _, ver = (name or "").partition(" ")
    major = (ver or "").split(".")[0]
    short = {"ubuntu": "ubu", "debian": "deb", "fedora": "fed", "rocky": "rocky", "almalinux": "alma", "rhel": "rhel",
             "centos": "cent", "alpine": "alp", "arch": "arch", "manjaro": "manj", "opensuse-leap": "suse",
             "opensuse-tumbleweed": "suse", "linuxmint": "mint", "pop": "pop", "nixos": "nix", "raspbian": "rpi"}.get(ident.lower(), ident.lower()[:5] or "linux")
    return short + ("" if short in ("arch", "manj", "suse", "nix") else major)


WIN_EVENTS = {("Kernel-Power", 42): "sleep", ("Kernel-Power", 107): "wake", ("Power-Troubleshooter", 1): "wake",
              ("Kernel-Power", 506): "standby", ("Kernel-Power", 507): "standby-exit",
              ("Kernel-Power", 41): "power-loss", ("EventLog", 6008): "power-loss",
              ("Kernel-General", 12): "boot", ("Kernel-General", 13): "shutdown"}
LAPTOP_CHASSIS = {8, 9, 10, 14, 30, 31, 32}


def _local_to_utc(stamp: str) -> str:
    """'2026-11-25 16:22:12 +0700' → '2026-11-25T09:22:12Z'."""
    import datetime as dt
    try:
        t = dt.datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S %z")
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def _epoch_iso(sec) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(sec)))
    except (TypeError, ValueError, OverflowError):
        return ""


def _sshd_conf(lines: List[str]) -> dict:
    """The few sshd settings that decide whether ptrm gets in (the first value wins, as in sshd; Match blocks noted)."""
    out: dict = {"match": []}
    in_match = False
    for l in lines:
        f = re.split(r"\s+", l.strip(), 1)
        k, v = f[0], (f[1].strip() if len(f) > 1 else "")
        if not k:
            continue
        if k.lower() == "match":
            in_match = True
            out["match"].append(v)
            continue
        key = k.lower()
        if in_match:
            out.setdefault("in_match", []).append(f"{k} {v}")
            continue
        out.setdefault(key, v)
    return {"port": _int(out.get("port"), 22), "password_auth": (out.get("passwordauthentication") or "yes (default)"),
            "forwarding": (out.get("allowtcpforwarding") or "yes (default)"), "listen": out.get("listenaddress", ""),
            "allow": " ".join(x for x in (out.get("allowusers"), out.get("allowgroups")) if x),
            "match": out["match"], "in_match": out.get("in_match", [])}


def parse_inventory(sec: Dict[str, List[str]], windows: bool, system: str = "") -> dict:
    """The inventory sections of a discovery's output, in one shape for every OS (see the module doc)."""
    g = lambda k: [l for l in sec.get(k, []) if l.strip()]
    inv: dict = {"at": utc_now()}
    if windows:
        raw = next((l[len("INVJSON:"):] for l in g("inv") if l.startswith("INVJSON:")), "")
        try:
            w = json.loads(raw) if raw else {}
        except ValueError:
            w = {}
        inv.update(_from_windows(w))
    elif system == "Darwin":
        inv.update(_from_macos(sec, g))
    else:
        inv.update(_from_linux(sec, g))
    inv["keys"] = {"host": sorted({l.split()[1] for l in g("inv.hostkeys") if len(l.split()) > 1}),
                   "authorized": [l.split()[1] for l in g("inv.authkeys") if l.startswith("user ")],
                   "authorized_admin": [l.split()[1] for l in g("inv.authkeys") if l.startswith("admin ")]}
    inv["complete"] = bool(inv.get("os", {}).get("name") or inv.get("cpu", {}).get("model"))
    return inv


def _from_windows(w: dict) -> dict:
    o: dict = {}
    os_ = w.get("os") or {}
    o["os"] = {"system": "Windows", "name": re.sub(r"^Microsoft\s+", "", os_.get("name") or ""), "version": os_.get("version", ""),
               "build": os_.get("build", ""), "boot": os_.get("boot") or ""}
    o["os"]["short"] = os_short("Windows", o["os"]["name"], o["os"]["version"], o["os"]["build"])
    m = w.get("machine") or {}
    o["machine"] = {"vendor": m.get("vendor", ""), "model": m.get("model", ""), "laptop": bool(m.get("laptop"))}
    c = w.get("cpu") or {}
    o["cpu"] = {"model": re.sub(r"\s+", " ", c.get("model") or ""), "cores": _int(c.get("cores")), "threads": _int(c.get("threads"))}
    mem = w.get("mem") or {}
    o["mem"] = {"total_gb": round((mem.get("total_mb") or 0) / 1024), "free_gb": round((mem.get("free_mb") or 0) / 1024, 1)}
    present = [n for n in w.get("gpu_present") or [] if n]
    gpus, gone = [], []
    dx: Dict[str, dict] = {}
    for x in w.get("gpu_dx") or []:
        n = x.get("name") or ""
        if not n or re.search(r"Basic Render|Remote Display", n):
            continue
        if n not in dx or (x.get("seen") or "") > (dx[n].get("seen") or ""):
            dx[n] = x
    for n in present:
        if re.search(r"Remote Display|Basic Display", n):
            continue
        x = dx.get(n) or {}
        gpus.append({"name": n, "vram_gb": round((x.get("vram_mb") or 0) / 1024) if x.get("vram_mb") else None})
    for n, x in dx.items():
        if n not in present:
            gone.append({"name": n, "vram_gb": round((x.get("vram_mb") or 0) / 1024), "last_seen": x.get("seen") or ""})
    o["gpus"], o["gpus_gone"] = gpus, gone
    o["disks"] = [{"mount": d.get("mount"), "fs": d.get("fs", ""), "size_gb": d.get("size_gb"), "free_gb": d.get("free_gb")}
                  for d in w.get("disks") or []]
    o["pdisks"] = [{"name": d.get("name", ""), "media": d.get("media", ""), "health": d.get("health", ""), "size_gb": d.get("size_gb")}
                   for d in w.get("pdisks") or []]
    ifaces = []
    for a in w.get("net") or []:
        med = (a.get("medium") or "").lower()
        kind = ("virtual" if a.get("virtual") or "virtual" in (a.get("desc") or "").lower() else
                "wifi" if "802.11" in med else "ethernet" if "802.3" in med else "bluetooth" if "bluetooth" in med else "other")
        if kind == "bluetooth" or a.get("status") == "Not Present":
            continue
        ips = [i for i in a.get("ips") or [] if not i.startswith("169.254.")]
        ifaces.append({"name": a.get("name"), "kind": kind, "up": a.get("status") == "Up", "mac": mac_norm(a.get("mac")),
                       "speed": a.get("speed", ""), "ips": ips, "dhcp": (a.get("origin") or "").lower() == "dhcp",
                       "desc": clip(a.get("desc"), 60)})
    r = w.get("route") or {}
    prof = w.get("netprofile") or []
    wifi = _netsh_wifi(w.get("wifi") or "")
    o["net"] = {"ifaces": ifaces, "route": {"gateway": r.get("gateway", ""), "iface": r.get("iface", "")},
                "dns": w.get("dns") or [], "profiles": [{"name": p.get("name"), "iface": p.get("iface"), "category": p.get("category")} for p in prof],
                "wifi": wifi}
    fw = w.get("fw") or []
    rules = [x for x in w.get("fw_ssh") or [] if x.get("enabled") and str(x.get("action")).lower() == "allow"]
    o["fw"] = {"on": any(p.get("enabled") for p in fw) if fw else None,
               "profiles": {p.get("profile"): bool(p.get("enabled")) for p in fw},
               "ssh_allowed_on": sorted({s.strip() for x in rules for s in str(x.get("profile") or "").split(",") if s.strip()}),
               "ssh_rules": [f"{x.get('name')} ({x.get('profile')})" for x in w.get("fw_ssh") or []][:4]}
    p = w.get("power") or {}
    sl, hb, lid = p.get("sleep_s") or {}, p.get("hibernate_s") or {}, p.get("lid") or {}
    mins = lambda v: None if v is None else round(v / 60)
    o["power"] = {"on_ac": p.get("on_ac", True), "battery": p.get("battery"), "sleep_ac_min": mins(sl.get("ac")),
                  "sleep_dc_min": mins(sl.get("dc")), "hibernate_ac_min": mins(hb.get("ac")),
                  "lid": {0: "nothing", 1: "sleep", 2: "hibernate", 3: "shutdown"}.get(lid.get("ac")) if o["machine"]["laptop"] else None}
    s = w.get("sshd") or {}
    o["sshd"] = dict(_sshd_conf(s.get("config") or []), status=s.get("status", ""), start=s.get("start", ""))
    a = w.get("account") or {}
    info = {k.strip().lower(): v.strip() for k, _, v in (x.partition(":") for x in a.get("info") or [])}
    o["account"] = {"user": a.get("user", ""), "admin": a.get("admin"), "active": info.get("account active", ""),
                    "password_expires": info.get("password expires", ""), "account_expires": info.get("account expires", "")}
    ev = []
    for e in w.get("events") or []:
        what = WIN_EVENTS.get((e.get("src"), e.get("id")))
        if what:
            ev.append({"at": e.get("at"), "what": what})
    o["events"] = sorted(ev, key=lambda e: e["at"] or "")[-24:]
    return o


def _netsh_wifi(text: str) -> dict:
    out = {}
    for k, key in (("SSID", "ssid"), ("Signal", "signal"), ("State", "state"), ("Radio type", "radio"), ("Band", "band")):
        m = re.search(rf"^\s*{k}\s*:\s*(.+?)\s*$", text, re.M)
        if m:
            out[key] = m.group(1)
    if "location permission" in text:
        out["note"] = "SSID hidden (Windows location permission off)"
    return out


def _from_macos(sec, g) -> dict:
    o: dict = {}
    os_ = g("inv.os")
    boot = ""
    for l in os_:
        m = re.search(r"sec = (\d+)", l)
        if m:
            boot = _epoch_iso(m.group(1))
    model = os_[2].strip() if len(os_) > 2 else ""
    o["os"] = {"system": "Darwin", "name": f"{os_[0].strip() if os_ else 'macOS'} {os_[1].strip() if len(os_) > 1 else ''}".strip(),
               "version": os_[1].strip() if len(os_) > 1 else "", "build": "", "boot": boot}
    o["os"]["short"] = os_short("Darwin", version=o["os"]["version"])
    o["machine"] = {"vendor": "Apple", "model": model, "laptop": model.startswith("MacBook")}
    c = g("inv.cpu")
    o["cpu"] = {"model": c[0].strip() if c else "", "cores": _int(c[1]) if len(c) > 1 else None, "threads": _int(c[2]) if len(c) > 2 else None}
    mem = _int((g("inv.mem") or ["0"])[0], 0)
    o["mem"] = {"total_gb": round(mem / 2 ** 30), "free_gb": None}
    gpus, cur = [], None
    for l in g("inv.gpu"):
        k, _, v = l.strip().partition(":")
        v = v.strip()
        if k == "Chipset Model":
            cur = {"name": v, "vram_gb": None}
            gpus.append(cur)
        elif k.startswith("VRAM") and cur is not None:
            n = _num(v) or 0
            gb = n / 1024 if "MB" in v else n
            cur["vram_gb"] = round(gb, 1) if gb < 2 else round(gb)
            if "Dynamic" in k:
                cur["shared"] = True
    if "arm64" in (sec.get("os") or [""])[2:3] or (o["cpu"]["model"] or "").startswith("Apple"):
        for x in gpus:
            x["unified"] = True
    o["gpus"], o["gpus_gone"] = gpus, []
    o["disks"], seen = [], set()
    for l in g("inv.df"):
        m = re.match(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+(.+)$", l)
        if not m or not (m.group(5) == "/" or m.group(5).startswith("/Volumes/")):
            continue
        size, free = round(int(m.group(2)) / 2 ** 20, 1), round(int(m.group(4)) / 2 ** 20, 1)
        if (size, free) in seen:  # APFS volumes of one container share its space
            continue
        seen.add((size, free))
        o["disks"].append({"mount": m.group(5), "fs": "apfs", "size_gb": size, "free_gb": free})
    sm = {k.strip(): v.strip() for k, _, v in (l.partition(":") for l in g("inv.smart"))}
    o["pdisks"] = [{"name": sm.get("Device / Media Name", ""), "media": "SSD" if sm.get("Solid State") == "Yes" else "",
                    "health": sm.get("SMART Status", ""), "size_gb": round((_num(sm.get("Disk Size")) or 0))}] if sm else []
    # interfaces: hardware ports (name, device) + ifconfig (status, inet, ether)
    ports, cur = {}, None
    for l in g("inv.ports"):
        if l.startswith("Hardware Port:"):
            cur = l.split(":", 1)[1].strip()
        elif l.startswith("Device:") and cur:
            ports[l.split(":", 1)[1].strip()] = cur
    ifc: Dict[str, dict] = {}
    name = None
    for l in g("inv.ifconfig"):
        m = re.match(r"^([a-z0-9]+):", l)
        if m:
            name = m.group(1)
            ifc[name] = {"name": name, "ips": [], "mac": "", "up": False}
            continue
        if not name:
            continue
        s = l.strip()
        if s.startswith("inet "):
            f = s.split()
            mask = f[3] if len(f) > 3 and f[2] == "netmask" else ""
            try:
                plen = bin(int(mask, 16)).count("1") if mask.startswith("0x") else 32
            except ValueError:
                plen = 32
            if not f[1].startswith(("127.", "169.254.")):
                ifc[name]["ips"].append(f"{f[1]}/{plen}")
        elif s.startswith("ether "):
            ifc[name]["mac"] = mac_norm(s.split()[1])
        elif s.startswith("status:"):
            ifc[name]["up"] = "active" in s
    ifaces = []
    for dev, x in ifc.items():
        port = ports.get(dev, "")
        if not port and not x["ips"] and not dev.startswith(("utun", "en")):
            continue
        kind = ("wifi" if re.search(r"Wi-Fi|AirPort", port) else "ethernet" if re.search(r"Ethernet|LAN|Thunderbolt Ethernet", port)
                else "virtual" if dev.startswith(("utun", "bridge", "awdl", "llw", "gif", "stf", "anpi", "ap")) or "Bridge" in port else "other")
        if kind in ("virtual", "other") and not x["ips"]:
            continue
        ifaces.append({"name": dev + (f" ({port})" if port else ""), "dev": dev, "kind": kind, "up": x["up"] or bool(x["ips"]),
                       "mac": x["mac"], "speed": "", "ips": x["ips"], "dhcp": None, "desc": port})
    rt = {k.strip(): v.strip() for k, _, v in (l.partition(":") for l in g("inv.route"))}
    wifi = {}
    for l in g("inv.wifi"):
        m = re.match(r"Current Wi-Fi Network:\s*(.+)", l.strip())
        if m:
            wifi["ssid"] = m.group(1)
    o["net"] = {"ifaces": ifaces, "route": {"gateway": rt.get("gateway", ""), "iface": next((i["name"] for i in ifaces if i.get("dev") == rt.get("interface")), rt.get("interface", ""))},
                "dns": g("inv.dns")[:6], "profiles": [], "wifi": wifi}
    fw = " ".join(g("inv.fw"))
    o["fw"] = {"on": True if "enabled" in fw else False if "disabled" in fw else None, "profiles": {},
               "ssh_allowed_on": [], "ssh_rules": [], "detail": clip(fw, 120)}
    pw = g("inv.power")
    txt = "\n".join(pw)
    section, vals = None, {"AC": {}, "Battery": {}}
    for l in pw:
        if l.startswith("AC Power"):
            section = "AC"
        elif l.startswith("Battery Power"):
            section = "Battery"
        elif section and l.strip():
            k, _, v = l.strip().partition(" ")
            vals[section][k] = _int(v.strip())
    batt = re.search(r"(\d+)%", txt)
    o["power"] = {"on_ac": "AC Power" in (pw[0] if pw else "AC Power"), "battery": _int(batt.group(1)) if batt else None,
                  "sleep_ac_min": vals["AC"].get("sleep"), "sleep_dc_min": vals["Battery"].get("sleep"), "hibernate_ac_min": None,
                  "lid": "sleep" if o["machine"]["laptop"] else None,
                  "autorestart": vals["AC"].get("autorestart"), "wake_on_lan": vals["AC"].get("womp")}
    o["sshd"] = dict(_sshd_conf(g("inv.sshd")), status="", start="launchd (Remote Login)")
    acc = g("inv.account")
    o["account"] = {"user": acc[0].strip() if acc else "", "admin": "admin" in (acc[1].split() if len(acc) > 1 else []),
                    "active": "", "password_expires": "", "account_expires": ""}
    ev = []
    for l in g("inv.events"):
        m = re.match(r"^(\S+ \S+ [+-]\d{4}) (Sleep|Wake|DarkWake|Shutdown|Start)\s", l)
        if m:
            ev.append({"at": _local_to_utc(m.group(1)), "what": {"Sleep": "sleep", "Wake": "wake", "DarkWake": "wake",
                                                                  "Shutdown": "shutdown", "Start": "boot"}[m.group(2)]})
    o["events"] = ev[-24:]
    return o


def _from_linux(sec, g) -> dict:
    o: dict = {}
    os_ = g("inv.os")
    kv = {l.split(" ", 1)[0]: l.split(" ", 1)[1].strip() if " " in l else "" for l in os_[2:]}
    idver = os_[1] if len(os_) > 1 else ""
    o["os"] = {"system": "Linux", "name": os_[0].strip() if os_ else "Linux", "version": idver.partition(" ")[2],
               "build": "", "boot": _epoch_iso(kv.get("btime")) if kv.get("btime") else ""}
    o["os"]["short"] = os_short("Linux", idver)
    o["machine"] = {"vendor": kv.get("vendor", ""), "model": kv.get("model", ""), "laptop": _int(kv.get("chassis"), 0) in LAPTOP_CHASSIS}
    lc = {k.strip(): v.strip() for k, _, v in (l.partition(":") for l in g("inv.cpu"))}
    cores = (_int(lc.get("Core(s) per socket"), 0) or 0) * (_int(lc.get("Socket(s)"), 1) or 1)
    o["cpu"] = {"model": lc.get("Model name", ""), "cores": cores or None, "threads": _int(lc.get("CPU(s)"))}
    kb = _int((g("inv.mem") or ["0"])[0], 0)
    o["mem"] = {"total_gb": round(kb / 2 ** 20), "free_gb": None}
    gpus = []
    for l in g("inv.gpu"):
        src, _, rest = l.partition(",")
        if src == "nvidia":
            n, _, mb = rest.rpartition(",")
            gpus.append({"name": n.strip(), "vram_gb": round((_num(mb) or 0) / 1024)})
        elif src == "amd":
            gpus.append({"name": "AMD GPU", "vram_gb": round((_num(rest) or 0) / 2 ** 30)})
        elif src == "pci" and not any(x.get("vram_gb") for x in gpus):
            gpus.append({"name": clip(rest.strip(), 60), "vram_gb": None})
    o["gpus"], o["gpus_gone"] = gpus, []
    o["disks"] = []
    for l in g("inv.df"):
        f = l.split()
        if len(f) >= 7 and f[0].startswith("/dev/"):
            o["disks"].append({"mount": f[6], "fs": f[1], "size_gb": round(_int(f[2], 0) / 2 ** 20, 1), "free_gb": round(_int(f[4], 0) / 2 ** 20, 1)})
    o["pdisks"] = []
    for l in g("inv.blk"):
        f = l.split(None, 4)
        if len(f) >= 3:
            o["pdisks"].append({"name": (f[4] if len(f) > 4 else f[0]).strip(), "media": "HDD" if f[1] == "1" else "SSD",
                                "health": "", "size_gb": round(_num(f[2]) or 0)})
    addrs: Dict[str, List[Tuple[str, bool]]] = {}
    for l in g("inv.addr"):
        f = l.split()
        if len(f) >= 2 and not f[1].startswith(("127.", "169.254.")):
            addrs.setdefault(f[0], []).append((f[1], len(f) > 2 and f[2] == "dhcp"))
    ifaces = []
    for l in g("inv.links"):
        f = l.split()
        if len(f) < 3:
            continue
        n, kind, state = f[0], f[1], f[2]
        if kind == "virtual" and not addrs.get(n):
            continue
        ifaces.append({"name": n, "kind": kind, "up": state == "up", "mac": mac_norm(f[3]) if len(f) > 3 else "",
                       "speed": f"{f[4]} Mbps" if len(f) > 4 and f[4].isdigit() else "",
                       "ips": [a for a, _ in addrs.get(n, [])], "dhcp": any(d for _, d in addrs.get(n, [])), "desc": ""})
    rt = (g("inv.route") or [""])[0]
    gw = re.search(r"via (\S+)", rt)
    dev = re.search(r"dev (\S+)", rt)
    wl = g("inv.wifi")
    wifi = {}
    if wl:
        if wl[0].startswith("yes:"):
            f = wl[0].split(":")
            wifi = {"ssid": f[1] if len(f) > 1 else "", "signal": (f[2] + "%") if len(f) > 2 else ""}
        else:
            wifi = {"ssid": wl[0].strip()}
    o["net"] = {"ifaces": ifaces, "route": {"gateway": gw.group(1) if gw else "", "iface": dev.group(1) if dev else ""},
                "dns": g("inv.dns")[:6], "profiles": [], "wifi": wifi}
    fw = " ".join(g("inv.fw"))
    on = True if re.search(r"Status: active|^running|\brunning\b", fw) else False if re.search(r"Status: inactive|not running", fw) else None
    o["fw"] = {"on": on, "profiles": {}, "ssh_allowed_on": [], "ssh_rules": [], "detail": clip(fw, 120) if on is None and fw else ""}
    pw = g("inv.power")
    ac = next((("online=1" in l) for l in pw if " Mains " in f" {l} "), None)
    bat = next((_int(re.search(r"cap=(\d+)", l).group(1)) for l in pw if " Battery " in f" {l} " and re.search(r"cap=(\d+)", l)), None)
    sl = next((l for l in pw if l.startswith("sleep ")), "")
    lid = next((l.split("=", 1)[1].strip() for l in pw if "HandleLidSwitch=" in l), None)
    o["power"] = {"on_ac": True if ac is None else ac, "battery": bat,
                  "sleep_ac_min": 0 if "masked" in sl else None, "sleep_dc_min": None, "hibernate_ac_min": None,
                  "lid": (lid or "suspend") if o["machine"]["laptop"] else None, "sleep_targets": sl[6:].strip()}
    sd = g("inv.sshd")
    unit = next((l[5:].strip() for l in sd if l.startswith("unit ")), "")
    o["sshd"] = dict(_sshd_conf([l for l in sd if not l.startswith("unit ")]), status="", start=unit)
    acc = g("inv.account")
    ch = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in acc[2:])}
    o["account"] = {"user": acc[0].strip() if acc else "", "admin": bool(set((acc[1] if len(acc) > 1 else "").split()) & {"sudo", "wheel", "admin"}),
                    "active": "", "password_expires": ch.get("password expires", ""), "account_expires": ch.get("account expires", "")}
    ev = []
    for l in g("inv.events"):
        m = re.match(r"^(reboot|shutdown)\s+.*?(\w{3} \w{3} +\d+ [\d:]+ \d{4})", l)
        if m:
            try:
                t = time.strptime(re.sub(r" +", " ", m.group(2)), "%a %b %d %H:%M:%S %Y")
                ev.append({"at": _epoch_iso(time.mktime(t)), "what": "boot" if m.group(1) == "reboot" else "shutdown"})
            except ValueError:
                pass
    o["events"] = sorted(ev, key=lambda e: e["at"])[-24:]
    return o


# ---------------------------------------------------------------- reading an inventory
def primary_iface(inv: dict) -> dict:
    net = inv.get("net") or {}
    name = (net.get("route") or {}).get("iface") or ""
    ifs = net.get("ifaces") or []
    return (next((i for i in ifs if i.get("name") == name or i.get("dev") == name), None)
            or next((i for i in ifs if i.get("up") and i.get("ips") and i.get("kind") in ("ethernet", "wifi")), None) or {})


def network_name(inv: dict, iface: Optional[dict] = None) -> str:
    net = inv.get("net") or {}
    iface = iface or primary_iface(inv)
    p = next((p for p in net.get("profiles") or [] if p.get("iface") == iface.get("name")), None)
    if iface.get("kind") == "wifi" and (net.get("wifi") or {}).get("ssid"):
        return net["wifi"]["ssid"]
    return (p or {}).get("name") or ((net.get("wifi") or {}).get("ssid") if iface.get("kind") == "wifi" else "") or ""


def network_category(inv: dict) -> str:
    iface = primary_iface(inv)
    p = next((p for p in (inv.get("net") or {}).get("profiles") or [] if p.get("iface") == iface.get("name")), None)
    return (p or {}).get("category") or ""


def primary_ip(inv: dict) -> str:
    ips = primary_iface(inv).get("ips") or []
    return ips[0].split("/")[0] if ips else ""


def all_ips(inv: dict) -> List[str]:
    out = []
    for i in (inv.get("net") or {}).get("ifaces") or []:
        if i.get("kind") in ("ethernet", "wifi", "other"):
            out += [x.split("/")[0] for x in i.get("ips") or []]
    p = primary_ip(inv)
    return ([p] if p else []) + [x for x in out if x != p]


def all_macs(inv: dict) -> List[str]:
    return [i["mac"] for i in (inv.get("net") or {}).get("ifaces") or [] if i.get("mac")]


def subnet(cidr: str) -> str:
    try:
        return str(ipaddress.ip_interface(cidr).network)
    except ValueError:
        return ""


def gpu_name(n: str) -> str:
    return clip(re.sub(r"\s+", " ", re.sub(r"\(R\)|\(TM\)|NVIDIA |GeForce |Graphics|Intel |AMD |Radeon ", "", n or "")).strip(), 30)


def vram_chip(inv: dict) -> str:
    """'16G', '2×16G', '24+16G', 'uni' (Apple Silicon: unified memory), '—' (no dedicated GPU memory known)."""
    gs = [x for x in inv.get("gpus") or [] if not x.get("shared") and x.get("vram_gb") and x["vram_gb"] >= 2]
    if not gs:
        return "uni" if any(x.get("unified") for x in inv.get("gpus") or []) else "—"
    v = sorted((int(x["vram_gb"]) for x in gs), reverse=True)
    if len(v) == 1:
        return f"{v[0]}G"
    return f"{len(v)}×{v[0]}G" if len(set(v)) == 1 else "+".join(map(str, v[:3])) + "G"


def chips(inv: dict, fallback: Optional[dict] = None) -> List[Tuple[str, str]]:
    """[(label, value)] for DASH's HOSTS line: OS, CPU (cores), MEM (GB), GPU (VRAM)."""
    fb = fallback or {}
    os_ = inv.get("os") or {}
    short = os_.get("short") or ("win" if fb.get("windows") else ((fb.get("os") or {}).get("system") or "?")[:5].lower())
    cpu = inv.get("cpu") or {}
    cores = cpu.get("cores") or cpu.get("threads") or (fb.get("cpu") or {}).get("count") or "?"
    mem = (inv.get("mem") or {}).get("total_gb") or round(((fb.get("mem_mb") or {}).get("total") or 0) / 1024) or "?"
    return [("OS", short), ("CPU", str(cores)), ("MEM", f"{mem}G" if mem != "?" else "?"), ("GPU", vram_chip(inv) if inv else "?")]


def net_brief(inv: dict) -> str:
    i = primary_iface(inv)
    if not i:
        return ""
    kind = {"wifi": "wifi", "ethernet": "eth"}.get(i.get("kind"), i.get("kind") or "")
    name = network_name(inv, i)
    return " ".join(x for x in (kind, name and f"'{name}'", primary_ip(inv)) if x)


def net_chips(inv: dict) -> List[Tuple[str, str]]:
    """[(what, value)] for DASH's NET chips: the medium of the route's interface (eth, wifi …), the network's name
    (Windows connection profile, Wi-Fi SSID), its IP. Empty when the inventory says nothing about the network."""
    i = primary_iface(inv)
    if not i:
        return []
    kind = {"wifi": "wifi", "ethernet": "eth"}.get(i.get("kind"), i.get("kind") or "?")
    return [("type", kind), ("name", network_name(inv, i) or "-"), ("ip", primary_ip(inv) or "-")]


def summary(inv: dict) -> str:
    """One line for the LLM: what the host is, where it is, how it can fail."""
    if not inv:
        return ""
    b = []
    os_, m, cpu = inv.get("os") or {}, inv.get("machine") or {}, inv.get("cpu") or {}
    b.append(f"{os_.get('name') or os_.get('short')}" + (f" (boot {when(os_['boot'])})" if os_.get("boot") else ""))
    if m.get("model"):
        b.append(f"{m['model']}" + (" laptop" if m.get("laptop") else ""))
    if cpu.get("model"):
        cm = clip(re.sub(r"\(R\)|\(TM\)|CPU|@.*", "", cpu["model"]).strip(), 36)
        b.append(f"{cm} {cpu.get('cores') or '?'}c/{cpu.get('threads') or '?'}t")
    b.append(f"{(inv.get('mem') or {}).get('total_gb')}G RAM")
    gs = [gpu_name(x["name"]) + (f" {x['vram_gb']}G" if x.get("vram_gb") and x["vram_gb"] >= 2 else "") for x in inv.get("gpus") or []]
    if gs:
        b.append("GPU " + ", ".join(clip(g, 40) for g in gs[:3]))
    for x in inv.get("gpus_gone") or []:
        b.append(f"(GPU {gpu_name(x['name'])} {x.get('vram_gb') or ''}G not attached, last {when(x.get('last_seen'))})")
    nb = net_brief(inv)
    if nb:
        cat = network_category(inv)
        b.append("net " + nb + (f" gw {inv['net']['route'].get('gateway')}" if (inv.get("net") or {}).get("route", {}).get("gateway") else "")
                 + (f" ({cat})" if cat else ""))
    d = [f"{x['mount']} {x['free_gb']}/{x['size_gb']}G free" for x in inv.get("disks") or [] if x.get("size_gb")]
    if d:
        b.append("disk " + ", ".join(d[:3]))
    p = inv.get("power") or {}
    pw = []
    if p.get("battery") is not None:
        pw.append(f"battery {p['battery']}%" + ("" if p.get("on_ac") else " DISCHARGING"))
    if p.get("sleep_ac_min"):
        pw.append(f"sleeps after {p['sleep_ac_min']} min idle (AC)")
    elif p.get("sleep_ac_min") == 0:
        pw.append("never sleeps on AC")
    if pw:
        b.append(" · ".join(pw))
    s = inv.get("sshd") or {}
    if s.get("start") or s.get("status"):
        b.append(f"sshd {s.get('status') or ''} start={s.get('start') or '?'}".replace("  ", " "))
    return " · ".join(b)


# ---------------------------------------------------------------- profiles and differences
def facets(inv: dict) -> dict:
    i = primary_iface(inv)
    ip = (i.get("ips") or [""])[0]
    return {
        "hw": {"model": (inv.get("machine") or {}).get("model", ""), "cpu": (inv.get("cpu") or {}).get("model", ""),
               "mem_gb": (inv.get("mem") or {}).get("total_gb"), "gpus": sorted(x["name"] for x in inv.get("gpus") or []),
               "disks": sorted(f"{x.get('name')} {x.get('size_gb')}G" for x in inv.get("pdisks") or [])},
        "net": {"medium": i.get("kind", ""), "network": network_name(inv, i), "subnet": subnet(ip) if ip else "",
                "gateway": ((inv.get("net") or {}).get("route") or {}).get("gateway", "")},
        "sw": {"os": (inv.get("os") or {}).get("short", ""), "build": (inv.get("os") or {}).get("build") or (inv.get("os") or {}).get("version", "")},
    }


def profile_label(inv: dict) -> str:
    f = facets(inv)
    n = f["net"]
    gpu = vram_chip(inv)
    return " · ".join(x for x in (
        f"{n['medium'] or '?'} '{n['network']}' {primary_ip(inv)}".replace(" ''", ""),
        f"{f['hw']['mem_gb']}G" + (f" GPU {gpu}" if gpu not in ("—", "") else ""),
        f["sw"]["os"]) if x)


def match_profile(profiles: List[dict], inv: dict) -> Optional[dict]:
    f = facets(inv)
    return next((p for p in profiles if p.get("hw") == f["hw"] and p.get("net") == f["net"] and p.get("sw") == f["sw"]), None)


def diff(old: dict, new: dict) -> List[dict]:
    """What changed between two inventories: [{what, sev}] — sev "!" when it can take services away."""
    if not old or not new:
        return []
    out: List[dict] = []

    def add(what, sev=""):
        out.append({"what": what, "sev": sev})

    fo, fn = facets(old), facets(new)
    oo, on = old.get("os") or {}, new.get("os") or {}
    if on.get("boot") and oo.get("boot") and abs(core.epoch(on["boot"]) - core.epoch(oo["boot"])) > 90:
        add(f"rebooted at {when(on['boot'])}", "!")
    if fo["sw"] != fn["sw"] and on.get("name"):
        add(f"OS {oo.get('name')} {fo['sw']['build']} → {on.get('name')} {fn['sw']['build']}")
    if fo["hw"]["model"] != fn["hw"]["model"]:
        add(f"machine {fo['hw']['model']} → {fn['hw']['model']} (another machine at this name?)", "!")
    if fo["hw"]["cpu"] != fn["hw"]["cpu"]:
        add(f"CPU {fo['hw']['cpu']} → {fn['hw']['cpu']}", "!")
    if fo["hw"]["mem_gb"] != fn["hw"]["mem_gb"]:
        add(f"memory {fo['hw']['mem_gb']}G → {fn['hw']['mem_gb']}G", "!")
    og = {x["name"]: x for x in old.get("gpus") or []}
    ng = {x["name"]: x for x in new.get("gpus") or []}
    for n in ng:
        if n not in og:
            add(f"GPU {n} {ng[n].get('vram_gb') or ''}G attached".replace(" G ", " "))
    for n in og:
        if n not in ng:
            add(f"GPU {n} {og[n].get('vram_gb') or ''}G removed (unplugged / not detected)".replace(" G ", " "), "!")
    od = set(fo["hw"]["disks"])
    nd = set(fn["hw"]["disks"])
    for x in nd - od:
        add(f"disk added: {x}")
    for x in od - nd:
        add(f"disk gone: {x}", "!")
    oh = {x.get("name"): x.get("health") for x in old.get("pdisks") or []}
    for x in new.get("pdisks") or []:
        if x.get("health") and oh.get(x.get("name")) and oh[x["name"]] != x["health"]:
            add(f"disk {x['name']} health {oh[x['name']]} → {x['health']}", "!" if x["health"] not in ("Healthy", "Verified") else "")
    of = {x["mount"]: x for x in old.get("disks") or []}
    for x in new.get("disks") or []:
        y = of.get(x["mount"])
        if not y or x.get("free_gb") is None or y.get("free_gb") is None:
            continue
        size = x.get("size_gb") or 1
        low = x["free_gb"] < max(10, size * 0.08)
        if abs(x["free_gb"] - y["free_gb"]) >= max(20, size * 0.1) or (low and y["free_gb"] >= max(10, size * 0.08)):
            add(f"disk {x['mount']} free {y['free_gb']}G → {x['free_gb']}G", "!" if low else "")
    if fo["net"] != fn["net"]:
        a, b = fo["net"], fn["net"]
        bits = []
        if a["medium"] != b["medium"]:
            bits.append(f"{a['medium'] or '?'} → {b['medium'] or '?'}")
        if a["network"] != b["network"]:
            bits.append(f"network '{a['network']}' → '{b['network']}'")
        if a["subnet"] != b["subnet"]:
            bits.append(f"subnet {a['subnet']} → {b['subnet']}")
        if a["gateway"] != b["gateway"]:
            bits.append(f"gateway {a['gateway']} → {b['gateway']}")
        add("network: " + ", ".join(bits), "!")
    if primary_ip(old) != primary_ip(new) and primary_ip(new):
        add(f"IP {primary_ip(old)} → {primary_ip(new)}", "!" if fo["net"]["subnet"] == fn["net"]["subnet"] else "")
    oc, nc = network_category(old), network_category(new)
    if oc != nc and nc:
        add(f"network category {oc or '?'} → {nc}", "!" if nc == "Public" else "")
    ofw, nfw = old.get("fw") or {}, new.get("fw") or {}
    if ofw.get("on") != nfw.get("on") and nfw.get("on") is not None:
        add(f"firewall {'on' if ofw.get('on') else 'off'} → {'on' if nfw.get('on') else 'off'}")
    if ofw.get("ssh_allowed_on") != nfw.get("ssh_allowed_on") and (ofw.get("ssh_allowed_on") or nfw.get("ssh_allowed_on")):
        add(f"firewall lets ssh in on: {','.join(ofw.get('ssh_allowed_on') or []) or 'nothing'} → {','.join(nfw.get('ssh_allowed_on') or []) or 'nothing'}", "!")
    op, np_ = old.get("power") or {}, new.get("power") or {}
    for k, label in (("sleep_ac_min", "sleep after idle (AC, min)"), ("lid", "lid action"), ("autorestart", "restart after power loss")):
        if op.get(k) != np_.get(k) and (op.get(k) is not None or np_.get(k) is not None):
            add(f"{label} {op.get(k)} → {np_.get(k)}", "!" if k == "sleep_ac_min" and np_.get(k) else "")
    if op.get("on_ac") and np_.get("on_ac") is False:
        add(f"now on battery ({np_.get('battery')}%)", "!")
    os_, ns = old.get("sshd") or {}, new.get("sshd") or {}
    for k in ("start", "status", "port", "password_auth", "forwarding"):
        if os_.get(k) != ns.get(k) and ns.get(k) not in (None, ""):
            add(f"sshd {k} {os_.get(k)} → {ns.get(k)}", "!" if k in ("start", "port", "forwarding") else "")
    oa, na = old.get("account") or {}, new.get("account") or {}
    for k in ("admin", "active", "password_expires"):
        if oa.get(k) != na.get(k) and na.get(k) not in (None, ""):
            add(f"account {k} {oa.get(k)} → {na.get(k)}", "!" if k != "admin" else "")
    ok_, nk = old.get("keys") or {}, new.get("keys") or {}
    if ok_.get("host") and nk.get("host") and set(ok_["host"]) != set(nk["host"]):
        add("host keys changed (reinstalled / another machine?)", "!")
    if len(ok_.get("authorized") or []) != len(nk.get("authorized") or []) and nk.get("authorized") is not None:
        add(f"authorized_keys {len(ok_.get('authorized') or [])} → {len(nk.get('authorized') or [])} keys")
    return out


def risks(inv: dict, access: Optional[dict] = None) -> List[dict]:
    """What can take this host's services away: [{sev, text, fix}] (sev "!" = likely, "" = worth knowing)."""
    out: List[dict] = []
    if not inv:
        return out
    win = (inv.get("os") or {}).get("system") == "Windows"
    mac = (inv.get("os") or {}).get("system") == "Darwin"
    p = inv.get("power") or {}
    if p.get("sleep_ac_min"):
        out.append({"sev": "!", "text": f"sleeps after {p['sleep_ac_min']} min idle on AC (ssh and tunnels do not keep it awake)",
                    "fix": ("powercfg /change standby-timeout-ac 0" if win else "sudo pmset -c sleep 0" if mac
                            else "sudo systemctl mask sleep.target suspend.target")})
    if (inv.get("machine") or {}).get("laptop") and p.get("lid") in ("sleep", "suspend", "hibernate"):
        out.append({"sev": "", "text": f"a laptop: closing the lid makes it {p['lid']}", "fix": "keep the lid open, or set the lid action to nothing on AC"})
    if p.get("on_ac") is False:
        out.append({"sev": "!", "text": f"runs on battery ({p.get('battery')}%)", "fix": "plug the charger"})
    if p.get("autorestart") == 0:
        out.append({"sev": "", "text": "does not power on by itself after a power cut", "fix": "sudo pmset -c autorestart 1"})
    for d in inv.get("disks") or []:
        if d.get("size_gb") and d.get("free_gb") is not None and d["free_gb"] < max(10, d["size_gb"] * 0.08):
            out.append({"sev": "!", "text": f"disk {d['mount']} nearly full ({d['free_gb']}G free of {d['size_gb']}G)", "fix": "free space (models, logs, caches)"})
    for d in inv.get("pdisks") or []:
        if d.get("health") and d["health"] not in ("Healthy", "Verified", "OK"):
            out.append({"sev": "!", "text": f"disk {d.get('name')} health: {d['health']}", "fix": "back up; replace the disk"})
    s = inv.get("sshd") or {}
    st = str(s.get("start") or "")
    if (win and st and st != "Automatic") or (st and not win and not mac and st not in ("enabled", "alias", "static", "indirect")):
        out.append({"sev": "!", "text": f"sshd does not start by itself after a reboot (start: {st})",
                    "fix": "Set-Service sshd -StartupType Automatic" if win else "sudo systemctl enable ssh"})
    if str(s.get("forwarding", "")).lower().startswith("no"):
        out.append({"sev": "!", "text": "sshd refuses port forwarding (AllowTcpForwarding no): tunnels cannot work", "fix": "AllowTcpForwarding yes in sshd_config"})
    fw = inv.get("fw") or {}
    cat = network_category(inv)
    allowed = fw.get("ssh_allowed_on") or []
    if win and fw.get("on") and allowed and "Any" not in allowed:
        if cat and cat not in allowed and not (cat == "DomainAuthenticated" and "Domain" in allowed):
            out.append({"sev": "!", "text": f"the firewall lets ssh in on {','.join(allowed)} networks only and this network is {cat}",
                        "fix": f"Set-NetConnectionProfile -InterfaceAlias '{primary_iface(inv).get('name')}' -NetworkCategory Private"})
        else:
            out.append({"sev": "", "text": f"the firewall lets ssh in on {','.join(allowed)} networks only: a new network joins as Public and blocks ssh",
                        "fix": "on a new network: set it to Private"})
    if win and fw.get("on") and not allowed and fw.get("ssh_rules") is not None:
        out.append({"sev": "", "text": "no enabled inbound firewall rule named *ssh* (ssh works through another rule?)", "fix": ""})
    a = inv.get("account") or {}
    pe = str(a.get("password_expires") or "")
    if pe and not re.match(r"(?i)never", pe):
        out.append({"sev": "", "text": f"the account password expires: {pe}", "fix": "renew it before; keys keep working, Windows logon does not"})
    if win and a.get("admin") and any("administrators" in m_.lower() for m_ in s.get("match") or []):
        k = inv.get("keys") or {}
        out.append({"sev": "", "text": "an admin account: sshd reads C:\\ProgramData\\ssh\\administrators_authorized_keys (not ~/.ssh)"
                    + ("" if k.get("authorized_admin") else " — not readable or empty here"), "fix": ""})
    i = primary_iface(inv)
    if i.get("kind") == "wifi":
        sig = (inv.get("net") or {}).get("wifi", {}).get("signal", "")
        out.append({"sev": "", "text": f"on Wi-Fi{(' (signal ' + sig + ')') if sig else ''}: drops, roaming", "fix": "an Ethernet cable is steadier"})
    if i.get("dhcp") and access and re.match(r"^\d+\.\d+\.\d+\.\d+$", str(access.get("hostname") or "")):
        out.append({"sev": "!", "text": f"ssh goes to the IP {access['hostname']} but the host gets it by DHCP (it can change)",
                    "fix": "a DHCP reservation, or reach it by name (mDNS .local)"})
    for g in inv.get("gpus_gone") or []:
        out.append({"sev": "", "text": f"GPU {g['name']} ({g.get('vram_gb')}G) is not attached (last seen {when(g.get('last_seen'))})",
                    "fix": "plug it (eGPU / dock) when a model needs it"})
    ev = inv.get("events") or []
    pl = [e for e in ev if e["what"] == "power-loss"]
    if pl:
        out.append({"sev": "", "text": f"unexpected shutdown / power loss at {when(pl[-1]['at'])}", "fix": "UPS; check the power supply"})
    day = [e for e in ev if e["what"] in ("sleep", "standby") and time.time() - core.epoch(e["at"]) < 86400]
    if len(day) >= 3:
        out.append({"sev": "", "text": f"went to sleep/standby {len(day)}× in the last 24 h", "fix": ""})
    return out


# ---------------------------------------------------------------- what this machine sees
_cache: Dict[str, Tuple[float, object]] = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


def ssh_config(host: str, ssh: str = "ssh") -> dict:
    """What the ssh client will do for <host> (ssh -G): hostname, port, user, identity files, proxy."""
    def get():
        rc, out, _ = run([ssh, "-G", host], 8)
        d: dict = {"identityfile": []}
        for l in out.splitlines():
            k, _, v = l.partition(" ")
            if k == "identityfile":
                d["identityfile"].append(os.path.expanduser(v))
            elif k in ("hostname", "port", "user", "proxyjump", "proxycommand", "hostkeyalias"):
                d[k] = v
        if not d.get("hostname"):
            u, _, h = host.rpartition("@")
            d.update(hostname=h, user=u, port="22")
        return d
    return _cached(("cfg", host, ssh), 30, get)


def resolve(name: str, timeout: float = 4.0) -> Tuple[List[str], str]:
    """IPv4 addresses of <name> ([], why) — mDNS .local names go through the system resolver."""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
        return [name], ""
    res: dict = {}

    def go():
        try:
            res["ips"] = sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
        except socket.gaierror as e:
            res["err"] = str(e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return [], f"no answer in {timeout:g}s"
    return res.get("ips", []), res.get("err", "")


def tcp(ip: str, port: int, timeout: float = 3.0) -> str:
    """open · refused · timeout · unreachable (…)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, int(port)))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return f"unreachable ({e.strerror or e})"
    finally:
        s.close()


def ping(ip: str) -> bool:
    if core.IS_WIN:
        cmd = ["ping", "-n", "1", "-w", "1500", ip]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", "2", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "2", ip]
    rc, _, _ = run(cmd, 5)
    return rc == 0


def arp_mac(ip: str) -> str:
    rc, out, _ = run(["arp", "-a", ip] if core.IS_WIN else ["arp", "-n", ip], 4)
    m = re.search(r"((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})", out)
    return mac_norm(m.group(1)) if m else ""


def local_net() -> dict:
    """The network this machine is on: interface, IP, gateway (and its MAC: a network's identity when its name is
    hidden), Wi-Fi name, VPN-like interfaces. label = what a human recognises."""
    def get():
        d: dict = {"iface": "", "ip": "", "gateway": "", "gw_mac": "", "ssid": "", "vpn": []}
        if sys.platform == "darwin":
            _, out, _ = run(["route", "-n", "get", "default"], 4)
            for l in out.splitlines():
                k, _, v = l.strip().partition(":")
                if k == "gateway":
                    d["gateway"] = v.strip()
                elif k == "interface":
                    d["iface"] = v.strip()
            if d["iface"]:
                d["ip"] = run(["ipconfig", "getifaddr", d["iface"]], 3)[1].strip()
                _, out, _ = run(["networksetup", "-getairportnetwork", d["iface"]], 4)
                m = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
                d["ssid"] = m.group(1).strip() if m else ""
            _, out, _ = run(["ifconfig"], 4)
            cur = None
            for l in out.splitlines():
                m = re.match(r"^([a-z0-9]+):", l)
                if m:
                    cur = m.group(1)
                elif cur and re.match(r"^(utun|tailscale|wg|ppp|ipsec)", cur) and l.strip().startswith("inet ") and not l.split()[1].startswith("169.254"):
                    d["vpn"].append(f"{cur} {l.split()[1]}")
        elif core.IS_WIN:
            _, out, _ = run(["powershell.exe", "-NoProfile", "-Command",
                             "$r=Get-NetRoute -DestinationPrefix '0.0.0.0/0'|Sort-Object RouteMetric|Select-Object -First 1;"
                             "$i=Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $r.ifIndex|Select-Object -First 1;"
                             "$p=Get-NetConnectionProfile -InterfaceIndex $r.ifIndex;"
                             "\"$($r.InterfaceAlias)|$($i.IPAddress)|$($r.NextHop)|$($p.Name)\""], 15)
            f = (out.strip().splitlines() or [""])[-1].split("|")
            if len(f) == 4:
                d.update(iface=f[0], ip=f[1], gateway=f[2], ssid=f[3])
        else:
            _, out, _ = run(["ip", "route", "show", "default"], 3)
            m = re.search(r"via (\S+) dev (\S+)", out)
            if m:
                d["gateway"], d["iface"] = m.group(1), m.group(2)
                _, o2, _ = run(["ip", "-o", "-4", "addr", "show", "dev", d["iface"]], 3)
                m2 = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", o2)
                d["ip"] = m2.group(1) if m2 else ""
            d["ssid"] = run(["iwgetid", "-r"], 3)[1].strip()
            _, o3, _ = run(["ip", "-o", "-4", "addr"], 3)
            d["vpn"] = [f"{l.split()[1]} {l.split()[3]}" for l in o3.splitlines() if re.match(r"^\d+:\s+(tun|wg|tailscale|ppp)", l)]
        if d["gateway"]:
            d["gw_mac"] = arp_mac(d["gateway"])
        d["label"] = " ".join(x for x in (f"'{d['ssid']}'" if d["ssid"] else "", d["iface"], d["ip"], f"gw {d['gateway']}" if d["gateway"] else "") if x) or "offline"
        d["sig"] = d["gw_mac"] or d["ssid"] or d["gateway"] or "offline"
        return d
    return _cached(("localnet",), 20, get)


def known_hostkeys(names: List[str]) -> List[str]:
    out = []
    for n in names:
        if not n:
            continue
        _, txt, _ = run(["ssh-keygen", "-F", n, "-l"], 5)
        out += [l.split()[2] for l in txt.splitlines() if l and not l.startswith("#") and len(l.split()) >= 3]
    return sorted(set(out))


def our_keys(cfg: dict) -> List[str]:
    """Fingerprints of the keys ssh offers: the agent's and the identity files that exist."""
    fps = []
    rc, out, _ = run(["ssh-add", "-l"], 4)
    if rc == 0:
        fps += [l.split()[1] for l in out.splitlines() if len(l.split()) > 1]
    for f in cfg.get("identityfile") or []:
        pub = f + ".pub"
        if os.path.isfile(pub):
            _, o, _ = run(["ssh-keygen", "-lf", pub], 4)
            if o.split()[1:2]:
                fps.append(o.split()[1])
    return sorted(set(fps))


def access(host: str, inv: Optional[dict] = None, ssh: str = "ssh") -> dict:
    """How this machine gets to <host>: the ssh target, the name resolved, the known host key, our keys and which of
    them the host authorises, the network this machine is on."""
    cfg = ssh_config(host, ssh)
    name = cfg.get("hostname") or host
    ips, err = resolve(name)
    mine = our_keys(cfg)
    a = {"hostname": name, "port": _int(cfg.get("port"), 22), "user": cfg.get("user", ""),
         "proxy": cfg.get("proxyjump") or cfg.get("proxycommand") or "", "resolved": ips, "resolve_err": err,
         "known_hostkeys": known_hostkeys([cfg.get("hostkeyalias") or "", name] + ([f"[{name}]:{cfg.get('port')}"] if str(cfg.get("port")) != "22" else [])),
         "our_keys": mine, "from": local_net().get("label", "")}
    if inv:
        auth = set((inv.get("keys") or {}).get("authorized") or []) | set((inv.get("keys") or {}).get("authorized_admin") or [])
        a["key_authorized"] = [k for k in mine if k in auth]
        hk = set((inv.get("keys") or {}).get("host") or [])
        a["hostkey_match"] = bool(hk & set(a["known_hostkeys"])) if hk and a["known_hostkeys"] else None
    return a


# ---------------------------------------------------------------- diagnosis
def quick_remote(host: str, rport: Optional[int], path: str, ssh: str) -> dict:
    """One ssh call: does the service answer on the host, and when did the host boot."""
    script = ""
    if rport:
        script += f"curl -s -m 4 -o /dev/null -w 'HTTP=%{{http_code}}' http://127.0.0.1:{int(rport)}{core.health_path(path)}; echo\n"
    script += "echo UP=$(cut -d' ' -f1 /proc/uptime 2>/dev/null || sysctl -n kern.boottime 2>/dev/null)\n"
    r = core.ssh_run(host, script, 25, ssh)
    out = {"ok": r["rc"] != 255, "err": core.ssh_error(r["err"], r["rc"]) if r["rc"] == 255 else "", "ms": r["ms"]}
    m = re.search(r"HTTP=(\d{3})", r["out"])
    out["code"] = m.group(1) if m else None
    m = re.search(r"UP=(.+)", r["out"])
    if m:
        v = m.group(1)
        sec = re.search(r"sec = (\d+)", v)
        if sec:
            out["boot"] = _epoch_iso(sec.group(1))
        elif _num(v):
            out["boot"] = _epoch_iso(time.time() - _num(v))
    return out


def diagnose(host: str, mem_host: dict, ssh: str = "ssh", rport: Optional[int] = None, kind: str = "",
             tunnel_log: str = "") -> dict:
    """Find the layer that broke between this machine and the service at <host>:<rport>.
    {layer, cause, evidence[], auto[], hands[], facts{}} — layers: local-net, dns, host-down, ssh-blocked, sshd-down,
    auth, hostkey, service, loading, tunnel, ok, unknown."""
    inv = mem_host.get("inventory") or {}
    cfg = ssh_config(host, ssh)
    name = cfg.get("hostname") or host
    sport = _int(cfg.get("port"), 22)
    ev: List[str] = []
    facts: dict = {"hostname": name}
    here = local_net()
    facts["local_net"] = here.get("label")
    rf = mem_host.get("reached_from") or {}
    last_from = rf.get("label") or ""
    moved = bool(rf.get("sig")) and rf.get("sig") != here.get("sig")
    if cfg.get("proxyjump") or cfg.get("proxycommand"):
        ev.append(f"ssh goes through a proxy ({cfg.get('proxyjump') or 'ProxyCommand'}): the checks below see the first hop only")
    ips, rerr = resolve(name)
    facts["resolved"] = ips
    known_ips = all_ips(inv)
    if ips:
        ev.append(f"{name} resolves to {', '.join(ips)}" + (f" (known: {', '.join(known_ips[:3])})" if known_ips and not set(ips) & set(known_ips) else ""))
    else:
        ev.append(f"{name} does not resolve ({rerr or 'no address'})" + (f"; last known IP {', '.join(known_ips[:3])}" if known_ips else ""))
    targets = ips or known_ips[:2]
    states = {}
    for ip in targets[:2]:
        states[ip] = tcp(ip, sport)
    facts["tcp"] = states
    ip = next((i for i, s in states.items() if s == "open"), next(iter(states), ""))
    st = states.get(ip, "")
    if ip:
        ev.append(f"TCP {ip}:{sport} → {st}")
    pinged = None
    mac = ""
    if st and st != "open":
        pinged = ping(ip)
        facts["ping"] = pinged
        ev.append(f"ping {ip} → {'answers' if pinged else 'no answer'}")
        mac = arp_mac(ip)
        facts["arp"] = mac
        macs = all_macs(inv)
        if mac and macs:
            ev.append(f"ARP {ip} → {mac} " + ("(the host's own interface)" if mac in macs else f"(NOT one of the host's MACs {', '.join(macs[:3])}: another device has this IP)"))
            facts["mac_match"] = mac in macs
    if moved and (not ips or st != "open"):
        ev.append(f"this machine is on {here.get('label')}; {host} was last reached from {last_from}")
    out = {"layer": "unknown", "cause": "", "evidence": ev, "auto": [], "hands": [], "facts": facts}
    model = (inv.get("machine") or {}).get("model") or ""
    laptop = (inv.get("machine") or {}).get("laptop")
    nb = net_brief(inv)
    p = inv.get("power") or {}
    lastev = [e for e in inv.get("events") or [] if e["what"] in ("sleep", "standby", "power-loss", "shutdown")]
    power_hint = []
    if p.get("sleep_ac_min"):
        power_hint.append(f"it sleeps after {p['sleep_ac_min']} min idle on AC")
    if lastev:
        power_hint.append(f"last {lastev[-1]['what']} {when(lastev[-1]['at'])}")

    def hands_power():
        h = [f"Power: is {host.split('@')[-1]} on{f' ({model})' if model else ''}? Power LED, fans"
             + ("; a laptop: plug the charger" + (f" (it was at {p.get('battery')}%)" if p.get("battery") is not None else "") if laptop else "")]
        h.append("Wake it: press a key, move the mouse or press the power button briefly"
                 + (f" — {'; '.join(power_hint)}" if power_hint else ""))
        if laptop and p.get("lid"):
            h.append(f"Lid: closed means {p['lid']} — open it")
        return h

    def hands_net():
        i = primary_iface(inv)
        if not i:
            return [f"Network: is it connected to the same network as this machine ({here.get('label')})?"]
        if i.get("kind") == "ethernet":
            return [f"Network: Ethernet cable plugged and its link light on ({i.get('name')}; it was {nb})"]
        return [f"Network: Wi-Fi connected to '{network_name(inv, i) or '?'}' ({nb}); in range?"]

    if moved and not st == "open":
        out.update(layer="local-net", cause=f"this machine changed network ({last_from} → {here.get('label')}); {host} is not reachable from here")
        out["hands"] = [f"Connect this machine back to the network {host} is on ({last_from}), or start the VPN that reaches it"] + hands_net()
        return out
    if not ips and not known_ips:
        out.update(layer="dns", cause=f"{name} does not resolve and no IP of it is known: the host is off this network, or off")
        out["hands"] = hands_power() + hands_net()
        return out
    if not ips and st == "open":
        out.update(layer="dns", cause=f"{name} does not resolve (mDNS/DNS) but its last IP {ip} answers ssh")
        out["auto"] = [f"use-ip {ip}"]
        out["hands"] = [f"Make the name resolve again (mDNS: is Bonjour/LLMNR allowed on {host}?), or reach it by IP: "
                        f"'HostName {ip}' for it in ~/.ssh/config"]
        return out
    if st == "refused":
        out.update(layer="sshd-down", cause=f"{ip} answers but no ssh server listens on {sport}: sshd stopped (or the host rebooted and sshd did not start)")
        s = inv.get("sshd") or {}
        out["hands"] = [("On the host: start the 'OpenSSH SSH Server' service (Services, or Start-Service sshd)" if (inv.get("os") or {}).get("system") == "Windows"
                         else "On the host: sudo systemctl start ssh") + (f"; it is set to start {s.get('start')}" if s.get("start") else "")]
        return out
    if st in ("timeout", "") or st.startswith("unreachable"):
        if pinged:
            cat = network_category(inv)
            out.update(layer="ssh-blocked", cause=f"{ip} answers ping but not ssh ({st}): a firewall blocks port {sport}, or sshd hangs")
            allowed = (inv.get("fw") or {}).get("ssh_allowed_on") or []
            h = []
            if allowed:
                h.append(f"On the host: is the network still {cat or 'Private'}? The firewall lets ssh in on {','.join(allowed)} only "
                         "(Settings → Network → the connection → Private)")
            h.append("On the host: restart the ssh server (Restart-Service sshd / systemctl restart ssh)")
            out["hands"] = h
            return out
        if facts.get("mac_match") is False:
            out.update(layer="host-down", cause=f"{ip} now belongs to another device ({mac}); {host} is off this network or got another IP")
        else:
            out.update(layer="host-down", cause=f"no answer from {host} ({ip}: {st}, no ping): it is off, asleep, or off the network"
                       + (f" — {'; '.join(power_hint)}" if power_hint else ""))
        out["hands"] = hands_power() + hands_net() + ["After a restart: log in if its services need a user session"]
        return out
    # ssh port open: try ssh itself
    r = quick_remote(host, rport, "/health" if kind == "llm" else "/", ssh)
    facts["ssh"] = "ok" if r["ok"] else r["err"]
    if not r["ok"]:
        e = r["err"]
        if "authentication" in e or "Permission denied" in e:
            out.update(layer="auth", cause=f"the host refused our key ({clip(e, 120)})")
            ak = (mem_host.get("access") or {}).get("key_authorized")
            out["hands"] = [f"On the host: is our key still in ~/.ssh/authorized_keys"
                            + (" (or C:\\ProgramData\\ssh\\administrators_authorized_keys for an admin)" if (inv.get("account") or {}).get("admin") else "")
                            + (f"? It was: {', '.join(ak)}" if ak else "?"),
                            "Account locked or its password expired? (keys keep working; a Windows logon may need the new password)",
                            "Or from here: ssh-copy-id " + host]
        elif "host key" in e:
            out.update(layer="hostkey", cause="the host key changed: the host was reinstalled, or another machine now has this name/IP")
            out["hands"] = [f"Check that {name} is really {model or 'the same machine'}; then ssh-keygen -R {name} and ssh once by hand to accept the new key"]
        else:
            out.update(layer="ssh-blocked", cause=f"ssh connects but fails: {clip(e, 160)}")
            out["hands"] = ["On the host: restart the ssh server"]
        return out
    ev.append(f"ssh ok ({r['ms']} ms)")
    if r.get("boot"):
        facts["boot"] = r["boot"]
        old = (inv.get("os") or {}).get("boot")
        if old and abs(core.epoch(r["boot"]) - core.epoch(old)) > 120:
            facts["rebooted"] = True
            ev.append(f"the host rebooted at {when(r['boot'])} (was up since {when(old)})")
    if rport:
        code = r.get("code") or "000"
        facts["service_code"] = code
        if code == "000":
            out.update(layer="service", cause=f"nothing answers on 127.0.0.1:{rport} on {host}: the service is down"
                       + (" (the host rebooted: a service started by hand is gone)" if facts.get("rebooted") else ""))
            out["auto"] = [f"launch {rport}"]
            out["hands"] = [f"Start the service on port {rport} on {host.split('@')[-1]} (or let me launch it from its recipe)"]
        elif code == "503":
            out.update(layer="loading", cause=f"the service on {host}:{rport} answers 503: still loading")
            out["auto"] = ["wait"]
        elif code.startswith("5"):
            out.update(layer="service", cause=f"the service on {host}:{rport} answers HTTP {code} (failing)")
            out["auto"] = [f"logs {rport}"]
        else:
            ev.append(f"on {host}: 127.0.0.1:{rport} answers HTTP {code}")
            if "administratively prohibited" in tunnel_log:
                out.update(layer="tunnel", cause="sshd refuses the port forwarding (AllowTcpForwarding)")
                out["hands"] = ["On the host: AllowTcpForwarding yes in sshd_config, then restart sshd"]
            else:
                out.update(layer="tunnel", cause=f"the service answers on {host} (HTTP {code}); the tunnel is what failed")
                out["auto"] = ["reopen"]
    else:
        out.update(layer="ok", cause=f"{host} is reachable over ssh")
    return out
