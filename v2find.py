#!/usr/bin/env python3
"""
v2find.py - Find V2Ray configs matching specific address patterns

This script processes V2Ray configuration URIs from text files, filters them
based on server address patterns (with wildcard support), and outputs matching
configs. It supports domain resolution, deduplication, and output to file.

Usage:
  python v2find.py *.txt -addr 127.0.*
  python v2find.py *.txt -addr *.185 -o output.txt
  python v2find.py server1.txt server2.txt -addr 192.168.*.* -s
  python v2find.py *.txt -addr example.* -s -o found.txt

Installation of required packages:
  pip install python-v2ray rich
"""

import argparse
import socket
import ipaddress
import sys
import logging
import glob
import threading
import time
import signal
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import deque
from typing import Dict, List, Set, Tuple, Optional, Any

# Third-party imports with enhanced error handling
try:
    from python_v2ray.config_parser import load_configs, deduplicate_configs
    from rich.console import Console, Group
    from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
    from rich.live import Live
    from rich.table import Table
    from rich import box
except ImportError as e:
    missing = str(e).split("'")[1] if "'" in str(e) else "unknown"
    print(f"\n❌ Error: Missing required package: {missing}\n")
    print("Please install all dependencies with:\n")
    print("  pip install python-v2ray rich\n")
    print("If you already have them installed, ensure they are in your current Python environment.\n")
    sys.exit(1)

# Initialize console with standard colors
console = Console(color_system="standard")

# Suppress third-party warnings and verbose logging
import warnings

warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("python_v2ray").setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)

# Global shutdown flag for graceful interruption
shutdown_flag = threading.Event()
_current_live = None


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    global _current_live
    if not shutdown_flag.is_set():
        if _current_live is not None:
            _current_live.console.print("\n[magenta]✨ Received shutdown signal, finishing current tasks...[/magenta]")
        else:
            console.print("\n[magenta]✨ Received shutdown signal, finishing current tasks...[/magenta]")
        shutdown_flag.set()
    else:
        if _current_live is not None:
            _current_live.console.print("\n[magenta]✨ Force quitting...[/magenta]")
        else:
            console.print("\n[magenta]✨ Force quitting...[/magenta]")
        sys.exit(130)


# ============================================================================
# Thread-safe data structures
# ============================================================================

