#!/usr/bin/env python3
"""Parse nginx access logs to show IP addresses, dates, and pages accessed."""

import os
import re
import sys

# Common nginx access log locations
LOG_PATHS = [
    "/var/log/nginx/access.log",
    "/var/log/nginx/access.log.1",
    "/var/log/httpd/access_log",
    "/var/log/apache2/access.log",
    "/usr/local/var/log/nginx/access.log",
]

# Regex for nginx combined log format:
# IP - - [date] "METHOD /path HTTP/x.x" status size "referer" "user-agent"
LOG_PATTERN = re.compile(
    r"^(\S+)\s+"  # IP address
    r"\S+\s+"  # ident (usually -)
    r"\S+\s+"  # auth user (usually -)
    r"\[([^\]]+)\]\s+"  # date/time
    r'"(\S+)\s+(\S+)\s+\S+"\s+'  # method and path
    r"(\d+)\s+"  # status code
    r"\S+"  # bytes
)


def find_log_file():
    """Find the nginx access log file."""
    for path in LOG_PATHS:
        if os.path.isfile(path):
            return path
    return None


def parse_log(log_path, filter_status=None, filter_ip=None):
    """Parse the access log and yield (ip, datetime, method, path, status)."""
    with open(log_path, errors="replace") as f:
        for line in f:
            match = LOG_PATTERN.match(line)
            if not match:
                continue
            ip, datetime_str, method, path, status = match.groups()
            if filter_status and status != filter_status:
                continue
            if filter_ip and ip != filter_ip:
                continue
            yield ip, datetime_str, method, path, status


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parse nginx access logs")
    parser.add_argument(
        "logfile", nargs="?", help="Path to access log file (auto-detected if omitted)"
    )
    parser.add_argument("--ip", help="Filter by specific IP address")
    parser.add_argument("--status", help="Filter by HTTP status code (e.g. 200)")
    parser.add_argument("--last", type=int, help="Show only the last N entries")
    parser.add_argument("--summary", action="store_true", help="Show summary grouped by IP")
    args = parser.parse_args()

    log_path = args.logfile
    if not log_path:
        log_path = find_log_file()
        if not log_path:
            print("ERROR: Could not find nginx access log.")
            print("Searched:", "\n  ".join(LOG_PATHS))
            print("\nSpecify the log file path as an argument:")
            print(f"  python3 {sys.argv[0]} /path/to/access.log")
            sys.exit(1)

    if not os.path.isfile(log_path):
        print(f"ERROR: File not found: {log_path}")
        sys.exit(1)

    print(f"Parsing: {log_path}\n")

    if args.summary:
        # Group by IP and show pages accessed
        ip_data = {}
        for ip, dt, _method, path, _status in parse_log(log_path, args.status, args.ip):
            if ip not in ip_data:
                ip_data[ip] = {"count": 0, "pages": set(), "last_seen": dt}
            ip_data[ip]["count"] += 1
            ip_data[ip]["pages"].add(path)
            ip_data[ip]["last_seen"] = dt

        # Sort by request count descending
        sorted_ips = sorted(ip_data.items(), key=lambda x: x[1]["count"], reverse=True)

        print(f"{'IP Address':<20} {'Requests':<10} {'Last Seen':<28} Pages")
        print("-" * 100)
        for ip, data in sorted_ips:
            pages = ", ".join(sorted(data["pages"])[:5])
            if len(data["pages"]) > 5:
                pages += f" ... (+{len(data['pages']) - 5} more)"
            print(f"{ip:<20} {data['count']:<10} {data['last_seen']:<28} {pages}")
        print(f"\nTotal unique IPs: {len(ip_data)}")
    else:
        # Detailed line-by-line output
        entries = list(parse_log(log_path, args.status, args.ip))

        if args.last:
            entries = entries[-args.last :]

        print(f"{'Date/Time':<28} {'IP Address':<20} {'Status':<8} {'Page'}")
        print("-" * 100)
        for ip, dt, method, path, status in entries:
            print(f"{dt:<28} {ip:<20} {status:<8} {method} {path}")

        print(f"\nTotal entries: {len(entries)}")


if __name__ == "__main__":
    main()
