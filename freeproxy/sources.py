"""免费代理源：默认 13 源（承自原项目）+ 解析器（ipport / geonode-json）。

解析层安全红线（承自原版 H5 修复）：仅接受公网 IP:port，拒绝内网/回环/链路本地/
保留/多播地址与一切 hostname，防恶意源注入内网地址造成 SSRF。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re

_SCHEME_PREFIX_RE = re.compile(r"^(?:https?|socks[45]?|socks)://", re.IGNORECASE)

# (name, url, format, protocol)  protocol: 源声明的协议，用于验证前的代理 scheme
DEFAULT_SOURCES = [
    ("proxyscrape-v3-http", "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&proxy_format=ipport&format=text&timeout=5000&country=all", "ipport", "http"),
    ("proxyscrape-v3-ssl", "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=https&proxy_format=ipport&format=text&timeout=5000&country=all", "ipport", "http"),
    ("proxyscrape-v3-socks4", "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks4&proxy_format=ipport&format=text&timeout=5000&country=all", "ipport", "socks5"),
    ("proxyscrape-v3-socks5", "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&proxy_format=ipport&format=text&timeout=5000&country=all", "ipport", "socks5"),
    ("proxyscrape-v2", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all", "ipport", "http"),
    ("geonode", "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http", "geonode-json", "http"),
    ("proxifly-github", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt", "ipport", "http"),
    ("thespeedx-http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "ipport", "http"),
    ("ercindedeoglu-http", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt", "ipport", "http"),
    ("proxy4parsing-http", "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt", "ipport", "http"),
    ("monosans-http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "ipport", "http"),
    ("thespeedx-socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "ipport", "socks5"),
    ("thespeedx-socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "ipport", "socks5"),
]


def is_valid_public_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def parse_ipport(text: str, protocol: str = "http"):
    """解析 ip:port 文本行（兼容 scheme 前缀/注释/空白，去重），返回 [(host, port, protocol)]。"""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proto = protocol
        m = re.match(r"^(https?|socks[45]?)://", line, re.IGNORECASE)
        if m:
            line = _SCHEME_PREFIX_RE.sub("", line, count=1)
            p = m.group(1).lower()
            proto = "socks5" if p.startswith("socks") else "http"
        if line.count(":") != 1:
            continue
        host, port = line.rsplit(":", 1)
        if not host or not port.isdigit() or not (0 < int(port) < 65536):
            continue
        if not is_valid_public_ip(host):
            continue
        key = (host, int(port))
        if key in seen:
            continue
        seen.add(key)
        out.append((*key, proto))
    return out


def parse_geonode(text: str):
    """解析 geonode JSON（data[].ip/port/protocols）。"""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "").strip()
        port = str(item.get("port") or "").strip()
        if not ip or not port.isdigit() or not is_valid_public_ip(ip):
            continue
        protocols = item.get("protocols") or []
        proto = "socks5" if "socks5" in protocols else "http"
        out.append((ip, int(port), proto))
    return out


def parse_payload(text: str, fmt: str, protocol: str):
    if fmt == "ipport":
        return parse_ipport(text, protocol)
    if fmt == "geonode-json":
        return parse_geonode(text)
    return []


def load_sources():
    """返回 [(name, url, format, protocol)]。支持 FREPROXY_SOURCES_FILE 覆盖（JSON 数组）。"""
    path = os.environ.get("FREPROXY_SOURCES_FILE", "")
    if path:
        with open(path, encoding="utf-8") as f:
            arr = json.load(f)
        return [(str(s["name"]), str(s["url"]), str(s.get("format", "ipport")),
                 str(s.get("protocol", "http"))) for s in arr]
    return list(DEFAULT_SOURCES)