class ThreadSafeCounter:
    """Thread-safe counter for accumulating integer statistics."""

    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def increment(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class ThreadSafeList:
    """Thread-safe list for collecting matching configs."""

    def __init__(self):
        self._list = []
        self._lock = threading.Lock()

    def append(self, item: str) -> None:
        with self._lock:
            self._list.append(item)

    def get_list(self) -> List[str]:
        with self._lock:
            return list(self._list)

    def __len__(self) -> int:
        with self._lock:
            return len(self._list)


# ============================================================================
# Core filtering and resolution functions
# ============================================================================

def address_matches_pattern(address: str, pattern: str, resolved_ip: Optional[ipaddress._BaseAddress] = None) -> bool:
    """
    Check if an address matches the given pattern with wildcard support.

    Patterns support:
    - Wildcards: * matches any sequence of characters
    - IP patterns: 127.0.*, *.185, 192.168.*.*
    - Domain patterns: example.*, *.example.com

    If the address is a domain and resolved_ip is provided, both are checked.
    """
    if fnmatch.fnmatch(address, pattern):
        return True

    if resolved_ip:
        ip_str = str(resolved_ip)
        if fnmatch.fnmatch(ip_str, pattern):
            return True

    return False


def resolve_domain_to_ip(domain: str, timeout: int = 5, retry_count: int = 3,
                         dns_lookup_counter: Optional[ThreadSafeCounter] = None) -> Optional[ipaddress._BaseAddress]:
    """
    Resolve a domain name to an IP address with retries and timeout.
    If the input is already an IP, it is returned immediately.
    """
    try:
        return ipaddress.ip_address(domain)
    except ValueError:
        pass

    if dns_lookup_counter:
        dns_lookup_counter.increment()

    max_attempts = retry_count + 1
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(1)
        try:
            socket.setdefaulttimeout(timeout)
            ipv4 = socket.gethostbyname(domain)
            return ipaddress.IPv4Address(ipv4)
        except socket.gaierror:
            try:
                addr_info = socket.getaddrinfo(domain, None)
                for family, _, _, _, sockaddr in addr_info:
                    if family == socket.AF_INET:
                        return ipaddress.IPv4Address(sockaddr[0])
                    elif family == socket.AF_INET6:
                        return ipaddress.IPv6Address(sockaddr[0])
            except (socket.error, socket.timeout, IndexError):
                if attempt == max_attempts - 1:
                    return None
                continue
        except (socket.error, socket.timeout):
            if attempt == max_attempts - 1:
                return None
            continue
    return None


def expand_file_patterns(patterns: List[str]) -> List[Path]:
    """Expand glob patterns to actual file paths."""
    files = []
    for pattern in patterns:
        if shutdown_flag.is_set():
            break
        matched = glob.glob(pattern)
        if not matched:
            files.append(Path(pattern))
        else:
            for p in matched:
                path = Path(p)
                if path.is_file():
                    files.append(path)
    return list(dict.fromkeys(files))


def create_stats_table(stats: Dict, elapsed_time: float = 0,
                       processing_speed: float = 0, total_configs: int = 0,
                       completed_count: int = 0, matched_count: int = 0) -> Table:
    """Build a Rich Table with real-time statistics."""
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1), border_style="cyan")
    table.add_column("Metric", style="blue", width=25)
    table.add_column("Value", style="white")

    if total_configs > 0:
        percent = (completed_count / total_configs) * 100
        table.add_row("Progress", f"[cyan]{percent:.1f}% ({completed_count:,}/{total_configs:,})[/cyan]")

    table.add_row("Total Configs", f"[white]{stats.get('total_lines', 0):,}[/white]")
    table.add_row("Matched", f"[green]{matched_count:,}[/green]")

    if stats.get('dns_lookups', 0) > 0:
        table.add_row("DNS Lookups", f"[cyan]{stats.get('dns_lookups', 0):,}[/cyan]")

    if stats.get('dns_errors', 0) > 0 and completed_count > 0:
        error_pct = (stats['dns_errors'] / completed_count) * 100
        table.add_row("Error (DNS)", f"[red]{stats.get('dns_errors', 0):,} ({error_pct:.1f}%)[/red]")

    if stats.get('invalid_lines', 0) > 0:
        table.add_row("Invalid", f"[yellow]{stats.get('invalid_lines', 0):,}[/yellow]")

    if processing_speed > 0:
        table.add_row("Speed", f"[blue]{processing_speed:.1f} configs/s[/blue]")

    if elapsed_time > 0:
        elapsed_str = f"{elapsed_time:.1f}s" if elapsed_time < 60 else f"{elapsed_time / 60:.1f}m"
        table.add_row("Time", f"[white]{elapsed_str}[/white]")

    return table


def get_config_key(config) -> Tuple:
    """Generate a unique key for a config for deduplication."""
    base = (config.address, config.port, config.protocol)
    if hasattr(config, 'id'):
        return base + (config.id,)
    if hasattr(config, 'password'):
        return base + (config.password,)
    return base


def is_ip_address(addr: str) -> bool:
    """Check if a string is a valid IP address."""
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


def process_config_line(args: Tuple) -> Optional[str]:
    """Worker function: resolve domain, check pattern match, collect matching configs."""
    config, original_line, pattern, resolve_dns, shared_state, results_list = args
    if shutdown_flag.is_set():
        return None

    dns_errors = shared_state["dns_errors_counter"]
    dns_lookups = shared_state["dns_lookups_counter"]
    invalid_count = shared_state["invalid_counter"]
    processed_counter = shared_state["processed_counter"]

    address = config.address
    port = config.port if hasattr(config, 'port') else None

    if not address or port is None:
        invalid_count.increment()
        return None

    resolved_ip = None
    if resolve_dns:
        resolved_ip = resolve_domain_to_ip(address, timeout=5, retry_count=1,
                                           dns_lookup_counter=dns_lookups)
        if resolved_ip is None and not is_ip_address(address):
            dns_errors.increment()
            processed_counter.increment()
            return None

    if address_matches_pattern(address, pattern, resolved_ip):
        results_list.append(original_line)

    processed_counter.increment()
    return original_line if address_matches_pattern(address, pattern, resolved_ip) else None


# ============================================================================
# Main processing function
# ============================================================================

