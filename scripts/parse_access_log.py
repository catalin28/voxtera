#!/usr/bin/env python3
"""Parse Caddy JSON access logs to show IP addresses, dates, and pages accessed."""

import json
import os
import sys
from datetime import UTC, datetime

LOG_PATH = "/var/log/caddy/voxtera-access.log"


def parse_log(log_path, filter_status=None, filter_ip=None):
    """Parse the Caddy JSON access log."""
    with open(log_path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            request = entry.get("request", {})
            ip = request.get("remote_ip", "?")
            method = request.get("method", "?")
            uri = request.get("uri", "?")
            status = str(entry.get("status", "?"))
            ts = entry.get("ts", 0)

            dt = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

            headers = request.get("headers", {})
            ua_list = headers.get("User-Agent", [])
            user_agent = ua_list[0] if ua_list else ""

            if filter_status and status != filter_status:
                continue
            if filter_ip and ip != filter_ip:
                continue

            yield ip, dt, method, uri, status, user_agent


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parse Caddy access logs")
    parser.add_argument("logfile", nargs="?", default=LOG_PATH, help="Path to access log file")
    parser.add_argument("--ip", help="Filter by specific IP address")
    parser.add_argument("--status", help="Filter by HTTP status code (e.g. 200)")
    parser.add_argument("--last", type=int, help="Show only the last N entries")
    parser.add_argument("--summary", action="store_true", help="Show summary grouped by IP")
    parser.add_argument("--no-bots", action="store_true", help="Exclude known bots")
    args = parser.parse_args()

    log_path = args.logfile
    if not os.path.isfile(log_path):
        print(f"ERROR: File not found: {log_path}")
        sys.exit(1)

    print(f"Parsing: {log_path}\n")

    BOT_KEYWORDS = ["bot", "crawler", "spider", "GPTBot", "OAI-SearchBot", "Googlebot", "Bingbot"]

    if args.summary:
        ip_data = {}
        for ip, dt, method, uri, status, ua in parse_log(log_path, args.status, args.ip):
            if args.no_bots and any(b.lower() in ua.lower() for b in BOT_KEYWORDS):
                continue
            if ip not in ip_data:
                ip_data[ip] = {"count": 0, "pages": set(), "first_seen": dt, "last_seen": dt}
            ip_data[ip]["count"] += 1
            ip_data[ip]["pages"].add(uri)
            ip_data[ip]["last_seen"] = dt

        sorted_ips = sorted(ip_data.items(), key=lambda x: x[1]["count"], reverse=True)

        print(f"{'IP Address':<18} {'Hits':<6} {'First Seen':<22} {'Last Seen':<22} Pages")
        print("-" * 110)
        for ip, data in sorted_ips:
            pages = ", ".join(sorted(data["pages"])[:5])
            if len(data["pages"]) > 5:
                pages += f" ... (+{len(data['pages']) - 5} more)"
            print(
                f"{ip:<18} {data['count']:<6} {data['first_seen']:<22} {data['last_seen']:<22} {pages}"
            )
        print(f"\nTotal unique IPs: {len(ip_data)}")
    else:
        entries = list(parse_log(log_path, args.status, args.ip))

        if args.no_bots:
            entries = [
                (ip, dt, m, u, s, ua)
                for ip, dt, m, u, s, ua in entries
                if not any(b.lower() in ua.lower() for b in BOT_KEYWORDS)
            ]

        if args.last:
            entries = entries[-args.last :]

        print(f"{'Date/Time':<22} {'IP Address':<18} {'Status':<8} {'Page'}")
        print("-" * 90)
        for ip, dt, method, uri, status, ua in entries:
            print(f"{dt:<22} {ip:<18} {status:<8} {method} {uri}")

        print(f"\nTotal entries: {len(entries)}")


if __name__ == "__main__":
    main()