def process_files(files: List[Path], pattern: str, resolve_dns: bool = True,
                  skip_duplicates: bool = False, workers: int = 4) -> Tuple[List[str], Dict]:
    """Process all input files, filter configs by address pattern using multi-threading."""
    global _current_live

    stats = {
        "total_files": 0,
        "processed_files": 0,
        "total_lines": 0,
        "processed_configs": ThreadSafeCounter(),
        "dns_lookups": ThreadSafeCounter(),
        "dns_errors": ThreadSafeCounter(),
        "invalid_lines": ThreadSafeCounter(),
        "duplicate_lines": 0,
    }

    if not files:
        console.print("[yellow]⚠️ No files to process[/yellow]")
        return [], {}

    all_lines = []
    console.print(f"\n[blue]Reading {len(files):,} file(s)...[/blue]")
    for file_path in files:
        if shutdown_flag.is_set():
            break
        stats["total_files"] += 1
        if not file_path.exists():
            console.print(f"[red]❌ File not found: {file_path}[/red]")
            continue
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = [line.rstrip('\n') for line in f if line.strip()]
            if not lines:
                continue
            stats["processed_files"] += 1
            stats["total_lines"] += len(lines)
            all_lines.extend(lines)
            console.print(f"  [cyan]📄[/cyan] {file_path.name}: [white]{len(lines):,}[/white] configs")
        except Exception as e:
            console.print(f"[red]Error reading {file_path.name}: {e}[/red]")

    if shutdown_flag.is_set() or not all_lines:
        return [], {}

    stats["total_lines"] = len(all_lines)
    console.print(f"\n[green]✨ Total configs to process: {stats['total_lines']:,}[/green]")

    console.print(f"\n[magenta]Parsing {stats['total_lines']:,} configs...[/magenta]")
    valid_configs = []
    invalid_count = 0
    for line in all_lines:
        if shutdown_flag.is_set():
            break
        try:
            parsed = load_configs(source=[line], is_subscription=False)
            if parsed:
                valid_configs.append((parsed[0], line))
            else:
                invalid_count += 1
        except Exception:
            invalid_count += 1
    stats["invalid_lines"].increment(invalid_count)

    if skip_duplicates and not shutdown_flag.is_set():
        console.print(f"[blue]Deduplicating {len(valid_configs)} valid configs...[/blue]")
        unique_map = {}
        for config, line in valid_configs:
            key = get_config_key(config)
            if key not in unique_map:
                unique_map[key] = (config, line)
        configs_to_dedup = [cfg for cfg, _ in unique_map.values()]
        unique_configs = deduplicate_configs(configs_to_dedup)
        unique_pairs = []
        for cfg in unique_configs:
            key = get_config_key(cfg)
            if key in unique_map:
                unique_pairs.append(unique_map[key])
        stats["duplicate_lines"] = len(valid_configs) - len(unique_pairs)
        valid_configs = unique_pairs
        console.print(f"[yellow]Removed {stats['duplicate_lines']:,} duplicate configs[/yellow]")
    else:
        stats["duplicate_lines"] = 0

    if shutdown_flag.is_set() or not valid_configs:
        return [], {}

    total_tasks = len(valid_configs)

    shared_state = {
        "dns_errors_counter": stats["dns_errors"],
        "dns_lookups_counter": stats["dns_lookups"],
        "invalid_counter": stats["invalid_lines"],
        "processed_counter": stats["processed_configs"],
    }

    results_list = ThreadSafeList()

    console.print(f"\n[magenta]Starting processing with {workers} workers...[/magenta]")
    console.print(f"[blue]Filter pattern: [cyan]{pattern}[/cyan][/blue]")
    if resolve_dns:
        console.print("[blue]DNS resolution: enabled[/blue]")
    console.print("[blue]Press Ctrl+C to interrupt\n[/blue]")

    start_time = time.time()
    speed_history = deque()

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="Worker") as executor:
            task_args = [(config, line, pattern, resolve_dns, shared_state, results_list)
                         for config, line in valid_configs]
            futures = [executor.submit(process_config_line, args) for args in task_args]

            completed_count = 0
            last_update_time = start_time

            progress = Progress(
                TextColumn("[magenta]Processing..."),
                BarColumn(bar_width=None, complete_style="cyan", finished_style="blue"),
                TextColumn("[white][progress.percentage]{task.percentage:>3.0f}%[/white]"),
                TextColumn("•"),
                TextColumn("[cyan]{task.completed}/{task.total}[/cyan]"),
                TextColumn("•"),
                TimeElapsedColumn(),
                TextColumn("•"),
                TimeRemainingColumn(),
                console=console,
                expand=True,
            )
            progress_task = progress.add_task("", total=total_tasks)

            with Live(console=console, refresh_per_second=4, vertical_overflow="visible") as live:
                _current_live = live
                try:
                    current_stats = {
                        "total_lines": stats["total_lines"],
                        "dns_lookups": 0,
                        "dns_errors": 0,
                        "invalid_lines": stats["invalid_lines"].value,
                    }
                    live.update(
                        Group(create_stats_table(current_stats, 0, 0, total_tasks, 0, len(results_list)), progress))

                    for future in as_completed(futures):
                        if shutdown_flag.is_set():
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        completed_count += 1
                        now = time.time()
                        speed_history.append((now, completed_count))
                        while speed_history and now - speed_history[0][0] > 60:
                            speed_history.popleft()
                        progress.update(progress_task, completed=completed_count)

                        if now - last_update_time >= 0.5 or completed_count % 50 == 0 or completed_count == total_tasks:
                            elapsed = now - start_time
                            current_stats = {
                                "total_lines": stats["total_lines"],
                                "dns_lookups": stats["dns_lookups"].value,
                                "dns_errors": stats["dns_errors"].value,
                                "invalid_lines": stats["invalid_lines"].value,
                            }
                            if len(speed_history) >= 2:
                                oldest_time, oldest_count = speed_history[0]
                                window = now - oldest_time
                                speed = (completed_count - oldest_count) / window if window > 0 else 0
                            else:
                                speed = completed_count / elapsed if elapsed > 0 else 0
                            live.update(Group(
                                create_stats_table(current_stats, elapsed, speed, total_tasks, completed_count,
                                                   len(results_list)), progress))
                            last_update_time = now

                        try:
                            future.result(timeout=1)
                        except Exception:
                            pass

                    if not shutdown_flag.is_set():
                        elapsed = time.time() - start_time
                        current_stats = {
                            "total_lines": stats["total_lines"],
                            "dns_lookups": stats["dns_lookups"].value,
                            "dns_errors": stats["dns_errors"].value,
                            "invalid_lines": stats["invalid_lines"].value,
                        }
                        avg_speed = completed_count / elapsed if elapsed > 0 else 0
                        progress.update(progress_task, completed=total_tasks)
                        live.update(Group(
                            create_stats_table(current_stats, elapsed, avg_speed, total_tasks, completed_count,
                                               len(results_list)), progress))
                        time.sleep(0.5)
                finally:
                    _current_live = None

    except KeyboardInterrupt:
        shutdown_flag.set()
        console.print("\n[yellow]⚠️ Processing interrupted[/yellow]")

    elapsed_time = time.time() - start_time
    if elapsed_time > 0 and total_tasks > 0:
        avg_speed = total_tasks / elapsed_time
        console.print(f"\n[blue]Average speed: {avg_speed:.1f} configs/second[/blue]")
        console.print(f"[white]Total time: {elapsed_time:.1f} seconds[/white]")

    final_stats = {
        "total_files": stats["total_files"],
        "processed_files": stats["processed_files"],
        "total_lines": stats["total_lines"],
        "processed_configs": stats["processed_configs"].value,
        "dns_lookups": stats["dns_lookups"].value,
        "dns_errors": stats["dns_errors"].value,
        "invalid_lines": stats["invalid_lines"].value,
        "duplicate_lines": stats["duplicate_lines"],
        "matched_count": len(results_list),
    }

    return results_list.get_list(), final_stats


def print_final_statistics(stats: Dict, skip_duplicates: bool = False) -> None:
    """Print final summary."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]✨ FINAL RESULTS[/bold magenta]")
    console.print("=" * 60)

    summary = Table(box=box.ROUNDED, show_header=False, border_style="cyan")
    summary.add_column("Metric", style="blue", width=25)
    summary.add_column("Value", style="white")

    if stats["total_lines"] > 0:
        match_rate = (stats["matched_count"] / stats["total_lines"]) * 100
        summary.add_row("Match Rate", f"[green]{match_rate:.1f}%[/green]")

    summary.add_row("Matched Configs", f"[green]{stats['matched_count']:,}[/green]")
    summary.add_row("Total Configs", f"[blue]{stats['total_lines']:,}[/blue]")

    if stats["dns_lookups"] > 0:
        summary.add_row("DNS Lookups", f"[cyan]{stats['dns_lookups']:,}[/cyan]")
    if stats["dns_errors"] > 0 and stats["total_lines"] > 0:
        error_pct = (stats["dns_errors"] / stats["total_lines"]) * 100
        summary.add_row("Error (DNS)", f"[red]{stats['dns_errors']:,} ({error_pct:.1f}%)[/red]")
    if stats["invalid_lines"] > 0:
        summary.add_row("Invalid Lines", f"[yellow]{stats['invalid_lines']:,}[/yellow]")
    if stats["duplicate_lines"] > 0:
        summary.add_row("Duplicate Lines", f"[yellow]{stats['duplicate_lines']:,}[/yellow]")

    console.print(summary)


# ============================================================================
# Main entry point
# ============================================================================

def main():
    """Parse arguments and orchestrate processing."""
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(
        description="Find V2Ray configs matching specific address patterns",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s *.txt -addr 127.0.*
  %(prog)s *.txt -addr *.185
  %(prog)s server1.txt server2.txt -addr 192.168.*.* -s
  %(prog)s *.txt -addr example.* -o output.txt
  %(prog)s configs/*.txt -addr 10.* -s -w 8 -o found.txt

Pattern Examples:
  127.0.*        - Addresses starting with 127.0.
  *.185          - Addresses ending with .185
  192.168.*.*    - Any IP in 192.168.x.x range
  example.*      - Domains starting with example.
  *.example.com  - Subdomains of example.com
"""
    )

    parser.add_argument("files", nargs="+", type=str,
                        help="Text files with V2Ray URIs (one per line). Supports glob patterns like *.txt")

    filter_group = parser.add_argument_group("Filter Options")
    filter_group.add_argument("-addr", "--address", type=str, required=True,
                              help="Address pattern to match (supports wildcards: *)")
    filter_group.add_argument("--no-resolve", action="store_true",
                              help="Disable DNS resolution for domain addresses")

    proc_group = parser.add_argument_group("Processing Options")
    proc_group.add_argument("-w", "--workers", type=int, default=4,
                            help="Number of worker threads (default: 4, max: 32)")
    proc_group.add_argument("-s", "--skip-duplicates", action="store_true",
                            dest="skip_duplicates",
                            help="Skip configs with duplicate server:port combinations")

    out_group = parser.add_argument_group("Output Options")
    out_group.add_argument("-o", "--output", type=Path, default=None,
                           help="Output file to save matched configs (default: print to stdout)")

    args = parser.parse_args()

    if args.workers < 1 or args.workers > 32:
        console.print("[red]❌ Error: Workers must be between 1 and 32[/red]")
        sys.exit(1)

    console.print("[bold magenta]🔍 V2Ray Config Finder[/bold magenta]")
    console.print("[blue]" + "=" * 60 + "[/blue]")
    console.print(f"[green]✓ Filter pattern: [cyan]{args.address}[/cyan][/green]")
    console.print(f"[green]✓ Using {args.workers} worker threads[/green]")
    if args.skip_duplicates:
        console.print("[blue]✓ Skipping duplicate configs[/blue]")
    if args.no_resolve:
        console.print("[yellow]⚠ DNS resolution disabled[/yellow]")
    else:
        console.print("[green]✓ DNS resolution enabled[/green]")

    console.print("\n[blue]📁 Searching for files...[/blue]")
    expanded_files = expand_file_patterns(args.files)
    if not expanded_files:
        console.print("[red]❌ No files found to process[/red]")
        sys.exit(1)
    console.print(f"[green]✨ Found {len(expanded_files):,} file(s)[/green]")

    console.print("\n[bold magenta]⚡ Starting processing...[/bold magenta]")
    matched_configs, stats = process_files(
        files=expanded_files,
        pattern=args.address,
        resolve_dns=not args.no_resolve,
        skip_duplicates=args.skip_duplicates,
        workers=args.workers
    )

    if not stats or stats.get("total_lines", 0) == 0:
        console.print("[yellow]⚠️ Processing was interrupted or no configs found[/yellow]")
        sys.exit(1)

    print_final_statistics(stats, args.skip_duplicates)

    console.print("\n" + "=" * 60)
    if matched_configs:
        if args.output:
            try:
                with open(args.output, 'w', encoding='utf-8') as f:
                    for config in matched_configs:
                        f.write(config + '\n')
                console.print(f"[green]✨ Success! {len(matched_configs):,} configs saved to: {args.output}[/green]")
            except IOError as e:
                console.print(f"[red]❌ Error writing to output file: {e}[/red]")
                sys.exit(1)
        else:
            console.print(f"[green]✨ Found {len(matched_configs):,} matching configs:[/green]")
            console.print("-" * 60)
            for config in matched_configs:
                console.print(f"[white]{config}[/white]")
    else:
        console.print("[yellow]⚠️ No configs matched the filter pattern[/yellow]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Interrupted by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]❌ Error: {str(e)[:200]}...[/red]")
        console.print("[yellow]💡 If the error persists, please check your input files and dependencies.[/yellow]")
        sys.exit(1)